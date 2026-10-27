"""The attachment readers (spec 6.1 item 4): what each kind of original yields as text, page by page.

Each reader gets a copy of the original in its job's folder, never the incoming folder: a reader broken by a planted
file must not reach the other originals there. Programs run through a runner, the sandbox in Core. A reader that fails
is named by its exit code alone, since its output may carry the file's content. When a limit cut what was read (the
first 30 minutes of a recording, the first 50 scanned pages, 2 MB of text), the pages say so in a note.
"""

from __future__ import annotations

import array
import contextlib
import importlib.metadata
import importlib.util
import os
import re
import shutil
import sys
import wave
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

from ..config import ProcessingConfig
from .sandbox import ProcessingStopped, Result, open_written

MAX_AUDIO_SECONDS = 30 * 60  # a longer recording is transcribed up to here
MAX_OCR_PAGES = 50  # scanned pages of one document read with OCR
MAX_IMAGE_PIXELS = 100_000_000  # a photo or a scanned page larger than this is not unpacked (spec 8)
MAX_TEXT_BYTES = 2_000_000  # what is kept of one original's text
# whisper makes words up for silence ("Thank you.", "you"): a recording whose loudest sound stays under -45 dBFS is
# silent and gets no text (found in the live run of 2b-1, a video note nobody spoke in)
SILENT_PEAK = round(32768 * 10 ** (-45 / 20))
MIN_PAGE_LETTERS = 100  # a PDF page with fewer letters in its text layer may be a scan: OCR reads it too
FRAGMENT_CHARS = 4000  # a transcript or a text file is kept in pieces of this size, a page each (spec 5.4)
OCR_JOBS = 2  # ocrmypdf's workers: one per core would hold several large pages in memory at once
PAGES_SECONDS = 120  # Core's renderer: a sheet or a PDF of a hundred pages
PAGES = "pages"  # Core's renderer is no kind of original, yet it needs Pillow, which the processing extra brings
PAGES_TOO_LARGE = 5  # the renderer's exit code when what it draws would pass its size (pages.OUTPUT_TOO_LARGE)
_SUFFIX = re.compile(r"\.[a-z0-9]{1,8}")
_SIZE = re.compile(r"(\d{1,6})x(\d{1,6})")
_CELLAR = re.compile(r"/Cellar/[^/]+/([^/]+)/")
_SKIPPED = re.compile(r"\[OCR skipped on page\(s\) (\d+)(?:-(\d+))?\]")
_CYRILLIC = re.compile(r"[\u0400-\u04ff]")
_LETTER = re.compile(r"[^\W\d_]")
# How an installer (uv, pip) begins a Python program when its interpreter's path has a space, as under "Application
# Support": a shell starts the interpreter with the script.
_TRAMPOLINE = re.compile(rb"#!/bin/sh\n'''exec' (['\"])([^'\"\n]+)\1 \"\$0\" \"\$@\"\n")


class ReaderFailed(Exception):
    """A reader could not read the original. The text names the program and its exit code, nothing it printed."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code  # the program's exit code, when it ended with one


class OutputTooLarge(ReaderFailed):
    """What Core's renderer drew is larger than where it goes allows: a photo or a document in Telegram."""


class ReaderMissing(Exception):
    """A program a reader needs is gone (Homebrew upgrades it under the same path): the job is not the original's
    fault and waits for a start that has the program. The text is the program's name."""


class Runner(Protocol):
    async def run(
        self,
        argv: Sequence[str],
        job_dir: Path,
        *,
        timeout: float,
        env: Mapping[str, str] | None = None,
        gpu: bool = False,
    ) -> Result: ...


@dataclass(frozen=True)
class Pages:
    texts: list[str]  # page 1 first; an empty string for a page without text
    extractor: str  # the program, its version and what it was told: "whisper.cpp 1.9.4 <model>", "ocrmypdf … rus+eng"
    note: str | None = None  # what a limit left out, in words the model can repeat


