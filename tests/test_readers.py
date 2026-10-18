"""The attachment readers: what each kind of file yields, page by page, and what a reader that fails or overruns
leaves behind. They run with stand-in programs: without a sandbox everywhere, and in the real sandbox on macOS."""

import asyncio
import dataclasses
import json
import os
import sys
import threading
import zipfile

import pytest

from fakereaders import INSIDE, OCRMYPDF, PlainRunner, processing_config
from klepa_core.processing import readers as readers_module
from klepa_core.processing.readers import (
    FRAGMENT_CHARS,
    ReaderFailed,
    ReaderMissing,
    Readers,
    decode,
    fragments,
    program_version,
)
from klepa_core.processing.sandbox import ProcessingTimeout, Sandbox, sandbox_available, sandbox_for

# A page of a document's text layer: enough letters to be taken for text, not for a scan.
POLICY = (
    "The policy covers the house and the car against fire, theft and water damage until the end of next year, "
    "and the garden too."
)
TERMS = (
    "The terms of the policy: the deductible is one hundred euros, and claims are made within thirty days "
    "of the damage, with photos."
)


@pytest.fixture(params=["plain", "sandbox"])
def setup(request, tmp_path):
    if request.param == "sandbox" and not sandbox_available():
        pytest.skip("sandbox-exec exists on macOS only")
    processing = processing_config(tmp_path, "rus+eng")
    runner = PlainRunner() if request.param == "plain" else Sandbox(hidden=(tmp_path / "home",))
    return processing, runner, tmp_path


def read(setup, kind, content, name="original.bin", **limits):
    processing, runner, tmp_path = setup
    source = tmp_path / "incoming" / name
    source.parent.mkdir(exist_ok=True)
    source.write_bytes(content)
    job = tmp_path / "jobs" / kind
    job.mkdir(parents=True)
    return asyncio.run(Readers(processing, runner, **limits).read(kind, source, job))


def test_a_voice_message_is_transcribed(setup):
    pages = read(setup, "voice", b"hello there", "voice.ogg")
    assert pages.texts == ["transcript: hello there"]
    assert pages.extractor == "whisper.cpp speech.bin"


def test_a_pdf_with_a_text_layer_is_read_page_by_page(setup):
    pages = read(setup, "pdf", f"{POLICY} PAGEBREAK {TERMS}".encode(), "policy.pdf")
    assert pages.texts == [POLICY, TERMS]
    assert (pages.extractor, pages.note) == ("pdftotext", None)


def test_a_page_without_a_text_layer_is_read_with_ocr_and_the_others_kept(setup):
    pages = read(setup, "pdf", f"{POLICY} PAGEBREAK  ".encode(), "mixed.pdf")
    assert pages.texts == [POLICY, "scanned page 2 in rus+eng"]
    assert pages.extractor == "pdftotext, ocrmypdf rus+eng"


def test_a_scan_with_a_little_text_of_its_own_is_read_with_ocr(setup):
    """A page number, a stamp or "Scanned by …" on a scan is a text layer too: OCR is forced on such pages, which
    keep whichever text has more letters."""
    content = f"{POLICY} PAGEBREAK Page 2 PAGEBREAK Scanned by CamScanner PAGEBREAK {TERMS}"
    pages = read(setup, "pdf", content.encode(), "mixed.pdf")
    assert pages.texts == [POLICY, "scanned page 2 in rus+eng", "scanned page 3 in rus+eng", TERMS]
    tmp_path = setup[2]
    args = json.loads((tmp_path / "jobs" / "pdf" / "ocrmypdf.args").read_text())
    assert args[args.index("--pages") + 1] == "2-3"
    assert "--force-ocr" in args
    assert "--skip-text" not in args
    assert args[args.index("--jobs") + 1] == "2"


def test_a_short_page_keeps_its_text_layer_when_ocr_finds_less(setup):
    title = "Insurance contract for the family house, signed in the spring."  # fewer than 100 letters
    pages = read(setup, "pdf", f"{title} PAGEBREAK {POLICY}".encode(), "contract.pdf")
    assert pages.texts == [title, POLICY]
    assert pages.extractor == "pdftotext"


