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
from collections.abc import Iterable, Mapping

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


def reader_peer(key: bytes, space_id: str) -> int:
    """The sender and chat of a space's background readings (stage 2c-2): a Telegram-shaped id no member has, one per
    space, so the gateway keeps each space's readings in a session of their own, beside the probe peer."""
    digest = hmac.new(key, b"klepa reader peer\x00" + space_id.encode(), hashlib.sha256).digest()
    return 2**51 + int.from_bytes(digest[:8], "big") % 2**51


class Peers:
    def __init__(self, key: bytes, members: Mapping[int, Member], probe: int, spaces: Iterable[str] = ()) -> None:
        self.probe = probe
        self._to_host = {telegram_id: stand_in(key, member.person_id) for telegram_id, member in members.items()}
        self._to_telegram = {host_id: telegram_id for telegram_id, host_id in self._to_host.items()}
        self._readers = {reader_peer(key, space): space for space in spaces}
        ids = [*self._to_telegram, probe, *self._readers]
        if len(self._to_telegram) != len(self._to_host) or len(set(ids)) != len(ids):
            raise ValueError("two stand-ins clash")  # one in 2**50 for a family; never seen

    def host_id(self, telegram_id: int | None) -> int | None:
        """The id the host knows a member by; the probe and reader peers are their own; None for anyone else."""
        if telegram_id == self.probe or telegram_id in self._readers:
            return telegram_id
        return None if telegram_id is None else self._to_host.get(telegram_id)

    def telegram_id(self, host_id: int | None) -> int | None:
        """The member a stand-in stands for; the probe and reader peers are their own; None for any other id."""
        if host_id == self.probe or host_id in self._readers:
            return host_id
        return None if host_id is None else self._to_telegram.get(host_id)

    def reader_space(self, peer: int | None) -> str | None:
        """The space whose readings a reader peer sends; None for any other id."""
        return None if peer is None else self._readers.get(peer)

    def reader_peers(self) -> dict[int, str]:
        return dict(self._readers)
