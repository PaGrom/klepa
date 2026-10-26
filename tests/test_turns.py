import pytest

from helpers import HOST_MEMBER, HOST_OWNER, MEMBER, OWNER, Clock, host_peers, job_of, record
from klepa_core.host.queue import HostQueue, text_digest
from klepa_core.host.turns import TURN_TTL_SECONDS, Prompt, TurnRegistry, session_of

PEER = 2**51 + 7
BOOT = "boot-aaaaaaaa"


def everything(host_message_id, kind):
    return True


@pytest.fixture
def setup(core_db):
    cfg, conn, journal = core_db
    clock = Clock()
    queue = HostQueue(conn, journal, cfg.members_by_telegram_id(), host_peers(cfg, PEER), clock=clock)
    return TurnRegistry(conn, queue, clock=clock), queue, journal, clock


def issue(queue, journal, update_id, sender, message_id, text="hi"):
    """The text the host got for the message, as Core served it."""
    message = {"message_id": message_id, "date": 1, "chat": {"id": sender}, "from": {"id": sender}, "text": text}
    journal.append_batch([{"update_id": update_id, "message": message}], "t")
    queue.enqueue(update_id, sender, message_id, sender, 1)
    served = [update["message"]["text"] for update in queue.serve(None, 100, everything)]
    return served[-1] if served else None


def prompt_of(*texts):
    """What before_agent_run gets as the prompt of a run of these texts: the gateway joins the fragments of a long
    text with nothing between them."""
    return Prompt(*text_digest("".join(texts)))


def key(peer):
    """The host's session of a member is under the member's stand-in."""
    return f"agent:main:telegram:direct:{ {OWNER: HOST_OWNER, MEMBER: HOST_MEMBER}.get(peer, peer) }"


def start(turns, run_id, peer=OWNER, *, prompt=True, text=None):
    if prompt:
        turns.prompt_built(run_id, BOOT, peer, peer)
    return turns.start(run_id, BOOT, peer, peer, key(peer), text)


def test_a_turn_that_answers_an_issued_message_registers(setup):
    turns, queue, journal, _ = setup
    issue(queue, journal, 1, OWNER, 10)
    turn = start(turns, "run-1")
    assert (turn.registered, turn.reason, turn.probe) == (True, "registered", False)
    assert turns.active_message(OWNER) == turn.host_message_id


def test_a_session_under_a_telegram_id_is_refused(setup):
    """The host knows members by stand-ins only: a session under a member's Telegram id is not one Core gave it."""
    turns, queue, journal, _ = setup
    issue(queue, journal, 1, OWNER, 10)
    turns.prompt_built("run-1", BOOT, OWNER, OWNER)
    assert turns.start("run-1", BOOT, OWNER, OWNER, f"agent:main:telegram:direct:{OWNER}").reason == "session"


def test_without_before_prompt_build_the_turn_is_refused(setup):
    turns, queue, journal, _ = setup
    issue(queue, journal, 1, OWNER, 10)
    assert start(turns, "run-1", prompt=False).reason == "no_prompt_build"


def test_a_turn_with_no_issued_message_is_refused(setup):
    turns, _, _, _ = setup
    assert start(turns, "run-1").reason == "no_issued_message"  # like a synthetic turn after a host restart


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        (("run-1", BOOT, OWNER, None, key(OWNER)), "no_sender"),
        (("run-1", BOOT, MEMBER, OWNER, key(OWNER)), "not_private"),
        (("run-1", BOOT, OWNER, OWNER, key(MEMBER)), "session"),
        (("run-1", BOOT, OWNER, OWNER, "agent:main:main"), "session"),
    ],
)
def test_sender_chat_and_session_must_agree(setup, args, reason):
    turns, queue, journal, _ = setup
    issue(queue, journal, 1, OWNER, 10)
    turns.prompt_built("run-1", BOOT, OWNER, OWNER)
    assert turns.start(*args).reason == reason


def test_a_run_prompted_for_one_chat_cannot_start_in_another(setup):
    turns, queue, journal, _ = setup
    issue(queue, journal, 1, MEMBER, 10)
    turns.prompt_built("run-1", BOOT, OWNER, OWNER)
    assert turns.start("run-1", BOOT, MEMBER, MEMBER, key(MEMBER)).reason == "run_mismatch"


def test_the_same_run_may_start_again_only_while_its_message_waits(setup):
    turns, queue, journal, _ = setup
    issue(queue, journal, 1, OWNER, 10)
    first = start(turns, "run-1")
    again = start(turns, "run-1")  # a continuation, or the host's recovery after a restart
    assert (again.registered, again.reason, again.host_message_id) == (True, "repeated", first.host_message_id)
    queue.mark_answered(OWNER, prefer=first.host_message_id)
    assert start(turns, "run-1").reason == "answered"


