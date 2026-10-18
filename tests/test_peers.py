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
