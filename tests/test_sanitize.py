import random
import re
import time
from html.parser import HTMLParser

import pytest

from klepa_core.host.sanitize import MAX_TEXT_UNITS, sanitize_html


@pytest.mark.parametrize(
    ("source", "html", "plain"),
    [
        ("plain text", "plain text", "plain text"),
        ("<b>bold</b> and <i>it</i>", "<b>bold</b> and <i>it</i>", "bold and it"),
        ("<strong>x</strong><em>y</em><del>z</del>", "<b>x</b><i>y</i><s>z</s>", "xyz"),
        ('<span class="tg-spoiler">s</span>', "<tg-spoiler>s</tg-spoiler>", "s"),
        ("a &lt; b &amp; c", "a &lt; b &amp; c", "a < b & c"),
        ("line<br>next", "line\nnext", "line\nnext"),
        ("<script>alert(1)</script>", "alert(1)", "alert(1)"),
        ("<b>unclosed", "<b>unclosed</b>", "unclosed"),
        ("</b>stray", "stray", "stray"),
        ("<blockquote><blockquote>q</blockquote></blockquote>", "<blockquote>q</blockquote>", "q"),
        ("<code><b>x</b></code>", "<code>x</code>", "x"),
        ('<pre><code class="language-python">print(1)</code></pre>', "<pre>print(1)</pre>", "print(1)"),
        # Telegram allows no formatting around a code span or a pre block: the tags close around it.
        ("<b>run <code>ls</code> now</b>", "<b>run </b><code>ls</code><b> now</b>", "run ls now"),
        ("<i><pre>x</pre></i>", "<pre>x</pre>", "x"),
        ("<blockquote>q <code>c</code></blockquote>", "<blockquote>q </blockquote><code>c</code>", "q c"),
        ("<b><code>unclosed", "<code>unclosed</code>", "unclosed"),
    ],
)
def test_formatting_is_kept_from_an_allow_list(source, html, plain):
    clean = sanitize_html(source)
    assert (clean.html, clean.plain) == (html, plain)


@pytest.mark.parametrize(
    ("source", "html"),
    [
        # A hidden address is shown, and never clickable (spec 4.3).
        (
            '<a href="https://evil.example/?d=secret">photo</a>',
            "photo (<code>https://evil.example/?d=secret</code>)",
        ),
        ('<a href="https://x.example">https://x.example</a>', "<code>https://x.example</code>"),
        (
            '<a href="https://evil.example">https://good.example</a>',
            "<code>https://good.example</code> (<code>https://evil.example</code>)",
        ),
        ("<a href='javascript:alert(1)'>x</a>", "x (<code>javascript:alert(1)</code>)"),
        ('<a href="tg://user?id=1">Someone</a>', "Someone (<code>tg://user?id=1</code>)"),
        ('<a href="https://x.example"><b>bold</b> link</a>', "bold link (<code>https://x.example</code>)"),
        # Bare addresses are not clickable either.
        ("see https://x.example/a now", "see <code>https://x.example/a</code> now"),
        ("go to www.x.example", "go to <code>www.x.example</code>"),
        ("evil.example/?d=1", "<code>evil.example/?d=1</code>"),
        # A code span may not sit inside other formatting: the tags close around it.
        ("<b>go https://x.example/a!</b>", "<b>go </b><code>https://x.example/a!</code>"),
        ("<i>a https://x.example b</i>", "<i>a </i><code>https://x.example</code><i> b</i>"),
        ("<code>https://x.example</code>", "<code>https://x.example</code>"),
        ("see evil.example:8080/x?d=1", "see <code>evil.example:8080/x?d=1</code>"),
        ("go 10.0.0.1/x", "go <code>10.0.0.1/x</code>"),
    ],
)
def test_no_address_from_the_host_is_ever_a_link(source, html):
    assert sanitize_html(source).html == html


def test_an_unclosed_link_still_shows_its_address():
    assert sanitize_html('<a href="https://x.example">text').html == "text (<code>https://x.example</code>)"


def test_length_counts_utf16_units_of_the_text_without_markup():
    assert sanitize_html("<b>" + "x" * MAX_TEXT_UNITS + "</b>").units == MAX_TEXT_UNITS
    assert sanitize_html("😀").units == 2


def test_long_runs_of_letters_and_dashes_take_linear_time():
    started = time.monotonic()
    sanitize_html("a-" * 30_000)
    sanitize_html("a." * 30_000 + "/")
    assert time.monotonic() - started < 1.0


def test_a_lone_surrogate_becomes_a_replacement_character():
    clean = sanitize_html("half an emoji: \ud83d")
    assert (clean.plain, clean.units) == ("half an emoji: �", 16)


PIECES = [
    "<b>",
    "</b>",
    "<i>",
    "</i>",
    "<u>",
    "</u>",
    "<s>",
    "</s>",
    "<code>",
    "</code>",
    "<pre>",
    "</pre>",
    "<blockquote>",
    "</blockquote>",
    '<span class="tg-spoiler">',
    "</span>",
    '<a href="https://e.example/x">',
    "</a>",
    "<p>",
    "</p>",
    "<br>",
    "text ",
    "https://x.example/p ",
    "www.y.example ",
    "z.example/q ",
    "1.2.3.4/r ",
    "&lt;",
    "&amp;",
    "😀",
    "\n",
]


