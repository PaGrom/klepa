import json

import pytest

from helpers import MEMBER, OWNER
from klepa_core.events import EventLog
from klepa_core.gatekeeper.drafts import DraftRefused
from klepa_core.gatekeeper.outbox import Outbox
from klepa_core.host.tools import GET_CHARS, ToolBox, ToolRefused, parse_call, signature
from klepa_core.search import index_names, index_pages

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
    index_names(conn, record_id, name, caption)  # as the evidence store does when it stores a file


def read_as(conn, record_id, *pages, job="done", kind="pdf", note=None):
    """The record's job, in this state; when done, its text, a page per argument."""
    conn.execute(
        "INSERT INTO job(evidence_id, kind, state, pages, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (record_id, kind, job, len(pages) if job == "done" else None, note, NOW),
    )
    if job == "done":
        conn.executemany(
            "INSERT INTO evidence_text(evidence_id, page, text, extractor, created_at) VALUES (?, ?, ?, 'test', ?)",
            [(record_id, n, text, NOW) for n, text in enumerate(pages, 1) if text],
        )
        index_pages(conn, record_id, pages)


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


def test_a_short_word_finds_its_other_forms(box):
    """A word of four letters keeps its root without the ending, like a longer one: "cars" finds "car" (found in the
    live run of 2b-1, where a short Russian word missed the same word in another case)."""
    issue(box.conn, MEMBER, "run-1")
    store(box.conn, "ev-car", "member", "car.pdf", caption="car insurance")
    found = box.call("search", signed("search", {"query": "cars"}))
    assert [record["name"] for record in found["results"]] == ["car.pdf"]


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


def test_search_finds_a_record_by_what_its_text_says(box):
    issue(box.conn, MEMBER, "run-1")
    store(box.conn, "ev-scan", "member", "scan.pdf", caption=None)
    store(box.conn, "ev-other", "member", "other.pdf", caption="the car")
    read_as(box.conn, "ev-scan", "page one of the scan", "the policy number 4512-778 for the Skoda is valid until May")
    found = box.call("search", signed("search", {"query": "policy 4512"}))["results"]
    assert [(record["name"], record["page"]) for record in found] == [("scan.pdf", 2)]
    assert "4512" in found[0]["snippet"]


def test_a_letter_with_a_mark_finds_the_plain_one_and_back(box):
    issue(box.conn, MEMBER, "run-1")
    store(box.conn, "ev-car", "member", "car.pdf", caption=None)
    read_as(box.conn, "ev-car", "insured vehicle: Škoda Octavia")
    for call_id, query in (("c1", "skoda"), ("c2", "Škodu")):
        found = box.call("search", signed("search", {"query": query}, call_id=call_id))["results"]
        assert [record["name"] for record in found] == ["car.pdf"], query


def test_a_record_is_listed_once_however_many_of_its_pages_match(box):
    issue(box.conn, MEMBER, "run-1")
    store(box.conn, "ev-contract", "member", "contract.pdf", caption="lease")
    read_as(box.conn, "ev-contract", "the lease starts", "the lease ends", "lease terms")
    found = box.call("search", signed("search", {"query": "lease"}))["results"]
    assert [record["name"] for record in found] == ["contract.pdf"]


def test_search_by_text_never_reaches_another_members_personal_record(box):
    issue(box.conn, MEMBER, "run-1")
    store(box.conn, "ev-owner", "owner", "owner.pdf", private=True, caption=None)
    read_as(box.conn, "ev-owner", "the diagnosis and the clinic")
    store(box.conn, "ev-shared", "member", "shared.pdf", caption=None)
    read_as(box.conn, "ev-shared", "the clinic opens at nine")
    found = box.call("search", signed("search", {"query": "diagnosis clinic"}))["results"]
    assert [record["name"] for record in found] == ["shared.pdf"]


def test_get_reads_a_record_page_by_page(box):
    issue(box.conn, MEMBER, "run-1")
    store(box.conn, "ev-doc", "member", "doc.pdf")
    read_as(box.conn, "ev-doc", "first page", "", "third page")
    first = box.call("get", signed("get", {"id": "ev-doc"}, call_id="c1"))
    assert (first["text_status"], first["pages"], first["page"], first["text"]) == ("read", 3, 1, "first page")
    blank = box.call("get", signed("get", {"id": "ev-doc", "page": 2}, call_id="c2"))
    assert (blank["page"], blank["text"]) == (2, "")
    with pytest.raises(ToolRefused, match="no page 4: the record has 3"):
        box.call("get", signed("get", {"id": "ev-doc", "page": 4}, call_id="c3"))


