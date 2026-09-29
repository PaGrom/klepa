import json

import pytest

from klepa_core.cards import build_card, card_file_name, read_card, write_card

KEY = b"k" * 32
ROW = {
    "id": "ev00",
    "space_id": "shared",
    "kind": "file",
    "original_name": "Contract.pdf",
    "disk_name": "ev00-Contract.pdf",
    "mime": "application/pdf",
    "size": 3,
    "sha256": "ab" * 32,
    "received_at": "2026-09-28T10:00:00.000+00:00",
    "channel": "telegram",
    "chat_id": 111111,
    "message_id": 7,
    "authenticated_subject": "owner",
    "caption": None,
    "tags": "[]",
}


def test_card_roundtrip(tmp_path):
    card = build_card(ROW)
    path = write_card(tmp_path, card, KEY)
    assert path.name == card_file_name("ev00") == "ev00.card.json"
    assert read_card(path, KEY) == card
    assert card["card_version"] == 1
    assert card["evidence_id"] == "ev00"


def test_tampered_card_is_rejected(tmp_path):
    path = write_card(tmp_path, build_card(ROW), KEY)
    data = json.loads(path.read_text())
    data["authenticated_subject"] = "member"
    path.write_text(json.dumps(data))
    assert read_card(path, KEY) is None


def test_card_without_signature_is_rejected(tmp_path):
    path = tmp_path / "ev00.card.json"
    path.write_text(json.dumps(build_card(ROW)))
    assert read_card(path, KEY) is None


def test_card_is_written_once(tmp_path):
    write_card(tmp_path, build_card(ROW), KEY)
    with pytest.raises(FileExistsError):
        write_card(tmp_path, build_card(ROW), KEY)