def test_scans_past_the_limit_are_left_and_the_pages_say_so(setup, monkeypatch):
    monkeypatch.setattr(readers_module, "MAX_OCR_PAGES", 2)
    pages = read(setup, "pdf", b" PAGEBREAK " * 3, "scans.pdf")  # four pages, none with text
    assert pages.texts == ["scanned page 1 in rus+eng", "scanned page 2 in rus+eng", "", ""]
    assert pages.note == "OCR read the first 2 of 4 scanned pages"


def test_a_signed_scan_is_read(setup):
    """ocrmypdf refuses a digitally signed PDF unless told that what it writes may break the signature, and it writes
    no PDF here; official documents are often signed (final review, I-A)."""
    pages = read(setup, "pdf", b"SCAN SIGNED", "statement.pdf")
    assert pages.texts == ["scanned page 1 in rus+eng", "scanned page 2 in rus+eng"]


def test_when_ocr_fails_the_text_layer_is_kept_and_the_note_says_so(setup):
    """One page that OCR cannot read must not take the pages already read with it."""
    pages = read(setup, "pdf", f"{POLICY} PAGEBREAK a short page OCRFAIL".encode(), "policy.pdf")
    assert pages.texts == [POLICY, "a short page OCRFAIL"]
    assert pages.extractor == "pdftotext"
    assert pages.note == "the scanned pages could not be read: ocrmypdf exited with 5"


def test_a_scan_whose_ocr_fails_fails(setup):
    """Nothing but scans, and OCR failed: there is no text to keep."""
    with pytest.raises(ReaderFailed, match="ocrmypdf exited with 5"):
        read(setup, "pdf", b"SCAN OCRFAIL", "scan.pdf")


@pytest.mark.skipif(not sandbox_available(), reason="sandbox-exec exists on macOS only")
@pytest.mark.parametrize("quote", ["'", '"'])
def test_a_python_reader_installed_under_a_path_with_a_space_runs_in_the_sandbox(tmp_path, quote):
    """Under a path with a space, as in "Application Support", an installer (uv, pip) begins a Python program with a
    shell that starts its interpreter, and no shell may run in the sandbox: Core starts the interpreter itself, with the
    script (found in the live run of stage 2b-1)."""
    tool = tmp_path / "Application Support" / "bin" / "ocrmypdf"
    tool.parent.mkdir(parents=True)
    trampoline = "#!/bin/sh\n'''exec' " + quote + sys.executable + quote + ' "$0" "$@"\n' + "' '''\n"
    tool.write_text(f"{trampoline}import sys\n{INSIDE}\n{OCRMYPDF}")
    tool.chmod(0o755)
    processing = dataclasses.replace(processing_config(tmp_path), ocrmypdf=tool)
    source = tmp_path / "incoming" / "scan.pdf"
    source.parent.mkdir()
    source.write_bytes(b"SCAN")
    job = tmp_path / "jobs" / "scan"
    job.mkdir(parents=True)
    pages = asyncio.run(Readers(processing, sandbox_for(processing, ())).read("pdf", source, job))
    assert pages.texts == ["scanned page 1 in eng", "scanned page 2 in eng"]


def test_a_scan_is_read_with_ocr_in_the_installations_languages(setup):
    pages = read(setup, "pdf", b"SCAN", "scan.pdf")
    assert pages.texts == ["scanned page 1 in rus+eng", "scanned page 2 in rus+eng"]
    assert pages.extractor == "ocrmypdf rus+eng"


def test_a_photo_is_read_with_ocr(setup):
    pages = read(setup, "image", b"receipt 42", "photo.jpg")
    assert pages.texts == ["photo text (rus+eng): receipt 42"]
    assert pages.extractor == "tesseract rus+eng"


