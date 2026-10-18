"""The sandbox every attachment reader runs in (spec 8: sandbox-exec, no network).

A reader is a program that parses a file someone sent: ffmpeg, whisper, pdftotext, tesseract, ocrmypdf, textutil.
It runs with no network, writes only in its job's folder, signals no other process, and runs no program but the
readers. It reads nothing in the hidden folders (the home folder, Core's data and documents folders, other people's
homes, the shared and temporary folders, the logs) but its job's folder and what the readers need, and of the folders
above those, their own entries alone (stat, never a listing). Only a step on the
GPU, whisper's, may also use Metal's shader cache, which every app of the user shares: with it whisper starts in
under a second instead of twenty, and it reads only the WAV that ffmpeg made. What a reader prints goes to files in
its folder, and a watchdog stops a reader that fills its folder. The sandbox matches real paths, so every path in the
profile is resolved first: /var is /private/var on macOS.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import os
import signal
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..config import ProcessingConfig

HOMEBREW = Path("/opt/homebrew")

SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
OUTPUT_TAIL = 4000  # what Core reads of a reader's output, from the end: its text goes to files of its own
MAX_FILE_BYTES = 1 << 30  # no file a reader writes grows past this: one output must not fill the disk
MAX_JOB_BYTES = 2 << 30  # nor all of them together
MAX_JOB_FILES = 10_000
WATCH_SECONDS = 0.5
# Folders where other apps and other people keep files: hidden from every reader, with the home folder and the user's
# own temporary folders. /Users holds every home, /Volumes every other disk.
# The services that reach past the sandbox: LaunchServices opens files, apps and addresses (a browser would carry what
# a reader read to the network), the pasteboard, Apple events, the keychain, the window server and the Dock. No reader
# needs them.
DENIED_SERVICES = (
    "com.apple.coreservices.launchservicesd",
    "com.apple.lsd.mapdb",
    "com.apple.lsd.modifydb",
    "com.apple.pasteboard.1",
    "com.apple.coreservices.appleevents",
    "com.apple.SecurityServer",
    "com.apple.securityd.xpc",
    "com.apple.windowserver.active",
    "com.apple.dock.server",
)
SHARED_FOLDERS = tuple(
    Path(folder)
    for folder in ("/Users", "/Volumes", "/private/tmp", "/private/var/tmp", "/private/var/log", "/Library/Logs")
)
# The limits a reader starts with (spec 8: rlimit), set by Core's own interpreter, which then becomes sandbox-exec: no
# core dump, since one would hold the file's content, and no file past the size. Python ignores SIGPIPE and SIGXFSZ,
# and a program it starts would inherit that: they are put back first. Memory has no limit that macOS enforces; the
# limits on pages, pixels and seconds bound it instead.
_LIMITS = (
    "import os, resource, signal, sys\n"
    "signal.signal(signal.SIGPIPE, signal.SIG_DFL)\n"
    "signal.signal(signal.SIGXFSZ, signal.SIG_DFL)\n"
    "resource.setrlimit(resource.RLIMIT_CORE, (0, 0))\n"
    "size = int(sys.argv[1])\n"
    "resource.setrlimit(resource.RLIMIT_FSIZE, (size, size))\n"
    "os.execv(sys.argv[2], sys.argv[2:])\n"
)


class ProcessingStopped(Exception):
    """A reader was killed, with every process it started: it ran past its time or wrote past its folder's limits.
    The text names the program and the limit, nothing it printed."""


class ProcessingTimeout(ProcessingStopped):
    """A reader ran past its time."""


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str
    stderr: str


@functools.cache
def sandbox_available() -> bool:
    """sandbox-exec is here and applies a profile: it may exist and still refuse, inside another sandbox or once
    Apple retires it. Asked once per process."""
    if sys.platform != "darwin" or not SANDBOX_EXEC.exists():
        return False
    try:
        probe = subprocess.run(
            [str(SANDBOX_EXEC), "-p", "(version 1)(allow default)", "/usr/bin/true"], capture_output=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


def _user_dir(name: str) -> Path | None:
    try:
        answer = subprocess.run(["/usr/bin/getconf", name], capture_output=True, text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(os.path.realpath(answer.stdout.strip())) if answer.stdout.strip() else None


def metal_caches() -> tuple[Path, ...]:
    """Where Metal keeps compiled shaders for this user: whisper writes there on its first run."""
    base = _user_dir("DARWIN_USER_CACHE_DIR")
    return () if base is None else (base / "com.apple.metal", base / "com.apple.metalfe")


def user_folder() -> Path:
    """The user's own temporary and cache folders (/private/var/folders/…/): other apps' temporary files are there."""
    temp = _user_dir("DARWIN_USER_TEMP_DIR")
    return temp.parent if temp is not None else Path(os.path.realpath("/private/var/folders"))


def _quote(path: Path) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _real(path: Path) -> Path:
    return Path(os.path.realpath(path))


def _subpaths(paths: Sequence[Path]) -> str:
    return " ".join(f"(subpath {_quote(_real(path))})" for path in paths)


def _usage(folder: Path, stop_at: int) -> tuple[int, int]:
    """Bytes and entries below a folder; counting stops once the entries pass `stop_at`."""
    size = count = 0
    for root, folders, files in os.walk(folder):
        for name in (*folders, *files):
            count += 1
            with contextlib.suppress(OSError):
                size += os.lstat(os.path.join(root, name)).st_size
        if count > stop_at:
            break
    return size, count


def _tail(path: Path) -> str:
    with path.open("rb") as file:
        file.seek(0, os.SEEK_END)
        file.seek(max(0, file.tell() - 4 * OUTPUT_TAIL))
        return file.read().decode("utf-8", "replace")[-OUTPUT_TAIL:]


@dataclass(frozen=True)
class Sandbox:
    hidden: tuple[Path, ...]  # nothing below these is readable but the job's folder and `readable`
    readable: tuple[Path, ...] = ()  # what readers need: the languages, the speech model, the programs, their Python
    private: tuple[Path, ...] = ()  # hidden again after `readable`, so no readable folder opens them: Core's own
    executable: tuple[Path, ...] | None = None  # the only programs a reader may start; None: any
    metal: tuple[Path, ...] = field(default_factory=metal_caches)  # for a step on the GPU only
    max_file_bytes: int = MAX_FILE_BYTES
    max_job_bytes: int = MAX_JOB_BYTES
    max_job_files: int = MAX_JOB_FILES

    def profile(self, job_dir: Path, *, gpu: bool = False) -> str:
        cache = self.metal if gpu else ()
        writes = _subpaths((job_dir, *cache)) + ' (literal "/dev/null") (literal "/dev/dtracehelper")'
        rules = [
            "(version 1)",
            "(allow default)",
            "(deny network*)",
            "(deny signal (target others))",
            "(deny mach-lookup " + " ".join(f'(global-name "{name}")' for name in DENIED_SERVICES) + ")",
            f"(deny file-write* (require-not (require-any {writes})))",
            f"(deny file-read* {_subpaths(self.hidden)})",
        ]
        if self.readable or cache:
            rules.append(f"(allow file-read* {_subpaths((*self.readable, *cache))})")
        if self.readable:
            # A framework build of Python (Homebrew's) resolves its environment's folder when it starts, stat by stat
            # up to the root: the folders above what a reader may read answer stat, never a listing.
            above = sorted({folder for path in self.readable for folder in _real(path).parents})
            rules.append("(allow file-read-metadata " + " ".join(f"(literal {_quote(f)})" for f in above) + ")")
        if self.private:
            rules.append(f"(deny file-read* {_subpaths(self.private)})")
        rules.append(f"(allow file-read* {_subpaths((job_dir,))})")  # last: the job's folder is in the data folder
        # Metal looks at the cache folder itself before it opens its own two: without that, whisper compiles its
        # shaders for twenty seconds on every run. The other apps' caches in it stay hidden.
        rules += [
            f"(allow file-read* (literal {_quote(_real(folder))}))" for folder in sorted({p.parent for p in cache})
        ]
        if self.executable is not None:
            rules += ["(deny process-exec*)", f"(allow process-exec* {_subpaths(self.executable)})"]
        return "\n".join(rules)

    async def run(
        self,
        argv: Sequence[str],
        job_dir: Path,
        *,
        timeout: float,
        env: Mapping[str, str] | None = None,
        gpu: bool = False,
    ) -> Result:
        """Run one reader in the job's folder, with that folder as its home and temporary directory."""
        job = _real(job_dir)
        (job / "tmp").mkdir(mode=0o700, exist_ok=True)
        environment = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
            "HOME": str(job),
            "TMPDIR": str(job / "tmp"),
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
            **(env or {}),
        }
        name = Path(argv[0]).name
        out_path, err_path = job / ".stdout", job / ".stderr"
        with out_path.open("wb") as out, err_path.open("wb") as err:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-S",
                "-c",
                _LIMITS,
                str(self.max_file_bytes),
                str(SANDBOX_EXEC),
                "-p",
                self.profile(job, gpu=gpu),
                *argv,
                cwd=job,
                env=environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                start_new_session=True,  # its own process group: a timeout kills every process it started
            )
        try:
            await asyncio.wait_for(self._watch(process, job, name), timeout)
        except TimeoutError:
            await _kill(process)
            raise ProcessingTimeout(f"{name} ran longer than {timeout:.0f} s") from None
        except BaseException:  # stopped by the watchdog, or Core is stopping
            await asyncio.shield(_kill(process))
            raise
        assert process.returncode is not None
        return Result(process.returncode, _tail(out_path), _tail(err_path))

    async def _watch(self, process: asyncio.subprocess.Process, job: Path, name: str) -> None:
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), WATCH_SECONDS)
                return
            size, count = await asyncio.to_thread(_usage, job, self.max_job_files)
            if size > self.max_job_bytes or count > self.max_job_files:
                raise ProcessingStopped(
                    f"{name} wrote more than {self.max_job_bytes >> 20} MB or {self.max_job_files} files"
                )


