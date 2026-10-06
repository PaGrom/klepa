import json

import pytest

from helpers import MEMBER, OWNER
from klepa_core.events import EventLog
from klepa_core.gatekeeper.outbox import Outbox
from klepa_core.host.tools import ToolBox, ToolRefused, parse_call, signature

KEY = b"k" * 32
NOW = 1_800_000_000.0


@pytest.fixture
def box(core_db, tmp_path):
    cfg, conn, _ = core_db
    persons = {member.telegram_id: member.person_id for member in cfg.members}
    names = {member.person_id: member.name for member in cfg.members}
    outbox = Outbox(conn, None, EventLog(conn), bot="family")
    incoming = tmp_path / "incoming"
    return ToolBox(conn, KEY, persons, names, outbox, incoming, clock=lambda: NOW)


def issue(conn, chat_id, run_id, *, expires=NOW + 600, issued=True):
    cursor = conn.execute(
        "INSERT INTO host_message(source_update_id, kind, chat_id, message_id, sender_id, date, created_at) "
        "VALUES (NULL, 'text', ?, (SELECT COALESCE(MAX(message_id), 0) + 1 FROM host_message), ?, 1, ?)",
        (chat_id, chat_id, NOW),
    )
    conn.execute(
        "INSERT INTO host_run(run_id, boot_id, chat_id, sender_id, prompt_built_at, host_message_id, registered_at, "
        "expires_at) VALUES (?, 'boot', ?, ?, ?, ?, ?, ?)",
        (run_id, chat_id, chat_id, NOW, cursor.lastrowid if issued else None, NOW, expires),
    )


def store(
    conn,
    record_id,
    person,
    name,
    *,
    private=False,
    path="2026/10/x.pdf",
    caption="insurance",
    state="stored",
    mime="application/pdf",
    received="2026-10-05T10:00:00Z",
):
    space = f"personal:{person}" if private else "shared"
    conn.execute(
        "INSERT INTO evidence(id, space_id, kind, original_name, disk_name, incoming_path, mime, size, sha256, "
        "received_at, channel, chat_id, message_id, update_id, authenticated_subject, ingest_key, state, caption) "
        "VALUES (?, ?, 'file', ?, ?, ?, ?, 3, ?, ?, 'telegram', 1, ?, 1, ?, ?, ?, ?)",
        (
            record_id,
            space,
            name,
            name,
            path,
            mime,
            "a" * 64,
            received,
            abs(hash(record_id)) % 10_000,
            person,
            f"k:{record_id}",
            state,
            caption,
        ),
    )


def signed(tool, params, run_id="run-1", call_id="call-1", key=KEY):
    return params | {
        "_klepa": {"run_id": run_id, "tool_call_id": call_id, "sig": signature(key, run_id, call_id, tool, params)}
    }


def test_a_signed_call_runs_for_the_person_the_turn_answers(box):
    issue(box.conn, MEMBER, "run-1")
    store(box.conn, "ev-shared", "member", "shared.pdf")
    store(box.conn, "ev-owner", "owner", "owner.pdf", private=True)
    found = box.call("search", signed("search", {"query": "insurance"}))
    assert [record["name"] for record in found["results"]] == ["shared.pdf"]


@pytest.mark.parametrize(
    ("arguments", "reason"),
    [
        ({"query": "insurance"}, "not signed"),  # no signature at all
        (signed("search", {"query": "insurance"}, key=b"x" * 32), "not signed"),  # the model's own guess
        (signed("search", {"query": "insurance"}) | {"query": "passport"}, "not signed"),  # a signature from history
        (signed("search", {"query": "insurance"}, call_id="http-1"), "not signed"),  # /tools/invoke
        (signed("get", {"query": "insurance"}), "not signed"),  # signed for another tool
    ],
)
def test_calls_without_a_signature_for_exactly_this_call_are_refused(box, arguments, reason):
    issue(box.conn, MEMBER, "run-1")
    with pytest.raises(ToolRefused, match=reason):
        box.call("search", arguments)


def test_a_call_id_is_used_once(box):
    issue(box.conn, MEMBER, "run-1")
    box.call("search", signed("search", {"query": "insurance"}))
    with pytest.raises(ToolRefused, match="made already"):
        box.call("search", signed("search", {"query": "insurance"}))


@pytest.mark.parametrize(("expires", "issued"), [(NOW - 1, True), (NOW + 600, False)])
def test_a_turn_that_is_over_or_answers_nothing_runs_no_tool(box, expires, issued):
    issue(box.conn, MEMBER, "run-1", expires=expires, issued=issued)
    with pytest.raises(ToolRefused, match="not registered"):
        box.call("search", signed("search", {"query": "insurance"}))


@pytest.mark.parametrize(
    ("params", "reason"),
    [
        ({"query": "x", "space": "personal:owner"}, "unknown parameters: space"),
        ({"query": 5}, "query must be a string"),
        ({"query": ["x"]}, "query must be a string"),
        ({"query": "x", "limit": True}, "limit must be a integer"),
    ],
)
def test_parameters_are_exactly_the_declared_ones(box, params, reason):
    issue(box.conn, MEMBER, "run-1")
    with pytest.raises(ToolRefused, match=reason):
        box.call("search", signed("search", params))


