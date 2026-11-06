# DECISIONS

Log every non-obvious choice: date, choice, reason. Newest last.

## 2026-11-06 — Initial architecture (planner)

- **D-01 Task order: thin vertical slice first.** T01-T11 build tooling, config, DB, HTTP client, one source (Greenhouse), filters, scoring, formatting and digest sending, ending in an end-to-end test (`tests/test_e2e_slice.py`). After that come the remaining sources, then bot feedback/commands, feedback injection, alerts, scheduler, evals and README. Reason: the pipeline is proven end to end before effort is spread across sources.
- **D-02 Commands use `.venv/bin/python -m ...`.** `python` is not on PATH (only `python3` 3.12.3). `source .venv/bin/activate` and `.venv/bin/ruff` are not in the permission allowlist, while `.venv/bin/python *` and `python3 *` are. So: `python3 -m venv .venv`, `.venv/bin/python -m pip install -r requirements.txt`, `.venv/bin/python -m pytest -q`, `.venv/bin/python -m ruff check .`, `.venv/bin/python -m ruff format --check .`. CLAUDE.md lists `python -m pytest -q` and an `activate` step. Agents may not edit CLAUDE.md, so PLAN.md section 0 is the operative command list. The two are equivalent once the venv is active.
- **D-03 `.env.example` cannot be edited by agents.** `.claude/settings.json` denies Read/Edit/Write on `./.env.*` for every agent, but SPEC's definition of done needs `.env.example` to be complete. The canonical env list lives in `src/config.py` (`ENV_VARS`), and the README has an env table that a test checks against `ENV_VARS`. The exact lines for `.env.example` go in PLAN section 3 (User actions) and the README. Agents must not use Bash or anything else to get around the deny rule. The existing variable names were not read. The three required names (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `ANTHROPIC_API_KEY`) are the conventional ones that CLAUDE.md and the autonomous-build skill already use.
- **D-04 Layout: `src/` is an importable package run from the repo root (`python -m src.main`), with pytest `pythonpath=["."]` and no packaging.** CLAUDE.md and the skills require the paths `src/sources/` and `src/scoring/`. A `src.` import prefix keeps those paths and avoids generic top-level module names (`config`, `db`, `http`) that would shadow stdlib or third-party modules.
- **D-05 Dependency injection everywhere.** Clock, httpx transport, Anthropic client, aiogram Bot, Telethon client and DB connection are all passed in. `load_dotenv()` runs only in `src.main.main()`. Nothing reads the environment or network at import time. Reasons: deterministic tests for dates (deadline windows, 09:00 Asia/Almaty, "Today is" in the prompt), no live network, and no `.env` access from tests.
- **D-06 Config split.** Secrets and deployment knobs come from env vars (`Settings`). User-editable, non-secret lists (ATS boards, hh queries, feeds, pages, Telegram channels, filter keywords, thresholds) go in a committed `config.toml`, read with stdlib `tomllib`. Reason: the watchlists are long and structured, which suits TOML better than env vars, and they hold no secrets. TOML instead of YAML because `tomllib` needs no extra dependency.
- **D-07 Optional sources are disabled, not failing, when credentials are missing.** `Source.enabled()` returns false with a reason shown in `/sources`. This applies to Kaggle, Telethon, and hh.kz if it needs a token. Reason: the bot must run unattended with only the three required secrets.
- **D-08 HTTP politeness policy.** There is one shared async `PoliteClient` on httpx, plus `httpx.MockTransport` in tests, so no respx dependency. Policy:
  - User-Agent: `oppo-tg/0.1 (personal job-alert bot; +<HTTP_CONTACT>)`.
  - Timeout 20s.
  - At least 2s between requests to the same host.
  - Up to 3 retries with 1/2/4s backoff on 429, 5xx and timeouts. `Retry-After` is honored, capped at 60s.
  - robots.txt is cached for 24h. A 404 means allowed. A 5xx or timeout on robots.txt means disallowed for that run, to be conservative.
  - Responses are cached in SQLite with a TTL and ETag/Last-Modified revalidation.
  
  Reason: the CLAUDE.md requirements (robots, rate limits, honest UA, caching, backoff) are met in one place, so no source can skip them.
