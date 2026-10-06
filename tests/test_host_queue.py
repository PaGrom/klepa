import pytest

from helpers import MEMBER, OWNER, Clock
from klepa_core.events import EventLog
from klepa_core.gatekeeper.outbox import Outbox
from klepa_core.host.queue import FORWARDED_MARK, MODEL_PROBE_TEXT, PROBE_TEXT, HostQueue

PEER = 2**51 + 7


def everything(host_message_id, kind):
    return True


def nothing(host_message_id, kind):
    return False


@pytest.fixture
def setup(core_db):
    cfg, conn, journal = core_db
    clock = Clock()
    queue = HostQueue(conn, journal, cfg.members_by_telegram_id(), PEER, clock=clock)
    return queue, journal, conn, clock


def enqueue(queue, journal, update_id, sender, message_id, text="hello", **extra):
    message = {
        "message_id": message_id,
        "date": 1_700_000_000,
        "chat": {"id": sender, "type": "private"},
        "from": {"id": sender, "is_bot": False, "first_name": "Someone"},
        "text": text,
        **extra,
    }
    journal.append_batch([{"update_id": update_id, "message": message}], "t")
    return queue.enqueue(update_id, sender, message_id, sender, 1_700_000_000, forwarded="forward_origin" in extra)


def test_serves_a_fixed_set_of_fields(setup):
    queue, journal, _, clock = setup
    enqueue(
        queue,
        journal,
        1,
        OWNER,
        10,
        entities=[{"type": "text_link", "offset": 0, "length": 5, "url": "https://x.example"}],
        reply_to_message={"message_id": 3, "text": "quoted"},
    )
    [update] = queue.serve(None, 100, everything)
    assert update["update_id"] >= int(clock())
    assert update["message"] == {
        "message_id": 10,
        "from": {"id": OWNER, "is_bot": False, "first_name": "Owner"},
        "chat": {"id": OWNER, "type": "private", "first_name": "Owner"},
        "date": 1_700_000_000,
        "text": "hello",
    }


def test_forwarded_text_is_marked(setup):
    queue, journal, _, _ = setup
    enqueue(queue, journal, 1, OWNER, 10, text="look", forward_origin={"type": "hidden_user"})
    [update] = queue.serve(None, 100, everything)
    assert update["message"]["text"] == f"{FORWARDED_MARK}\nlook"


def test_the_same_message_under_a_new_update_id_is_kept_once(setup):
    queue, journal, _, _ = setup
    assert enqueue(queue, journal, 1, OWNER, 10)
    assert not enqueue(queue, journal, 2, OWNER, 10)
    assert len(queue.serve(None, 100, everything)) == 1


def test_unacknowledged_updates_come_again_until_the_offset_passes_them(setup):
    queue, journal, _, _ = setup
    enqueue(queue, journal, 1, OWNER, 10)
    [first] = queue.serve(None, 100, everything)
    assert queue.serve(None, 100, everything) == [first]
    assert queue.serve(first["update_id"] + 1, 100, everything) == []


def test_nothing_is_given_again_while_serving_is_not_allowed(setup):
    # A restarted gateway that has not passed the start gate must not get even what its last boot was given.
    queue, journal, _, _ = setup
    enqueue(queue, journal, 1, OWNER, 10)
    [first] = queue.serve(None, 100, everything)
    assert queue.serve(None, 100, nothing) == []
    assert queue.serve(None, 100, everything) == [first]


def test_update_ids_never_go_down_even_when_the_clock_does(setup):
    queue, journal, _, clock = setup
    enqueue(queue, journal, 1, OWNER, 10)
    [first] = queue.serve(None, 100, everything)
    clock.advance(-3600)  # a restored core.db, or a clock set back
    enqueue(queue, journal, 2, OWNER, 11)
    [second] = queue.serve(first["update_id"] + 1, 100, everything)
    assert second["update_id"] > first["update_id"]


def test_messages_wait_until_they_may_be_served_and_keep_their_order(setup):
    queue, journal, _, _ = setup
    enqueue(queue, journal, 1, OWNER, 10, text="a")
    enqueue(queue, journal, 2, MEMBER, 20, text="b")
    enqueue(queue, journal, 3, OWNER, 11, text="c")
    assert queue.serve(None, 100, nothing) == []
    assert [u["message"]["text"] for u in queue.serve(None, 100, everything)] == ["a", "b", "c"]


def test_limit_caps_one_answer(setup):
    queue, journal, _, _ = setup
    for i in range(3):
        enqueue(queue, journal, i + 1, OWNER, 10 + i)
    assert len(queue.serve(None, 2, everything)) == 2


