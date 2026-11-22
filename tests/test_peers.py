import pytest

from helpers import MEMBER, OWNER, STRANGER
from klepa_core.host import peers as module
from klepa_core.host.peers import Peers, stand_in

KEY = b"k" * 32
PROBE = 2**51 + 7


def test_a_stand_in_is_the_same_on_every_start_and_shaped_like_a_telegram_id():
    assert stand_in(KEY, "member") == stand_in(KEY, "member")
    assert 2**51 <= stand_in(KEY, "member") < 2**52
    assert stand_in(KEY, "member") != stand_in(KEY, "owner")
    assert stand_in(KEY, "member") != stand_in(b"j" * 32, "member")  # another installation, other stand-ins


def test_the_host_knows_members_by_stand_ins_and_core_turns_them_back(core_db):
    cfg, _, _ = core_db
    peers = Peers(KEY, cfg.members_by_telegram_id(), PROBE)
    member, owner = peers.host_id(MEMBER), peers.host_id(OWNER)
    assert (member, owner) == (stand_in(KEY, "member"), stand_in(KEY, "owner"))
    assert (peers.telegram_id(member), peers.telegram_id(owner)) == (MEMBER, OWNER)
    assert peers.host_id(PROBE) == peers.telegram_id(PROBE) == PROBE  # the probe peer is a stand-in already


@pytest.mark.parametrize("value", [STRANGER, MEMBER, OWNER, None, 0])
def test_any_other_id_from_the_host_stands_for_nobody(core_db, value):
    cfg, _, _ = core_db
    assert Peers(KEY, cfg.members_by_telegram_id(), PROBE).telegram_id(value) is None  # a Telegram id included


def test_a_stranger_has_no_stand_in(core_db):
    cfg, _, _ = core_db
    assert Peers(KEY, cfg.members_by_telegram_id(), PROBE).host_id(STRANGER) is None


def test_stand_ins_that_clash_stop_the_host_from_starting(core_db, monkeypatch):
    cfg, _, _ = core_db
    monkeypatch.setattr(module, "stand_in", lambda key, person_id: PROBE)
    with pytest.raises(ValueError, match="clash"):
        Peers(KEY, cfg.members_by_telegram_id(), PROBE)


def test_each_space_has_a_reader_peer_of_its_own_which_stands_for_itself(core_db):
    """Stage 2c-2: background readings come from a reader peer per space, beside the probe peer: the host's reading of
    a shared photo and of a member's personal scan never share a session."""
    from klepa_core.host.peers import reader_peer

    cfg, _, _ = core_db
    spaces = ("shared", "personal:owner", "personal:member")
    peers = Peers(KEY, cfg.members_by_telegram_id(), PROBE, spaces)
    readers = {reader_peer(KEY, space) for space in spaces}
    assert len(readers) == 3
    assert all(2**51 <= peer < 2**52 for peer in readers)
    for space in spaces:
        peer = reader_peer(KEY, space)
        assert peers.host_id(peer) == peers.telegram_id(peer) == peer
        assert peers.reader_space(peer) == space
    assert peers.reader_space(PROBE) is None
    assert peers.reader_space(peers.host_id(MEMBER)) is None
    assert set(peers.reader_peers()) == readers


def test_a_reader_peer_that_clashes_stops_the_host_from_starting(core_db, monkeypatch):
    cfg, _, _ = core_db
    monkeypatch.setattr(module, "reader_peer", lambda key, space: PROBE)
    with pytest.raises(ValueError, match="clash"):
        Peers(KEY, cfg.members_by_telegram_id(), PROBE, ("shared",))
