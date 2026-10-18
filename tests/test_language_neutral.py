"""The engine's code and its tests are written in no family's language: what a person reads lives in the locales, and
only the language tests hold it (the owner's rule)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CYRILLIC = re.compile("[\u0400-\u04ff]")
LANGUAGE_TESTS = {"tests/test_languages.py"}


def test_no_code_or_test_holds_cyrillic_outside_the_language_tests():
    found = []
    for path in sorted([*ROOT.glob("core/**/*.py"), *ROOT.glob("core/**/*.ts"), *ROOT.glob("tests/**/*.py")]):
        relative = path.relative_to(ROOT).as_posix()
        if relative in LANGUAGE_TESTS or "node_modules" in path.parts:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if CYRILLIC.search(line):
                found.append(f"{relative}:{number}")
    assert found == []
