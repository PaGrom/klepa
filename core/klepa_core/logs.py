"""Logging for Core as a service: short lines to stderr, and anything shaped like a bot token redacted."""

from __future__ import annotations

import logging
import re
import sys
import traceback
from types import TracebackType
from typing import TextIO

TOKEN = re.compile(r"\d{5,}:[A-Za-z0-9_-]{30,}")


def redact(text: str) -> str:
    return TOKEN.sub("<bot-token>", text)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def configure_logging(stream: TextIO | None = None) -> None:
    """Send logging, warnings and uncaught exceptions to `stream` (stderr, which launchd keeps in a file), redacted."""
    target = stream or sys.stderr
    handler = logging.StreamHandler(target)
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    logging.captureWarnings(True)

    def hook(kind: type[BaseException], value: BaseException, tb: TracebackType | None) -> None:
        target.write(redact("".join(traceback.format_exception(kind, value, tb))))

    sys.excepthook = hook
