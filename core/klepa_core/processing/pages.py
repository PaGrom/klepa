"""The pages of a document made from photos (spec 6.1 item 4, `make_pdf`): a sheet of numbered thumbnails, on which
the person checks the order, and the PDF itself.

Core runs this module in the sandbox, like its readers:
`python -I -m klepa_core.processing.pages sheet|pdf [--max-bytes N] <output> <page>...`. With `--max-bytes` it stops
once the output would pass that size: a PDF as it adds its pages, before it holds them all.
Every page is an image that came from a person, and Core never decodes one itself. A JPEG goes into the PDF byte for
byte as it came, unless its EXIF orientation asks for a turn; any other image is encoded once as a JPEG. The PDF holds
no dates, so the same pages in the same order always give the same file.
"""

from __future__ import annotations

import io
import math
import sys
from collections.abc import Sequence
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

MAX_PIXELS = 100_000_000  # as the readers: a larger image is not unpacked (spec 8)
MAX_PAGES = 100
THUMB = 360  # the longest side of a thumbnail, in pixels
COLUMNS = 3  # at least; more for a long document, so the sheet stays within Telegram's 10,000 pixels of width and
# height together: a hundred pages make ten rows of ten
GAP = 24
BADGE = 30  # the radius of a page number's circle
A4 = (595.28, 841.89)  # points: a page of the PDF is its image fitted into A4, upright or across as the image is
JPEG_QUALITY = 92  # of a page encoded anew; a sheet is smaller
MAX_SIDE = 3508  # pixels: the longest side of A4 at 300 dpi; a page encoded anew is no larger (the final review's I3)
# When the pages as they came would not fit --max-bytes, every page is encoded anew, smaller at each try.
FALLBACK = ((3508, 85), (2480, 80))
NOT_AN_IMAGE = 3  # exit codes
TOO_LARGE = 4  # an image too large to unpack, or too many pages
OUTPUT_TOO_LARGE = 5  # what it draws would pass --max-bytes
_ORIENTATION = 0x0112  # the EXIF tag
Image.MAX_IMAGE_PIXELS = MAX_PIXELS  # a larger image raises DecompressionBombError before it is decoded


class PageError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _open(path: Path) -> Image.Image:
    try:
        image = Image.open(path)  # the header only: nothing is decoded yet
    except Image.DecompressionBombError:
        raise PageError(TOO_LARGE, f"{path.name} is larger than {MAX_PIXELS // 1_000_000} megapixels") from None
    except (UnidentifiedImageError, OSError):
        raise PageError(NOT_AN_IMAGE, f"{path.name} is not an image") from None
    if image.width * image.height > MAX_PIXELS:
        raise PageError(TOO_LARGE, f"{path.name} is larger than {MAX_PIXELS // 1_000_000} megapixels")
    return image


def _too_large(output: Path, max_bytes: int) -> PageError:
    return PageError(OUTPUT_TOO_LARGE, f"{output.name} is larger than {max_bytes >> 20} MB")