@pytest.mark.parametrize(
    "body",
    [
        b'{"name": "search", "name": "get", "arguments": {}}',
        b'{"name": "search", "arguments": {"query": "a", "query": "b"}}',
        b'{"name": "search", "arguments": {"limit": 1.5}}',
        b'{"name": "search", "arguments": {"limit": NaN}}',
        b"[]",
        b'{"name": "search"}',
        b"x" * 20_000,
        b"[" * 10_000,  # nested deeper than the parser goes
    ],
)
def test_a_call_that_parses_two_ways_or_not_at_all_is_refused(body):
    with pytest.raises(ToolRefused):
        parse_call(body)


def test_another_members_personal_record_is_never_read_or_sent(box):
    issue(box.conn, MEMBER, "run-1")
    store(box.conn, "ev-owner", "owner", "owner.pdf", private=True)
    with pytest.raises(ToolRefused, match="no such record"):
        box.call("get", signed("get", {"id": "ev-owner"}, call_id="c1"))
    with pytest.raises(ToolRefused, match="no such record"):
        box.call("send_original", signed("send_original", {"id": "ev-owner"}, call_id="c2"))


def test_send_original_queues_the_stored_bytes_for_the_persons_chat_once(box):
    issue(box.conn, OWNER, "run-1")
    store(box.conn, "ev-1", "owner", "Διαβατήριο.pdf", private=True)
    assert box.call("send_original", signed("send_original", {"id": "ev-1"}, call_id="c1")) == {
        "sent": True,
        "name": "Διαβατήριο.pdf",
    }
    box.call("send_original", signed("send_original", {"id": "ev-1"}, call_id="c2"))  # asked twice in one turn
    rows = box.conn.execute("SELECT method, chat_id, payload FROM outbound").fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert (rows[0]["method"], rows[0]["chat_id"]) == ("sendDocument", OWNER)
    assert payload["path"] == str(box.incoming_dir / "2026/10/x.pdf")
    assert (payload["sha256"], payload["size"], payload["name"]) == ("a" * 64, 3, "Διαβατήριο.pdf")


def test_another_form_of_a_word_finds_the_record(box):
    """A person writes "my insurances" where the caption says "insurance": words match by their beginnings, and a
    record that matches more of the words comes first."""
    issue(box.conn, MEMBER, "run-1")
    store(box.conn, "ev-car", "member", "car.pdf", caption="car insurance policy")
    store(box.conn, "ev-flat", "member", "flat.pdf", caption="flat insurance", received="2026-10-06T10:00:00Z")
    store(box.conn, "ev-passport", "member", "passport.pdf", caption="passport scan")
    found = box.call("search", signed("search", {"query": "my insurances for the car"}))
    assert [record["name"] for record in found["results"]] == ["car.pdf", "flat.pdf"]


def test_without_words_search_lists_the_latest_records(box):
    issue(box.conn, MEMBER, "run-1")
    store(box.conn, "ev-old", "member", "old.pdf", received="2026-10-01T10:00:00Z")
    store(box.conn, "ev-new", "member", "new.pdf", received="2026-10-04T10:00:00Z")
    store(box.conn, "ev-owner", "owner", "owner.pdf", private=True, received="2026-10-05T10:00:00Z")
    found = box.call("search", signed("search", {"query": "  "}, call_id="c1"))
    assert [record["name"] for record in found["results"]] == ["new.pdf", "old.pdf"]
    found = box.call("search", signed("search", {}, call_id="c2"))
    assert [record["name"] for record in found["results"]] == ["new.pdf", "old.pdf"]


@pytest.mark.parametrize(("limit", "count"), [(0, 1), (-5, 1), (2, 2), (1000, 50)])
def test_a_search_returns_between_one_and_fifty_records(box, limit, count):
    issue(box.conn, MEMBER, "run-1")
    for n in range(55):
        store(box.conn, f"ev-{n}", "member", f"{n}.pdf")
    found = box.call("search", signed("search", {"query": "insurance", "limit": limit}))
    assert len(found["results"]) == count


def test_search_lists_only_records_get_can_read(box):
    issue(box.conn, MEMBER, "run-1")
    store(box.conn, "ev-gone", "member", "gone.pdf", state="expired")
    store(box.conn, "ev-kept", "member", "kept.pdf")
    found = box.call("search", signed("search", {"query": "insurance"}))
    assert [record["name"] for record in found["results"]] == ["kept.pdf"]


def test_an_original_goes_out_under_a_name_and_type_a_request_can_carry(box):
    """The name and the MIME type came from the sender's device: a line break or a stray byte in either would stop
    the upload, so the name loses its control characters and an odd type becomes a plain one."""
    issue(box.conn, OWNER, "run-1")
    store(box.conn, "ev-1", "owner", "line\nbreak\x00.pdf", private=True, mime="application/pdf\r\nX-Evil: 1")
    assert box.call("send_original", signed("send_original", {"id": "ev-1"})) == {"sent": True, "name": "linebreak.pdf"}
    payload = json.loads(box.conn.execute("SELECT payload FROM outbound").fetchone()["payload"])
    assert (payload["name"], payload["mime"]) == ("linebreak.pdf", "application/octet-stream")


@pytest.mark.parametrize(
    "klepa",
    [
        signed("search", {"query": "insurance"}, run_id="run-1\n")["_klepa"],  # a line break after a good id
        {"run_id": "run-1", "tool_call_id": "call-1", "sig": "\u00e9" * 64},  # not hex at all
    ],
)
def test_a_malformed_signature_is_refused_not_an_error(box, klepa):
    issue(box.conn, MEMBER, "run-1")
    issue(box.conn, MEMBER, "run-1\n")
    with pytest.raises(ToolRefused, match="not signed"):
        box.call("search", {"query": "insurance", "_klepa": klepa})
