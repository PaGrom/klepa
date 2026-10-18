"""The sandbox that every attachment reader runs in (spec 8: sandbox-exec, no network). macOS only: elsewhere Core
reads no attachment at all."""

import asyncio
import dataclasses
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

import klepa_core
from fakereaders import processing_config
from klepa_core.processing.sandbox import (
    DENIED_SERVICES,
    OUTPUT_TAIL,
    SHARED_FOLDERS,
    ProcessingStopped,
    ProcessingTimeout,
    Sandbox,
    sandbox_available,
    sandbox_for,
    user_folder,
)

pytestmark = pytest.mark.skipif(not sandbox_available(), reason="sandbox-exec exists on macOS only")
PYTHON = sys.executable


@pytest.fixture
def sandbox(tmp_path):
    home = tmp_path / "home"
    (home / "allowed").mkdir(parents=True)
    (home / "allowed" / "data.txt").write_text("readable")
    (home / "secret.txt").write_text("not for a reader")
    job = tmp_path / "jobs" / "1"
    job.mkdir(parents=True)
    return Sandbox(hidden=(home,), readable=(home / "allowed",)), job


def run(sandbox_and_job, code, timeout=30.0):
    sandbox, job = sandbox_and_job
    return asyncio.run(sandbox.run([PYTHON, "-c", code], job, timeout=timeout))


def test_a_reader_writes_in_its_job_folder_and_nowhere_else(sandbox, tmp_path):
    _, job = sandbox
    code = (
        f"open({str(job / 'out.txt')!r}, 'w').write('ok')\n"
        f"try:\n    open({str(tmp_path / 'outside.txt')!r}, 'w')\nexcept PermissionError:\n    print('denied')\n"
    )
    result = run(sandbox, code)
    assert result.returncode == 0, result.stderr
    assert (job / "out.txt").read_text() == "ok"
    assert not (tmp_path / "outside.txt").exists()
    assert "denied" in result.stdout


