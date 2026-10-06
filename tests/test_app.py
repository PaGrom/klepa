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


def test_a_person_hears_in_the_installs_language_when_an_original_did_not_go(make_config, short_dir, tmp_path):
    """send_original answers "sent" when Core queues the file; a send that then fails is told to the person."""
    import json

    from helpers import MEMBER
    from klepa_core import db
    from klepa_core.alerts import Alerts
    from klepa_core.events import EventLog
    from klepa_core.gatekeeper.outbox import Outbox
    from klepa_core.journal import InboundJournal

    section = f'\n[host]\nsocket = "{short_dir / "run" / "adapter.sock"}"\nruntime_dir = "{tmp_path / "runtime"}"\n'
    cfg = make_config(text=BASE_CONFIG + section)
    app.init_layout(cfg)
    conn = db.connect(cfg.core_db_path)
    db.migrate(conn)
    events = EventLog(conn)
    outbox = Outbox(conn, None, events)
    journal = InboundJournal(tmp_path / "journal.db")
    try:
        app._host(cfg, conn, journal, None, outbox, Alerts(conn, None, events, cfg.locale), events, "123:abc", None)
        assert outbox.on_failed is not None
        outbox.on_failed("original:run-1:ev-1", MEMBER, {"name": "scan.pdf"})
        row = conn.execute("SELECT chat_id, payload FROM outbound WHERE idempotency_key='failed:original:run-1:ev-1'")
        notice = row.fetchone()
        assert (notice["chat_id"], json.loads(notice["payload"])["text"]) == (
            MEMBER,
            cfg.locale.text("original_failed").format(name="scan.pdf"),
        )
    finally:
        journal.close()
        conn.close()