class TelegramRules(HTMLParser):
    """Checks rewritten HTML against Telegram's rules for nested entities."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []

    def handle_starttag(self, tag, attrs):
        assert tag in {"b", "i", "u", "s", "tg-spoiler", "blockquote", "code", "pre"}
        assert attrs == []
        assert tag not in self.stack  # no repeats, no blockquote in a blockquote
        if tag in ("code", "pre"):
            assert self.stack == []  # nothing around a code span or a pre block
        assert not {"code", "pre"} & set(self.stack)  # nothing inside one either
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack[-1] == tag
        self.stack.pop()

    def handle_data(self, data):
        if any(mark in data for mark in ("://", "www.", ".example/", "1.2.3.4")):
            assert self.stack[-1:] in (["code"], ["pre"])  # every address is inactive


def test_random_html_always_keeps_telegrams_rules():
    rng = random.Random(1)
    for _ in range(3000):
        source = "".join(rng.choice(PIECES) for _ in range(rng.randrange(30)))
        html = sanitize_html(source).html
        checker = TelegramRules()
        checker.feed(html)
        checker.close()
        assert checker.stack == [], source
        assert not re.search(r"<([a-z-]+)></\1>", html), source


@pytest.mark.parametrize(
    ("source", "html"),
    [
        ("write to a.b+c@mail.example.com now", "write to <code>a.b+c@mail.example.com</code> now"),
        ("ask @evil_bot here", "ask <code>@evil_bot</code> here"),
        ("see example.com today", "see <code>example.com</code> today"),
        ("see sub.example.co.uk:8080 now", "see <code>sub.example.co.uk:8080</code> now"),
        ("the file scan.pdf is ready", "the file <code>scan.pdf</code> is ready"),
        ("<b>mail me at x@y.org</b>", "<b>mail me at </b><code>x@y.org</code>"),
        # A sentence ends after a name: Telegram leaves the full stop out of the link it makes.
        ("You can read more at evil.com.", "You can read more at <code>evil.com</code>."),
        ("Open secret-data-123.evil.com. Then", "Open <code>secret-data-123.evil.com</code>. Then"),
        # Names and addresses in other alphabets are linked too.
        (
            "see \u03c0\u03b1\u03c1\u03ac\u03b4\u03b5\u03b9\u03b3\u03bc\u03b1.\u03b5\u03bb now",
            "see <code>\u03c0\u03b1\u03c1\u03ac\u03b4\u03b5\u03b9\u03b3\u03bc\u03b1.\u03b5\u03bb</code> now",
        ),
        (
            "see \u03c0\u03b1\u03c1\u03ac\u03b4\u03b5\u03b9\u03b3\u03bc\u03b1.\u03b5\u03bb/a?b=c now",
            "see <code>\u03c0\u03b1\u03c1\u03ac\u03b4\u03b5\u03b9\u03b3\u03bc\u03b1.\u03b5\u03bb/a?b=c</code> now",
        ),
        (
            "mail x@\u03c0\u03b1\u03c1\u03ac\u03b4\u03b5\u03b9\u03b3\u03bc\u03b1.\u03b5\u03bb now",
            "mail <code>x@\u03c0\u03b1\u03c1\u03ac\u03b4\u03b5\u03b9\u03b3\u03bc\u03b1.\u03b5\u03bb</code> now",
        ),
    ],
)
def test_what_telegram_would_link_by_itself_is_inactive(source, html):
    """Stage 2 (spec 4.3, scenario 29): e-mail addresses, @mentions and names with a top-level part."""
    assert sanitize_html(source).html == html


@pytest.mark.parametrize("source", ["v1.2 and 3.14", "e.g. this", "a @b c", "x@y", "@abc"])
def test_text_that_telegram_does_not_link_stays_as_it_is(source):
    assert sanitize_html(source).html == source


@pytest.mark.parametrize("unit", ["a.", "a-", "a@", "@a", "a.a@", "aa.", "\u03b1.", "\u03b1\u03b1.", "a.a."])
def test_long_runs_of_the_new_patterns_take_linear_time(unit):
    started = time.monotonic()
    sanitize_html(unit * 20_000 + "!")
    assert time.monotonic() - started < 1.0


@pytest.mark.parametrize(
    ("source", "html"),
    [
        # OpenClaw renders the model's evil.**com** as evil.<b>com</b>: the pieces of text between tags are no address
        # on their own, but the person sees one, and Telegram links what the person sees.
        ("see evil.<b>com</b>/steal?d=1", "see <code>evil.com/steal?d=1</code>"),
        ("evil<b>.</b>com/x", "<code>evil.com/x</code>"),
        ("Write to @<b>evil_bot</b>", "Write to <code>@evil_bot</code>"),
        ("bob@evil.<u>com</u>", "<code>bob@evil.com</code>"),
        ("see evil.<i>com</i>/x and <tg-spoiler>more</tg-spoiler>", "see <code>evil.com/x</code> and more"),
        # A tag Core drops leaves its text joined to the text around it.
        (
            'open evil<tg-emoji emoji-id="5368324170671202286">.</tg-emoji>com/steal?d=x',
            "open <code>evil.com/steal?d=x</code>",
        ),
        # Top-level names in punycode.
        ("see evil.xn--p1ai/login", "see <code>evil.xn--p1ai/login</code>"),
        ("shop.xn--80asehdb now", "<code>shop.xn--80asehdb</code> now"),
    ],
)
def test_an_address_split_by_tags_is_still_inactive(source, html):
    assert sanitize_html(source).html == html


def test_formatting_without_addresses_is_kept():
    source = "a <i>normal</i> sentence with <b>bold</b> words and <tg-spoiler>a secret</tg-spoiler>."
    assert sanitize_html(source).html == source
