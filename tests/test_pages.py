"""The PDF of a document made from photos (spec 6.1 item 4)."""

import io
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
    assert pages.main(["pdf", str(tmp_path / "out.pdf"), str(big)]) == pages.TOO_LARGE
    assert pages.main(["sheet", str(tmp_path / "out.jpg"), str(big)]) == 2  # no sheets since 2026-10-27
    assert not (tmp_path / "out.pdf").exists()


def test_what_would_pass_its_size_stops_with_its_own_code(tmp_path):
    """The renderer stops a PDF that would pass Telegram's size as it adds its pages, before it holds them all."""
    page = str(photo(tmp_path / "1.jpg"))
    assert pages.main(["pdf", "--max-bytes", "100", str(tmp_path / "out.pdf"), page]) == pages.OUTPUT_TOO_LARGE
    assert pages.main(["pdf", "--max-bytes", "x", str(tmp_path / "out.pdf"), page]) == 2
    assert readers_module.PAGES_TOO_LARGE == pages.OUTPUT_TOO_LARGE  # Core's side names the code without Pillow


def noise(path, size):
    """A photo that compresses badly, as real ones do: grey noise, saved at the highest quality."""
    grey = Image.effect_noise(size, 64)
    Image.merge("RGB", (grey, grey, grey)).save(path, "JPEG", quality=100)
    return path


def test_a_page_encoded_anew_is_no_larger_than_a4_at_300_dpi(tmp_path):
    """The final review's I3: a phone's 24-megapixel photo is no more paper than A4 can show."""
    pages.pdf(tmp_path / "doc.pdf", [photo(tmp_path / "big.png", size=(5000, 4000), fmt="PNG")])
    data = (tmp_path / "doc.pdf").read_bytes()
    assert b"/Width 3508 /Height 2806" in data


