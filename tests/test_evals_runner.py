import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from src.config import AppConfig
from src.evals import run as ev

ROOT = Path(__file__).resolve().parents[1]
CFG = AppConfig.load(ROOT / "tests" / "fixtures" / "config_repo_snapshot.toml")


class FakeClient:
    """Returns a fixed score per call, or by title substring."""

    def __init__(self, default=8.0, by_title=None):
        self.default, self.by_title, self.calls = default, by_title or {}, []
        self.messages = self

    async def create(self, **kw):
        self.calls.append(kw)
        text = kw["messages"][0]["content"]
        score = next((s for k, s in self.by_title.items() if k in text), self.default)
        body = json.dumps(
            {"score": score, "reason": "r", "deadline": None, "red_flags": [], "tags": []}
        )
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=body)])


def item(title, lo, hi, kind="internship", **kw):
    return ev.parse_item(
        {
            "source": "eval",
            "external_id": title,
            "title": title,
            "company": "Co",
            "url": "https://example.com/x",
            "kind": kind,
            "expected_min": lo,
            "expected_max": hi,
            **kw,
        }
    )


def test_labeled_set_validates():
    items = ev.load_items()
    assert len(items) >= 20
    ids = {i.posting.external_id for i in items}
    assert len(ids) == len(items)
    assert all(0 <= i.expected_min <= i.expected_max <= 10 for i in items)
    assert any(i.expected_min >= 7 for i in items) and any(i.expected_max <= 2 for i in items)


def test_pass_fail_counts_and_summary():
    items = [
        item("Quant Intern A", 8, 10),  # score 9 -> pass
        item("Quant Intern B", 8, 10),  # score 5 -> fail, distance 3
        item("ML Intern C", 0, 3),  # score 4 -> fail, distance 1
        item("Unpaid Intern", 0, 2, description="This is an unpaid internship"),  # filtered, 0
        item("Senior Quant Researcher", 7, 9, kind="job"),  # filtered but expected high -> fail
    ]
    client = FakeClient(by_title={"Quant Intern A": 9, "Quant Intern B": 5, "ML Intern C": 4})
    import asyncio

    s = asyncio.run(ev.run_eval(items, client, "m", CFG, threshold=0.5))
    assert [r["pass"] for r in s["results"]] == [True, False, False, True, False]
    assert (s["passed"], s["total"]) == (2, 5)
    assert s["pass_rate"] == 0.4
    assert s["ok"] is False
    assert s["results"][3]["status"] == "filtered:unpaid"
    assert len(client.calls) == 3  # filtered items never reach the API
    assert s["mean_abs_error"] == (0 + 3 + 1 + 0 + 7) / 5
    assert s["prompt_version"]


def test_main_writes_report_and_exit_codes(tmp_path, capsys):
    p = tmp_path / "set.jsonl"
    p.write_text(
        json.dumps(
            {
                "source": "eval",
                "external_id": "a",
                "title": "Quant Intern",
                "company": "Co",
                "url": "https://example.com/a",
                "kind": "internship",
                "expected_min": 7,
                "expected_max": 10,
            }
        )
        + "\n"
    )
    out = tmp_path / "last.json"
    env = {
        "ANTHROPIC_API_KEY": "k",
        "OPPO_CONFIG": str(ROOT / "tests" / "fixtures" / "config_repo_snapshot.toml"),
    }
    rc = ev.main([], env, lambda key: FakeClient(8.0), p, out)
    assert rc == 0
    data = json.loads(out.read_text())
    assert data["passed"] == 1 and data["prompt_version"]
    assert "PASS a" in capsys.readouterr().out
    rc = ev.main(["--threshold", "0.9"], env, lambda key: FakeClient(1.0), p, out)
    assert rc == 1


def test_refuses_without_api_key(tmp_path, capsys):
    def boom(key):
        raise AssertionError("client must not be built")

    out = tmp_path / "last.json"
    assert ev.main([], {}, boom, ev.LABELED, out) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    assert not out.exists()


def test_main_pins_sdk_loggers_even_with_anthropic_log_debug(tmp_path, monkeypatch):
    """FIX-35 (folded in): the manual eval run uses the same logger pin as the bot (D-154)."""
    import logging

    names = ("anthropic", "httpx", "httpcore")
    old = {n: logging.getLogger(n).level for n in names}
    monkeypatch.setenv("ANTHROPIC_LOG", "debug")
    try:
        for n in names:
            logging.getLogger(n).setLevel(logging.DEBUG)
        p = tmp_path / "set.jsonl"
        p.write_text("")
        env = {
            "ANTHROPIC_API_KEY": "k",
            "OPPO_CONFIG": str(ROOT / "tests" / "fixtures" / "config_repo_snapshot.toml"),
        }
        rc = ev.main([], env, lambda key: FakeClient(8.0), p, tmp_path / "last.json")
        assert rc in (0, 1)
        import os

        assert "ANTHROPIC_LOG" not in os.environ
        assert all(logging.getLogger(n).level == logging.WARNING for n in names)
    finally:
        for n, lvl in old.items():
            logging.getLogger(n).setLevel(lvl)


class TriageFake:
    """Triage replies: relevant=false for titles containing a marker, else true."""

    def __init__(self, reject=("Fake",)):
        self.reject, self.calls = reject, []
        self.messages = self

    async def create(self, **kw):
        self.calls.append(kw)
        text = kw["messages"][0]["content"]
        body = json.dumps({"relevant": not any(k in text for k in self.reject), "reason": "r"})
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=body)])


def test_triage_eval_reports_recall_offline():
    items = [
        item("Quant Intern A", 8, 10),  # positive, kept
        item("Quant Intern B", 7, 9),  # positive, kept
        item("ML Intern C", 6, 8),  # positive, kept
        item("Fake Intern D", 0, 2),  # negative, rejected
        item("Unpaid Intern", 7, 9, description="This is an unpaid internship"),  # filtered first
    ]
    client = TriageFake()
    s = asyncio.run(ev.run_triage_eval(items, client, CFG))
    assert s["positives"] == 3 and s["kept"] == 3 and s["recall"] == 1.0 and s["ok"]
    assert s["negatives"] == 1 and s["rejected"] == 1 and len(client.calls) == 4
    assert "recall 3/3" in ev.format_triage_report(s)
    s = asyncio.run(ev.run_triage_eval(items, TriageFake(("Quant Intern A",)), CFG))
    assert s["kept"] == 2 and not s["ok"]


def test_triage_main_flag_writes_separate_report(tmp_path, capsys):
    p = tmp_path / "set.jsonl"
    p.write_text(
        json.dumps(
            {
                "source": "eval",
                "external_id": "a",
                "title": "Quant Intern",
                "company": "Co",
                "url": "https://example.com/a",
                "kind": "internship",
                "expected_min": 7,
                "expected_max": 10,
            }
        )
        + "\n"
    )
    env = {
        "ANTHROPIC_API_KEY": "k",
        "OPPO_CONFIG": str(ROOT / "tests" / "fixtures" / "config_repo_snapshot.toml"),
    }
    out, tout = tmp_path / "last.json", tmp_path / "triage.json"
    rc = ev.main(["--triage"], env, lambda key: TriageFake(), p, out, tout)
    assert rc == 0 and not out.exists()
    assert json.loads(tout.read_text())["recall"] == 1.0
    assert "recall 1/1" in capsys.readouterr().out
    rc = ev.main(["--triage"], env, lambda key: TriageFake(("Quant",)), p, out, tout)
    assert rc == 1
