import json
from pathlib import Path

import pytest

from src.scoring.reason import FLAG_MAX, REASON_MAX, clip_reason, clip_text, is_generic

FIX = json.loads((Path(__file__).parent / "fixtures" / "reasons.json").read_text("utf-8"))


def test_fixture_sizes():
    assert len(FIX["generic"]) >= 15 and len(FIX["concrete"]) >= 15


@pytest.mark.parametrize("r", FIX["generic"])
def test_generic(r):
    assert is_generic(r)


@pytest.mark.parametrize("r", FIX["concrete"])
def test_concrete(r):
    assert not is_generic(r)


def test_acceptance_examples():
    assert is_generic("Good match for your profile")
    assert not is_generic("PyTorch and numerical optimization match; Almaty on-site")


def test_clip_short_is_unchanged_and_one_line():
    assert clip_reason("a  b\n c") == "a b c"


def test_clip_long_word_boundary():
    out = clip_reason(" ".join(["optimization"] * 40))
    assert len(out) <= REASON_MAX and out.endswith("…")
    assert set(out[:-1].split()) == {"optimization"}  # no partial word


def test_clip_exact_limit_and_over():
    assert clip_reason("x" * REASON_MAX) == "x" * REASON_MAX
    assert len(clip_reason(("word " * 30).strip())) <= REASON_MAX


def test_clip_single_long_token_hard_cut():
    out = clip_reason("y" * 300)
    assert len(out) == REASON_MAX and out.endswith("…")


def test_clip_flag():
    assert len(clip_text("z " * 100, FLAG_MAX)) <= FLAG_MAX
