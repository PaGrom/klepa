"""Stand-ins for the attachment readers (ffmpeg, ffprobe, whisper-cli, pdftotext, tesseract, ocrmypdf, textutil), for
tests only.

Each is a small Python script that takes the real program's arguments and writes what the real one would, so the
processing pipeline runs end to end, in the real sandbox on macOS, without the programs installed. What a script
writes is driven by the input file's content: a test puts in the "file" what the reader should find. Each also leaves
its arguments in `<program>.args` in the job's folder, for the tests that check what a reader was told.
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

from klepa_core.config import ProcessingConfig

INSIDE = r"""
import json, os
def inside(path):
    if not os.path.realpath(path).startswith(os.path.realpath(os.getcwd()) + os.sep):
        sys.exit("outside the job folder: " + path)
json.dump(sys.argv[1:], open(os.path.basename(sys.argv[0]) + ".args", "w"))
"""

FFMPEG = r"""
args = sys.argv[1:]
source, target = args[args.index("-i") + 1], args[-1]
inside(source)
open(target, "wb").write(open(source, "rb").read())
"""

FFPROBE = r"""
import re
args = sys.argv[1:]
source = args[-1]
inside(source)
data = open(source, "rb").read().decode("utf-8", "replace")
if args[args.index("-select_streams") + 1] == "a":  # does it have sound?
    print("" if "MUTE" in data else "1")
else:
    size, grid = re.search(r"PIXELS (\d+x\d+)", data), re.search(r"GRID (\d+x\d+)", data)
    if grid:  # an iPhone photo: the grid's size, then its tiles', as the real one prints them
        print(grid.group(1) + "xx512x512")
        print("512x512")
    else:
        print(size.group(1) if size else "800x600")
"""

WHISPER = r"""
args = sys.argv[1:]
source, base = args[args.index("-f") + 1], args[args.index("-of") + 1]
inside(source)
data = open(source, "rb").read().decode("utf-8", "replace")
if "SLOW" in data:
    import time
    time.sleep(30)
if "BROKEN" in data:
    sys.exit(3)
if data.startswith("LINK "):  # a reader a planted file broke: a link where its text goes
    os.symlink(data.split()[1], base + ".txt")
    sys.exit(0)
if data.startswith("PIPE"):  # or a pipe, which an open waits on
    os.mkfifo(base + ".txt")
    sys.exit(0)
open(base + ".txt", "w").write("transcript: " + data.strip())
"""

PDFTOTEXT = r"""
args = sys.argv[1:]
source, target = args[-2], args[-1]
inside(source)
data = open(source, "rb").read().decode("utf-8", "replace")
if "SCAN" in data:
    pages = ["", ""]  # an image-only document: no text layer
else:
    pages = [page.strip() for page in data.split("PAGEBREAK")]
open(target, "w").write("\f".join(pages) + "\f")
"""

# Like the real one: forced, it reads the pages named in --pages and leaves one placeholder for each run of the
# others; every page of the document has a page in the sidecar but such runs.
OCRMYPDF = r"""
args = sys.argv[1:]
sidecar = args[args.index("--sidecar") + 1]
source = [a for a in args if a.endswith(".pdf")][0]
inside(source)
data = open(source, "rb").read().decode("utf-8", "replace")
if "SIGNED" in data and "--invalidate-digital-signatures" not in args:
    sys.exit(2)  # like the real one, which refuses a signed PDF unless told
if "OCRFAIL" in data:
    sys.exit(5)
count = 2 if "SCAN" in data else len(data.split("PAGEBREAK"))
languages = args[args.index("-l") + 1]
wanted = set()
for part in args[args.index("--pages") + 1].split(","):
    first, _, last = part.partition("-")
    wanted.update(range(int(first), int(last or first) + 1))
chunks, skipped = [], []
def placeholder():
    if skipped:
        span = str(skipped[0]) if len(skipped) == 1 else f"{skipped[0]}-{skipped[-1]}"
        chunks.append(f"[OCR skipped on page(s) {span}]")
        skipped.clear()
