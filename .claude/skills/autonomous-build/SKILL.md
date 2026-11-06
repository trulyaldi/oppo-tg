---
name: autonomous-build
description: Run the full autonomous build loop for the oppo-tg bot: planner writes PLAN.md, implementer executes tasks in order with tests, reviewer audits, implementer fixes, repeat until the SPEC.md definition of done is met. Use when asked to build, continue building, or finish the bot.
---

Run this loop from the main conversation (subagents cannot spawn subagents, so you orchestrate). Never wait for user approval.

1. Read CLAUDE.md, SPEC.md, DECISIONS.md and PLAN.md.
2. If PLAN.md has no task list, invoke the `planner` agent to write it. It logs decisions in DECISIONS.md.
3. Loop over unchecked tasks in PLAN.md order: invoke the `implementer` agent for exactly one task. It tests, commits once, and marks the task done. If it reports a blocker it could not resolve, invoke `planner` to re-plan, then continue.
4. Confirm `python -m pytest -q` is green after each task (the Stop hook also enforces this).
5. When all tasks are checked, invoke the `reviewer` agent. It appends `FIX-` tasks to PLAN.md or writes `REVIEW PASSED`.
6. If there are new unchecked tasks, go back to step 3. Repeat until SPEC.md's definition of done is satisfied and the reviewer has passed.
7. Stop only when done, or when blocked on something only the user can provide (e.g. real TELEGRAM_BOT_TOKEN, ANTHROPIC_API_KEY, TELEGRAM_CHAT_ID, Telethon credentials). In that case list exactly what is needed and where to put it, and what remains.

Guardrails: tests use fixtures only; never read or write `.env`; respect robots.txt and ToS; never scrape LinkedIn directly.
