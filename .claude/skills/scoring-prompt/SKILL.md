---
name: scoring-prompt
description: Where the user profile and scoring rubric live, the JSON output contract for LLM scoring, how to evaluate a prompt change against evals/, and how recent feedback is injected. Use when editing the scoring prompt, rubric, profile, or eval set.
---

- Profile and rubric: SPEC.md is the source of truth. The prompt lives in `src/scoring/` (profile and rubric as separate text files or constants, loaded at runtime so edits do not need code changes).
- Rubric weights: match 35, upside 25, interest 15, timing 15, accessibility 10. Score 0-10.
- Output contract v2 (strict JSON, validate and retry once on parse failure; D-251): `{"score", "reason" (English, at most 110 chars, names the concrete match or mismatch), "type", "deadline", "red_flags" (max 3, each at most 40 chars), "employer_region", "region_confidence", "employer_is_startup", "visa_barrier"}`. No tags. Flag visa/work-authorization needs in `red_flags`; never filter on them.
- Feedback injection: append the 20 most recent liked and 20 most recent disliked items (title, company, stored reason clipped to 110) as examples in the prompt.
- Evals: labeled set in `evals/` (JSONL: posting + expected score range). After ANY prompt, rubric or profile change, re-run the eval runner and record the result in DECISIONS.md. Tests mock the Claude API; evals are the only thing allowed to call it, and only on explicit run.
- Model: choose a cheap, fast Claude model for scoring; log the choice in DECISIONS.md. Use the `claude-api` skill for current model ids and params.