def test_a_text_file_is_read_as_it_is(setup):
    pages = read(setup, "text", "notes\nline two é".encode(), "notes.txt")
    assert pages.texts == ["notes\nline two é"]
    assert (pages.extractor, pages.note) == ("text", None)


WORD = "\u0434\u043e\u0433\u043e\u0432\u043e\u0440 \u0430\u0440\u0435\u043d\u0434\u044b"  # a lease, in Russian


@pytest.mark.parametrize(
    ("data", "text"),
    [
        (WORD.encode("cp1251"), WORD),  # a CSV that Excel saved on a Russian Windows
        ("\ufeffBills;Water".encode(), "Bills;Water"),  # UTF-8 with a byte order mark
        (("Bills;" + WORD).encode("utf-16"), "Bills;" + WORD),  # UTF-16 with its mark
        (b"Bills \xff\xfe2026", "Bills \ufffd\ufffd2026"),  # nothing fits: replaced, never failed
    ],
)
def test_a_text_file_is_decoded_as_it_was_saved(data, text):
    assert decode(data) == text


def test_a_text_past_the_limit_is_kept_in_part_and_says_so(setup, monkeypatch):
    monkeypatch.setattr(readers_module, "MAX_TEXT_BYTES", 100)
    pages = read(setup, "text", b"word " * 100, "long.txt")
    assert len("".join(pages.texts)) <= 100
    assert pages.note == "the text was cut at 2 MB"


def test_a_text_a_reader_left_as_a_link_is_refused_not_followed(setup):
    """A reader broken by a planted file may leave a link where its text goes: Core may read what the reader may not,
    and must not follow it (final review, C1)."""
    secret = setup[2] / "secret.txt"
    secret.write_text("123:NOT-FOR-THE-MODEL")
    with pytest.raises(ReaderFailed, match=r"transcript\.txt is not a plain file"):
        read(setup, "voice", f"LINK {secret}".encode(), "voice.ogg")


def test_a_text_a_reader_left_as_a_pipe_fails_at_once(setup):
    """Or a pipe, which an open would wait on forever, and Core with it."""
    errors: list[str] = []

    def attempt():
        try:
            read(setup, "voice", b"PIPE", "voice.ogg")
        except ReaderFailed as exc:
            errors.append(str(exc))

    thread = threading.Thread(target=attempt, daemon=True)
    thread.start()
    thread.join(10)
    if thread.is_alive():  # free the open that waits, so that the test run ends
        os.close(os.open(setup[2] / "jobs" / "voice" / "transcript.txt", os.O_WRONLY | os.O_NONBLOCK))
        thread.join(5)
    assert errors == ["transcript.txt is not a plain file"]


def test_a_reader_that_fails_is_reported_by_its_exit_code_alone(setup):
    """Its output may carry the file's content: it never reaches the error."""
    with pytest.raises(ReaderFailed) as caught:
        read(setup, "voice", b"BROKEN secret words", "voice.ogg")
    assert str(caught.value) == "whisper-cli exited with 3"


def test_a_reader_that_overruns_is_stopped(setup):
    with pytest.raises(ProcessingTimeout):
        read(setup, "voice", b"SLOW", "voice.ogg", seconds=1.0)


def test_without_a_speech_model_a_voice_message_is_kept_unread(setup):
    processing, runner, tmp_path = setup
    no_model = (dataclasses.replace(processing, whisper_model=None), runner, tmp_path)
    with pytest.raises(ReaderFailed, match="no speech model"):
        read(no_model, "voice", b"hello", "voice.ogg")


def test_the_reader_gets_a_copy_in_its_job_folder(setup):
    """Each stand-in refuses a file outside its job folder: a reader broken by a planted file must not reach the
    other originals in incoming/."""
    _, _, tmp_path = setup
    read(setup, "image", b"text", "photo.jpg")
    assert (tmp_path / "jobs" / "image" / "input.jpg").read_bytes() == b"text"


