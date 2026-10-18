"""Who the host knows, and by which id (spec 4.5; the canary test of stage 2b-2).

The host never learns a member's Telegram id: the gateway puts the ids it is given into every prompt it sends to the
model provider. Each member has a stand-in instead, a number of a Telegram user id's shape, derived from the adapter's
key and the member's person id: the same on every start, and kept when the member's Telegram account changes. Core
gives the host messages from stand-ins, takes the host's messages to stand-ins, and turns stand-ins back into Telegram
ids at the gatekeeper. The live probe's peer is a stand-in of its own already.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping

from ..config import Member


def probe_peer(key: bytes) -> int:
    """A Telegram-shaped user id no member has: the sender and chat of the live probe (spec 4.6). It lies in
    [2**51, 2**52), inside the 52 bits Telegram ids use and far above real ones, and it is the same on every start."""
    digest = hmac.new(key, b"klepa probe peer", hashlib.sha256).digest()
    return 2**51 + int.from_bytes(digest[:8], "big") % 2**51


def stand_in(key: bytes, person_id: str) -> int:
    """A member's id for the host. It lies in [2**51, 2**52), inside the 52 bits Telegram ids use and far above real
    ones, like the probe peer's."""
    digest = hmac.new(key, b"klepa host peer\x00" + person_id.encode(), hashlib.sha256).digest()
    return 2**51 + int.from_bytes(digest[:8], "big") % 2**51


class Peers:
    def __init__(self, key: bytes, members: Mapping[int, Member], probe: int) -> None:
        self.probe = probe
        self._to_host = {telegram_id: stand_in(key, member.person_id) for telegram_id, member in members.items()}
        self._to_telegram = {host_id: telegram_id for telegram_id, host_id in self._to_host.items()}
        if len(self._to_telegram) != len(self._to_host) or probe in self._to_telegram:
            raise ValueError("two stand-ins clash")  # one in 2**50 for a family; never seen

    def host_id(self, telegram_id: int | None) -> int | None:
        """The id the host knows a member by; the probe peer is its own; None for anyone else."""
        if telegram_id == self.probe:
            return self.probe
        return None if telegram_id is None else self._to_host.get(telegram_id)

    def telegram_id(self, host_id: int | None) -> int | None:
        """The member a stand-in stands for; the probe peer is its own; None for any other id."""
        if host_id == self.probe:
            return self.probe
        return None if host_id is None else self._to_telegram.get(host_id)