def test_the_probe_is_a_message_of_its_own_peer_and_is_never_served_after_it_is_retired(setup):
    queue, _, _, _ = setup
    probe = queue.add_probe()
    [update] = queue.serve(None, 100, lambda host_message_id, kind: host_message_id == probe)
    message = update["message"]
    assert (message["from"]["id"], message["chat"]["id"], message["text"]) == (PEER, PEER, PROBE_TEXT)
    stale = queue.add_probe()
    queue.retire(stale)
    assert queue.serve(update["update_id"] + 1, 100, everything) == []


def test_probe_message_ids_grow_with_the_clock(setup):
    queue, _, conn, clock = setup
    first = queue.add_probe()
    clock.advance(-10_000)
    second = queue.add_probe()
    ids = [row[0] for row in conn.execute("SELECT message_id FROM host_message WHERE id IN (?, ?)", (first, second))]
    assert ids[1] > ids[0] >= int(clock()) - 1


def test_a_conversation_stays_open_ten_minutes_after_the_answer(setup):
    queue, journal, _, clock = setup
    enqueue(queue, journal, 1, OWNER, 10)
    assert not queue.conversation_open(OWNER)  # not issued yet
    queue.serve(None, 100, everything)
    assert queue.conversation_open(OWNER)
    queue.mark_answered(OWNER)
    clock.advance(599)
    assert queue.conversation_open(OWNER)
    clock.advance(2)
    assert not queue.conversation_open(OWNER)
    assert not queue.conversation_open(MEMBER)


def test_an_answer_goes_to_the_turns_message_else_to_the_oldest(setup):
    queue, journal, _, _ = setup
    enqueue(queue, journal, 1, OWNER, 10)
    enqueue(queue, journal, 2, OWNER, 11)
    queue.serve(None, 100, everything)
    newest = queue.oldest_unanswered(OWNER, exclude={queue.oldest_unanswered(OWNER)["id"]})["id"]
    assert queue.mark_answered(OWNER, prefer=newest) == newest
    oldest = queue.mark_answered(OWNER)
    assert oldest is not None
    assert oldest != newest
    assert queue.mark_answered(OWNER) is None


def test_oldest_unanswered_skips_other_senders_and_busy_messages(setup):
    queue, journal, _, _ = setup
    enqueue(queue, journal, 1, OWNER, 10)
    enqueue(queue, journal, 2, OWNER, 11)
    queue.serve(None, 100, everything)
    first = queue.oldest_unanswered(OWNER)
    assert queue.oldest_unanswered(OWNER, sender_id=MEMBER) is None
    assert queue.oldest_unanswered(OWNER, exclude={first["id"]})["message_id"] == 11


def test_issued_in_chat_knows_only_issued_messages_of_that_chat(setup):
    queue, journal, _, _ = setup
    enqueue(queue, journal, 1, OWNER, 10)
    assert not queue.issued_in_chat(OWNER, 10)
    queue.serve(None, 100, everything)
    assert queue.issued_in_chat(OWNER, 10)
    assert not queue.issued_in_chat(MEMBER, 10)


def test_hold_replies_go_once_per_ten_minutes_per_waiting_chat(setup):
    queue, journal, conn, clock = setup
    outbox = Outbox(conn, None, EventLog(conn))
    enqueue(queue, journal, 1, OWNER, 10)
    enqueue(queue, journal, 2, OWNER, 11)
    assert queue.send_hold_replies(outbox, "on hold") == 1
    assert queue.send_hold_replies(outbox, "on hold") == 0
    clock.advance(600)
    assert queue.send_hold_replies(outbox, "on hold") == 1
    queue.serve(None, 100, everything)
    clock.advance(600)
    assert queue.send_hold_replies(outbox, "on hold") == 0  # nothing waits any more
    keys = [row[0] for row in conn.execute("SELECT idempotency_key FROM outbound ORDER BY id")]
    assert len(keys) == 2
    assert all(key.startswith(f"hold:{OWNER}:") for key in keys)


def test_a_message_without_its_journal_entry_is_skipped_and_never_waits_again(setup):
    queue, _, _, _ = setup
    queue.enqueue(99, OWNER, 10, OWNER, 1)  # the journal knows no update 99
    assert queue.serve(None, 100, everything) == []
    assert queue.waiting_chats() == []


def test_the_model_probe_asks_the_model_and_a_new_probe_retires_both_kinds(setup):
    queue, _, conn, _ = setup
    hook = queue.add_probe()
    model = queue.add_probe("model_probe")  # the gate's second probe; retires the first
    [update] = queue.serve(None, 100, everything)
    message = update["message"]
    assert (message["chat"]["id"], message["text"]) == (PEER, MODEL_PROBE_TEXT)
    queue.add_probe()  # a new gate retires the model probe too
    kinds = dict(conn.execute("SELECT id, answered_at IS NOT NULL FROM host_message WHERE id IN (?, ?)", (hook, model)))
    assert kinds == {hook: 1, model: 1}
    queue.retire(model)  # retiring twice changes nothing
