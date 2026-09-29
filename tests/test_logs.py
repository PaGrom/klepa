import io
import logging
import os
import sys

import klepa_core.__main__ as cli
from klepa_core.logs import configure_logging, redact

# Built at run time: a token-shaped string literal in the source would set off secret scanners.
TOKEN = "1234567890:" + "Q" * 35


def test_redact_hides_bot_tokens_only():
    text = f"GET https://api.telegram.org/bot{TOKEN}/getUpdates failed; update 45070102"
    assert TOKEN not in redact(text)
    assert "<bot-token>" in redact(text)
    assert "45070102" in redact(text)


def test_logging_and_uncaught_exceptions_are_redacted(monkeypatch):
    root = logging.getLogger()
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)  # restored after the test
    monkeypatch.setattr(root, "handlers", list(root.handlers))
    monkeypatch.setattr(root, "level", root.level)
    stream = io.StringIO()
    try:
        configure_logging(stream)
        logging.getLogger("klepa").error("request to %s failed", f"https://api.telegram.org/bot{TOKEN}/x")
        try:
            raise RuntimeError(f"bad url bot{TOKEN}")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())
    finally:
        logging.captureWarnings(False)
    output = stream.getvalue()
    assert TOKEN not in output
    assert output.count("<bot-token>") == 2


def test_run_turns_on_redacted_logging_before_core_starts(monkeypatch, make_config, install):
    make_config()
    order = []
    monkeypatch.setattr(cli, "configure_logging", lambda: order.append("logging"))

    async def core(cfg):
        order.append("core")
        return 0

    monkeypatch.setattr(cli, "_run_with_signals", core)
    previous = os.umask(0o022)
    try:
        assert cli.main(["run", "--config", str(install["tmp"] / "config.toml")]) == 0
    finally:
        os.umask(previous)
    assert order == ["logging", "core"]
