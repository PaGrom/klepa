"""Spreadsheets as text (spec 6.1 item 4): an xlsx or ods file, sheet by sheet.

Core runs this module as a reader of its own, in the sandbox like any other:
`python -I -m klepa_core.processing.sheets <input> <output>`. It needs nothing but the standard library.

A sheet becomes a page: its name on the first line, then its rows, the cells of a row joined by tabs; empty rows and
cells are left out, so a sheet's thousands of empty repeats cost nothing. The file is a zip of XML: every member's
unpacked size is checked before anything is read, so a small file cannot unpack into gigabytes, and the XML is read as
a stream. The output stops one byte past what Core keeps of a text, so that Core can say it was cut.
"""

from __future__ import annotations

import sys
import zipfile
from collections.abc import Iterator
from typing import BinaryIO
from xml.etree import ElementTree

MAX_MEMBER_BYTES = 64 << 20
MAX_TOTAL_BYTES = 256 << 20
MAX_OUTPUT_BYTES = 2_000_000  # what Core keeps of one original's text (readers.MAX_TEXT_BYTES)
NOT_A_SPREADSHEET = 3  # exit codes
TOO_LARGE = 4
_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_RELATION = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_TABLE = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
_TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
_SPACES = frozenset({_TEXT + "s", _TEXT + "tab", _TEXT + "line-break"})  # a run of spaces, a tab, a line break

Event = tuple[str, str | list[str]]  # ("sheet", its name) or ("row", its cells)


class _TooLarge(Exception):
    pass


def _check(archive: zipfile.ZipFile) -> None:
    sizes = [member.file_size for member in archive.infolist()]
    if any(size > MAX_MEMBER_BYTES for size in sizes) or sum(sizes) > MAX_TOTAL_BYTES:
        raise _TooLarge


def _texts(element: ElementTree.Element, tag: str) -> str:
    return "".join(text.text or "" for text in element.iter(tag))


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    shared = []
    with archive.open("xl/sharedStrings.xml") as stream:
        for _, element in ElementTree.iterparse(stream):
            if element.tag == _MAIN + "si":
                shared.append(_texts(element, _MAIN + "t"))
                element.clear()
    return shared


def _xlsx_cell(cell: ElementTree.Element, shared: list[str]) -> str:
    kind = cell.get("t")
    if kind == "inlineStr":
        return _texts(cell, _MAIN + "t")
    value = cell.find(_MAIN + "v")
    text = "" if value is None or value.text is None else value.text
    if kind == "s":
        return shared[int(text)] if text.isdigit() and int(text) < len(shared) else ""
    return text


def _xlsx(archive: zipfile.ZipFile) -> Iterator[Event]:
    shared = _shared_strings(archive)
    with archive.open("xl/_rels/workbook.xml.rels") as stream:
        targets = {relation.get("Id"): relation.get("Target") or "" for relation in ElementTree.parse(stream).getroot()}
    with archive.open("xl/workbook.xml") as stream:
        listed = [
            (sheet.get("name") or "", sheet.get(_RELATION)) for sheet in ElementTree.parse(stream).iter(_MAIN + "sheet")
        ]
    members = set(archive.namelist())
    for name, relation in listed:
        target = targets.get(relation, "")
        path = target.lstrip("/") if target.startswith("/") else "xl/" + target
        if path not in members:
            continue
        yield "sheet", name
        with archive.open(path) as stream:
            for _, element in ElementTree.iterparse(stream):
                if element.tag == _MAIN + "row":
                    cells = [text.strip() for cell in element.iter(_MAIN + "c") if (text := _xlsx_cell(cell, shared))]
                    if any(cells):
                        yield "row", [cell for cell in cells if cell]
                    element.clear()


def _ods_cell(cell: ElementTree.Element) -> str:
    """A cell's text: every paragraph and every formatted run in it, with a space wherever the document breaks a
    paragraph or writes a run of spaces, a tab or a line break."""
    parts: list[str] = []

    def walk(node: ElementTree.Element) -> None:
        parts.append(" " if node.tag in _SPACES else node.text or "")
        for child in node:
            walk(child)
            parts.append(child.tail or "")
        if node.tag == _TEXT + "p":
            parts.append(" ")

    walk(cell)
    return " ".join("".join(parts).split())


def _ods(archive: zipfile.ZipFile) -> Iterator[Event]:
    with archive.open("content.xml") as stream:
        for event, element in ElementTree.iterparse(stream, events=("start", "end")):
            if event == "start" and element.tag == _TABLE + "table":
                yield "sheet", element.get(_TABLE + "name") or ""
            elif event == "end" and element.tag == _TABLE + "table-row":
                cells = [_ods_cell(cell) for cell in element if cell.tag == _TABLE + "table-cell"]
                if any(cells):
                    yield "row", [cell for cell in cells if cell]
                element.clear()


def _write(events: Iterator[Event], out: BinaryIO) -> None:
    written = 0
    for kind, value in events:
        line = ("\f" if written else "") + str(value) if kind == "sheet" else "\t".join(value)
        data = (line + "\n").encode("utf-8")
        if written + len(data) > MAX_OUTPUT_BYTES:
            out.write(data[: MAX_OUTPUT_BYTES + 1 - written])
            return
        out.write(data)
        written += len(data)


def main(argv: list[str]) -> int:
    source, target = argv
    try:
        with zipfile.ZipFile(source) as archive:
            _check(archive)
            members = set(archive.namelist())
            if {"xl/workbook.xml", "xl/_rels/workbook.xml.rels"} <= members:
                events = _xlsx(archive)
            elif "content.xml" in members:
                events = _ods(archive)
            else:
                return NOT_A_SPREADSHEET
            with open(target, "wb") as out:
                _write(events, out)
    except _TooLarge:
        return TOO_LARGE
    except (zipfile.BadZipFile, ElementTree.ParseError, KeyError, ValueError, EOFError):
        return NOT_A_SPREADSHEET
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
