"""The search index (spec 5.6): names, captions and texts in one FTS5 table, the space filter in the query itself."""

import pytest

from klepa_core.search import find, index_names, index_pages

SHARED, OWNER, MEMBER = "shared", "personal:owner", "personal:member"
STATES = ("stored", "processed")


@pytest.fixture
def conn(core_db):
    return core_db[1]


def record(conn, record_id, space="shared", *, name="file.pdf", caption=None, pages=(), state="processed"):
    conn.execute(
        "INSERT INTO evidence(id, space_id, kind, original_name, received_at, channel, chat_id, message_id, "
        "update_id, authenticated_subject, ingest_key, state, caption) "
        "VALUES (?, ?, 'file', ?, '2026-10-07T10:00:00Z', 'telegram', 1, ?, 1, 'owner', ?, ?, ?)",
        (record_id, space, name, abs(hash(record_id)) % 100_000, f"k:{record_id}", state, caption),
    )
    index_names(conn, record_id, name, caption)
    index_pages(conn, record_id, list(pages))


def test_a_record_is_found_by_its_text_with_the_page_and_the_words_around(conn):
    record(conn, "ev-1", pages=("a cover page", "the policy number 4512-778 is valid until May"))
    (hit,) = find(conn, (SHARED,), STATES, ["4512"], 10)
    assert (hit.evidence_id, hit.page) == ("ev-1", 2)
    assert "4512" in (hit.snippet or "")


def test_a_record_found_by_its_name_or_caption_has_no_snippet(conn):
    record(conn, "ev-1", name="passport.pdf", caption="for the trip")
    assert [(hit.evidence_id, hit.page, hit.snippet) for hit in find(conn, (SHARED,), STATES, ["trip"], 10)] == [
        ("ev-1", 0, None)
    ]


def test_each_record_once_at_its_best_page(conn):
    record(conn, "ev-1", pages=("lease", "lease lease lease terms of the lease", "lease"))
    hits = find(conn, (SHARED,), STATES, ["lease"], 10)
    assert [hit.evidence_id for hit in hits] == ["ev-1"]


def test_the_space_filter_is_in_the_query(conn):
    record(conn, "ev-owner", OWNER, pages=("the clinic and the diagnosis",))
    record(conn, "ev-member", MEMBER, pages=("the clinic opens at nine",))
    record(conn, "ev-shared", pages=("the clinic is closed on Sunday",))
    found = {hit.evidence_id for hit in find(conn, (SHARED, MEMBER), STATES, ["clinic"], 10)}
    assert found == {"ev-member", "ev-shared"}


def test_only_records_in_the_given_states(conn):
    record(conn, "ev-gone", pages=("the old lease",), state="deleted")
    record(conn, "ev-kept", pages=("the new lease",))
    assert [hit.evidence_id for hit in find(conn, (SHARED,), STATES, ["lease"], 10)] == ["ev-kept"]


def test_marks_on_letters_do_not_matter(conn):
    record(conn, "ev-1", pages=("insured vehicle: Škoda Octavia, café receipt",))
    for stems in (["skod"], ["škod"], ["cafe"], ["café"]):
        assert [hit.evidence_id for hit in find(conn, (SHARED,), STATES, stems, 10)] == ["ev-1"], stems


def test_at_most_the_limit(conn):
    for n in range(5):
        record(conn, f"ev-{n}", caption="insurance")
    assert len(find(conn, (SHARED,), STATES, ["insur"], 3)) == 3


def test_a_page_without_text_has_no_row(conn):
    record(conn, "ev-1", pages=("first", "", "third"))
    rows = conn.execute("SELECT page FROM evidence_fts WHERE evidence_id='ev-1' ORDER BY page").fetchall()
    assert [row[0] for row in rows] == [0, 1, 3]


def test_a_record_found_by_its_caption_still_shows_its_best_text_page(conn):
    """The name or caption may rank first; the model still gets the page with the words and the words around
    (finding M5)."""
    pages = ("cover", "terms", "insurance policy 4512-778")
    record(conn, "ev-1", name="scan.pdf", caption="car insurance", pages=pages)
    (hit,) = find(conn, (SHARED,), STATES, ["insur"], 10)
    assert (hit.evidence_id, hit.page) == ("ev-1", 3)
    assert "4512-778" in (hit.snippet or "")


def test_the_space_filter_comes_before_the_limit(conn):
    """Three records the caller may not see rank higher; the one it may see is still found (finding M6)."""
    for n in range(3):
        record(conn, f"ev-private-{n}", OWNER, caption="clinic", pages=("clinic clinic clinic",))
    record(conn, "ev-shared", pages=("a long note that mentions the clinic once among many other words",))
    assert [hit.evidence_id for hit in find(conn, (SHARED, MEMBER), STATES, ["clinic"], 1)] == ["ev-shared"]


def test_d_with_stroke_is_found_as_dj_and_back(conn):
    """unicode61 folds marks on Latin letters, but d with stroke has none: the index writes it "dj" (finding I7)."""
    record(conn, "ev-1", caption="Đak of the week", pages=("the djak got a prize",))
    record(conn, "ev-2", pages=("the đački dom on the corner",))
    assert {hit.evidence_id for hit in find(conn, (SHARED,), STATES, ["djak"], 10)} == {"ev-1"}
    assert {hit.evidence_id for hit in find(conn, (SHARED,), STATES, ["đak"], 10)} == {"ev-1"}
    assert {hit.evidence_id for hit in find(conn, (SHARED,), STATES, ["djack"], 10)} == {"ev-2"}
