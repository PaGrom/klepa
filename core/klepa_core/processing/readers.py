"""The attachment readers (spec 6.1 item 4): what each kind of original yields as text, page by page.

Each reader gets a copy of the original in its job's folder, never the incoming folder: a reader broken by a planted
file must not reach the other originals there. Programs run through a runner, the sandbox in Core. A reader that fails
is named by its exit code alone, since its output may carry the file's content. When a limit cut what was read (the
first 30 minutes of a recording, 2 MB of text), the pages say so in a note.

Since stage 2c-2 nothing here reads a picture: a photo is drawn once for the model, and a PDF names the pages
pdftotext found no text on; the background reader (host/reader.py) has the model read both (the owner, 2026-11-22:
no OCR anywhere).
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
import time
import wave
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Protocol

from ..config import ProcessingConfig
from .sandbox import Result, open_written

MAX_AUDIO_SECONDS = 30 * 60  # a longer recording is transcribed up to here
MAX_IMAGE_PIXELS = 100_000_000  # a photo or a scanned page larger than this is not unpacked (spec 8)
MAX_TEXT_BYTES = 2_000_000  # what is kept of one original's text
# whisper makes words up for silence ("Thank you.", "you"): a recording whose loudest sound stays under -45 dBFS is
# silent and gets no text (found in the live run of 2b-1, a video note nobody spoke in)
SILENT_PEAK = round(32768 * 10 ** (-45 / 20))
MIN_PAGE_LETTERS = 100  # a PDF page with fewer letters in its text layer may be a scan: the model reads it too
FRAGMENT_CHARS = 4000  # a transcript or a text file is kept in pieces of this size, a page each (spec 5.4)
PAGES_SECONDS = 120  # Core's renderer: a PDF of a hundred pages
PAGES = "pages"  # Core's renderer is no kind of original, yet it needs Pillow, which the processing extra brings
PAGES_TOO_LARGE = 5  # the renderer's exit code when what it draws would pass its size (pages.OUTPUT_TOO_LARGE)
PICTURE_SIDE = 1568  # a page drawn when it arrives (stage 2c-2): the large view, and what sheets are made of
PICTURE_BYTES = 2_000_000  # what that drawing may take on disk
SCAN_PAGES = 100  # scanned pages of one PDF the model reads (stage 2c-2)
_SUFFIX = re.compile(r"\.[a-z0-9]{1,8}")
_SIZE = re.compile(r"(\d{1,6})x(\d{1,6})")
_CELLAR = re.compile(r"/Cellar/[^/]+/([^/]+)/")
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
    extractor: str  # the program, its version and what it was told: "whisper.cpp 1.9.4 <model>", "pdftotext 26.1"
    note: str | None = None  # what a limit left out, in words the model can repeat
    scan_pages: tuple[int, ...] = ()  # read by the model: the PDF pages pdftotext found no text on (stage 2c-2)
    pictures: Mapping[int, Path] = field(default_factory=dict)  # read by the model: a page drawn, in the job's folder


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
    """pdftotext ends every page with a form feed."""
    pages = text.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    return pages


def _letters(text: str) -> int:
    return len("".join(text.split()))


def _note(*notes: str | None) -> str | None:
    return "; ".join(note for note in notes if note) or None


def program_version(program: Path) -> str | None:
    """The version a reader was installed as: Homebrew keeps each in a folder of its own (…/Cellar/<name>/<version>/),
    and a package of Core's own environment has it in its metadata. None when neither says."""
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

    def unavailable(self) -> dict[str, list[str]]:
        """What each kind of original lacks to be read, by name: a program, the speech model; and what Core's renderer
        of pages lacks, which draws photos and pages for the model."""
        p = self.processing
        model = [] if p.whisper_model is not None and p.whisper_model.is_file() else ["whisper_model"]
        pillow = [] if _has_pillow() else ["pillow"]
        pdf, image = _missing(p.pdftotext, p.pdftoppm) + pillow, _missing(p.ffprobe, p.ffmpeg) + pillow
        needs = {
            "voice": _missing(p.ffmpeg, p.whisper) + model,
            "video": _missing(p.ffprobe, p.ffmpeg, p.whisper) + model,
            "pdf": pdf,
            "image": image,
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
        deadline: float | None = None,
    ) -> Result:
        program = Path(str(argv[0]))
        if not os.access(program, os.X_OK):
            raise ReaderMissing(program.name)
        timeout = self.seconds or limit
        if deadline is not None:  # a picture the model waits for: the whole of it within its time
            timeout = min(timeout, deadline - time.monotonic())
            if timeout <= 0:
                raise ReaderFailed(f"{name or program.name} had no time left")
        command = [*_launcher(program), *(str(arg) for arg in argv[1:])]
        result = await self.runner.run(command, job, timeout=timeout, env=env, gpu=gpu)
        if result.returncode != 0:
            raise ReaderFailed(f"{name or program.name} exited with {result.returncode}", result.returncode)
        return result

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
        # The model reads the scanned pages in the background (stage 2c-2): a scan's little text of its own (a page
        # number, a stamp) stays its text until then.
        to_read = tuple(scanned[:SCAN_PAGES])
        more = len(scanned) - len(to_read)
        unread = f"the model reads the first {len(to_read)} of {len(scanned)} scanned pages" if more else None
        return Pages(pages, self.pdftotext, _note(unread, TEXT_CUT if cut else None), scan_pages=to_read)

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
        # Drawn once for the model, which reads it in the background (stage 2c-2); the picture is kept for views.
        drawn = job / "picture-1.jpg"
        await self._draw_image(source, drawn, job, PICTURE_SIDE, PICTURE_BYTES, None)
        return Pages([""], "picture", pictures={1: drawn})

    async def _image_size_check(self, source: Path, folder: Path, deadline: float | None) -> None:
        probe = ["-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height"]
        probe += ["-show_entries", "stream_group_component=width,height", "-of", "csv=p=0:s=x"]
        result = await self._step([self.processing.ffprobe, *probe, source], folder, 30, deadline=deadline)
        pixels = max((int(width) * int(height) for width, height in _SIZE.findall(result.stdout)), default=0)
        if not pixels:
            raise ReaderFailed("the image's size could not be read")
        if pixels > MAX_IMAGE_PIXELS:
            raise ReaderFailed(f"the image is larger than {MAX_IMAGE_PIXELS // 1_000_000} megapixels")

    async def _draw_image(
        self, source: Path, output: Path, folder: Path, side: int, max_bytes: int, deadline: float | None
    ) -> bytes:
        if source.suffix.lower() in (".heic", ".heif"):
            converted = folder / "joined.png"
            await self.to_png(source, converted, folder, max_side=side, deadline=deadline)
            source = converted
        args = ["view", "--side", side, "--max-bytes", max_bytes, output, source]
        return await self._render(args, output, folder, max_bytes, deadline)

    async def _render(
        self, args: Sequence[object], output: Path, folder: Path, max_bytes: int, deadline: float | None
    ) -> bytes:
        argv = [sys.executable, "-I", "-m", "klepa_core.processing.pages", *args]
        try:
            await self._step(argv, folder, PAGES_SECONDS, name="pages", deadline=deadline)
        except ReaderFailed as exc:
            if exc.code == PAGES_TOO_LARGE:
                raise OutputTooLarge(f"{output.name} does not fit {max_bytes} bytes") from None
            raise
        with _output(output) as file:
            data = file.read(max_bytes + 1)
        if len(data) > max_bytes or data[:3] != b"\xff\xd8\xff":
            raise ReaderFailed(f"{output.name} is no picture of its size")
        return data

    async def picture(
        self,
        kind: str,
        original: Path,
        page: int,
        folder: Path,
        *,
        side: int,
        max_bytes: int,
        deadline: float | None = None,
    ) -> bytes:
        """One page of a record drawn for the model (stage 2c-2), in `folder`, which the caller made for this call: a
        photo or an image (page 1), or a page of a PDF. A copy of the original goes into the folder first: the
        sandbox never reads Core's data folder."""
        if kind not in ("image", "pdf"):
            raise ReaderFailed(f"no pages to show for {kind}")
        if kind == "image" and page != 1:
            raise ReaderFailed("a photo has one page")
        suffix = original.suffix.lower()
        copy = folder / ("input" + (suffix if _SUFFIX.fullmatch(suffix) else ""))
        shutil.copyfile(original, copy)
        output = folder / "picture.jpg"
        if kind == "image":
            await self._image_size_check(copy, folder, deadline)
            return await self._draw_image(copy, output, folder, side, max_bytes, deadline)
        drawn = ["-f", page, "-l", page, "-singlefile", "-jpeg", "-scale-to", side, copy, folder / "page"]
        try:
            await self._step([self.processing.pdftoppm, *drawn], folder, 60, deadline=deadline)
        except ReaderFailed as exc:
            raise ReaderFailed(f"no page {page}" if exc.code == 99 else str(exc), exc.code) from None
        return await self._draw_image(folder / "page.jpg", output, folder, side, max_bytes, deadline)

    async def sheet(
        self,
        layout: str,
        pages: Sequence[Path],
        first: int,
        folder: Path,
        *,
        max_bytes: int,
        deadline: float | None = None,
    ) -> bytes:
        """An overview or strips of pictures already drawn (stage 2c-2): only Core's renderer runs, over files in
        `folder`."""
        if layout not in ("overview", "strips"):
            raise ReaderFailed(f"no sheet {layout}")
        output = folder / f"{layout}.jpg"
        return await self._render(
            [layout, "--max-bytes", max_bytes, output, first, *pages], output, folder, max_bytes, deadline
        )

    async def to_png(
        self, source: Path, target: Path, job: Path, *, max_side: int | None = None, deadline: float | None = None
    ) -> None:
        """A HEIC image as a PNG, its tiles joined, by ffmpeg: Pillow cannot open HEIC, and macOS's sips
        does not work in the sandbox, as it reads the home folder and writes in the user's temporary folder. With
        `max_side`, no longer than that and never enlarged: a 24-megapixel photo's PNG is slow to write and large."""
        ffmpeg = self.processing.ffmpeg
        convert: list[object] = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", source, "-frames:v", 1]
        if max_side is None:
            await self._step([*convert, "-y", target], job, 60)
            return
        fit = f"scale='min(iw,{max_side})':'min(ih,{max_side})':force_original_aspect_ratio=decrease"
        grid = [*convert, "-filter_complex", f"[0:g:0]{fit}[page]", "-map", "[page]", "-y", target]
        try:  # an iPhone's photo is a grid of tiles, which ffmpeg joins in a filter graph of its own (ffmpeg 9)
            await self._step(grid, job, 60, deadline=deadline)
        except ReaderFailed:  # one image, with no grid of tiles
            await self._step([*convert, "-vf", fit, "-y", target], job, 60, deadline=deadline)

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
