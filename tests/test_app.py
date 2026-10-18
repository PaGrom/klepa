from helpers import BASE_CONFIG
from klepa_core import app
from klepa_core.host.gateway import LaunchdGateway
from klepa_core.host.peers import probe_peer, stand_in
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
    key = b"k" * 32
    expect, control = app._gateway(cfg, key)
    assert isinstance(control, LaunchdGateway)
    assert control.port == 19300
    assert expect is not None
    assert expect.plugin_sha256 == adapter_sha256()
    assert expect.policy["gateway.reload.mode"] == "off"
    # The gateway admits the members' stand-ins and the probe peer: never a Telegram id.
    allowed = [str(stand_in(key, "owner")), str(stand_in(key, "member")), str(probe_peer(key))]
    assert expect.policy["channels.telegram.allowFrom"] == allowed
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
        alerts = Alerts(conn, None, events, cfg.locale)
        app._host(cfg, conn, journal, None, outbox, alerts, events, "123:abc", None, frozenset)
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


def processing_setup(make_config, tmp_path, monkeypatch, **changes):
    """Core's database, alerts and a [processing] section of stand-ins, with sandbox-exec taken as present."""
    import dataclasses

    from fakereaders import processing_config, processing_section
    from klepa_core import db
    from klepa_core.alerts import Alerts
    from klepa_core.events import EventLog

    monkeypatch.setattr(app, "sandbox_available", lambda: True)
    processing = dataclasses.replace(processing_config(tmp_path), **changes)
    cfg = make_config(text=BASE_CONFIG + processing_section(processing))
    app.init_layout(cfg)
    conn = db.connect(cfg.core_db_path)
    events = EventLog(conn)
    return cfg, conn, Alerts(conn, None, events, cfg.locale), events


def alerts_logged(conn):
    import json

    rows = conn.execute("SELECT data FROM event_log WHERE kind IN ('alert_unsent', 'alert_queued')")
    return [json.loads(row[0])["alert"] for row in rows]


def test_processing_whose_sandbox_would_open_a_hidden_folder_reads_nothing_and_says_so(
    make_config, tmp_path, monkeypatch
):
    """OCR's languages in the home folder itself would make all of it readable (finding I3)."""
    from pathlib import Path

    cfg, conn, alerts, events = processing_setup(make_config, tmp_path, monkeypatch, tessdata=Path.home())
    parts = app._processing(cfg, conn, alerts, events)
    assert parts is not None
    assert (parts.worker, parts.loops, parts.readable()) == (None, [], frozenset())
    assert alerts_logged(conn) == ["processing_unavailable"]


def test_a_jobs_folder_that_cannot_be_made_ready_reads_nothing_and_says_so(make_config, tmp_path, monkeypatch):
    """Core still starts and receives (finding I4)."""
    from klepa_core.processing.jobs import Worker

    cfg, conn, alerts, events = processing_setup(make_config, tmp_path, monkeypatch)

    def refuse(worker):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(Worker, "sweep", refuse)
    parts = app._processing(cfg, conn, alerts, events)
    assert parts is not None
    assert (parts.worker, parts.loops) == (None, [])
    assert alerts_logged(conn) == ["processing_unavailable"]


def test_a_program_gone_since_start_takes_its_kind_from_what_get_says_is_read_and_tells_the_owner(
    make_config, tmp_path, monkeypatch
):
    """Homebrew replaced a program while Core ran (finding M8)."""
    cfg, conn, alerts, events = processing_setup(make_config, tmp_path, monkeypatch)
    parts = app._processing(cfg, conn, alerts, events)
    assert parts is not None
    assert parts.worker is not None
    assert parts.readable() == parts.worker.kinds
    assert "voice" in parts.readable()
    assert parts.worker.on_missing is not None
    parts.worker.kinds -= {"voice"}
    parts.worker.on_missing("voice", "whisper-cli")
    assert "voice" not in parts.readable()
    assert alerts_logged(conn) == ["processing_unavailable"]


def test_the_tools_learn_what_core_reads_now(make_config, short_dir, tmp_path):
    """get tells "waiting" for a kind the worker does not read (finding I8): the host is built after processing."""
    from klepa_core import db
    from klepa_core.alerts import Alerts
    from klepa_core.events import EventLog
    from klepa_core.gatekeeper.outbox import Outbox
    from klepa_core.journal import InboundJournal

    section = f'\n[host]\nsocket = "{short_dir / "run" / "adapter.sock"}"\nruntime_dir = "{tmp_path / "runtime"}"\n'
    cfg = make_config(text=BASE_CONFIG + section)
    app.init_layout(cfg)
    conn = db.connect(cfg.core_db_path)
    events = EventLog(conn)
    journal = InboundJournal(tmp_path / "journal.db")
    try:
        alerts = Alerts(conn, None, events, cfg.locale)
        outbox = Outbox(conn, None, events)
        readable = lambda: frozenset({"pdf"})  # noqa: E731
        host = app._host(cfg, conn, journal, None, outbox, alerts, events, "123:abc", None, readable)
        assert host is not None
        assert host.tools.readable() == frozenset({"pdf"})
    finally:
        journal.close()
        conn.close()
