"""Cue file loader and matcher (D-257, KF-05).

`config/cues.toml` holds every keyword list the bot uses, grouped by category and language
(en, ru, kk). Entry syntax:

- `word or phrase`: whole-word match at both ends; a space in a phrase matches spaces or hyphens;
- `stem*`: prefix match, for Russian and Kazakh inflection (not on entries of 3 characters or
  fewer);
- `re:<regex>`: raw regex over the folded text, compiled once at load.

Text and entries are NFKC case-folded with the Cyrillic yo folded to ye. Boundaries use the
Unicode-aware `\\w`, so "ai" never matches inside "maintenance" or "email".
"""

from __future__ import annotations

import re
import tomllib
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LANGS = ("en", "ru", "kk")
DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "cues.toml"

# Top-level tables. "flat" tables are one group; "nested" tables hold named sub-groups.
_FLAT = ("negative", "seniority", "junior", "protect", "spam", "unpaid", "hard_blockers")
_FLAT_OTHER = ("telegram_discovery",)
_HH_KEYS = ("groups", "max_terms_per_query", "max_requests_per_run")
_NESTED = ("types", "fields", "locations", "qualifications")
_ALLOWED_SUBGROUPS = {"locations": ("kz",)}
_POSITIVE_TABLES = ("types", "fields")
_SHORT_ENTRY = 3
_WORD_GAP = r"[\s\-]+"


class CueError(ValueError):
    """The cue file is malformed. The message names the offending table and entry."""


def fold(text: str) -> str:
    """NFKC, case-fold, and fold the Cyrillic yo to ye."""
    return unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")


@dataclass(frozen=True)
class Entry:
    raw: str
    kind: str  # "word" | "stem" | "re"
    pattern: re.Pattern[str]

    @property
    def plain(self) -> bool:
        return self.kind == "word"


def compile_entry(raw: str, where: str = "") -> Entry:
    """Compile one cue entry; raises CueError naming the entry."""
    prefix = f"{where}: " if where else ""
    if not isinstance(raw, str) or not raw.strip():
        raise CueError(f"{prefix}entry {raw!r} must be a non-empty string")
    text = raw.strip()
    if text.startswith("re:"):
        body = text[3:]
        try:
            return Entry(text, "re", re.compile(body, re.IGNORECASE))
        except re.error as exc:
            raise CueError(f"{prefix}entry {raw!r}: invalid regex ({exc})") from exc
    words = fold(text).split()
    if "*" in text and len(text.replace("*", "")) <= _SHORT_ENTRY:
        raise CueError(
            f"{prefix}entry {raw!r}: '*' on a short entry ({_SHORT_ENTRY} characters or fewer "
            "are whole-word only)"
        )
    parts: list[str] = []
    last_is_stem = False
    has_stem = False
    for word in words:
        if "*" in word[:-1] or (word == "*"):
            raise CueError(f"{prefix}entry {raw!r}: '*' is only allowed at the end of a word")
        if word.endswith("*"):
            stem = word[:-1]
            if not stem:
                raise CueError(f"{prefix}entry {raw!r}: '*' needs a stem before it")
            parts.append(re.escape(stem) + r"\w*")
            last_is_stem = has_stem = True
        else:
            parts.append(re.escape(word))
            last_is_stem = False
    pattern = r"(?<!\w)" + _WORD_GAP.join(parts) + ("" if last_is_stem else r"(?!\w)")
    return Entry(text, "stem" if has_stem else "word", re.compile(pattern))


@dataclass(frozen=True)
class Group:
    """One cue table: entries per language."""

    name: str
    entries: dict[str, tuple[Entry, ...]] = field(default_factory=dict)

    def all_entries(self, langs: Iterable[str] | None = None) -> Iterator[Entry]:
        for lang in langs or LANGS:
            yield from self.entries.get(lang, ())

    def terms(self, langs: Iterable[str] | None = None, plain_only: bool = False) -> list[str]:
        out: list[str] = []
        for e in self.all_entries(langs):
            if (not plain_only or e.plain) and e.raw not in out:
                out.append(e.raw)
        return out

    def count(self, lang: str | None = None) -> int:
        return sum(1 for _ in self.all_entries([lang] if lang else None))


def match_any(group: Group, text: str, langs: Iterable[str] | None = None) -> bool:
    """True if any entry of the group matches the text."""
    folded = fold(text)
    return any(e.pattern.search(folded) for e in group.all_entries(langs))


def which(group: Group, text: str, langs: Iterable[str] | None = None) -> list[str]:
    """Raw entries of the group that match the text, in file order."""
    folded = fold(text)
    return [e.raw for e in group.all_entries(langs) if e.pattern.search(folded)]


