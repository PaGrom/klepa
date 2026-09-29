import plistlib
import stat
import subprocess

import pytest

from klepa_core import macos
from klepa_core.config import ConfigError


class FakeRun:
    """Stands in for launchctl, tmutil and the interpreter check."""

    def __init__(self, fail=(), *, failures=1000, stdout=""):
        self.calls: list[list[str]] = []
        self.fail = set(fail)
        self.failures = failures  # how many calls of a failing command fail before one works
        self.stdout = stdout

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        seen = sum(call[1] == argv[1] for call in self.calls)
        code = 1 if argv[1] in self.fail and seen <= self.failures else 0
        return subprocess.CompletedProcess(argv, code, stdout=self.stdout, stderr="boom" if code else "")


def no_sleep(seconds):
    return None


def test_install_checks_first_then_writes_the_agent_and_bootstraps_it(make_config, tmp_path):
    cfg = make_config()
    run = FakeRun()
    python = tmp_path / "venv" / "bin" / "python"
    config = tmp_path / "config.toml"
    plist_path = macos.install(cfg, config, python, agents_dir=tmp_path / "agents", run=run, uid=501)
    plist = plistlib.loads(plist_path.read_bytes())
    assert plist["Label"] == "klepa.core"
    assert "ProcessType" not in plist
    assert plist["ThrottleInterval"] == 60
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["RunAtLoad"] is True
    assert plist["ProgramArguments"] == [str(python), "-m", "klepa_core", "run", "--config", str(config.resolve())]
    logs = cfg.data_dir / "logs"
    assert stat.S_IMODE(logs.stat().st_mode) == 0o700
    for name in ("core.out.log", "core.err.log"):
        assert stat.S_IMODE((logs / name).stat().st_mode) == 0o600
    assert run.calls == [
        [str(python), "-c", "import klepa_core"],
        ["launchctl", "bootout", "gui/501/klepa.core"],
        ["launchctl", "bootstrap", "gui/501", str(plist_path)],
    ]


def test_install_retries_bootstrap_while_launchd_tears_the_old_agent_down(make_config, tmp_path):
    run = FakeRun({"bootstrap"}, failures=2)
    config, python = tmp_path / "c.toml", tmp_path / "python"
    macos.install(make_config(), config, python, agents_dir=tmp_path, run=run, uid=501, sleep=no_sleep)
    assert [call[1] for call in run.calls].count("bootstrap") == 3


def test_install_reports_a_failed_bootstrap(make_config, tmp_path):
    config, python = tmp_path / "c.toml", tmp_path / "python"
    run = FakeRun({"bootstrap"})
    with pytest.raises(macos.ServiceError, match="boom"):
        macos.install(make_config(), config, python, agents_dir=tmp_path, run=run, uid=501, sleep=no_sleep)


def test_install_refuses_an_interpreter_without_the_engine(make_config, tmp_path):
    config, python = tmp_path / "c.toml", tmp_path / "python"
    with pytest.raises(macos.ServiceError, match="cannot import klepa_core"):
        macos.install(make_config(), config, python, agents_dir=tmp_path, run=FakeRun({"-c"}), uid=501)
    assert not (tmp_path / "klepa.core.plist").exists()


def test_install_refuses_a_token_file_core_could_not_read(make_config, install, tmp_path):
    install["token_file"].chmod(0o644)
    config, python = tmp_path / "c.toml", tmp_path / "python"
    with pytest.raises(ConfigError, match="0600"):
        macos.install(make_config(), config, python, agents_dir=tmp_path, run=FakeRun(), uid=501)


def test_uninstall_restart_and_status(tmp_path):
    (tmp_path / "klepa.core.plist").write_text("x")
    run = FakeRun()
    macos.uninstall(agents_dir=tmp_path, run=run, uid=501)
    macos.restart(run=run, uid=501)
    assert run.calls == [
        ["launchctl", "bootout", "gui/501/klepa.core"],
        ["launchctl", "kickstart", "-k", "gui/501/klepa.core"],
    ]
    assert not (tmp_path / "klepa.core.plist").exists()
    printed = "gui/501/klepa.core = {\n\tstate = running\n\tpid = 4242\n\tendpoints = {\n\t\tstate = active\n\t}\n}"
    assert macos.status(run=FakeRun(stdout=printed), uid=501) == "running, pid 4242"
    assert macos.status(run=FakeRun({"print"}), uid=501) == "not installed"


def test_keys_are_excluded_from_time_machine_and_a_failure_is_reported(tmp_path):
    run = FakeRun()
    assert macos.exclude_from_time_machine(tmp_path, run=run) is None
    assert run.calls == [["tmutil", "addexclusion", str(tmp_path)]]
    assert macos.exclude_from_time_machine(tmp_path, run=FakeRun({"addexclusion"})) == "boom"
