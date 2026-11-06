"""Live source check for `src.main probe` (T19e, D-56). Never touches postings."""

from __future__ import annotations

import sqlite3
from typing import TextIO

from src.clock import Clock
from src.config import AppConfig, Settings
from src.http import PoliteClient
from src.sources import base
from src.sources.base import (
    MIN_SOURCE_FAMILIES,
    SourceContext,
    run_source,
    source_credentials,
    source_family,
)


def reset_probe_state(conn: sqlite3.Connection) -> None:
    """Clear only per-run state (FIX-01, D-72): page snapshots and source run records.

    http_cache and robots_cache stay, so the 1h Jobicy TTL (D-58) and the 24h robots TTL (D-08)
    still apply across probe runs. Only call this on the probe DB, never the real DB_PATH.
    """
    with conn:
        conn.execute("DELETE FROM page_snapshots")
        conn.execute("DELETE FROM source_runs")


async def run_probe(
    http: PoliteClient, settings: Settings, cfg: AppConfig, clock: Clock, out: TextIO
) -> int:
    """Run each enabled source once, print one line per source, return the exit code."""
    ctx = SourceContext(http=http, settings=settings, cfg=cfg, clock=clock)
    families_with_items: set[str] = set()
    out.write("family  name  status  ok  n_items\n")
    for name in sorted(base.REGISTRY):
        src = base.REGISTRY[name]()
        family = source_family(src)
        creds = source_credentials(src)
        status = "credentials: " + ", ".join(creds) if creds else "counted"
        if not src.enabled(settings, cfg):
            if not creds:
                reason = "disabled in config.toml"
                fn = getattr(src, "disabled_reason", None)
                if callable(fn):
                    reason = fn(settings, cfg) or reason
                status = f"disabled: {reason}"
            out.write(f"{family}  {name}  {status}  -  -\n")
            continue
        res = await run_source(src, ctx)
        n = len(res.items)
        out.write(f"{family}  {name}  {status}  {'yes' if res.ok else 'no'}  {n}\n")
        if res.error:
            out.write(f"  error: {res.error}\n")
        if not creds and res.ok and n >= 1:
            families_with_items.add(family)
    total = len(families_with_items)
    out.write(f"Counted families with items: {total} (need {MIN_SOURCE_FAMILIES})\n")
    return 0 if total >= MIN_SOURCE_FAMILIES else 1
