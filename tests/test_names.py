import unicodedata

import pytest

from klepa_core.names import MAX_NAME_BYTES, disk_name, sanitize_original_name

EID = "ev0123456789abcdef0123"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Contract.pdf", "Contract.pdf"),
        ("../x.pdf", "_x.pdf"),
        ("/etc/passwd", "_etc_passwd"),
        ("a\\b:c.txt", "a_b_c.txt"),
        ("\u202efdp.exe", "fdp.exe"),
        ("  .hidden  ", "hidden"),
        ("...", "file"),
        ("", "file"),
        (None, "file"),
        ("tab\there.txt", "tabhere.txt"),
    ],
)
def test_sanitize_original_name(raw, expected):
    assert sanitize_original_name(raw) == expected


def test_sanitize_normalizes_to_nfc():
    nfd = unicodedata.normalize("NFD", "Café.pdf")
    assert sanitize_original_name(nfd) == unicodedata.normalize("NFC", "Café.pdf")


def test_sanitize_is_nfc_after_dropping_format_characters():
    # A zero-width space keeps the accent apart during a first normalization.
    assert sanitize_original_name("e\u200b\u0301.txt") == unicodedata.normalize("NFC", "e\u0301.txt")


def test_disk_name_prefixes_evidence_id():
    assert disk_name(EID, "Scan.pdf") == f"{EID}-Scan.pdf"


def test_disk_name_is_at_most_255_bytes_and_keeps_extension():
    name = disk_name(EID, "é" * 300 + ".pdf")
    assert len(name.encode("utf-8")) <= MAX_NAME_BYTES
    assert name.startswith(f"{EID}-")
    assert name.endswith(".pdf")


def test_disk_name_never_contains_separators():
    for raw in ["../../x", "a/b/c", "..\\..\\x"]:
        assert "/" not in disk_name(EID, raw)


def test_card_lookalike_cannot_collide_with_card_file():
    name = disk_name(EID, "Contract.pdf.json")
    assert name == f"{EID}-Contract.pdf.json"
    assert name != f"{EID}.card.json"
