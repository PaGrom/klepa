"""Language data for everything Core says itself and for private caption keywords.

The words live in locales/<code>.toml. This module only picks a file, checks that it is complete
and chooses the plural form; it holds no words of its own.
"""
from __future__ import annotations

import re
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

TEXT_KEYS = ("stage1", "start", "no_commands", "unsupported", "too_large", "failed")
_CODE = re.compile(r"^[a-z]{2,3}$")


def _one_other(n: int) -> str:
    return "one" if n == 1 else "other"


def _one_few(rest: str) -> Callable[[int], str]:
    def rule(n: int) -> str:
        if n % 10 == 1 and n % 100 != 11:
            return "one"
        if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
            return "few"
        return rest

    return rule


# CLDR plural categories for whole numbers: rule id -> (categories, rule).
PLURAL_RULES: dict[str, tuple[tuple[str, ...], Callable[[int], str]]] = {
    "one-other": (("one", "other"), _one_other),
    "one-few-many": (("one", "few", "many"), _one_few("many")),
    "one-few-other": (("one", "few", "other"), _one_few("other")),
}


class LocaleError(Exception):
    """A locale is unknown or its file is incomplete."""


def plural_category(rule: str, n: int) -> str:
    return PLURAL_RULES[rule][1](n)


@dataclass(frozen=True)
class Locale:
    code: str
    plural: str
    texts: Mapping[str, str]
    receipts: Mapping[str, Any]
    shared_folder: str
    private_keywords: tuple[str, ...]

    def text(self, key: str) -> str:
        return self.texts[key]

    def receipt(self, kinds: Sequence[str]) -> str:
        n = len(kinds)
        if n == 1:
            return self.receipts["one_voice"] if kinds[0] == "voice" else self.receipts["one_file"]
        if all(kind == "voice" for kind in kinds):
            return self.receipts["voices"].format(n=n)
        return self.receipts["files"][plural_category(self.plural, n)].format(n=n)


def _packaged() -> Traversable:
    return resources.files(__package__).joinpath("locales")


def available_locales() -> list[str]:
    return sorted(item.name.removesuffix(".toml") for item in _packaged().iterdir() if item.name.endswith(".toml"))


def load_locale(code: str, directory: Path | None = None) -> Locale:
    if not isinstance(code, str) or not _CODE.match(code):
        raise LocaleError(f"bad locale code: {code!r}")
    source = (directory if directory is not None else _packaged()).joinpath(f"{code}.toml")
    if not source.is_file():
        raise LocaleError(f"unknown locale: {code}")
    try:
        raw = tomllib.loads(source.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise LocaleError(f"locale {code}: {exc}") from None
    return _checked(code, raw)


def _checked(code: str, raw: dict[str, Any]) -> Locale:
    def fail(problem: str) -> LocaleError:
        return LocaleError(f"locale {code}: {problem}")

    def is_text(value: object, template: bool = False) -> bool:
        return isinstance(value, str) and bool(value.strip()) and (not template or "{n}" in value)

    rule = raw.get("plural")
    if rule not in PLURAL_RULES:
        raise fail(f"plural must be one of {sorted(PLURAL_RULES)}")
    texts = raw.get("texts") or {}
    missing = [key for key in TEXT_KEYS if not is_text(texts.get(key))]
    if missing:
        raise fail(f"missing texts {missing}")
    receipts = raw.get("receipts") or {}
    for key in ("one_file", "one_voice"):
        if not is_text(receipts.get(key)):
            raise fail(f"missing receipts.{key}")
    if not is_text(receipts.get("voices"), template=True):
        raise fail("receipts.voices needs {n}")
    files = receipts.get("files") or {}
    for category in PLURAL_RULES[rule][0]:
        if not is_text(files.get(category), template=True):
            raise fail(f"receipts.files.{category} needs {{n}}")
    spaces = raw.get("spaces") or {}
    if not is_text(spaces.get("shared_folder")):
        raise fail("missing spaces.shared_folder")
    keywords = spaces.get("private_keywords")
    if not isinstance(keywords, list) or not keywords or not all(is_text(k) for k in keywords):
        raise fail("spaces.private_keywords must be a non-empty list of words")
    return Locale(code=code, plural=rule, texts=dict(texts), receipts=dict(receipts),
                  shared_folder=spaces["shared_folder"], private_keywords=tuple(keywords))
