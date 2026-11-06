# oppo-tg

Personal Telegram bot that finds, ranks and delivers job, internship, startup and hackathon opportunities (Kazakhstan + worldwide), tuned to one user. No web UI.

**Read SPEC.md, PLAN.md and DECISIONS.md before starting work.** SPEC.md is the source of truth for requirements; PLAN.md is the task list; DECISIONS.md logs every non-obvious choice (choice + reason).

## Pipeline

collect -> dedupe -> cheap filter -> LLM score -> Telegram digest -> feedback

## Stack

Python 3.11+, aiogram (Telegram), SQLite, a cron-style scheduler (APScheduler or equivalent), Claude API for scoring. Tests: pytest. Lint/format: ruff.

## Conventions

- One module per source in `src/sources/`, each implementing the shared `Source` interface and returning the shared posting schema.
- Tests use saved fixtures in `tests/fixtures/`, never the live network. Never call the real Telegram or Claude API in tests (mock them).
- No secrets in code. Config comes from `.env` (see `.env.example`). Never read or write `.env`.
- Respect robots.txt, rate limits and site ToS. Send an honest User-Agent, cache responses, back off on errors.
- Never scrape LinkedIn directly.
- One commit per PLAN.md task; mark the task done in PLAN.md in the same commit.

## Commands

- Tests: `python -m pytest -q`
- Lint: `ruff check . && ruff format --check .`
- Setup: `python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt`

## Workflow

Agents live in `.claude/agents/` (planner, implementer, reviewer). The full autonomous loop is the `autonomous-build` skill. Never stop to ask for approval; record decisions in DECISIONS.md.
