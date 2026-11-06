"""Score adjustment and recomputation from the current weights (PLAN W5, D-94)."""

from __future__ import annotations

import sqlite3

from src.ranking.employer import classify_employer
from src.ranking.weights import Weights

_LABELS = {
    "intl_top_tier": "top-tier",
    "intl_other": "intl",
    "kz_employer": "KZ employer",
    "kz_startup": "KZ startup",
}
_INTL = ("intl_top_tier", "intl_other")


def _signed(x: float) -> str:
    return f"{x:+.1f}"


def adjust(
    base: float,
    category: str,
    visa_barrier: bool | None,
    remote: bool | None,
    weights: Weights,
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
    adj = round(adj, 2)
    adjusted = round(min(10.0, max(0.0, base + adj)), 2)
    return adjusted, adj, ", ".join(parts) if adj else ""


def _opt_bool(v: int | None) -> bool | None:
    return None if v is None else bool(v)


def recompute_adjusted(conn: sqlite3.Connection, weights: Weights) -> int:
    """Re-classify and re-adjust rows whose weights_version differs. Returns rows updated."""
    rows = conn.execute(
        "SELECT s.posting_id, s.base_score, s.employer_region, s.region_confidence,"
        " s.employer_is_startup, s.visa_barrier, p.company, p.kind, p.remote"
        " FROM scores s JOIN postings p ON p.id = s.posting_id"
        " WHERE s.weights_version IS NULL OR s.weights_version != ?",
        (weights.version,),
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
            ).category
            adjusted, adj, detail = adjust(
                r["base_score"], cat, _opt_bool(r["visa_barrier"]), _opt_bool(r["remote"]), weights
            )
            conn.execute(
                "UPDATE scores SET employer_category=?, adjustment=?, adjustment_detail=?,"
                " adjusted_score=?, weights_version=? WHERE posting_id=?",
                (cat, adj, detail, adjusted, weights.version, r["posting_id"]),
            )
    return len(rows)
