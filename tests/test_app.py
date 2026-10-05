from helpers import BASE_CONFIG
from klepa_core import app
from klepa_core.host.gateway import LaunchdGateway
from klepa_core.host.runtime import adapter_sha256


def test_the_open_file_limit_is_raised_from_launchds_256(monkeypatch):
    limits = {"soft": 256, "hard": app.resource.RLIM_INFINITY}

    def getrlimit(which):
        return limits["soft"], limits["hard"]

    def setrlimit(which, values):
        limits["soft"] = values[0]

    monkeypatch.setattr(app.resource, "getrlimit", getrlimit)
    monkeypatch.setattr(app.resource, "setrlimit", setrlimit)
    assert app.raise_file_limit() == app.OPEN_FILES
    limits.update(soft=256, hard=1024)
    assert app.raise_file_limit() == 1024
    limits.update(soft=20000, hard=app.resource.RLIM_INFINITY)
    assert app.raise_file_limit() == 20000  # never lowered


def test_a_launchd_host_gets_cores_own_gateway_held_to_the_reference(make_config, short_dir, tmp_path):
    section = f'\n[host]\nsocket = "{short_dir / "run" / "adapter.sock"}"\nruntime_dir = "{tmp_path / "runtime"}"\n'
    cfg = make_config(text=BASE_CONFIG + section)
    expect, control = app._gateway(cfg, 2**51 + 3)
    assert isinstance(control, LaunchdGateway)
    assert control.port == 19300
    assert expect is not None
    assert expect.plugin_sha256 == adapter_sha256()
    assert expect.policy["gateway.reload.mode"] == "off"
    assert expect.policy["channels.telegram.allowFrom"] == ["111111", "222222", str(2**51 + 3)]
    assert (expect.model, expect.runtime, expect.version) == ("anthropic/claude-sonnet-5", "openclaw", "2026.9.4")


def test_a_gateway_somebody_else_runs_is_gated_by_its_heartbeat_alone(make_config, short_dir):
    section = f'\n[host]\nsocket = "{short_dir / "run" / "adapter.sock"}"\ngateway = "external"\n'
    assert app._gateway(make_config(text=BASE_CONFIG + section), 2**51 + 3) == (None, None)