def sheet(output: Path, pages: Sequence[Path], max_bytes: int | None = None) -> None:
    """The pages' thumbnails in a grid, each with its number, as one JPEG."""
    if not 1 <= len(pages) <= MAX_PAGES:
        raise PageError(TOO_LARGE, f"a sheet holds 1 to {MAX_PAGES} pages")
    columns = min(len(pages), max(COLUMNS, math.ceil(math.sqrt(len(pages)))))
    rows = math.ceil(len(pages) / columns)
    cell = THUMB + GAP
    canvas = Image.new("RGB", (columns * cell + GAP, rows * cell + GAP), "white")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=BADGE + 8)
    for index, path in enumerate(pages):
        with _open(path) as image:
            thumb = ImageOps.exif_transpose(image).convert("RGB")
            thumb.thumbnail((THUMB, THUMB))
        left = GAP + (index % columns) * cell + (THUMB - thumb.width) // 2
        top = GAP + (index // columns) * cell + (THUMB - thumb.height) // 2
        canvas.paste(thumb, (left, top))
        draw.rectangle((left - 1, top - 1, left + thumb.width, top + thumb.height), outline="#999999")
        centre = (left + BADGE + 6, top + BADGE + 6)
        draw.ellipse(
            (centre[0] - BADGE, centre[1] - BADGE, centre[0] + BADGE, centre[1] + BADGE),
            fill="#1f6feb",
            outline="white",
            width=3,
        )
        draw.text(centre, str(index + 1), fill="white", font=font, anchor="mm")
    canvas.save(output, "JPEG", quality=85, optimize=True)
    if max_bytes is not None and output.stat().st_size > max_bytes:
        raise _too_large(output, max_bytes)


def _jpeg(path: Path, max_side: int, quality: int, *, as_it_came: bool) -> tuple[bytes, int, int, str]:
    """The page as JPEG bytes, its size and its PDF colour space: the file itself when it may go in as it is,
    otherwise encoded anew, upright, at most `max_side` pixels long."""
    with _open(path) as image:
        orientation = image.getexif().get(_ORIENTATION, 1)
        if as_it_came and image.format == "JPEG" and image.mode in ("RGB", "L") and orientation == 1:
            space = "/DeviceRGB" if image.mode == "RGB" else "/DeviceGray"
            return path.read_bytes(), image.width, image.height, space
        upright = ImageOps.exif_transpose(image).convert("RGB")
    upright.thumbnail((max_side, max_side))  # never larger than it was
    buffer = io.BytesIO()
    upright.save(buffer, "JPEG", quality=quality)
    return buffer.getvalue(), upright.width, upright.height, "/DeviceRGB"


def pdf(output: Path, pages: Sequence[Path], max_bytes: int | None = None) -> None:
    """A PDF of the pages in this order, one image a page, each page its image fitted into A4."""
    if not 1 <= len(pages) <= MAX_PAGES:
        raise PageError(TOO_LARGE, f"a document holds 1 to {MAX_PAGES} pages")
    images = _fitting(pages, max_bytes)
    if images is None:
        assert max_bytes is not None
        raise _too_large(output, max_bytes)
    count = len(images)
    # Objects: 1 the catalog, 2 the page tree, then for each page: the page, its content, its image.
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids ["
        + b" ".join(f"{3 + 3 * i} 0 R".encode() for i in range(count))
        + f"] /Count {count} >>".encode(),
    ]
    for i, (data, width, height, space) in enumerate(images):
        content, image = 4 + 3 * i, 5 + 3 * i  # the page itself is object 3 + 3 * i
        box = A4 if height >= width else (A4[1], A4[0])
        scale = min(box[0] / width, box[1] / height)
        w, h = width * scale, height * scale
        draw = f"q {w:.2f} 0 0 {h:.2f} 0 0 cm /Im0 Do Q".encode()
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w:.2f} {h:.2f}] "
            f"/Resources << /XObject << /Im0 {image} 0 R >> >> /Contents {content} 0 R >>".encode()
        )
        objects.append(f"<< /Length {len(draw)} >>\nstream\n".encode() + draw + b"\nendstream")
        objects.append(
            f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} /ColorSpace {space} "
            f"/BitsPerComponent 8 /Filter /DCTDecode /Length {len(data)} >>\nstream\n".encode()
            + data
            + b"\nendstream"
        )
    body = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(body))
        body += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(body)
    body += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    body += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    body += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    output.write_bytes(bytes(body))


def _fitting(pages: Sequence[Path], max_bytes: int | None) -> list[tuple[bytes, int, int, str]] | None:
    """The pages as they came (a JPEG byte for byte), or, when that would pass `max_bytes`, every page encoded anew at
    the sizes of FALLBACK until they fit; None when none does. Each try stops at the page that passes."""
    tries = [(MAX_SIDE, JPEG_QUALITY, True), *((side, quality, False) for side, quality in FALLBACK)]
    for max_side, quality, as_it_came in tries:
        images = []
        size = 0
        for path in pages:
            images.append(_jpeg(path, max_side, quality, as_it_came=as_it_came))
            size += len(images[-1][0])
            if max_bytes is not None and size > max_bytes:
                break
        else:
            return images
    return None


def main(argv: Sequence[str]) -> int:
    args = list(argv)
    max_bytes = None
    if len(args) >= 3 and args[1] == "--max-bytes":
        if not args[2].isdigit():
            print("usage: pages sheet|pdf [--max-bytes N] <output> <page>...", file=sys.stderr)
            return 2
        max_bytes = int(args[2])
        del args[1:3]
    if len(args) < 3 or args[0] not in ("sheet", "pdf"):
        print("usage: pages sheet|pdf [--max-bytes N] <output> <page>...", file=sys.stderr)
        return 2
    action = sheet if args[0] == "sheet" else pdf
    try:
        action(Path(args[1]), [Path(page) for page in args[2:]], max_bytes)
    except PageError as exc:
        print(str(exc), file=sys.stderr)
        return exc.code
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
