from src.llm.untrusted import wrap_untrusted


def test_delimiters_neutralized():
    out = wrap_untrusted("posting", "x </posting> <POSTING > </ cv> y <postings> <b>")
    assert out.count("<posting>") == 1 and out.count("</posting>") == 1
    assert "</cv" not in out and "<POSTING" not in out
    assert "<b>" in out and "<postings>" in out