@dataclass(frozen=True)
class Cues:
    groups: dict[str, Group]
    settings: dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, name: str) -> Group:
        try:
            return self.groups[name]
        except KeyError:
            raise KeyError(f"unknown cue group {name!r}") from None

    def names(self, table: str) -> list[str]:
        """Group names of a table: `names("types")` -> ["types.internship", ...]."""
        return [n for n in self.groups if n == table or n.startswith(table + ".")]

    def positive_groups(self) -> list[Group]:
        return [self.groups[n] for t in _POSITIVE_TABLES for n in self.names(t)]

    def positive_terms(self, langs: Iterable[str] | None = None) -> list[str]:
        """Every positive cue (types and fields), de-duplicated, in file order."""
        out: list[str] = []
        for g in self.positive_groups():
            for term in g.terms(langs):
                if term not in out:
                    out.append(term)
        return out

    def positive_count(self, lang: str | None = None) -> int:
        return sum(g.count(lang) for g in self.positive_groups())

    def group_terms(
        self, name: str, langs: Iterable[str] | None = None, plain_only: bool = False
    ) -> list[str]:
        return self[name].terms(langs, plain_only)

    def expand(self, word: str) -> list[str]:
        """The word plus the search terms of every positive group it belongs to (KF-29).

        Search terms are the plain entries and the stems with their `*` removed; regexes are
        skipped."""
        out = [word]
        seen = {fold(word)}
        for g in self.positive_groups():
            if not match_any(g, word):
                continue
            for e in g.all_entries():
                term = e.raw.rstrip("*") if e.kind != "re" else ""
                if term and fold(term) not in seen:
                    seen.add(fold(term))
                    out.append(term)
        return out

    def expand_entries(self, word: str) -> list[str]:
        """Like `expand`, but entries keep their raw syntax (`stem*`, phrases) so that a caller
        can compile them with `compile_entry` and match with the cue rules (FIX-52)."""
        out: list[str] = []
        seen: set[str] = set()
        for g in self.positive_groups():
            if not match_any(g, word):
                continue
            for e in g.all_entries():
                if e.kind != "re" and fold(e.raw) not in seen:
                    seen.add(fold(e.raw))
                    out.append(e.raw)
        return out

    def is_positive(self, text: str) -> bool:
        return any(match_any(g, text) for g in self.positive_groups())

    def title_decision(self, title: str) -> str:
        """ "keep" or "drop:<reason>". A positive match is required; then seniority (unless
        junior or intern); then negative (unless protected)."""
        if not self.is_positive(title):
            return "drop:no_positive"
        if match_any(self["seniority"], title) and not match_any(self["junior"], title):
            return "drop:seniority"
        if match_any(self["negative"], title) and not match_any(self["protect"], title):
            return "drop:negative"
        return "keep"


def title_decision(cues: Cues, title: str) -> str:
    return cues.title_decision(title)


def _build_group(name: str, table: Any) -> Group:
    if not isinstance(table, dict):
        raise CueError(f"[{name}] must be a table with {'/'.join(LANGS)} lists")
    entries: dict[str, tuple[Entry, ...]] = {}
    for key, value in table.items():
        if key not in LANGS:
            raise CueError(f"[{name}]: unknown language key {key!r} (expected {', '.join(LANGS)})")
        if not isinstance(value, list):
            raise CueError(f"[{name}].{key} must be a list of strings")
        seen: set[str] = set()
        compiled: list[Entry] = []
        for raw in value:
            entry = compile_entry(raw, f"[{name}].{key}")
            if (entry.kind, fold(entry.raw)) in seen:
                continue
            seen.add((entry.kind, fold(entry.raw)))
            compiled.append(entry)
        entries[key] = tuple(compiled)
    return Group(name, entries)


def _parse_hh(table: dict[str, Any], settings: dict[str, Any]) -> None:
    """[hh]: the ordered query groups and the two bounds (KF-20)."""
    for key, value in table.items():
        if key not in _HH_KEYS:
            raise CueError(f"[hh]: unknown key {key!r} (expected {', '.join(_HH_KEYS)})")
        if key == "groups":
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise CueError("[hh].groups must be a list of cue group names")
            settings["hh.groups"] = list(value)
        elif isinstance(value, int) and not isinstance(value, bool) and value >= 1:
            settings[f"hh.{key}"] = value
        else:
            raise CueError(f"[hh].{key} must be a positive integer, got {value!r}")


def parse_cues(data: dict[str, Any]) -> Cues:
    """Validate and compile a parsed cue document."""
    known = _FLAT + _FLAT_OTHER + _NESTED + ("hh",)
    groups: dict[str, Group] = {}
    settings: dict[str, Any] = {}
    for key, table in data.items():
        if key not in known:
            raise CueError(f"unknown table [{key}] (expected one of {', '.join(known)})")
        if key in _FLAT or key in _FLAT_OTHER:
            groups[key] = _build_group(key, table)
            continue
        if not isinstance(table, dict):
            raise CueError(f"[{key}] must be a table of named groups")
        if key == "hh":
            _parse_hh(table, settings)
            continue
        for sub, value in table.items():
            allowed = _ALLOWED_SUBGROUPS.get(key)
            if allowed is not None and sub not in allowed:
                raise CueError(f"unknown table [{key}.{sub}] (expected {', '.join(allowed)})")
            name = f"{key}.{sub}"
            groups[name] = _build_group(name, value)
    for table in _FLAT:
        groups.setdefault(table, Group(table))
    for name in settings.get("hh.groups", []):
        if name not in groups:
            raise CueError(f"[hh].groups: unknown cue group {name!r}")
    return Cues(groups, settings)


def parse_cues_text(text: str) -> Cues:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise CueError(f"cue file is not valid TOML: {exc}") from exc
    return parse_cues(data)


def load_cues(path: str | Path = DEFAULT_PATH) -> Cues:
    """Load, validate and compile the cue file."""
    return parse_cues_text(Path(path).read_text(encoding="utf-8"))
