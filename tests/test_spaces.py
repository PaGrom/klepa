import pytest

from klepa_core.spaces import decide_space, is_private_caption

KEYWORDS = ("just for me", "private")


@pytest.mark.parametrize(
    "caption, private",
    [
        ("just for me", True),
        ("Just for me: lab results", True),
        ("Private!", True),
        ("this is private", True),
        ("JUST FOR ME", True),
        ("privately owned", False),
        ("unprivate", False),
        (None, False),
        ("", False),
    ],
)
def test_private_caption(caption, private):
    assert is_private_caption(caption, KEYWORDS) is private


def test_decide_space():
    assert decide_space("just for me", "owner", "shared", KEYWORDS) == "personal:owner"
    assert decide_space("receipt", "owner", "shared", KEYWORDS) == "shared"
    assert decide_space(None, "member", "personal", KEYWORDS) == "personal:member"
