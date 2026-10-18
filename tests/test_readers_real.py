"""The readers with the REAL programs, in the real sandbox: whisper-cli, ffmpeg, ffprobe, pdftotext, ocrmypdf,
tesseract, macOS's textutil, and Core's own reader of spreadsheets.

They run only when KLEPA_TEST_PROCESSORS names the speech model (for example
"~/.cache/whisper/ggml-large-v3-turbo-q8_0.bin") on a Mac with the programs installed (Homebrew's whisper-cpp,
ffmpeg, poppler, tesseract, ghostscript, and the engine's `processing` extra); CI has none of them. The samples are
made here: speech by macOS's `say`, PDFs by `cupsfilter`, scans by `pdftoppm` and Pillow, a scan's own little text by
pikepdf, a Word document by `textutil`, a HEIC photo by `sips`, videos by ffmpeg. English, so the code stays free of
other alphabets; the live run reads the family's own languages.
"""

import asyncio
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from klepa_core.config import DEFAULT_TESSDATA, HOMEBREW_BIN, ProcessingConfig
from klepa_core.processing import readers as readers_module
from klepa_core.processing.readers import Readers
from klepa_core.processing.sandbox import sandbox_available, sandbox_for
from test_sheets import xlsx

MODEL = os.environ.get("KLEPA_TEST_PROCESSORS")
pytestmark = pytest.mark.skipif(
    not MODEL or not sandbox_available(), reason="needs KLEPA_TEST_PROCESSORS: a speech model and the readers"
)
POLICY = (
    "INSURANCE POLICY 4512-778\n"
    "Insured vehicle: Skoda Octavia\n"
    "Valid until 15 November 2027\n"
    "The insurer pays for repairs at any authorised service in the country.\n"
)
LEASE = (
    "APARTMENT LEASE AGREEMENT\n"
    "The tenant pays the rent on the first working day of every month.\n"
    "The landlord repairs the heating within three days of a written notice.\n"
)
DEPOSIT = "SECURITY DEPOSIT 1500 EUR\nReturned within 30 days\nafter the keys are handed back\n"
METER = "WATER METER 0042137\nRead on 3 March 2027\nby the building manager\n"
FFMPEG = [str(HOMEBREW_BIN / "ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]


@pytest.fixture
def readers():
    model = Path(str(MODEL)).expanduser()
    processing = ProcessingConfig(
        whisper=HOMEBREW_BIN / "whisper-cli",
        whisper_model=model,
        ffmpeg=HOMEBREW_BIN / "ffmpeg",
        ffprobe=HOMEBREW_BIN / "ffprobe",
        pdftotext=HOMEBREW_BIN / "pdftotext",
        tesseract=HOMEBREW_BIN / "tesseract",
        ocrmypdf=Path(sys.executable).parent / "ocrmypdf",
        tessdata=DEFAULT_TESSDATA,
        ocr_languages="eng",
    )
    sandbox = sandbox_for(processing, ())  # as Core makes it: the home folder and the user's own folders hidden
    return Readers(processing, sandbox)


def job(tmp_path, name):
    folder = tmp_path / "jobs" / name
    folder.mkdir(parents=True)
    return folder


def pdf_of(text, path):
    source = path.with_suffix(".txt")
    source.write_text(text)
    path.write_bytes(subprocess.run(["cupsfilter", str(source)], capture_output=True, check=True).stdout)
    return path


def scan_of(text, path, label=None):
    """A page of `text` as a picture, the way a scanner makes it (and `path` with ".png", the picture alone). `label`
    is a little text of the scan's own, drawn as text over the picture: a page number, a scanner app's mark."""
    layer = pdf_of(text, path.with_name(path.stem + "-layer.pdf"))
    subprocess.run(["pdftoppm", "-r", "200", "-png", "-singlefile", str(layer), str(path.with_suffix(""))], check=True)
    from PIL import Image

    Image.open(path.with_suffix(".png")).convert("RGB").save(path, resolution=200)
    if label:
        import pikepdf

        with pikepdf.open(path, allow_overwriting_input=True) as scan:
            page = scan.pages[0]
            font = pikepdf.Dictionary(
                Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica
            )
            name = page.add_resource(font, pikepdf.Name.Font, pikepdf.Name.F9)
            page.contents_add(scan.make_stream(f"BT {name} 10 Tf 30 20 Td ({label}) Tj ET".encode()))
            scan.save(path)
    return path


def joined(parts, path):
    import pikepdf

    with pikepdf.new() as document:
        sources = [pikepdf.open(part) for part in parts]
        for source in sources:
            document.pages.extend(source.pages)
        document.save(path)
        for source in sources:
            source.close()
    return path


def test_speech_is_transcribed(readers, tmp_path):
    voice = tmp_path / "voice.aiff"
    subprocess.run(["say", "-v", "Samantha", "-o", str(voice), "Renew the car insurance before November."], check=True)
    started = time.monotonic()
    pages = asyncio.run(readers.read("voice", voice, job(tmp_path, "voice")))
    assert time.monotonic() - started < 15  # with Metal's cache; without it whisper compiles shaders for 20 s a run
    assert "insurance" in pages.texts[0].lower()
    assert "november" in pages.texts[0].lower()


def test_a_pdf_and_its_scan_read_the_same(readers, tmp_path):
    pdf = pdf_of(POLICY, tmp_path / "policy.pdf")
    layer = asyncio.run(readers.read("pdf", pdf, job(tmp_path, "pdf")))
    assert re.fullmatch(r"pdftotext \d[\w.]*", layer.extractor)  # with Homebrew's version
    assert "Skoda Octavia" in layer.texts[0]

    scan = asyncio.run(readers.read("pdf", scan_of(POLICY, tmp_path / "scan.pdf"), job(tmp_path, "scan")))
    assert re.fullmatch(r"ocrmypdf \d[\w.]* eng", scan.extractor)  # with the version of the engine's extra
    assert "Skoda Octavia" in scan.texts[0]

    photo = asyncio.run(readers.read("image", tmp_path / "scan.png", job(tmp_path, "photo")))
    assert "4512-778" in photo.texts[0]


def test_a_mixed_pdf_reads_its_text_page_and_its_scans_even_labelled_ones(readers, tmp_path, monkeypatch):
    """A scan often carries a little text of its own, and ocrmypdf skips any page with text unless forced (finding
    I5): a page number, or a scanner app's mark of more than ten letters."""
    mixed = joined(
        [
            pdf_of(LEASE, tmp_path / "lease.pdf"),
            scan_of(DEPOSIT, tmp_path / "deposit.pdf", label="Page 2"),
            scan_of(METER, tmp_path / "meter.pdf", label="Scanned by CamScanner"),
        ],
        tmp_path / "mixed.pdf",
    )
    pages = asyncio.run(readers.read("pdf", mixed, job(tmp_path, "mixed")))
    assert len(pages.texts) == 3
    assert "first working day of every month" in pages.texts[0]
    assert "SECURITY DEPOSIT" in pages.texts[1]
    assert "handed back" in pages.texts[1]
    assert "building manager" in pages.texts[2]
    assert not any("OCR skipped" in text for text in pages.texts)
    assert re.fullmatch(r"pdftotext \S+, ocrmypdf \S+ eng", pages.extractor)
    assert pages.note is None

    monkeypatch.setattr(readers_module, "MAX_OCR_PAGES", 1)  # the first scan is read; the second keeps its mark
    capped = asyncio.run(readers.read("pdf", mixed, job(tmp_path, "capped")))
    assert "handed back" in capped.texts[1]
    assert "CamScanner" in capped.texts[2]
    assert "building manager" not in capped.texts[2]
    assert capped.note == "OCR read the first 1 of 2 scanned pages"


def test_a_signed_pdf_is_read(readers, tmp_path):
    """ocrmypdf refuses a signed PDF unless told that what it writes may break the signature: it writes no PDF here."""
    import pikepdf

    two = joined(
        [pdf_of(LEASE, tmp_path / "lease.pdf"), scan_of(DEPOSIT, tmp_path / "deposit.pdf", label="Page 2")],
        tmp_path / "two.pdf",
    )
    signed = tmp_path / "signed.pdf"
    with pikepdf.open(two) as document:
        document.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array(), SigFlags=3)
        document.save(signed)
    pages = asyncio.run(readers.read("pdf", signed, job(tmp_path, "signed")))
    assert "first working day of every month" in pages.texts[0]
    assert "handed back" in pages.texts[1]
    assert pages.note is None


def test_a_word_document_is_read(readers, tmp_path):
    notice = tmp_path / "notice.html"
    notice.write_text("<h1>Notice</h1><p>The water will be off on Tuesday from 9 to 12.</p>")
    docx = tmp_path / "notice.docx"
    subprocess.run(["textutil", "-convert", "docx", "-output", str(docx), str(notice)], check=True)
    pages = asyncio.run(readers.read("document", docx, job(tmp_path, "docx")))
    assert "The water will be off on Tuesday from 9 to 12." in pages.texts[0]
    assert pages.extractor == "textutil"


def test_a_spreadsheet_is_read_by_cores_own_reader_in_the_sandbox(readers, tmp_path):
    pages = asyncio.run(readers.read("sheet", xlsx(tmp_path / "bills.xlsx"), job(tmp_path, "sheet")))
    assert pages.texts == ["Bills\nElectricity\t42.5\nWater and gas\tpaid", "Notes\nRenew the insurance"]


def test_a_heic_photo_is_read(readers, tmp_path):
    """sips stores a picture this large as a grid of 512-pixel tiles, as an iPhone does."""
    scan_of(POLICY, tmp_path / "policy-scan.pdf")
    heic = tmp_path / "photo.heic"
    convert = ["sips", "-s", "format", "heic", str(tmp_path / "policy-scan.png"), "--out", str(heic)]
    subprocess.run(convert, check=True, capture_output=True)
    pages = asyncio.run(readers.read("image", heic, job(tmp_path, "heic")))
    assert "4512-778" in pages.texts[0]


def test_a_videos_sound_is_transcribed_and_a_muted_video_has_no_text(readers, tmp_path):
    voice = tmp_path / "voice.aiff"
    words = "Happy birthday, see you at the lake on Sunday."
    subprocess.run(["say", "-v", "Samantha", "-o", str(voice), words], check=True)
    clip = tmp_path / "clip.mp4"
    black = ["-f", "lavfi", "-i", "color=c=black:s=320x240:r=10"]
    sound = ["-i", str(voice), "-shortest", "-c:a", "aac"]
    subprocess.run([*FFMPEG, *black, *sound, "-c:v", "mpeg4", str(clip)], check=True)
    pages = asyncio.run(readers.read("video", clip, job(tmp_path, "clip")))
    assert "birthday" in pages.texts[0].lower()
    assert "sunday" in pages.texts[0].lower()

    muted = tmp_path / "muted.mp4"
    subprocess.run([*FFMPEG, *black, "-t", "2", "-c:v", "mpeg4", str(muted)], check=True)
    silent = asyncio.run(readers.read("video", muted, job(tmp_path, "muted")))
    assert (silent.texts, silent.note) == ([], "the video has no sound")


def test_the_programs_are_where_the_defaults_say(readers):
    for program in ("whisper-cli", "ffmpeg", "ffprobe", "pdftotext", "tesseract"):
        assert shutil.which(program, path=str(HOMEBREW_BIN)), program