def test_a_long_transcript_is_kept_in_fragments_cut_between_sentences(setup):
    """A page of text is what get returns at once: a long recording's transcript is kept in fragments (spec 5.4)."""
    sentence = "We need to renew the insurance before the end of the month. "
    pages = read(setup, "voice", (sentence * 200).encode(), "voice.ogg")
    assert len(pages.texts) > 1
    assert all(len(text) <= FRAGMENT_CHARS for text in pages.texts)
    assert all(text.endswith("month.") for text in pages.texts)
    assert " ".join(pages.texts) == ("transcript: " + sentence * 200).strip()


def test_a_long_text_file_is_cut_between_paragraphs(setup):
    paragraph = "A line of the family's notes about the car and the house.\n" * 10
    pages = read(setup, "text", ((paragraph + "\n") * 20).encode(), "notes.txt")
    assert len(pages.texts) > 1
    assert all(len(text) <= FRAGMENT_CHARS and text.endswith("house.") for text in pages.texts)


def test_a_text_without_breaks_is_cut_at_the_size():
    assert [len(piece) for piece in fragments("x" * 9000)] == [FRAGMENT_CHARS, FRAGMENT_CHARS, 1000]
    assert fragments("  ") == []


def test_an_image_past_the_pixel_limit_is_not_read(setup):
    """Its size is read first, from its header (spec 8: pixel limits): a small file can unpack into gigabytes."""
    with pytest.raises(ReaderFailed, match="larger than 100 megapixels"):
        read(setup, "image", b"PIXELS 20000x20000", "huge.png")


def test_an_image_whose_size_cannot_be_read_is_not_read(setup):
    with pytest.raises(ReaderFailed, match="size could not be read"):
        read(setup, "image", b"PIXELS 0x0", "broken.png")


def test_what_each_kind_of_original_lacks_to_be_read_is_named(tmp_path):
    processing = processing_config(tmp_path, "rus+eng")
    assert Readers(processing, PlainRunner()).unavailable() == {}
    (processing.tessdata / "rus.traineddata").unlink()
    broken = dataclasses.replace(processing, whisper=tmp_path / "nowhere" / "whisper-cli", whisper_model=None)
    assert Readers(broken, PlainRunner()).unavailable() == {
        "voice": ["whisper-cli", "whisper_model"],
        "video": ["whisper-cli", "whisper_model"],
        "pdf": ["rus.traineddata"],
        "image": ["rus.traineddata"],
    }
    (processing.tessdata / "rus.traineddata").write_bytes(b"language")
    processing.ffmpeg.unlink()  # it joins a HEIC photo's tiles
    assert set(Readers(processing, PlainRunner()).unavailable()) == {"voice", "video", "image"}


def test_a_programs_version_is_read_from_where_homebrew_put_it(tmp_path):
    """A text names the program, the model and the version that read it (spec 5.4): Homebrew keeps each version in a
    folder of its own, and its link in bin/ points there."""
    cellar = tmp_path / "Cellar" / "tesseract" / "5.5.3" / "bin"
    cellar.mkdir(parents=True)
    (cellar / "tesseract").write_text("")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "tesseract").symlink_to(cellar / "tesseract")
    assert program_version(tmp_path / "bin" / "tesseract") == "5.5.3"
    assert program_version(tmp_path / "elsewhere" / "tesseract") is None


def test_a_word_document_is_read_through_textutil(setup):
    pages = read(setup, "document", b"the lease of the flat", "lease.docx")
    assert pages.texts == ["document text: the lease of the flat"]
    assert pages.extractor == "textutil"


def test_a_spreadsheet_is_read_sheet_by_sheet(setup):
    """Core's own reader of spreadsheets runs as a program, in the sandbox like the others."""
    tmp_path = setup[2]
    source = tmp_path / "bills.xlsx"
    workbook = (
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
        '<sheet name="Bills" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    relations = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>'
    )
    sheet = (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1">'
        '<c r="A1" t="inlineStr"><is><t>Water</t></is></c><c r="B1"><v>31.2</v></c></row></sheetData></worksheet>'
    )
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relations)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    pages = read(setup, "sheet", source.read_bytes(), "bills.xlsx")
    assert pages.texts == ["Bills\nWater\t31.2"]
    assert pages.extractor == "sheets"


