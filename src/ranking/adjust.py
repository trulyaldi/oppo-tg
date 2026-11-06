"""Score adjustment and recomputation from the current weights (PLAN W5, D-94)."""

from __future__ import annotations

import sqlite3
from typing import Any

from src.ranking.employer import classify_employer
from src.ranking.weights import Weights

_LABELS = {
    "intl_top_tier": "top-tier",
    "intl_other": "intl",
    "kz_employer": "KZ employer",
    "kz_startup": "KZ startup",
}
_INTL = ("intl_top_tier", "intl_other")


def region_hints(cfg: Any) -> dict[str, str]:
    """Source name -> region_hint from [sources.*] config (D-194)."""
    out: dict[str, str] = {}
    for name, sec in (getattr(cfg, "sources", None) or {}).items():
        hint = sec.get("region_hint") if isinstance(sec, dict) else None
        if isinstance(hint, str) and hint.strip():
            out[name] = hint.strip().lower()
    return out


def blocker_penalty(cfg: Any) -> float:
    """Penalty for hard-blocked postings in lower mode, else 0 (D-195)."""
    f = getattr(cfg, "filters", None) or {}
    if str(f.get("hard_blocker_action", "lower")).strip().lower() == "filter":
        return 0.0
    try:
        return float(f.get("hard_blocker_penalty", 2.0))
    except (TypeError, ValueError):
        return 2.0


def _signed(x: float) -> str:
    return f"{x:+.1f}"


def adjust(
    base: float,
    category: str,
    visa_barrier: bool | None,
    remote: bool | None,
    weights: Weights,
    hard_blocker: str | None = None,
    blocker_penalty: float = 0.0,
) -> tuple[float, float, str]:
    """Return (adjusted, adjustment, detail). Clamped to [0, 10], rounded to 2 decimals."""
    adj = float(getattr(weights.adjustments, category, 0.0)) if category != "unknown" else 0.0
    parts: list[str] = []
    if adj:
        parts.append(f"{_signed(adj)} {_LABELS[category]}")
    if category in _INTL and visa_barrier is True and remote is not True:
        visa = weights.adjustments.intl_onsite_visa_barrier
        if visa:
            adj += visa
            parts.append(f"{_signed(visa)} visa")
    if hard_blocker and blocker_penalty:
        adj -= abs(blocker_penalty)
        parts.append(f"{_signed(-abs(blocker_penalty))} hard blocker ({hard_blocker})")
    adj = round(adj, 2)
    adjusted = round(min(10.0, max(0.0, base + adj)), 2)
    return adjusted, adj, ", ".join(parts) if adj else ""


def _opt_bool(v: int | None) -> bool | None:
    return None if v is None else bool(v)


def recompute_adjusted(
    conn: sqlite3.Connection,
    weights: Weights,
    hints: dict[str, str] | None = None,
    blocker_penalty: float = 0.0,
) -> int:
    """Re-classify and re-adjust rows whose weights_version differs. Returns rows updated."""
    hints = hints or {}
    version = weights.version
    if hints:  # a changed hint must trigger a recompute too
        version += "+" + ",".join(f"{k}={v}" for k, v in sorted(hints.items()))
    if blocker_penalty:
        version += f"+hb={blocker_penalty:g}"
    rows = conn.execute(
        "SELECT s.posting_id, s.base_score, s.employer_region, s.region_confidence,"
        " s.employer_is_startup, s.visa_barrier, p.company, p.kind, p.remote, p.source,"
        " p.hard_blocker"
        " FROM scores s JOIN postings p ON p.id = s.posting_id"
        " WHERE s.weights_version IS NULL OR s.weights_version != ?",
        (version,),
    ).fetchall()
    with conn:
        for r in rows:
            cat = classify_employer(
                r["company"],
                r["kind"],
                r["employer_region"],
                r["region_confidence"],
                _opt_bool(r["employer_is_startup"]),
                weights,
                hints.get(r["source"]),
            ).category
            adjusted, adj, detail = adjust(
                r["base_score"],
                cat,
                _opt_bool(r["visa_barrier"]),
                _opt_bool(r["remote"]),
                weights,
                r["hard_blocker"],
                blocker_penalty,
            )
            conn.execute(
                "UPDATE scores SET employer_category=?, adjustment=?, adjustment_detail=?,"
                " adjusted_score=?, weights_version=? WHERE posting_id=?",
                (cat, adj, detail, adjusted, version, r["posting_id"]),
            )
    return len(rows)
