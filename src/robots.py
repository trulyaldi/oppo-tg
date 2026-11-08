"""RFC 9309 robots.txt matching with `*` and `$` wildcards (D-267).

`urllib.robotparser` ignores wildcards, so real files such as enbek.kz and Remotive were
misread. This is a small in-house parser and matcher, no new dependency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_PCT = re.compile(r"%[0-9a-fA-F]{2}")


@dataclass
class Rules:
    # agent token (lower case) -> list of (allow, pattern)
    groups: dict[str, list[tuple[bool, str]]] = field(default_factory=dict)


def _norm(s: str) -> str:
    return _PCT.sub(lambda m: m.group(0).upper(), s)


def parse(body: str) -> Rules:
    rules = Rules()
    agents: list[str] = []
    in_agents = False  # last directive was a user-agent line
    for raw in body.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "user-agent":
            if not in_agents:
                agents = []
            agents.append(value.lower())
            rules.groups.setdefault(value.lower(), [])
            in_agents = True
        elif key in ("allow", "disallow"):
            in_agents = False
            if not value:
                continue  # empty Disallow (or Allow) adds no restriction
            for a in agents:
                rules.groups[a].append((key == "allow", _norm(value)))
        else:
            in_agents = False
    return rules


def _matches(pattern: str, path: str) -> bool:
    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]
    regex = ".*".join(re.escape(p) for p in pattern.split("*"))
    return re.match(regex + ("$" if anchored else ""), path, re.DOTALL) is not None


def allowed(rules: Rules, agent: str, path_with_query: str) -> bool:
    agent = agent.lower()
    group = rules.groups.get(agent)
    if group is None:
        group = rules.groups.get("*")
    if not group:
        return True
    path = _norm(path_with_query or "/")
    best_len = -1
    best_allow = True
    for allow, pattern in group:
        if _matches(pattern, path):
            n = len(pattern)
            if n > best_len or (n == best_len and allow):
                best_len, best_allow = n, allow
    return best_allow