def decode(data: bytes) -> str:
    """A text file's bytes as text: a byte order mark first, then UTF-8, then the Cyrillic code page that Windows and
    Excel still save with (cp1251) when its letters come out Cyrillic, else UTF-8 with the bad bytes replaced."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    with contextlib.suppress(UnicodeDecodeError):
        return data.decode("utf-8")
    legacy = data.decode("cp1251", "replace")
    letters = _LETTER.findall(legacy)
    if letters and len(_CYRILLIC.findall(legacy)) * 2 >= len(letters):
        return legacy
    return data.decode("utf-8", "replace")


@contextlib.contextmanager
def _output(path: Path) -> Iterator[BinaryIO]:
    """What a reader wrote, never through a link and never a pipe (final review, C1)."""
    try:
        file = open_written(path)
    except FileNotFoundError:
        raise ReaderFailed(f"{path.name} was not written") from None
    except OSError:
        raise ReaderFailed(f"{path.name} is not a plain file") from None
    with file:
        yield file


def _read(path: Path) -> tuple[str, bool]:
    """A file's text, at most MAX_TEXT_BYTES of it, and whether there was more."""
    with _output(path) as file:
        data = file.read(MAX_TEXT_BYTES + 1)
    cut = len(data) > MAX_TEXT_BYTES
    return decode(data[:MAX_TEXT_BYTES]).replace("\x00", ""), cut


def _pages(text: str) -> list[str]:
    """pdftotext and ocrmypdf end every page with a form feed."""
    pages = text.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    return pages


def _sidecar(text: str) -> dict[int, str]:
    """ocrmypdf's text by page number. A page it did not read is a placeholder, and it writes one placeholder for a
    run of such pages ("[OCR skipped on page(s) 4-6]"), so the pages are counted by the numbers in it."""
    read, number = {}, 1
    for chunk in _pages(text):
        skipped = _SKIPPED.fullmatch(chunk.strip())
        if skipped:
            number = int(skipped[2] or skipped[1]) + 1
            continue
        read[number] = chunk.strip()
        number += 1
    return read


def _letters(text: str) -> int:
    return len("".join(text.split()))


def _ranges(numbers: Sequence[int]) -> str:
    """1,2,3,5 → "1-3,5", as ocrmypdf's --pages takes them."""
    parts: list[str] = []
    for number in numbers:
        if parts and parts[-1].split("-")[-1] == str(number - 1):
            parts[-1] = parts[-1].split("-")[0] + f"-{number}"
        else:
            parts.append(str(number))
    return ",".join(parts)


def _note(*notes: str | None) -> str | None:
    return "; ".join(note for note in notes if note) or None


def program_version(program: Path) -> str | None:
    """The version a reader was installed as: Homebrew keeps each in a folder of its own (…/Cellar/<name>/<version>/),
    and a package of Core's own environment, such as ocrmypdf, has it in its metadata. None when neither says."""
    match = _CELLAR.search(os.path.realpath(program))
    if match:
        return match[1]
    if program.parent == Path(sys.executable).parent:
        with contextlib.suppress(importlib.metadata.PackageNotFoundError):
            return importlib.metadata.version(program.name)
    return None


def _named(name: str, version: str | None) -> str:
    return name if version is None else f"{name} {version}"


def _missing(*programs: Path) -> list[str]:
    return [program.name for program in programs if not os.access(program, os.X_OK)]


def _has_pillow() -> bool:
    return importlib.util.find_spec("PIL") is not None


def _launcher(program: Path) -> list[str]:
    """How the sandbox starts a program. No shell may run there, so a Python program that begins with a shell
    trampoline is started by its interpreter, with the script (found in the live run of stage 2b-1)."""
    try:
        with program.open("rb") as file:
            match = _TRAMPOLINE.match(file.read(1024))
    except OSError:
        match = None
    return [match[2].decode(), str(program)] if match else [str(program)]


def _peak(file: BinaryIO) -> int | None:
    """The loudest sample of a 16-bit WAV; None when it is not one."""
    try:
        with wave.open(file) as audio:
            if audio.getsampwidth() != 2:
                return None
            loudest = 0
            while frames := audio.readframes(1 << 20):
                samples = array.array("h", frames)
                if sys.byteorder == "big":
                    samples.byteswap()
                loudest = max(loudest, max(samples), -min(samples))
            return loudest
    except (wave.Error, EOFError):
        return None


