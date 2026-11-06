"""Deterministic contact type from the role string (N1, D-106)."""

from __future__ import annotations

import re

_RECRUITER = re.compile(
    r"recruit|\btalent\b|sourcer|people partner|\bhr\b|human resources|рекрутер|подбор",
    re.IGNORECASE,
)
_FOUNDER = re.compile(r"\bfounder\b|основател", re.IGNORECASE)
_CEO = re.compile(r"\bceo\b|генеральный директор", re.IGNORECASE)
_HIRING = re.compile(
    r"head of|\bdirector\b|\bvp\b|vice president|\bcto\b|\blead\b|team lead|"
    r"руководител|тимлид|директор",
    re.IGNORECASE,
)
_MANAGER = re.compile(r"\bmanager\b|менеджер", re.IGNORECASE)
_MANAGER_STRONG = re.compile(
    r"(engineering|research|data|software|technical|product|team|ml|ai)\s+manager",
    re.IGNORECASE,
)
_ENGINEER = re.compile(
    r"engineer|developer|researcher|scientist|\bquant\b|trader|инженер|разработчик|"
    r"исследовател",
    re.IGNORECASE,
)
_ANALYST = re.compile(r"analyst|аналитик", re.IGNORECASE)


def _classify(text: str) -> tuple[str, float]:
    if _RECRUITER.search(text):
        return "recruiter", 0.9
    if _FOUNDER.search(text):
        return "founder", 0.9
    if _CEO.search(text) and not _HIRING.search(text):
        return "founder", 0.6
    if _HIRING.search(text):
        return "hiring_manager", 0.8
    if _MANAGER.search(text):
        return "hiring_manager", 0.8 if _MANAGER_STRONG.search(text) else 0.6
    if _ENGINEER.search(text):
        return "engineer", 0.8
    if _ANALYST.search(text):
        return "engineer", 0.6
    return "other", 0.3


def classify_role(role: str, context: str = "") -> tuple[str, float]:
    """Return (contact_type, confidence); computed by code, never taken from a model."""
    kind, conf = _classify(role or "")
    if kind == "other" and not (role or "").strip() and context:
        kind, conf = _classify(context)
        conf = min(conf, 0.5)
    return kind, conf
