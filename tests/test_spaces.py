import pytest

from klepa_core.config import DEFAULT_PRIVATE_KEYWORDS as KEYWORDS
from klepa_core.spaces import decide_space, is_private_caption


@pytest.mark.parametrize(
    "caption, private",
    [
        ("только для меня", True),
        ("Только для меня: анализы", True),
        ("Лично!", True),
        ("это личное", True),
        ("JUST FOR ME", True),
        ("отлично, вот документы", False),
        ("наличные", False),
        (None, False),
        ("", False),
    ],
)
def test_private_caption(caption, private):
    assert is_private_caption(caption, KEYWORDS) is private


def test_decide_space():
    assert decide_space("только для меня", "owner", "shared", KEYWORDS) == "personal:owner"
    assert decide_space("чек", "owner", "shared", KEYWORDS) == "shared"
    assert decide_space(None, "member", "personal", KEYWORDS) == "personal:member"
