"""The host's text for Telegram (spec 4.3, D29): its HTML is parsed and written again from an allow-list.

Formatting survives. Links do not: an anchor becomes its text followed by its address in a code span, and bare web
addresses go into code spans too, so no web address from the host is ever clickable and a hidden one is always
shown. Stage 2 lets an address stay a link when it came in the person's own message, and closes what this pass
leaves to Telegram's own detection: e-mail addresses, @mentions and bare names without a path (spec 4.3, scenario
29).

Telegram allows no formatting inside or around a code span or a pre block, and no blockquote inside a blockquote.
So the open tags close before a code span or a pre block and open again after it, repeats are dropped, and empty
pairs are removed. The result is always well formed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser

MAX_TEXT_UNITS = 4096  # Telegram's limit, in UTF-16 code units of the text without markup

_KEEP = {
    "b": "b",
    "strong": "b",
    "i": "i",
    "em": "i",
    "u": "u",
    "ins": "u",
    "s": "s",
    "strike": "s",
    "del": "s",
    "tg-spoiler": "tg-spoiler",
    "blockquote": "blockquote",
    "code": "code",
    "pre": "pre",
}
_LITERAL = frozenset({"code", "pre"})  # nothing inside is formatted or a link, so text goes in as it is
_EMPTY = re.compile(r"<([a-z-]+)></\1>")
# Each branch starts only where a token starts and has no nested repetition, so a long run of letters and dashes
# costs linear time: the host's text must never stall Core's event loop.
_ADDRESS = re.compile(
    r"(?i)(?<![\w.-])(?:(?:https?|tg|ftp)://|www\.)[^\s<>]+"
    r"|(?<![\w.-])(?:[\w-]+\.)+(?:[^\W\d_]{2,63}|xn--[a-z0-9-]{1,59})(?::\d{1,5})?/[^\s<>]*"
    r"|(?<![\w.-])\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?(?:/[^\s<>]*)?"
    # What Telegram links by itself (stage 2): e-mail addresses, @mentions and names with a top-level part, in any
    # alphabet, also before the full stop that ends a sentence. Wider than Telegram's own list of domains on purpose:
    # a file name such as scan.pdf only turns monospace.
    r"|(?<![\w.+-])[\w.+-]{1,64}@[\w-]+(?:\.[\w-]+)+"
    r"|(?<![\w@])@[a-z0-9_]{4,32}(?![\w@])"
    r"|(?<![\w.@/-])(?:[\w-]+\.)+(?:[^\W\d_]{2,63}|xn--[a-z0-9-]{1,59})(?::\d{1,5})?(?![\w@/-])(?!\.[\w-])"
)


@dataclass(frozen=True)
class Sanitized:
    html: str  # what Core sends with parse_mode HTML
    plain: str  # the same text without markup: what the person reads

    @property
    def units(self) -> int:
        return len(self.plain.encode("utf-16-le")) // 2


class _Rewriter(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.plain: list[str] = []
        self.stack: list[tuple[str, str]] = []  # open formatting: (source tag, tag written)
        self.size = 0  # characters of plain text so far
        self.coded: list[tuple[int, int]] = []  # the stretches of plain text that are inside code or pre
        self.literal: tuple[str, str] | None = None  # the open code span or pre block
        self.link: list[str] | None = None
        self.href = ""

    def _close_all(self) -> None:
        for _, name in reversed(self.stack):
            self.out.append(f"</{name}>")

    def _open_all(self) -> None:
        for _, name in self.stack:
            self.out.append(f"<{name}>")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.link is not None or self.literal is not None:
            return  # an anchor's text is kept plain, and nothing is formatted inside code
        if tag == "a":
            self.link = []
            self.href = dict(attrs).get("href") or ""
            return
        if tag == "br":
            self._text("\n")
            return
        name = _KEEP.get(tag)
        if tag == "span" and ("class", "tg-spoiler") in attrs:
            name = "tg-spoiler"
        if name is None or any(open_name == name for _, open_name in self.stack):
            return  # unknown, or already open: Telegram refuses a blockquote inside a blockquote
        if name in _LITERAL:
            self._close_all()
            self.literal = (tag, name)
        else:
            self.stack.append((tag, name))
        self.out.append(f"<{name}>")

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            if self.link is not None:
                self._close_link()
            return
        if self.link is not None:
            return
        if self.literal is not None:
            if tag == self.literal[0]:
                self._end_literal()
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                while len(self.stack) > i:
                    self.out.append(f"</{self.stack.pop()[1]}>")
                return

    def _end_literal(self) -> None:
        assert self.literal is not None
        self.out.append(f"</{self.literal[1]}>")
        self.literal = None
        self._open_all()

    def handle_data(self, data: str) -> None:
        if self.link is not None:
            self.link.append(data)
        else:
            self._text(data)

    def _plain(self, text: str, coded: bool) -> None:
        if coded and text:
            self.coded.append((self.size, self.size + len(text)))
        self.plain.append(text)
        self.size += len(text)

    def _text(self, data: str) -> None:
        if self.literal is not None:
            self._plain(data, True)
            self.out.append(escape(data, quote=False))
            return
        start = 0
        for match in _ADDRESS.finditer(data):
            self._plain(data[start : match.start()], False)
            self.out.append(escape(data[start : match.start()], quote=False))
            self._plain(match.group(), True)
            self._code(match.group())
            start = match.end()
        self._plain(data[start:], False)
        self.out.append(escape(data[start:], quote=False))

    def _code(self, address: str) -> None:
        """An address as inactive text: close the open tags, write a code span, open them again."""
        self._close_all()
        self.out.append(f"<code>{escape(address, quote=False)}</code>")
        self._open_all()

    def _close_link(self) -> None:
        assert self.link is not None
        text, href = "".join(self.link).strip(), self.href.strip()
        self.link = None
        if text:
            self._text(text)
        if href and href != text:
            if text:
                self._text(" (")
            self._plain(href, True)
            if self.literal is not None:
                self.out.append(escape(href, quote=False))
            else:
                self._code(href)
            if text:
                self._text(")")

    def finish(self) -> Sanitized:
        self.close()
        if self.link is not None:
            self._close_link()
        if self.literal is not None:
            self._end_literal()
        while self.stack:
            self.out.append(f"</{self.stack.pop()[1]}>")
        html = "".join(self.out)
        while (tidy := _EMPTY.sub("", html)) != html:  # closing and opening around a code span leaves empty pairs
            html = tidy
        return Sanitized(html, "".join(self.plain))

    def uncovered(self, plain: str) -> bool:
        """Whether the text the person sees holds an address outside code. Tags split text into pieces, and an
        address split across pieces (evil.<b>com</b>, which OpenClaw makes of the model's evil.**com**) is no
        address in any one of them, yet Telegram links what the person sees. Code breaks Telegram's detection the
        way a space does, so the text in code is masked with spaces."""
        masked = list(plain)
        for start, end in self.coded:
            masked[start:end] = " " * (end - start)
        return _ADDRESS.search("".join(masked)) is not None


def sanitize_html(source: str) -> Sanitized:
    """Rewrite Telegram HTML from the host: allowed formatting only, no web address active, always well formed."""
    rewriter = _Rewriter()
    # A lone surrogate (a chunker that cut an emoji in half) cannot be sent; it becomes the replacement character.
    rewriter.feed(source.encode("utf-16", "surrogatepass").decode("utf-16", "replace"))
    result = rewriter.finish()
    if rewriter.uncovered(result.plain):
        # An address split by tags: the visible text again, as one piece and without formatting, so that every
        # address in it becomes a code span. The person loses the formatting of this message, never its words.
        flat = _Rewriter()
        flat._text(result.plain)
        return flat.finish()
    return result
