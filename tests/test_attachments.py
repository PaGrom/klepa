"""While a file is read (spec 6.1 items 4-6, the owner's choices of 2026-10-18): Core's interim answer after two
seconds, its edit when the reading ends, and cancel by button or by word."""

import json

import pytest

from helpers import MEMBER, OWNER, Clock, host_peers, job_of, record
from klepa_core.events import EventLog
from klepa_core.gatekeeper.attachments import Attachments
from klepa_core.gatekeeper.drafts import Answer
from klepa_core.gatekeeper.intake import Classified
from klepa_core.gatekeeper.outbox import Outbox
from klepa_core.host.queue import HostQueue
from klepa_core.locale import load_locale
from klepa_core.processing.jobs import Jobs


class Api:
    """Answers presses; Core's other sends go through the outbox, which these tests read."""

    def __init__(self):
        self.answered = []

    async def answer_callback_query(self, callback_query_id, text=None, *, show_alert=False):
        self.answered.append((callback_query_id, text) + (("alert",) if show_alert else ()))


@pytest.fixture
def setup(core_db):
    cfg, conn, journal = core_db
    clock = Clock()
    events = EventLog(conn)
    queue = HostQueue(conn, journal, cfg.members_by_telegram_id(), host_peers(cfg, 2**51 + 7), clock=clock)
    outbox = Outbox(conn, None, events, clock=clock)
    jobs = Jobs(conn, events, clock=clock)
    stopped = []
    api = Api()
    attachments = Attachments(
        conn, queue, outbox, api, cfg.locale, events, jobs=jobs, stop_reading=stopped.extend, clock=clock
    )
    return attachments, queue, conn, clock, api, stopped


def batch(queue, conn, record_id="ev1", *, chat_id=OWNER, message_id=10, kind="voice", reader="voice", state="running"):
    record(conn, record_id, chat_id=chat_id, message_id=message_id, kind=kind)
    job_of(conn, record_id, reader, state)
    return queue.enqueue_attachment(chat_id, [(message_id, message_id, record_id)])


def outbound(conn, key):
    row = conn.execute("SELECT * FROM outbound WHERE idempotency_key=?", (key,)).fetchone()
    return None if row is None else (row["method"], row["state"], json.loads(row["payload"]))


def confirm(conn, key, telegram_message_id):
    """Telegram took the send and gave it this id."""
    conn.execute(
        "UPDATE outbound SET state='CONFIRMED', telegram_message_id=? WHERE idempotency_key=?",
        (telegram_message_id, key),
    )


def press_of(conn, key, telegram_message_id, *, chat_id=OWNER, index=0):
    markup = outbound(conn, key)[2]["reply_markup"]
    data = markup["inline_keyboard"][index][0]["callback_data"]
    return Classified(
        "press", chat_id=chat_id, from_id=chat_id, message_id=telegram_message_id, callback_id="cb", data=data
    )


def test_a_reading_that_ends_within_two_seconds_gets_no_interim_answer(setup):
    attachments, queue, conn, clock, *_ = setup
    message = batch(queue, conn)
    conn.execute("UPDATE job SET state='done', pages=0")
    clock.advance(3)
    attachments.tick()
    assert outbound(conn, f"interim:{message}") is None


def test_a_longer_reading_gets_listening_with_a_cancel_button_as_a_reply_to_the_file(setup):
    attachments, queue, conn, clock, *_ = setup
    message = batch(queue, conn)
    attachments.tick()
    assert outbound(conn, f"interim:{message}") is None  # not yet: two seconds first
    clock.advance(2.5)
    attachments.tick()
    method, state, payload = outbound(conn, f"interim:{message}")
    assert (method, state, payload["text"], payload["reply_to"]) == ("sendMessage", "PENDING", "🎧 listening…", 10)
    [[button]] = payload["reply_markup"]["inline_keyboard"]
    assert button["text"] == "Cancel"
    assert len(button["callback_data"]) == 32  # 128 random bits
    action = conn.execute("SELECT action, chat_id FROM button_action WHERE id=?", (button["callback_data"],)).fetchone()
    assert (action["action"], action["chat_id"]) == (f"cancel:{message}", OWNER)
    attachments.tick()
    assert conn.execute("SELECT COUNT(*) FROM outbound").fetchone()[0] == 1  # once


def test_a_document_gets_reading(setup):
    attachments, queue, conn, clock, *_ = setup
    message = batch(queue, conn, kind="file", reader="pdf")
    clock.advance(2.5)
    attachments.tick()
    assert outbound(conn, f"interim:{message}")[2]["text"] == "📄 reading…"


