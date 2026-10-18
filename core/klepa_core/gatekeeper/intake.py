"""Classify a raw Telegram update (docs/architecture.md: Intake). Pure function, no I/O.

Only `message` updates and presses of Core's buttons (`callback_query`) from a private chat where chat.id == from.id
== a member are accepted (spec 6.1 item 2). Everything else is rejected (strangers, groups, bots) or ignored (other
update types).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import Member


@dataclass(frozen=True)
class Attachment:
    kind: str
    file_id: str
    file_unique_id: str | None
    file_size: int | None
    file_name: str | None
    mime: str | None


@dataclass(frozen=True)
class Classified:
    action: str
    reason: str | None = None
    from_id: int | None = None
    chat_id: int | None = None
    message_id: int | None = None
    person_id: str | None = None
    date: int | None = None
    text: str | None = None
    caption: str | None = None
    media_group_id: str | None = None
    attachment: Attachment | None = None
    forwarded: bool = False
    command: str | None = None
    callback_id: str | None = None  # a press: Telegram's id for it, and the button's data
    data: str | None = None


def _attachment(msg: dict[str, Any]) -> Attachment | None:
    if "document" in msg:
        d = msg["document"]
        return Attachment(
            "file", d["file_id"], d.get("file_unique_id"), d.get("file_size"), d.get("file_name"), d.get("mime_type")
        )
    if msg.get("photo"):
        best = max(msg["photo"], key=lambda p: (p.get("file_size") or 0, p.get("width", 0) * p.get("height", 0)))
        return Attachment(
            "photo", best["file_id"], best.get("file_unique_id"), best.get("file_size"), None, "image/jpeg"
        )
    if "voice" in msg:
        v = msg["voice"]
        return Attachment(
            "voice", v["file_id"], v.get("file_unique_id"), v.get("file_size"), None, v.get("mime_type", "audio/ogg")
        )
    if "audio" in msg:
        a = msg["audio"]
        return Attachment(
            "audio", a["file_id"], a.get("file_unique_id"), a.get("file_size"), a.get("file_name"), a.get("mime_type")
        )
    for key in ("video", "video_note", "animation"):
        if key in msg:
            v = msg[key]
            return Attachment(
                "video",
                v["file_id"],
                v.get("file_unique_id"),
                v.get("file_size"),
                v.get("file_name"),
                v.get("mime_type", "video/mp4"),
            )
    return None


def _press(callback: dict[str, Any], members: dict[int, Member]) -> Classified:
    """A press of one of Core's buttons, on a message of the bot in a member's private chat."""
    sender = callback.get("from") or {}
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    if sender.get("is_bot") or chat.get("type") != "private" or chat.get("id") != sender.get("id"):
        return Classified("reject", reason="press_chat", from_id=sender.get("id"))
    sender_id = sender.get("id")
    member = members.get(sender_id) if isinstance(sender_id, int) else None
    if member is None or not isinstance(callback.get("id"), str):
        return Classified("reject", reason="not_member", from_id=sender.get("id"))
    return Classified(
        "press",
        from_id=sender["id"],
        chat_id=chat["id"],
        message_id=message.get("message_id"),
        person_id=member.person_id,
        callback_id=callback["id"],
        data=str(callback.get("data") or ""),
    )


def classify(update: dict[str, Any], members: dict[int, Member]) -> Classified:
    kinds = sorted(k for k in update if k != "update_id")
    if kinds == ["callback_query"]:
        return _press(update["callback_query"], members)
    if kinds != ["message"]:
        return Classified("ignore", reason="update_type:" + (",".join(kinds) or "empty"))
    msg = update["message"]
    chat = msg.get("chat") or {}
    sender = msg.get("from")
    if chat.get("type") != "private":
        return Classified("reject", reason=f"chat_type:{chat.get('type')}", from_id=(sender or {}).get("id"))
    if not sender:
        return Classified("reject", reason="no_sender")
    if sender.get("is_bot"):
        return Classified("reject", reason="bot_sender", from_id=sender.get("id"))
    if chat.get("id") != sender.get("id"):
        return Classified("reject", reason="chat_sender_mismatch", from_id=sender.get("id"))
    member = members.get(sender.get("id"))
    if member is None:
        return Classified("reject", reason="not_member", from_id=sender.get("id"))
    base: dict[str, Any] = {
        "from_id": sender["id"],
        "chat_id": chat["id"],
        "message_id": msg["message_id"],
        "person_id": member.person_id,
        "date": msg.get("date"),
        "media_group_id": msg.get("media_group_id"),
        "forwarded": "forward_origin" in msg,
    }
    attachment = _attachment(msg)
    if attachment is not None:
        return Classified("attachment", attachment=attachment, caption=msg.get("caption"), **base)
    text = msg.get("text")
    if isinstance(text, str):
        if text.startswith("/"):
            command = text.split()[0][1:].split("@")[0].lower()
            return Classified("command", text=text, command=command, **base)
        return Classified("text", text=text, **base)
    return Classified("unsupported", reason="message_kind", **base)
