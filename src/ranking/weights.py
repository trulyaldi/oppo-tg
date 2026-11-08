"""Employer-region weights: YAML loader, validation, built-in defaults (PLAN W1, D-91)."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

BUILTIN_VERSION = "builtin"
KZ_CATEGORIES = ("kz_employer", "kz_startup", "kz_program")
INTL_CATEGORIES = ("intl_top_tier", "intl_other")


class WeightsError(ValueError):
    """Invalid weights or watchlist file. The message names the offending field."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Adjustments(_Strict):
    intl_top_tier: float = Field(0.5, ge=-5, le=5)
    intl_other: float = Field(0.0, ge=-5, le=5)
    kz_employer: float = Field(1.0, ge=-5, le=5)
    kz_startup: float = Field(1.5, ge=-5, le=5)
    kz_program: float = Field(1.0, ge=-5, le=5)
    intl_onsite_visa_barrier: float = Field(-0.5, ge=-5, le=5)


class Quotas(_Strict):
    kz: int = Field(6, ge=0)
    intl: int = Field(3, ge=0)
    best_remaining: int = Field(1, ge=0)

    @property
    def total(self) -> int:
        return self.kz + self.intl + self.best_remaining

    @field_validator("best_remaining")
    @classmethod
    def _nonzero_sum(cls, v: int, info: Any) -> int:
        d = info.data
        if "kz" in d and "intl" in d and d["kz"] + d["intl"] + v < 1:
            raise ValueError("digest_quotas must sum to at least 1")
        return v


class Flags(_Strict):
    kz: str = Field("\U0001f1f0\U0001f1ff", max_length=8)
    intl: str = Field("\U0001f30d", max_length=8)


class Employer(_Strict):
    name: str
    aliases: list[str] = Field(default_factory=list)
    domain: str | None = None

    @field_validator("name")
    @classmethod
    def _name_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must be non-empty")
        return v


class KzEmployer(Employer):
    startup: bool = False


class _Watchlists(_Strict):
    intl_top_tier: str
    kz_employers: str


class _WatchFile(_Strict):
    employers: list[Any]


class _WeightsFile(_Strict):
    # All keys required in the file (a typo or omission must fail loudly).
    adjustments: dict[str, float]
    region_min_confidence: float = Field(ge=0, le=1)
    digest_quotas: Quotas
    flags: Flags
    watchlists: _Watchlists


class Weights(_Strict):
    adjustments: Adjustments = Field(default_factory=Adjustments)
    region_min_confidence: float = Field(0.7, ge=0, le=1)
    digest_quotas: Quotas = Field(default_factory=Quotas)
    flags: Flags = Field(default_factory=Flags)
    intl_top_tier: list[Employer] = Field(default_factory=list)
    kz_employers: list[KzEmployer] = Field(default_factory=list)
    version: str = BUILTIN_VERSION


BUILTIN_WEIGHTS = Weights()


def bucket(category: str | None) -> str | None:
    """Map an employer category to its digest quota bucket ("kz", "intl") or None."""
    if category in KZ_CATEGORIES:
        return "kz"
    if category in INTL_CATEGORIES:
        return "intl"
    return None


def _read_yaml(path: Path, label: str) -> tuple[Any, bytes]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise WeightsError(f"{label}: cannot read {path}: {exc.strerror or exc}") from exc
    try:
        return yaml.safe_load(raw), raw
    except yaml.YAMLError as exc:
        raise WeightsError(f"{label}: invalid YAML in {path.name}") from exc


def _fmt(exc: ValidationError, label: str) -> WeightsError:
    parts = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
    return WeightsError(f"{label}: " + "; ".join(parts))


def _load_watchlist(path: Path, label: str, model: type[Employer]) -> tuple[list, bytes]:
    data, raw = _read_yaml(path, label)
    try:
        wf = _WatchFile.model_validate(data if data is not None else {})
        return [model.model_validate(e) for e in wf.employers], raw
    except ValidationError as exc:
        raise _fmt(exc, label) from exc


def load_weights(path: str | Path) -> Weights:
    """Load and validate weights.yaml plus both watchlists. Raises WeightsError (ValueError)."""
    path = Path(path)
    data, raw = _read_yaml(path, "weights")
    if not isinstance(data, dict):
        raise WeightsError("weights: top level must be a mapping")
    try:
        wf = _WeightsFile.model_validate(data)
        expected = set(Adjustments.model_fields)
        got = set(wf.adjustments)
        for key in sorted(expected - got):
            raise WeightsError(f"adjustments.{key}: missing")
        for key in sorted(got - expected):
            raise WeightsError(f"adjustments.{key}: unknown key")
        adjustments = Adjustments.model_validate(wf.adjustments)
    except ValidationError as exc:
        raise _fmt(exc, "weights") from exc

    base = path.parent
    intl, intl_raw = _load_watchlist(
        base / wf.watchlists.intl_top_tier, "watchlists.intl_top_tier", Employer
    )
    kz, kz_raw = _load_watchlist(
        base / wf.watchlists.kz_employers, "watchlists.kz_employers", KzEmployer
    )

    seen: dict[str, str] = {}
    for label, entries in (("intl_top_tier", intl), ("kz_employers", kz)):
        for e in entries:
            for alias in [e.name, *e.aliases]:
                key = alias.strip().casefold()
                if not key:
                    raise WeightsError(f"watchlists.{label}: empty alias for {e.name!r}")
                if key in seen:
                    raise WeightsError(
                        f"watchlists.{label}: alias {alias!r} already listed under {seen[key]}"
                    )
                seen[key] = f"{label}/{e.name}"

    digest = hashlib.sha256()
    for blob in (raw, intl_raw, kz_raw):
        digest.update(blob)
    return Weights(
        adjustments=adjustments,
        region_min_confidence=wf.region_min_confidence,
        digest_quotas=wf.digest_quotas,
        flags=wf.flags,
        intl_top_tier=intl,
        kz_employers=kz,
        version=digest.hexdigest()[:12],
    )


def load_weights_or_default(path: str | Path, log: logging.Logger | None = None) -> Weights:
    """Never raises: on any problem log the error (no content) and return BUILTIN_WEIGHTS."""
    try:
        return load_weights(path)
    except Exception as exc:  # noqa: BLE001 - a YAML typo must not stop the digest (D-91)
        (log or logging.getLogger(__name__)).warning("weights unusable, using defaults: %s", exc)
        return BUILTIN_WEIGHTS