- **D-09 Posting id = `sha1("{source}:{external_id}")[:16]`.** Telegram callback data is capped at 64 bytes. `fb:applied:` uses 11, which leaves room, and 16 hex chars (64 bits) cannot realistically collide at this volume. Cross-source duplicates are handled separately by a `fingerprint` (normalized company + title).
- **D-10 Plain `sqlite3`, no ORM, migrations via `PRAGMA user_version`, WAL mode.** A single-user bot with about 10 tables does not need SQLAlchemy. WAL lets the bot handlers and scheduler jobs share the file safely.
- **D-11 Scoring output is validated with pydantic v2, with one retry.** pydantic is already a dependency of aiogram 3. On a parse or validation failure the scorer retries once, sending the bad output plus a correction message, as the scoring-prompt skill requires. A second failure sets status `score_error`, and the item is retried on later runs up to 3 attempts. Posting text is wrapped in `<posting>` tags and the prompt says to treat it as untrusted data, as a prompt-injection defense, because scraped text and Telegram posts are attacker-controllable.
- **D-12 Scoring model.** Default `SCORING_MODEL=claude-haiku-4-5` (cheap and fast), overridable by env. The implementer must confirm the current cheapest suitable model id with the `claude-api` skill in T09 and update this entry if it changes. Each call scores one posting, with `temperature=0` and `max_tokens=300`. The static system block (profile + rubric + contract) uses prompt caching (`cache_control: ephemeral`), so repeated calls in a run are cheap. Reasons: one posting per call keeps parsing and retries simple; batching gives little benefit at about 150 items per run.
- **D-13 Cost-control prefilter beyond SPEC's hard filters.** SPEC lists the hard filters as spam, unpaid, senior-only and duplicates. Two additions:
  - A "stale" filter: deadline passed, or posted more than 60 days ago.
  - A title-keyword relevance prefilter for JOB/INTERNSHIP kinds only. A title must match a positive keyword such as intern, quant, research, data, ML, machine learning, AI, scientist, analyst, engineer, developer or trader, otherwise it gets `filter_reason=irrelevant`.

  `MAX_SCORES_PER_RUN=150` caps LLM calls per run. Reason: large ATS boards (Databricks, Anthropic and others) list hundreds of irrelevant roles such as sales and legal, and scoring them all would waste API budget. Hackathon, fellowship, research and competition kinds skip the relevance check. The keywords are in `config.toml`, so the user can widen them.
- **D-14 Deadline precedence.** A structured deadline from the source wins. Otherwise the LLM-extracted deadline is used. Reason: source fields are more reliable than extraction from free text.
- **D-15 Reaction semantics.**
  - Every press is appended to `reaction_log` (full history kept).
  - `like` and `dislike` are mutually exclusive.
  - Pressing an active button toggles it off.
  - `save` and `applied` toggle independently.

  This reads the digest-format skill's "last action wins per action type" as one current state per action. Feedback examples use only currently active likes and dislikes.
- **D-16 Digest = a header message plus one message per item, each with its own inline keyboard.** Buttons must map one-to-one to items, and per-item messages never come near the 4096-char limit. Item text is hard-capped at 3500 chars before rendering, so it is never cut mid-tag.
- **D-17 Digest selection.**
  - Up to 10 items with score >= 5.0, ordered by score descending, then deadline ascending.
  - The digest is never padded with weak items to reach 5. SPEC's "5-10" is a target, not a reason to send noise.
  - With zero qualifying items, one "No new opportunities today." message is sent, so the user knows the bot is alive.
  - An item is delivered at most once, ever (`deliveries.posting_id` primary key), whether by digest or alert.
- **D-18 No emoji in bot output.** The alert marker is `<b>ALERT</b>`, the active-button marker is `✓`, and red flags are shown as `Red flags: ...`. Reasons: readable plain text, and it matches the project's no-emoji preference.
- **D-19 Single-user bot over long polling.** A middleware drops every update whose chat id is not `TELEGRAM_CHAT_ID`. Long polling (not webhooks) means no public HTTPS endpoint or server is needed. Reason: it is a personal bot, and buttons that write to the DB must not be reachable by strangers.
- **D-20 Scheduler: APScheduler 3.x `AsyncIOScheduler`, in the same asyncio process as aiogram polling.**
  - `collect_and_score` runs every 3h (`COLLECT_CRON_HOURS`) and sends alerts afterwards.
  - `daily_digest` runs at 09:00 Asia/Almaty.
  - `prune` runs daily.
  - All jobs use `coalesce=True`, `max_instances=1` and `misfire_grace_time=3600`, and every job body is wrapped in `safe_job` so exceptions are logged and never stop the scheduler.

  3.x rather than 4.x because 3.x is the long-stable API, while 4.x has had breaking pre-releases. `/pause` skips (does not queue) digests and alerts, while collection and scoring continue, so `/top` stays current.
- **D-21 A single `requirements.txt` that also holds the test/lint tools (pytest, pytest-asyncio, ruff).** CLAUDE.md's setup installs only `requirements.txt`, and tests must run right after setup. Versions use lower bounds with major-version caps, not exact pins, for a personal project on Python 3.11+ (developed on 3.12). `tzdata` is included so `Asia/Almaty` resolves on any OS. The tz database reflects Kazakhstan's move to a single UTC+5 zone on 2024-03-01.
- **D-22 Source selection and ToS stance.**
  - **Solid, credential-free, first:**
    - Greenhouse Job Board API, Lever Postings API and Ashby Posting API. All are public APIs published so third parties can list jobs.
    - Hacker News "Who is hiring" via the official Algolia HN API.
    - RSS/Atom feeds.
    - A fellowship/research page watcher that fetches configured public pages only after a robots check, hashes the content and alerts on change.
  - **Probe at implementation time:**
    - hh.kz via the official `api.hh.ru`, with the required `HH-User-Agent`. Anonymous access may be restricted.
    - Devpost (`/api/hackathons` JSON) and MLH (events page): robots/ToS are checked first.
  - **Credential-gated:** Kaggle (official API, Basic auth) and Telegram channels via Telethon (user session, public channels, read-only).
  - **Never:** LinkedIn.

  Each probe-dependent task has an explicit exit: drop the source, log the reason here, mark the task done.
