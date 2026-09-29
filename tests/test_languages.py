"""Checks of the language data itself. The only test module with non-English text."""
import pytest

from klepa_core.locale import load_locale
from klepa_core.spaces import is_private_caption


@pytest.mark.parametrize(
    "code, n, expected",
    [
        ("ru", 2, "📄 получила 2 файла"), ("ru", 5, "📄 получила 5 файлов"), ("ru", 11, "📄 получила 11 файлов"),
        ("ru", 14, "📄 получила 14 файлов"), ("ru", 21, "📄 получила 21 файл"), ("ru", 22, "📄 получила 22 файла"),
        ("uk", 2, "📄 отримала 2 файли"), ("uk", 5, "📄 отримала 5 файлів"), ("uk", 11, "📄 отримала 11 файлів"),
        ("uk", 21, "📄 отримала 21 файл"),
        ("sr", 2, "📄 primila sam 2 fajla"), ("sr", 5, "📄 primila sam 5 fajlova"),
        ("sr", 12, "📄 primila sam 12 fajlova"), ("sr", 21, "📄 primila sam 21 fajl"),
    ],
)
def test_file_receipts(code, n, expected):
    assert load_locale(code).receipt(["file"] * n) == expected


@pytest.mark.parametrize(
    "code, one_file, one_voice, voices",
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
    "caption, private",
    [
        ("только для меня", True),
        ("Только для меня: анализы", True),
        ("Лично!", True),
        ("это личное", True),
        ("отлично, вот документы", False),
        ("наличные", False),
        ("удостоверение личности", False),
    ],
)
def test_russian_private_keywords(caption, private):
    assert is_private_caption(caption, load_locale("ru").private_keywords) is private


@pytest.mark.parametrize(
    "code, caption, private",
    [
        ("uk", "тільки для мене", True), ("uk", "Особисто!", True), ("uk", "посвідчення особи", False),
        ("sr", "samo za mene", True), ("sr", "Lično!", True), ("sr", "lična karta", False),
    ],
)
def test_other_private_keywords(code, caption, private):
    assert is_private_caption(caption, load_locale(code).private_keywords) is private