def test_a_reader_cannot_read_the_home_folder_beyond_what_it_needs(sandbox, tmp_path):
    home = tmp_path / "home"
    code = (
        f"print(open({str(home / 'allowed' / 'data.txt')!r}).read())\n"
        f"try:\n    open({str(home / 'secret.txt')!r}).read()\nexcept PermissionError:\n    print('denied')\n"
    )
    result = run(sandbox, code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["readable", "denied"]


def test_a_reader_has_no_network(sandbox):
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        code = (
            "import socket\n"
            f"try:\n    socket.create_connection(('127.0.0.1', {port}), timeout=2)\n    print('connected')\n"
            "except OSError as exc:\n    print('refused', type(exc).__name__)\n"
        )
        result = run(sandbox, code)
    assert result.stdout.startswith("refused"), result.stdout + result.stderr


def test_a_readers_output_is_read_from_cores_own_file_never_through_a_link(sandbox, tmp_path):
    """A reader may replace the file its output goes to with a link: Core reads the file it opened (final review,
    C1)."""
    secret = tmp_path / "home" / "secret.txt"
    code = f"import os\nprint('the reader said this')\nos.unlink('.stdout')\nos.symlink({str(secret)!r}, '.stdout')\n"
    result = run(sandbox, code)
    assert "not for a reader" not in result.stdout
    assert result.stdout.strip() == "the reader said this"


def test_a_link_left_where_the_output_goes_is_replaced_never_written_through(sandbox, tmp_path):
    """An earlier step's reader may leave a link where the next step's output goes: opened for writing, it would
    empty the file it points to."""
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    job = sandbox[1]
    (job / ".stdout").symlink_to(victim)
    (job / ".stderr").symlink_to(victim)
    result = run(sandbox, "print('hello')")
    assert victim.read_text() == "precious"
    assert result.stdout.strip() == "hello"


def test_a_pipe_left_where_the_output_goes_does_not_stop_core(sandbox):
    """A pipe in place of the output file would keep Core's read waiting forever, and all of Core with it."""
    results = []
    code = "import os\nos.unlink('.stdout')\nos.mkfifo('.stdout')\nprint('done')\n"
    thread = threading.Thread(target=lambda: results.append(run(sandbox, code)), daemon=True)
    thread.start()
    thread.join(15)
    if thread.is_alive():  # free the open that waits, so that the test run ends
        os.close(os.open(sandbox[1] / ".stdout", os.O_WRONLY | os.O_NONBLOCK))
        thread.join(5)
    assert [result.stdout.strip() for result in results] == ["done"]


def test_a_reader_that_overruns_is_killed_with_its_children(sandbox):
    _, job = sandbox
    code = (
        "import subprocess, time\n"
        f"child = subprocess.Popen(['/bin/sleep', '30'])\nopen({str(job / 'child.pid')!r}, 'w').write(str(child.pid))\n"
        "time.sleep(30)\n"
    )
    started = time.monotonic()
    with pytest.raises(ProcessingTimeout):
        run(sandbox, code, timeout=1.0)
    assert time.monotonic() - started < 10
    child = int((job / "child.pid").read_text())
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


def test_the_profile_names_real_paths(tmp_path):
    """The sandbox matches the real path: /var is /private/var on macOS, and a rule for /var matches nothing."""
    link = tmp_path / "link"
    link.symlink_to(tmp_path)
    profile = Sandbox(hidden=(link / "home",), readable=(link / "data",)).profile(link / "job")
    assert str(link) not in profile
    assert str(tmp_path.resolve() / "job") in profile


def test_a_reader_leaves_no_core_dump_and_writes_no_file_past_the_limit(tmp_path):
    """A core dump would hold the file's content; one huge output must not fill the disk (spec 8: rlimit)."""
    job = tmp_path / "jobs" / "1"
    job.mkdir(parents=True)
    sandbox = Sandbox(hidden=(tmp_path / "home",), max_file_bytes=1_000_000)
    code = "import resource\nprint(resource.getrlimit(resource.RLIMIT_CORE), resource.getrlimit(resource.RLIMIT_FSIZE))"
    result = asyncio.run(sandbox.run([PYTHON, "-c", code], job, timeout=30))
    assert result.stdout.split("\n")[0] == "(0, 0) (1000000, 1000000)"
    dd = ["/bin/dd", "if=/dev/zero", f"of={job / 'big'}", "bs=100000", "count=30"]
    result = asyncio.run(sandbox.run(dd, job, timeout=30))
    assert result.returncode != 0
    assert (job / "big").stat().st_size <= 1_000_000


def test_a_reader_may_resolve_the_paths_it_may_read_but_not_list_the_folders_above(tmp_path):
    """A framework build of Python (Homebrew's) resolves its environment's folder when it starts, stat by stat up to
    the root: the folders above what a reader may read answer stat, though never a listing."""
    home = tmp_path / "home"
    lib = home / "Developer" / "engine" / "lib"
    lib.mkdir(parents=True)
    (lib / "module.py").write_text("x = 1")
    (home / "Developer" / "secret.txt").write_text("not for a reader")
    job = tmp_path / "jobs" / "1"
    job.mkdir(parents=True)
    code = (
        "import os\n"
        f"print(os.path.realpath({str(lib / 'module.py')!r}, strict=True))\n"
        f"try:\n    print(os.listdir({str(home / 'Developer')!r}))\n"
        "except PermissionError:\n    print('listing refused')\n"
    )
    result = asyncio.run(Sandbox(hidden=(home,), readable=(lib,)).run([PYTHON, "-c", code], job, timeout=30))
    assert result.stdout.split("\n")[:2] == [os.path.realpath(lib / "module.py"), "listing refused"], result.stderr


def test_the_data_and_documents_folders_are_hidden_wherever_they_are(tmp_path):
    """The data folder holds the keys and every original: a reader broken by a planted file must not read them, even
    when the folder is not in the home folder. Its own job's folder, inside it, stays readable."""
    data = tmp_path / "data"
    (data / "keys").mkdir(parents=True)
    (data / "keys" / "family-bot.token").write_text("123:SECRET")
    job = data / "jobs" / "1"
    job.mkdir(parents=True)
    (job / "input.txt").write_text("the copy")
    sandbox = Sandbox(hidden=(tmp_path / "home", data))
    code = (
        f"print(open({str(job / 'input.txt')!r}).read())\n"
        f"try:\n    open({str(data / 'keys' / 'family-bot.token')!r}).read()\nexcept PermissionError:\n"
        "    print('denied')\n"
    )
    result = asyncio.run(sandbox.run([PYTHON, "-c", code], job, timeout=30))
    assert result.stdout.split() == ["the", "copy", "denied"], result.stderr


def test_only_a_step_on_the_gpu_may_write_the_shared_shader_cache(tmp_path):
    """Metal's shader cache is shared by every app of the user: whisper writes it, the parsers of what people send
    never do."""
    metal = tmp_path / "metal"
    metal.mkdir()
    job = tmp_path / "jobs" / "1"
    job.mkdir(parents=True)
    sandbox = Sandbox(hidden=(tmp_path / "home",), metal=(metal,))
    code = f"open({str(metal / 'cache')!r}, 'w').write('x')"
    assert asyncio.run(sandbox.run([PYTHON, "-c", code], job, timeout=30)).returncode != 0
    assert asyncio.run(sandbox.run([PYTHON, "-c", code], job, timeout=30, gpu=True)).returncode == 0


def test_an_installations_sandbox_hides_its_private_folders_and_reads_what_readers_need(tmp_path):
    processing = processing_config(tmp_path)
    data, documents = tmp_path / "data", tmp_path / "documents"
    sandbox = sandbox_for(processing, (data, documents))
    assert {Path.home(), data, documents, user_folder(), *SHARED_FOLDERS} == set(sandbox.hidden)
    assert sandbox.private == (data, documents)  # hidden again after the readable ones
    assert {processing.tessdata, processing.whisper, processing.whisper_model, Path(sys.prefix)} <= set(
        sandbox.readable
    )
    assert processing.whisper.parent not in sandbox.readable  # the program, not its folder
    assert Path(klepa_core.__file__).resolve().parents[1] in sandbox.readable  # Core's reader of spreadsheets
    assert sandbox.executable is not None
    assert {processing.whisper, processing.textutil, Path("/opt/homebrew")} <= set(sandbox.executable)


def test_a_step_on_the_gpu_may_look_at_the_cache_folder_itself_not_into_it(tmp_path):
    """Metal checks the user's cache folder before it opens its own two: without that, whisper compiles its shaders
    for twenty seconds on every run. The other apps' caches in that folder stay hidden."""
    cache = tmp_path / "C"
    sandbox = Sandbox(hidden=(tmp_path,), metal=(cache / "com.apple.metal", cache / "com.apple.metalfe"))
    profile = sandbox.profile(tmp_path / "job", gpu=True)
    assert f'(allow file-read* (literal "{cache.resolve()}"))' in profile
    assert f'(literal "{cache.resolve()}")' not in sandbox.profile(tmp_path / "job")


def test_the_sandbox_counts_as_available_only_when_it_applies_a_profile(monkeypatch, tmp_path):
    """sandbox-exec may exist and still refuse: inside another sandbox, or once Apple retires it."""
    from klepa_core.processing import sandbox as module

    module.sandbox_available.cache_clear()
    assert module.sandbox_available()
    refusing = tmp_path / "sandbox-exec"
    refusing.write_text("#!/bin/sh\nexit 71\n")
    refusing.chmod(0o755)
    monkeypatch.setattr(module, "SANDBOX_EXEC", refusing)
    module.sandbox_available.cache_clear()
    try:
        assert not module.sandbox_available()
    finally:
        monkeypatch.undo()
        module.sandbox_available.cache_clear()


def test_no_link_clone_or_rename_reaches_a_hidden_file(tmp_path):
    """The job's folder is on the same volume as the keys: a reader must not make a hidden file appear in it."""
    data = tmp_path / "data"
    (data / "keys").mkdir(parents=True)
    secret = data / "keys" / "family-bot.token"
    secret.write_text("123:SECRET")
    job = data / "jobs" / "1"
    job.mkdir(parents=True)
    code = f"""
import ctypes, os
secret, job = {str(secret)!r}, {str(job)!r}
libc = ctypes.CDLL(None, use_errno=True)
def clone(source, target):
    if libc.clonefile(source.encode(), target.encode(), 0):
        raise OSError(ctypes.get_errno(), "clonefile")
def through_symlink(source, target):
    os.symlink(source, target)
    return open(target).read()
for name, attempt in (("link", os.link), ("symlink", through_symlink), ("clone", clone), ("rename", os.rename)):
    try:
        attempt(secret, job + "/" + name)
        print(name, open(job + "/" + name).read())
    except OSError as exc:
        print(name, "refused", exc.errno)
"""
    result = asyncio.run(Sandbox(hidden=(tmp_path / "home", data)).run([PYTHON, "-c", code], job, timeout=30))
    assert "SECRET" not in result.stdout, result.stdout
    refused = [f"{name} refused 1" for name in ("link", "symlink", "clone", "rename")]  # EPERM: the sandbox said no
    assert result.stdout.split("\n")[:4] == refused
    assert secret.read_text() == "123:SECRET"


def test_an_installations_sandbox_hides_the_shared_and_temporary_folders(tmp_path):
    """Other apps keep temporary files in /private/tmp and the user folders; other people's homes are in /Users."""
    sandbox = sandbox_for(processing_config(tmp_path), (tmp_path / "data",))
    assert set(SHARED_FOLDERS) <= set(sandbox.hidden)
    shared = Path(tempfile.mkdtemp(prefix="klepa-test-", dir="/private/tmp"))
    try:
        (shared / "other-app.txt").write_text("another app's file")
        job = tmp_path / "jobs" / "1"
        job.mkdir(parents=True)
        code = f"print(open({str(shared / 'other-app.txt')!r}).read())"
        python = (Path(sys.prefix), Path(os.path.realpath(sys.base_prefix)).parent)  # the test's own, as Core's
        result = asyncio.run(Sandbox(hidden=SHARED_FOLDERS, readable=python).run([PYTHON, "-c", code], job, timeout=30))
        assert "another app" not in result.stdout
        assert "PermissionError" in result.stderr
    finally:
        shutil.rmtree(shared)


def test_a_reader_cannot_signal_another_process(sandbox):
    """A reader broken by a planted file must not stop Core, the gateway or anything else of the user's."""
    other = subprocess.Popen(["/bin/sleep", "30"])
    try:
        code = (
            f"import os, signal\ntry:\n    os.kill({other.pid}, signal.SIGTERM)\n"
            "except PermissionError:\n    print('refused')"
        )
        assert run(sandbox, code).stdout.strip() == "refused"
        assert other.poll() is None
    finally:
        other.kill()
        other.wait()


def test_only_the_programs_named_may_run(tmp_path):
    job = tmp_path / "jobs" / "1"
    job.mkdir(parents=True)
    # A framework build of Python (Homebrew's) starts the interpreter inside its installation (Resources/Python.app)
    interpreter = Path(os.path.realpath(sys.base_prefix))
    sandbox = Sandbox(hidden=(tmp_path / "home",), executable=(interpreter, Path(sys.prefix)))
    code = (
        "import subprocess\n"
        "for program in ('/bin/sleep', '/usr/bin/osascript', '/bin/sh'):\n"
        "    try:\n        subprocess.run([program, '0'], check=False)\n        print(program, 'ran')\n"
        "    except PermissionError:\n        print(program, 'refused')\n"
    )
    result = asyncio.run(sandbox.run([PYTHON, "-c", code], job, timeout=30))
    assert result.stdout.split("\n")[:3] == ["/bin/sleep refused", "/usr/bin/osascript refused", "/bin/sh refused"]


def test_a_readable_folder_never_reopens_a_private_one(tmp_path):
    """A rule that allows a folder wins over the one that hid what is inside it: the data folder is hidden again."""
    data = tmp_path / "data"
    (data / "keys").mkdir(parents=True)
    (data / "keys" / "family-bot.token").write_text("123:SECRET")
    job = data / "jobs" / "1"
    job.mkdir(parents=True)
    (job / "input.txt").write_text("the copy")
    sandbox = Sandbox(hidden=(tmp_path / "home", data), readable=(tmp_path,), private=(data,))
    code = (
        f"print(open({str(job / 'input.txt')!r}).read())\n"
        f"try:\n    open({str(data / 'keys' / 'family-bot.token')!r}).read()\n"
        "except PermissionError:\n    print('denied')\n"
    )
    assert asyncio.run(sandbox.run([PYTHON, "-c", code], job, timeout=30)).stdout.split() == ["the", "copy", "denied"]


def test_an_installation_whose_readable_folder_holds_a_hidden_one_is_refused(tmp_path):
    """A speech model kept in the home folder itself is fine (the file is readable, not its folder); OCR languages
    configured as the home folder would open all of it."""
    processing = processing_config(tmp_path)
    model = Path.home() / "a-speech-model-file.bin"  # never created: only its path matters
    assert model in sandbox_for(dataclasses.replace(processing, whisper_model=model), ()).readable
    with pytest.raises(ValueError, match="would open"):
        sandbox_for(dataclasses.replace(processing, tessdata=Path.home()), ())


def test_an_installation_that_keeps_what_readers_need_in_cores_own_folders_is_refused(tmp_path):
    """Core's data and documents folders are hidden again after what readers may read: a speech model or OCR languages
    kept there would fail every job, so Core says so at start instead."""
    processing = processing_config(tmp_path)
    data = tmp_path / "data"
    with pytest.raises(ValueError, match="inside"):
        sandbox_for(dataclasses.replace(processing, whisper_model=data / "models" / "speech.bin"), (data,))
    with pytest.raises(ValueError, match="inside"):
        sandbox_for(dataclasses.replace(processing, tessdata=data / "tessdata"), (tmp_path / "documents", data))


def test_a_reader_that_fills_its_folder_is_stopped(tmp_path):
    """One file has its limit; the job's folder has one too, in bytes and in files, or many files fill the disk."""
    job = tmp_path / "jobs" / "1"
    job.mkdir(parents=True)
    sandbox = Sandbox(hidden=(tmp_path / "home",), max_job_bytes=2_000_000, max_job_files=200)
    many_bytes = (
        "import time\nfor n in range(40):\n    open(f'part{n}', 'wb').write(b'0' * 200_000)\n    time.sleep(0.05)"
    )
    with pytest.raises(ProcessingStopped, match="more than"):
        asyncio.run(sandbox.run([PYTHON, "-c", many_bytes], job, timeout=30))
    many_files = "import time\nfor n in range(2000):\n    open(f'f{n}', 'w').close()\n    time.sleep(0.001)"
    with pytest.raises(ProcessingStopped, match="more than"):
        asyncio.run(sandbox.run([PYTHON, "-c", many_files], job, timeout=30))


def test_a_flood_of_output_stays_in_the_jobs_folder(tmp_path):
    """What a reader prints goes to files in its folder; Core keeps only the tail."""
    job = tmp_path / "jobs" / "1"
    job.mkdir(parents=True)
    code = "import sys\nfor _ in range(200):\n    sys.stdout.write('x' * 100_000)\nprint('the end')"
    result = asyncio.run(Sandbox(hidden=(tmp_path / "home",)).run([PYTHON, "-c", code], job, timeout=60))
    assert result.returncode == 0
    assert len(result.stdout) <= OUTPUT_TAIL
    assert result.stdout.endswith("the end\n")


def test_a_reader_reaches_no_service_that_opens_shares_or_unlocks(sandbox):
    """LaunchServices would open an address in a browser, past the sandbox's network rule; the pasteboard, Apple events
    and the keychain hold or move the user's data."""
    code = (
        "import ctypes\nlibc = ctypes.CDLL(None)\nport = ctypes.c_uint()\n"
        "bootstrap = ctypes.c_uint.in_dll(libc, 'bootstrap_port')\n"
        f"for name in {list(DENIED_SERVICES)!r}:\n"
        "    print(name, libc.bootstrap_look_up(bootstrap, name.encode(), ctypes.byref(port)))\n"
    )
    answers = dict(line.rsplit(" ", 1) for line in run(sandbox, code).stdout.splitlines())
    assert set(answers) == set(DENIED_SERVICES)
    assert "0" not in answers.values()  # 0: found; the sandbox answers 1100


def test_a_reader_gets_cores_five_variables_and_its_steps_own_alone(tmp_path, monkeypatch):
    """Core's environment may hold what no reader should see (finding M6)."""
    import json

    monkeypatch.setenv("KLEPA_TEST_SECRET", "not for a reader")
    job = tmp_path / "jobs" / "1"
    job.mkdir(parents=True)
    code = "import json, os; print(json.dumps(sorted(os.environ)))"
    sandbox = Sandbox(hidden=(tmp_path / "home",))
    result = asyncio.run(sandbox.run([PYTHON, "-c", code], job, timeout=30, env={"TESSDATA_PREFIX": "/nowhere"}))
    names = [name for name in json.loads(result.stdout) if name != "__CF_USER_TEXT_ENCODING"]  # macOS adds it
    assert names == ["HOME", "LANG", "LC_ALL", "PATH", "TESSDATA_PREFIX", "TMPDIR"]
