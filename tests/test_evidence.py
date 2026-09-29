import hashlib
from datetime import UTC, datetime

import pytest

from klepa_core import db
from klepa_core.cards import card_file_name, read_card
from klepa_core.durable import partial_name
from klepa_core.events import EventLog
from klepa_core.evidence import EvidenceStore, IncomingFile

KEY = b"k" * 32
SEPT30_2230_UTC = int(datetime(2026, 9, 30, 22, 30, tzinfo=UTC).timestamp())


@pytest.fixture
def store(make_config):
    cfg = make_config()
    conn = db.connect(cfg.core_db_path)
    db.migrate(conn)
    db.seed(conn, cfg)
    return EvidenceStore(conn, cfg.incoming_dir, cfg.documents_dir, KEY, cfg.timezone, EventLog(conn))


def incoming(message_id=7, data=b"%PDF test", name="Договор.pdf", date=SEPT30_2230_UTC):
    return IncomingFile(update_id=1000 + message_id, chat_id=111111, message_id=message_id, message_date=date,
                        person_id="owner", space_id="shared", kind="file", original_name=name,
                        mime="application/pdf", data=data, file_id="F", file_unique_id="U",
                        media_group_id=None, caption=None)


async def test_ingest_writes_original_once_in_local_month(store):
    row = await store.ingest(incoming())
    assert row["incoming_path"].startswith("2026/10/")  # 00:30 on 1 October in Berlin
    assert (store.incoming_dir / row["incoming_path"]).read_bytes() == b"%PDF test"
    assert row["sha256"] == hashlib.sha256(b"%PDF test").hexdigest()
    assert (row["state"], row["copy_state"], row["authenticated_subject"]) == ("stored", "pending", "owner")
    again = await store.ingest(incoming())
    assert again["id"] == row["id"]
    assert len([p for p in store.incoming_dir.rglob("*") if p.is_file()]) == 1


async def test_copy_to_documents_with_signed_card(store):
    row = await store.ingest(incoming())
    assert await store.copy_pending() == 1
    copied = store.conn.execute("SELECT * FROM evidence WHERE id=?", (row["id"],)).fetchone()
    assert copied["copy_state"] == "copied"
    assert copied["documents_path"].startswith("Общее/2026/10/")
    target = store.documents_dir / copied["documents_path"]
    assert target.read_bytes() == b"%PDF test"
    card = read_card(target.parent / card_file_name(row["id"]), KEY)
    assert card["sha256"] == row["sha256"] and card["authenticated_subject"] == "owner"
    assert await store.copy_pending() == 0


async def test_documents_unavailable_defers_copy(store):
    row = await store.ingest(incoming())
    store.documents_dir.chmod(0o500)
    try:
        assert await store.copy_pending() == 0
        assert await store.copy_pending() == 0
        state = store.conn.execute("SELECT copy_state FROM evidence WHERE id=?", (row["id"],)).fetchone()[0]
        assert state == "pending"
        assert store.events.kinds().count("documents_copy_deferred") == 1  # logged once, not every retry
    finally:
        store.documents_dir.chmod(0o700)
    assert await store.copy_pending() == 1


async def test_stale_partial_from_a_crash_does_not_block_copy(store):
    row = await store.ingest(incoming())
    target_dir = store.documents_dir / "Общее" / "2026" / "10"
    target_dir.mkdir(parents=True)
    (target_dir / partial_name(row["disk_name"])).write_bytes(b"half")
    assert await store.copy_pending() == 1
    assert (target_dir / row["disk_name"]).read_bytes() == b"%PDF test"


async def test_too_large_is_recorded_without_a_file(store):
    row = store.record_too_large(incoming(message_id=8, data=b""), 25 * 1024 * 1024)
    assert (row["state"], row["copy_state"], row["size"]) == ("too_large", "none", 25 * 1024 * 1024)
    assert not [p for p in store.incoming_dir.rglob("*") if p.is_file()]