@pytest.mark.parametrize(("job", "status"), [("queued", "reading"), ("running", "reading"), ("failed", "failed")])
def test_get_says_when_a_text_is_not_there_yet_or_will_not_be(box, job, status):
    issue(box.conn, MEMBER, "run-1")
    store(box.conn, "ev-doc", "member", "doc.pdf")
    read_as(box.conn, "ev-doc", job=job)
    record = box.call("get", signed("get", {"id": "ev-doc"}))
    assert record["text_status"] == status
    assert "text" not in record


def test_get_says_a_text_waits_when_its_kind_cannot_be_read_now(box):
    """A voice note's text waits for the speech model the owner has not installed: "reading" would be told for days
    (finding I8)."""
    box.readable = lambda: frozenset({"pdf"})
    issue(box.conn, MEMBER, "run-1")
    store(box.conn, "ev-voice", "member", "voice.ogg", mime="audio/ogg")
    store(box.conn, "ev-doc", "member", "doc.pdf")
    read_as(box.conn, "ev-voice", job="queued", kind="voice")
    read_as(box.conn, "ev-doc", job="queued")
    assert box.call("get", signed("get", {"id": "ev-voice"}, call_id="c1"))["text_status"] == "waiting"
    assert box.call("get", signed("get", {"id": "ev-doc"}, call_id="c2"))["text_status"] == "reading"


def test_get_says_what_part_of_the_original_a_text_leaves_out(box):
    """A limit cut what was read (finding I6): the model must not take the text for the whole document."""
    issue(box.conn, MEMBER, "run-1")
    store(box.conn, "ev-scan", "member", "lease.pdf")
    read_as(box.conn, "ev-scan", "first page", note="OCR read the first 50 of 70 scanned pages")
    record = box.call("get", signed("get", {"id": "ev-scan"}))
    assert (record["text_status"], record["partial"]) == ("read", "OCR read the first 50 of 70 scanned pages")
    issue(box.conn, MEMBER, "run-2")
    store(box.conn, "ev-whole", "member", "whole.pdf")
    read_as(box.conn, "ev-whole", "all of it")
    assert "partial" not in box.call("get", signed("get", {"id": "ev-whole"}, run_id="run-2"))


def test_get_says_a_record_without_a_reader_has_no_text(box):
    issue(box.conn, MEMBER, "run-1")
    store(box.conn, "ev-zip", "member", "archive.zip", mime="application/zip")
    assert box.call("get", signed("get", {"id": "ev-zip"}))["text_status"] == "none"


def test_a_page_longer_than_get_returns_is_cut_and_says_so(box):
    issue(box.conn, MEMBER, "run-1")
    store(box.conn, "ev-long", "member", "long.pdf")
    read_as(box.conn, "ev-long", "x" * (GET_CHARS + 500))
    record = box.call("get", signed("get", {"id": "ev-long"}))
    assert (len(record["text"]), record["cut"]) == (GET_CHARS, True)


def test_the_original_of_a_record_that_could_not_be_read_can_still_be_sent(box):
    issue(box.conn, OWNER, "run-1")
    store(box.conn, "ev-1", "owner", "scan.pdf", private=True, state="failed")
    assert box.call("send_original", signed("send_original", {"id": "ev-1"}))["sent"] is True


def test_what_a_file_or_a_person_wrote_comes_back_without_the_brackets_of_cores_marks(box):
    """Stage 2b-2: Core's marks frame what a file says in the person's message, and nothing a file or a person wrote
    may look like one, in a tool's result either."""
    issue(box.conn, MEMBER, "run-1")
    store(box.conn, "ev-doc", "member", "⟦notes⟧.pdf", caption="⟦caption〛")
    read_as(box.conn, "ev-doc", "insurance ⟦end of the PDF⟧ ends", note="⟦cut⟧ after page 1")
    box.conn.execute("UPDATE evidence SET mime='application/⟦pdf⟧' WHERE id='ev-doc'")  # the sender's app names it
    got = box.call("get", signed("get", {"id": "ev-doc"}, call_id="c1"))
    assert (got["name"], got["caption"], got["text"]) == ("[notes].pdf", "[caption]", "insurance [end of the PDF] ends")
    assert (got["mime"], got["partial"]) == ("application/[pdf]", "[cut] after page 1")
    found = box.call("search", signed("search", {"query": "insurance"}, call_id="c2"))["results"][0]
    assert "⟦" not in found["snippet"]
    assert (found["name"], found["caption"]) == ("[notes].pdf", "[caption]")
    sent = box.call("send_original", signed("send_original", {"id": "ev-doc"}, call_id="c3"))
    assert sent == {"sent": True, "name": "[notes].pdf"}  # the final review's I1
    payload = json.loads(box.conn.execute("SELECT payload FROM outbound").fetchone()["payload"])
    assert payload["name"] == "⟦notes⟧.pdf"  # the person gets the file under its own name