@pytest.mark.parametrize(
    ("end", "said"), [("done", "🎧 listened"), ("failed", "I couldn't read it. The file is saved.")]
)
def test_when_the_reading_ends_core_edits_its_answer_and_the_button_goes(setup, end, said):
    attachments, queue, conn, clock, *_ = setup
    message = batch(queue, conn)
    clock.advance(2.5)
    attachments.tick()
    confirm(conn, f"interim:{message}", 777)
    conn.execute("UPDATE job SET state=?", (end,))
    attachments.tick()
    method, _, payload = outbound(conn, f"interim_edit:{message}")
    assert (method, payload) == ("editMessageText", {"text": said, "message_id": 777})
    assert conn.execute("SELECT used_at FROM button_action").fetchone()[0] is not None  # a late press is refused
    attachments.tick()
    assert conn.execute("SELECT COUNT(*) FROM outbound").fetchone()[0] == 2  # one edit


async def test_cancel_by_the_button_stops_the_reading_and_the_file_never_reaches_the_host(setup):
    attachments, queue, conn, clock, api, stopped = setup
    message = batch(queue, conn)
    running = conn.execute("SELECT id FROM job").fetchone()[0]
    clock.advance(2.5)
    attachments.tick()
    confirm(conn, f"interim:{message}", 777)
    await attachments.press(press_of(conn, f"interim:{message}", 777))
    assert api.answered == [("cb", None)]
    assert stopped == [running]  # the worker kills its reader
    assert conn.execute("SELECT state FROM job").fetchone()[0] == "cancelled"
    assert queue.serve(None, 100, lambda *_: True) == []
    attachments.tick()
    assert outbound(conn, f"interim_edit:{message}")[2]["text"] == "Cancelled. The file is saved."


@pytest.mark.parametrize("problem", ["other_chat", "used", "expired", "other_message", "unknown"])
async def test_a_press_that_is_not_good_cancels_nothing_and_says_so(setup, problem):
    attachments, queue, conn, clock, api, stopped = setup
    message = batch(queue, conn)
    clock.advance(2.5)
    attachments.tick()
    confirm(conn, f"interim:{message}", 777)
    press = press_of(conn, f"interim:{message}", 777)
    if problem == "other_chat":
        press = Classified("press", chat_id=MEMBER, message_id=777, callback_id="cb", data=press.data)
    elif problem == "used":
        conn.execute("UPDATE button_action SET used_at='t'")
    elif problem == "expired":
        clock.advance(25 * 3600)
    elif problem == "other_message":
        press = Classified("press", chat_id=OWNER, message_id=778, callback_id="cb", data=press.data)
    else:
        press = Classified("press", chat_id=OWNER, message_id=777, callback_id="cb", data="f" * 32)
    await attachments.press(press)
    assert api.answered == [("cb", "This button no longer works.")]
    assert stopped == []
    assert conn.execute("SELECT state FROM job").fetchone()[0] == "running"


def test_the_word_cancels_the_one_file_being_read_and_core_answers_it(setup):
    attachments, queue, conn, _, _, stopped = setup
    batch(queue, conn)
    assert attachments.cancel_word(OWNER, 11, "  Cancel! ") is True
    assert stopped == [conn.execute("SELECT id FROM job").fetchone()[0]]
    method, _, payload = outbound(conn, f"cancelled:{OWNER}:11")
    assert (method, payload["text"], payload["reply_to"]) == ("sendMessage", "Cancelled. The file is saved.", 11)


def test_the_word_is_the_persons_when_nothing_is_read_or_it_says_more(setup):
    attachments, queue, conn, *_ = setup
    assert attachments.cancel_word(OWNER, 11, "cancel") is False
    batch(queue, conn)
    assert attachments.cancel_word(OWNER, 12, "cancel my insurance") is False
    assert attachments.cancel_word(MEMBER, 13, "cancel") is False  # another chat's files are not this chat's
    assert conn.execute("SELECT state FROM job").fetchone()[0] == "running"


async def test_with_several_files_being_read_the_word_asks_which(setup):
    attachments, queue, conn, _, api, _ = setup
    assert api.answered == []
    first = batch(queue, conn)
    second = batch(queue, conn, "ev2", message_id=11, kind="file", reader="pdf")
    conn.execute("UPDATE evidence SET original_name='report.pdf' WHERE id='ev2'")
    assert attachments.cancel_word(OWNER, 12, "cancel") is True
    _, _, payload = outbound(conn, f"cancel_which:{OWNER}:12")
    labels = [row[0]["text"] for row in payload["reply_markup"]["inline_keyboard"]]
    assert (payload["text"], labels) == ("What should I cancel?", ["🎧 voice message", "📄 report.pdf", "All"])
    confirm(conn, f"cancel_which:{OWNER}:12", 900)
    await attachments.press(press_of(conn, f"cancel_which:{OWNER}:12", 900, index=2))
    states = {row[0] for row in conn.execute("SELECT state FROM job")}
    assert states == {"cancelled"}
    assert conn.execute("SELECT COUNT(*) FROM host_message WHERE cancelled_at IS NOT NULL").fetchone()[0] == 2
    assert outbound(conn, f"edit:cancel_which:{OWNER}:12")[2] == {
        "text": "Cancelled. The file is saved.",
        "message_id": 900,
    }
    assert (first, second) != (None, None)


