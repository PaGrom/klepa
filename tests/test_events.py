import pytest

from klepa_core import db
from klepa_core.events import EventLog


def test_log_and_forbidden_keys(tmp_path):
    conn = db.connect(tmp_path / "core.db")
    db.migrate(conn)
    events = EventLog(conn)
    events.log("core_started")
    events.log("update_rejected", {"reason": "not_member", "from_id": 1})
    assert events.kinds() == ["core_started", "update_rejected"]
    for key in ("token", "text", "caption", "url"):
        with pytest.raises(ValueError):
            events.log("x", {key: "secret"})