def fragments(text: str, size: int = FRAGMENT_CHARS) -> list[str]:
    """A text without pages in pieces of at most `size` characters, cut after a paragraph, a line, a sentence or a
    word where one ends in the second half of the piece."""
    pieces = []
    text = text.strip()
    while len(text) > size:
        window = text[:size]
        cut = size
        for mark in ("\n\n", "\n", ". ", "? ", "! ", " "):
            at = window.rfind(mark)
            if at >= size // 2:
                cut = at + len(mark.rstrip())
                break
        pieces.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        pieces.append(text)
    return pieces


TEXT_CUT = f"the text was cut at {MAX_TEXT_BYTES // 1_000_000} MB"


class Readers:
    def __init__(self, processing: ProcessingConfig, runner: Runner, *, seconds: float | None = None) -> None:
        self.processing = processing
        self.runner = runner
        self.seconds = seconds  # tests: one time limit for every step
        # A text names the program, the model and the version that read it (spec 5.4).
        self.whisper = _named("whisper.cpp", program_version(processing.whisper))
        self.ffmpeg = _named("ffmpeg", program_version(processing.ffmpeg))
        self.pdftotext = _named("pdftotext", program_version(processing.pdftotext))
        self.ocrmypdf = _named("ocrmypdf", program_version(processing.ocrmypdf))
        self.tesseract = _named("tesseract", program_version(processing.tesseract))

    def unavailable(self) -> dict[str, list[str]]:
        """What each kind of original lacks to be read, by name: a program, the speech model, a language's data; and
        what Core's renderer of pages lacks."""
        p = self.processing
        languages = [
            f"{language}.traineddata"
            for language in p.ocr_languages.split("+")
            if not (p.tessdata / f"{language}.traineddata").is_file()
        ]
        model = [] if p.whisper_model is not None and p.whisper_model.is_file() else ["whisper_model"]
        needs = {
            "voice": _missing(p.ffmpeg, p.whisper) + model,
            "video": _missing(p.ffprobe, p.ffmpeg, p.whisper) + model,
            "pdf": _missing(p.pdftotext, p.ocrmypdf) + languages,
            "image": _missing(p.ffprobe, p.ffmpeg, p.tesseract) + languages,
            "document": _missing(p.textutil),
            PAGES: [] if _has_pillow() else ["pillow"],
        }
        return {kind: names for kind, names in needs.items() if names}

    async def read(self, kind: str, source: Path, job: Path) -> Pages:
        if kind == "text":  # read by Core itself: decoding bytes runs no parser
            text, cut = _read(source)
            return Pages(fragments(text), "text", TEXT_CUT if cut else None)
        suffix = source.suffix.lower()
        copy = job / ("input" + (suffix if _SUFFIX.fullmatch(suffix) else ""))
        shutil.copyfile(source, copy)
        readers = {
            "voice": self._voice,
            "video": self._video,
            "pdf": self._pdf,
            "image": self._image,
            "document": self._document,
            "sheet": self._sheet,
        }
        if kind not in readers:
            raise ReaderFailed(f"no reader for {kind}")
        return await readers[kind](copy, job)

    async def _step(
        self,
        argv: Sequence[object],
        job: Path,
        limit: float,
        env: Mapping[str, str] | None = None,
        *,
        gpu: bool = False,
        name: str | None = None,
    ) -> Result:
        program = Path(str(argv[0]))
        if not os.access(program, os.X_OK):
            raise ReaderMissing(program.name)
        timeout = self.seconds or limit
        command = [*_launcher(program), *(str(arg) for arg in argv[1:])]
        result = await self.runner.run(command, job, timeout=timeout, env=env, gpu=gpu)
        if result.returncode != 0:
            raise ReaderFailed(f"{name or program.name} exited with {result.returncode}", result.returncode)
        return result

    def _ocr_env(self) -> dict[str, str]:
        return {"TESSDATA_PREFIX": str(self.processing.tessdata)}

    async def _voice(self, source: Path, job: Path) -> Pages:
        model = self.processing.whisper_model
        if model is None:
            raise ReaderFailed("no speech model is configured")
        audio = job / "audio.wav"
        convert = ["-nostdin", "-hide_banner", "-loglevel", "error", "-i", source, "-vn", "-t", MAX_AUDIO_SECONDS]
        await self._step([self.processing.ffmpeg, *convert, "-ar", 16000, "-ac", 1, "-y", audio], job, 300)
        with _output(audio) as file:
            seconds = max(0.0, (os.fstat(file.fileno()).st_size - 44) / 32_000)  # 16 kHz, 16-bit, mono
            peak = _peak(file)
        if peak is not None and peak < SILENT_PEAK:
            return Pages([], self.ffmpeg, "the recording is silent")
        # --suppress-nst: no "you" or "so" made up for silence and noise
        transcribe = [
            "-m",
            model,
            "-l",
            "auto",
            "-nt",
            "--suppress-nst",
            "-otxt",
            "-of",
            job / "transcript",
            "-f",
            audio,
        ]
        await self._step([self.processing.whisper, *transcribe], job, 120 + seconds, gpu=True)
        text, cut = _read(job / "transcript.txt")
        long = seconds >= MAX_AUDIO_SECONDS - 1
        minutes = f"only the first {MAX_AUDIO_SECONDS // 60} minutes were transcribed" if long else None
        return Pages(fragments(text), f"{self.whisper} {model.name}", _note(minutes, TEXT_CUT if cut else None))

    async def _video(self, source: Path, job: Path) -> Pages:
        """A video's sound, read as a voice message's; an animation or a video without sound has no text."""
        probe = ["-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0"]
        if not (await self._step([self.processing.ffprobe, *probe, source], job, 30)).stdout.strip():
            return Pages(
                [], f"ffprobe {program_version(self.processing.ffprobe) or ''}".strip(), "the video has no sound"
            )
        return await self._voice(source, job)

    async def _pdf(self, source: Path, job: Path) -> Pages:
        await self._step([self.processing.pdftotext, "-layout", "-enc", "UTF-8", source, job / "text.txt"], job, 120)
        layer, cut = _read(job / "text.txt")
        pages = [page.strip() for page in _pages(layer)]
        scanned = [n for n, page in enumerate(pages, 1) if _letters(page) < MIN_PAGE_LETTERS]
        if not scanned:
            return Pages(pages, self.pdftotext, TEXT_CUT if cut else None)
        # A scan often carries a little text of its own (a page number, a stamp, "Scanned by …"), and ocrmypdf skips
        # any page with text unless forced: OCR is forced on these pages alone, and a page keeps whichever text has
        # more letters, its layer's or OCR's.
        chosen = scanned[:MAX_OCR_PAGES]
        languages = self.processing.ocr_languages
        # A digitally signed PDF is refused unless ocrmypdf is told that what it writes may break the signature: it
        # writes no PDF here, and the original is never touched (final review, I-A).
        options = [
            "-l", languages, "--force-ocr", "--pages", _ranges(chosen), "--jobs", OCR_JOBS,
            "--max-image-mpixels", MAX_IMAGE_PIXELS // 1_000_000, "--invalidate-digital-signatures",
            "--output-type", "none", "--sidecar", job / "ocr.txt", source, "-",
        ]  # fmt: skip
        limit = 120 + 60 * len(chosen)
        try:
            await self._step([self.processing.ocrmypdf, *options], job, limit, self._ocr_env())
        except (ReaderFailed, ProcessingStopped) as exc:
            if not any(pages):  # nothing but scans: no text to keep
                raise
            # OCR failed, and the pages the text layer gave stay read: one bad page must not take them all.
            failed = f"the scanned pages could not be read: {exc}"
            return Pages(pages, self.pdftotext, _note(failed, TEXT_CUT if cut else None))
        sidecar, ocr_cut = _read(job / "ocr.txt")
        read = _sidecar(sidecar)
        from_ocr = 0
        for number in chosen:
            text = read.get(number, "")
            if number <= len(pages) and _letters(text) > _letters(pages[number - 1]):
                pages[number - 1] = text
                from_ocr += 1
        ocr = f"{self.ocrmypdf} {languages}"
        extractor = ocr if from_ocr == len(pages) else f"{self.pdftotext}, {ocr}" if from_ocr else self.pdftotext
        unread = (
            f"OCR read the first {len(chosen)} of {len(scanned)} scanned pages" if len(scanned) > len(chosen) else None
        )
        return Pages(pages, extractor, _note(unread, TEXT_CUT if cut or ocr_cut else None))

    async def _image(self, source: Path, job: Path) -> Pages:
        # An iPhone's HEIC photo is a grid of 512-pixel tiles: ffprobe names the grid's size before the tiles'.
        probe = ["-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height"]
        probe += ["-show_entries", "stream_group_component=width,height", "-of", "csv=p=0:s=x"]
        sizes = _SIZE.findall((await self._step([self.processing.ffprobe, *probe, source], job, 30)).stdout)
        pixels = max((int(width) * int(height) for width, height in sizes), default=0)
        if not pixels:
            raise ReaderFailed("the image's size could not be read")
        if pixels > MAX_IMAGE_PIXELS:
            raise ReaderFailed(f"the image is larger than {MAX_IMAGE_PIXELS // 1_000_000} megapixels")
        if source.suffix in (".heic", ".heif"):
            converted = job / "converted.png"
            await self.to_png(source, converted, job)
            source = converted
        languages = self.processing.ocr_languages
        await self._step([self.processing.tesseract, source, job / "ocr", "-l", languages], job, 120, self._ocr_env())
        text, cut = _read(job / "ocr.txt")
        return Pages([text.strip()], f"{self.tesseract} {languages}", TEXT_CUT if cut else None)

    async def to_png(self, source: Path, target: Path, job: Path, *, max_side: int | None = None) -> None:
        """A HEIC image as a PNG, its tiles joined, by ffmpeg: tesseract and Pillow cannot open HEIC, and macOS's sips
        does not work in the sandbox, as it reads the home folder and writes in the user's temporary folder. With
        `max_side`, no longer than that and never enlarged: a 24-megapixel photo's PNG is slow to write and large."""
        ffmpeg = self.processing.ffmpeg
        convert: list[object] = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", source, "-frames:v", 1]
        if max_side is None:
            await self._step([*convert, "-y", target], job, 60)
            return
        fit = f"scale='min(iw,{max_side})':'min(ih,{max_side})':force_original_aspect_ratio=decrease"
        try:  # an iPhone's photo is a grid of tiles, which ffmpeg joins in a filter graph of its own (ffmpeg 9)
            await self._step(
                [*convert, "-filter_complex", f"[0:g:0]{fit}[page]", "-map", "[page]", "-y", target], job, 60
            )
        except ReaderFailed:  # one image, with no grid of tiles
            await self._step([*convert, "-vf", fit, "-y", target], job, 60)

    async def pages(self, action: str, inputs: Sequence[Path], job: Path, *, max_bytes: int) -> bytes:
        """Core's own renderer of a document's pages (processing.pages) in the sandbox, like a reader: the PDF
        ("pdf"), at most `max_bytes` of it."""
        output = job / "document.pdf"
        argv = [sys.executable, "-I", "-m", "klepa_core.processing.pages", action, "--max-bytes", max_bytes, output]
        try:
            await self._step([*argv, *inputs], job, PAGES_SECONDS, name="pages")
        except ReaderFailed as exc:
            if exc.code == PAGES_TOO_LARGE:
                raise OutputTooLarge(f"{output.name} is larger than {max_bytes >> 20} MB") from None
            raise
        with _output(output) as file:
            data = file.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise OutputTooLarge(f"{output.name} is larger than {max_bytes >> 20} MB")
        return data

    async def _document(self, source: Path, job: Path) -> Pages:
        """Word, RTF, OpenDocument and HTML documents through macOS's textutil."""
        target = job / "document.txt"
        convert = ["-convert", "txt", "-encoding", "UTF-8", "-output", target, source]
        await self._step([self.processing.textutil, *convert], job, 120)
        text, cut = _read(target)
        return Pages(fragments(text), "textutil", TEXT_CUT if cut else None)

    async def _sheet(self, source: Path, job: Path) -> Pages:
        """xlsx and ods through Core's own reader, run in the sandbox like the others: a sheet is a page."""
        target = job / "sheets.txt"
        await self._step(
            [sys.executable, "-I", "-m", "klepa_core.processing.sheets", source, target], job, 120, name="sheets"
        )
        text, cut = _read(target)
        return Pages([page.strip() for page in _pages(text)], "sheets", TEXT_CUT if cut else None)