def test_originals_that_would_not_fit_are_encoded_anew_smaller_until_they_do(tmp_path, monkeypatch):
    monkeypatch.setattr(pages, "MAX_SIDE", 400)
    monkeypatch.setattr(pages, "FALLBACK", ((300, 85), (200, 80)))
    files = [noise(tmp_path / f"{n}.jpg", (600, 800)) for n in range(2)]
    whole = sum(path.stat().st_size for path in files)
    pages.pdf(tmp_path / "doc.pdf", files, max_bytes=whole // 2)
    data = (tmp_path / "doc.pdf").read_bytes()
    assert files[0].read_bytes() not in data  # no longer as they came
    assert data.count(b"/Width 225 /Height 300") == 2  # every page at the first size that fits
    assert len(data) < whole // 2
    with pytest.raises(pages.PageError):
        pages.pdf(tmp_path / "none.pdf", files, max_bytes=100)
    assert not (tmp_path / "none.pdf").exists()


def test_a_jpeg_goes_in_without_what_says_where_when_and_with_what_it_was_taken(tmp_path):
    """The final review's M3: Telegram strips a photo, never a file; a PDF people forward must not say where home is.
    The image itself still goes in byte for byte."""
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"  # the camera's maker
    exif[0x8825] = {2: (45.0, 15.0, 30.0)}  # GPS: latitude
    path = tmp_path / "file.jpg"
    Image.new("RGB", (600, 800), "#c8d6e5").save(path, "JPEG", exif=exif.tobytes(), comment=b"taken at home")
    original = path.read_bytes()
    pages.pdf(tmp_path / "doc.pdf", [path])
    data = (tmp_path / "doc.pdf").read_bytes()
    assert b"Exif" not in data
    assert b"PhoneMaker" not in data
    assert b"taken at home" not in data
    scan = original[original.index(b"\xff\xda") :]
    assert scan in data  # from the start of the scan to its end, as it came
    assert pages.without_metadata(original) in data
    assert pages.without_metadata(b"not a jpeg") is None


def test_a_transparent_page_is_white_paper_not_black(tmp_path):
    """The final review's M7: what a PNG leaves transparent is paper."""
    image = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
    image.paste((20, 20, 200, 255), (100, 100, 300, 300))  # an opaque blue square
    image.save(tmp_path / "page.png")
    pages.pdf(tmp_path / "doc.pdf", [tmp_path / "page.png"])
    data = (tmp_path / "doc.pdf").read_bytes()
    with Image.open(io.BytesIO(data[data.index(b"\xff\xd8") : data.rindex(b"\xff\xd9") + 2])) as page:
        assert min(page.getpixel((10, 10))) > 240  # white where it was transparent
        assert page.getpixel((200, 200))[2] > 150  # the blue square stays


# ---- pictures for the model (stage 2c-2) ----
def jpeg_size(path):
    with Image.open(path) as image:
        return image.format, image.size


def test_a_view_is_upright_no_longer_than_its_side_and_says_nothing_of_where_it_was_taken(tmp_path):
    exif = Image.Exif()
    exif[0x0112] = 6  # turned: the camera held upright
    exif[0x8825] = {2: (45.0, 15.0, 30.0)}  # GPS
    source = tmp_path / "photo.jpg"
    noise(source, (4000, 3000))
    Image.open(source).save(source, "JPEG", exif=exif.tobytes(), comment=b"taken at home", quality=95)
    out = tmp_path / "view.jpg"
    code = pages.main(["view", "--side", "1568", "--max-bytes", "400000", str(out), str(source)])
    assert code == 0
    data = out.read_bytes()
    assert data[:3] == b"\xff\xd8\xff"
    kind, (width, height) = jpeg_size(out)
    assert kind == "JPEG"
    assert height > width
    assert max(width, height) <= 1568
    assert abs(height / width - 4 / 3) < 0.01
    assert b"Exif" not in data
    assert b"taken at home" not in data
    assert len(data) <= 400_000  # noise, the worst case, fits by a lower quality and then a smaller side


def test_a_small_picture_is_never_enlarged_and_its_transparency_is_white(tmp_path):
    source = tmp_path / "small.png"
    Image.new("RGBA", (600, 400), (0, 0, 0, 0)).save(source)
    out = tmp_path / "view.jpg"
    assert pages.main(["view", "--side", "1568", "--max-bytes", "400000", str(out), str(source)]) == 0
    with Image.open(out) as image:
        assert image.size == (600, 400)
        assert image.getpixel((10, 10)) == (255, 255, 255)


def test_a_view_that_cannot_fit_or_is_no_image_exits_with_its_own_code(tmp_path):
    source = noise(tmp_path / "noise.jpg", (2000, 2000))
    out = tmp_path / "view.jpg"
    assert pages.main(["view", "--side", "1568", "--max-bytes", "500", str(out), str(source)]) == pages.OUTPUT_TOO_LARGE
    (tmp_path / "note.jpg").write_text("not a picture")
    assert pages.main(["view", "--side", "800", "--max-bytes", "9999", str(out), str(tmp_path / "note.jpg")]) == (
        pages.NOT_AN_IMAGE
    )
    assert pages.main(["view", "--side", "x", "--max-bytes", "9999", str(out), str(source)]) == 2


def test_a_thumbnail_is_no_longer_than_its_side(tmp_path):
    source = photo(tmp_path / "page.jpg", size=(3000, 4000))
    out = tmp_path / "thumb.jpg"
    assert pages.main(["thumb", "--side", "600", str(out), str(source)]) == 0
    assert jpeg_size(out) == ("JPEG", (450, 600))


def test_an_overview_shows_up_to_16_pages_numbered_from_the_first_given(tmp_path):
    thumbs = [str(photo(tmp_path / f"{i}.jpg", size=(450, 600), colour=f"#{i * 15:02x}4060")) for i in range(1, 17)]
    out = tmp_path / "overview.jpg"
    assert pages.main(["overview", "--max-bytes", "400000", str(out), "17", *thumbs]) == 0
    assert jpeg_size(out) == ("JPEG", (1176, 1568))
    assert out.stat().st_size <= 400_000
    assert pages.main(["overview", "--max-bytes", "400000", str(out), "1", *thumbs, thumbs[0]]) == pages.TOO_LARGE


def test_strips_show_the_top_and_bottom_of_each_page_legibly(tmp_path):
    """The ordering sheet: a line of 9-point print at the top of an A4 page stays at least 10 pixels high."""
    page = Image.new("RGB", (2480, 3508), "white")  # A4 at 300 dpi
    for y in range(80, 3508, 300):
        page.paste((0, 0, 0), (200, y, 2280, y + 37))  # 9-point lines at 300 dpi are about 37 pixels high
    source = tmp_path / "a4.jpg"
    page.save(source, "JPEG")
    thumb = tmp_path / "thumb.jpg"
    page.resize((1240, 1754)).save(thumb, "JPEG")  # the cached thumbnail a strip is drawn from
    out = tmp_path / "strips.jpg"
    assert pages.main(["strips", "--max-bytes", "400000", str(out), "1", *([str(thumb)] * pages.STRIP_PAGES)]) == 0
    with Image.open(out) as image:
        assert image.width <= 1568
        assert image.height <= 1568
        dark = [y for y in range(image.height) if image.getpixel((image.width // 2, y))[0] < 40]  # print, not rules
        runs, start = [], None
        for y in range(image.height + 1):
            inside = y in dark
            if inside and start is None:
                start = y
            elif not inside and start is not None:
                runs.append(y - start)
                start = None
        assert runs
        assert min(runs) >= 10
