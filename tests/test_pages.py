"""The pages of a document made from photos: the sheet of numbered thumbnails and the PDF (spec 6.1 item 4)."""

import re
import shutil
import subprocess

import pytest
from PIL import Image

from klepa_core.processing import pages
from klepa_core.processing import readers as readers_module


def photo(path, size=(600, 800), colour="#c8d6e5", exif_orientation=None, fmt="JPEG"):
    image = Image.new("RGB", size, colour)
    if exif_orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = exif_orientation
        image.save(path, fmt, exif=exif.tobytes())
    else:
        image.save(path, fmt)
    return path


def page_count(data):
    return len(re.findall(rb"/Type /Page\b", data))


def test_a_sheet_numbers_the_pages_in_a_grid_of_three(tmp_path):
    files = [photo(tmp_path / f"{n}.jpg", colour=c) for n, c in enumerate(("red", "green", "blue", "black"), 1)]
    pages.sheet(tmp_path / "sheet.jpg", files)
    with Image.open(tmp_path / "sheet.jpg") as sheet:
        assert sheet.format == "JPEG"
        cell = pages.THUMB + pages.GAP
        assert sheet.size == (3 * cell + pages.GAP, 2 * cell + pages.GAP)  # four pages: two rows of three
        left = pages.GAP + (pages.THUMB - 270) // 2  # a 600 x 800 photo is a 270 x 360 thumbnail
        centre = (left + pages.BADGE + 6, pages.GAP + pages.BADGE + 6)
        red, _, blue = sheet.getpixel((centre[0] - pages.BADGE + 8, centre[1]))  # in page 1's circle, by the digit
        assert (red < 100, blue > 150) == (True, True)  # the circle's blue


def test_a_long_documents_sheet_grows_in_columns_and_stays_within_what_telegram_takes(tmp_path):
    first = photo(tmp_path / "1.jpg", size=(60, 80))
    files = [first] + [shutil.copyfile(first, tmp_path / f"{n}.jpg") for n in range(2, pages.MAX_PAGES + 1)]
    pages.sheet(tmp_path / "sheet.jpg", files)
    with Image.open(tmp_path / "sheet.jpg") as sheet:
        cell = pages.THUMB + pages.GAP
        assert sheet.size == (10 * cell + pages.GAP, 10 * cell + pages.GAP)  # ten rows of ten
        assert sum(sheet.size) <= 10_000  # a photo's width and height together, for Telegram
    pages.sheet(tmp_path / "ten.jpg", files[:10])
    with Image.open(tmp_path / "ten.jpg") as sheet:
        assert sheet.size == (4 * cell + pages.GAP, 3 * cell + pages.GAP)


def test_a_jpeg_goes_into_the_pdf_byte_for_byte_and_the_pdf_is_the_same_each_time(tmp_path):
    first, second = photo(tmp_path / "1.jpg"), photo(tmp_path / "2.jpg", colour="#222222")
    pages.pdf(tmp_path / "a.pdf", [first, second])
    pages.pdf(tmp_path / "b.pdf", [first, second])
    data = (tmp_path / "a.pdf").read_bytes()
    assert data == (tmp_path / "b.pdf").read_bytes()  # no dates: the same pages, the same file
    assert data.startswith(b"%PDF-1.4")
    assert data.rstrip().endswith(b"%%EOF")
    assert page_count(data) == 2
    assert first.read_bytes() in data
    assert second.read_bytes() in data
    assert data.index(first.read_bytes()) < data.index(second.read_bytes())  # in the order given


def test_a_turned_jpeg_and_a_png_are_encoded_upright_once(tmp_path):
    turned = photo(tmp_path / "turned.jpg", size=(800, 600), exif_orientation=6)  # stored on its side
    png = photo(tmp_path / "page.png", fmt="PNG")
    pages.pdf(tmp_path / "doc.pdf", [turned, png])
    data = (tmp_path / "doc.pdf").read_bytes()
    assert turned.read_bytes() not in data
    assert b"/Width 600 /Height 800" in data  # turned upright
    assert data.count(b"/Filter /DCTDecode") == 2


@pytest.mark.skipif(shutil.which("pdfinfo") is None, reason="needs poppler's pdfinfo")
def test_poppler_reads_the_pdf_whose_pages_fit_a4_upright_or_across(tmp_path):
    files = [photo(tmp_path / f"{n}.jpg") for n in range(3)] + [photo(tmp_path / "wide.jpg", size=(800, 600))]
    pages.pdf(tmp_path / "doc.pdf", files)
    info = subprocess.run(["pdfinfo", "-l", "4", tmp_path / "doc.pdf"], capture_output=True, text=True, check=True)
    assert re.search(r"Pages:\s+4", info.stdout)
    assert re.search(r"Page    1 size:\s+595.28 x 793.71 pts", info.stdout)  # 600 x 800 pixels, as wide as A4
    assert re.search(r"Page    4 size:\s+793.71 x 595.28 pts", info.stdout)  # 800 x 600, as high as A4 across


def test_what_is_not_an_image_or_too_large_exits_with_its_own_code(tmp_path, monkeypatch):
    (tmp_path / "note.jpg").write_text("not a picture")
    assert pages.main(["pdf", str(tmp_path / "out.pdf"), str(tmp_path / "note.jpg")]) == pages.NOT_AN_IMAGE
    monkeypatch.setattr(pages, "MAX_PIXELS", 1000)
    monkeypatch.setattr(pages.Image, "MAX_IMAGE_PIXELS", 1000)
    big = photo(tmp_path / "big.jpg", size=(100, 100))
    assert pages.main(["sheet", str(tmp_path / "out.jpg"), str(big)]) == pages.TOO_LARGE
    assert not (tmp_path / "out.pdf").exists()


def test_what_would_pass_its_size_stops_with_its_own_code(tmp_path):
    """The renderer stops a PDF that would pass Telegram's size as it adds its pages, before it holds them all."""
    page = str(photo(tmp_path / "1.jpg"))
    assert pages.main(["pdf", "--max-bytes", "100", str(tmp_path / "out.pdf"), page]) == pages.OUTPUT_TOO_LARGE
    assert pages.main(["sheet", "--max-bytes", "100", str(tmp_path / "out.jpg"), page]) == pages.OUTPUT_TOO_LARGE
    assert pages.main(["pdf", "--max-bytes", "x", str(tmp_path / "out.pdf"), page]) == 2
    assert readers_module.PAGES_TOO_LARGE == pages.OUTPUT_TOO_LARGE  # Core's side names the code without Pillow
