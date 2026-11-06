"""Parse and validate the model's JSON score response (D-11)."""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from src.models import ScoreResult

_FENCE = re.compile(r"^```[a-zA-Z]*\s*\n?(.*?)\n?```$", re.DOTALL)


class ScoreParseError(ValueError):
    """The model output was not a valid score object."""


def parse_score_response(text: str) -> ScoreResult:
    body = text.strip()
    m = _FENCE.match(body)
    if m:
        body = m.group(1).strip()
    try:
        data, end = json.JSONDecoder().raw_decode(body)
    except json.JSONDecodeError as e:
        raise ScoreParseError(f"invalid JSON: {e}") from e
    if body[end:].strip():
        raise ScoreParseError("trailing text after JSON")
    if not isinstance(data, dict):
        raise ScoreParseError("JSON is not an object")
    if isinstance(data.get("reason"), str):
        data["reason"] = " ".join(data["reason"].split())
    try:
        return ScoreResult.model_validate(data)
    except ValidationError as e:
        raise ScoreParseError(str(e)) from e
