"""Digest picking with per-bucket quotas and fall-through (PLAN W6, D-95)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TypeVar

from src.ranking.weights import Quotas, bucket

T = TypeVar("T")


def pick_with_quotas(
    items: Sequence[T],
    quotas: Quotas,
    category_of: Callable[[T], str | None],
) -> list[T]:
    """Pick from items already sorted best-first; returns the picked set in the same order.

    The first `quotas.kz` kz-bucket and `quotas.intl` intl-bucket items are taken, then the
    remaining `total - picked` slots go to the best unpicked items of any bucket. Unused
    bucket slots fall through to that last step. Unknown-bucket items only use free slots.
    """
    picked: set[int] = set()
    left = {"kz": quotas.kz, "intl": quotas.intl}
    for idx, item in enumerate(items):
        b = bucket(category_of(item))
        if b is not None and left[b] > 0:
            left[b] -= 1
            picked.add(idx)
    free = quotas.total - len(picked)
    for idx in range(len(items)):
        if free <= 0:
            break
        if idx not in picked:
            picked.add(idx)
            free -= 1
    return [items[i] for i in sorted(picked)]