def test_two_turns_take_two_messages_and_a_third_finds_none(setup):
    turns, queue, journal, _ = setup
    issue(queue, journal, 1, OWNER, 10)
    issue(queue, journal, 2, OWNER, 11)
    first, second = start(turns, "run-1"), start(turns, "run-2")
    assert first.host_message_id != second.host_message_id
    assert start(turns, "run-3").reason == "no_issued_message"


def test_an_expired_registration_frees_its_message(setup):
    turns, queue, journal, clock = setup
    issue(queue, journal, 1, OWNER, 10)
    first = start(turns, "run-1")
    clock.advance(TURN_TTL_SECONDS + 1)
    assert turns.active_message(OWNER) is None
    assert start(turns, "run-2").host_message_id == first.host_message_id


def test_the_probe_turn_is_marked(setup):
    turns, queue, _, _ = setup
    probe = queue.add_probe()
    queue.serve(None, 100, everything)
    turn = start(turns, "run-p", PEER)
    assert (turn.registered, turn.host_message_id, turn.probe) == (True, probe, True)


@pytest.mark.parametrize(
    ("session_key", "ok"),
    [
        (key(OWNER), True),
        (f"agent:other:telegram:direct:{HOST_OWNER}", True),
        (key(MEMBER), False),
        (f"agent:main:telegram:direct:{OWNER}", False),
        (None, False),
    ],
)
def test_session_of(session_key, ok):
    assert session_of(session_key, HOST_OWNER) is ok
    assert session_of(session_key, None) is False


def test_a_probe_left_by_a_stopped_core_never_takes_the_next_probes_turn(setup):
    turns, queue, _, _ = setup
    queue.add_probe()  # Core stopped in the middle of this probe: it was given to the host and never retired
    queue.serve(None, 100, everything)
    probe = queue.add_probe()
    queue.serve(None, 100, everything)
    assert start(turns, "run-p", PEER).host_message_id == probe


def test_a_turn_of_a_gateway_process_that_died_does_not_hold_its_message(setup):
    """Scenario 32: the gateway crashed mid-turn; its own queue runs the turn again in the new process, under a new
    boot, and that run takes the message the dead run still names."""
    turns, queue, journal, _ = setup
    issue(queue, journal, 1, OWNER, 10)
    assert start(turns, "run-old").registered
    turns.prompt_built("run-new", "boot-bbbbbbbb", OWNER, OWNER)
    recovered = turns.start("run-new", "boot-bbbbbbbb", OWNER, OWNER, key(OWNER))
    assert (recovered.registered, recovered.reason) == (True, "registered")
    # within one boot the message stays taken: two live turns never answer one message
    turns.prompt_built("run-same", "boot-bbbbbbbb", OWNER, OWNER)
    assert turns.start("run-same", "boot-bbbbbbbb", OWNER, OWNER, key(OWNER)).reason == "no_issued_message"


def test_a_reply_records_how_the_turn_ended_and_names_what_it_answers(setup):
    turns, queue, journal, _ = setup
    issue(queue, journal, 1, OWNER, 10)
    turn = start(turns, "run-1")
    assert turns.replied("run-1", False) == ("text", turn.host_message_id)
    row = turns.conn.execute("SELECT ended_at, succeeded FROM host_run WHERE run_id='run-1'").fetchone()
    assert row["ended_at"] is not None
    assert row["succeeded"] == 0
    assert turns.replied("run-unknown", True) is None


def test_the_model_probe_turn_is_marked(setup):
    turns, queue, _, _ = setup
    queue.add_probe("model_probe")
    queue.serve(None, 100, everything)
    turns.prompt_built("run-m", BOOT, PEER, PEER)
    turn = turns.start("run-m", BOOT, PEER, PEER, key(PEER))
    assert (turn.registered, turn.probe, turn.model_probe) == (True, False, True)


def test_a_turn_on_a_read_attachment_is_its_senders_like_any_message(setup, core_db):
    """Scenario 18: a turn on a processed attachment registers for the person who sent it; another member's run finds
    nothing of theirs to answer."""
    turns, queue, _, _ = setup
    _, conn, _ = core_db
    record(conn, "ev1", chat_id=OWNER, message_id=10, kind="voice")
    job_of(conn, "ev1", "voice", "done", "buy milk")
    queue.enqueue_attachment(OWNER, [(1, 10, "ev1")])
    queue.serve(None, 100, everything)
    assert start(turns, "run-m", MEMBER).reason == "no_issued_message"
    turn = start(turns, "run-1")
    assert (turn.registered, turn.reason) == (True, "registered")
    assert turns.active_message(OWNER) == turn.host_message_id


def answered(queue, *ids):
    rows = queue.conn.execute(
        f"SELECT id FROM host_message WHERE answered_at IS NOT NULL AND id IN ({','.join('?' * len(ids))})", ids
    ).fetchall()
    return {int(row[0]) for row in rows}