async def _kill(process: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    await process.wait()


def sandbox_for(processing: ProcessingConfig, private: Sequence[Path]) -> Sandbox:
    """The sandbox of an installation. Hidden: the home folder, the shared folders, the user's own temporary folders
    and `private` (Core's data and documents folders, wherever they are), which are hidden again after the readable
    ones. Readable there: the programs and the speech model (the files, not their folders), the OCR languages, and the
    environment and interpreter that run ocrmypdf (finding 5: the whole folder of uv's interpreters, for its
    version-less links). Only Homebrew's programs, the configured ones, Apple's textutil, and that interpreter
    may run. Refuses (ValueError) a readable folder that is or holds a hidden one, which would open it, and anything
    readers need kept in `private`, which is hidden again after it: every job would fail."""
    programs = (
        processing.whisper,
        processing.ffmpeg,
        processing.ffprobe,
        processing.pdftotext,
        processing.tesseract,
        processing.ocrmypdf,
        processing.textutil,
    )
    interpreters = Path(os.path.realpath(sys.base_prefix)).parent
    core = Path(__file__).resolve().parents[2]  # the folder that holds klepa_core: Core's own reader of spreadsheets
    readable = [processing.tessdata, Path(sys.prefix), interpreters, core, *programs]
    if processing.whisper_model is not None:
        readable.append(processing.whisper_model)
    hidden = (Path.home(), *SHARED_FOLDERS, *private, user_folder())
    for entry in readable:
        for folder in hidden:
            if _real(folder) == _real(entry) or _real(entry) in _real(folder).parents:
                raise ValueError(f"{entry.name or entry} would open a hidden folder to the readers")
        if any(_real(folder) in _real(entry).parents for folder in private):
            raise ValueError(f"{entry.name} is inside Core's own folders, which the readers never see")
    executable = (HOMEBREW, Path(sys.prefix), interpreters, *programs)
    return Sandbox(hidden=hidden, readable=tuple(readable), private=tuple(private), executable=executable)