def test_a_file_that_is_no_spreadsheet_fails_as_one(setup):
    with pytest.raises(ReaderFailed, match="sheets exited with 3"):
        read(setup, "sheet", b"not a zip", "fake.xlsx")


def test_a_heic_photo_is_turned_into_png_by_ffmpeg_then_read(setup):
    """tesseract cannot open HEIC, and macOS's sips cannot work in the sandbox (it reads the home folder and writes
    in the user's temporary folder): ffmpeg makes the PNG, joining an iPhone photo's tiles."""
    pages = read(setup, "image", b"passport page", "photo.HEIC")
    assert pages.texts == ["photo text (rus+eng): passport page"]
    folder = setup[2] / "jobs" / "image"
    assert json.loads((folder / "ffmpeg.args").read_text())[-1].endswith("converted.png")
    assert json.loads((folder / "tesseract.args").read_text())[0].endswith("converted.png")


def test_a_tiled_photo_is_measured_by_its_whole_grid(setup):
    """ffprobe names an iPhone photo's grid with its 512-pixel tiles: the limit counts the grid."""
    with pytest.raises(ReaderFailed, match="larger than 100 megapixels"):
        read(setup, "image", b"GRID 12000x9000", "photo.heic")


def test_a_videos_sound_is_transcribed(setup):
    pages = read(setup, "video", b"a birthday wish", "clip.mp4")
    assert pages.texts == ["transcript: a birthday wish"]


def test_a_video_without_sound_has_no_text_and_says_so(setup):
    pages = read(setup, "video", b"MUTE animation", "funny.mp4")
    assert (pages.texts, pages.note) == ([], "the video has no sound")


def test_a_recording_past_the_limit_is_transcribed_in_part_and_says_so(setup, monkeypatch):
    monkeypatch.setattr(readers_module, "MAX_AUDIO_SECONDS", 60)
    pages = read(setup, "voice", b"0" * (32_000 * 61 + 44), "long.ogg")  # the stand-in's WAV: 61 s of sound
    assert pages.note == "only the first 1 minutes were transcribed"


@pytest.mark.parametrize("kind", ["voice", "video"])
def test_a_silent_recording_has_no_text_and_says_so(setup, kind):
    """whisper makes words up for silence ("Thank you." for a silent video note, found in the live run of 2b-1): a
    recording whose loudest sound stays under -45 dBFS gets no text, and whisper is not run."""
    pages = read(setup, kind, b"SILENT", "circle.mp4")
    assert (pages.texts, pages.note) == ([], "the recording is silent")
    assert not (setup[2] / "jobs" / kind / "whisper-cli.args").exists()


def test_a_quiet_recording_is_still_transcribed(setup):
    read(setup, "voice", b"AUDIBLE", "voice.ogg")
    assert (setup[2] / "jobs" / "voice" / "whisper-cli.args").exists()


def test_whisper_is_told_not_to_make_up_words_for_silence(setup):
    read(setup, "voice", b"hello", "voice.ogg")
    _, _, tmp_path = setup
    assert "--suppress-nst" in json.loads((tmp_path / "jobs" / "voice" / "whisper-cli.args").read_text())


def test_a_program_gone_since_start_is_named_missing_not_failed(setup):
    """Homebrew upgrades a program under the same path: the job waits for it rather than failing for good."""
    setup[0].tesseract.unlink()
    with pytest.raises(ReaderMissing, match="tesseract"):
        read(setup, "image", b"receipt", "photo.jpg")


def test_a_crafted_suffix_never_reaches_a_readers_arguments(setup):
    """The name came from the sender's device: the copy keeps only a suffix of letters and digits (finding M6)."""
    read(setup, "image", b"receipt", "photo.png -l deu")
    _, _, tmp_path = setup
    args = json.loads((tmp_path / "jobs" / "image" / "tesseract.args").read_text())
    assert args[0].endswith("/input")
    assert "deu" not in args