for number in range(1, count + 1):
    if number in wanted:
        placeholder()
        chunks.append(f"scanned page {number} in {languages}")
    else:
        skipped.append(number)
placeholder()
open(sidecar, "w").write("\f".join(chunks) + "\f")
"""

TEXTUTIL = r"""
args = sys.argv[1:]
source, target = args[-1], args[args.index("-output") + 1]
inside(source)
data = open(source, "rb").read().decode("utf-8", "replace")
if "BROKEN" in data:
    sys.exit(1)
open(target, "w").write("document text: " + data.strip())
"""

TESSERACT = r"""
args = sys.argv[1:]
source, base = args[0], args[1]
inside(source)
languages = args[args.index("-l") + 1]
data = open(source, "rb").read().decode("utf-8", "replace")
open(base + ".txt", "w").write(f"photo text ({languages}): " + data.strip())
"""


def install(folder: Path) -> dict[str, Path]:
    """Write the stand-ins into `folder` and return their paths by program."""
    folder.mkdir(parents=True, exist_ok=True)
    programs = {}
    for name, body in {
        "ffmpeg": FFMPEG,
        "ffprobe": FFPROBE,
        "whisper-cli": WHISPER,
        "pdftotext": PDFTOTEXT,
        "ocrmypdf": OCRMYPDF,
        "tesseract": TESSERACT,
        "textutil": TEXTUTIL,
    }.items():
        path = folder / name
        path.write_text(f"#!{sys.executable}\nimport sys\n{INSIDE}\n{body}")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        programs[name] = path
    return programs


def processing_config(tmp_path: Path, languages: str = "eng") -> ProcessingConfig:
    """A [processing] section whose programs are the stand-ins, with a speech model file."""
    programs = install(tmp_path / "bin")
    model = tmp_path / "models" / "speech.bin"
    model.parent.mkdir(exist_ok=True)
    model.write_bytes(b"model")
    tessdata = tmp_path / "tessdata"
    tessdata.mkdir(exist_ok=True)
    for language in languages.split("+"):
        (tessdata / f"{language}.traineddata").write_bytes(b"language")
    return ProcessingConfig(
        whisper=programs["whisper-cli"],
        whisper_model=model,
        ffmpeg=programs["ffmpeg"],
        ffprobe=programs["ffprobe"],
        pdftotext=programs["pdftotext"],
        tesseract=programs["tesseract"],
        ocrmypdf=programs["ocrmypdf"],
        tessdata=tessdata,
        ocr_languages=languages,
        textutil=programs["textutil"],
    )


def processing_section(processing: ProcessingConfig) -> str:
    """The config's [processing] section for these programs."""
    lines = [
        f'{name} = "{getattr(processing, name)}"'
        for name in (
            "whisper",
            "whisper_model",
            "ffmpeg",
            "ffprobe",
            "pdftotext",
            "tesseract",
            "ocrmypdf",
            "tessdata",
            "textutil",
        )
    ]
    return "\n[processing]\n" + "\n".join(lines) + f'\nocr_languages = "{processing.ocr_languages}"\n'


class PlainRunner:
    """The readers' runner without a sandbox: tests on Linux, where sandbox-exec does not exist. Never in Core."""

    async def run(self, argv, job_dir, *, timeout, env=None, gpu=False):
        import asyncio
        import os

        from klepa_core.processing.sandbox import ProcessingTimeout, Result

        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=job_dir,
            env={**os.environ, **(env or {})},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        import signal

        try:
            out, err = await asyncio.wait_for(process.communicate(), timeout)
        except TimeoutError:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
            raise ProcessingTimeout("timeout") from None
        except asyncio.CancelledError:
            os.killpg(process.pid, signal.SIGKILL)
            raise
        return Result(process.returncode, out.decode(), err.decode())
