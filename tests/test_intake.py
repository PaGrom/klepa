import pytest

from klepa_core.config import Member
from klepa_core.gatekeeper.intake import classify

MEMBERS = {111111: Member("owner", 111111, "Owner", "owner"), 222222: Member("member", 222222, "Member", "member")}


def msg(from_id=111111, chat_id=None, chat_type="private", is_bot=False, **fields):
    message = {"message_id": 5, "date": 1790000000,
               "chat": {"id": from_id if chat_id is None else chat_id, "type": chat_type},
               "from": {"id": from_id, "is_bot": is_bot, "first_name": "T"}}
    message.update(fields)
    return {"update_id": 1, "message": message}


def test_document_from_member():
    document = {"file_id": "F", "file_unique_id": "U", "file_size": 10, "file_name": "a.pdf",
                "mime_type": "application/pdf"}
    c = classify(msg(document=document, caption="чек"), MEMBERS)
    assert (c.action, c.person_id, c.caption, c.message_id) == ("attachment", "owner", "чек", 5)
    assert (c.attachment.kind, c.attachment.file_name, c.attachment.file_size) == ("file", "a.pdf", 10)


def test_largest_photo_is_picked():
    photo = [{"file_id": "s", "file_size": 100, "width": 90, "height": 60},
             {"file_id": "b", "file_size": 9000, "width": 1280, "height": 960}]
    assert classify(msg(photo=photo), MEMBERS).attachment.file_id == "b"


def test_voice_and_forwarded_flag():
    c = classify(msg(voice={"file_id": "V", "duration": 3}, forward_origin={"type": "user"}), MEMBERS)
    assert (c.attachment.kind, c.forwarded) == ("voice", True)


@pytest.mark.parametrize("text, command", [("/start", "start"), ("/queue@klepa_bot steer", "queue"), ("/MODEL", "model")])
def test_commands(text, command):
    c = classify(msg(text=text), MEMBERS)
    assert (c.action, c.command) == ("command", command)


def test_plain_text():
    assert classify(msg(text="привет"), MEMBERS).action == "text"


@pytest.mark.parametrize(
    "update, reason",
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


@pytest.mark.parametrize("kind", ["edited_message", "channel_post", "business_message", "callback_query", "my_chat_member"])
def test_other_update_types_are_ignored(kind):
    c = classify({"update_id": 1, kind: {}}, MEMBERS)
    assert (c.action, c.reason) == ("ignore", f"update_type:{kind}")


def test_sticker_is_unsupported():
    assert classify(msg(sticker={"file_id": "S"}), MEMBERS).action == "unsupported"
