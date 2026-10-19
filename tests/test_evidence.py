import dataclasses
import errno
import hashlib
import json
import threading
from datetime import UTC, datetime

import pytest

from helpers import wait_until
from klepa_core import db
from klepa_core.cards import card_file_name, read_card
from klepa_core.documents import DocumentsFolder
from klepa_core.durable import partial_name
from klepa_core.events import EventLog
from klepa_core.evidence import EvidenceStore, IncomingFile, ingest_key
from klepa_core.names import disk_name

KEY = b"k" * 32
SEPT30_2230_UTC = int(datetime(2026, 9, 30, 22, 30, tzinfo=UTC).timestamp())


@pytest.fixture
def store(make_config):
    cfg = make_config()
    cfg.incoming_dir.mkdir(mode=0o700)  # init_layout creates it before Core runs
    conn = db.connect(cfg.core_db_path)
    db.migrate(conn)
    db.seed(conn, cfg)
    return EvidenceStore(conn, cfg.incoming_dir, cfg.documents_dir, KEY, cfg.timezone, EventLog(conn))


def incoming(message_id=7, data=b"%PDF test", name="Contract.pdf", date=SEPT30_2230_UTC):
    return IncomingFile(
        update_id=1000 + message_id,
        chat_id=111111,
        message_id=message_id,
        message_date=date,
        person_id="owner",
        space_id="shared",
        kind="file",
        original_name=name,
        mime="application/pdf",
        data=data,
        file_id="F",
        file_unique_id="U",
        media_group_id=None,
        caption=None,
    )


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
    assert copied["documents_path"].startswith("Shared/2026/10/")
    target = store.documents_dir / copied["documents_path"]
    assert target.read_bytes() == b"%PDF test"
    card = read_card(target.parent / card_file_name(row["id"]), KEY)
    assert card["sha256"] == row["sha256"]
    assert card["authenticated_subject"] == "owner"
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
    target_dir = store.documents_dir / "Shared" / "2026" / "10"
    target_dir.mkdir(parents=True)
    (target_dir / partial_name(row["disk_name"])).write_bytes(b"half")
    assert await store.copy_pending() == 1
    assert (target_dir / row["disk_name"]).read_bytes() == b"%PDF test"


async def test_too_large_is_recorded_without_a_file(store):
    row = store.record_too_large(incoming(message_id=8, data=b""), 25 * 1024 * 1024)
    assert (row["state"], row["copy_state"], row["size"]) == ("too_large", "none", 25 * 1024 * 1024)
    assert not [p for p in store.incoming_dir.rglob("*") if p.is_file()]


async def test_missing_documents_root_is_not_recreated(store):
    await store.ingest(incoming())
    store.documents_dir.rmdir()
    assert await store.copy_pending() == 0
    assert not store.documents_dir.exists()
    store.documents_dir.mkdir()
    assert await store.copy_pending() == 1


async def test_leftovers_of_an_interrupted_save_are_reused(store):
    f = incoming()
    evidence_id = store.evidence_id_for(ingest_key(f.chat_id, f.message_id))
    month_dir = store.incoming_dir / "2026" / "10"
    month_dir.mkdir(parents=True)
    name = disk_name(evidence_id, f.original_name)
    (month_dir / partial_name(name)).write_bytes(b"half")  # died while writing
    (month_dir / name).write_bytes(f.data)  # an earlier attempt finished the file but not the row
    row = await store.ingest(f)
    assert row["id"] == evidence_id
    assert sorted(p.name for p in month_dir.iterdir()) == [name]


async def test_different_bytes_under_our_name_are_refused(store):
    f = incoming()
    month_dir = store.incoming_dir / "2026" / "10"
    month_dir.mkdir(parents=True)
    name = disk_name(store.evidence_id_for(ingest_key(f.chat_id, f.message_id)), f.original_name)
    (month_dir / name).write_bytes(b"something else")
    with pytest.raises(FileExistsError):
        await store.ingest(f)
    assert (month_dir / name).read_bytes() == b"something else"


@pytest.mark.parametrize(
    ("kind", "mime", "suffix"),
    [
        ("photo", "image/jpeg", "-photo.jpg"),
        ("voice", "audio/ogg", "-voice.ogg"),
        ("video", "video/mp4", "-video.mp4"),
        ("audio", "audio/mpeg", "-audio.mp3"),
        ("file", "application/pdf", "-file.pdf"),
        ("file", None, "-file"),
    ],
)
async def test_unnamed_files_get_their_kind_and_an_extension(store, kind, mime, suffix):
    row = await store.ingest(dataclasses.replace(incoming(), kind=kind, mime=mime, original_name=None))
    assert row["disk_name"].endswith(suffix)
    assert row["original_name"] is None


async def test_a_hanging_documents_folder_defers_the_copy_and_reports_it(store, monkeypatch):
    store.documents = DocumentsFolder(store.documents_dir, timeout=0.2)
    release = threading.Event()
    write_copy = store._write_copy

    def hanging(*args):
        release.wait()
        write_copy(*args)

    monkeypatch.setattr(store, "_write_copy", hanging)
    await store.ingest(incoming())
    assert await store.copy_pending() == 0
    assert store.last_copy_error == ("DocumentsTimeout", errno.ETIMEDOUT)
    release.set()  # the folder answers again, and the stuck call finishes its copy
    await wait_until(lambda: not store.documents.stuck)
    assert await store.copy_pending() == 1  # the finished copy is recognised, not written twice
    assert store.last_copy_error is None