def test_an_interim_answer_that_never_left_is_withdrawn_and_a_cancel_is_still_told(setup):
    attachments, queue, conn, clock, *_ = setup
    message = batch(queue, conn)
    clock.advance(2.5)
    attachments.tick()  # PENDING: Telegram has not taken it yet
    assert attachments.cancel_word(OWNER, 11, "cancel") is True
    attachments.tick()
    assert outbound(conn, f"interim:{message}")[1] == "FAILED"  # it never leaves now
    assert outbound(conn, f"cancelled:{message}")[2]["text"] == "Cancelled. The file is saved."


def two_files(queue, conn, second_reader="voice"):
    """One batch of a voice note and a second file, both being read."""
    record(conn, "ev1", chat_id=OWNER, message_id=10, kind="voice")
    job_of(conn, "ev1", "voice", "running")
    record(conn, "ev2", chat_id=OWNER, message_id=11, kind="voice" if second_reader == "voice" else "file")
    job_of(conn, "ev2", second_reader, "running")
    return queue.enqueue_attachment(OWNER, [(10, 10, "ev1"), (11, 11, "ev2")])


@pytest.mark.parametrize(
    ("second", "said"),
    [
        ("cancelled", "I couldn't read it. The file is saved."),  # ended by Core's stop, not by the person
        ("queued", "I'll read it later. The file is saved."),  # its reader waits for what the owner installs
    ],
)
def test_the_edit_says_listened_only_when_every_file_was_read(setup, second, said):
    """Review 1's M3: one file read and one not is not "listened"."""
    attachments, queue, conn, clock, *_ = setup
    message = two_files(queue, conn, "pdf")
    clock.advance(2.5)
    attachments.tick()
    confirm(conn, f"interim:{message}", 777)
    conn.execute("UPDATE job SET state='done', pages=0 WHERE evidence_id='ev1'")
    conn.execute("UPDATE job SET state=? WHERE evidence_id='ev2'", (second,))
    queue.readable = lambda: frozenset({"voice"})  # the PDF reader is missing now
    attachments.tick()
    assert outbound(conn, f"interim_edit:{message}")[2]["text"] == said


def test_every_interim_answer_whose_reading_ended_is_edited_in_one_tick(setup):
    attachments, queue, conn, clock, *_ = setup
    first = batch(queue, conn)
    second = batch(queue, conn, "ev2", chat_id=MEMBER, message_id=20)
    clock.advance(2.5)
    attachments.tick()
    confirm(conn, f"interim:{first}", 777)
    confirm(conn, f"interim:{second}", 778)
    conn.execute("UPDATE job SET state='done', pages=0")
    attachments.tick()
    assert outbound(conn, f"interim_edit:{first}") is not None
    assert outbound(conn, f"interim_edit:{second}") is not None


def test_the_tick_finds_its_messages_by_index_however_many_core_keeps(setup):
    """Review 1's M10: the tick runs twice a second; it never reads every message Core ever kept."""
    attachments, *_ = setup
    conn = attachments.conn
    statements = []
    conn.set_trace_callback(statements.append)
    attachments.tick()
    conn.set_trace_callback(None)
    selects = [statement for statement in statements if statement.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 2
    for statement in selects:
        plan = " ".join(str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + statement))
        assert "INDEX host_message_" in plan, plan


def test_a_cancel_after_an_interim_answer_whose_fate_is_unknown_is_still_told(setup):
    """Review 1's M5: Core cannot edit a message it does not know arrived; a reply says the cancel was taken."""
    attachments, queue, conn, clock, *_ = setup
    message = batch(queue, conn)
    clock.advance(2.5)
    attachments.tick()
    conn.execute("UPDATE outbound SET state='UNKNOWN' WHERE idempotency_key=?", (f"interim:{message}",))
    assert attachments.cancel_word(OWNER, 11, "cancel") is True
    attachments.tick()
    assert outbound(conn, f"cancelled:{message}")[2]["text"] == "Cancelled. The file is saved."


