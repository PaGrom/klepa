import errno
import json

import pytest

from helpers import OWNER
from klepa_core import db
from klepa_core.alerts import Alerts
from klepa_core.events import EventLog
from klepa_core.gatekeeper.outbox import Outbox


@pytest.fixture
def setup(make_config):
    cfg = make_config()
    conn = db.connect(cfg.core_db_path)
    db.migrate(conn)
    db.seed(conn, cfg)
    events = EventLog(conn)
    now = [1000.0]
    outbox = Outbox(conn, None, events, bot="service")
    return cfg, conn, events, outbox, now


def bind(conn):
    conn.execute("INSERT INTO service_binding(person_id, chat_id, bound_at) VALUES ('owner', ?, 't')", (OWNER,))


def queued(conn):
    return [
        (row["chat_id"], json.loads(row["payload"])["text"])
        for row in conn.execute("SELECT chat_id, payload FROM outbound WHERE bot='service' ORDER BY id")
    ]


def test_before_the_bot_is_bound_alerts_are_logged_and_not_muted(setup):
    cfg, conn, events, outbox, now = setup
    alerts = Alerts(conn, outbox, events, cfg.locale, clock=lambda: now[0])
    assert alerts.raise_("restarted") is False
    assert alerts.raise_("restarted") is False
    assert queued(conn) == []
    assert events.kinds().count("alert_unsent") == 1  # logged once per period
    bind(conn)  # the owner binds the bot a minute later
    now[0] += 60
    assert alerts.raise_("restarted") is True


def test_one_alert_per_class_per_period(setup):
    cfg, conn, events, outbox, now = setup
    bind(conn)
    alerts = Alerts(conn, outbox, events, cfg.locale, clock=lambda: now[0])
    assert alerts.raise_("channel_dead", error="Unauthorized") is True
    assert alerts.raise_("channel_dead", error="Unauthorized") is False
    assert alerts.raise_("restarted") is True  # another class is not held back
    now[0] += 3601
    assert alerts.raise_("channel_dead", error="Conflict") is True
    texts = [text for chat, text in queued(conn)]
    assert all(chat == OWNER for chat, _ in queued(conn))
    assert texts[0].startswith("⚠️ Telegram refuses")
    assert "Unauthorized" in texts[0]
    assert len(texts) == 3
    assert events.kinds().count("alert_queued") == 3


def test_documents_problem_names_the_permission_when_macos_denies_access(setup):
    cfg, conn, events, outbox, now = setup
    bind(conn)
    alerts = Alerts(conn, outbox, events, cfg.locale, clock=lambda: now[0], documents_grace=0)
    assert alerts.documents_failed("PermissionError", errno.EPERM)
    assert alerts.documents_failed("FileNotFoundError", errno.ENOENT)
    denied, missing = (text for _, text in queued(conn))
    assert "Full Disk Access" in denied
    assert "python" in denied.lower()
    assert missing.startswith("⚠️ The documents folder is not available (FileNotFoundError)")


def test_a_documents_problem_is_reported_only_once_it_has_lasted_the_grace_period(setup):
    cfg, conn, events, outbox, now = setup
    bind(conn)
    alerts = Alerts(conn, outbox, events, cfg.locale, clock=lambda: now[0], documents_grace=300)
    assert alerts.documents_failed("FileNotFoundError", errno.ENOENT) is False  # at login the volume mounts late
    now[0] += 200
    assert alerts.documents_failed("FileNotFoundError", errno.ENOENT) is False
    alerts.documents_ok()  # it mounted
    now[0] += 200
    assert alerts.documents_failed("FileNotFoundError", errno.ENOENT) is False  # a new problem, a new clock
    now[0] += 301
    assert alerts.documents_failed("FileNotFoundError", errno.ENOENT) is True
    assert len(queued(conn)) == 1


def test_unknown_alert_is_a_programming_error(setup):
    cfg, conn, events, outbox, _ = setup
    with pytest.raises(ValueError, match="unknown alert"):
        Alerts(conn, outbox, events, cfg.locale).raise_("sky_is_falling")


def test_a_folder_that_does_not_answer_points_at_a_waiting_macos_prompt(setup):
    cfg, conn, events, outbox, now = setup
    bind(conn)
    alerts = Alerts(conn, outbox, events, cfg.locale, clock=lambda: now[0], documents_grace=0)
    # The live run: macOS waited 13 hours for an answer, and the alert gave no hint.
    assert alerts.documents_failed("DocumentsTimeout", errno.ETIMEDOUT)
    (text,) = (text for _, text in queued(conn))
    assert text.startswith("⚠️ The documents folder does not answer (DocumentsTimeout)")
    assert "prompt" in text
    assert "python" in text.lower()
