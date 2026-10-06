import math

from klepa_core.host.instruction import LANGUAGES, MAX_CHARS, TOOLS, instruction
from klepa_core.host.tools import TOOLS as CORE_TOOLS

TOKEN_BUDGET = 5000  # spec 10: the instruction with the tools' descriptions


def tokens(text: str) -> int:
    """An upper bound that holds for any tokenizer of English text worth using: one token per three bytes."""
    return math.ceil(len(text.encode()) / 3)


def test_the_instruction_is_english_and_short_in_every_language_of_the_install():
    for code in LANGUAGES:
        text = instruction(code)
        assert len(text) <= MAX_CHARS
        assert text.isascii()
        assert LANGUAGES[code] in text


def test_the_instruction_names_exactly_cores_tools():
    text = instruction("en")
    assert all(name in text for name in TOOLS)
    assert sorted(TOOLS) == sorted(f"klepa__{tool.name}" for tool in CORE_TOOLS)


def test_the_instruction_and_the_tools_stay_within_the_token_budget():
    import json

    described = json.dumps([tool.spec() for tool in CORE_TOOLS])
    assert tokens(instruction("en")) + tokens(described) <= TOKEN_BUDGET


def test_the_instruction_says_what_klepa_cannot_do_yet():
    """Told nothing, a model promises what it cannot keep: "remind me on Friday", "sure"."""
    text = instruction("en")
    assert "cannot yet" in text
    assert all(word in text for word in ("reminders", "remember", "inside files", "never promise"))


def test_klepa_speaks_of_herself_as_her_own_messages_do():
    assert "feminine" in instruction("en")


def test_the_instruction_says_whom_an_original_reaches_and_what_records_cannot_undergo():
    """Asked to "send my passport to my sister" or to "delete my passport scan", a model told nothing says "done"."""
    text = instruction("en")
    assert "only to the person who writes" in text
    assert all(word in text for word in ("change", "rename", "move", "share", "delete"))
    assert "Klepa removes them" not in text  # links reach the person as plain text, not removed
