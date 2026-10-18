import pytest

from helpers import MEMBER, OWNER, STRANGER
from klepa_core.config import Member
from klepa_core.gatekeeper.intake import classify

MEMBERS = {111111: Member("owner", 111111, "Owner", "owner"), 222222: Member("member", 222222, "Member", "member")}


def msg(from_id=111111, chat_id=None, chat_type="private", is_bot=False, **fields):
    message = {
        "message_id": 5,
        "date": 1790000000,
        "chat": {"id": from_id if chat_id is None else chat_id, "type": chat_type},
        "from": {"id": from_id, "is_bot": is_bot, "first_name": "T"},
    }
    message.update(fields)
    return {"update_id": 1, "message": message}


def test_document_from_member():
    document = {
        "file_id": "F",
        "file_unique_id": "U",
        "file_size": 10,
        "file_name": "a.pdf",
        "mime_type": "application/pdf",
    }
    c = classify(msg(document=document, caption="receipt"), MEMBERS)
    assert (c.action, c.person_id, c.caption, c.message_id) == ("attachment", "owner", "receipt", 5)
    assert (c.attachment.kind, c.attachment.file_name, c.attachment.file_size) == ("file", "a.pdf", 10)


def test_largest_photo_is_picked():
    photo = [
        {"file_id": "s", "file_size": 100, "width": 90, "height": 60},
        {"file_id": "b", "file_size": 9000, "width": 1280, "height": 960},
    ]
    assert classify(msg(photo=photo), MEMBERS).attachment.file_id == "b"


def test_voice_and_forwarded_flag():
    c = classify(msg(voice={"file_id": "V", "duration": 3}, forward_origin={"type": "user"}), MEMBERS)
    assert (c.attachment.kind, c.forwarded) == ("voice", True)


@pytest.mark.parametrize(
    ("text", "command"), [("/start", "start"), ("/queue@klepa_bot steer", "queue"), ("/MODEL", "model")]
)
def test_commands(text, command):
    c = classify(msg(text=text), MEMBERS)
    assert (c.action, c.command) == ("command", command)


def test_plain_text():
    assert classify(msg(text="hello"), MEMBERS).action == "text"


@pytest.mark.parametrize(
    ("update", "reason"),
    [
        (msg(from_id=999999), "not_member"),
        (msg(chat_id=-100, chat_type="group"), "chat_type:group"),
        (msg(is_bot=True), "bot_sender"),
        (msg(chat_id=333), "chat_sender_mismatch"),
    ],
)
def test_rejections(update, reason):
    c = classify(update, MEMBERS)
    assert (c.action, c.reason) == ("reject", reason)


def test_no_sender_is_rejected():
    update = msg()
    del update["message"]["from"]
    assert classify(update, MEMBERS).reason == "no_sender"


@pytest.mark.parametrize("kind", ["edited_message", "channel_post", "business_message", "my_chat_member"])
def test_other_update_types_are_ignored(kind):
    c = classify({"update_id": 1, kind: {}}, MEMBERS)
    assert (c.action, c.reason) == ("ignore", f"update_type:{kind}")


def press(from_id, chat_id, chat_type="private", data="abc"):
    message = {"message_id": 900, "chat": {"id": chat_id, "type": chat_type}}
    return {"update_id": 1, "callback_query": {"id": "cb1", "from": {"id": from_id}, "message": message, "data": data}}


def test_a_members_press_in_their_own_chat_is_taken():
    c = classify(press(MEMBER, MEMBER), MEMBERS)
    assert (c.action, c.chat_id, c.message_id, c.callback_id, c.data) == ("press", MEMBER, 900, "cb1", "abc")


@pytest.mark.parametrize(
    ("update", "reason"),
    [
        (press(STRANGER, STRANGER), "not_member"),
        (press(MEMBER, OWNER), "press_chat"),  # a press can come only from the chat's own person
        (press(MEMBER, -100123, "supergroup"), "press_chat"),
    ],
)
def test_other_presses_are_rejected(update, reason):
    c = classify(update, MEMBERS)
    assert (c.action, c.reason) == ("reject", reason)


def test_sticker_is_unsupported():
    assert classify(msg(sticker={"file_id": "S"}), MEMBERS).action == "unsupported"
