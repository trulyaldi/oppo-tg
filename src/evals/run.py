"""Eval runner (T28). The only code that may call the real Claude API, on manual run only.

Usage: .venv/bin/python -m src.evals.run [--threshold 0.8] [--limit N]
Needs ANTHROPIC_API_KEY in the environment. Tests inject a fake client instead.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from src import db
from src.clock import FixedClock
from src.config import AppConfig
from src.logging_setup import configure_logging
from src.models import Kind, Posting
from src.pipeline.filters import hard_filter, relevance_ok
from src.scoring.prompt import prompt_version
from src.scoring.scorer import Scorer

ROOT = Path(__file__).resolve().parents[2]
LABELED = ROOT / "evals" / "labeled.jsonl"
LAST_RUN = ROOT / "evals" / "last_run.json"
# Fixed "today" so deadline/stale behaviour does not drift as the calendar moves.
EVAL_NOW = datetime(2026, 11, 6, 6, 0, tzinfo=UTC)
DEFAULT_THRESHOLD = 0.8
DEFAULT_MODEL = "claude-haiku-4-5"
NO_EXAMPLES: dict[str, list[dict[str, Any]]] = {"like": [], "dislike": []}


@dataclass
class EvalItem:
    posting: Posting
    expected_min: float
    expected_max: float
    note: str


def parse_item(d: Mapping[str, Any]) -> EvalItem:
    lo, hi = float(d["expected_min"]), float(d["expected_max"])
    if not 0 <= lo <= hi <= 10:
        raise ValueError(f"bad expected range [{lo}, {hi}]")
    posting = Posting(
        source=d["source"],
        external_id=d["external_id"],
        title=d["title"],
        company=d["company"],
        url=d["url"],
        location=d.get("location") or "",
        remote=d.get("remote"),
        description=d.get("description") or "",
        posted_at=datetime.fromisoformat(d["posted_at"]) if d.get("posted_at") else None,
        deadline=date.fromisoformat(d["deadline"]) if d.get("deadline") else None,
        kind=Kind(d.get("kind", "other")),
        raw_tags=tuple(d.get("raw_tags") or ()),
    )
    return EvalItem(posting, lo, hi, d.get("note", ""))


def load_items(path: Path = LABELED) -> list[EvalItem]:
    items = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                items.append(parse_item(json.loads(line)))
            except (ValueError, KeyError) as e:
                raise ValueError(f"{path}:{n}: {e}") from e
    return items


def distance_to_range(score: float, lo: float, hi: float) -> float:
    return lo - score if score < lo else score - hi if score > hi else 0.0


async def run_eval(
    items: list[EvalItem],
    client: Any,
    model: str,
    cfg: AppConfig,
    threshold: float = DEFAULT_THRESHOLD,
) -> dict[str, Any]:
    """Hard filters, then the real Scorer. A filtered item counts as score 0."""
    today = EVAL_NOW.date()
    conn = db.connect(":memory:")
    db.migrate(conn)
    scorer = Scorer(client, model, conn, FixedClock(EVAL_NOW), max_calls=max(10, 3 * len(items)))
    results = []
    for it in items:
        p = it.posting
        reason = hard_filter(p, cfg, today) or (None if relevance_ok(p, cfg) else "irrelevant")
        score: float | None = None
        status = "scored"
        out: dict[str, Any] = {}
        if reason:
            score, status = 0.0, f"filtered:{reason}"
        else:
            res = await scorer.score(p, NO_EXAMPLES)
            if res is None:
                status = "score_error"
            else:
                score = res.score
                out = {"reason": res.reason, "red_flags": res.red_flags, "tags": res.tags}
        passed = score is not None and it.expected_min <= score <= it.expected_max
        results.append(
            {
                "id": p.external_id,
                "title": p.title,
                "expected": [it.expected_min, it.expected_max],
                "score": score,
                "status": status,
                "pass": passed,
                "error": None
                if score is None
                else distance_to_range(score, it.expected_min, it.expected_max),
                "note": it.note,
                **out,
            }
        )
    n = len(results)
    n_pass = sum(r["pass"] for r in results)
    errs = [r["error"] if r["error"] is not None else 10.0 for r in results]
    rate = n_pass / n if n else 0.0
    conn.close()
    return {
        "prompt_version": scorer.version,
        "model": model,
        "run_at": datetime.now(UTC).isoformat(),
        "threshold": threshold,
        "total": n,
        "passed": n_pass,
        "pass_rate": rate,
        "mean_abs_error": sum(errs) / n if n else 0.0,
        "ok": rate >= threshold,
        "results": results,
    }


def format_report(summary: Mapping[str, Any]) -> str:
    lines = []
    for r in summary["results"]:
        lo, hi = r["expected"]
        got = "n/a" if r["score"] is None else f"{r['score']:.1f}"
        mark = "PASS" if r["pass"] else "FAIL"
        lines.append(f"{mark} {r['id']}: got {got} ({r['status']}) expected {lo:g}-{hi:g}")
    lines.append(
        f"prompt_version={summary['prompt_version']} model={summary['model']} "
        f"pass {summary['passed']}/{summary['total']} = {summary['pass_rate']:.0%} "
        f"(threshold {summary['threshold']:.0%}) mean_abs_error={summary['mean_abs_error']:.2f}"
    )
    return "\n".join(lines)


def _real_client(api_key: str) -> Any:
    from anthropic import AsyncAnthropic

    return AsyncAnthropic(api_key=api_key)


def main(
    argv: list[str] | None = None,
    environ: Mapping[str, str] | None = None,
    client_factory: Callable[[str], Any] = _real_client,
    items_path: Path = LABELED,
    out_path: Path = LAST_RUN,
) -> int:
    ap = argparse.ArgumentParser(prog="python -m src.evals.run", description=__doc__)
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args(argv)
    env = os.environ if environ is None else environ
    # same SDK logger pin as the bot: ANTHROPIC_LOG=debug must not print request bodies (D-141)
    configure_logging((env.get("LOG_LEVEL") or "INFO").upper())
    key = (env.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        print(
            "ANTHROPIC_API_KEY is not set. The eval runner calls the real Claude API and "
            "must be run manually with the key exported in your shell.",
            file=sys.stderr,
        )
        return 2
    items = load_items(items_path)[: args.limit]
    cfg = AppConfig.load(env.get("OPPO_CONFIG") or ROOT / "config.toml")
    model = (env.get("SCORING_MODEL") or "").strip() or DEFAULT_MODEL
    print(f"prompt_version={prompt_version()} model={model} items={len(items)}")
    summary = asyncio.run(run_eval(items, client_factory(key), model, cfg, args.threshold))
    print(format_report(summary))
    out_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
