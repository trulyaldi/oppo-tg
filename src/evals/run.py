"""Eval runner (T28). The only code that may call the real Claude API, on manual run only.

Usage: .venv/bin/python -m src.evals.run [--threshold 0.8] [--limit N] [--triage]
Needs ANTHROPIC_API_KEY in the environment. Tests inject a fake client instead.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from src import db
from src.clock import FixedClock
from src.config import AppConfig, TriageConfig
from src.logging_setup import configure_logging
from src.models import Kind, OpportunityType, Posting
from src.pipeline.filters import hard_filter, relevance_ok
from src.scoring.prompt import prompt_version
from src.scoring.scorer import Scorer
from src.scoring.triage import Triager, triage_prompt_version

ROOT = Path(__file__).resolve().parents[2]
LABELED = ROOT / "evals" / "labeled.jsonl"
LAST_RUN = ROOT / "evals" / "last_run.json"
LAST_TRIAGE_RUN = ROOT / "evals" / "last_triage_run.json"
TRIAGE_THRESHOLD = 0.9  # recall: triage may drop noise, but must not drop good items
POSITIVE_MIN = 5.0  # items labeled at least this good must survive triage (digest_min_score)
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
    expected_type: str | None = None


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
    et = d.get("expected_type")
    if et is not None:
        et = OpportunityType(et).value  # ValueError on a typo
    return EvalItem(posting, lo, hi, d.get("note", ""), et)


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


async def run_triage_eval(
    items: list[EvalItem],
    client: Any,
    cfg: AppConfig,
    threshold: float = TRIAGE_THRESHOLD,
    model: str | None = None,
) -> dict[str, Any]:
    """Triage recall on the labeled set: how many items labeled >= 5 get relevant=true.

    Hard filters run first, as in the pipeline; filtered items never reach triage and are
    skipped. Negatives (labeled max < 5) report how many triage rejected, for information.
    """
    today = EVAL_NOW.date()
    conn = db.connect(":memory:")
    db.migrate(conn)
    tcfg = TriageConfig.from_cfg(cfg)
    triager = Triager(
        client, conn, FixedClock(EVAL_NOW), replace(tcfg, daily_call_cap=10**6), model
    )
    rows = []
    for it in items:
        p = it.posting
        if hard_filter(p, cfg, today) or not relevance_ok(p, cfg):
            continue
        verdict, error = await triager.triage_posting(p)
        positive = it.expected_min >= POSITIVE_MIN
        rows.append(
            {
                "id": p.external_id,
                "title": p.title,
                "positive": positive,
                "relevant": None if verdict is None else verdict.relevant,
                "expected_type": it.expected_type,
                "type": None if verdict is None or verdict.type is None else verdict.type.value,
                "error": error,
                "reason": None if verdict is None else verdict.reason,
            }
        )
    pos = [r for r in rows if r["positive"]]
    neg = [r for r in rows if not r["positive"]]
    kept = sum(r["relevant"] is True for r in pos)
    recall = kept / len(pos) if pos else 1.0
    typed = [r for r in rows if r["expected_type"] is not None and r["error"] is None]
    type_ok = sum(r["type"] == r["expected_type"] for r in typed)
    conn.close()
    return {
        "prompt_version": triage_prompt_version(),
        "model": triager.model,
        "run_at": datetime.now(UTC).isoformat(),
        "threshold": threshold,
        "positives": len(pos),
        "kept": kept,
        "recall": recall,
        "typed": len(typed),
        "type_correct": type_ok,
        "type_accuracy": type_ok / len(typed) if typed else None,
        "negatives": len(neg),
        "rejected": sum(r["relevant"] is False for r in neg),
        "errors": sum(r["error"] is not None for r in rows),
        "ok": recall >= threshold,
        "results": rows,
    }


def format_triage_report(summary: Mapping[str, Any]) -> str:
    lines = []
    ta = summary.get("type_accuracy")
    acc = "n/a" if ta is None else f"{summary['type_correct']}/{summary['typed']} = {ta:.0%}"
    for r in summary["results"]:
        got = "error" if r["relevant"] is None else ("yes" if r["relevant"] else "no")
        want = "yes" if r["positive"] else "any"
        bad = r["positive"] and r["relevant"] is not True
        typ = ""
        if r.get("expected_type"):
            typ = f", type {r.get('type') or 'none'} (wanted {r['expected_type']})"
        lines.append(f"{'FAIL' if bad else 'ok'} {r['id']}: triage {got} (wanted {want}){typ}")
    lines.append(
        f"prompt_version={summary['prompt_version']} model={summary['model']} "
        f"recall {summary['kept']}/{summary['positives']} = {summary['recall']:.0%} "
        f"(threshold {summary['threshold']:.0%}) type_accuracy {acc} "
        f"rejected {summary['rejected']}/{summary['negatives']} negatives, "
        f"{summary['errors']} errors"
    )
    return "\n".join(lines)


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
    triage_out_path: Path = LAST_TRIAGE_RUN,
) -> int:
    ap = argparse.ArgumentParser(prog="python -m src.evals.run", description=__doc__)
    ap.add_argument("--threshold", type=float, default=None)
    ap.add_argument(
        "--triage",
        action="store_true",
        help="measure triage recall (uses [triage] model) instead of scoring accuracy",
    )
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
    if args.triage:
        threshold = TRIAGE_THRESHOLD if args.threshold is None else args.threshold
        tmodel = TriageConfig.from_cfg(cfg).model
        print(f"triage_prompt_version={triage_prompt_version()} model={tmodel} items={len(items)}")
        report = asyncio.run(run_triage_eval(items, client_factory(key), cfg, threshold))
        print(format_triage_report(report))
        triage_out_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return 0 if report["ok"] else 1
    threshold = DEFAULT_THRESHOLD if args.threshold is None else args.threshold
    model = (env.get("SCORING_MODEL") or "").strip() or DEFAULT_MODEL
    print(f"prompt_version={prompt_version()} model={model} items={len(items)}")
    summary = asyncio.run(run_eval(items, client_factory(key), model, cfg, threshold))
    print(format_report(summary))
    out_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