async def test_a_copy_conflict_is_tried_again_after_a_restart(store):
    row = await store.ingest(incoming())
    target_dir = store.documents_dir / "Shared" / "2026" / "10"
    target_dir.mkdir(parents=True)
    (target_dir / row["disk_name"]).write_bytes(b"someone else's file")
    assert await store.copy_pending() == 0
    assert store.conn.execute("SELECT copy_state FROM evidence").fetchone()[0] == "failed"
    (target_dir / row["disk_name"]).unlink()  # the owner clears the conflict
    assert store.retry_failed_copies() == 1
    assert await store.copy_pending() == 1


def indexed(store, record_id):
    rows = store.conn.execute("SELECT page, body, name, caption FROM evidence_fts WHERE evidence_id=?", (record_id,))
    return [tuple(row) for row in rows]


async def test_a_stored_original_is_found_by_its_name_and_caption(store):
    """Search reads one index: an original is in it from the moment it is stored, by its name and caption (page 0);
    its text joins later, page by page, once a reader has read it."""
    row = await store.ingest(dataclasses.replace(incoming(), caption="policy for the car"))
    assert indexed(store, row["id"]) == [(0, "", "Contract.pdf", "policy for the car")]
    big = store.record_too_large(incoming(message_id=8, name="Scan.pdf"), 30_000_000)
    assert indexed(store, big["id"]) == [(0, "", "Scan.pdf", "")]
    await store.ingest(incoming())  # the same message again: still one row
    assert len(indexed(store, row["id"])) == 1


async def test_a_record_core_made_is_kept_like_an_original_with_its_sources_and_its_pages_texts(store):
    """A document from photos (stage 2c-1): stored once under the person, its sources in order, found by its text."""
    made = await store.ingest_derived(
        "draft:1:1",
        space_id="shared",
        person_id="owner",
        chat_id=111111,
        message_id=0,
        name="Document 2026-10-19.pdf",
        mime="application/pdf",
        data=b"%PDF made",
        sources=["ev2", "ev1"],
        texts=["the lease, page one", ""],
    )
    assert (made["channel"], made["kind"], made["state"], made["copy_state"]) == (
        "core",
        "file",
        "processed",
        "pending",
    )
    assert (made["authenticated_subject"], made["update_id"]) == ("owner", 0)
    assert (json.loads(made["tags"]), json.loads(made["derived_from"])) == (["derived"], ["ev2", "ev1"])
    assert (store.incoming_dir / made["incoming_path"]).read_bytes() == b"%PDF made"
    assert made["sha256"] == hashlib.sha256(b"%PDF made").hexdigest()
    texts = store.conn.execute("SELECT page, text FROM evidence_text WHERE evidence_id=?", (made["id"],)).fetchall()
    assert [tuple(row) for row in texts] == [(1, "the lease, page one")]  # a page without text keeps no row
    job = store.conn.execute("SELECT kind, state, pages FROM job WHERE evidence_id=?", (made["id"],)).fetchone()
    assert tuple(job) == ("pdf", "done", 2)  # read from the start: no reader ever reads it
    found = store.conn.execute("SELECT evidence_id, page FROM evidence_fts WHERE evidence_fts MATCH 'lease'")
    assert [tuple(row) for row in found] == [(made["id"], 1)]
    again = await store.ingest_derived(
        "draft:1:1",
        space_id="shared",
        person_id="owner",
        chat_id=111111,
        message_id=0,
        name="Document 2026-10-19.pdf",
        mime="application/pdf",
        data=b"%PDF made",
        sources=["ev2", "ev1"],
        texts=["the lease, page one", ""],
    )
    assert again["id"] == made["id"]
    assert store.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 1


async def test_a_record_core_made_is_copied_at_once_with_its_sources_on_its_card(store):
    """It came in no batch, so nothing a batch may still change holds it back."""
    made = await store.ingest_derived(
        "draft:1:1",
        space_id="shared",
        person_id="owner",
        chat_id=111111,
        message_id=0,
        name="Document 2026-10-19.pdf",
        mime="application/pdf",
        data=b"%PDF made",
        sources=["ev2", "ev1"],
        texts=["page one"],
    )
    assert await store.copy_pending(ready=lambda row: False) == 1
    row = store.conn.execute("SELECT copy_state, documents_path FROM evidence WHERE id=?", (made["id"],)).fetchone()
    copy = store.documents_dir / row["documents_path"]
    assert (row["copy_state"], copy.read_bytes()) == ("copied", b"%PDF made")
    card = read_card(copy.parent / card_file_name(made["id"]), KEY)
    assert card["derived_from"] == ["ev2", "ev1"]


async def test_a_record_made_from_an_album_moves_with_it_until_it_is_copied_and_then_counts(store):
    """Review 1's I4: a late private caption makes the album personal; what Core made of its photos goes with it."""
    for message_id in (1, 2):
        await store.ingest(dataclasses.replace(incoming(message_id), kind="photo", media_group_id="a"))
    album = [row["id"] for row in store.conn.execute("SELECT id FROM evidence ORDER BY message_id")]
    made = await store.ingest_derived(
        "draft:1:1",
        space_id="shared",
        person_id="owner",
        chat_id=111111,
        message_id=0,
        name="Document.pdf",
        mime="application/pdf",
        data=b"%PDF made",
        sources=[album[1]],
        texts=["page"],
    )
    store.move_album(111111, "a", "personal:owner")
    assert store.conn.execute("SELECT space_id FROM evidence WHERE id=?", (made["id"],)).fetchone()[0] == (
        "personal:owner"
    )
    store.conn.execute("UPDATE evidence SET space_id='shared', copy_state='copied'")
    assert store.album_copied_count(111111, "a") == 3  # the two photos and the document made of one
    store.move_album(111111, "a", "personal:owner")
    assert store.conn.execute("SELECT space_id FROM evidence WHERE id=?", (made["id"],)).fetchone()[0] == "shared"