- **D-23 SPEC.md belongs to the user.** The only SPEC edit agents make is appending an implemented/dropped note to the matching bullet in the Sources section, as the add-source skill requires. Requirements and [EDIT] items are never changed by agents. Company watchlists and channels go in `config.toml`.
- **D-24 YC Work at a Startup is never scraped.** It requires a logged-in account, and automated access to logged-in areas breaks its terms. T27 instead probes the public `ycombinator.com/jobs` pages under robots.txt and drops the source if they are disallowed. YC startups are also covered by HN "Who is hiring" and the Ashby/Lever boards many YC companies use.
- **D-25 Alert rule matches SPEC literally by default.** An item alerts when score >= 9.0, or when its deadline falls within 7 days (today through today + 7, in Asia/Almaty). The deadline path has a config knob, `alert_deadline_min_score`, defaulting to 0.0, so SPEC is followed exactly. Items reaching scoring have already passed the hard filters. If near-deadline alerts prove noisy, the user can raise the knob in `config.toml` without a code change. Alerts respect `/pause`.
- **D-26 Counting toward "at least 6 sources".** Each module in `src/sources/` counts as one source. Greenhouse, Lever and Ashby are separate APIs with separate parsers, so by that count the probe-free sources are already 6: Greenhouse, Lever, Ashby, HN, RSS and the page watcher. A stricter reviewer may count the ATS trio as one. Under that count the probe-free sources are only 4 (ATS, HN, RSS, page watcher), and reaching 6 needs at least two of hh.kz, Devpost and MLH to survive their probes. T19 ends with a source-count checkpoint. If fewer than 6 remain under the strict count, the implementer reports a blocker and the planner adds replacement credential-free sources before T20. The solid sources are scheduled first (T06, T12-T17).
- **D-27 Test infrastructure.**
  - pytest-asyncio in `asyncio_mode=auto`.
  - An autouse conftest guard makes any AF_INET/AF_INET6 socket connect raise, so an accidental live call fails loudly.
  - HTTP is faked with `httpx.MockTransport`. Telegram and Anthropic clients are simple fakes that record calls.
  - Tests stay flat in `tests/test_*.py`, because `.claude/hooks/run-tests.sh` checks `ls tests/test_*.py` (not recursive).
- **D-28 T01 step order.** The PostToolUse hook runs pytest whenever a `.py` file is written, falling back to system `python3`, which has no pytest. So T01 creates the venv and installs requirements before writing any test file. If `pip install` fails (no PyPI access), the implementer reports a blocker instead of improvising.
- **D-29 Eval runner lives at `src/evals/run.py`, with data in `evals/labeled.jsonl`.** It is the only code allowed to call the real Claude API, and only when run explicitly. Running it needs the user's API key, so the first eval result is a user action (PLAN section 3). Unit tests cover the runner with a fake client.
- **D-30 User actions are a separate PLAN section (section 3), not a checkbox task.** An unchecked task would stall the loop: the implementer always takes the first unchecked task, and the reviewer only runs once every task is checked. The orchestrator reports the list at the end, and README repeats it (T29). The reviewer must not file FIX- tasks for items only the user can do: secrets, `.env.example` contents, Telethon login, a real digest, a real eval run.
- **D-31 robots.txt on official API hosts.** HTML pages always obey robots.txt. A documented public API host (Greenhouse, Lever, Ashby, Algolia HN, api.hh.ru, the Kaggle API) may have a robots.txt aimed at crawlers that blanket-disallows paths the provider explicitly invites developers to call. The rule: check that host's robots once at implementation time. If the API path is allowed, keep the check on. If it is disallowed but the provider's API docs/terms explicitly invite programmatic use, the source may pass `check_robots=False` for that API host only, after recording the robots finding and the terms link here. Reason: dropping a core source, or bypassing robots silently, would each be worse than a documented, scoped exception.
- **D-32 ruff excludes `*.md`.** ruff 0.17 formats Python code blocks inside Markdown, so `ruff format --check .` flagged the illustrative snippets in PLAN.md. `extend-exclude` in pyproject.toml now includes `"*.md"`. Reason: docs are not source, and rewriting plan snippets adds noise.
- **D-33 PoliteClient T04 shape.** Constructor takes `contact` (builds the UA) plus injectable `sleep` and `monotonic`; `db`/`clock` are accepted but unused until T05 (robots/cache). `get_text`/`get_json` take only `params`/`headers` for now (`ttl_s`, `check_robots` arrive in T05). Retry backoff is 1, 2, 4s, or `Retry-After` capped at 60s; timeouts and transport errors are retried like 5xx. The per-host rate limit sleeps through the same injected `sleep`, so tests record it.
