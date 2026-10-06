import pytest

from helpers import MEMBER, OWNER, Clock
from klepa_core.host.queue import HostQueue
from klepa_core.host.turns import TURN_TTL_SECONDS, TurnRegistry, session_of

PEER = 2**51 + 7
BOOT = "boot-aaaaaaaa"


def everything(host_message_id, kind):
    return True


@pytest.fixture
def setup(core_db):
    cfg, conn, journal = core_db
    clock = Clock()
    queue = HostQueue(conn, journal, cfg.members_by_telegram_id(), PEER, clock=clock)
    return TurnRegistry(conn, queue, clock=clock), queue, journal, clock


def issue(queue, journal, update_id, sender, message_id):
    message = {"message_id": message_id, "date": 1, "chat": {"id": sender}, "from": {"id": sender}, "text": "hi"}
    journal.append_batch([{"update_id": update_id, "message": message}], "t")
    queue.enqueue(update_id, sender, message_id, sender, 1)
    queue.serve(None, 100, everything)


def key(peer):
    return f"agent:main:telegram:direct:{peer}"


def start(turns, run_id, peer=OWNER, *, prompt=True):
    if prompt:
        turns.prompt_built(run_id, BOOT, peer, peer)
    return turns.start(run_id, BOOT, peer, peer, key(peer))


def test_a_turn_that_answers_an_issued_message_registers(setup):
    turns, queue, journal, _ = setup
    issue(queue, journal, 1, OWNER, 10)
    turn = start(turns, "run-1")
    assert (turn.registered, turn.reason, turn.probe) == (True, "registered", False)
    assert turns.active_message(OWNER) == turn.host_message_id


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
    [(key(OWNER), True), (f"agent:other:telegram:direct:{OWNER}", True), (key(MEMBER), False), (None, False)],
)
def test_session_of(session_key, ok):
    assert session_of(session_key, OWNER) is ok


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