def test_a_turn_takes_the_message_its_prompt_is_and_its_answer_closes_the_older_ones(setup):
    """The live run of 2026-10-26: a chat one message behind since a run that answered two. A turn takes the message
    its prompt is, not the chat's oldest, and its answer closes every older one: they get no turn of their own."""
    turns, queue, journal, _ = setup
    issue(queue, journal, 1, OWNER, 10, "left behind")
    text = issue(queue, journal, 2, OWNER, 11, "the album")
    turn = start(turns, "run-1", text=prompt_of(text))
    stale, album = (int(row[0]) for row in queue.conn.execute("SELECT id FROM host_message ORDER BY id"))
    assert turn.host_message_id == album
    queue.mark_answered(OWNER, prefer=album)
    assert answered(queue, stale, album) == {stale, album}


def test_a_turn_of_a_long_text_and_the_next_one_takes_the_newer(setup):
    """The gateway joins a text of 4000 characters or more with the next message, as fragments of one long text
    (OpenClaw 2026.9.4, its Telegram ingress): one turn for two messages."""
    turns, queue, journal, _ = setup
    long = issue(queue, journal, 1, OWNER, 10, "L" * 4090)
    short = issue(queue, journal, 2, OWNER, 11, "beta two")
    first, second = (int(row[0]) for row in queue.conn.execute("SELECT id FROM host_message ORDER BY id"))
    turn = start(turns, "run-1", text=prompt_of(long, short))
    assert turn.host_message_id == second
    queue.mark_answered(OWNER, prefer=second)
    assert answered(queue, first, second) == {first, second}


def test_a_prompt_core_cannot_match_takes_the_oldest_as_before(setup):
    turns, queue, journal, _ = setup
    issue(queue, journal, 1, OWNER, 10, "first")
    issue(queue, journal, 2, OWNER, 11, "second")
    oldest = int(queue.conn.execute("SELECT MIN(id) FROM host_message").fetchone()[0])
    assert start(turns, "run-1", text=prompt_of("something the gateway made")).host_message_id == oldest
    assert start(turns, "run-2", text=None).host_message_id == oldest + 1


def test_two_messages_of_one_text_are_taken_in_turn(setup):
    turns, queue, journal, _ = setup
    one = issue(queue, journal, 1, OWNER, 10, "ok")
    issue(queue, journal, 2, OWNER, 11, "ok")
    first = start(turns, "run-1", text=prompt_of(one))
    second = start(turns, "run-2", text=prompt_of(one))
    assert second.host_message_id == first.host_message_id + 1


def test_an_answer_leaves_an_older_message_whose_own_turn_runs(setup):
    turns, queue, journal, _ = setup
    one = issue(queue, journal, 1, OWNER, 10, "first")
    two = issue(queue, journal, 2, OWNER, 11, "second")
    first = start(turns, "run-1", text=prompt_of(one))
    second = start(turns, "run-2", text=prompt_of(two))
    queue.mark_answered(OWNER, prefer=second.host_message_id)
    assert answered(queue, first.host_message_id, second.host_message_id) == {second.host_message_id}


def test_an_older_message_core_has_no_digest_of_is_taken_first(setup):
    """The review's I1: a message issued before Core restarted has no digest; its run comes first, so a newer message
    of the same text must not take it."""
    turns, queue, journal, _ = setup
    issue(queue, journal, 1, OWNER, 10, "yes")
    text = issue(queue, journal, 2, OWNER, 11, "yes")
    oldest = int(queue.conn.execute("SELECT MIN(id) FROM host_message").fetchone()[0])
    del queue.digests[oldest]  # it was served before Core restarted
    assert start(turns, "run-1", text=prompt_of(text)).host_message_id == oldest


def test_fragments_are_only_messages_of_consecutive_ids(setup):
    """The review's M3: the gateway joins a long text only with the message of the next id."""
    turns, queue, journal, _ = setup
    long = issue(queue, journal, 1, OWNER, 10, "L" * 4090)
    short = issue(queue, journal, 2, OWNER, 12, "beta two")
    oldest = int(queue.conn.execute("SELECT MIN(id) FROM host_message").fetchone()[0])
    assert start(turns, "run-1", text=prompt_of(long, short)).host_message_id == oldest


def test_a_message_served_again_with_another_text_is_known_by_either(setup):
    """The review's M4: an attachment's mark may change before the host takes it again after a hiccup."""
    turns, queue, journal, _ = setup
    issue(queue, journal, 1, OWNER, 10, "left behind")
    text = issue(queue, journal, 2, OWNER, 11, "as first served")
    newer = int(queue.conn.execute("SELECT MAX(id) FROM host_message").fetchone()[0])
    queue._keep_digest(newer, {"text": "as served again"})
    assert start(turns, "run-1", text=prompt_of(text)).host_message_id == newer