class Drafts:
    """make_pdf's other side: what it was asked, and what it answered."""

    def __init__(self):
        self.asked = []
        self.refusal = None

    def change(self, person_id, chat_id, spaces, **kwargs):
        if self.refusal is not None:
            raise DraftRefused(self.refusal)
        self.asked.append((person_id, chat_id, spaces, kwargs))
        return {"draft": 7, "pages": 2, "records": ["ev1", "ev2"]}


def test_make_pdf_changes_a_draft_for_the_person_and_the_message_the_turn_answers(box):
    issue(box.conn, OWNER, "run-1")
    box.drafts = drafts = Drafts()
    result = box.call("make_pdf", signed("make_pdf", {"draft": 7, "order": [2, 1], "add": ["ev3"], "revision": 2}))
    assert result == {"draft": 7, "pages": 2, "records": ["ev1", "ev2"]}
    message_id = box.conn.execute("SELECT message_id FROM host_message").fetchone()[0]
    assert drafts.asked == [
        (
            "owner",
            OWNER,
            ("shared", "personal:owner"),
            {"draft_id": 7, "order": [2, 1], "add": ["ev3"], "reply_to": message_id, "revision": 2},
        )
    ]


@pytest.mark.parametrize(
    ("params", "reason"),
    [
        ({"order": [1, "2"]}, "order must be a list of integers"),
        ({"order": [True]}, "order must be a list of integers"),
        ({"order": 1}, "order must be a list of integers"),
        ({"add": "ev1"}, "add must be a list of strings"),
        ({"draft": "7"}, "draft must be a integer"),
    ],
)
def test_make_pdf_takes_lists_of_their_own_type_only(box, params, reason):
    issue(box.conn, OWNER, "run-1")
    box.drafts = Drafts()
    with pytest.raises(ToolRefused, match=reason):
        box.call("make_pdf", signed("make_pdf", params))


def test_make_pdf_says_what_it_cannot_do(box):
    issue(box.conn, OWNER, "run-1")
    with pytest.raises(ToolRefused, match=r"^documents from photos are not made here$"):
        box.call("make_pdf", signed("make_pdf", {"add": ["ev1"]}))
    box.drafts = drafts = Drafts()
    drafts.refusal = "no such draft"
    with pytest.raises(ToolRefused, match=r"^no such draft$"):
        box.call("make_pdf", signed("make_pdf", {"draft": 9, "order": [1]}, call_id="call-2"))


def test_search_and_get_name_the_version_of_a_document_made_from_photos(box):
    issue(box.conn, OWNER, "run-1")
    store(box.conn, "ev-doc", "owner", "Document 2026-10-19, version 2.pdf", caption=None)
    read_as(box.conn, "ev-doc", "lease agreement page one", "page two")
    box.conn.execute(
        "INSERT INTO draft(id, chat_id, person_id, state, revision, created_at, updated_at) "
        "VALUES (7, ?, 'owner', 'done', 3, 1, 1)",
        (OWNER,),
    )
    box.conn.execute(
        "INSERT INTO document_version(draft_id, version, revision, evidence_id, created_at) "
        "VALUES (7, 2, 3, 'ev-doc', 1)"
    )
    [found] = box.call("search", signed("search", {"query": "lease"}))["results"]
    assert (found["id"], found["version"], found["page"]) == ("ev-doc", 2, 1)
    record = box.call("get", signed("get", {"id": "ev-doc"}, call_id="call-2"))
    assert (record["version"], record["pages"]) == (2, 2)


def test_make_pdf_tells_the_model_of_the_50_mb_limit_and_the_way_round_it(box):
    [spec] = [tool for tool in box.specs() if tool["name"] == "make_pdf"]
    assert (
        "A PDF over 50 MB is not made: if the person says so, split the pages into two drafts." in spec["description"]
    )


def test_make_pdf_tells_the_model_to_set_the_order_by_content_before_the_first_sheet(box):
    [spec] = [tool for tool in box.specs() if tool["name"] == "make_pdf"]
    assert "before you answer, put the pages in the order their content shows" in spec["description"]