async def test_the_button_of_an_interim_answer_whose_fate_is_unknown_still_cancels(setup):
    """Review 1's M5: Telegram's id for the message was never learnt, yet the button on it is the person's."""
    attachments, queue, conn, clock, api, _ = setup
    message = batch(queue, conn)
    clock.advance(2.5)
    attachments.tick()
    conn.execute("UPDATE outbound SET state='UNKNOWN' WHERE idempotency_key=?", (f"interim:{message}",))
    await attachments.press(press_of(conn, f"interim:{message}", 777))
    assert api.answered == [("cb", None)]
    assert conn.execute("SELECT state FROM job").fetchone()[0] == "cancelled"


async def test_a_press_after_the_batch_reached_the_host_cancels_nothing_and_says_so(setup):
    attachments, queue, conn, clock, api, stopped = setup
    message = batch(queue, conn)
    clock.advance(2.5)
    attachments.tick()
    confirm(conn, f"interim:{message}", 777)
    conn.execute("UPDATE job SET state='done', pages=0")
    assert len(queue.serve(None, 100, lambda *_: True)) == 1  # given to the host before the press came
    await attachments.press(press_of(conn, f"interim:{message}", 777))
    assert api.answered == [("cb", "This button no longer works.")]
    assert stopped == []
    assert conn.execute("SELECT cancelled_at FROM host_message WHERE id=?", (message,)).fetchone()[0] is None


def test_the_word_replayed_after_a_crash_is_still_cores_and_answered_once(setup):
    """Review 1's M6: Core took the word, then stopped before the journal marked it; it comes again."""
    attachments, queue, conn, *_ = setup
    batch(queue, conn)
    assert attachments.cancel_word(OWNER, 11, "cancel") is True
    assert attachments.cancel_word(OWNER, 11, "cancel") is True  # not the person's to the model now
    assert [row[0] for row in conn.execute("SELECT idempotency_key FROM outbound")] == [f"cancelled:{OWNER}:11"]


def test_a_cancel_word_of_any_language_klepa_speaks_counts(setup):
    """Review 1's M6: the installation speaks English; a member writes in another of Klepa's languages."""
    attachments, queue, conn, *_ = setup
    batch(queue, conn)
    assert attachments.cancel_word(OWNER, 11, load_locale("ru").cancel_words[0]) is True
    assert conn.execute("SELECT state FROM job").fetchone()[0] == "cancelled"


def test_the_edit_never_says_read_when_a_file_of_the_batch_has_no_reader(setup):
    """The final review's M4: a scan and a ZIP; the scan was read, but the ZIP is not read at all."""
    attachments, queue, conn, clock, *_ = setup
    record(conn, "ev1", chat_id=OWNER, message_id=10, kind="file", name="scan.pdf")
    job_of(conn, "ev1", "pdf", "running")
    record(conn, "ev2", chat_id=OWNER, message_id=11, kind="file", name="photos.zip")  # no reader: no job
    message = queue.enqueue_attachment(OWNER, [(10, 10, "ev1"), (11, 11, "ev2")])
    clock.advance(2.5)
    attachments.tick()
    confirm(conn, f"interim:{message}", 777)
    conn.execute("UPDATE job SET state='done', pages=0")
    attachments.tick()
    assert outbound(conn, f"interim_edit:{message}")[2]["text"] == "I couldn't read it. The file is saved."


class Drafts:
    """The drafts' side of a press: what they were given, and their answer."""

    def __init__(self):
        self.pressed = []

    def press(self, action, chat_id):
        self.pressed.append((action, chat_id))
        return Answer("how to change the order", alert=True)


async def test_a_drafts_button_goes_to_the_drafts_only_once_its_checks_pass(setup):
    attachments, _, conn, clock, api, _ = setup
    attachments.drafts = drafts = Drafts()
    attachments.outbox.enqueue_text("draft_sheet:1:1", OWNER, "a sheet")
    confirm(conn, "draft_sheet:1:1", 500)
    conn.execute(
        "INSERT INTO button_action(id, action, chat_id, outbound_key, expires_at) VALUES (?, ?, ?, ?, ?)",
        ("b" * 32, "draft_order:1:1", OWNER, "draft_sheet:1:1", clock() + 60),
    )
    press = Classified("press", chat_id=OWNER, from_id=OWNER, message_id=500, callback_id="cb", data="b" * 32)
    await attachments.press(press)
    assert drafts.pressed == [("draft_order:1:1", OWNER)]
    assert api.answered == [("cb", "how to change the order", "alert")]
    elsewhere = Classified("press", chat_id=OWNER, from_id=OWNER, message_id=501, callback_id="cb2", data="b" * 32)
    await attachments.press(elsewhere)
    clock.advance(61)
    await attachments.press(press)
    assert drafts.pressed == [("draft_order:1:1", OWNER)]  # on another message, then expired: never theirs
    assert api.answered[1:] == [("cb2", "This button no longer works."), ("cb", "This button no longer works.")]
