"""Checks of the language data itself. The only test module with non-English text."""

import pytest

from klepa_core.locale import load_locale
from klepa_core.search import find, index_names, index_pages
from klepa_core.spaces import is_private_caption


@pytest.mark.parametrize(
    ("code", "n", "expected"),
    [
        ("ru", 2, "📄 получила 2 файла"),
        ("ru", 5, "📄 получила 5 файлов"),
        ("ru", 11, "📄 получила 11 файлов"),
        ("ru", 14, "📄 получила 14 файлов"),
        ("ru", 21, "📄 получила 21 файл"),
        ("ru", 22, "📄 получила 22 файла"),
        ("uk", 2, "📄 отримала 2 файли"),
        ("uk", 5, "📄 отримала 5 файлів"),
        ("uk", 11, "📄 отримала 11 файлів"),
        ("uk", 21, "📄 отримала 21 файл"),
        ("sr", 2, "📄 primila sam 2 fajla"),
        ("sr", 5, "📄 primila sam 5 fajlova"),
        ("sr", 12, "📄 primila sam 12 fajlova"),
        ("sr", 21, "📄 primila sam 21 fajl"),
    ],
)
def test_file_receipts(code, n, expected):
    assert load_locale(code).receipt(["file"] * n) == expected


@pytest.mark.parametrize(
    ("code", "one_file", "one_voice", "voices"),
    [
        ("ru", "📄 получила", "🎧 получила голосовое", "🎧 получила голосовые: 3"),
        ("uk", "📄 отримала", "🎧 отримала голосове", "🎧 отримала голосові: 3"),
        ("sr", "📄 primila sam", "🎧 primila sam glasovnu poruku", "🎧 primila sam glasovne poruke: 3"),
    ],
)
def test_single_and_voice_receipts(code, one_file, one_voice, voices):
    loc = load_locale(code)
    assert (loc.receipt(["photo"]), loc.receipt(["voice"]), loc.receipt(["voice"] * 3)) == (one_file, one_voice, voices)


@pytest.mark.parametrize(
    ("caption", "private"),
    [
        ("только для меня", True),
        ("Только для меня: анализы", True),
        ("Лично!", True),
        ("это личное", True),
        ("отлично, вот документы", False),
        ("наличные", False),
        ("удостоверение личности", False),
        ("личные фото", True),
        ("личный паспорт", True),
        ("личная переписка", True),
    ],
)
def test_russian_private_keywords(caption, private):
    assert is_private_caption(caption, load_locale("ru").private_keywords) is private


@pytest.mark.parametrize(
    ("code", "caption", "private"),
    [
        ("uk", "тільки для мене", True),
        ("uk", "Особисто!", True),
        ("uk", "посвідчення особи", False),
        ("uk", "особисті фото", True),
        ("uk", "особистий документ", True),
        ("sr", "samo za mene", True),
        ("sr", "Lično!", True),
        ("sr", "lična karta", False),
        ("sr", "privatne slike", True),
        ("en", "personal documents", True),
    ],
)
def test_other_private_keywords(code, caption, private):
    assert is_private_caption(caption, load_locale(code).private_keywords) is private


@pytest.mark.parametrize("code", ["ru", "uk", "sr"])
def test_service_texts_render(code):
    loc = load_locale(code)
    line = loc.service_text(
        "line",
        headline=loc.service_text("attention"),
        host=loc.service_text("host_paused"),
        last_intake=loc.service_text("never"),
        snapshot=loc.service_text("no_snapshot"),
        documents=loc.service_text("unavailable", error="PermissionError"),
        pending_copies=2,
        failed_copies=1,
        unknown_sends=0,
    )
    assert "PermissionError" in line
    assert "{" not in line
    assert loc.service_text("status_button")


def _record(conn, record_id, *, name=None, caption=None, pages=()):
    conn.execute(
        "INSERT INTO evidence(id, space_id, kind, original_name, received_at, channel, chat_id, message_id, "
        "update_id, authenticated_subject, ingest_key, state, caption) "
        "VALUES (?, 'shared', 'file', ?, '2026-10-07T10:00:00Z', 'telegram', 1, ?, 1, 'owner', ?, 'processed', ?)",
        (record_id, name, abs(hash(record_id)) % 100_000, f"k:{record_id}", caption),
    )
    index_names(conn, record_id, name, caption)
    index_pages(conn, record_id, list(pages))


@pytest.mark.parametrize(
    ("written", "stem"),
    [
        ("справка для ребёнка", "ребенк"),
        ("справка для ребенка", "ребёнк"),
        ("Приём у врача", "прием"),
        ("всё оплачено", "все"),
        ("Ёлка во дворе", "елк"),
        ("елка во дворе", "Ёлк"),
    ],
)
def test_search_finds_yo_written_as_ye_and_back(core_db, written, stem):
    """OCR and whisper mostly write ye where people and the model write yo; SQLite folds only Latin marks (finding
    I7)."""
    conn = core_db[1]
    _record(conn, "ev-page", pages=(written,))
    _record(conn, "ev-caption", name="scan.pdf", caption=written)
    found = {hit.evidence_id for hit in find(conn, ("shared",), ("processed",), [stem], 10)}
    assert found == {"ev-page", "ev-caption"}
