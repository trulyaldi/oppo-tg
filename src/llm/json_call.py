"""One Messages call -> validated pydantic model (D-98, 8.1.10). Never raises or logs content."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from src.llm.budget import LlmBudget

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_FENCE = re.compile(r"^```[a-zA-Z]*\s*\n?(.*?)\n?```$", re.DOTALL)
CORRECTION = (
    "Your previous reply was not valid: {error}\n"
    "Reply again with only the JSON object for the schema, no other text."
)


@dataclass
class CallResult:
    value: Any = None
    error_kind: str | None = None  # budget | api | invalid
    calls: int = 0


class _Invalid(ValueError):
    pass


def _text(response: Any) -> str:
    return "".join(b.text for b in response.content if getattr(b, "type", None) == "text")


def _parse(raw: str, schema: type[T]) -> T:
    body = raw.strip()
    m = _FENCE.match(body)
    if m:
        body = m.group(1).strip()
    try:
        data, end = json.JSONDecoder().raw_decode(body)
    except json.JSONDecodeError as e:
        raise _Invalid(f"invalid JSON: {e.msg}") from e
    if body[end:].strip():
        raise _Invalid("trailing text after JSON")
    try:
        return schema.model_validate(data)
    except ValidationError as e:
        raise _Invalid(f"schema validation failed: {e.error_count()} error(s)") from e


async def call_json(
    client: Any,
    *,
    model: str,
    system_blocks: list[dict],
    user_text: str,
    schema: type[T],
    max_tokens: int,
    budget: LlmBudget,
    purpose: str,
) -> CallResult:
    res = CallResult()
    messages: list[dict] = [{"role": "user", "content": user_text}]
    try:
        for attempt in range(2):
            if not budget.reserve(purpose):
                res.error_kind = "budget"
                return res
            res.calls += 1
            response = await client.messages.create(
                model=model, max_tokens=max_tokens, system=system_blocks, messages=messages
            )
            raw = _text(response)
            try:
                res.value = _parse(raw, schema)
                return res
            except _Invalid as e:
                if attempt == 1:
                    res.error_kind = "invalid"
                    log.warning("llm invalid reply purpose=%s model=%s", purpose, model)
                    return res
                messages = messages + [
                    {"role": "assistant", "content": raw or "(empty reply)"},
                    {"role": "user", "content": CORRECTION.format(error=str(e)[:300])},
                ]
    except Exception as e:
        res.error_kind = "api"
        log.warning(
            "llm call failed purpose=%s model=%s error_kind=api (%s)",
            purpose,
            model,
            type(e).__name__,
        )
    return res
