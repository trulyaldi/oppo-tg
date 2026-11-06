---
name: add-source
description: How to add a new opportunity source (job board, ATS, feed, Telegram channel) to the oppo-tg bot. Use when implementing or modifying any module in src/sources/.
---

Steps:
1. Create `src/sources/<name>.py` implementing the shared `Source` interface (see PLAN.md architecture). Fetch only via the shared HTTP client (honest User-Agent, timeout, rate limit, backoff, robots.txt check, response caching).
2. Normalize every item to the shared posting schema (id, source, title, company, url, location, remote flag, description, posted_at, deadline, raw tags). Stable id for dedupe.
3. Save a real sample response in `tests/fixtures/<name>.*`. Capture it once, trim it, and strip anything sensitive. Never fetch in tests.
4. Write `tests/test_source_<name>.py`: parser test against the fixture, plus empty response, malformed response, and HTTP error cases.
5. Handle failures: a failing source logs and returns nothing; it must never crash the pipeline or other sources. Respect 429/Retry-After.
6. Register the source in the source registry and update the sources list (SPEC.md Sources section and the /sources command output).
7. Never scrape LinkedIn directly; check the site's ToS and robots.txt, and record the finding in DECISIONS.md if non-obvious.
