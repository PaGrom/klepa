"""Spreadsheets as text: an xlsx or ods file, sheet by sheet, read by a program of Core's own in the sandbox."""

import zipfile

import pytest

from klepa_core.processing import sheets

WORKBOOK = (
    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
    '<sheet name="Bills" sheetId="1" r:id="rId1"/><sheet name="Notes" sheetId="2" r:id="rId2"/></sheets></workbook>'
)
RELATIONS = (
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
    '<Relationship Id="rId2" Target="/xl/worksheets/sheet2.xml"/></Relationships>'
)
SHARED = (
    '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    "<si><t>Electricity</t></si><si><r><t>Water </t></r><r><t>and gas</t></r></si></sst>"
)
SHEET1 = (
    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
    '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><v>42.5</v></c></row>'
    '<row r="2"><c r="A2" t="s"><v>1</v></c><c r="B2"/><c r="C2" t="inlineStr"><is><t>paid</t></is></c></row>'
    '<row r="3"><c r="A3"/></row>'
    "</sheetData></worksheet>"
)
SHEET2 = (
    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
    '<row r="1"><c r="A1" t="inlineStr"><is><t>Renew the insurance</t></is></c></row></sheetData></worksheet>'
)
CONTENT = (
    '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><office:body><office:spreadsheet>'
    '<table:table table:name="Trips"><table:table-row><table:table-cell><text:p>Museum</text:p></table:table-cell>'
    '<table:table-cell table:number-columns-repeated="16380"/>'
    "<table:table-cell><text:p>May</text:p></table:table-cell></table:table-row>"
    '<table:table-row table:number-rows-repeated="1048570"><table:table-cell/></table:table-row></table:table>'
    "</office:spreadsheet></office:body></office:document-content>"
)


def xlsx(path, **members):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", WORKBOOK)
        archive.writestr("xl/_rels/workbook.xml.rels", RELATIONS)
        archive.writestr("xl/sharedStrings.xml", SHARED)
        archive.writestr("xl/worksheets/sheet1.xml", SHEET1)
        archive.writestr("xl/worksheets/sheet2.xml", SHEET2)
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def read(source, tmp_path):
    target = tmp_path / "out.txt"
    code = sheets.main([str(source), str(target)])
    return code, target.read_text() if target.exists() else None


def test_a_workbook_is_read_sheet_by_sheet(tmp_path):
    code, text = read(xlsx(tmp_path / "bills.xlsx"), tmp_path)
    assert code == 0
    assert text == "Bills\nElectricity\t42.5\nWater and gas\tpaid\n\fNotes\nRenew the insurance\n"


def test_an_open_document_spreadsheet_is_read_without_its_empty_repeats(tmp_path):
    source = tmp_path / "trips.ods"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("content.xml", CONTENT)
    code, text = read(source, tmp_path)
    assert (code, text) == (0, "Trips\nMuseum\tMay\n")


def test_a_member_that_unpacks_past_its_limit_is_not_read(tmp_path, monkeypatch):
    monkeypatch.setattr(sheets, "MAX_MEMBER_BYTES", 1000)
    code, text = read(xlsx(tmp_path / "bomb.xlsx", **{"xl/media/big.bin": b"0" * 5000}), tmp_path)
    assert (code, text) == (sheets.TOO_LARGE, None)


@pytest.mark.parametrize("data", [b"not a zip at all", b"PK\x03\x04broken"])
def test_a_file_that_is_no_spreadsheet_is_refused(tmp_path, data):
    source = tmp_path / "fake.xlsx"
    source.write_bytes(data)
    assert read(source, tmp_path)[0] == sheets.NOT_A_SPREADSHEET


def test_a_zip_without_a_workbook_is_refused(tmp_path):
    source = tmp_path / "other.xlsx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("readme.txt", "hello")
    assert read(source, tmp_path)[0] == sheets.NOT_A_SPREADSHEET


def test_the_text_stops_just_past_what_core_keeps(tmp_path, monkeypatch):
    """Core keeps 2 MB of a text and notes the cut when there was more: one byte more is enough to tell."""
    monkeypatch.setattr(sheets, "MAX_OUTPUT_BYTES", 20)
    code, text = read(xlsx(tmp_path / "bills.xlsx"), tmp_path)
    assert code == 0
    assert len(text.encode()) == 21
