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
- **D-34 PoliteClient T05 shape.** `get_text`/`get_json` gain `ttl_s` (None = no response cache) and `check_robots` (default True). Robots and the response cache are active only when both `db` and `clock` are given; without them the client is a plain polite fetcher (keeps T04 tests and ad-hoc use simple). robots.txt: cached 24h per host in `robots_cache`; 4xx -> allow all; 5xx/network failure -> deny that call and do not cache; parsed with `urllib.robotparser` using agent token `oppo-tg`. Cache hits are served before the robots check (no network). Only 200 bodies are cached; a 304 returns the cached body and refreshes `fetched_at`. Cache key is the URL including params.
- **D-35 T06 Greenhouse source, robots finding and kept boards (D-31).** Fetched `https://boards-api.greenhouse.io/robots.txt` once on 2026-11-06 with the honest UA: HTTP 200, `User-agent: *` / `Disallow: /embed/` only. `/v1/boards/...` is allowed, so `check_robots=True` stays (no bypass). Probed each candidate token once (`/v1/boards/{token}/jobs`, 2s apart): anthropic 674 jobs, databricks 921, stripe 768, janestreet 219, imc 162, jumptrading 93, drweng 165 (DRW), scaleai 167 all 200; `hudsonrivertrading` 404 (dropped; true token unknown). Kept in `config.toml`: anthropic, databricks, stripe, janestreet, imc, jumptrading, drweng, scaleai, with display names in `[sources.greenhouse.names]` (company falls back to the payload `company_name`). Fixture `tests/fixtures/greenhouse_jobs.json` is one real `?content=true` response from jumptrading, trimmed to 5 jobs (2 intern, 3 other), `data_compliance` removed, content cut to 6000 chars. Extras beyond PLAN: `application_deadline` maps to `deadline` when present; `remote` is True when location says remote, else None (unknown, not False); department names become `raw_tags` when present.
- **D-36 Robots/caching cannot be silently skipped in source wiring.** `src.http.build_client(db, clock, ...)` takes db and clock as required arguments (D-34) and is the only way production code should construct a client. `SourceContext.__post_init__` raises ValueError if its `http` lacks db or clock. Tests cover both and that a robots-disallowed URL is refused through `build_client`.
- **D-37 T07 filter details.** Keywords match case-insensitively on word boundaries: keywords of 3 chars or fewer (ml, ai) match whole words only, longer ones match as prefixes (intern -> internship). senior_only applies to JOB/INTERNSHIP titles only and is waived when the title contains "intern"; unpaid is skipped for HACKATHON/COMPETITION; spam also matches an "earn $N/day|week|hour" regex. stale = deadline before today (Asia/Almaty) or posted more than `max_age_days` ago. Fingerprint is normalized company + title; with an empty company the source is included to avoid collapsing unrelated postings. `mark_duplicates` keeps the earliest-seen row and only changes rows still `new`.
- **D-38 T08 prompt shape.** `build_system_blocks(examples)` takes `{"like": [...], "dislike": [...]}` (dicts with title, company, tags, as returned by `db.recent_feedback_examples`) and an optional `base_dir` for tests. Block 1 (profile.md + rubric.md, `cache_control` ephemeral) never changes with examples; block 2 (uncached) lists them. `prompt_version()` hashes profile.md and rubric.md (sha256, 12 hex) and accepts `base_dir`. The parser strips one code fence, uses `raw_decode` so trailing prose is an error, collapses whitespace in `reason`, and relies on the shared `ScoreResult` pydantic model (coerces "8.5", rejects score > 10 and bad dates).
- **D-39 T09 scorer and model check.** Confirmed with the claude-api skill (2026-11-06): `claude-haiku-5-5` is cheaper ($0.10/$0.50 per MTok) but rejects non-default sampling (`temperature=0` returns 400) and thinks by default, which would eat the `max_tokens=300` budget; `claude-haiku-4-5` ($1/$5) accepts `temperature=0`, has no thinking by default and is a supported model. D-12 stays: default `SCORING_MODEL=claude-haiku-4-5`; revisit Haiku 5.5 only with the temperature/effort params dropped and re-evals. `Scorer.score` is pure (returns `ScoreResult | None`, never raises) and `score_pending` writes the DB. `max_calls` is a per-Scorer API-call cap (retries count); a posting whose retry is cut off by the cap becomes `score_error`, postings not started stay `new`. Feedback examples (20 like, 20 dislike) are loaded once per `score_pending`. `src.pipeline.run.score_pending(conn, client, model, clock, max_calls)` is the wiring, with the client an `AsyncAnthropic`.
- **D-40 T10 formatting details.** `Item` (frozen dataclass in `src/delivery/format.py`, with `Item.from_row` for `db.undelivered_scored` rows; structured deadline wins over LLM deadline, D-14) is the type T11 uses. The URL line is HTML-escaped (`&` -> `&amp;`), and `safe_url` also rejects URLs with whitespace/control chars or over 1000 chars. Text is escaped with `quote=False` (only `&<>`). Over-length items are shrunk by truncating the raw reason (then re-escaping), never escaped text, so no entity or tag is cut; title (200), company (100), red flags (5 x 150) are clipped up front. `feedback_keyboard` raises ValueError if callback data exceeds 64 bytes.
- **D-41 T11 details.** Paused is `state.paused == "1"`. `send_digest` returns the number of item messages sent and records each delivery right after its own send, so a failed item is skipped (logged, not recorded) and retried next digest. `collect_all(conn, http, settings, cfg, clock)` takes the PoliteClient built by `build_client(db, clock)`; sources run concurrently, DB writes happen sequentially afterwards. `src.main.main(argv, environ)` catches `config.MissingConfig` via the module attribute (tests/test_config.py reloads `src.config`, which breaks class identity for a direct import), exits 2 listing missing vars. `collect` also scores pending postings (needed for the digest to have content).
- **D-42 T12 Lever source, robots finding and kept companies (D-31).** Fetched `https://api.lever.co/robots.txt` once on 2026-11-06 with the honest UA: HTTP 200, `User-agent: *` / `Allow: /` / `Crawl-delay: 1`. API path allowed, so `check_robots=True` stays (no bypass); the per-host rate limit already spaces calls. Probed candidates once each (2s apart): palantir 309 postings, spotify 86, weride 11 all 200; mistral and kaiko 404 (dropped). Kept in `config.toml`: palantir, spotify, weride, with display names in `[sources.lever.names]` (Lever has no company field in the payload; falls back to the slug). Fixture `tests/fixtures/lever_postings.json` is real data from one palantir response, trimmed to 5 postings (2 Internship, 2 Full-time, 1 remote Scholarship), descriptions cut to 1500 chars, HTML and list fields removed. Real commitment value is "Internship", not PLAN's "Intern", so INTERNSHIP matches `\bintern\w*\b` in commitment or title. `remote` is True for `workplaceType == remote` or "remote" in location, False for `onsite`, else None. Tags: department, team, commitment. external_id is `{slug}:{id}`. Source count checkpoint is deferred to T19.
- **D-43 T13 Ashby source, robots finding and kept boards (D-31).** Fetched `https://api.ashbyhq.com/robots.txt` once on 2026-11-06 with the honest UA: HTTP 401 `Unauthorized` (no robots file). Per D-34 a 4xx is treated as allow-all, so `check_robots=True` stays (no bypass). Probed candidates once each (2s apart, `includeCompensation=false`): openai 200 (822 jobs, 13.7 MB), cohere 200 (119 jobs, 2 MB), perplexity 200 (137, 1.2 MB), ramp 200 (170, 2.8 MB). Kept in `config.toml`: cohere, perplexity, ramp (display names in `[sources.ashby.names]`). openai is left out for now because its response is 13.7 MB per fetch (stored in the response cache); add it to `boards` if wanted. Fixture `tests/fixtures/ashby_board.json` is real data from the one cohere response, trimmed to 5 jobs (2 Intern, 1 remote FullTime, 1 OnSite FullTime, 1 Contract), descriptions cut. Real `employmentType` is `Intern`; INTERNSHIP matches `\bintern(?:ship)?s?\b` in employmentType or title (not `intern\w*`, which wrongly matched "Internal Applications"; Lever's T12 regex has the same flaw, not fixed here). `remote` is the `isRemote` bool as given (note: it is true for many Hybrid roles, e.g. OpenAI; accepted per PLAN), None if absent. Entries with `isListed: false` are skipped. Tags: department, team, employmentType. external_id is `{board}:{id}`.
- **D-44 T14 Hacker News source, robots finding and fixtures (D-31).** Fetched `https://hn.algolia.com/robots.txt` once on 2026-11-06 with the honest UA: HTTP 404 (no robots file), treated as allow-all per D-34, so `check_robots=True` stays (no bypass). Captured one real `search_by_date?tags=story,author_whoishiring&hitsPerPage=10` response and one real `items/50338728` response (Nov 2026 thread, 201 top-level comments, 350 KB), each with a single request 2s apart. `tests/fixtures/hn_search.json` keeps 4 of the 10 hits (two Who is hiring, two Who wants to be hired); `hn_item.json` keeps 7 real top-level comments (text cut to 1500 chars, authors removed, replies trimmed to one short child) plus one hand-written deleted comment (`text: null`, id 50339999), since the real thread had none. The story is the newest hit whose title matches "who is hiring". The posting header is the first line of the comment; HN separates it from the body with a bare `<p>`, which `html_to_text` would glue on, so `<p>` is pre-turned into `<br><p>`. Company is the first pipe segment (so headers that lead with a location give a location as company, accepted per PLAN); title is the first later segment containing a role keyword, else the first 80 chars of the header; location is the first remaining segment that is not a work-type, remote, URL or salary segment. `remote` is True when "remote" is in the header, else None. INTERNSHIP uses `\bintern(?:ship)?s?\b` on the header only (not the body, which would mark most long posts). external_id is the comment id; no tags. Cache TTL 1h for both calls.
- **D-45 T15 RSS/Atom source, robots findings and seeded feeds (D-31).** On 2026-11-06 with the honest UA, one request each: `https://hnrss.org/robots.txt` HTTP 200 `User-agent: *` / `Disallow:` (allow all); `https://www.nsf.gov/robots.txt` HTTP 200, disallows funding search/`/funding/opps`, careers, news/releases etc. but not `/rss/`. Both keep `check_robots=True` (no bypass). Seeded feeds in `config.toml` (`[[sources.rss.feeds]]`): `https://hnrss.org/jobs` (HN Jobs, kind `job`, the YC job posts; hnrss is a third-party mirror that asks for polite use, we fetch hourly at most via the 1h cache) and `https://www.nsf.gov/rss/rss_www_funding-upcoming/rss.xml` (NSF upcoming due dates, kind `research`; the older `/rss/rss_www_funding_upcoming.xml` URL 301-redirects here, so the final URL is configured). Only two feeds were seeded: devpost `hackathons.atom` returned 406, Nature Careers had no feed (404), and no other fellowship feed with a permissive robots was found quickly; add more under `[[sources.rss.feeds]]` as found. Fixtures: `rss_sample.xml` is the real hnrss response trimmed to 4 items with `dc:creator` (usernames) removed; `rss_nsf.xml` is the real NSF response unchanged (4 items, no per-item date); `atom_sample.xml` is HAND-WRITTEN from the Atom spec (no real Atom feed was captured), covering published/updated fallback, author, HTML summary and an entry without a link. Behavior: feedparser only parses text fetched by PoliteClient (never fetches). Entries without title or link are skipped; external_id is `{feed name}:{entry id or link}`; company is the entry author else the feed name; kind comes from config (unknown value -> OTHER), and a `job` feed entry whose title matches `\bintern(?:ship)?s?\b` becomes INTERNSHIP. A "Deadline Date: Month D, YYYY" line in the description (NSF format) fills `deadline`. Garbage or empty XML yields no entries, HTTP errors skip that feed only. Cache TTL 1h.
- **D-46 T16 page watcher, robots findings and seeded pages (D-31).** HTML pages always obey robots.txt (no bypass; `check_robots=True`). On 2026-11-06 with the honest UA, one request per robots.txt and per candidate page (2s apart), checked with `urllib.robotparser` for agent `oppo-tg`: imc.com robots `Allow: /` (page `/eu/careers/` 200; the PLAN's early-careers URLs 404); optiver.com `Disallow: /externalContentView/` only (`/join-us/jobs/` 200); janestreet.com disallows only specific puzzle/apply paths (`/join-jane-street/internships/` 200 and allowed); home.cern robots allows `/summer-student-programme` (200); summer.epfl.ch 200 (www.epfl.ch has no robots, 404 on guessed paths); inf.ethz.ch robots returned an HTML 404 page (4xx -> allow-all per D-34), SSRF page `/studies/summer-research-fellowship.html` 200; oist.jp robots allows the page, `/admissions/research-internship/apply-research-internship` 200 but the body is only ~3 KB (looks like a stub for non-browser agents; kept, may rarely yield text). Citadel (citadel.com) answers 403 to our UA on every careers page, so it is NOT seeded (robots itself allows; we do not evade the block). Seeded in `config.toml` (`[[sources.pagewatch.pages]]`): IMC, Optiver, Jane Street internships, CERN summer student, Summer@EPFL, ETH SSRF, OIST. The old `urls = []` key is replaced by `pages`. Fixtures `page_v1.html`/`page_v2.html` are HAND-WRITTEN, with body text taken from the real Jane Street internships page (plus script/style/nav/footer wrappers and a deadline block that v2 changes); no full real page was stored (60-700 KB). Behavior: text from `css_selector`, else `<main>`, else `<body>` (unknown selector falls back); script/style/noscript/template removed; hash is sha256 of whitespace-normalized text; first sight -> title `{name}`, change -> `{name}: page updated`; description is the first 3000 chars; kind from config (`job` + intern word -> INTERNSHIP, `\bintern(?:ship)?s?\b`). The snapshot row is written during `fetch`, before the posting reaches the DB, so a crash between the two would drop that one update (accepted; sources have no post-persist hook). Pages that fail, are robots-disallowed or have no text are skipped without touching the snapshot. Cache TTL 1h. Big noisy pages (IMC, Optiver job lists) may fire on any listing change; accepted as signal.
- **D-47 T17 hh.kz source, robots/terms finding and fixture (D-31, D-07).** On 2026-11-06 with the honest UA, one request each (2s apart): `https://api.hh.ru/robots.txt` HTTP 404 (JSON not_found, no robots file; allow-all per D-34, so `check_robots=True` stays, no bypass). `GET /vacancies?area=40&text=data+scientist&per_page=50&period=14` with `User-Agent` and `HH-User-Agent` set to our UA and no token returned HTTP 403 `{"errors":[{"type":"forbidden"}]}` (DDoS-Guard in front). So anonymous access is refused; we do not evade it. Per D-07 the source reports disabled with reason "needs HH_ACCESS_TOKEN" (`HHSource.disabled_reason`, used by `enabled()`; /sources should show it) when the optional token is missing, and is otherwise fully implemented (`Authorization: Bearer`, `HH-User-Agent` = our UA). API docs: https://github.com/hhru/api (terms: https://hh.ru/article/1175). Fixture `tests/fixtures/hh_vacancies.json` is HAND-WRITTEN from the documented `/vacancies` shape (no real response obtainable without a token): 5 items, Cyrillic titles, `<highlighttext>` snippets, one remote, two intern-like by title, one by `noExperience`. Mapping: external_id = vacancy id (same vacancy across queries is returned once); INTERNSHIP if `experience.id == noExperience` or title matches `\bintern(?:ship)?s?\b` (Russian "стажер/стажировка" is not matched by that regex, only via noExperience; accepted); `remote` True for schedule `remote`, False for any other schedule, None if absent; description is requirement + responsibility snippets via html_to_text; `area` and `period` configurable in `[sources.hh]`. Cache TTL 1h.
- **D-48 T18 Devpost hackathons source, robots/terms finding and fixture (D-31).** On 2026-11-06 with the honest UA, one request each: `https://devpost.com/robots.txt` HTTP 200, `User-agent: *` / `Disallow:` (allow all; only BLEXBot, Omgilibot, Omgili, ImagesiftBot, Bytespider are banned), so `check_robots=True` stays (no bypass). `GET /api/hackathons?status[]=upcoming&status[]=open&page=1` with `Accept: application/json` returned HTTP 200 JSON (9 hackathons per page, `meta.total_count` 191). The Devpost ToS page was not read or parsed; this is an unauthenticated public JSON listing, fetched at most 3 pages per run with the 1h cache and per-host rate limit. (The `hackathons.atom` feed returned 406 in T15, but the JSON API works, so Devpost is kept.) Fixture `tests/fixtures/devpost_hackathons.json` is the real page-1 response trimmed to 4 hackathons plus 2 HAND-WRITTEN entries (a cross-year in-person "Dec 15, 2026 - Jan 20, 2027" in Berlin, and a "Dec 28 - 30, 2026" online one), because the live data had only Online events and no cross-year ranges. Mapping: external_id = id; company = `organization_name`; deadline = end date of `submission_period_dates` (formats: "Oct 01 - Nov 30, 2026", "Dec 15, 2026 - Jan 20, 2027", "Dec 28 - 30, 2026"; unparseable -> None); `remote` True when location is "Online", else None; raw_tags = theme names plus "prize $X" when the (HTML-stripped) prize is non-zero; kind HACKATHON (so no intern matching needed). Pagination stops at 3 pages, an empty page, or a page with no new ids. Items without title or url are skipped; any error or bad JSON yields what was collected so far (usually `[]`). Config key `[sources.devpost] enabled` already existed.
- **D-49 T19 MLH events source, robots/terms finding and fixture (D-31).** On 2026-11-06 with the honest UA, one request each (2s apart): `https://mlh.io/robots.txt` HTTP 200, `User-agent: *` disallows only `/account/ /tools/ /_/ /v4/ /auth/ /admin/ /graphql` and a few invite paths, then `Allow: /`; `/seasons/...` is allowed, so `check_robots=True` stays (no bypass). The terms page (`https://mlh.io/terms`) was fetched and read this time: it prohibits using the Site "commercially", reverse engineering, interfering with or damaging the Site, and unauthorized access; it has no clause against automated access, scraping or crawling. This is a personal, non-commercial bot making one cached (1h) request per run, so MLH is kept. `GET https://mlh.io/seasons/2027/events` returned HTTP 200 (378 KB server-rendered HTML, 48 event cards, schema.org Event microdata). Fixture `tests/fixtures/mlh_events.html` is 5 REAL cards from that response (Digital; In-Person+Hardware; In-Person+Diversity; an ended one; one more In-Person) with svg/img/image meta and class attributes stripped, wrapped in a minimal page. Parsing: cards are `a[itemtype=schema.org/Event]`; name = `h4`; start/end from `startDate`/`endDate` metas (UTC date); location = addressLocality, Region, Country metas; badge `span[title]` values (Digital, In-Person, Hardware, Diversity, High school) become raw_tags; remote True for "Digital", False for "In-Person", else None. Deadline = event start date (PLAN); events whose end date is before today (Asia/Almaty) are skipped because the season page also lists past events (so a past-dated item never reaches the digest). external_id and url are the event URL with the `utm_*` query stripped (many events link off-site, e.g. luma.com). Company is "Major League Hacking". Season URL: month >= 7 -> year+1, from the injected clock in the configured timezone; tested for 2026-11-06 and 2027-03-01 (both `/seasons/2027/events`). No cards -> `[]` plus a warning log. (FIX-10 note: mlh.io 302-redirects to www.mlh.com, so MLH's canonical host is www.mlh.com; the client now robots-checks and throttles that host, see D-80.)
- **D-50 Devpost dropped (terms forbid automated access); supersedes D-48's keep decision.** Read the Devpost Terms of Service (`https://info.devpost.com/legal/devpost/terms-of-service`, reached via `info.devpost.com/terms`; `devpost.com/site/terms-of-service` is a 404) on 2026-11-06. Section 4 lists as prohibited: "Uses manual or automated software, devices, scripts robots, or other means or processes to access, 'scrape,' 'crawl' or 'spider' the Site, User Content ..., or any related data or information". The `/api/hackathons` JSON endpoint is undocumented, not offered as a public API, and is part of "the Site", so programmatic access is not permitted even though robots.txt allows it. Action: `[sources.devpost] enabled = false` in config.toml, SPEC.md Sources marked dropped. The module `src/sources/devpost.py` and its tests stay in the repo (inert, disabled by config) in case Devpost grants permission; do not re-enable without it. Hackathon coverage now comes from MLH (and Kaggle for competitions, T25).
- **D-51 T19 source-count checkpoint.** Sources registered and fixture-tested after T06 and T12-T19 that work with NO credentials and are enabled: (1) ATS boards (Greenhouse, Lever, Ashby counted as one per PLAN), (2) Hacker News Who is hiring, (3) RSS/Atom feeds, (4) page watcher, (5) MLH events = 5. Not counted: hh.kz (needs `HH_ACCESS_TOKEN`, D-47; anonymous gets 403), Devpost (dropped, D-50), Kaggle (T25, needs credentials), Telegram channels (T26, needs Telethon credentials). Only T27 (YC jobs) could add a credential-free source: probed `https://www.ycombinator.com/robots.txt` (HTTP 200, `Allow: /`, only `/verify/*`, `/library?*`, `/companies?*` disallowed) and `/jobs/role/software-engineer` (HTTP 200, Inertia `data-page` JSON embedded), so it looks feasible, but its terms are unread and D-24 notes Work at a Startup terms disallow automated access. So the count is 5, reaching 6 only if T27 passes its terms check: BLOCKER REPORTED, the planner should add a replacement credential-free source (or accept the hh.kz token / T27 path) before T20 per D-26.

## 2026-11-06 — Re-plan after the T19 source-count blocker (planner)

- **D-52 Count rule for SPEC's "At least 6 sources working". Supersedes D-26's lenient per-module count and D-51's tally.**
  - **Interpretation.** SPEC does not define "source". This rule is an interpretation, recorded here; it does not change SPEC.
  - **Unit counted: a source family.** Each module in `src/sources/` is its own family, except that Greenhouse, Lever and Ashby form one family, `ats`. They are separate modules with separate vendor APIs and separate failure modes. But they read the same kind of listing (employer ATS job boards) from one company watchlist and differ only in vendor, so counting them three times would inflate coverage. Every other family is a distinct discovery channel. RSS feeds, the page watcher, HN, MLH, Jobicy and EURAXESS each find opportunities a different way.
  - **The honest counter-argument.** SPEC names Greenhouse, Lever and Ashby individually, the same way it names Devpost, MLH and Kaggle. So a per-module count is also defensible. With the T19a/T19b additions it gives 9 credential-free modules. We do NOT rely on it; the family count is the enforced, stricter rule.
  - **Not used: grouping by SPEC bullet.** That grouping would also merge RSS with the page watcher and fold EURAXESS into the same "Fellowship and research-program pages" bullet. It is not a sound basis, because one bullet covers several independent channels.
  - **Which sources count.** A family counts if at least one of its modules meets all of these:
    - registered;
    - enabled in the committed `config.toml`;
    - `enabled()` is true with ONLY the three required env vars set;
    - declares no `credentials`;
    - has a four-case fixture test `tests/test_source_<name>.py`;
    - has a DECISIONS entry with its robots finding (and, for new sources, its terms finding).
  - **Credential-gated sources never count.** These are hh (`HH_ACCESS_TOKEN`), kaggle (`KAGGLE_USERNAME`/`KAGGLE_KEY`) and telegram_channels (`TG_API_ID`/`TG_API_HASH`). They are listed as "unlocks with credentials" and do not count even when the user sets credentials, because agents cannot verify them live and the DoD must hold on a fresh install.
  - **Dropped or config-disabled sources never count.** These are devpost (D-50) and yc (D-54).
  - **Threshold:** `MIN_SOURCE_FAMILIES = 6`.
  - **Verification.**
    - Offline: `tests/test_source_count.py` (T19d), which builds `count_report` from the repo config with only the three required env vars.
    - Live: `src.main probe` (T19e, D-56). A family counts live only if a run returned at least 1 item.
  - **Count today:** 5 (ats, hn, rss, pagewatch, mlh).
  - **Expected after T19a/T19b:** 7 (ats, hn, rss, pagewatch, mlh, jobicy, euraxess), so one family can fail without missing the DoD. T19c (Himalayas) is a conditional fallback in case both new sources drop.
- **D-53 New credential-free sources: probes and choices.** The planner made one request per robots.txt or page on 2026-11-06, with the honest UA and 2s between requests. Results:
  - **Jobicy remote jobs API: chosen (T19a).**
    - robots.txt: 200, `/api/` not disallowed, `Content-Signal: ... ai-input=yes`.
    - The API docs page (`https://jobicy.com/jobs-rss-feed`) says the public API is usable "without a separate agreement" and that AI models may be used to analyze listings.
    - Conditions: automated passes no more than once per hour; credit Jobicy with a direct link; free-tier apply links must go to the Jobicy page.
    - A test call to `/api/v2/remote-jobs?count=5&industry=data-science` returned 200 JSON (its `friendlyNotice` repeats the credit and link rule).
    - How we comply: 1h cache TTL, and `url` is the Jobicy job page, which is our visible credit.
  - **EURAXESS research jobs: chosen (T19b).**
    - robots.txt allows `/jobs/search` with facet query strings.
    - The search page is server-rendered: 10 cards per page with `/jobs/<id>` links.
    - Its footer legal notice (`https://commission.europa.eu/legal-notice_en`) licenses content under CC BY 4.0 and has no clause on automated access. The implementer must confirm there is no EURAXESS-specific terms page and should prefer an official feed if one exists.
    - It covers SPEC goal 4 (research and PhD-track positions).
  - **Himalayas API: conditional fallback only (T19c).**
    - robots.txt allows the site.
    - Terms clause 113 allows "personal and commercial integrations" of the API.
    - But clauses 30 and 93 ban bots and scraping outside the API, clause 30A restricts Personal Agents to non-bulk use, the terms never mention LLM processing, and they bar passing jobs to third parties.
    - That is borderline, so it runs only if the count is below 6, with explicit drop conditions.
  - **Rejected candidates:**
    - Remotive: robots.txt has `Disallow: /api/*`.
    - DrivenData: its Terms of Use (`/termsofuse/`) forbid "any robot, spider or other automatic device ... to access the Website for any purpose".
    - Astana Hub (`astanahub.com`): answers 403 to our honest UA. We do not evade it, and its robots.txt also disallows `/api/`.
    - enbek.kz: robots.txt disallows the vacancies (`/*/вакансии/*`) and search paths.
    - Codeforces: `api/contest.list` returned 200, but `/apiHelp` returned 403 to our UA, so the API terms could not be read. Under the new-source rule an unread terms page means no.
    - AIcrowd: not pursued; low priority, and robots.txt blocks AI crawlers.
    - Arbeitnow: robots.txt allows everything, but it is mostly German on-site jobs, which is low relevance.
- **D-54 YC dropped (terms forbid scraping). Supersedes D-24's probe plan.**
  - The YC Terms of Use (`https://www.ycombinator.com/legal`, "Site Content, Software and Trademarks") say: "you will not engage in or use any data mining, robots, scraping or similar data gathering or extraction methods".
  - robots.txt allows `/jobs` (D-51), but the terms forbid it, so the public jobs pages are not fetched.
  - T27 is rewritten as a no-code task: config flag off and a SPEC dropped note. YC startups stay covered through HN Who is hiring, the hnrss "HN Jobs" feed and the Ashby/Lever boards.
- **D-55 SPEC edits for sources without a matching bullet. Extends D-23.**
  - The add-source skill (step 6) requires updating SPEC's Sources section. For a new source that fits no existing bullet, the agent may append ONE new bullet at the end of the Sources list, in the form `- <Category>: <Source>. (implemented: <name> in src/sources/<name>.py; added by agent, D-55)`.
  - Jobicy gets such a bullet ("Remote job board API: Jobicy."). EURAXESS gets its note appended to the existing "Fellowship and research-program pages" bullet. Himalayas, if ever implemented, gets its note appended to the Jobicy bullet.
  - No existing SPEC text, requirement or [EDIT] item is changed.
- **D-56 `probe` CLI for live verification of "working".**
  - Fixture tests prove that parsing works, not that a source works live. `python -m src.main probe` runs every enabled source once against the real network. It needs no secrets, never calls Telegram or Claude, and uses a separate DB (`data/probe.db`), so pagewatch snapshots and the cache never affect the real DB. It prints per-source status and the counted-family total, and exits 1 below 6.
  - It is a manual command: never run in tests (tests use fakes and MockTransport).
  - The implementer runs it once in T19e and records the output here. The user re-runs it after setup (PLAN section 3).
  - It is added to the section 0 command table, the `--help` assertion in T19e/T29 and the README command test.
- **D-57 Kazakhstan coverage gap.**
  - No credential-free Kazakhstan source survived the checks:
    - hh.kz needs a token (D-47);
    - Astana Hub returns 403 to our honest UA;
    - enbek.kz's robots.txt disallows its vacancy paths;
    - Telegram channels need Telethon credentials.
  - PLAN section 3 therefore marks `HH_ACCESS_TOKEN` as RECOMMENDED, not optional-if-required.
  - The user can also add Kazakh company career pages to `[[sources.pagewatch.pages]]` after a robots check.
  - Kazakhstan relevance in scoring is unaffected: the profile and rubric still weight Astana/Almaty highly for postings from any source.
- **D-58 T19a Jobicy remote jobs source, robots/terms finding and fixture (D-31, D-53).** On 2026-11-06 with the honest UA, one request each (2s apart). `https://jobicy.com/robots.txt` HTTP 200: `User-agent: *` disallows only search/query-param patterns (`/*?s=`, `/*?q=`, `/*?r=`, `/*?w=...`, `/*?tool=`, `/job-listings/`, `/search/`, `/go/` etc.); `/api/jobs?count=&industry=&tag=` matches none, so `check_robots=True` stays (no bypass). Terms read at `https://jobicy.com/jobs-rss-feed` (API docs, updated Oct 19 2026, "Fair use"): "You may use AI models to analyze job data and generate derived outputs ... matching scores", "can use the public API without a separate agreement", "may store, cache, process, and retain Jobicy API data", automated polling "must not exceed once per hour", "Apply links provided by our free API must continue to direct users to the original job pages on Jobicy", and "Clearly credit Jobicy as the original source". No clause forbids automated access. Kept. Endpoint is the current `GET https://jobicy.com/api/jobs` (not legacy v2), `count=100`, one page only (no cursor), `industry` slugs verified via `?get=industries`: `data-science` and `engineering` seeded. The API returns jobs from the last 7 days. Compliance: 1h cache TTL (one pass per hour at most); `url` is the Jobicy job page (the visible credit and apply link), never the ATS link (we send no API key). Fixture `tests/fixtures/jobicy_jobs.json` is 4 REAL data-science jobs (logos removed, descriptions cut to 700 chars) plus 1 HAND-WRITTEN internship entry (id 999001), since the live data had no intern roles. Mapping: external_id = id, company = companyName, location = jobGeo, remote always True, description = html_to_text(jobDescription or jobExcerpt), posted_at = pubDate (UTC), raw_tags = industries + types + level, INTERNSHIP if `\bintern(?:ship)?s?\b` matches title or jobType else JOB. Same id across queries is emitted once; one failing query does not drop others. SPEC got the D-55 appended bullet.
- **D-59 T19b EURAXESS dropped (WAF refuses our UA on every query-string request, so no filtered or paged listing is reachable).** On 2026-11-06 with the honest UA, one request each, 2s apart. `https://euraxess.ec.europa.eu/robots.txt` HTTP 200: contains `Disallow: /*?` ("Disallow any search with parameter"), `Disallow: */api/*`, `*/rest/*`, and then `Allow: /jobs`, `Allow: /jobs/search?page=`. Under RFC 9309 longest-match the facet URLs are arguably allowed (`Allow: /jobs` is longer than `/*?`), but the file's stated intent is to disallow parameterised searches except `?page=`; Python's `urllib.robotparser` ignores wildcards, so it would not catch this either. The legal notice (`https://commission.europa.eu/legal-notice_en`, linked from the footer) was read: content is CC BY 4.0 with attribution, no clause on automated access, and no EURAXESS-specific terms page is linked from the search page. No RSS/Atom feed or export is advertised (no `<link rel=alternate>`, no feed links). Plain `GET /jobs/search` returned HTTP 200 (server-rendered cards, 10 per page; facet ids seen: Mathematics 298, Physics 345, Computer science 78, Statistics 311; profiles R1 447, R2 448, R3 449). But `GET /jobs/search?page=1` (explicitly allowed by robots), `?f[0]=job_research_field:298` and a field+profile combination all returned HTTP 403 with a "Sorry - <id>" block page, while the plain URL kept returning 200. So the site's WAF blocks our client for every parameterised request: only the first, unfiltered page of newest jobs is reachable, which is not useful for matching (SPEC goal 4) and cannot be filtered or paged. We do not evade the block (no UA change, no headers). Action: `[sources.euraxess] enabled = false` in config.toml, no module, SPEC bullet notes the drop, task marked done. Revisit only if EURAXESS offers a feed, API or an access arrangement. Count effect (D-52): euraxess does not count, so the credential-free families are 6 (ats, hn, rss, pagewatch, mlh, jobicy) = MIN_SOURCE_FAMILIES; per its trigger check T19c (Himalayas) is not needed, though there is now no spare family.
- **D-60 T19c not needed: count = 6.** Verified by inspecting `src/sources/__init__.py` (registry), `config.toml` and D-52/D-59: credential-free enabled families are ats (greenhouse/lever/ashby), hn, rss, pagewatch, mlh, jobicy = 6 = MIN_SOURCE_FAMILIES (devpost and euraxess disabled, hh needs a token and does not count). Himalayas not probed or implemented; code untouched. There is ZERO spare margin: any one source failing leaves a count of 5, so the `HH_ACCESS_TOKEN` (hh.kz) or another new source is the buffer.
- **D-61 T19d source count table (computed by `count_report` with only the three required env vars, repo `config.toml`).** Counted families (6 = MIN_SOURCE_FAMILIES, 8 modules): ats = ashby, greenhouse, lever; hn = hn; rss = rss; pagewatch = pagewatch; mlh = mlh; jobicy = jobicy. Credential-gated (never counted): hh needs HH_ACCESS_TOKEN (kaggle needs KAGGLE_USERNAME + KAGGLE_KEY and telegram_channels needs TG_API_ID + TG_API_HASH; neither has a module yet, so they appear once implemented). Disabled: devpost ("disabled in config.toml", D-50); euraxess has no module (D-59); yc has no module and is `enabled = false` (D-54). Note: config.toml already had a `[sources.yc] enabled = true` table, which duplicated the new one and broke TOML parsing, so it was removed and replaced by the disabled one. Zero margin remains (D-60).
- **D-62 T19e live probe result (2026-11-06, `.venv/bin/python -m src.main probe`, run once, honest UA, fresh `data/probe.db`, exit code 0).** `Settings.from_env(environ, require_secrets=False)` is used only by `probe`; the default still raises `MissingConfig`. Output:
  ```
  family  name  status  ok  n_items
  ats  ashby  counted  yes  426
  devpost  devpost  disabled: disabled in config.toml  -  -
  ats  greenhouse  counted  yes  3174
  hh  hh  credentials: HH_ACCESS_TOKEN  -  -
  hn  hn  counted  yes  201
  jobicy  jobicy  counted  yes  200
  ats  lever  counted  yes  406
  mlh  mlh  counted  yes  24
  pagewatch  pagewatch  counted  yes  7
  rss  rss  counted  yes  24
  Counted families with items: 6 (need 6)
  ```
  All six counted families (ats, hn, jobicy, mlh, pagewatch, rss) returned items. Margin is still zero (D-60). Note: mlh.io redirects (302) to www.mlh.com and the request followed it. pagewatch returned 7 items (snapshot-based, first run), so it is the thinnest family.
- **D-63 T20 bot details.** Whitelist is an outer middleware on `dp.update` that extracts the chat id from the Update (message kinds, or callback_query.message); updates with no chat id (or a different one) are dropped silently, fail closed. `build_dispatcher` passes `conn`, `settings`, `cfg`, `clock` as workflow data. Feedback answers a short toast ("Liked", "... removed"), and `TelegramBadRequest` from `edit_reply_markup` (e.g. not modified) is logged, not raised. Whitelisted-callback end-to-end is not tested through the dispatcher because `cb.answer` would hit the Bot API; handler functions are tested directly.

- **D-64 T21 feedback injection.** Wiring already existed from T09 (D-39: `Scorer._examples` loads 20 like and 20 dislike active examples, newest first, once per `score_pending`, passed to `build_system_blocks`). T21 only adds `tests/test_feedback_injection.py` covering the acceptance. `profile.md`, `rubric.md` and the static block are untouched, so `prompt_version` is unchanged and no evals are needed.
- **D-65 T22 bot commands.** Handlers are plain async functions (`cmd_*`) taking a message with `.answer`, registered in `build_dispatcher` via `Command(...)`; every reply is sent with `parse_mode="HTML"`. `/top` = top 10 by score among rows with `scores.scored_at` in the last 14 days (delivered or not), one message plus keyboard each. `/saved` and `/applied` are one message of up to 20 numbered `title — company` plus link lines. `/sources` uses `count_report` (status counted / `credentials: <vars>` / `disabled: <reason>`), family via `source_family`, last run from `source_runs`, and ends with `Credential-free source families enabled: N (need 6)` (PLAN text, not the shorter wording). `/pause` toggles (second `/pause` unpauses, replying "Resumed."); `/resume` always unpauses. The `/sources` fixture test uses the repo `config.toml`, so it must be updated when sources or config change.
- **D-66 T23 instant alerts.** `select_alerts(conn, cfg, clock, tz="Asia/Almaty")` reuses `undelivered_scored`, so anything alerted or digested is excluded by the `deliveries` primary key. Deadline window is today through today + `alert_deadline_days` inclusive (today counts; past deadlines never alert), evaluated in the configured timezone from the injected clock. `send_alerts` returns 0 when paused, sends one `<b>ALERT</b>` message per item with the feedback keyboard, and records each delivery (kind `alert`) right after its own send; a failed send is logged and retried next run. In `collect`/`once` it runs right after scoring (before the digest), so `once` can alert and then digest the rest.
- **D-67 T24 scheduler and run mode.** `build_scheduler(SchedulerDeps)` takes injected async callables (collect, score, alerts, digest, prune) plus clock/timezone/digest time; real wiring lives in `main._serve`. `collect_and_score` is an interval job (every `COLLECT_CRON_HOURS`, first run at start) running collect, score, alerts in order, each step isolated so a failing step does not skip the later ones; every job is wrapped by `safe_job` (logs, never raises) and has `coalesce=True`, `max_instances=1` (prevents overlap), `misfire_grace_time=3600`. `prune` runs daily 04:00 local: `db.prune` deletes `http_cache`/`robots_cache` rows older than 7 days and filtered/duplicate postings first seen over 60 days ago that have no score, delivery or reaction. `run_until_stopped` starts the scheduler and polling, waits for a stop event (SIGINT/SIGTERM handlers set it), then shuts the scheduler down and cancels polling; if polling dies it is logged and the scheduler keeps running. Polling uses `handle_signals=False` so our handlers own shutdown.
- **D-68 T25 Kaggle competitions dropped (terms unreadable; D-31 new-source rule), no module written.** On 2026-11-06 with the honest UA, one request each, 2s apart: `https://www.kaggle.com/robots.txt` returned HTTP 404 (an HTML not-found page, no robots file; allow-all per D-34, so `check_robots=True` would have stayed). `https://www.kaggle.com/terms` (also via a web.archive.org copy) returns HTTP 200 but only a ~6 KB JavaScript shell with no terms text, and WebFetch got only the page title, so the terms of use and API terms could not be read. The kaggle-api README (https://github.com/Kaggle/kaggle-api) calls the CLI "the official CLI to interact with Kaggle" but does not state API terms. The PLAN rule says a new source whose terms page cannot be read is not implemented: no fetching code was written, `[sources.kaggle] enabled = false` in config.toml, SPEC.md Sources bullet notes the drop, T25 marked done. No credentials exist here, so the authenticated API was not called and no fixture was written (it would have been hand-written from the documented competitions/list shape, as in D-47). Reconsider if the terms become readable and permit programmatic API use with a personal key: then add `src/sources/kaggle.py` with `credentials = ("KAGGLE_USERNAME", "KAGGLE_KEY")`, never counted (D-52). Source count unchanged (6 families, D-61); `/sources` fixture needs no change because there is no kaggle module.
- **D-69 T26 Telegram channels source (Telethon, credential-gated).** `credentials = ("TG_API_ID", "TG_API_HASH")`, so it is listed under `credential_gated` and never counted (D-52); count stays 6 (D-61). Disabled with a reason (shown in `/sources`) when config disabled, creds missing, no session file at `TG_SESSION_PATH` (or `<path>.session`), or no channels (D-07). Client is injected via `client_factory(settings)` (default imports telethon lazily; tests use a fake with `connect`, `is_user_authorized`, `iter_messages`, `disconnect`; no network, no session file read). Channels come only from `[sources.telegram_channels].channels` (handles, `@x` or t.me links; invalid or invite links ignored); the seeded list is empty because the user supplies them. Last-seen id per channel lives in `state` (`telegram_channels:last_id:<channel>`) and is written only after a channel is read fully; reads use `iter_messages(limit=100, min_id=last)`. `FloodWaitError` stops the run (log, keep postings from channels already finished); client `flood_sleep_threshold=60` lets Telethon sleep through short waits itself; 1s pause between channels. Skipped: empty text, ad keyword messages (`#реклама`, `на правах рекламы`, `промокод`, `sponsored`, ...) whether forwarded or not. Text is stripped of control characters and treated as inert data (it only reaches the scorer in the delimited posting block). Kind is INTERNSHIP on intern/стажиров keywords in the first 300 chars, else JOB. `src/tools/telethon_login.py` is an interactive one-time login reading env vars only; `*.session` is already gitignored. The `/sources` fixture gained the telegram_channels line.
- **D-70 T28 eval set and runner.** `evals/labeled.jsonl` has 23 synthetic postings (quant/ML internships, research programs, Kazakh startup and hackathons, plus negatives: senior-only, unpaid, spam, expired, irrelevant roles) with `expected_min`/`expected_max`/`note`. Run manually with `ANTHROPIC_API_KEY` exported: `.venv/bin/python -m src.evals.run [--threshold 0.8] [--limit N]`; exit 2 without the key, 1 below threshold (default pass rate 0.8), else 0. Hard filters and the title relevance prefilter run first; a filtered item counts as score 0 (passes only if `expected_min == 0`). Time is pinned to 2026-11-06 (FixedClock) so deadlines in the set do not drift, and feedback examples are empty so runs are comparable across prompt versions. Mean abs error is distance to the expected range (0 inside; unscored counts 10). Output `evals/last_run.json` (gitignored) carries `prompt_version`. The labeled set was not yet run against the real API (no key here): per the scoring-prompt skill, run it and record the result here before relying on the thresholds or after any prompt/profile/rubric change.
- **D-71 T29 README and e2e.** README states plainly under "Verification status" that the bot has never run against real Telegram/Claude, evals never ran (D-70), and hh/telegram_channels never ran live. The `.env.example` block in README keeps the PLAN section 3 lines including the unused `KAGGLE_*` (they remain in `ENV_VARS`, so the tested env table lists them as unused, D-68); removing them from `ENV_VARS` was out of scope. Telethon login and evals read the process environment (not `.env`), so the README tells the user to `set -a; . ./.env; set +a` first. `tests/test_e2e_all_sources.py` runs the repo `config.toml` (all sources, MockTransport routed by host to existing fixtures, pagewatch pages get `page_v1.html`) through collect, fake scorer, alerts and digest to a fake bot; hh must not be called.

- **D-72 FIX-01 probe is repeatable (D-56 addendum).** `main._probe` calls `reset_probe_state(conn)` (in `src/pipeline/probe.py`) before running: it deletes only `page_snapshots` and `source_runs` in the probe DB. `http_cache` and `robots_cache` are kept so the 1h Jobicy TTL (D-58) and 24h robots TTL (D-08) still hold across runs; with snapshots cleared pagewatch treats the cached body as new and emits items, so a re-run no longer drops to 5/6. The real `DB_PATH` is never touched. README Count rule and Troubleshooting no longer claim `HH_ACCESS_TOKEN` restores the counted total (D-52).
- **D-73 FIX-02 total request failure is a failed run.** `src/sources/base.py` gains `SourceUnavailable` and `RequestTally` (success/failure counts, `raise_if_all_failed`). greenhouse, lever, ashby, rss, pagewatch, jobicy and hh count each board/feed/page/query (None data or an exception is a failure) and raise when all failed, so `run_source` records `ok=False` with an error. Partial failure, empty feeds, unchanged pagewatch pages and parse-empty responses stay ok. hn and mlh lost their catch-all `try/except` (single or paired requests): a None response raises `SourceUnavailable`, other exceptions reach `run_source`; "no Who is hiring story found" and "no cards parsed" stay ok. Robots-disallowed requests return None from the client, so they count as failures too. The `/sources` fixture needed no change (it does not render per-run errors).

- **D-74 FIX-03 parent dirs are created.** `db.connect` creates the parent directory of a file DB path (skips `:memory:`), so collect/digest/once/run work in a fresh clone; the `mkdir` in `_probe` was removed as redundant. `telegram_channels.default_client_factory` creates the parent of `TG_SESSION_PATH` before building the Telethon client (the Telethon login tool already did).

- **D-75 FIX-04 one shared intern rule.** `src/text.INTERN_RE = \bintern(?:ship)?s?\b` (case-insensitive) is the only intern matcher; all sources (including lever and greenhouse, which used `intern\w*`) and `filters` import it, so "Internal"/"International" no longer count. Greenhouse keeps a separate `SUMMER_RE` (`\bsummer\b`) for "Summer Analyst". In filters the senior-only exemption uses `INTERN_RE` on the title, and `_kw_match("intern", ...)` (the positive keyword) delegates to it; other keywords keep prefix matching. `telegram_channels` keeps its own regex because it adds Russian stems.

- **D-76 FIX-06 tests no longer depend on user-edited config.** `tests/fixtures/config_repo_snapshot.toml` is a committed copy of `config.toml`; the `/sources` fixture, filters, evals runner and the full e2e run load it. Only `tests/test_source_count.py` and the e2e count check read the real `config.toml`, on purpose (DoD guard for 6 sources). `test_default_config_channels_empty` became `test_repo_config_channels_are_valid_handles`. Verified in a scratch copy with a channel and an extra Greenhouse board added: whole suite green (the scratch copy is not a git repo, so `test_gitignore` was excluded there). The snapshot must be refreshed by hand if the repo default config changes in ways tests depend on.

- **D-77 FIX-07 login tool uses Settings.** `telethon_login.main` resolves env via `Settings.from_env(os.environ, require_secrets=False)`, so an empty `TG_SESSION_PATH=` falls back to `data/telethon.session` (as the bot does) instead of an in-memory Telethon session. A non-integer `TG_API_ID` prints a message and returns 2. Tests inject a fake `telethon.TelegramClient`; no network, session file or `.env` is touched.

- **D-79 FIX-09 honest UA default, README accuracy, DoD checklist.** Default `HTTP_CONTACT` is the project repo `https://github.com/trulyaldi/oppo-tg` (replaces the wrong `https://github.com/oppo-tg`; supersedes the "placeholder" wording in PLAN); the user's email is not used, and the README tells the user to set their own contact. `digest_min_items` was dead config (D-17: never pad), so it is removed from `Thresholds`, `config.toml`, the test snapshot and README; `AppConfig.from_dict` silently ignores it so existing user configs keep loading. README and CLI help now say `collect`/`once` also send instant alerts. README "Verification status" gained a per-item Definition of done list; `.env.example` is marked not met until the user adds the block (D-03). The 2026-11-06 probe result is described as a first-run result.

- **D-78 FIX-08 Kaggle env leftovers removed (addendum to D-68, supersedes the D-71 note).** `KAGGLE_USERNAME`/`KAGGLE_KEY` are gone from `ENV_VARS`, `Settings`, the README env table and `.env.example` block, and PLAN section 3; `[sources.kaggle]` keeps only `enabled = false` with the D-68 comment. Stray `KAGGLE_*` in a user `.env` are ignored. `.env.example` is not touched by agents (D-03); the user may delete those two lines by hand.

- **D-80 FIX-10 redirects are followed manually.** `PoliteClient` sets `follow_redirects=False`; `_fetch` loops over `_fetch_once` (throttle, lock, retries as before). Redirect statuses are 301/302/303/307/308 with a `Location` (304 is not a redirect). Max 5 hops, then None. Relative `Location` is resolved against the current URL; non-http(s) targets are refused (None). With `check_robots` on (and db/clock present, D-34) every hop URL goes through `_robots_allowed`, which is cached per host, so same-host hops cost no extra fetch; a disallowed hop returns None and is never requested. robots.txt fetches follow redirects without a robots check. Caller headers and params are not forwarded on hops (headers only on same-host hops). Cache key stays the original request URL. `src/sources/mlh.py` still points at `mlh.io` (not changed; the redirect is now handled safely, and www.mlh.com's robots/terms were not re-verified).

- **D-80 addendum (FIX-12) redirect hardening.** Caller headers are forwarded on a hop only when scheme, host and port are all unchanged (plus the http -> https upgrade of the same host on default ports 80 -> 443, as httpx does); any other hop, including cross-port, drops them (User-Agent stays ours). An https -> http hop is refused (None, logged, not requested). A 3xx without a `Location` returns None from `_fetch`, and a 304 with no cached row returns None from `get_text`; neither is cached, so pagewatch cannot hash a redirect stub. The default contact is one constant, `DEFAULT_HTTP_CONTACT` in `src/config.py`, used by `ENV_VARS`, `Settings`, `PoliteClient` and `build_client`.
- **D-37 addendum (FIX-11).** (1) Relevance check is skipped for `pagewatch` items (their titles are page names like "IMC Careers", and the user curates those pages), simplest option; D-13's cost control stays for ATS boards. (2) Russian stems (аналитик, данны, инженер, разработчик, программист, исследовател, учен, трейдер, машинн, стаж, квант) added to `positive_title_keywords` in `config.toml` and the test snapshot; they are longer than 3 chars so they prefix-match, and Python `\b`/`\w` are Unicode-aware. "Менеджер по продажам" still fails. (3) The PLAN 1.8 senior-only exemption is now implemented: `INTERN_RE` (kept whole-word, so "Internal"/"International" do not exempt) plus junior|graduate|new grad|student|entry|trainee and the stem "стаж". Earlier D-37 narrowing had no recorded reason. (4) FELLOWSHIP is now exempt from the unpaid filter, as PLAN 1.8 says; no reason to keep it out (fellowships are often stipend-light, and the kind already bypasses relevance).

- **D-81 FIX-13 MLH uses its canonical host www.mlh.com (addendum to D-49 and D-80).** `season_url` now returns `https://www.mlh.com/seasons/<year>/events`; `mlh.io` answers `302` to that host (re-checked 2026-11-06), and the cross-host hop dropped conditional headers so MLH could never get a 304. Robots and terms check for www.mlh.com, recorded 2026-11-06 (reviewer, UA `oppo-tg/0.1 (personal job-alert bot; +https://github.com/trulyaldi/oppo-tg)`, one request each, 2s apart): `https://www.mlh.com/robots.txt` HTTP 200, `User-agent: *`, Disallow `/account/ /tools/ /_/ /v4/ /auth/ /admin/ /sidekiq/ /marksmith/ /graphql /events/*/partners/*/promo_codes/ /projects/invites/ /submissions/invites/ /submissions/*/invites/`, then `Allow: /`; `urllib.robotparser.can_fetch("oppo-tg", "/seasons/2027/events")` is True, so `check_robots=True` stays. Terms `https://www.mlh.com/terms` HTTP 200, "Last updated: June 16th, 2021": grants a right "to access, view, and use the Site solely for your personal purposes and non-commercial use"; prohibits using the Service or Site commercially, reverse engineering and interfering with the Site; no clause about robots, scraping, crawling, spiders, automated access or data mining. MLH stays kept: personal, non-commercial, one cached request per hour. Tests and README use www.mlh.com; README Setup now says `.env.example` is only complete after "First run / user actions" step 1.


- **D-82 FIX-14 Russian seniority and apprentice fixes (addendum to D-37, D-13).** (1) `senior_only_keywords` gains the Russian stems старш, ведущ, руководител, главн, директор (prefix match, title only; same trainee/intern waiver as English), in `config.toml` and the test snapshot. Trade-off: "Главный"/"Ведущий" are always read as senior; an occasional false filter of a non-senior "ведущий" title is accepted (hard filters stay cheap, D-13). (2) Positive stem `учен` replaced by `учены` and `учёны` (casefold keeps ё, so both spellings are listed): matches Ученый/Учёный/Ученые, no longer Ученик/ученичество. (3) Narrowing of the FIX-11 junior waiver (not a contradiction of PLAN 1.8, whose intent is trainee roles): bare `\bстаж` became `\bстаж(?:ер|ёр|ир)`, so стажер/стажёр/стажировка still waive but "стаж от 5 лет" (experience) does not. The positive relevance stem `стаж` is unchanged.
- **D-83 Scoring call fixed for the installed SDK, and total scoring failure now alerts.** `anthropic` 2.6.0's `AsyncMessages.create()` has no `temperature` (or `top_p` / `top_k`) parameter, so every scoring call raised `TypeError` and was swallowed by `Scorer.score`, leaving an empty digest with only a log line. Fix: the scorer sends only `model`, `max_tokens`, `system` and `messages`. This supersedes the `temperature=0` in D-12/D-39/PLAN T09; output stability now rests on the strict JSON contract plus the validate-and-retry-once parser (D-11), not on sampling. Model id `claude-haiku-4-5` re-checked on 2026-11-06 against the claude-api skill model table and the installed SDK's own `Model` literal: both list it, so it stays. Tests: the old fake client accepted any keyword (`**kw`) and asserted `temperature == 0`, so it could never catch this; `tests/test_scorer.py` now has a fake that binds the call against `inspect.signature(AsyncMessages.create)`, a test that the default model is in the SDK's model list, and a check that re-adding `temperature` fails the suite (verified by temporarily re-adding it). New `src/pipeline/scoring_alert.py`: when a run scores nothing and at least one posting failed, `score_pending(..., bot=, chat_id=)` sends a Telegram "ERROR: scoring failed" message with the model and the last error (HTML-escaped, truncated to 200 chars). It is sent at most once per 6 hours (state key `scoring_failure_alerted_at`), is re-armed by the next run that scores anything, is sent even when `/pause` is on (it is an operational error, not content), and never raises: a failed Telegram send is logged and retried on the next run. Partial failures (some items scored) do not alert.
- **D-84 S1 scoring call re-verified (addendum to D-83).** Checked 2026-11-06 with `pip show anthropic` (2.6.0) and `inspect.signature(AsyncMessages.create)`: parameters are model, max_tokens, messages, system, stop_sequences, stream, thinking, tool_choice, tools, metadata, output_config, service_tier and similar; no `temperature`/`top_p`/`top_k`. HEAD 2d762cd's fix is correct: `Scorer._call` sends only model, max_tokens, system, messages. Model: the SDK `Model` literal lists claude-haiku-5-5, claude-sonnet-5-5, claude-opus-5-5 and also claude-haiku-4-5, so the default `claude-haiku-4-5` is valid and stays. Not switched to claude-haiku-5-5 because D-39 recorded that it thinks by default, which would eat the `max_tokens=300` budget, and this cannot be verified offline; override with `SCORING_MODEL=claude-haiku-5-5` after an eval run. New tests drive `Scorer` through a real `AsyncAnthropic` client on a mock transport (request body keys checked, HTTP 401 becomes a scoring error, `temperature=0` raises `TypeError`). Note: anthropic 2.x uses `httpx2`, not `httpx`; passing an `httpx.AsyncClient` raises `TypeError`, so these tests use `httpx2.MockTransport` (httpx2 comes in as an anthropic dependency).
- **D-85 S2 total scoring failure: exit code (addendum to D-83).** D-83 already alerted on "errors > 0 and scored == 0". Gaps filled: (1) `is_total_failure` now also counts `skipped` postings as candidates (nothing scored although candidates existed, e.g. a zero call budget); (2) `collect` and `once` now exit 1 in that case (`_run` returns the code, `main` returns it), after still sending alerts and the digest so the rest of the run completes; (3) tests in `tests/test_main_exit.py` drive `_run` with a mocked bot, Claude client and collector. `run` (long-lived scheduler) cannot exit per job, so it only alerts (throttled to once per 6 hours); in one-shot mode the throttle can suppress a repeat message but the exit code is still 1.
- **D-86 S3 single-instance lock.** `src/lock.py` `InstanceLock`: the lock file is the DB path with a `.lock` suffix (`data/oppo.lock` by default; `data/*` is gitignored, and a custom `DB_PATH` gets its own lock). Created with `O_CREAT|O_EXCL`, holds the PID. If the file exists and its PID is alive (`os.kill(pid, 0)`), `run` prints "ERROR: another oppo-tg instance is already running (PID n, ...)" to stderr and exits 3; if the PID is dead or the file is garbage the lock is taken over. Release only deletes the file if it still holds our PID. Only `run` locks: `collect`/`digest`/`once` do not poll Telegram, so they cannot cause TelegramConflictError, and the user may run them beside a service. Known limit: PID reuse after a crash could make a stale lock look alive; the user removes the file by hand (the message names it). SIGKILL leaves a stale file, handled by the PID check.
- **D-87 S4 feedback persistence and whitelist verified.** Code was already correct: `handle_feedback` writes `reactions` and `reaction_log` and commits; `WhitelistMiddleware` (outer middleware on updates) drops anything not from `TELEGRAM_CHAT_ID`, with existing tests `test_whitelist` (message, no-chat update) and `test_whitelist_blocks_callback`. Gap filled: the only restart test used `handle_feedback` directly with one action. New `test_all_reactions_survive_restart_through_dispatcher` sends like, save, applied and dislike callbacks through a real aiogram dispatcher (Bot API calls stubbed by patching `Bot.__call__`, no network), closes the connection, reopens the same db file with a new connection and new dispatcher, checks all state (dislike replaces like) and that a Save toggle still works.
- **D-88 S5 final check and live probe (2026-11-06).** `python -m pytest -q` and `ruff check . && ruff format --check .` clean. `python -m src.main probe` (README command, separate `data/probe.db`, exit status printed table "Counted families with items: 6 (need 6)"). Working (ok, items): ashby 425, greenhouse 3169, lever 407, hn 201, jobicy 200, mlh 24, pagewatch 7, rss 24. Not working, by design and not fixed: hh (needs HH_ACCESS_TOKEN), telegram_channels (needs TG_API_ID, TG_API_HASH), devpost (disabled in config.toml). Many ATS fetches answered 304 from the cache, which is expected. No secrets were printed or read.

## 2026-11-06 — Outreach and weights design (planner, PLAN section 8)

- **D-89 SPEC edits for the outreach-and-weights features. Extends D-23 and D-55.**
  - The orchestrator's brief explicitly asked for SPEC.md to describe the new features. So this time the planner appended new sections: "Employer-region weighting", "CV", "Contacts", "Prepare and draft" and "Privacy". It also appended two bullets under Delivery.
  - No existing SPEC line was reworded.
  - The new definition-of-done items go in a separate section, `## Definition of done: outreach and weights`, not into the original list. Reason: `tests/test_readme.py::test_dod_status_lists_all_spec_items` splits SPEC at the first `## Definition of done` and asserts exactly 8 bullets. A separate `##` section keeps that test green on this branch with no code change. F1 adds a README check for the new section.
  - D-23 stays the rule for every other change. Requirements are changed only when the user (or a brief relaying the user) asks.
- **D-90 New dependencies: PyYAML and pypdf. Amends D-06 and D-21.**
  - D-06 chose TOML to avoid an extra dependency. The brief explicitly asks for `config/weights.yaml`, so we add `pyyaml>=6,<7` and load it only with `yaml.safe_load`, which never builds arbitrary objects. A W1 test greps for `yaml.load(`.
  - PDF CVs need a text extractor. `pypdf>=5,<7` is pure Python and BSD-licensed, and it reads from memory (no temp files).
  - Both are added and installed in W1 BEFORE any `.py` file imports them. The PostToolUse hook runs pytest on every `.py` write, as D-28 learned. Neither package is installed in the venv today (checked 2026-11-06).
  - `config.toml` stays the main config. The only new key that points at YAML is `[ranking] weights_file`.
- **D-91 Weights file location, validation and fallback.** (The once-per-day invalid-weights notice is built in W6.)
  - `config/weights.yaml` holds the adjustments, `region_min_confidence`, `digest_quotas`, flags and watchlist paths. The watchlists are `config/watchlists/intl_top_tier.yaml` and `config/watchlists/kz_employers.yaml`. All are committed, because they contain no secrets.
  - The file is validated with pydantic (`extra="forbid"`), so a typo fails loudly instead of being ignored.
  - At selection time an invalid or missing file never stops the bot. The code logs the validation error, uses `BUILTIN_WEIGHTS` (the shipped defaults, empty watchlists) and sends at most one Telegram notice per day. Reason: a YAML typo must not silently skip the 09:00 digest.
  - Tests read a committed copy in `tests/fixtures/weights_snapshot/` (FIX-06 lesson). The snapshot config `tests/fixtures/config_repo_snapshot.toml` points `[ranking] weights_file` at that copy; this is its one intended difference from `config.toml`. So every test that loads the snapshot, e2e included, is immune to the user's YAML edits. The only test on the real file checks that it validates, not its values.
  - `Weights.version` is a hash of the YAML and both watchlists. It drives recomputation (D-94).
  - No env var was added, so `.env.example` (which agents cannot edit, D-03) needs no change.
- **D-92 "Region-blind" LLM score and new scoring fields.**
  - **Definition.** The LLM score must not credit or penalise the employer's home country. The job-location accessibility criterion (10%) stays, because it measures whether the candidate can actually work there. Without this split, the code-side KZ bonus would double-count.
  - **Rubric change.** `rubric.md` gains a section "Employer region (not part of the score)" and four optional output fields:
    - `employer_region`: kz, intl or unknown, by HQ/legal entity, not job location;
    - `region_confidence`: 0-1;
    - `employer_is_startup`;
    - `visa_barrier`.
  - The fields are optional with safe defaults, so old fixtures and old replies still parse. `MAX_TOKENS` goes from 300 to 400 to fit them.
  - The rubric change bumps `prompt_version`. The scoring-prompt skill requires an eval re-run, which needs the real API key, so it is a user action (PLAN section 3, added in F1). Old score rows keep `employer_region = 'unknown'`, meaning no adjustment unless a watchlist matches.
- **D-93 Employer classification: watchlist first, exact normalized match, LLM fallback with a confidence floor.**
  - Order:
    1. top-tier watchlist;
    2. KZ watchlist (`startup: true|false` per entry);
    3. LLM `kz` with confidence >= `region_min_confidence` (0.7): `kz_startup` if `employer_is_startup` is true or the kind is STARTUP, else `kz_employer`;
    4. LLM `intl` with confidence >= 0.7: `intl_other`;
    5. otherwise `unknown`, with adjustment 0.
  - Names match only after normalization (NFKC, casefold, ё->е, punctuation and legal forms such as LLC/LLP/ТОО/АО removed). There is no fuzzy matching, because a false top-tier match would push noise to the top of the digest. Aliases cover the variants instead.
  - Classification uses the posting's company field, never its location, so a remote role at a KZ company is KZ and an Almaty office of Citadel is top-tier international.
  - Only the watchlist can make an employer top-tier. The LLM never can.
  - pagewatch postings used the page name as company ("IMC Careers"), which exact matching would never map to IMC. `[[sources.pagewatch.pages]]` therefore gains an optional `company` key (W4), set for IMC, Optiver, Jane Street, CERN, EPFL, ETH Zurich and OIST. Without it the page name is used, as before. Posting ids are unaffected.
  - The KZ seed list is a starting point. Freedom Holding is listed as KZ because it is run from Almaty, although its listed parent is US-incorporated. The user can move it.
- **D-94 Adjustment formula, persistence and recomputation.**
  - Formula: `adjusted = clamp(base + adjustments[category] + visa, 0, 10)`, rounded to 2 decimals.
  - `visa` = `intl_onsite_visa_barrier` (-0.5) only when all of these hold:
    - the category is intl_top_tier or intl_other (it stacks with both: an on-site top-tier role with a visa barrier gets +0.5 net);
    - `visa_barrier` is true;
    - `remote` is not true.
    It never applies to KZ or unknown employers.
  - **DB.** `scores.score` is renamed `base_score` (SQLite 3.45.1 supports RENAME COLUMN). New columns: `adjusted_score`, the four LLM inputs, `employer_category`, `adjustment`, `adjustment_detail` and `weights_version`. Migration v2 backfills `adjusted_score = base_score` and leaves `weights_version` NULL, so the first recompute classifies old rows by watchlist.
  - **Why the inputs are stored, not just the result.** `recompute_adjusted` runs after scoring and before every digest, alert and `/top`. It re-classifies only rows whose `weights_version` differs from the current file. So editing the YAML re-ranks existing items with no code change and no LLM call. If `adjusted_score` were frozen at scoring time, YAML edits would only affect new items.
- **D-95 Digest selection with quotas.**
  1. Eligibility gates on `base_score >= digest_min_score`, not the adjusted score, so a bonus cannot lift a weak posting into the digest. Example: a KZ startup with base 4.5 stays out at the 5.0 floor.
  2. Within the eligible set, picks and display order use `adjusted_score`.
  3. Quotas (kz 4, intl 4, best_remaining 2) are the single source of truth for digest size. `thresholds.digest_max_items` is removed, and `from_dict` drops it silently as D-79 did for `digest_min_items`, so old configs load. Reason: two overlapping size knobs would conflict.
  4. Fall-through: kz and intl slots that cannot be filled go to the best remaining items.
  5. Unknown-region items have no bucket and no flag. They compete only for best-remaining and fall-through slots. Reason: the KZ quota is reserved for confirmed KZ employers, and the international quota for confirmed international ones.
  6. Display order is adjusted desc over the whole digest, not grouped by region. So with the defaults a clearly bad KZ item (base <= 5.5, adjusted <= 6.5) always sits below a clearly good international item (base >= 7.5, adjusted >= 7.0). W6 tests every combination.
- **D-96 Flag emoji in item headers. A narrow exception to D-18.**
  - The brief explicitly asks for flag emoji tags, KZ `🇰🇿` and world `🌍`. They are the only emoji the bot emits. Both are set in `weights.yaml` `flags`, and `""` turns one off.
  - The existing unflagged layout stays byte-for-byte the same, so `render_item_expected.txt` is unchanged and a new fixture covers flagged items.
  - When the adjustment is non-zero, the score line shows `adjusted/10 (base X, detail)`, so the user always sees why an item moved.
- **D-97 Alerts use the base score.**
  - An item alerts when `base_score >= alert_score` (9.0), as the brief asks, or when its deadline is within 7 days and `base_score >= alert_deadline_min_score`.
  - A region bonus never triggers an alert. Example: a top-tier item at base 8.6 (adjusted 9.6) waits for the digest.
- **D-98 Outreach LLM calls: one shared helper, models in config.toml, daily cap.**
  - **Helper.** `src/llm/json_call.py` follows the proven scorer pattern (D-11, D-83, D-84):
    - it sends only `model`, `max_tokens`, `system`, `messages`;
    - it never sends `tools`, `tool_choice` or `temperature`;
    - it validates with pydantic and retries once;
    - it never raises and never logs content.
  - **Tests** use the S1 fake bound to `inspect.signature(AsyncMessages.create)`.
  - **Models** (both present in the installed SDK `Model` literal, checked 2026-11-06; C1 re-checks):
    - `claude-haiku-4-5` (cheap) for CV parsing and pasted-contact extraction;
    - `claude-sonnet-4-6` for `/prepare` and `/draft`, where judgement and writing quality matter.
  - `max_tokens` is generous (2000/800/2000/400), because a model may think by default (D-39), and only text blocks are read.
  - **Config.** Models and caps live in `config.toml` `[outreach]`, not env vars. They are not secrets, and new env vars would need `.env.example` lines that agents cannot write (D-03).
  - **Cap.** `daily_llm_call_cap = 40` covers all outreach calls per Asia/Almaty day, retries included. It is counted in `llm_usage`, which stores counts only, no content. Scoring keeps its own `MAX_SCORES_PER_RUN`.
- **D-99 Untrusted text in prompts.**
  - Posting, page, pasted-profile and contact text, and the CV too, go into the user message inside `<posting>`, `<page>`, `<profile>`, `<contact>` and `<cv>` tags.
  - `wrap_untrusted` neutralizes any `<`/`</` followed by a reserved tag name (case-insensitive, whitespace allowed) to `‹`, so data cannot close a delimiter and pose as instructions.
  - The static system block says this text is data, never instructions, and that the model cannot contact anyone.
  - Outputs are schema-validated, and every claim is re-checked in code (D-107), so a successful injection can at worst produce text that the verifier then strips.
  - The scorer's existing `<posting>` block is left unchanged, so the scoring prompt_version does not move twice.
- **D-100 CV storage and handling.**
  - **Where.** The CV is stored in the SQLite DB (`data/oppo.db`, under `data/*`, which is gitignored). The original upload is never written to disk: the bot downloads the PDF into `io.BytesIO`, pypdf reads it in memory, and the bytes are dropped.
  - **Only the current version is kept.** Older rows are deleted on upload, which minimises stored personal data. `version` is an AUTOINCREMENT integer, so numbers stay monotonic.
  - **Identity.** `sha` = sha256 of the normalized text (16 hex). Identical text does not bump the version and makes no LLM call.
  - **Limits.** PDF or pasted text only, no DOCX; at most 5 MB, 20 pages and 30000 characters.
  - **Scanned PDFs.** A PDF with no text layer is refused with "paste the text instead". There is no OCR: it would mean another dependency, and its accuracy cannot be trusted for claim verification.
  - **Anthropic notice.** It appears twice: in the `/cv` prompt (before anything is sent) and in the confirmation.
  - **Fixtures.** Every CV, contact and profile fixture is fictional and carries `FAKE FIXTURE`, which P7 checks.
- **D-101 CV section parsing is grounded.**
  - The cheap model extracts education, experience, projects and skills.
  - Code then keeps only strings that appear in the CV text after normalization. For bullets, the whole bullet must be a substring.
  - Invented skills or jobs are dropped and counted.
  - A parse with neither education nor experience is rejected and nothing is stored.
  - Reason: the parsed `skills` feed the verifier's skill lexicon (D-107), so an invented skill would otherwise become "evidence".
- **D-102 Generic pending-input state.**
  - `state.pending_input` holds JSON `{kind: cv|contact, posting_id, expires_at}`, with a 15-minute expiry (`[outreach] pending_input_minutes`).
  - A plain text or document message is consumed only while a matching pending state is live. All other non-command messages are ignored with no LLM call.
  - `/cancel` clears the state.
  - It is stored in SQLite rather than aiogram FSM memory, so it survives a restart and follows the existing `state` pattern.
- **D-103 Contacts: on demand, minimal fields, short retention.**
  - **When.** Discovery runs only when the user runs `/prepare` for one posting (`/contacts` lists stored contacts and makes no requests; amended by D-143). It never runs in collect, the scheduler or alerts; an N6 test greps for this. That is the "no bulk collection" guarantee.
  - **Order.**
    1. posting text (no network);
    2. for `hn` items, the single Algolia comment item (author handle + contact lines);
    3. the employer's own domain `/team`, then `/about` (domain from the watchlist, else the posting URL host unless it is an ATS/aggregator);
    4. for hackathons and competitions, the event page itself (organizers).
  - **Limits.** At most 2 page fetches per `/prepare` and 20 per day, at most 10 contacts per posting, rediscovery at most every 7 days.
  - **Stored fields.** Name, role, company, source URL, a note of at most 200 characters and the expiry, plus type, confidence, source kind and posting id.
  - **No emails or phones.** They are redacted from every field even when published, because the brief's allowed fields do not include them and the user can open the source URL. A posting that lists an address gets the note "posting lists a direct contact address; see the posting".
  - **Expiry.** `min(created + 60 days, deadline + 1 day)`. The daily prune deletes expired contacts, and their drafts go with them by FK cascade.
- **D-104 Blocked hosts. LinkedIn and YC are human-assisted only.**
  - `PoliteClient` refuses, with no request and no robots fetch, these hosts and their subdomains, including on redirect hops:
    - LinkedIn (`linkedin.com`, `lnkd.in`);
    - `ycombinator.com` (its terms forbid scraping, D-54);
    - `devpost.com` (D-50);
    - job aggregators whose terms forbid scraping (glassdoor, indeed, wellfound, angel.co, crunchbase);
    - paid contact finders (hunter.io, apollo.io, rocketreach.co, zoominfo.com, lusha.com, contactout.com, signalhire.com).
  - **Why in the client.** A future code path cannot fetch them by accident.
  - **LinkedIn.** The bot builds two people-search URLs per posting (company + "recruiter", company + team term) as URL buttons the user opens in their own browser. The user may paste profile text or a profile URL. The URL is stored as `source_url` and never fetched.
  - **YC.** The brief listed YC company pages as a source, but D-54 forbids fetching them. So for HN/HN-Jobs postings the bot shows a `ycombinator.com/companies?query=<company>` link for the user, and pasted text is accepted the same way.
- **D-105 Terms stance for on-demand team and organizer pages.**
  - The D-31/D-53 new-source rule (read the terms before writing fetching code) governs collection sources that poll a fixed site repeatedly.
  - Contact pages are different. They are arbitrary employer or event sites, fetched once, when the user asks, for one posting, at most 2 pages: the same access a person opening the page would make.
  - Reading every employer's terms is impossible at that scale. Instead:
    - robots.txt is always obeyed;
    - the UA is honest;
    - responses are cached for 7 days;
    - fetching stops on 403/429 and never evades them;
    - the blocked-host list (D-104) covers known no-scraping sites.
  - These fetches are not a "source" and do not count toward D-52.
- **D-106 Contact type classifier is deterministic.**
  - `classify_role` matches English and Russian stems on the role string, in this order of precedence:
    1. recruiter (recruit, talent, sourcer, people partner, HR, рекрутер, подбор): 0.9;
    2. founder (whole-word founder/co-founder, основатель): 0.9; bare CEO 0.6;
    3. hiring_manager (head of, director, VP, CTO, lead, team lead, руководитель, тимлид): 0.8; bare "manager" 0.6;
    4. engineer (engineer, developer, researcher, scientist, quant, trader, инженер, разработчик, исследователь): 0.8; analyst 0.6;
    5. other: 0.3.
  - "Founding Engineer" is an engineer, and "Engineering Manager" is a hiring manager.
  - The type is always computed by code from the role, never taken from a model reply, so pasted or injected text cannot relabel a contact.
  - Reasons: it is testable, free, and good enough for choosing a draft tone.
- **D-107 Tailoring output is structured, and the verifier is deterministic.**
  - **No free prose about the candidate.** Fit points, emphasize/cut/reorder items and keywords each carry a verbatim `cv_evidence` quote; gaps carry `posting_evidence`.
  - **Checks in code:**
    - V1: the evidence is a normalized substring of the CV;
    - V2: every number or year in a claim appears in its evidence;
    - V3: inflation words (led, managed, founded, expert, winner, grandmaster, PhD and similar, plus Russian stems) appear in the evidence;
    - V4: skill-lexicon terms appear in the CV;
    - K: keywords must appear in BOTH the CV and the posting;
    - G: gap advice must not state invented numbers or skills.
  - **On failure.** Failing items are removed and only counted ("Removed N unsupported statements."); their text is not echoed. A keyword that the posting asks for but the CV lacks becomes a gap.
  - **Evidence format.** The CV goes to the model as stored text without line numbers, because evidence must be a verbatim quote and copied line markers would fail V1.
  - **No second-model verifier.** An LLM judge can itself hallucinate, costs a call and cannot be tested deterministically. Evidence quotes plus number, inflation and skill checks catch the realistic failure modes: invented credentials, inflated scope, wrong numbers and borrowed keywords.
  - **Known limit.** A claim can paraphrase real evidence slightly too strongly with no flagged word. The structured schema and the per-claim quote shown to the model reduce this.
  - **Fabrication test.** P1/P2 use a bait posting (C++, kdb+, years of trading, Kaggle Grandmaster) and a canned fabricated reply.
- **D-108 Caching: never regenerate.**
  - Tailoring is cached per (posting, CV version) and drafts per (contact, CV version). A cached result is returned even after a prompt change, as the brief asks (prompt_version is stored for reference).
  - Failed or unverifiable results are never cached, so a later try can succeed.
  - A new CV version deletes stale cache and draft rows through `on_new_cv_version` hooks in the same transaction, and `/forgetcv` deletes them through `on_forget_cv` hooks. P2 and P5 register these hooks, because their tables do not exist when C3 builds the CV store. So no code ever deletes from a table that has not been migrated yet.
  - A per-key `asyncio.Lock` makes a double press produce one LLM call.
- **D-109 Drafts.**
  - **Shape.** 3-5 sentences, one hook with a verbatim quote from the posting or the contact's role/note, one ask that appears in the body, and claims with CV evidence.
  - **Guidance by contact type.** Recruiter: fit + process. Hiring manager: technical hook + 15-minute call. Founder: product hook + short call. Engineer: question + advice or referral.
  - **Sentence-level checks.** Numbers must be in the CV, posting or contact. Inflation words must be in the CV. Skill terms must be in the CV or the hook quote.
  - **On failure.** Offending sentences are dropped. If fewer than 3 remain, or the ask is lost, the draft is regenerated once; the correction lists the unsupported terms only. A second failure gives an honest "could not write a draft" message.
  - **Display.** Drafts are shown in `<pre>` under "Not sent: copy it yourself."
  - **No sending path.** No code sends email or Telegram messages to anyone but `TELEGRAM_CHAT_ID`, and the model gets no tools. P6 and P7 test this.
- **D-110 Telegram UX details.**
  - Posting ids (16 hex) cannot be typed comfortably, so:
    - `/saved` lines and `/prepare` output carry tappable command links `/prepare_<id>`, `/contacts_<id>` and `/draft_<id>` (Telegram commands allow `[a-z0-9_]`, up to 32 characters);
    - typed refs accept any unique hex prefix of at least 6 characters.
  - **Callback prefixes:** `pr:` prepare, `dr:` draft, `ac:` add contact. Each is at most 19 bytes, well under the 64-byte limit, which is asserted.
  - **Prepare button.** A second keyboard row `[Prepare]` appears once Like or Save is active, so the first row stays exactly as before.
  - **Responsiveness.** LLM work happens after `cb.answer`, so Telegram's callback spinner never times out.
  - **Long output** is split at line boundaries into chunks of at most 4000 characters, with no tag spanning lines (digest-format skill).
- **D-111 Privacy logging rule.**
  - Outreach code logs only ids, counts, versions, `purpose`, `model` and `error_kind`. It never logs CV text, contact names or roles, pasted text, posting text, model output or drafts. Exception messages that could echo content are reduced to their type.
  - Canary tests (C1, C4, N1, N7, P2, P7) run flows at DEBUG and assert that no canary string reaches the logs.
  - Nothing outreach-related is written outside the DB.
- **D-112 Smoke test.**
  - F2 adds `--max-scores N` to `collect` and `once`. The effective cap is `min(MAX_SCORES_PER_RUN, N)`, tested offline.
  - The implementer runs `once --max-scores 20` once. `load_dotenv` runs with `override=False` (checked in the installed python-dotenv), but the cap comes from the CLI flag, so `.env` cannot raise it. The program reads `.env`; the agent never does.
  - The run makes no outreach calls, because outreach is user-initiated in Telegram only.
  - Only counts and the KZ/intl/unknown split of the digest are recorded.
  - Exit clause: if secrets are missing or the permission system refuses, the smoke run becomes a user action (D-30) with the exact command, and the task is marked done.
- **D-113 Task order and migration numbering.**
  1. W1-W7 come first. They are independent of outreach and improve every digest.
  2. Then C1-C5: the shared LLM helper, then text CV before PDF, so the CV path works before the pypdf parts.
  3. Then P1-P4, a thin `/prepare` slice from posting text with the verifier and cache.
  4. Then N1-N7, contacts wired into `/prepare`.
  5. Then P5-P7: drafts need contacts. P7 is a cross-cutting privacy sweep.
  6. Then F1 (README/DoD) and F2 (capped smoke run).
  - Migrations take the next free `user_version` in task order: v2 W2, v3 C1, v4 C3, v5 P2, v6 N1, v7 P5. If a re-plan reorders tasks, the next free number is used, and each migration test upgrades a DB at the previous version that already holds rows. The user's real `data/oppo.db` must survive every step.
- **D-114 W1 install result and loader details.**
  - `pip install pyyaml pypdf` succeeded in `.venv` (PyYAML 6.0.3, pypdf 6.21.4). Both are in `requirements.txt` with the D-90 bounds.
  - Loader errors are `WeightsError(ValueError)` with the field named in the message; pydantic errors are re-wrapped so no YAML content leaks into logs.
  - Duplicate-alias check uses strip+casefold over names and aliases across both watchlists. Full normalization (legal forms, NFKC) is W4's `normalize_company`; W1 only needs to catch obvious duplicates.
  - In the file all five adjustment keys are required (missing or unknown key fails), while `Weights()` defaults serve `BUILTIN_WEIGHTS`.
  - `AppConfig` gains `ranking` and `outreach` plain dicts; `[outreach]` is not in config.toml yet.
- **D-115 W3 scoring contract applied.** `ScoreResult` gained `employer_region` (default unknown), `region_confidence` (0..1, default 0), `employer_is_startup` and `visa_barrier` (nullable); `save_score` stores them; `MAX_TOKENS` is 400; rubric.md has the "Employer region (not part of the score)" section and the extended output contract. `prompt_version` changed from `59e8516c95e4` to a new hash. The eval re-run needs the real API key, so it is a user action (PLAN section 3, F1).
- **D-116 W4 classifier details.** `normalize_company` replaces non-word characters with spaces (so `Kar-Tel` matches itself consistently) and keeps `.` only inside a word; legal forms and leading/trailing `the` are dropped, and an all-legal-form name normalizes to empty (falls to LLM rules). pagewatch gained an optional `company` key (falls back to `name`, then url); set in config.toml and the test snapshot for the seven pages (D-93).
- **D-117 W5 adjust details.** `adjust` detail labels are `top-tier`, `intl`, `KZ employer`, `KZ startup` and `visa`, each with a signed one-decimal amount (`"+1.0 top-tier, -0.5 visa"`); the detail is empty when the net adjustment is 0. The stored `adjustment` is the net (pre-clamp) value. `recompute_adjusted` returns the number of rows updated.
- **D-118 W6 digest quotas.** `select_digest(conn, cfg, weights=None)` loads weights from `cfg.ranking.weights_file` (none configured -> `BUILTIN_WEIGHTS`), recomputes adjusted scores, gates on `base_score`, then `pick_with_quotas(items, quotas, category_of)` returns the picked set in adjusted-desc order (ties: deadline asc nulls last, base desc, rowid). `digest_max_items` is dropped silently by `from_dict`. `send_digest` calls `load_weights_checked` (returns the error string on fallback) and sends the once-per-Almaty-day "weights.yaml invalid" notice before the pause check; the error is cut to 200 chars, then HTML-escaped, and the day key is stored only after a successful send.
- **D-119 W7 rendering and selection.** `Item` gained `flag`, set by `item_from_row(row, weights)` in digest.py from the employer bucket (empty for unknown or when the YAML flag is ""). The score line shows `adjusted/10 (base X, detail)` only when `adjustment_detail` is non-empty. `select_alerts` gained an optional `weights` arg, recomputes first and gates on base score (D-97). `cmd_top(message, conn, clock, cfg=None)` loads weights from cfg, recomputes, and renders flags; app.py passes cfg. The flag emoji exception is D-96.
- **D-120 C1 LLM helper details.** Models verified present in the installed SDK `Model` literal: `claude-haiku-4-5` and `claude-sonnet-4-6` (test_models_known_to_sdk guards it). `LlmBudget.reserve(purpose)` counts before each call; the cap is summed across purposes for the Almaty day. If the retry cannot reserve, `call_json` returns `error_kind="budget"` with `calls == 1`. Pydantic errors reach the model only as an error count (no echoed input), `OutreachConfig.from_cfg` rejects unknown keys. The static `prompts/*.md` system block is built by the P/C tasks that need it; C1 takes `system_blocks` as a parameter. Schema is v3 (`llm_usage`).
- **D-121 C2 CV parse details.** `norm` (NFKC, casefold, unified quotes/dashes, bullet markers stripped, whitespace collapsed) lives in `src/cv/extract.py` so P1's verifier can reuse it. `cv_sha` hashes the whitespace-collapsed text so it is stable across whitespace-only edits. `parse_cv_sections` returns `ParseResult(sections, error, dropped, calls)`; errors are `budget|api|invalid|no sections`. Grounding: an education/experience/project item whose required string (institution, organization+role, name) is ungrounded is dropped whole (1 counted); an ungrounded degree is blanked (1 counted); ungrounded skills/tech are dropped; bullets, details and `other` must be V1 quotes (>= 8 chars, substring). The static prompt is sent with `cache_control: ephemeral`.
- **D-122 C3 CV store details.** Schema is v4 (`cv_versions`). `save_cv(conn, text, sections_json, source_kind, model, now)` takes the sections as a JSON string (callers dump `CvSections`), keeps only the newest row, and compares `cv_sha` so whitespace-only edits are no change. The hook registries are module-level lists (`on_new_cv_version`, `on_forget_cv`) run inside the save/forget `with conn:` block, so a raising hook rolls everything back. `forget_cv` returns `{"cv_versions": n, "derived": hook_rows}`.
- **D-123 C4 pending input and CV text flow.** `pending.py` adds `peek_pending` (non-clearing) beside `set/take/clear`. The text handler is gated by an async filter that is true only while an unexpired `cv` pending exists, so other text (and expired input) falls through to later handlers with no reply and no LLM call; the filter must be async because aiogram runs sync filters in a thread and the SQLite connection is single-thread. A too-short paste keeps the pending state so the user can retry; every other outcome (saved, unchanged, parse failure) clears it. `build_dispatcher` gained an optional `client` (None in tests that do not need it; ingest then replies that parsing is unavailable); `main._serve` passes the Anthropic client. The 'Cleared {k} cached results' sentence is deferred to P2 because `save_cv` does not return hook counts and no hooks exist yet. `ingest_cv_text` is shared so C5 (PDF) only adds extraction. CV text is never logged.
- **D-124 C5 PDF ingestion details.** `pdf_to_text(data, max_pages)` raises `CvInputError` (messages never contain CV content; unexpected pypdf errors are reduced to "Could not read that PDF."). Magic check allows leading whitespace. The document handler is gated by the same async pending-`cv` filter as text, so documents while not pending (or from other chats, dropped by the whitelist middleware) trigger no reply and no download. Size is checked from Telegram's `file_size` before any download; the file goes to `io.BytesIO` only. A failed or scanned PDF keeps pending so the user can retry; success runs the shared `ingest_cv_text(source_kind="pdf")`. Fixtures `cv_fake.pdf` (full text of `cv_fake.txt`, one page, Helvetica) and `cv_scanned_fake.pdf` (page with a grey rectangle, no text) were generated by a small hand-written minimal-PDF script (no extra dependency, not committed; both are FAKE FIXTURE content). Encrypted-PDF test builds its input in-test with pypdf.
- **D-125 P1 verifier details.** `verify_tailoring(result, cv_text, posting_text, cv_skills) -> (verified, removed)` and `verify_draft(draft, cv_text, posting_text, contact_text, cv_skills) -> DraftVerdict(ok, body, removed, unsupported, reason)` are pure and never raise on malformed input. Inflation stems of 5+ chars match as word prefixes, shorter ones (led, won, gold, head) as whole words with s/ing/er suffixes, so "ledger" and "header" are not hits. Bare `go` and `r` are left out of the skill lexicon (they would remove honest "R&D"/"go to market" sentences); `golang` is in. Items over 200 chars are removed. Gap advice (G) is checked for numbers, inflation stems and skill terms against CV plus posting. Keywords failing K that the posting asks for become default gaps (capped at 4, de-duplicated). In drafts the ask's own numbers ("15 minutes") are allowed since the ask is a proposal, not a claim about the candidate; skill terms inside the hook quote are allowed per 8.1.12. Failed draft claims are counted in `removed` but do not by themselves fail the draft; the unsupported list holds terms only, never CV text. The fabrication test uses a dedicated fake CV `cv_bait_fake.txt` (no C++/kdb+, "Python (2 years)") because `cv_fake.txt` really lists C++.
- **D-126 P2 tailoring service details.** Schema is v5 (`tailoring_cache`). `prepare` returns `PrepareResult(posting_id, cv_version, result, removed, cached)` or `PrepareError(kind)` with kind `no_cv|no_posting|budget|api|invalid`. The model output schema `TailorOut` is deliberately loose (dict/list fields); the deterministic verifier is the validator. Posting evidence is verified against the wrapped (delimiter-neutralized) posting block that was actually sent, so a quote of neutralized text still matches. Cache hooks are registered by `register_hooks()` (idempotent) at import of `src/outreach/prepare.py`; `outreach_handlers` imports it and re-calls it at ingest so tests that clear the module-level hook lists still work. The "Cleared {k} cached results" sentence is computed in the handler from the `tailoring_cache` row count before and after `save_cv` (no change to `save_cv`'s return). Locks are per (posting, cv_version) in a module dict; the cache is re-checked inside the lock so concurrent calls make one client call.
- **D-127 P3 prepare rendering details.** `render_prepare(posting, result, contacts, links, removed, unreachable=False)` returns HTML chunks; every dynamic string is clipped to 200 chars and escaped, and each line is self-contained (tags opened and closed on the same line), so `chunk_lines` splits only between lines and every chunk is balanced. `links` ((label, url) pairs) go to `prepare_keyboard(posting_id, contacts, links)`, not into the text, and unsafe URLs are dropped. Contacts are mappings with id, contact_type, type_confidence, name, role, source_url. Add-contact button is `ac:<posting_id>` (matches 8.1.14), always present. Fit points render under the `Fit:` line without a heading, and keywords list only the keyword. `unreachable` adds a one-line note (PLAN signature required the argument).
- **D-128 P4 /prepare handlers.** `resolve_ref` returns `Ref(posting_id, error)`; refs are lowercased, must be hex, and prefix lookup is an index range scan on `postings.id`; an exact full id wins over longer matches. `/prepare <ref>`, the regex command `prepare_<16hex>` (aiogram `Command(re.compile(...))`) and the `pr:` callback share `run_prepare`, which replies via the already-whitelisted incoming message (so only `TELEGRAM_CHAT_ID` receives output; the whitelist middleware is unchanged). The callback answers before any client call. The `prepare_keyboard` (with its `ac:` Add contact button) is deliberately not attached in P4, because `ac:` has no handler until N7 and a dead button is worse than none; N6/N7 attach it. `/saved` appends ` /prepare_<id>` to the title line; `/start` lists `/prepare`. `test_keyboard` in test_format.py changed: like-active now yields two rows by design.
- **D-129 N1 contacts store details.**
  - Schema is v6 (`contacts`). `add_contact(conn, posting_id, RawContact, *, source_kind, source_url, company, now, ttl_days=60, max_per_posting=10)` returns the new id, or None for an empty name, a duplicate `(posting_id, name, source_url)` or a full posting (the cap is checked before insert). The caller passes config values; N6 reads them from `OutreachConfig`.
  - Only name, role, company and note go through `redact`; `source_url` is kept only if http(s) (else `''`), so URLs are never digit-redacted. Redaction covers emails (including `[at]` forms), phone-like runs with 7 or more digits (ISO dates are left alone) and any bare 7-digit run.
  - Deadline for expiry: `postings.deadline`, else `scores.deadline`. `expires_at` is `min(now + ttl, midnight after the deadline)` in the clock's timezone. A past deadline yields an already-expired contact.
  - `contacts_for` and `prune_contacts` compare parsed datetimes, not strings, so mixed offsets stay correct. `db.prune` returns an extra `contacts` count. Nothing logs contact fields, only counts.
  - `classify_role(role, context)` uses context only when the role is empty, with confidence capped at 0.5. "manager" gets 0.8 with an engineering/research/data/software/technical/product/team/ml/ai prefix, else 0.6.
- **D-130 N2 blocked hosts confirmation (D-104).** `BLOCKED_HOSTS` and `is_blocked_host` live in `src/http.py`; the check runs in `get_text` (before cache, robots and network), at the start of `_fetch` and on every redirect hop before its robots check, so a blocked host never gets a request or a robots fetch. It logs `blocked host` at INFO and returns None. The disabled Devpost source (D-50) is now unreachable through the client by design; `tests/test_source_devpost.py` lifts the block with an autouse monkeypatch so its parser tests still run. `links.py` returns `linkedin_search_urls(company, title)` as two `(label, url)` pairs (recruiter, then the team term) and `yc_company_url(company)` (None for an empty company); keywords are percent-encoded with `%20` and clipped to 120 chars, and every URL passes `safe_url`.
- **D-131 N3 posting-text parser details.** `parse_posting_contacts(text)` works line by line (HTML input goes through `html_to_text` first), matches only the explicit patterns, and requires 2-4 capitalized Latin/Cyrillic words (hyphenated parts allowed, atomic so a fifth capitalized word rejects) with no digits or `@`. A small generic-word list (Human, Team, Resources, Our, ...) rejects department phrases. Roles are kept only if at most 6 words with no digits or `@`; the same person found twice keeps the first non-empty role. A published email or Telegram handle adds the note "posting lists a direct contact address; see the posting" to the first named contact only; with no named contact nothing is returned (RawContact needs a name, and a nameless row would be noise), so "Contact us at jobs@x.com" yields []. At most 10 contacts.
- **D-132 N4 HN comment parser details.** `parse_hn_comment` accepts only a dict with a non-empty string `text` (deleted comment -> []); the author contact is created only if the handle matches `[A-Za-z0-9_-]{1,40}` (anything else is dropped, so a hostile handle never reaches the store). Self-description roles: co-founder, founder, CTO/CEO/COO, "head of X", "I'm hiring" (hiring manager), Russian основатель/сооснователь, else `poster`. The comment HTML goes through `html_to_text` once, then N3 runs on the text; N3 contacts duplicating the author name are skipped. `fetch_hn_contacts` validates the id as digits, makes one `get_json(ttl_s=3600)` with default robots checking, and returns [] on any exception.
- **D-133 N5 team-page parser details.** `parse_team_page(html, page_url)` uses BeautifulSoup `html.parser` (no new dependency). Order: schema.org microdata, JSON-LD (walked recursively, so `@graph`, `employee`, `member`, `organizer` are covered; broken JSON blocks are skipped), then cards, with script/style/noscript removed before cards. Card names must be 2-4 capitalized words (N3's word pattern and generic-word list), must not contain nav words (contact, sales, about, ...) and must not sit inside `<nav>`; the role is the next sibling's text (leading dashes stripped, <= 120 chars) and must hit the classifier lexicon (type other fails) or organizer/coordinator/chair/mentor/host. Organizer note is used for JSON-LD under an `organizer` key and for cards on a page with an "Organizers" heading. Structured names need 2-5 words, no digits or `@`; dedupe by casefolded name, max 20; emails (`mailto:`, `itemprop=email`) are never read, and every field goes through `clean_field`. Any parser exception returns [].
- **D-134 N6 discovery orchestrator details.**
  - `discover_contacts(conn, http, posting_row, cfg, clock, weights) -> DiscoveryReport(added, unreachable)` runs posting text (offline), then the HN comment for `source == "hn"`, then pages. It is called only from `run_prepare`, before tailoring; a grep test keeps it out of `src/pipeline/`, the scheduler and alerts. `http=None` (the default for tests and any caller without a client) means offline sources only, and then no "discovered" stamp is written.
  - Page targets: watchlist `domain` (matched with `normalize_company`), else the posting host unless it is an ATS/aggregator/blocked host; paths `/team` then `/about`, stop at the first page that yields a stored person. For hackathon/competition kinds: the posting URL (source kind `organizer_page`), then `/team` on that host. Event pages on aggregator hosts (mlh.com, mlh.io) are skipped on purpose: the organizer there is the listing site, not the event.
  - Caps count attempts, not requests: each page try (including a robots refusal or cache hit) counts toward `max_page_fetches_per_prepare` (2) and the day counter `contacts:pages:<Almaty day>` (20). A robots.txt fetch is not counted as a page. If the daily cap blocks a fetch, the posting is not stamped `contacts:discovered:<id>`, so it can run again tomorrow; otherwise the stamp is written (even with 0 results) and blocks rediscovery for `rediscover_after_days`.
  - 403/429 stop: `PoliteClient.get_text` only returns None on failure, so `PoliteClient.denied` (a set of hosts that answered 401/403/429) was added; discovery clears the host before a fetch and stops if it is set afterwards. Nothing is retried or evaded.
  - D-128 resolution: `/prepare` now attaches `prepare_keyboard`, but with URL buttons only (LinkedIn recruiter and team-term buttons, plus a YC companies button for `hn` and "HN Jobs" RSS postings), on the last chunk. `prepare_keyboard` gained `add_contact=True`; N6 passes False and no contacts, so no `dr:`/`ac:` callback buttons exist until P6/N7 add their handlers (N7 should switch them on). Contacts are listed in the text with type and confidence.
  - `build_dispatcher` and `main._serve` pass the shared `PoliteClient` as `http`; handlers take it as an optional last argument. Only counts are logged.
- **D-135 N7 /contacts and /addcontact.**
  - `parse_pasted(text)` strips all URLs from the body first (only the first `linkedin.com/in/...` is kept, as `source_url`, never fetched; a scheme-less one gets `https://`). Short form: one line `Name - Role` / `Name, Role at Company`, or two lines (name, then role). The name must be 2-5 capitalized words with no digits or `@`. More than 2 lines or 300 chars goes to the cheap model. A URL-only paste gets the fixed "I never open LinkedIn links" reply and keeps the pending input, as does an unparseable short paste.
  - LLM extraction (`extract_contact.md`, `wrap_untrusted("profile", ...)`, reserved tag already covered) grounds name and role as `norm` substrings of the pasted text, else the paste is rejected and nothing is stored; company and note are kept only if grounded, else dropped (a model summary could carry injected text). The contact type is always `classify_role(role)`; the model has no type field. Residual risk accepted: if the user's own pasted text contains the word "founder" in an injection line and the model returns it as the role, the contact is typed founder; the user pasted it and sees the type in the reply.
  - Short-form company is the posting's company (the role keeps any "at X" text). LLM company replaces it when grounded.
  - `/contacts <ref>`, `/contacts_<16hex>`, `/addcontact <ref> [text]` and `ac:<ref>` share `store_pasted_contact`. The `ac:` button is now attached by `/prepare` (always, even with no link buttons) and `/contacts`; `dr:` buttons stay off until P6. Pending text is consumed by a separate filter (`pending kind == contact` with a posting id), so non-command text with no pending input, or an expired one, is ignored with no LLM call. Output is bot replies to the whitelisted chat only; nothing is ever sent to the contact.
  - Fixed a latent bug: `START_TEXT` is sent in HTML mode and contained a literal `<id>`; it is now `&lt;id&gt;`.
- **D-136 P5 draft service details.** Schema is v7 (`outreach_drafts`). `draft(conn, client, contact_id, cfg, clock, budget)` returns `DraftResult(contact_id, posting_id, cv_version, body, hook, ask, removed, cached)` or `DraftError(kind)` with kind `no_cv|no_contact|budget|api|invalid|unsupported` (missing and expired contacts both give `no_contact`). Any failed `verify_draft` verdict (shape, hook quote, length, ask, too few sentences) triggers exactly one regeneration whose correction lists unsupported terms and the reason only, never CV text; a second failure caches nothing. The stored `body` is the verified body (bad sentences dropped), and `hook` stores the verified quote, not the model's hook text. The contact type and its guidance line sit in the user text outside the untrusted blocks (the type comes from our classifier) and the full per-type guidance is in `draft.md`. The cached tailoring result contributes only verified keywords and emphasize texts. Hooks are registered at import of `draft.py` (idempotent); `outreach_handlers` does not import it until P6. Fixture trick: the fabricated reply's "Jane Street" sentence also contains "led" so the deterministic inflation check catches it; the verifier alone cannot detect an invented employer name, which is why the prompt forbids it and the claims list is checked.
- **D-137 P6 /draft handlers.** `/draft <id>`, `/draft_<digits>` and `dr:<id>` reply through `message.answer` in the whitelisted chat (one message: header line, newline, `<pre>` escaped body, clipped to 4000 chars total); no `send_message` in outreach code, and a grep test forbids send paths. Unknown/expired contact is checked before the CV check, so `/draft 99999` always says "Contact not found or expired." Draft buttons are now on in `/prepare` and `/contacts` (up to 5). Hooks: `outreach_handlers` imports `draft` and also calls `register_hooks()` for prepare and draft before `save_cv` and `forget_cv`, so they are registered at startup via `src.bot.app` (subprocess test) and survive test registry clears. `/start` lists /draft.
- **D-138 P7 privacy sweep details.** `tests/test_privacy_sweep.py` runs CV ingest -> `/prepare` (MockTransport team page) -> `/addcontact` -> `/draft` at DEBUG with canaries in the CV, posting, team page and draft reply, and also asserts stored contact names, draft bodies and CV names never appear in `caplog.text` (stronger than the canary strings alone). No leak was found, so no `src/` change. The temp-file check uses a chdir to an empty tmp dir plus an unchanged `git status --porcelain --ignored` of the repo. The `FAKE FIXTURE` rule covers CV documents and `contacts/` fixtures; `cv_parse_reply.json` is a canned model reply of structured fields (not a CV document) and is exempt. `cv_scanned_fake.pdf` has no text layer, so the marker was added as a trailing PDF comment (tests still pass). The send-path grep also covers `src/cv/` and forbids `send_document`/`forward_message`.
- **D-139 F1 README and DoD.** README gained "Employer-region weights", "CV, prepare and drafts" and "Contacts" sections, an outreach DoD status block (checked by a new `tests/test_readme.py` test against the SPEC section) and user actions 7 (install, watchlists, CV, optional SCORING_MODEL) and 8 (capped smoke run, listed as a user action because F2 has not run). The D-92 eval re-run was appended to step 6. Statements were checked against `src/config.py`, `src/bot/app.py`, `config/weights.yaml` and D-98..D-138.

- **D-140 FIX-15 entity grounding.** `verify.py` now extracts name-like tokens per sentence (capitalized words, 2+ capital acronyms, CamelCase, Cyrillic capitalized words) and checks each token whole-word against a corpus. Per-word, not whole-phrase, so "Sample Labs Research Team" is not a false positive but "Jane Street" and "MIT" are caught. Skipped: `ENTITY_STOP` (greetings, articles, weekdays, months, a few geography words and degree acronyms), skill-lexicon terms and CV skills, "I", single letters, and a lone sentence-initial capital (an ordinary word). Corpora: tailoring fit/emphasize/cut/reorder (via `claim_ok`, also draft claims) use the CV only; gap `how_to_address` uses CV + posting; draft sentences use CV + posting + contact. Drafts also drop first-person completed-experience claims (past verbs, "my internship/degree at", "I hold an MSc from") whose entity is only in the posting or contact; questions and intent ("would love to join", "was excited to") are exempt. Cyrillic words match by stem (drop last 2 letters, min 4) so "Яндексе" is grounded by "Яндекс". The ask number exemption is now call length only (`\d+ min(ute)s`). C2 `_Grounder.ok` uses `has_term` whole-word matching. Residual risks: an invented lone sentence-initial name ("Citadel hired me.") is not seen; a lowercase invented name is not seen; the past-claim pattern is regex-based and covers common phrasing only, so unusual wording of a posting-only employer claim can pass (the entity is still grounded in the posting, and the prompt forbids it). Fixture: `entity_probes.json` (fictional, fabricated sentences have no number, inflation word or skill term).

- **D-141 FIX-16 SDK logger pin (D-111 addendum).** New `src/logging_setup.py::configure_logging(level)` does `basicConfig(level)` then sets `anthropic`, `httpx`, `httpx2`, `httpcore`, `aiogram.event` and `telethon` loggers to WARNING whatever `LOG_LEVEL` is; `main()` calls it. Logger-level pin chosen over a filter because it also skips building the record and covers child loggers (`anthropic._base_client`). `aiogram.event` only (not all of `aiogram`) as in the task, so aiogram INFO elsewhere is kept. `tests/test_logging_setup.py` uses the real `AsyncAnthropic` on `httpx2.MockTransport` at DEBUG for `call_json` and for ingest + `/prepare`, a control test proving the SDK leaks the canary without the pin, and a check that `src.*` DEBUG logs still appear. README `LOG_LEVEL` row notes the pin.

- **D-142 FIX-17 /draft shows removed count (D-137 amendment).** `render_draft(..., removed=0)` appends `\nRemoved N unsupported statements.` after the `</pre>` when `removed > 0`; the clip loop includes the note in the 4000-char budget, so only the body is clipped. `run_draft` passes `DraftResult.removed` (also for cached drafts, which store it). With `removed == 0` the output is unchanged.

- **D-143 FIX-18 /contacts does not discover (option b).** Chose to correct the docs, not wire discovery into `/contacts`. Reasons: `/contacts` is a cheap read of stored rows (PLAN 8.1.14, existing tests); adding network fetches there would add a second path spending the daily page-fetch cap and rediscovery window, widening the on-demand surface for no user benefit since `/prepare` already discovers and `/contacts` shows the result. README "Contacts", D-103 and SPEC "Contacts" now say discovery runs at `/prepare` only. SPEC wording changed minimally to match shipped behaviour.

- **D-144 FIX-22 draft contact-info filter.** `verify_draft` drops any body sentence with a URL (scheme, `www.` or bare domain on a common TLD list), an email, an `@handle`, or a phone-like run of 7+ digits (year ranges like 2019-2023 exempt), counts it in `removed` and adds `contact-info` to `unsupported`. Fewer than 3 kept sentences hits the existing "too few" regenerate path. A domain followed by a path is always dropped; bare-domain detection uses a TLD allowlist so "Node.js" and "C++" are not flagged; the cost is that an unlisted TLD slips through, but a scheme/www/email/handle catches the common phishing shapes.

- **D-145 FIX-19 /saved and /applied are chunked.** `reaction_items` builds one string per item (title line plus URL line kept together); `_cmd_list` sends `chunk_lines(items, 4000)` as one message per chunk. An item whose URL would push it over 4000 chars drops the URL line rather than being split. `render_reaction_list` is kept (joined items) so short-list output is unchanged.

- **D-146 FIX-20 atomic migrations.** `migrate` loops over `_MIGRATIONS`, one explicit `BEGIN` ... `COMMIT` per step (`isolation_level=None` during migration, restored after; `ROLLBACK` on any exception, which also covers `PRAGMA user_version`, since SQLite makes it transactional). A failed step leaves the DB exactly at the prior version. Before a v1->v2 upgrade of a file DB the backup API writes `<db>.pre-v2` (gitignored by `data/*`); in-memory DBs skip it. Kept the backup because the rename is the one destructive step.

- **D-147 FIX-21 [outreach] validated at config load.** `OutreachConfig.__post_init__` checks types (str fields non-empty strings; int fields real ints, bool rejected) and ranges; `AppConfig.from_dict` builds an `OutreachConfig` from the table so an unknown key, a string `daily_llm_call_cap` etc. raise `ValueError` naming the key (`[outreach] <key> ...`) when the config loads, so the bot fails fast instead of silently failing each command. Limits are positive, except `daily_llm_call_cap` and the two page-fetch caps which accept 0 as an off switch (existing cap-reached tests use 0). `AppConfig.outreach` stays a plain dict and handlers keep calling `from_cfg`. README wording ("unknown keys are rejected") is now true; unchanged.

- **D-148 FIX-23 flock instead of PID check. Amends D-86.** `InstanceLock` opens the lock file `O_CREAT|O_RDWR`, takes `fcntl.flock(LOCK_EX|LOCK_NB)` and holds the fd for the process lifetime; the kernel drops it on any death, so PID reuse after a reboot or `wsl --shutdown` cannot block `run` (the systemd restart loop is gone) and two starters cannot both take a stale lock. The PID is still written for the message; a failed flock raises `AlreadyRunning` (same text, exit code 3 in `run`). `pid_alive` is removed. Release unlinks the file only while still holding the flock and only if it holds our PID; acquire re-checks that the locked fd is the file still at the path (inode) and retries, closing the unlink race. D-86's "known limit" (stale-looking live PID, manual removal) no longer applies.

- **D-149 FIX-24 contact discovery refuses local and IP-literal targets.** `src/http.py` `is_unsafe_host` (localhost, `.localhost/.local/.internal/.lan/.home.arpa`, IPv6 literals, and any host whose last label is numeric or `0x..`, which covers dotted, decimal and hex IPv4 forms) is checked in `PoliteClient._fetch` for the first URL and every redirect hop, so every source gets it; refused fetches return None with no request. `discover.py` `safe_target` additionally rejects explicit non-default ports (not enforced in the client because an existing test and legitimate redirects use :8443) and drops such targets before the fetch caps, counting each as unreachable. No DNS resolution is done (tests forbid DNS, and a resolve-then-connect check would still race rebinding), so a public name that resolves to a private address is not caught; accepted for a personal bot whose responses are only parsed for names.

- **D-150 FIX-25 rubric clarifies goal 3 (D-92 addendum).** One paragraph added to `src/scoring/rubric.md` under "Employer region": goal 3's "in Kazakhstan or remote" is about where the work happens (Accessibility) and the startup stage, not credit for a Kazakhstan HQ, which code adds via `kz_startup` +1.0. SPEC goals and `profile.md` untouched. `prompt_version` is a hash of profile.md + rubric.md, so it bumps automatically (no constant to edit; the test checks the clarification text and the 12-char hash). Cached scores carry the old version and are rescored on the next run. The eval re-run (PLAN section 3, README user action 6) is still a user action: not run here, no live API calls.

- **D-151 FIX-26 degree and title acronyms are grounded (D-140 addendum).** `msc, bsc, ba, ma, mba, gpa, ceo, cto, cfo` removed from `ENTITY_STOP`, so they must appear whole-word in the corpus like any entity (CV for tailoring claims and drafts-claims, CV + posting for gaps, CV + posting + contact for draft sentences). `hr, ui, ux, qa, kpi, ai, ml, api, cv` stay stop-listed (departments and generic tech words, not credentials a user could be accused of faking). Lowercase credential phrases the capitalized-token rule cannot see ("master's", "master of", "bachelor's", "vice president", "магистр", "бакалавр") get a small regex (`credentials()`), checked as a subset of the corpus. Cost: an honest "I am a bachelor's student" with only "BSc" in the CV is dropped (fail closed).

- **D-152 FIX-27 more fabrication phrasings (D-140 addendum).** (a) A lone sentence-initial capitalized word is now an entity when the sentence has a first-person marker (I, I'm, me, my, я, мне, ...), unless it is in a starter/function-word list (Previously, Could, Working, ...) or ends in -ly/-ing; so "Citadel hired me last summer." is caught when Citadel is not in the corpus. Cost: an unlisted ordinary starter word absent from CV/posting/contact drops an honest sentence (fail closed, regeneration). (b) Posting/contact-only experience rule (`is_past_claim`) gains "I am/I'm ... <role noun> at X", "former ... X", "X was where I ...", "X was my (previous) employer/school/...", and "my summer/year/time ... at X" (time nouns added to `_NOUN`); `_INTENT` still exempts wishes. (c) Tailoring gap `how_to_address` that is a second-person experience claim ("your internship at X", "your X internship") about a name only in the posting is removed (`is_second_person_claim`); naming the posting company without that claim is still allowed. (d) Cyrillic stem match is bounded to 3 trailing letters, so "Маркет" is not grounded by "маркетинг" and "Каспи" not by "каспийский"; cost: nominative entity vs a CV form with 4+ letter ending (e.g. "яндексами") is dropped.

- **D-153 FIX-28 fewer false drops (D-125, D-140, D-144 addendum).** (a) `_INTENT` (the past-claim exemption for the text between the past verb and the preposition) gains impressed, inspired, struck, fascinated, intrigued, drawn, interested, curious, following/follow(ed), admire(d), motivated, so "I was impressed by the work at Acme Fake." is kept; "I worked at Acme Fake." is still dropped. (b) `claim_ok` takes an optional `posting_text`, passed only for tailoring `fit.points`: names missing from the CV but present in the posting are allowed unless `is_second_person_claim` says the text tells the user they have experience there ("your internship at X", "your X internship"). emphasize, cut, reorder and draft claims stay CV-only. (c) `has_contact_info(sentence, cv_n)` splits the old URL regex: scheme, `www.` or a domain with a path is always flagged; a bare domain on a listed TLD (Socket.IO, ASP.NET) is skipped only when it appears whole-word in the CV. Email, @handle and phone rules are unchanged, so the FIX-22 phishing repro is still dropped.

- **D-154 FIX-29 logger pin survives ANTHROPIC_LOG (D-141 addendum).** `configure_logging` now pops `ANTHROPIC_LOG` from the environment, imports `anthropic` (so its import-time `setup_logging()` has already run and cannot run again), then pins the quiet loggers to WARNING. Chosen over a handler filter because the root logger level and handlers stay untouched and the pin also skips building records. Tests: in-process (env set, SDK `setup_logging()` re-run, pin holds), a fresh subprocess with `ANTHROPIC_LOG=debug` (proves the import-order bug is gone), and the real-`AsyncAnthropic` `call_json` canary at `LOG_LEVEL=DEBUG` with the variable set. README `LOG_LEVEL` row notes it.

- **D-155 FIX-30 tailoring output is contact-info free (D-144 addendum).** `claim_ok` (fit points, emphasize, cut, reorder, draft claims) and the gap `how_to_address` check call `has_contact_info(text, cv)` and remove the item, counted in `removed`. The CV-named bare-domain exemption of D-153 applies, so "Socket.IO" in the CV is fine. The verbatim `posting_evidence` and `cv_evidence` quotes are not filtered (they are exact quotes the user can check). `tailor_reply_honest.json` is unchanged (existing prepare tests assert removed=0).

- **D-156 FIX-31 written-out numbers are numbers (D-125 addendum).** `numbers()` now also returns number words from `lexicon.NUMBER_WORDS` / `NUMBER_STEMS_RU`: two..twenty and thirty..ninety (mapped to digit strings, so "five" matches "5" in the CV), dozen(s), hundred(s), thousand(s), million(s), billion(s), half, twice, double(d), triple(d), quadruple(d), any `\w+fold` word, and Russian два..десять (nominative and genitive), тысяч*, миллион*, миллиард*, вдвое, втрое. "one" and ordinals are left out, so "One thing ..." and "third-year" stay. Every existing numbers() check (claim V2, gap V2, draft `allowed_nums`) picks it up. `call_length_numbers` runs the same lexicon on the word before "minute(s)", so "fifteen-minute" is a valid call length. Fail-closed costs: "half-day", "double major", "manifold" and "in two minutes" are dropped unless the same word is in the evidence/CV; "thirty percent" does not match "30%" (the `%` form is a different token). Reviewer-listed soft cases without a number word ("many", "several", "a couple of") are still not checked.

- **D-157 FIX-32 posting-only numbers are not the user's experience (D-109/D-125 addendum).** (a) Draft: a body sentence with a first-person marker (`_FIRST_PERSON`: I, I'm, me, my, я, мне, ...) is dropped when it contains a number (digit or number word, D-156) that is not in the CV. Exempt: the call length shared by the ask and that sentence ("15-minute"/"fifteen-minute"), and 4-digit years 19xx/20xx (dates such as "Summer 2026", not quantities of experience; cost: an invented year that is in the posting passes). Sentences without a first-person marker keep the old rule (number may come from CV, posting or contact), so "The posting mentions 3 years of C++." is kept. (b) Gap `how_to_address`: each clause (split on `; : . ! ?`) with a second-person marker (you, you've, your, ...) is dropped when it has a number not in the CV, same exemptions; "The role asks for 3 years of C++; point to the C++ parser." is kept. Fail-closed costs: "I noticed the posting mentions 3 years of C++." and "I am applying for the 2-month program" are dropped (first person plus a posting number); a gap tip that puts the posting number and "your" in one clause is dropped. Claim items (fit/emphasize/cut/reorder/draft claims) were already evidence-only for numbers.

- **D-158 FIX-33 experience at a posting-only company (D-152/D-153 addendum).** `is_second_person_claim` gains: "you (have/'ve) <past verb> ... at/with/for/in X" (mirror of `is_past_claim`, same `_INTENT` exemption), "you are/were ... <role> ... at X" (exempt when the span says candidate/applicant/fit/match), "X ... knows/knew/remembers/recognizes you", "X hired/employed/recruited/took on you", and "work"/"project(s)" join the second-person nouns ("your work at X"; kept out of the first-person `_NOUN` so "my interest in the work at X" is not a claim). `is_past_claim` gains the fronted form "(at|with|for|in) X[,] I (have) <past verb> ..." (Russian "в X я работал ..."), exempt when the rest of the sentence carries intent ("At X, I would be excited to ..."), and "X hired/employed/recruited/took on me". The entity may be followed by up to two words before the comma, so "At Acme Fake, I built ..." is caught even when only "Acme" is missing from the CV. Fit points, gaps and draft sentences all use both functions (fit points and gaps previously only the second-person one). Applies only to names that are absent from the CV and present in the posting/contact, so "At Sample Labs I built ..." and the `draft_reply_ok.json` shape stay honest. Fail-closed costs: "You are a research intern at X" style fit text is dropped even when the model means the role; "At X, I built ..." for a company that is both in the posting and the CV is kept (the CV grounds it). Note: `draft_reply_ok.json` still loses its second claim ("Studied Mathematics at Sample Institute") in claim_ok at HEAD, a FIX-15 sentence-initial bug fixed in FIX-34 (D-159).

- **D-159 FIX-34 imperative and claim-verb starters are not names (D-140/D-152 addendum).** `entities()` skips the sentence-initial token when it is in `_VERB_STARTERS | _STARTERS`, regardless of the next token. `_VERB_STARTERS` holds tailoring imperatives (highlight, mention, emphasize, stress, move, put, list, place, add, drop, cut, shorten, remove, keep, lead, show, note, say, and similar) plus CV-claim past verbs (studied, built, worked, cleaned, joined, interned, ...). Tokens after the verb are still checked, so "Highlight Jane Street internship first" reports Jane and Street. Side effect, found while testing: the second claim of `draft_reply_ok.json` ("Studied Mathematics at Sample Institute") was being removed at HEAD by the same bug, so `tests/test_bot_draft.py::test_draft_command_shows_escaped_pre` asserted `removed == 1` and the "Removed N" note on an honest fixture; it now asserts 0 and no note (the note path is still covered by `test_draft_command_reports_removed` and `test_render_draft_removed_note`). The fixture file itself is unchanged. Cost: a real name that equals a listed verb at sentence start ("Lead Bank", "Note ...") is not seen as an entity there when it is the first word.

- **D-160 FIX-35 doctorate and C-level titles; eval runner logging (D-151/D-154 addendum).** `_CRED` gains `doctorate`, `doctoral`, `chief <word> officer` and Russian `доктор* наук` / `кандидат* наук`, checked as a subset of the corpus like the other credential phrases (the match text is compared, so "chief technology officer" in the CV does not ground "chief executive officer"; "doctoral" does not ground "doctorate", fail closed). Folded into this commit by request: `src/evals/run.py` `main` now calls `configure_logging(LOG_LEVEL or INFO)` before the key check, so a shell with `ANTHROPIC_LOG=debug` no longer prints eval request bodies (D-154 spirit); test in `tests/test_evals_runner.py`.

- **D-161 Known limitations of the heuristic verifier after FIX-31..FIX-35 (recorded, not tasks).** The reviewer-listed gaps stay open by design (a deterministic check cannot close them without an LLM or a parsed CV timeline): (1) an invented lowercase or unnamed employer is not seen ("a quant researcher at a hedge fund"); (2) lowercase invented titles ("staff engineer") and lowercase invented names ("interned at google") are not seen; (3) temporal status is not checked ("I am a graduate of X" passes while the CV shows 2022 - 2026); (4) soft inflation with no number, number word, inflation stem or name passes ("I mentored junior interns at Sample Labs"; "several", "many", "a couple of" are not number words); (5) anaphora passes ("I know Acme Fake well from my internship there"); (6) bare domains on TLDs outside the `_BARE` list (for example `evil.top`) are not flagged as contact info. Summary of the fail-closed costs accepted this round: "I noticed the posting mentions 3 years of C++." is dropped (D-157); "half-day", "double major", "manifold" are dropped unless in the CV (D-156); a gap tip with "your" and a posting number in one clause is dropped (D-157); "You are a research intern at X" fit text is dropped (D-158); a name equal to a listed verb at sentence start is not seen as an entity (D-159); "doctoral" does not ground "doctorate" (D-160).

- **D-162 FIX-36 imperative gap tips with posting numbers (D-157 addendum).** `gap_claims_posting_number` now flags a clause (split on `; : . ! ?`) that has a number or number word absent from the CV when the clause is second-person (D-157) or starts with an imperative claim verb (mention, stress, highlight, claim, point to, present, frame, note, show, state, cite, describe, lead with, ...). It is kept when an attribution ("the role/posting/job/program/team/they/it asks/wants/requires/needs/mentions/expects/lists ...") occurs before the first such number in the clause. Years 19xx/20xx stay exempt. Fail-closed costs: "Mention 2+ years, as the role asks" (attribution after the number) is dropped; an imperative gap tip with any non-CV number ("Apply before 15 June") is dropped. Non-imperative clauses without "you/your" are unchanged.

- **D-163 FIX-37 singular written-out durations (D-156 addendum).** `numbers()` (via `number_words`) now returns "1" for "one/a/an/single/один" directly before year(s), month(s), week(s) or semester(s) ("over a year" matches through "a year"), "10" for "a/one decade(s)", and "1" for "your/my year at/with/in/on X" and Russian "год опыта". So "I have a year of Python experience" is checked like "1 year": dropped unless the CV has that number. "summer" and "term" are not units ("I spent a summer at Sample Labs" stays), and "One thing ...", "third-year" stay unchanged. Fail-closed costs: "once a week", "a month ago" or "a year ago" in a first-person draft sentence are dropped unless "1" is in the CV; a CV containing any "1" digit grounds them (numbers are compared as a set, not by unit).

- **D-164 FIX-38 "lead with" is not inflation (D-125 addendum).** `inflation_stems` removes "lead/leads/leading with" from the text before matching the stems, unless preceded by "team " or "tech " ("team lead with five reports" still counts). "led", "lead engineer", "I lead the team" and "I led a team" still match. This fixes the loss of "Lead with ..." emphasize/reorder items and gap tips. Cost: "I lead with X" as a self-claim is also masked, but still goes through the other checks.

- **D-165 FIX-39 README claims-check wording.** README "CV, prepare and drafts" now says the claims check is a deterministic heuristic that removes most invented employers, degrees, titles and numbers but cannot catch everything (names D-161), and tells the user to read every draft and tailoring tip before use. Pinned by `tests/test_readme.py`.

- **D-166 F2 live smoke test (2026-11-06).** Ran `MAX_SCORES_PER_RUN=20 python -m src.main once` against the real APIs, after backing up data/oppo.db outside the repo. Result: 8 sources, 0 failed, 22 new, 20 scored, 0 scoring errors, 0 alerts, 1 digest of 10 items, exit 0. All 10 digest items were intl_top_tier (IMC, DRW), so the KZ quota fell through to best-remaining. Region flags and the 4/4/2 split on a mixed batch were NOT observed live (only covered by tests). /cv, /prepare and /draft were NOT exercised live because they need an interactive Telegram session and a CV: they are covered by offline tests only. Not done: eval re-run after the D-92 rubric change (user action).

## 2026-11-07 — Audit: coverage and freshness (planner)

Branch `feature/coverage-and-freshness`, HEAD 1bcbb78. The facts below come from reading the code. The counts come from one read-only aggregate query on the local `data/oppo.db` (no CV, contact or `.env` data was read).

- **D-167 Audit A1: collect interval per source.**
  - **One job for everything.** There is a single APScheduler job, `collect_and_score`, on an `IntervalTrigger(hours=max(1, collect_hours))` (src/scheduler.py:56-76). Its interval comes from `COLLECT_CRON_HOURS`, default 3 (src/config.py:37, :63, src/main.py:167).
  - **No per-source schedule.** Every enabled source runs on every collect, all concurrently through `asyncio.gather` (src/pipeline/run.py:34-37). There are no per-source intervals, no tiers and no jitter.
  - **Same interval for all.** Greenhouse, Lever, Ashby, HN, hh, RSS, MLH, Jobicy, pagewatch and telegram_channels are all polled every 3 h. Telegram channels are only polled (src/sources/telegram_channels.py:154-214). There is no live listening.
  - **Response cache TTL.** Every source passes `ttl_s=3600`:
    - greenhouse.py:17, lever.py:16, ashby.py:14, hn.py:30, hh.py:14, rss.py:18, pagewatch.py:15, mlh.py:15 and jobicy.py:14 (Jobicy's terms say at most once per hour, D-58).
    - `PoliteClient.get_text` returns a cached body younger than the TTL without any request (src/http.py:281-290). Otherwise it revalidates with If-None-Match/If-Modified-Since (src/http.py:295-299).
    - Consequence for Part 2: a tier polled more often than every 60 min would only ever see the cache. Tier1 must bypass the TTL and always revalidate.
  - **Validator support is unknown.** Whether boards-api.greenhouse.io, api.lever.co and api.ashbyhq.com return ETag or Last-Modified has not been verified. D-88 only says "many ATS fetches answered 304".
  - **Response sizes.** Greenhouse is fetched with `content=true` (greenhouse.py:103). Ashby responses run 1-14 MB (D-43). Both are expensive to poll every 15 min if the host sends no validators.
  - **Worst-case latency today.** About 3 h from publication to collection, plus scoring, plus alert or digest.

- **D-168 Audit A2: new-posting detection.**
  - **first_seen_at already exists.** It has been `postings.first_seen_at TEXT NOT NULL` since SCHEMA_1 (src/db.py:28).
  - **It is set once.** `upsert_posting` sets it on INSERT only and never changes it. A re-sighting bumps only `last_seen_at` (src/db.py:204-235).
  - **Where it is used:** scoring order (newest first, src/db.py:252-258), duplicate precedence (src/pipeline/dedupe.py:29-31) and pruning (src/db.py:498-503).
  - **Missing pieces:**
    - **No freshness logic.** Alerts and the digest never read `first_seen_at`. Alerts fire on base score >= 9 or a deadline within 7 days, for any undelivered scored item (src/delivery/alerts.py:39-49).
    - **No baseline.** The first fetch of a new source or board makes every existing posting look "new" and alert-eligible.
    - **No latency measurement.** Nothing records the time from first sight to delivery.
    - **No content-change detection.** On a re-sighting, title and description are never updated (src/db.py:233).
  - **Latent bug.** `prune` deletes filtered and duplicate rows whose `first_seen_at` is older than 60 days, even when they are still listed (src/db.py:498-503). The next collect re-inserts them with a new `first_seen_at`, so stale postings would look fresh. Fix: prune on `last_seen_at`.

- **D-169 Audit A3: ATS timestamp fields.**
  - **Greenhouse.**
    - Each job carries `updated_at` and `first_published` (keys in the real-response fixture `tests/fixtures/greenhouse_jobs.json`).
    - The parser keeps only `updated_at`, as `posted_at` (src/sources/greenhouse.py:67), and drops `first_published`.
  - **Lever.**
    - Each posting carries `createdAt` in epoch milliseconds (fixture `lever_postings.json`). The public postings README (github.com/lever/postings-api) documents no timestamp fields.
    - The parser keeps `createdAt` as `posted_at` (src/sources/lever.py:61).
  - **Ashby.**
    - Each job carries `publishedAt`, documented as "ISO DateTime when the job was last published" (developers.ashbyhq.com/docs/public-job-posting-api). There is no `updatedAt`.
    - The parser keeps it as `posted_at` (src/sources/ashby.py:57).
  - **Inconsistent meaning.** `posted_at` means "updated" for Greenhouse, "created" for Lever and "last published" for Ashby.
  - **Where posted_at matters.** It drives the stale filter (`max_age_days`, src/pipeline/filters.py:64-70).
  - **Decision.** Repointing `posted_at` would silently change what counts as stale. The API publish time goes into a new column instead (D-185).

- **D-170 Audit A4: postings reaching the scorer per run.**
  - **Per-run cap.** At most `MAX_SCORES_PER_RUN` = 150 API calls per run (src/config.py:38, :64; src/main.py:68, :142). Retries count against it (src/scoring/scorer.py:81, :98-111), and calls run 4 at a time (scorer.py:22).
  - **Candidate selection.** Status `new` or `score_error` with fewer than 3 attempts, newest `first_seen_at` first, `LIMIT max_calls` (src/pipeline/run.py:77, src/db.py:252-258).
  - **Before the scorer.** Duplicates are marked, then hard filters and the title prefilter run (run.py:56-57).
  - **No daily cap on scoring.** At 8 runs a day the ceiling is 1200 calls a day.
  - **Shared budget bug.** The only daily LLM cap is outreach's `LlmBudget`. Its `used()` sums every purpose for the day (src/llm/budget.py:22-26), so reusing it for scoring as written would eat the outreach cap.
  - **Cap exhaustion looks like failure.** `is_total_failure` counts `skipped` (budget exhausted) as a failure (src/pipeline/scoring_alert.py:25-28). `once`/`collect` then exits 1 (src/main.py:74-76), so hitting a cap would fire "ERROR: scoring failed".
  - **Local DB figures (2026-11-07, about 10 collect runs).**
    - 4526 postings: 1049 scored, 2636 filtered, 793 duplicate, 48 new.
    - Filtered breakdown: irrelevant 1180, senior_only 994, stale 456, unpaid 5, spam 1.
    - Of the 1049 scored: mean base score 3.05; 709 <= 3; 875 below the digest floor of 5; 40 >= 7.5; 7 >= 9. So about 83% of scoring calls went to items that can never reach the digest.
    - No Kazakhstan employer was ever classified. Categories: intl_top_tier 342, intl_other 387, unknown 320.
    - hh and telegram_channels have never produced a posting (both credential-gated, D-47, D-69).

- **D-171 Audit A5: what the pre-filter lets through.**
  - **Scope.** The title relevance check applies to JOB and INTERNSHIP kinds only. Pagewatch and every other kind skip it (src/pipeline/filters.py:23, :74-78).
  - **The reported "ai"/"ml" substring issue is not present.** Keywords of 3 chars or fewer already match whole words only (filters.py:34-44, D-37), and a regression test exists (`tests/test_filters.py:60`, "Retail Assistant").
  - **The real leak is breadth:**
    - (a) Longer keywords match as prefixes and there is no negative list. Any title containing engineer, analyst, data, research or developer passes. Of the 1049 scored titles, 89 contain sales, recruiting, legal, marketing, design, finance, support or "solutions" words, and only 2 of those scored >= 5. Examples: "Technical Support Engineer", "Solutions Engineer", "Trade Support Engineer", "AI Solutions Program Manager, Finance".
    - (b) Whole-word "ai" passes any "AI ..." title. "ai" is in 131 of the titles scored below 3; "manager" is in 147.
    - (c) "Sr"/"Sr." is not a senior keyword (config.toml:151-155). 72 low-scoring titles contain "sr".
  - **Missing checks.**
    - No hard-blocker detection (citizens only, security clearance).
    - No cheap triage: every relevance-passing posting gets a full scoring call.

- **D-172 Audit A6: politeness and other latent issues.**
  - **Current politeness.**
    - A fixed 2 s gap per host, enforced by a per-host lock (src/http.py:89, :116-121, :195-207).
    - Retries at 1, 2 and 4 s; `Retry-After` is honoured, capped at 60 s (http.py:217-224).
    - No jitter, no global concurrency cap, no per-host configuration.
    - No backoff that survives across runs. The `denied` set lives in memory only (http.py:104, :209-210).
  - **4xx statuses are invisible to callers.** `get_text`/`get_json` return None for every 4xx (http.py:211-214). The hh source cannot tell a 400 (bad User-Agent) from a 401/403 (bad token).
  - **telegram_channels has two defects:**
    - It advances `last_id` inside `fetch`, before the postings are stored (telegram_channels.py:212-213). A crash in between loses those messages.
    - It swallows total failure, so a dead session is recorded as an ok run (telegram_channels.py:183-185).

## 2026-11-07 — Doc verification for new sources and tools (planner)

The planner checked each source once on 2026-11-07 using WebFetch/WebSearch. No code was written and no fixture was captured. The implementer still captures one real response per IN source and records a robots check of the exact path fetched (PLAN rules). The IN/OUT verdict below is final, so the terms do not need re-reading.

- **D-173 Workable: IN.**
  - **Endpoint.** Workable's help article "Using the Workable API to create a careers page" (help.workable.com/hc/en-us/articles/115012771647) lists the public endpoints `https://www.workable.com/api/accounts/{subdomain}?details=true` (plus `/locations`, `/departments`). They take no token; only the `/spi/v3` example uses a bearer token.
  - **robots.txt.** `www.workable.com/robots.txt` disallows only `/user_password_resets`, `/admin`, `/auth/google` and `/j/`. `apply.workable.com/robots.txt` has an empty `Disallow:`. Both carry `Content-Signal: search=yes, ai-input=yes, ai-train=no`.
  - **Terms.** The terms (www.workable.com/terms) are a Workable-Customer agreement with no clause on visitors, scraping or automated access.
  - **Use.** A board source in tier1/tier2, discoverable from `apply.workable.com/{slug}` URLs. The endpoint may redirect to `apply.workable.com/api/v1/widget/accounts/{slug}`, which is allowed (same robots).

- **D-174 SmartRecruiters: OUT.**
  - **Docs.** developers.smartrecruiters.com lists `GET https://api.smartrecruiters.com/v1/companies/{companyIdentifier}/postings`, but its Posting API pages describe API-key/OAuth access and do not invite anonymous third-party use.
  - **robots.txt.** `https://api.smartrecruiters.com/robots.txt` is `User-agent: LinkedInBot / Allow: /v1/companies/` then `User-agent: * / Disallow: /`. The API host explicitly excludes every other crawler.
  - **Decision.** The D-31 exception does not apply, so we do not fetch it. Config gets `enabled = false` with this reason.

- **D-175 Recruitee: OUT.**
  - **Docs.** Official docs (docs.recruitee.com/reference/authentication-1.md) say Careers Site API calls need an `X-Careers-Sites-Token` header: "Calls without the authorization header will return the `401 Unauthorized` error". The stated deadline for introducing the token is 10 February 2027.
  - **Tokens are per company.** Recruitee's help article (support.recruitee.com/en/articles/1066282) says the token is created by the company in its own settings.
  - **Decision.** Unauthenticated `{company}.recruitee.com/api/offers/` works today, but it is against the documented contract and will stop working within 3 months. Config gets `enabled = false`.

- **D-176 Personio: OUT (terms unreadable).**
  - **Feed.** The developer hub documents a public XML feed at `{account}.jobs.personio.de/xml` (developer.personio.de/docs/retrieving-open-job-positions; fields include `createdAt`). Tenant robots (`personio.jobs.personio.de/robots.txt`) is `Allow: /`.
  - **Terms could not be read.** Personio's legal pages (www.personio.com/legal-notice/, /terms-of-use/, www.personio.de/agb/) answered HTTP 429 to every fetch. A search found no visitor terms.
  - **Decision.** The PLAN new-source rule says unreadable terms means no fetching code (as for Kaggle, D-68). Config gets `enabled = false` with this reason. Re-checking later is a user action.

- **D-177 Remotive: OUT.**
  - **API terms.** The official API repo (github.com/remotive-io/remote-jobs-api, which remotive.com/api-documentation redirects to) invites use with conditions:
    - link back and mention Remotive;
    - "max. 4 times a day";
    - more than 2 requests per minute are blocked;
    - "Jobs displayed are delayed by 24 hours";
    - no resubmission to third-party job sites.
  - **robots.txt.** `https://remotive.com/robots.txt` has `User-agent: *` / `Disallow: /api/*`.
  - **Decision.** remotive.com is the main site host, not a dedicated API host, so the D-31 API-host exception does not fit. The 24 h delay and 4-per-day limit would also add nothing to freshness. Config gets `enabled = false`.

- **D-178 Remote OK: IN, with attribution.**
  - **API terms.** `https://remoteok.com/api` element 0 is the legal notice: "Please link back (with follow, and without nofollow!) to the URL on Remote OK and mention Remote OK as a source ... If you do not we'll have to suspend API access" and "don't use the Remote OK logo". The site legal page (remoteok.com/legal) asks API users to "link back with a web hyperlink or in-app hyperlink", and has no anti-scraping clause.
  - **robots.txt.** The `User-agent: *` groups are `Allow: /` and `Crawl-delay: 1`, and `/api` is not disallowed. The file has `Content-Signal: search=yes,ai-train=no,use=reference` and no ai-input restriction. We never train, so sending the text to the scorer is fine.
  - **Use.** Tier2, at most one request per run, a per-host minimum interval of 60 min.
  - **Mapping.** The posting `url` is the Remote OK job `url`, never `apply_url`. Every rendered item says "via Remote OK", and the logo is never used. Skip element 0. Job fields: id, epoch, date, company, position, tags, description, location, salary_min, salary_max, url, apply_url.

- **D-179 Arbeitnow: IN, with attribution.**
  - **Endpoint.** `https://www.arbeitnow.com/api/job-board-api` takes no key (arbeitnow.com/blog/job-board-api) and has a `visa_sponsorship` parameter.
  - **robots.txt.** `User-agent: *` / `Disallow:` / `Disallow: /*?__hstc`, so the API path is allowed.
  - **Terms** (arbeitnow.com/terms):
    - s.2: "for personal, non-commercial transitory viewing only".
    - s.11: "You also agree to providing a link back to Arbeitnow.com on your platform" and "We may revoke the permission to use API at any time".
  - **Use.** This bot is personal and non-commercial, and items link to the Arbeitnow URL with "via Arbeitnow". Tier2, a per-host minimum interval of 60 min, at most `max_pages` (default 2) pages per run.

- **D-180 We Work Remotely RSS: OUT.**
  - **robots.txt** allows the feeds.
  - **Terms are unreadable.** `weworkremotely.com/terms-of-service` and `/api-terms-and-guidelines` both answered HTTP 403 to automated fetches.
  - **What the search excerpt says.** WWR's API terms state that "scraping, copying, saving, or storing our data is strictly prohibited" and bar using the data to build job search services. Storing postings is what this bot does.
  - **Decision.** Not implemented. Config gets `enabled = false`.

- **D-181 Anthropic web search tool (platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool, read 2026-11-07).**
  - **Tool versions.** Tool `name: "web_search"`. Versions are `web_search_20250305` (basic), `web_search_20260209` (dynamic filtering) and `web_search_20260318` (adds `response_inclusion`).
    - On 20260209+ `allowed_callers` defaults to code execution.
    - Models without programmatic tool calling, which includes Haiku 4.5 per the models page, need `allowed_callers: ["direct"]` or the API returns 400.
    - We use `web_search_20250305`. It supports `claude-haiku-4-5-20251001`.
  - **Parameters.**
    - `max_uses` caps searches per turn; failed searches count.
    - `allowed_domains` or `blocked_domains`, never both, otherwise 400.
    - `user_location` (type approximate, city/region/country/timezone).
  - **Pricing and usage.** $10 per 1,000 searches plus token costs. Search results count as input tokens. A failed search is not billed. Usage is reported in `usage.server_tool_use.web_search_requests`.
  - **Failure modes.**
    - An org admin can disable the tool, giving a 400 `invalid_request_error`.
    - Errors inside a 200 response arrive as `web_search_tool_result_error` (`too_many_requests`, `max_uses_exceeded`, ...).
    - Long turns may stop with `pause_turn`.
  - **SDK shape** (anthropic 2.6.0 installed): `client.messages.create(model=..., max_tokens=..., messages=[...], tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": N, "allowed_domains": [...]}])`. Result URLs are in `web_search_tool_result.content[].url`.

- **D-182 hh.ru token type and User-Agent (api.hh.ru OpenAPI spec, read 2026-11-07).**
  - **User-Agent.** "В запросе необходимо передавать заголовок `User-Agent`, но если ваша реализация http клиента не позволяет, можно отправить `HH-User-Agent`. Если не отправлен ни один заголовок, то ответом будет `400 Bad Request`." (A User-Agent header is required, or HH-User-Agent if the client cannot set it; with neither, the API answers 400.)
    - The two are interchangeable; if both are sent, only `HH-User-Agent` is processed.
    - The value should name the application and a developer contact email. Example: `MyApp/1.0 (my-app-feedback@example.com)`.
  - **No anonymous search.** `GET /vacancies` (operationId get-vacancies): "Если не передан токен авторизации, то после первого запроса будет предложено пройти капчу". In other words, without a token the API demands a captcha after the first request. D-47 observed 403, and a Habr report says anonymous `/vacancies` has returned 403 since April 2026.
  - **Token type.** The security scheme is OAuth2 with both `authorizationCode` and `clientCredentials` flows (tokenUrl `https://api.hh.ru/token`). The application token (`grant_type=client_credentials`) "необходимо сгенерировать 1 раз" (is generated once). Requesting it again revokes the old one, and the response carries no `expires_in`. User tokens expire after 1209600 s (14 days) and need refresh.
  - **Decision.** `HH_ACCESS_TOKEN` must be an application token from an app registered at dev.hh.ru. We implement no OAuth refresh, so user tokens are unsupported. App registration may need hh moderation (Habr report); that is a user action.
  - **D-47 correction.** D-47's "terms" link `hh.ru/article/1175` is the search query-language page (the spec cites it for the `text` parameter). The real terms of service are at `dev.hh.ru/admin/developer_agreement`, which needs a login and was not read.

- **D-183 Triage model id check (platform.claude.com/docs/en/about-claude/models/overview, read 2026-11-07).**
  - **What changed.** The current Haiku is `claude-haiku-5-5`. `claude-haiku-4-5` (snapshot `claude-haiku-4-5-20251001`) is listed under "Legacy models (still available)", so it is valid but no longer current.
  - **Prices** (platform.claude.com/docs/en/about-claude/pricing, read 2026-11-07):
    - Haiku 4.5: $1 per MTok input, $5 output;
    - Haiku 5.5: $0.10 input, $0.50 output, for prompts up to 100k tokens.
  - **Decision.** Default `[triage] model = "claude-haiku-4-5"`, as the user specified. This also matches `SCORING_MODEL` and the outreach `cheap_model`, and is one config edit to change.
  - **Why not switch now.** Haiku 5.5 is about 10x cheaper, but it uses adaptive thinking (default effort medium). The JSON helpers do not set or test thinking or effort parameters, so switching untested could add thinking tokens or 400s.
  - **User action.** Run `src.evals.run --triage` with `claude-haiku-5-5`, then switch `[triage] model` and `SCORING_MODEL` if recall holds.

## 2026-11-07 — Coverage and freshness plan decisions (planner)

- **D-184 Task order: cost guards come first.**
  - **Order followed.** PLAN section 10 follows the requested order: migration, baseline, limiter/tiers, alerts, discovery, new sources, KZ, noise/cost, review.
  - **One deviation.** FC-02 (per-purpose LLM budgets, a daily scoring cap, "cap reached" kept separate from "scoring failed", and the cap warning) runs right after the migration, not in Part 5.
  - **Why.** Tier1 at 15 min with 150 calls per run would otherwise have no daily bound for a dozen tasks. The fixes are small and independent of the rest.
  - **Rest of Part 5.** Triage, negative keywords and hard blockers stay in Part 5.

- **D-185 Freshness semantics and schema v8.**
  - **Source of truth.** `first_seen_at`, which already exists (D-168), is the only freshness clock.
  - **API timestamps.** API publish times are stored in a new `source_published_at` column, filled with:
    - Greenhouse `first_published` (fallback `updated_at`);
    - Lever `createdAt`;
    - Ashby `publishedAt`;
    - hh `published_at`;
    - Workable, Remote OK and Arbeitnow publish dates;
    - the Telegram message date.

    It is shown only as context and used in /stats for "source lag" (`first_seen_at - source_published_at`). It never decides fresh, baseline or alert. `posted_at` keeps its current meaning, so the stale filter does not change.
  - **Schema v8:**
    - postings gains `target`, `source_published_at`, `content_hash` and `baseline`;
    - a new `target_state` table (per source+target: tier, baselined_at, last_ok_at, last_error, has_validators, etc.);
    - a new `host_state` table for persistent backoff;
    - deliveries gains `latency_s`.
  - **Backfill:**
    - `target` comes from the `external_id` prefix for board and channel sources;
    - every existing (source, target) is marked baselined;
    - every existing posting gets `baseline=1`, so the upgrade causes no alert burst;
    - `content_hash` stays '' (unknown). The first re-sighting fills it without counting as a change.
  - **Prune fix.** `prune` uses `last_seen_at`.

- **D-186 Targets and tiers.**
  - **Targets.** A target is one fetchable unit: a board token, channel, feed or page, or `""` for single-endpoint sources. Sources may expose `targets(ctx)` and `fetch_target(ctx, target)`. The old `fetch(ctx)` still works and means "all targets", for probe and existing tests.
  - **Tier assignment:**
    - tier1: config.toml watchlist boards (greenhouse, lever, ashby, workable);
    - tier2: discovered boards, hn, rss, jobicy, mlh, hh, telegram_channels polling, remoteok, arbeitnow;
    - tier3: pagewatch pages.

    `[sources.<name>] tier = "tierN"` overrides the default.
  - **Defaults (config.toml `[scheduler]`):**
    - tier1 15 min, jitter 60 s;
    - tier2 90 min, jitter 300 s;
    - tier3 60 min, jitter 300 s.
  - **Revalidation.** Tier1 fetches bypass the TTL and always revalidate.
  - **Hosts without validators.** A tier1 target whose last 200 response had neither ETag nor Last-Modified is fetched at most every `no_validator_min_minutes` (60).
  - **Greenhouse light polling.** Tier1 Greenhouse boards poll the list without `content=true` and fetch detail only for job ids not seen before, capped per run. The list body is a fraction of the content body.
  - **ToS floors.** A per-source `min_poll_minutes` floor protects terms when the user lowers a tier interval: jobicy 60 (D-58), remoteok 60, arbeitnow 60.
  - **One pipeline lock.** One process-wide asyncio lock covers dedupe, filter, (triage,) score and alerts, so two tiers never score the same rows. It also covers the digest and the board-adding writes of discovery.
  - **Collection runs outside the lock,** as do discovery validation requests and storing live Telegram messages. Tiers fetch disjoint targets, and the shared limiter keeps politeness, so a slow tier2 run never delays tier1.
  - **COLLECT_CRON_HOURS** is kept in `ENV_VARS` but documented as ignored. Agents cannot edit `.env.example` (D-03), and no new env vars are added in this plan.

- **D-187 Politeness limiter.**
  - **Configuration.** `[politeness]` in config.toml sets:
    - `default_min_interval_s` (2.0);
    - per-host overrides in `[politeness.hosts]`, matched on the host suffix, so `jobs.personio.de`-style tenant hosts can share one budget;
    - `jitter_fraction` (0.25 of the interval, added, never subtracted);
    - `max_concurrent_requests` (4, global semaphore);
    - `backoff_base_s` (300) and `backoff_max_s` (21600).
  - **Persistent backoff.** It lives in `host_state`. 429, 5xx after retries, timeouts after retries and 403 set `backoff_until = now + min(base * 2^(failures-1), max)`. A `Retry-After` above 60 s also sets `backoff_until` instead of sleeping. A 2xx or 304 resets it. A host in backoff is skipped, and the skip counts as a target failure for that run.
  - **Determinism.** Randomness and the clock are injected for tests.

- **D-188 Baseline mode.**
  - **What counts as new.** A (source, target) with no `baselined_at` is "new".
  - **First successful fetch.** The postings it inserts get `baseline=1`, then `baselined_at` is set. A failed fetch (None, all requests failed, host in backoff) does not complete the baseline.
  - **Treatment of baseline postings:**
    - they go through filters, triage and scoring like any posting, but `pending_for_scoring` and triage order non-baseline first, so caps are spent on new items first;
    - they may use at most `baseline_max_share` (default 0.5) of each daily cap, scoring and triage, through separate `score_baseline`/`triage_baseline` budget purposes. Ordering alone only helps within one run, and twenty discovered boards a day could otherwise fill the whole cap before an 18:00 posting arrives;
    - they are never sent as instant or fresh alerts;
    - they can appear in the digest, ranked after fresh items within a score band.
  - **Live Telegram messages** are never baseline. Their channel was baselined by its first poll, and a channel added at runtime is baselined by its first poll before the listener subscribes.

- **D-189 Fresh alerts, digest order, latency.**
  - **Fresh flag.** An item is fresh when `baseline=0` and `first_seen_at >= now - fresh_window_hours` (48).
  - **Fresh alert.** Fresh, and base score >= `fresh_alert_score` (7.5; base score, as SPEC line 42 requires for alerts), at most `fresh_alert_max_per_day` (5) fresh alerts per Asia/Almaty day, best base score first. Delivery kind is `fresh_alert`.
  - **Existing rules stay.** Base >= 9 or a deadline within 7 days, uncapped, kind `alert`.
  - **Overlap.** An item that meets an existing rule is sent under that rule and does not use the fresh cap.
  - **Never alerted:** baseline items and hard-blocked items in `lower` mode (D-195).
  - **Digest order.** Band = floor(adjusted score / `digest_score_band`), default 1.0. Order is band desc, fresh first, then the existing key. Quotas are unchanged.
  - **Latency.** `record_delivery` stores `latency_s = sent_at - first_seen_at` and logs one INFO line with kind, source, tier and latency (no posting text).
  - **/stats** (new) shows:
    - median and p90 latency over 7 days, overall and per source;
    - fresh alerts today against the cap;
    - scoring and triage calls today against their caps;
    - new postings in the last 24 h per tier;
    - discovered board count;
    - web-search spend this month.
  - **/sources** gains a tier column.
  - **SPEC.** SPEC's command list is user-owned (D-23), so /stats, /boards and /channels are documented in the README only.

- **D-190 Board auto-discovery.**
  - **Input.** Only already-collected data: posting `url` and URLs inside descriptions, at most 2048 chars each.
  - **Recognized URLs:**
    - Greenhouse: `boards.greenhouse.io/{t}`, `job-boards.greenhouse.io/{t}`, and `?for={t}` on `boards.greenhouse.io/embed/...`;
    - Lever: `jobs.lever.co/{s}`;
    - Ashby: `jobs.ashbyhq.com/{n}`;
    - Workable: `apply.workable.com/{s}`.
  - **Rejected URLs:** EU variants (`jobs.eu.lever.co`, `job-boards.eu.greenhouse.io`), which need different API hosts (recorded, not supported). Also rejected:
    - userinfo in the netloc;
    - explicit ports;
    - hosts that only end with or contain an allowed host;
    - non-http(s) schemes;
    - percent-encoded or non-ASCII tokens;
    - reserved first path segments (`embed`, `api`, `j`, ...).

    Tokens must match a strict per-ATS regex and are lowercased where the ATS is case-insensitive.
  - **Never fetch the extracted URL.** The canonical public API URL is rebuilt from the validated token.
  - **Validation.**
    - One polite request to that API URL (robots on, limiter on). Valid means 200 plus a schema-valid payload.
    - Candidates live in `board_candidates` (schema v9), with a 30-day negative cache.
    - Caps: `max_validations_per_day` 40, `max_new_boards_per_day` 20.
  - **Storage.**
    - Valid boards go to `data/discovered_boards.toml` (data/ is gitignored, so the file is per-install). Each entry holds source, token, company, discovered_from, discovered_at and removed.
    - Writes are atomic (temp file plus rename) by a small in-house writer that escapes all strings. Its output is round-trip tested with `tomllib`; no new dependency.
    - Company names are untrusted: escaped in TOML and HTML, clipped to 120 chars.
  - **Removal.** `/boards remove` tombstones an entry (`removed = true`), so it is never rediscovered.
  - **Tier and baseline.** Discovered boards are tier2 and get baseline on their first fetch.

- **D-191 Aggregator attribution.**
  - **Link and label.** Postings from remoteok, arbeitnow and jobicy (existing) link to the aggregator's own job page, never to `apply_url`. The rendered item (digest, alerts, /top) adds "via Remote OK", "via Arbeitnow" or "via Jobicy".
  - **Where it lives.** A `SOURCE_ATTRIBUTION` map in `src/delivery/format.py`, covered by tests. No logos are used.

- **D-192 Optional web-search discovery.**
  - **Off by default.** `[discovery.web_search] enabled = false`.
  - **Schedule.** Weekly cron (default Monday 06:00 Asia/Almaty), plus a manual `python -m src.main discover --web`.
  - **Call shape.** Each configured query is one Messages call with tool `web_search_20250305`, a configured `max_uses` (default 3) and `allowed_domains` set to the four ATS board hosts. Candidates come only from `web_search_tool_result.content[].url`, never from model text, and go through the D-190 extractor, validator and caps.
  - **Hard monthly cost cap.** `monthly_cost_cap_usd` (default 2.00) is enforced in code:
    - before each call, reserve the worst case: `max_uses * price_per_search_usd`, plus `max_input_tokens_estimate * input price`, plus `max_tokens * output price`;
    - if month spend plus the reservation exceeds the cap, the call is not made and the user gets one Telegram notice per month;
    - after the call, the actual cost (from `usage`) is recorded in `discovery_spend` (schema v9).
  - **Prices** are config values with defaults read from the pricing page on 2026-11-07: $0.01 per search (D-181), and $1 input / $5 output per MTok for Haiku 4.5 (D-183). The cap is only as good as these constants, so update them if the model changes.
  - **Not done (scope).** Aggregator `apply_url` values often point at ATS boards, but they are not stored and are not used for discovery.
  - **Errors.** `pause_turn` is not continued. A 400 "web search not enabled" disables the job for the month and notifies once.

- **D-193 Telethon live listening.**
  - **Channel list.** config.toml `channels` plus a runtime overlay in the `tg_channels` table (schema v10) that `/channels add|remove` edits. Runtime changes never write config.toml. A config channel can be hidden at runtime with a `removed` row.
  - **Joined channels only.** NewMessage events arrive only for channels the account has joined. The listener subscribes to the configured channels the account already belongs to.
  - **Joining is a user action.** It changes the user's own account, so `auto_join` defaults to false. Channels that are not joined stay on tier2 polling, which is also the fallback whenever the listener is disconnected.
  - **One client per session file.** In `run` mode the polling source reuses the listener's client, because two clients on one session file would contend for its SQLite lock.
  - **Ordering fix.** `last_id` is advanced only after the postings are stored (fixes D-172).
  - **Missing credentials or session.** The listener is not started, this is logged once, and /channels and /sources say why.
  - **Session files.** `*.session` and `*.session-journal` are already gitignored (`.gitignore:7`, `:16`, `tests/test_gitignore.py`).

- **D-194 Kazakhstan region hint.**
  - **Config.** `[sources.hh] region_hint = "kz"` and `[sources.telegram_channels] region_hint = "kz"` (defaults).
  - **Use in classification.** `classify_employer` gains a `hint` argument, applied only when no watchlist entry matches and the LLM region guess is below `region_min_confidence` or "unknown". It then returns `kz_employer`, or `kz_startup` if the LLM or kind says startup, with source "hint".
  - **What still wins.** A watchlist match or a confident LLM "intl" (for example, a foreign company posting on hh.kz). SPEC's headquarters rule is kept.
  - **No prompt change**, so no eval re-run is needed. The existing "bad KZ cannot outrank good intl" test must stay green.

- **D-195 Pre-filter and hard blockers.**
  - **New config lists:**
    - `negative_title_keywords` holds phrases that filter as `negative_title`. Bare "finance" is excluded because it would kill quant-finance titles; "finance operations" is used instead.
    - `protect_title_keywords` (quant, quantitative, machine learning, research scientist, data scientist) overrides the negative list.
  - **Seniority.** "sr" joins `senior_only_keywords` (whole word, so it matches "Sr.").
  - **Hard blockers.**
    - `hard_blocker_keywords` (citizens only, citizenship required, security clearance, ts/sci, active clearance, polygraph) is searched in title and description.
    - `hard_blocker_action = "lower"` is the default, because SPEC line 21 says flag work-authorization requirements and do not filter on them. In lower mode the posting gets `hard_blocker` text and a red flag, is never instant- or fresh-alerted, and its adjusted score is reduced by `hard_blocker_penalty` (2.0) in the ranking step. The base score is untouched.
    - `"filter"` is opt-in and filters before triage with reason `hard_blocker`.

- **D-196 Triage and caps.**
  - **When triage runs.** After the filters, for each `new` posting with no cached verdict for its (posting_id, content_hash). Pagewatch is exempt.
  - **The call.** One `call_json` to `[triage] model` with:
    - a short static system prompt (`src/scoring/triage.md`);
    - title, company and location;
    - the first `max_desc_chars` (500) of the description, wrapped with `wrap_untrusted`;
    - output `{"relevant": bool, "reason": str <= 120 chars}`.
  - **Verdicts.**
    - Yes sets status `triaged`, which is the only status (plus `score_error`) that `pending_for_scoring` selects when triage is enabled.
    - No sets status `filtered` with reason `triage_no`; the reason text is kept.
    - Errors leave the posting `new` with `triage_attempts + 1`. After 3, the status is `triage_error`.
  - **Cache.** Verdicts are cached in `triage_results` (schema v11). Scored postings are never rescored. A changed `content_hash` on a not-yet-scored posting triggers a new triage.
  - **Daily caps.** `[scoring] daily_call_cap` (default 200) and `[triage] daily_call_cap` (default 600) run on `LlmBudget` scoped to their own purposes. Outreach keeps its own 40.
  - **Cap warning.** Reaching a cap sends one Telegram warning per cap per day. It is not reported as a failure, and the CLI exit code stays 0.
  - **Total failure.** A run where every attempted triage call, or every attempted scoring call, failed (API or invalid) sends the existing throttled error notice, extended with the stage name.

- **D-197 Smoke test cap.**
  - **Cap.** The final smoke test allows at most 20 LLM calls in total for triage plus full scoring. This counts triage, so a cheaper call cannot be used to run past the user's "max 20 scoring calls".
  - **How it is enforced.** A scratch copy of config.toml (passed with `OPPO_CONFIG=...` on the command line) sets `[triage] daily_call_cap = 12` and `[scoring] daily_call_cap = 8`, and the command line sets `MAX_SCORES_PER_RUN=8`.
  - **Discovery.** Web search stays disabled. Board validation is capped at 3.
  - **Database.** As in D-166, back up `data/oppo.db` outside the repo, then run against it.

## 2026-11-07 — FC-01 implementation (implementer)

- **D-198 Schema v8 backfill details.**
  - **Source-run rows.** The `source_runs` backfill into `target_state` (target `''`) only covers sources that have no `target_state` row yet, so a board source such as greenhouse gets no junk `''` row next to its real boards, while credential-gated sources such as hh still get one.
  - **Tier.** `target_state.tier` is `''` after migration (tier is assigned by config at runtime, FC-05+).
  - **Old-migration test.** `test_migration_v5_to_v6_keeps_rows` re-runs v6 on an already-v8 database, so it now limits `_MIGRATIONS` to the first 7 steps via monkeypatch (re-running v8's ALTERs would fail).

## 2026-11-07 — FC-02 implementation (implementer)

- **D-199 Cost guard details.**
  - **Optional budget.** `Scorer` takes `budget` and `baseline_budget` as optional arguments, so direct `Scorer(...)` callers (tests, evals) are unlimited. `pipeline.run.score_pending` always builds both from `ScoringConfig` (default cap 200).
  - **Purposes.** Daily cap counts `score` + `score_baseline`; the baseline sub-cap counts `score_baseline` only and is `floor(share x cap)`. `OUTREACH_PURPOSES = (parse_cv, extract_contact, tailor, draft)` lives in `src/llm/budget.py`.
  - **Capped vs failed.** A call refused by either cap raises `BudgetExhausted` inside the scorer; the posting is left unchanged (no attempt recorded) and counted as `capped`. A cap hit on the retry after a bad first reply also counts as capped. The public `Scorer.score()` still returns None for that case.
  - **Run-limit skips.** Items skipped by the per-run `max_calls` stay `skipped` and are no longer a failure either.
  - **Ordering.** `pending_for_scoring` now orders `baseline ASC` first, then newest first, so a baseline backlog inside the SQL `LIMIT` window cannot crowd out new postings.
  - **Warning.** `cap_warned:score:<Almaty day>` state key, sent from `report_scoring_outcome` (only when a bot is given), independent of the failure throttle. It is sent for the sub-cap too, because those items are also counted as capped.

## 2026-11-07 — FC-03 implementation (implementer)

- **D-200 Publish timestamps and content hash details.**
  - **Hash location.** `content_hash` lives in `src/text.py` (not `dedupe.py`) because `db.py` needs it and `dedupe.py` imports `db`. Fields are NFKC-normalized, whitespace-collapsed and joined with a unit separator before sha256.
  - **Return type.** `Upsert` is a `StrEnum` in `db.py`. Callers compare with `is Upsert.INSERTED`; `collect_all` counts only INSERTED as new. The three existing `test_db.py` bool assertions were updated.
  - **CHANGED scope.** A changed hash updates title, location, description, content_hash and `source_published_at`, but not company, fingerprint, kind, url or deadline (only the fields listed in the plan).
  - **Target/baseline.** `collect_all` still calls with defaults (`target=""`, `baseline=False`); FC-04/FC-05 pass the real values.


- **D-201 FC-04 target plumbing choices.**
  - **Shared loop.** Target sources keep `fetch(ctx)` as `fetch_all_targets` (every target in turn, `RequestTally`, same all-failed error), so probe and old tests are unchanged. `fetch_target` raises on failure; `run_target` converts that to `ok=False`.
  - **rss.** The target key is the feed name, so feeds sharing a name form one target. One bad feed of the group does not hide the others (existing test shares names).
  - **telegram_channels.** `fetch_target` opens and closes one Telethon client per channel and raises on an unauthorized session or a read error, so that target is marked failed. The inter-channel pause is not applied in `fetch_target`. The legacy `fetch` is unchanged.
  - **source_runs.** `collect_all` runs a source's targets sequentially, sources concurrently. A source run is ok unless every target failed, and only a failed run stores an error (the first failed target's). Partial failures live in `target_state.last_error`.
  - **Tier.** `Target.tier` stays "" until tier assignment lands. `record_target_attempt` keeps the stored tier when given "". `baselined_at` is untouched (FC-05).

- **D-202** (2026-11-07): The user asked to shorten the plan. Cut FC-09 (Greenhouse light polling), FC-19 (record dropped sources), FC-21 (Arbeitnow) and FC-22 (web-search discovery). Reason: speed. FC-09 only saves bandwidth, FC-19 is already covered by D-174..D-177 and D-180 (plus a README note in FC-31), FC-21 is a second aggregator, FC-22 is optional and costs money. They are marked `[-]` in PLAN.md and are skipped by the build loop. Any reference in other tasks to web-search spend or light polling is moot.

- **D-203 FC-05 baseline choices.**
  - **Where.** `collect_all` reads `target_state.baselined_at` before storing a target's postings (missing row or NULL means new), inserts with `baseline=1`, records the attempt, then `db.mark_baselined` sets `baselined_at` only if it is still NULL. Failed results never reach it, so a failed first fetch stays new. A first ok fetch with zero items still baselines the target.
  - **Alerts.** `select_alerts` skips `baseline=1` rows before any rule. `undelivered_scored` already returns `p.*`, so it carries `baseline`; the digest does not filter on it.
  - **Tests.** The HTTP cache is cleared between collects in `tests/test_baseline.py`, since fixtures would otherwise be served from cache. Scoring order is tested after resetting status, because the fixture dates are stale against the fixed clock.

- **D-204 FC-06 config choices.**
  - **Cut keys.** FC-09, FC-19, FC-21 and FC-22 are cut (D-202), so no light-polling, arbeitnow or web-search keys; only the jobicy `min_poll_minutes = 60` floor is added now. `remoteok`/new aggregators add theirs when they land.
  - **Validation.** All new numbers must be positive (ints where a count or seconds, `default_min_interval_s` and `digest_score_band` may be floats); bools are rejected. Errors name the section and key. `[politeness.hosts]` values are validated as positive numbers, keyed by host suffix.
  - **Thresholds.** `Thresholds.__post_init__` validates only the new fields, so existing zero-valued thresholds stay legal.
  - **README.** The `COLLECT_CRON_HOURS` table row was updated to match the new `ENV_VARS` description.

- **D-205 FC-07 limiter choices.**
  - **Scope of a failure.** `record()` is called once per request run (after retries), not per attempt, so "three 429 runs" give +300/+600/+1200 s. 2xx/3xx (incl. 304) reset; 429, 5xx, timeouts/transport errors and 403 back off; other 4xx (404 etc.) leave the count alone. Backoff and `host_state` are keyed by lowercase hostname (no port); the interval rule uses the longest host suffix.
  - **Retry-After.** Above `RETRY_AFTER_CAP_S` (60 s) the run stops at once and records `backoff_until = now + min(max(exponential, retry_after), backoff_max_s)`; at or below the cap it still sleeps and retries.
  - **Back-compat.** With no `politeness` config, `PoliteClient` uses `min_interval_s` (0 allowed, for tests) as the default interval; without a db the backoff lives in memory only. `build_client` forwards `politeness`; `main.py` passes `cfg.politeness`. `collect_all` calls `limiter.new_run()` so the skip is logged once per host per run.
  - **Existing tests.** Hosts are shared (e.g. one ATS API host for many boards), so one board's 500 now backs the whole host off. Source isolation tests (ashby, lever, greenhouse, jobicy) list the failing target last; `test_retry_after_and_cap` expects no sleep above the cap; the baseline test clears `host_state` between its two collects.

- **D-206 FC-08 fetch choices.**
  - `PoliteClient.fetch` returns a `FetchResult` and never raises; `get_text` is a thin wrapper returning `.text`. The response cache is used when `ttl_s` is set or `revalidate=True` (revalidate without ttl still stores validators).
  - Board sources report `has_validators` through `SourceContext.validators[(source, key)]`, filled by the shared helper `fetch_board_json` in `base.py` and popped by `run_target` into `TargetResult.has_validators`. Reason: sources run concurrently on one ctx and `fetch_target` returns only postings, so this avoids changing its signature. `collect_all` already persisted `tr.has_validators`.
  - A 304 returns the cached body, which is parsed as usual, so `upsert_posting` bumps `last_seen_at`. Setting `ctx.revalidate` for tier1 is done by FC-10.

- **D-207 FC-10 tiered collector choices.**
  - **Tier of a target.** `[sources.x] tier` wins, then `Target.tier`, then the source's `default_tier` attribute (greenhouse, lever, ashby: tier1; pagewatch: tier3; everything else tier2). A tier1 default applies only to `origin == "config"` targets, so discovered boards fall to tier2.
  - **Floors.** `min_poll_minutes` is checked against the newest `target_state.last_attempt_at` of the source. A skipped source writes no `source_runs` row. The no-validator rule skips only a tier1 target that has a state row with `has_validators=0` and a `last_ok_at` within `no_validator_min_minutes`; a never-fetched target always runs. With FC-09 cut, tier1 greenhouse just revalidates (304 when unchanged).
  - **Lock.** `pipeline_lock()` returns one `asyncio.Lock` per running loop (a module-level Lock would bind to the first loop that contends and break across test loops). `run_tier_cycle` collects outside it, then runs process (dedupe + filters), score and alerts inside it. `collect_tier(process=False)` is what the scheduler uses; the default `process=True` runs `mark_duplicates` and `apply_filters` itself, as the plan says. `collect_all` runs the three tiers in turn, then processes once. The digest in `_serve` takes the lock.
  - **Scheduler.** `SchedulerDeps` loses `collect/score/alerts/collect_hours` and gains `run_tier` and `tiers`; jobs are `collect_tier1..3` with `IntervalTrigger(minutes, jitter)` and first runs at +0/2/4 min. `_run` (collect/once CLI) still uses `collect_all`. A source whose `targets()` raises is now logged and skipped (it used to record a failed source run).

- **D-208 FC-11 fresh alert choices.**
  - **Return type.** `select_alerts` now returns `[(item, kind)]`; existing-rule items (`alert`) come first, then fresh items (`fresh_alert`) best base first. Existing tests unpack the tuple and pin the base-score rules with `fresh_alert_score = 10.0`.
  - **Cap counting.** Today's `fresh_alert` deliveries are counted in Python by converting `sent_at` to the Asia/Almaty date (no timezone arithmetic in SQL). An item meeting an existing rule is sent as `alert` and never uses the cap.
  - **Header.** Fresh alerts use `<b>NEW</b>` instead of `<b>ALERT</b>`.
  - **Paused** sends nothing: `send_alerts` already returns early; no change.

- **D-209 FC-12 digest order choices.**
  - **Clock.** `select_digest` gains an optional `clock` argument (default `SystemClock`); `send_digest` passes its clock. Freshness is `baseline=0` and `first_seen_at >= now - fresh_window_hours`, same as alerts.
  - **Band.** Computed on the adjusted score; a non-positive `digest_score_band` falls back to 1.0 to avoid division by zero. Quotas are applied after sorting, unchanged.

- **D-210 FC-13 latency and /stats choices.**
  - **Latency.** `record_delivery` computes `latency_s = sent_at - postings.first_seen_at` itself and logs `delivery kind=.. source=.. tier=.. latency_s=..` once per new delivery (no posting text). Tier comes from `target_state.tier` (joined on source+target), `unknown` when absent.
  - **/stats scope.** FC-09/19/21/22 are cut (D-202), so the web-search spend and discovered-board lines of D-189 are dropped. There is no triage stage yet, so only scoring calls (`score`, `score_baseline`) are shown against `[scoring] daily_call_cap`. Percentiles use nearest rank. Postings without a `target_state` tier count under `unknown`.
  - **/sources.** Each line shows the source tier (`[sources.x] tier`, else the source default); greenhouse, lever and ashby add `(boards: tierN count, ...)` from `target_state` when known.
  - **FC-12 gap.** Already pinned by `test_fresh_first_within_band_but_band_wins` (fresh 7.2 above non-fresh 7.9 in the same band); no new test.


- **D-211 FC-14 extractor choices.**
  - **Casing.** Greenhouse and Workable tokens are lowercased; Lever and Ashby tokens keep their case (their boards are case-sensitive or unknown). Ashby also allows dots.
  - **Rejection.** Any `:` or `@` in the netloc is rejected (so `:443` too), as is any non-ASCII or control/space char anywhere in the URL, which also covers Cyrillic and punycode lookalikes. `..` tokens, `%` tokens and a wider reserved-segment list (`embed`, `api`, `j`, `jobs`, ...) are rejected. The embed `?for=` form is accepted on `boards.greenhouse.io` only.
  - **Text scan.** URLs are matched with a regex, trailing punctuation is stripped, and the first 50 matches are scanned (not the first 50 valid ones). URLs over 2048 chars are skipped.

- **D-212 FC-15 discovered-board store choices.**
  - **Format.** `data/discovered_boards.toml` holds `[[board]]` tables with `source`, `token`, `company`, `status` (`active` or `removed`) and `added_at`. The file is runtime data and already gitignored by `data/*`; tests use tmp paths (the snapshot config points at a non-existent fixture path).
  - **Lookup.** Tokens compare case-insensitively within a source, so a config token `acme` beats a discovered `Acme`. `tombstone` of an unseen board adds a removed entry so it is never discovered later.
  - **Targets.** `discovered_targets` in `src/sources/base.py` appends active boards as `tier2`, origin `discovered`, only when `[discovery] enabled`. A source is still enabled only when its config list is non-empty (unchanged `enabled()`), so discovered boards need at least one config board of that ATS; accepted to keep FC-15 small.
  - **Config.** `[discovery]` is validated (`DiscoveryConfig`): positive integer caps, `run_time` as HH:MM, unknown keys rejected.

- **D-213 FC-16 board validation choices.**
  - **Schema v9.** Adds only `board_candidates`. `discovery_spend` is dropped: it existed only for web-search discovery, which is cut (D-202). Version numbering is unchanged (v9 here, v10 FC-23, ...).
  - **Statuses.** `valid`, `invalid` (HTTP 400/401/403/404/410, bad JSON or wrong schema; negative-cached `negative_cache_days`) and `error` (network, 429, 5xx, robots: not an invalid verdict, retried the next UTC day). Both validation and new-board caps are counted per UTC day from `board_candidates.checked_at`, errors included.
  - **Cursor.** `discovery:last_rowid` advances only past postings fully handled; when a cap stops a run, the remaining postings are rescanned the next day.
  - **validate.** Each of greenhouse/lever/ashby gets `validate(ctx, token) -> (ok, company)` built on `base.validate_board` (one `PoliteClient.fetch`, robots and limiter on, canonical URL from the token). Lever and Ashby have no company name in the payload, so company is empty. Workable arrives in FC-18.
  - **Eligibility.** A ref is validated only if its source is registered, has `validate`, and is `enabled` in config; others are skipped and not cached.
  - **Lock.** Requests run outside `PIPELINE_LOCK`; the store write and candidate row are written inside it. A corrupt boards file means nothing is added and the candidate is not recorded.
  - **Scheduler and CLI.** `SchedulerDeps.discover` (optional) runs daily at `[discovery] run_time`; `python -m src.main discover` runs once and needs no Telegram/Claude secrets.

- **D-214 FC-17 /boards choices.**
  - **Owner check.** No new check: the `WhitelistMiddleware` drops every update from another chat before any handler runs, so `/boards` is owner-only like all commands (tested through the dispatcher).
  - **Numbering.** Numbers are positions among active boards in file order; they shift after a removal, so the list is re-read on every command. `remove` accepts a number or `source:token` (source lowercased, token case-insensitive) and only matches active boards. Anything else (bad or out-of-range index, unknown key, extra words) gets the usage line. Origin is always `discovered` because only discovered boards live in the file.
  - **Escaping.** Source, token and company are HTML-escaped and clipped, so each line is far below the chunk limit.

- **D-215 FC-18 Workable source choices.**
  - **Robots check.** Not re-run in FC-18 (the user forbids live calls outside the final smoke test); D-173 already records `www.workable.com/robots.txt` (disallows only `/user_password_resets`, `/admin`, `/auth/google`, `/j/`) and the empty `Disallow:` on `apply.workable.com`. `/api/accounts/{slug}` is not disallowed. The shared HTTP client still checks robots at fetch time.
  - **Fixture is hand-written, not captured.** `tests/fixtures/workable_account.json` was written from the documented response shape (D-173 and Workable help article 115012771647: `name`, `description`, `jobs[]` with `title`, `shortcode`, `employment_type`, `telecommuting`, `url`, `published_on`, `created_at`, `country`, `city`, `description`). It is labelled so in `tests/fixtures/workable_account.NOTE.txt` and in the test docstring. A real-response check is deferred to the final smoke test or to the user; if the real shape differs, `parse_jobs` and the fixture need adjusting.
  - **Endpoint.** `https://www.workable.com/api/accounts/{slug}?details=true` (the redirect to the apply.workable.com widget API is followed by the client's checked redirect logic). `validate` sends no query so the check is a single cheap request; valid means a JSON object with a `jobs` list, company = its `name`.
  - **Mapping.** `external_id = slug:shortcode`; entries lacking title, shortcode/code or url, or whose url host is not `apply.workable.com`, are skipped (hostile-url guard). `telecommuting` true gives remote and location "Remote"; otherwise "city, country". `published_at = published_on` else `created_at` (date strings read as UTC midnight). INTERN_RE on employment type or title gives INTERNSHIP.
  - **Enabled.** True when `[sources.workable]` is enabled and there is a config account or an active discovered workable board. With the shipped empty `accounts = []` it is therefore not counted in `test_source_count`, which is unchanged. The /sources fixture shows it as disabled.
  - **Not touched.** The `/sources` board-count SQL and the v8 migration list still name only greenhouse/lever/ashby; workable boards appear there in a later task if wanted.

- **D-216 FC-20 Remote OK source and attribution choices.**
  - **No live calls.** The robots check was not re-run; D-178 already records it (`User-agent: *` is `Allow: /`, `Crawl-delay: 1`, `/api` not disallowed). The shared client still checks robots at fetch time.
  - **Fixture is hand-written, not captured.** `tests/fixtures/remoteok_api.json` follows the shape in D-178 (element 0 legal notice, then id, epoch, date, company, position, tags, description, location, salary_min/max, url, apply_url). Labelled so in `remoteok_api.NOTE.txt` and the test docstring. A real-response check is deferred to the final smoke test or the user; if the shape differs, `parse_jobs` and the fixture need adjusting. It includes a hostile-host url, a title-less job, a non-dict element and an epoch-null job (date fallback).
  - **Source shape.** Own family `remoteok`, default tier2, `min_poll_minutes = 60`, `[politeness.hosts] "remoteok.com" = 5.0`. One GET of `https://remoteok.com/api` with no query, cached 1 h. No data (fetch failure) raises `SourceUnavailable`; a legal-notice-only array is an ok empty result. Jobs whose `url` host is not remoteok.com are skipped. The optional `tags` list is a local case-insensitive any-match filter. `published_at` = `epoch`, else `date`. `test_source_count` now expects the `remoteok` family.
  - **Attribution.** `SOURCE_ATTRIBUTION = {"remoteok": "Remote OK", "jobicy": "Jobicy"}` (no arbeitnow, FC-21 cut by D-202). `Item` gets a `source` field (filled from the posting row) and `render_item` adds a plain "via <name>" line before the URL, so digest, alerts and /top all show it. No logo.

- **D-217 FC-23 runtime Telegram channels choices.**
  - **Overlay API.** `channels(cfg, conn=None)` keeps the config-only behavior when `conn` is None (so `count_report` and `probe` are unchanged); with a connection it applies the `tg_channels` overlay. `config_channels(cfg)` is the config-only list. The 50 cap applies after the merge, so config channels come first and win.
  - **Enabled with runtime channels.** `TelegramChannelsSource.enabled/disabled_reason` take an optional `conn`. `pipeline.run` passes it for this source only, so a runtime-added channel is polled even if config lists none.
  - **Case.** Handles are matched case-insensitively for duplicate/remove checks; the stored spelling is the first one seen. Re-adding a removed channel flips its row back to `added`.
  - **Baseline.** Nothing extra is written: a new channel has no `target_state` row, so its first successful poll is its baseline (D-188). Remove then re-add keeps the old `target_state`, so it is not re-baselined.
  - **Status text.** Until the live listener exists (FC-24), status is "polling, baselined", "polling, first poll pending", or "not polling (<disabled reason>)". FC-24 should add "live".
  - **Existing migration tests** now compare against `db.SCHEMA_VERSION` instead of a literal.

- **D-218 FC-24 Telethon live listener choices.**
  - **Joined detection.** The listener reads `iter_dialogs()` usernames (case-insensitive) and subscribes `events.NewMessage(chats=...)` to configured channels in that set. `auto_join` (default false) calls `JoinChannelRequest` for the rest. Not-joined channels stay on tier2 polling.
  - **Debounce.** The first stored message starts a `live_flush_s` (default 30) window; later messages join the same batch (no trailing reset, so a busy channel cannot starve the flush). `on_new(batch)` then runs process, score and alerts under PIPELINE_LOCK via `run_tier_cycle("live", ...)`. Storing happens before and outside the lock.
  - **Cursor ordering.** `advance_last_id` is monotonic and runs after the store, for live events and for polling. Polling's `fetch_target` registers a callback in `SourceContext.after_store`; `run_target` carries it as `TargetResult.on_stored` and `_collect_tier` runs it after `upsert_posting`. The legacy `fetch()` (no store step) still advances after a full read.
  - **Second D-172 defect.** `fetch()` now raises `SourceUnavailable` for an unauthorized session or when every channel failed (flood wait counts as a failure), so `source_runs` is not ok. Existing tests were updated to expect a failed run.
  - **One client.** A module-level listener handle (`set_listener/get_listener`) lets the polling source reuse the listener's client (reconnecting it if needed, never disconnecting it). While the listener is disconnected `live_channels()` is empty, so all channels are polled. If `start()` failed, polling builds its own client as before.
  - **Missing credentials, session or authorization.** `start()` returns False, logs one warning with the reason, and keeps `listener.reason`; no Telegram message is sent (D-193: logged once, /channels shows the status). `/channels` shows "live" for live channels. `/channels add|remove` calls `listener.reload()`.
  - **Helper module.** `telegram_live.py` is not a Source and is excluded from the registry-coverage test.

- **D-219 FC-25 hh error texts and daily notice.**
  - **Mechanism.** `hh.fetch` uses `PoliteClient.fetch()` and on status 400/401/403 raises `SourceUnavailable` with the exact D-182 text at once (every query would fail the same way). `source_runs.error` therefore reads `SourceUnavailable: hh ...`.
  - **User-Agent.** `HH-User-Agent: oppo-tg/0.1 (<HTTP_CONTACT>)` from `Settings.http_contact` (not the generic client UA).
  - **Notice.** The collector has no bot, so the source stores the message in `state` (`hh_auth_error_pending`); `score_pending` (which already has bot and chat id) calls `notify_auth_error_once`, which sends at most one message per Asia/Almaty day (`hh_auth_notice_day`) and clears the pending flag. The notice can lag the failing run by one scoring step.

- **D-220 FC-26 hint plumbing.**
  - `recompute_adjusted(conn, weights, hints=None)` takes a source -> hint map built by `region_hints(cfg)` from `[sources.*] region_hint`. Digest, alerts and /top pass it.
  - A changed hint must trigger a recompute, but `weights.version` is a file hash. When hints are set, the stored `weights_version` is `version+src=hint,...`; with no hints it is unchanged.
  - Only the hint value "kz" is acted on; other values are ignored.

- **D-221 FC-27 negative-title filter details.** `negative_title` applies only to job/internship kinds and runs after `senior_only`, before `stale`. The list adds a few obvious phrases beyond the acceptance set (recruiting, ux designer, account manager, customer success, paralegal, accountant) and bare "sales", which is safe because `protect_title_keywords` (quant, quantitative, machine learning, research scientist, data scientist) overrides it. Hard-blocker config is not part of this task. The ai/ml regression is a test only; whole-word matching for keywords of 3 chars or fewer already existed.

- **D-222 FC-28 implementation notes.**
  - Detection runs in `apply_filters` (whole-phrase, case-insensitive match on title+description). Lower mode writes `postings.hard_blocker`; filter mode returns reason `hard_blocker` from `hard_filter`, after the other filters.
  - The penalty is applied in `recompute_adjusted` (new `blocker_penalty` arg, 0 in filter mode). The cache version now includes `+hb=<penalty>` and the stored `weights_version` is that full version, so changing the penalty or hints triggers a recompute.
  - Hard-blocked items still appear in the digest (with the lowered score and a leading "Hard blocker: <phrase>" red-flag line); only alerts (instant and fresh) skip them.

## 2026-11-07 — FC-29 implementation (implementer)

- **D-223 Triage implementation notes (D-196, D-183).**
  - **Wiring.** `pipeline.run.score_pending` takes an optional `triage_cfg`; `None` or `enabled=false` keeps the old flow (scorer selects `new`). With it enabled, `triage_pending` runs first, then the scorer runs with `triage=True` (`pending_for_scoring(..., triage=True)` selects `triaged` + `score_error`). `main.py` passes `TriageConfig.from_cfg(cfg)` in both the CLI and the scheduler path. Existing scoring-focused tests build no triage config, or disable it, so they stay about scoring.
  - **Reset on change.** `upsert_posting` CHANGED sets status back to `new` (clearing `filter_reason`, `triage_attempts`, `score_attempts`) for postings in `triaged`, `triage_error`, `score_error` or `filtered`. `scored` (and `duplicate`, `new`) are left alone, so a scored posting is never re-triaged or rescored. `filtered` is included so keyword filters and triage re-judge a posting whose title or description changed.
  - **Reply schema.** `TriageVerdict` forbids extra keys and uses `StrictBool` for `relevant`. An over-long `reason` is clipped to 120 characters instead of rejected, because rejecting costs a retry call for a harmless excess. `call_json` supplies the one correction retry.
  - **Prompt safety.** Title, company, location and the first `max_desc_chars` of the description go through `wrap_untrusted("posting", ...)`, which defuses reserved tags inside posting text. `triage.md` states that the posting content is untrusted data and must not be obeyed.
  - **Caps.** Purposes are `triage` and `triage_baseline`. The daily budget counts both; the baseline sub-cap counts only `triage_baseline` and is checked before the call, so with the baseline share used up a non-baseline posting is still triaged until the daily cap. A capped posting stays `new` with no attempt recorded. Order is non-baseline first, then newest.
  - **Failure and exit code.** Triage uses the existing notice with stage name "triage" and its own throttle key (`triage_failure_alerted_at`) and cap-warning key (`cap_warned:triage:<day>`). When every triage call failed and nothing could be scored, `score_pending` adds the triage errors to `errors` after the report, so `collect`/`once` exit non-zero without a second, duplicate scoring notice.
  - **/stats.** One line, always shown: `Triage today: yes N, no N, errors N, calls used/cap`. Yes and no count `triage_results` rows created today (Almaty day); errors is the current number of `triage_error` postings.
  - **Eval.** `python -m src.evals.run --triage` reports recall: of the labeled items with `expected_min >= 5` that pass the hard filters, the share triaged `relevant`. Default threshold 0.9, report in `evals/last_triage_run.json`. It uses the `[triage] model`, not `SCORING_MODEL`. Only tested offline with a fake client; the live run is a user action (D-183).

- **D-224 Stage-aware failure notice (FC-30, D-196, D-223).** `report_scoring_outcome` became `report_stage_outcome(stage, bot, ...)`; the FC-29 triage call now goes through it rather than a parallel path. The stage is the first argument and has no default, so a caller cannot silently fall back to scoring. The model hint in the notice is per stage: scoring names `SCORING_MODEL`, triage names "the [triage] model in config.toml" (the old text wrongly told triage users to check `SCORING_MODEL`). Throttle keys and the exit code were already per D-223 and are unchanged.

- **D-225 FC-31 README claims (D-202).** The README documents only what the code does, checked against `src/` and `config.toml`. Left out on purpose: web-search discovery and Arbeitnow (cut by D-202), so no config section or command is documented for them. The "User actions" list went to the end of PLAN.md section 10 (the plan said section 10 of this file). The dropped sources were added as rows of the Sources table (with D-refs) instead of a second table, because `test_registry_names_in_sources_table` already reads that table. The README says the Workable and Remote OK fixtures are hand-written and that the real-response check is pending; it does not claim the smoke test ran.

- **D-226 FC-32 review verdict, coverage and freshness (2026-11-07).**
  - **Verdict.** Not passed yet. Four blocking findings (FIX-40 to FIX-43) and eight non-blocking ones (FIX-44 to FIX-51), filed in PLAN.md section 11. FC-32 stays open until the blocking ones are fixed and re-reviewed.
  - **Checks that passed.** `pytest` exits 0 and ruff is clean. Migrations v8 to v12 are guarded, transactional and keep the data. Baseline is set only on an ok first fetch. The fresh-alert cap, digest bands, tier jobs, the lock split, floors, the limiter, conditional requests, discovery's canonical-URL-only validation, dropped sources and LinkedIn never fetched, caps reserved before each call including retries, and log and gitignore hygiene were all checked. The evidence is listed at the top of PLAN.md section 11.
  - **Why FIX-40 blocks.** It overturns the D-201 note that the per-channel `fetch_target` need not pause. With the per-target collector, polling no longer pauses between channels, no longer stops on a Telegram flood wait, and logs in once per channel. On `main` it did all three. The account at risk is the user's own, and FC-32 lists rate-limit behaviour as a review criterion.
  - **Why FIX-41 blocks although it predates the branch.** The scorer's hand-built `<posting>` tags (and the unwrapped feedback examples in the system block) were acceptable while scoring input came mostly from ATS boards. This branch sends public Telegram channel text, live messages, Remote OK, Workable and discovered-board company names to the scorer. A forged score of 9 or more becomes an uncapped instant alert. FC-32 item 4 asks for the untrusted wrapper on every model input, and triage already uses it.
  - **Why FIX-42 blocks.** Adding Remote OK and Workable without extending `AGGREGATOR_HOSTS` makes `/prepare` fetch the aggregator's own `/team` and `/about` pages and file those people as the employer's contacts. That breaks the SPEC contact-source rule and D-103.
  - **Why FIX-43 blocks.** The README offers `[triage] enabled = false` as the way out, and FC-29 accepts it as "restores the pre-triage flow". In fact it silently strands every posting triage had already marked relevant.
  - **Why the rest are non-blocking.** FIX-44 is bounded: fresh alerts are capped, and it needs a partial failure on a first fetch. FIX-45 and FIX-46 are edge configurations and rare headers. FIX-47 to FIX-51 are documentation, ignore rules, wasted validations and one missing credit line in two private lists.

- **D-227 FIX-40 Telegram polling: one client, pause, flood-wait stop (supersedes the D-201 sentence "The inter-channel pause is not applied in `fetch_target`").**
  - **Hook.** `_run_source_targets` hands the whole target list to `src.run_targets(ctx, targets)` when a source defines it. Only `telegram_channels` does; other sources are unchanged.
  - **One client.** `run_targets` opens the listener's shared client (never disconnected) or one owned client, checks authorization once, and closes the owned client once per run. `fetch_target` reuses that client while `run_targets` is active and still opens its own when called standalone (probe, `fetch_all_targets`).
  - **Pause and stop.** `pause_s` (default 1 s) is awaited between channels. A `FloodWaitError` in `fetch_target` records the seconds; the remaining channels get `ok=False, error="skipped: telegram flood wait Ns"` with no request and no `on_stored` hook, so their cursors stay. An open or authorization failure fails every target without a request.
  - **Not done.** Persisting a Telegram backoff across runs is optional in the task and skipped; the next tier2 run polls again from the stored cursors.

- **D-228 FIX-41 scoring prompt uses the untrusted wrapper.**
  - **Choice.** `build_user_message` now uses `wrap_untrusted("posting", body)`. The feedback examples stay in the second system block (keeps the cached static block and the block layout), but each section's lines go through `wrap_untrusted("examples", ...)`, a new reserved tag in `RESERVED_TAGS`, so a title containing `</posting>` or `</examples>` is defused. Title, company and tags are collapsed to one line so they cannot forge headings. `rubric.md` says the examples are untrusted data.
  - **Reason.** A dedicated tag means no extra `</posting>` appears in any system or user block, and no scorer change is needed.
  - **User action.** The prompt version changed (rubric.md edited). Re-run the scoring eval live before trusting the thresholds; not run here.

- **D-229 FIX-42 aggregator hosts for Remote OK and Workable (2026-11-07).** Added `remoteok.com` and `workable.com` to `AGGREGATOR_HOSTS` (suffix match covers `apply.` and `www.`). Guard test derives hosts from the board fixtures' url fields (lever, ashby, jobicy, hh, remoteok, workable). Greenhouse is excluded because its fixture urls include employer-hosted careers pages (a legitimate employer host), and Devpost because event subdomains are per-hackathon organizer pages by design; the remoteok bait url `evil.example` is skipped since the source rejects it.

- **D-230 FIX-43 triage off no longer strands triaged rows (D-196, D-223, D-226).** `pending_for_scoring(triage=False)` now selects `new`, `triaged` and `score_error`; `triage=True` still selects `triaged` and `score_error` only. Rows triage judged relevant before the user set `[triage] enabled = false` are scored on the next run with no triage call. `triage_error` rows are left alone, as before (they were never judged relevant). Turning triage back on leaves `new` rows to be triaged as usual.

- **D-231 FIX-47 README corrections (2026-11-07).** Workable config accounts documented as tier1, and discovered Workable boards as polled without a config account (D-215). The `/stats` triage error figure is relabeled "errors (all time)" instead of changing the query, since it counts every `triage_error` posting and a per-day count would need a new timestamp source. The README describes `[triage] daily_call_cap = 0` as the code behaves today (all postings `capped` while triage is enabled; use `enabled = false` to skip triage), because FIX-45 is not fixed this round. When FIX-45 lands, update that README sentence and its test.

- **D-232 FC-33 capped live smoke test (2026-11-07, D-197).** Run on branch feature/coverage-and-freshness against the real network, Claude API and Telegram, with a scratch config (triage cap 12, scoring cap 8, validations 3) and `MAX_SCORES_PER_RUN=8`. DB backed up to the scratchpad first; not restored (no corruption). Today's `llm_usage` was empty before the run.
  - **LLM usage (confirmed from `llm_usage`).** triage 7 + triage_baseline 5 = 12 (cap 12), score 1 (cap 8); total 13 of the 20 allowed. Triage: 1 yes, 11 no, 0 errors, 0 cached, 0 capped. Scored 1, 0 errors.
  - **`once`.** exit 0, 99 s. 9 sources ran, 0 failed (per tier: tier1 greenhouse, lever, ashby; tier2 hn, jobicy, mlh, remoteok, rss; tier3 pagewatch). 4557 items, 131 new postings, 108 filtered by the cheap filter, 0 duplicates. Targets: 27 in `target_state`, all baselined (tier1 14, tier2 6 across hn, jobicy, mlh, remoteok and 2 rss feeds, tier3 7), none with a last error. Two DNS errors (`www.mlh.com`, `hnrss.org`: "Temporary failure in name resolution") were logged as transient; those sources still reported ok (likely served from cache or earlier state). I did not investigate further and did not rerun.
  - **Alerts and digest.** 0 alerts (none by any kind). Digest of 10 items sent to the user's own chat. Digest latency figure logged was about 42,560 s (11.8 h) for all 10 items; these were postings first seen in earlier runs, so this is not a freshness measurement. No fresh-alert latency was measured live because no alert fired.
  - **`discover`.** exit 0. Scanned 3627 candidates, validated 3 (the cap), added 3 (lever auw, ashby equall, ashby Mastra), 0 invalid, 0 errors, 6 skipped.
  - **Remote OK.** Ran live and parsed a real response: 99 items, 99 new. That is the first real-response check of the Remote OK parser (D-216); no parse failure.
  - **hh and Telegram.** hh did not run (no source run row; it is credential-gated and I did not look for credentials). Telegram Bot API delivery worked (digest sent). The Telethon live listener and `telegram_channels` did not run (`once` does not start them); no polling session was started.
  - **NOT exercised live.** Workable (no configured accounts, no discovered Workable boards polled), hh, Telethon live listener and channel history, fresh alerts and their latency, the triage eval and scoring eval, the long-running scheduler and tiered cadence, the web search (cut, FC-22), /prepare, /contacts, /draft, button callbacks, systemd unit.


## 2026-11-08 — kz-first-fresh-cv planning (planner)

Branch `feature/kz-first-fresh-cv`, created from `main` at 9eb48fe. The brief is in `docs/briefs/kz-first-fresh-cv.md`, and the plan is PLAN.md section 12 (KF-01 to KF-38). Source and page checks were made on 2026-11-08 with an honest planner User-Agent (`oppo-tg-planner/0.1 (personal job-alert bot; ...)`), one request per URL, plus one repeat fetch for page stability. No `.env` was read.

- **D-233 Starting state (audit, 2026-11-08).**
  - **Branches.**
    - `feature/coverage-and-freshness` exists; its tip is `main` (9eb48fe), 0 commits ahead, so it is fully merged.
    - `feature/outreach-and-weights` exists at 1bcbb78, 0 commits ahead of `main`, so it is fully merged.
    - `feature/telegram-discovery` and `feature/linkedin-safe-ingestion` never existed: no local or remote branch, and no commit message mentions them.
    - Pieces the brief needs from them are built here: channel discovery, forwarded messages and `/add` (KF-21 to KF-24). LinkedIn stays human-assisted only (SPEC Contacts, D-104). Nothing fetches or automates LinkedIn, and this branch adds no LinkedIn ingestion.
  - **Tests and lint.** At the start, `pytest -q` is green and `ruff check` / `ruff format --check` are clean.
  - **Correction to the handover.** Not every PLAN.md task is checked. FIX-44, FIX-45, FIX-46, FIX-49, FIX-50 and FIX-51 (section 11) are still `[ ]`; FC-32 closed them as known limits (PLAN 10, FC-32 status line).
    - Several become more likely on this branch:
      - FIX-44: hh moves to tier1 with more queries, so a partial first fetch is likelier.
      - FIX-46: tier1 polls more often.
      - FIX-45: Part 9 caps.
    - They are folded into section 12: FIX-45 in KF-01, FIX-44 in KF-02, FIX-46 in KF-03, FIX-49 in KF-28 and FIX-51 in KF-16. FIX-50 is closed by this planning commit (D-264).
  - **Already built (verified by reading the code; KF tasks verify and close gaps, they do not rebuild):**
    - `first_seen_at` and baseline mode (D-188, D-203);
    - tiered polling with jitter (D-186, D-207), the per-host limiter and persistent backoff (D-187, D-205), and conditional requests (D-206);
    - fresh alerts at 7.5 with at most 5 a day (D-189, D-208), delivery latency, and `/stats` with median and p90 per source (D-210);
    - haiku triage with caps and the all-fail exit code (D-196, D-223, D-224);
    - board discovery, `data/discovered_boards.toml` and the 20 per day cap, plus `/boards` (D-190, D-212 to D-214);
    - Workable and Remote OK (D-215, D-216);
    - hard blockers and negative titles (D-195, D-221, D-222);
    - Telegram channel polling plus the live listener (D-193, D-217, D-218, D-227);
    - employer-region weighting and quotas (`src/ranking`, D-91 to D-97) and the KZ hint (D-194, D-220);
    - CV upload and parse (`src/cv`, D-120 to D-122);
    - `/prepare`, `/draft` and the verifier (`src/outreach/verify.py`), and the outreach daily cap.
  - **Gaps found while reading:**
    - `src/sources/telegram_live.py` still contains `_join` (JoinChannelRequest) behind `auto_join` (D-218). The brief forbids joining outright (D-256).
    - `src/http.py` checks robots with `urllib.robotparser`. It does prefix matching only and ignores Google-style `*` and `$` rules, so a rule like `Disallow: /*/search/*` (enbek.kz) or `Disallow: /api/*` (Remotive) would read as allowed. KF-04 fixes this.
    - There is no `/add` command and no search command.
    - Pagewatch pages carry only a `Kind`, which has no summer_school, grant or accelerator value.

- **D-234 Plan shape and order (section 12).**
  - **Order.**
    1. Close the open FIXes that this branch makes worse (KF-01 to KF-04).
    2. The cue file, because the pre-filter, hh, discovery and search all read it (KF-05, KF-06).
    3. Opportunity types (schema v13, triage v2, the post-triage step).
    4. KZ-first ranking.
    5. The scorer contract and the new item format, which together give an end-to-end slice of the new behaviour.
    6. Freshness and Telegram.
    7. KZ sources, then international sources.
    8. Search and stats.
    9. CV review.
    10. Caps and the handler sweep, then README, review and smoke.
  - **Size.** 38 tasks, each one commit.
  - **Verify tasks.** "Verify + close gaps" tasks (KF-02, KF-28, KF-30, KF-34) start by pinning the existing behaviour with a test, and only then change code.

- **D-235 enbek.kz: SKIPPED (robots).**
  - **robots.txt** (`https://www.enbek.kz/robots.txt`, `User-agent: *`) includes `Disallow: /*/search/*` and `Disallow: /*/%D0%B2%D0%B0%D0%BA%D0%B0%D0%BD%D1%81%D0%B8%D0%B8/*` (".../вакансии/...").
  - **The listing is disallowed.** The vacancy listing the home page links to is `/ru/search/vacancy` (also `/ru/search/vac`), which the first rule covers.
  - **No public API.** A web search (2026-11-08) found no public API or developer documentation for enbek.kz.
  - **No workaround.** We will not reach vacancies through the sitemap or by walking individual `/ru/vacancy/...` pages, because that would get around the listing disallow.
  - **Terms.** Not read; robots decides.
  - **Coverage.** Kazakhstan vacancies stay covered by hh (area 40) and Telegram channels.

- **D-236 Astana Hub (astanahub.com): SKIPPED (honest UA blocked).**
  - **Blocked.** `https://astanahub.com/`, `/ru/` and `/en/` answer HTTP 403 with a Cloudflare "Attention Required!" page to our honest User-Agent. `robots.txt` itself answered 200.
  - **Robots.** It also disallows `/api/` and `/search/`.
  - **Decision.** The section 0 rule says a site that blocks our honest UA is dropped and the block is never evaded. Re-checking is a user action.

- **D-237 Remotive: OUT (unchanged from D-177).** `https://remotive.com/robots.txt` still has `User-agent: *` / `Disallow: /api/*`. The API (`/api/remote-jobs`) is on the main site host, so the D-31 API-host exception does not apply.

- **D-238 Arbeitnow: IN (re-evaluated; D-179 terms unchanged, D-202 had cut it for scope only).**
  - **robots.txt.** `User-agent: *` / `Disallow:` / `Disallow: /*?__hstc`, so `/api/job-board-api` is allowed.
  - **Terms** (https://www.arbeitnow.com/terms, read 2026-11-08):
    - s.2: "Permission is granted to temporarily download one copy of the materials ... for personal, non-commercial transitory viewing only."
    - s.11: "You also agree to providing a link back to Arbeitnow.com on your platform. We may revoke the permission to use API at any time."
  - **API `meta.terms`:** "This is a free public API for jobs, please do not abuse. I would appreciate linking back to the site. By using the API, you agree to the terms of service present on Arbeitnow.com". `meta.info`: "Jobs are updated every hour and order by the `created_at` timestamp."
  - **Our use.** The bot is personal and non-commercial. Items link to the Arbeitnow job URL, render "via Arbeitnow", and are polled at most hourly.
  - **Real shape** (one GET, 2026-11-08):
    - top level `data`, `links`, `meta`;
    - 300 jobs per page;
    - job keys `slug, company_name, title, description` (an HTML fragment, entity-encoded), `remote` (bool), `url, tags` (list), `job_types` (list), `location`, `created_at` (epoch seconds, int).
  - **Headers.** `x-ratelimit-limit: 50`, `last-modified` present, Cloudflare cached.
  - **Fixture.** The planner committed a trimmed 4-job copy as `tests/fixtures/arbeitnow_api.json`, with `arbeitnow_api.NOTE.txt`. Jobs with emails or phone numbers were skipped, so the implementer does not need a live call.
  - **Note.** The board is Germany-centric, so expect few matches. It is added because the brief asks for it and the terms allow it.

- **D-239 We Work Remotely RSS: OUT (unchanged from D-180).**
  - `robots.txt` allows `/` and the feeds.
  - `https://weworkremotely.com/terms-of-service` and `/api-terms-and-guidelines` still answer HTTP 403 to the honest UA, so the terms cannot be read.
  - The D-180 excerpt ("scraping, copying, saving, or storing our data is strictly prohibited") also rules out storing postings.

- **D-240 SmartRecruiters: OUT (unchanged from D-174).**
  - `https://api.smartrecruiters.com/robots.txt` is `User-agent: LinkedInBot / Allow: /v1/companies/` then `User-agent: * / Disallow: /`.
  - developers.smartrecruiters.com (Posting API, read 2026-11-08): "Public Posting API supports both API Key and OAuth 2.0 Client Credentials authentication". The API is for "customers to build fully customizable career sites".

- **D-241 Recruitee: OUT (unchanged from D-175).** docs.recruitee.com/reference/authentication-1 (read 2026-11-08): "Deadline for introducing the token to calls is 10 February 2027" and "Calls without the authorization header will return the 401 Unauthorized error". The token is created by each company, and we have none.

- **D-242 Personio: OUT (unchanged from D-176).** `www.personio.com/legal-notice/` and `/terms-of-use/` again answered HTTP 429 on 2026-11-08, so the terms are unreadable. Tenant robots (`personio.jobs.personio.de`) is reachable, but robots alone is not enough under section 0.

- **D-243 hh.ru token type and User-Agent (re-confirmed 2026-11-08 from `https://api.hh.ru/openapi/specification/public`).**
  - **User-Agent.** "В запросе необходимо передавать заголовок `User-Agent`, но если ваша реализация http клиента не позволяет, можно отправить `HH-User-Agent`. Если не отправлен ни один заголовок, то ответом будет `400 Bad Request`."
  - **Format.** The `HH-User-Agent` parameter is described as "Название приложения и контактная почта разработчика" (app name and developer contact email), example `MyApp/1.0 (my-app-feedback@example.com)`.
  - **Token.** OAuth2 `clientCredentials` (application token): "Токен приложения необходимо сгенерировать 1 раз ... При этом ранее выданный токен отзывается." User tokens carry `expires_in: 1209600`.
  - **Unchanged.** D-182 stands: `HH_ACCESS_TOKEN` must be an application token.
  - **Trap 10 is already satisfied.** The header is built from `HTTP_CONTACT` (D-219), never hardcoded. No `HH_USER_AGENT` variable is added: agents cannot edit `.env.example` (D-03), and `ENV_VARS` and the README table are pinned by tests.
  - **New in KF-20.** hh is disabled, with a clear reason, when `HTTP_CONTACT` contains no `@`, because hh asks for a contact email.

- **D-244 Telethon and Telegram API facts used by the plan (read 2026-11-08).**
  - **Entities** (docs.telethon.dev/en/stable/concepts/entities.html):
    - "You should always favour `client.get_input_entity()` over `client.get_entity()`". `get_entity` always makes an API call; `get_input_entity` uses the cache when possible.
    - Every entity seen is "cached in the `.session` file (an SQLite database)". A username not yet cached "may make a request" (ResolveUsername).
  - **Resolve limits.** The docs state no ResolveUsername rate limit. `core.telegram.org/method/contacts.resolveUsername` lists only 400 errors (USERNAME_INVALID, USERNAME_NOT_OCCUPIED, ...). We put no number on it and simply resolve rarely: once per handle, cached with id and access_hash (D-252).
  - **iter_messages** (docs.telethon.dev client reference):
    - `min_id`: "All the messages with a lower (older) ID or equal to this will be excluded".
    - `wait_time`: "Wait time (in seconds) between different GetHistoryRequest ... Use this parameter to avoid hitting the FloodWaitError"; the flood limit "seems to be around 30 seconds per 10 requests".
  - **flood_sleep_threshold:** "The threshold below which the library should automatically sleep on flood wait ... errors". Longer waits raise `FloodWaitError` (`e.seconds`).
  - **messages.getHistory** (core.telegram.org): error `406 CHANNEL_PRIVATE` "You haven't joined this channel/supergroup".
    - Reading a public channel without joining is not stated in the docs. The existing polling (D-193) already relies on it.
    - The plan treats CHANNEL_PRIVATE as "not readable without joining": the channel is marked `private` and dropped from polling, and the user is told in `/channels`.
    - Confirmed live only in the KF-38 smoke test, and only when the user has Telethon credentials.
  - **contacts.search** (core.telegram.org):
    - "Returns users found by username substring". Parameters `q`, `limit`; errors `QUERY_TOO_SHORT` and `SEARCH_QUERY_EMPTY`.
    - "Only users can use this method", so it needs the user's Telethon session, not the bot.
    - It returns `contacts.found#b3134d9d my_results:Vector<Peer> results:Vector<Peer> chats:Vector<Chat> users:Vector<User>`, where `chats` is "Found chats" (core.telegram.org/constructor/contacts.found). Channels arrive in `chats`; KF-22 keeps broadcast channels that have a username.
    - Discovery queries therefore exclude short cues (fewer than 4 characters). The cue file lists transliterated tokens too (`vakansii`, `rabota`, `jobs_kz`), because usernames are Latin.
  - **FAQ.** "The best advice we can give you is to not abuse the API, like calling many requests really quickly." The library should be used only on well-established accounts.

- **D-245 Pagewatch seed vetting (2026-11-08).**
  - **Method.** For each exact URL: fetch robots.txt, then one GET with the honest UA, then a second GET about 20 s later, comparing the hash of the script-stripped visible text.
  - **Wildcard rules checked by hand.** `urllib.robotparser` ignores `*`/`$` (D-233), so each vetted host's wildcard lines were read manually. None matches a vetted path; they cover query strings, PDFs, Tilda forms and login pages.
  - **Final URLs.** Redirect targets are configured directly.
  - **KZ employers and programs, ADDED:**
    - `https://job.kaspi.kz/` (Kaspi.kz, job)
    - `https://halykbank.kz/about/career` (Halyk Bank, job)
    - `https://kolesa.group/career/job` (Kolesa Group, job)
    - `https://btsdigital.kz/ru/career` (BTS Digital, job)
    - `https://career.forte.kz/` (ForteBank, job)
    - `https://campus.epam.kz/ru` (EPAM Kazakhstan, internship; region intl by the HQ rule)
    - `https://nu.edu.kz/about/work-at-nu/jobs-at-nu/` (Nazarbayev University, research)
    - `https://nu.edu.kz/students/academic-mobility-and-career/summer-schools/` (Nazarbayev University, summer_school)
    - `https://nu.edu.kz/students/academic-mobility-and-career/jobs-and-mentoring/` (Nazarbayev University, internship)
    - `https://issai.nu.edu.kz/` (ISSAI, research)
    - `https://research.nu.edu.kz/` (Nazarbayev University, research)
    - `https://science-fund.kz/` (Science Fund, grant)

    All have robots allowed, HTTP 200, and an identical hash on two fetches.
  - **KZ, SKIPPED:**
    - Kazakhtelecom `https://telecom.kz/ru/career`: the page is a JS app with 1 visible word after stripping scripts.
    - KazMunayGas `https://kmg.kz/ru/career/`: robots allowed and stable, but oil and gas is low relevance for an ML/quant student and would add noise.
    - Kcell and Beeline KZ: no public career page found (guessed paths 404, homepages link none; vacancies are on hh).
    - Freedom (ffin.kz redirects to fbroker.kz; bankffin.kz/ru/career 404).
    - Jusan and Chocofamily: TLS certificate verification failed.
    - Arbuz `job.arbuz.kz`: HTTP 525.
    - QazInnovations: robots unreachable, treated as disallowed.
    - Tech Garden: robots 522.
    - Bolashak: the page fetch failed.
    - Air Astana: 6 visible words (JS).
    - Astana IT University homepage: not a careers or announcement page.
    - EPAM global `/careers/locations/kazakhstan`: robots 403.
    - Astana Hub: D-236.
    - enbek: D-235.
  - **International, ADDED:**
    - `https://summerstudents.desy.de/` (DESY, summer_school)
    - `https://www.daad.de/rise/en/rise-worldwide/` (DAAD RISE Worldwide, research)
    - `https://www.mitacs.ca/our-programs/globalink-research-internship-students/` (Mitacs Globalink, research)
    - `https://summerofcode.withgoogle.com/` (Google Summer of Code, internship)
    - `https://www.oxfordml.school/` (Oxford ML Summer School, summer_school)
    - `https://www.eeml.eu/` (EEML, summer_school)
    - `https://www.m2lschool.org/` (M2L School, summer_school)
    - `https://prosperity.imc.com/` (IMC Prosperity, competition)
    - `https://www.janestreet.com/join-jane-street/programs-and-events/` (Jane Street, internship)
    - `https://deepmind.google/careers/` (Google DeepMind, job)
    - `https://www.mpg.de/jobboard` (Max Planck Society, research)

    The existing seven pages (IMC, Optiver, Jane Street internships, CERN, Summer@EPFL, ETH SSRF, OIST) stay, with types added.
  - **International, SKIPPED:**
    - KAUST `admissions.kaust.edu.sa/study/internships`: the hash changed between two fetches 20 s apart, so it would raise a false change every hour.
    - lablab.ai: cross-host redirect to `lablab.build:443`.
    - HackJunction: 1 visible word.
    - HackZurich: 51 words, no event text.
    - Returned 404: CERN openlab summer, ISTA ISTernship, ICTP opportunities, G-Research early careers, Weizmann Kupcinet-Getz, EPFL summer research program, Correlation One.
    - Returned 403: Citadel students, HRT campus, IAS PCMI, Perimeter undergraduate.
    - Two Sigma: robots 403.
    - Simons Foundation grants: not student-level.
  - **Hackathons.** International hackathon pages are mostly JS or blocked. Hackathons stay covered by the `mlh` source.
  - **Terms.** Per-site terms of use were deliberately not read for the seeds:
    - pagewatch is an existing source, not a new one (section 0's terms rule is for new sources);
    - these are public career and announcement pages meant for applicants;
    - pagewatch makes one GET per page per hour, with robots honoured, an honest UA and conditional requests.

    If a site later blocks us (403) or disallows the path, the shared client stops fetching it.
  - **Re-check at fetch time.** The shared client still checks robots on every fetch (wildcard-aware after KF-04). KF-25 and KF-26 re-run the robots check offline against the planner-recorded robots bodies (fixtures) and add no URL outside this list.

- **D-246 Trap 1: the unpaid check moves after triage.**
  - **Choice.**
    - The cheap filter no longer checks unpaid keywords.
    - A new post-triage step (KF-09) checks them only when the effective `opportunity_type` is `job` or `internship`, then sets `filtered` / `unpaid` before scoring.
    - With triage disabled, for triage-exempt sources (pagewatch) and for cached old verdicts that carry no type, the step uses the provisional type (D-247).
  - **Reason.** The type comes from triage, so checking before triage would filter summer schools that a source labelled `job`, and miss jobs labelled `other`. The cost is small: D-170 counted 5 unpaid filters out of 4526 postings, so at most a handful of extra cheap triage calls.

- **D-247 Trap 2: `kind` and `opportunity_type` coexist.**
  - **Two fields.** `Posting.kind` (the source's coarse label, the `Kind` enum) is unchanged, so no source module changes. A new `postings.opportunity_type` column (schema v13) holds one of the 12 brief values (`OpportunityType` StrEnum in `src/models.py`), and `postings.type_source` says which of `kind|config|triage|scorer` set it.
  - **Precedence:** scorer > triage > pagewatch `type` config > `map(kind)`. The effective type is the column.
    - Each later stage overwrites the column only with a valid value.
    - The scorer sees 4000 description characters, triage sees 500.
  - **Map:** job→job, internship→internship, hackathon→hackathon, competition→competition, fellowship→fellowship, research→research, startup→startup_role, other→other.
  - **Backfill.** v13 sets `opportunity_type = map(kind)` and `type_source='kind'` on every existing row. Nothing is re-triaged or rescored.
  - **Triage schema.** `TriageVerdict` (extra="forbid") gains fields, so the triage prompt changes and `triage_prompt_version()` changes.
    - An invalid `type` in a triage reply is not a parse failure. It falls back to the provisional type, with no retry, the same choice as the scorer (D-251), because a retry costs a call. Unknown keys or a non-bool `relevant` still fail as before.
    - The verdict cache key stays `(posting_id, content_hash)`, so an old cached verdict without a type is reused and keeps the mapped type.
    - The triage eval (`src.evals.run --triage`) adds a type-accuracy figure.
  - **Extraction scope.** Title, company, location, contact, deadline and scam are requested only for free-text items (telegram_channels, forwarded and `/add`). Structured ATS items return relevant, reason, type and language only.
    - Triage `MAX_TOKENS` rises from 150 to 300. Cost follows the tokens actually produced, and structured replies stay short.

- **D-248 Trap 3: KZ-first weights and the "bad KZ vs good intl" invariant.**
  - **New defaults** (the brief): kz_startup +1.5, kz_employer +1.0, kz_program +1.0, intl_top_tier +0.5, intl_other 0, intl_onsite_visa_barrier −0.5. The largest KZ-over-intl uplift is 1.5 + 0.5 = 2.0.
  - **Arithmetic.** A KZ startup at base 5.5 adjusts to 7.0. intl_other at 7.5, on-site with a visa barrier, also adjusts to 7.0. The existing test (`tests/test_quotas.py::test_bad_kz_never_outranks_good_intl`, kz bases up to 5.5, intl bases from 7.5) then passes only through the final `-base` tiebreak, a zero margin.
  - **Freshness breaks it.** The digest key is `(-band, not fresh, -adjusted, deadline, -base)`. A fresh KZ 7.0 is listed above a non-fresh intl_other at 7.0 to 7.9 in band 7. Under the old weights that could not happen (5.5 + 1.0 = 6.5, band 6).
  - **Decision, stated openly.** "Clearly bad" becomes base <= 5.0 (the digest floor) and "clearly good" stays base >= 7.5. With that gap of 2.5, the KZ item's band is always lower whatever its freshness.
    - This is a narrowing of the test's "bad" range from 5.5 to 5.0. It reflects the strength of preference the user chose; it is not a silent loosening.
    - KF-10 extends the test with a freshness dimension and kz_program.
    - It keeps the old 5.5 cases for non-fresh items, where they still pass by the tiebreak, and adds a test that documents the 5.5-fresh case as the accepted exception.
    - SPEC's new section defines both terms.
  - **kz_program is exclusive.** The category is decided once: a KZ employer whose effective type is in PROGRAM_TYPES = {grant, scholarship, fellowship, hackathon, competition, accelerator, summer_school} gets `kz_program` (+1.0), never kz_employer or kz_startup, so +2.5 cannot stack.
    - A program run by a KZ startup is kz_program, because the startup bonus is about working at a startup (SPEC goal 3).
    - `research` is not a program type; a KZ research job is kz_employer, which is +1.0 either way.
  - **Unknown-region fresh threshold.** Items whose region is unknown use the stricter intl threshold, 8.0.

- **D-249 Trap 4: language plus location rule for unknown employers.**
  - **Order inside `classify_employer`:**
    1. watchlist;
    2. a confident LLM region;
    3. the source hint (hh, telegram_channels, D-194);
    4. the new rule;
    5. unknown.
  - **The new rule.** If the employer is still unknown and the posting language is `ru` or `kk` and its location (or the triage-extracted location) matches a `locations.kz` cue, the result is KZ, with source `lang_loc`.
  - **Language.** `postings.language` from triage when set. Otherwise a deterministic detector: any of `әғқңөұүһі` gives kk; at least 30% Cyrillic letters in title plus description gives ru; anything else is en.
  - **English postings located in KZ stay unknown,** following the SPEC HQ rule: a foreign company's KZ office is intl. The rule never overrides a watchlist entry or a confident LLM "intl".

- **D-250 Trap 5: snapshot discipline.**
  - **Per-task list.** Each KF task lists the snapshot fixtures it may change (PLAN 12, "Snapshot ownership"). Every other snapshot must stay byte-identical in that commit.
  - **Files covered:** `tests/fixtures/weights_snapshot/`, `config_repo_snapshot.toml`, `render_item_expected*.txt`, `stats_expected.txt`, `sources_expected.txt`, `filter_cases.json`, `score_responses.json`.
  - **Reviewer check.** The reviewer checks `git show --stat` of each KF commit against that table.
  - **Rule.** A snapshot changes only in the task that changes the behaviour it pins.

- **D-251 Trap 6: scorer contract v2 (Part 7).**
  - **Reply:**

    `{"score", "reason", "type", "employer_region", "region_confidence", "employer_is_startup", "visa_barrier", "deadline", "red_flags"}`
  - **Field limits:**
    - `reason` <= 110 characters, in English, naming the concrete match or mismatch;
    - `type` is one of the 12 values;
    - `red_flags` is at most 3 items, each <= 40 characters.
  - **Region keys kept.** "Region" in the brief keeps all four region keys because ranking needs them (SPEC employer-region section).
  - **tags dropped.** They are no longer requested, which saves tokens. The feedback examples use the stored reason (clipped to 110) instead of tags. Old rows keep their tags column.
  - **Too-long reason.** The parser truncates it cleanly at a word boundary to <= 110 characters, ending with "…". It is never rejected, because a retry costs a call.
  - **Generic reasons are flagged, not rejected.** `src/scoring/reason.py::is_generic(reason)` is a deterministic rule: after stripping generic phrases and stopwords (good, great, strong, match, fit, profile, role, opportunity, candidate, skills, background, relevant, ...), fewer than 3 content words means generic. A generic reason sets `scores.reason_generic=1` and is counted in `/stats` and in the eval.
    - Flag-only, because rejecting costs a retry against the cap.
    - The rubric demands concreteness, and the eval and smoke test report the generic rate. Above 5%, the prompt is tightened (a reviewer item).
  - **Tokens.** `MAX_TOKENS` goes from 400 to 250. The estimated worst case is about 160 tokens: a 110-character reason, 3 × 40-character flags and roughly 200 ASCII characters of keys. The rest is headroom, because truncated JSON costs a retry.
    - A reply with `stop_reason == "max_tokens"` is logged and counted, and the parse failure follows the existing retry path.
  - **Prompt version.** `rubric.md` changes, so `prompt_version()` changes. The scoring eval (`evals/labeled.jsonl`) gains expected types and a few RU/KK items.
  - **Old rows.** Schema v13 adds `scores.contract` (1 for old rows, 2 for new ones). Old rows are rendered with a fallback (reason clipped to 110, type from the posting column) and are never rescored.

- **D-252 Trap 7: tiers and the Telegram polling loop.**
  - **Tiers:**
    - tier1 `tier1_minutes = 10`, `tier1_jitter_s = 300`: 10 to 15 min, for watchlist boards and hh. hh moves from tier2 to tier1 with `min_poll_minutes = 10`, at most `[sources.hh] max_requests_per_run = 4`.
    - tier2 `tier2_minutes = 60`, `tier2_jitter_s = 3600`: 1 to 2 h, because APScheduler jitter is uniform(0, jitter) and only delays (D-226 evidence).
    - tier3 stays at 60 min with jitter 300.
  - **The `tg` tier.** Telegram polling leaves tier2 for its own tier `tg` (`[scheduler] tg_minutes = 4`, `tg_jitter_s = 60`: 4 to 5 min). It is a fourth interval job and covers only channels without a live subscription.
  - **Bounds.**
    - At most `[sources.telegram_channels] max_channels_per_poll = 20` per run, chosen round-robin by oldest `last_polled_at`.
    - `tg_minutes >= 3` and `max_channels_per_poll <= 30` are enforced by config validation.
    - One GetHistory per channel per run (`limit=100, min_id=last_id`).
    - Pause between channels: `pause_s = 4` for the `tg` path, validated at least 3. The old 1 s default (D-227) would send 20 requests in about 20 s, three times Telethon's own figure of "around 30 seconds per 10 requests", and would hit FloodWait on most runs. At 4 s, 20 channels take about 80 s, which fits the 4-minute loop.
    - At the defaults that is at most 300 history requests per hour (20 per run, 15 runs).
  - **Entity cache.** A new `tg_entities` table (schema v14) stores handle, channel_id, access_hash, title, status (`ok|private|invalid`), resolved_at and last_polled_at.
    - A handle is resolved once with `get_input_entity` and then polled with `InputPeerChannel(channel_id, access_hash)`. It is re-resolved only after CHANNEL_INVALID or an access-hash error, at most once a day per handle.
    - At most `max_resolves_per_poll = 3` new resolves per run.
  - **FloodWait.** A `FloodWaitError(seconds)` stops the run (FIX-40 behaviour) and also persists `tg_flood_until = now + seconds × 1.1` in `state`. Every Telethon read (polling, discovery) is skipped until then and logged once.
  - **Live events** only for channels the account has already joined (unchanged).

- **D-253 Trap 8: latency from first seen to alert.**
  - **Already exposed.** `record_delivery` stores `latency_s` and `/stats` shows median and p90 per source for alerts and digest (FC-13, D-210). Nothing is "only logged".
  - **KF-30 adds:**
    - separate lines for `alert` and `fresh_alert`;
    - a KZ vs intl split (by employer bucket);
    - a per-tier median including `tg`.
  - **Snapshot.** `stats_expected.txt` changes in KF-30 only.

- **D-254 Traps 9 and 15: daily caps.**
  - **Existing caps:** `[scoring] daily_call_cap` (200), `[triage] daily_call_cap` (600) and `[outreach] daily_llm_call_cap` (40).
  - **New cap.** `[telegram_discovery] daily_call_cap = 30` counts every discovery Telethon call: `contacts.search`, `GetFullChannelRequest` and each sample-history read. It is enforced before each call.
    - It reuses `LlmBudget` and the `llm_usage` table with purposes `tg_search` and `tg_sample`. A new usage table for one counter is not worth a migration, and these purposes are excluded from every LLM cap by the existing per-purpose sums.
  - **Warnings.** Each cap sends one Telegram warning per Asia/Almaty day (`cap_warned:<stage>:<day>`). Outreach keeps its inline reply.
  - **All-fail.** "All triage or scoring calls in a run failed" keeps D-224 behaviour: a notice and a non-zero exit code. KF-34 re-pins it with tests, including the new post-triage step.
  - **FIX-45 is resolved in KF-01.** `[triage] daily_call_cap = 0` with `enabled = true` is rejected at config load, with a message naming the key and pointing to `enabled = false`. The baseline sub-cap applies to every attempt, the retry included.

- **D-255 Trap 10: hh User-Agent contact.** Already satisfied, see D-243. The contact comes from `HTTP_CONTACT` (env), never from code. The user's email is never written into the repo. Setting `HTTP_CONTACT` to an email is a user-provided item.

- **D-256 Trap 11: Telethon safety, Add/Ignore, forwards and `/add`.**
  - **Join code removed.** `auto_join` and `TelegramListener._join` (JoinChannelRequest) are deleted (KF-18). A leftover `auto_join` key in config.toml is rejected with a message, because the brief forbids joining outright.
  - **Read-only wrapper.** Every Telethon use goes through `src/sources/telegram_ro.py::ReadOnlyTelegram`.
    - Allowed methods: connect, disconnect, is_user_authorized, get_input_entity, iter_messages, iter_dialogs, `add_event_handler` for `events.NewMessage`.
    - Allowed raw requests: `contacts.SearchRequest`, `channels.GetFullChannelRequest`, `messages.GetHistoryRequest`.
    - Anything else raises `ReadOnlyViolation`. A test drives every Telegram code path with a recording fake and asserts that none of these was called: send_message, send_file, forward_messages, delete_messages, edit_message, send_read_acknowledge, any Join/Leave/Import request.
  - **Add/Ignore storage.** The Add button writes the handle to the existing `tg_channels` overlay (state `added`, schema v10) inside the gitignored `data/oppo.db`. It does not create `data/telegram_channels.toml`: the overlay is already the runtime channel store used by `/channels add`, and it lives under the gitignored `data/` as the brief requires.
  - **Ignore and candidates.** Ignore is stored in `tg_channel_candidates.status = 'ignored'` (schema v15), so the handle is never suggested again. No join call is ever made; a channel is added only by the user's button press.
  - **Sample posts.** They are HTML-escaped and clipped to 200 characters before display.
  - **Discovery caps.** Discovery searches and sample reads count against the D-254 cap.
  - **Chat checks.**
    - Every new handler (`/findchannels`, `tc:` callbacks, forwarded messages, `/add`, `/search`, `/cvreview`, `/boards`) relies on the existing `WhitelistMiddleware`, which drops any update not from TELEGRAM_CHAT_ID before handlers run.
    - KF-35 adds one parametrized dispatcher test proving each is silent for another chat.
  - **`/add`** did not exist, so it is built (KF-24). `/add <posting text>` stores a `manual` posting, runs triage for extraction, then the post-triage step, then scoring, and replies with the rendered item (delivery kind `manual`).
    - A triage "not relevant" verdict does not drop a user-added item.
    - The URL is the first http(s) URL in the text. Without one, the bot asks for the link. Nothing is fetched.
  - **Forwards.** A forwarded channel post (`forward_origin` of type channel with a username) becomes `source=telegram_channels`, `external_id=<username>:<message_id>` and url `https://t.me/<username>/<message_id>`, so it dedupes against polling. Other forwards are handled like `/add`.
  - **Handler order.** The forward handler is registered after the pending `/cv` and `/addcontact` text handlers, so it never takes their input.
  - **Prepare.** It is now on every item (D-260). The liked/saved gate in `on_prepare_callback`, if any, is removed, because pressing Prepare is an explicit request that the outreach cap still bounds.

- **D-257 Trap 12: `config/cues.toml`.**
  - **Layout.** Groups by category, then language:
    - `[types.<name>]`, `[fields.<name>]`, `[locations.kz]` and `[negative]`, each with `en`, `ru`, `kk` lists;
    - `[seniority]`, `[junior]`, `[protect]`, `[spam]`, `[unpaid]`, `[hard_blockers]`, `[qualifications]`, also per language;
    - `[hh]` (query groups and bounds);
    - `[telegram_discovery]` (queries).
  - **Entry syntax:**
    - `word or phrase` matches on whole words at both ends, case-folded, with ё folded to е;
    - `stem*` matches as a prefix (for Russian and Kazakh inflection);
    - `re:<regex>` is allowed for rare cases, such as the existing intern regex that keeps "internal" and "international" out (FIX-04). It is compiled at load, and an invalid regex fails naming the entry.
  - **Short tokens.** Entries of 3 characters or fewer (ai, ml, qa, c++) are always whole-word, even when written with `*`; that is a load error.
  - **Counts.** At least 150 positive cues (types plus fields), with at least 35 in ru and 35 in kk. A test counts them.
  - **Fixture.** `tests/fixtures/cue_titles.json` has at least 60 titles (at least 15 per language) with expected keep/drop, including "Maintenance Technician", "Email Marketing Manager", "Domain Expert", "Retail Assistant", "Chairperson", "Стажер-бухгалтер" (drop, negative "бухгалтер*") and "Air Astana cabin crew" (drop).
  - **Single source.** The pre-filter, hh queries (KF-20), Telegram discovery queries and job-ratio estimate (KF-22), `/search` expansion (KF-29) and CV market aggregation (KF-31) all read this one file.
  - **Migration.** `[filters]` in config.toml keeps only non-cue settings (`max_age_days`, `hard_blocker_action`, `hard_blocker_penalty`) plus `cues_file = "config/cues.toml"`.
    - A leftover `[filters]` keyword list (positive_title_keywords, spam_keywords, unpaid_keywords, senior_only_keywords, hard_blocker_keywords, negative_title_keywords, protect_title_keywords) makes config loading fail with "<key> moved to config/cues.toml". There are no overrides, so there is one file to edit, as the brief asks.
    - The repo config.toml is updated in the same task.
  - **hh request bound.** One request per configured query group: an OR query built from up to 12 of that group's terms, skipping stems and regexes. There is never one request per cue. `max_requests_per_run` bounds the total.

- **D-258 Trap 13: SmartRecruiters, Recruitee and Personio need nothing.** All three are OUT (D-240 to D-242). No discovery URL pattern or account list is built for them, and `src/discovery/extract.py` stays limited to greenhouse, lever, ashby and workable.

- **D-259 Trap 14: CV review design (Part 8).**
  - **Market aggregation is deterministic** (`src/cv/market.py`, no LLM).
    - Inputs: postings first seen in the last 30 days that have base score >= `[cv_review] market_min_score` (7.0) or an active like.
    - Each posting counts each `fields` / `qualifications` cue group once.
    - Split by employer bucket: KZ = kz_*, intl = intl_*. Unknown-region postings are left out of both.
    - Output: the top 10 groups per bucket with counts and shares. A bucket with fewer than 5 postings is reported as "not enough data".
  - **One LLM call.** `[outreach] tailor_model` gets the parsed CV sections, `src/scoring/profile.md` (the SPEC target profile) and the market summary. The CV goes inside `wrap_untrusted("cv", ...)`; the market summary holds only cue labels and counts. With the `call_json` correction retry, that is at most 2 calls.
    - Purpose `cv_review` is added to `OUTREACH_PURPOSES`, so the existing outreach daily cap applies.
  - **Output schema:**
    - `fit_summary`: 3 lines;
    - `changes`: 5 items `{what, why}`, ranked;
    - `gaps`: 3 items `{gap, bucket, mark: add_if_true|worth_learning, note}`;
    - `report`: `{structure, length, ordering, bullets: [{original, suggestion}], keywords, kz_notes, intl_notes, formatting}`.
  - **Verification** uses `src/outreach/verify.py`.
    - Each `bullets[].suggestion` and each `changes[].what` goes through the same claim checks as tailoring: numbers, entities, credentials, skills not in the CV, and inflation stems.
    - An unsupported bullet becomes a question ("Did you <claim>? If yes, add it.") and its unsupported tokens are listed.
    - `keywords` not found in the CV text are dropped.
    - A gap marked `add_if_true` is worded as a question; one already present in the CV is dropped.
    - The counts of removed and converted items are shown.
  - **Delivery.**
    - The chat summary is under 3500 characters.
    - The full report is Markdown, sent with aiogram `BufferedInputFile(bytes, filename="cv_review_v<N>.md")`. It is built in memory and never written to disk; it is cached only in `data/oppo.db`.
  - **Cache.** `cv_reviews` (schema v16) is keyed by CV version and prompt version.
    - The `on_new_cv_version` and `on_forget_cv` hooks delete rows, so a new upload or `/forgetcv` clears the cache.
    - `/cvreview` shows the cached review (0 calls). `/cvreview refresh` regenerates it within the outreach cap.
    - `/cv` runs the review automatically, but only when the version changed.
  - **Tests:** the fictional `tests/fixtures/cv_fake.txt`, a fixture market sample, and fabrication-trap replies (an invented employer, an inflated number, a skill missing from the CV, an invented degree).

- **D-260 Trap 16: the item format (Part 7) and its conflicts.**
  - **Layout** (HTML parse mode, as the digest-format skill and D-16 require; everything escaped):
    - line 1: `<flag> <b>title</b> — company · city · until YYYY-MM-DD` plus an optional ` · ⚠ <marker>`;
    - line 2: `Relevance N/10: <reason>`;
    - line 3: the link, plus ` · via <aggregator>` when required.
  - **Buttons.** One row: Like, Dislike, Save, Applied, Prepare. Callback data stays under 64 bytes.
  - **N is the base score,** the LLM relevance, formatted without a trailing `.0`. The adjusted score decides order only (SPEC: alerts and the floor use the base score).
  - **Attribution vs "nothing more".** Remote OK threatens to suspend API access without a credit (D-178), and Arbeitnow's terms require a link back (D-238). The credit therefore stays, appended to the link line, not as an extra line.
  - **Alert marker.** Alerts prefix line 1 with `<b>ALERT</b> · ` or `<b>NEW</b> · `, so the user can tell an instant alert from a digest item. This is the only addition.
  - **Red-flag marker.** One short marker, clipped to 24 characters, chosen in this order: hard blocker > scam or personal-contact-only (triage) > visa barrier > the first model red flag. "⚠" (U+26A0) is the one new non-ASCII symbol (D-18 had allowed —, · and ✓; flags came with D-96).
  - **Removed:** the old `Deadline:` line, the `Red flags:` paragraph and the `(base x, +1.0 KZ startup)` detail. `/prepare` still shows the detail.
  - **Lengths.**
    - Title clipped to 90 characters, company to 50, city to 30. Line 1 stays at or under 220 characters.
    - Old rows: the reason is clipped to 110 characters at a word boundary (clean "…").
    - The whole item stays at or under 600 characters plus the URL (URL capped at 1000 by `safe_url`).
  - **Scope.** Used by the digest, alerts, `/top`, `/search` and `/add` replies.
  - **The skill file.** `.claude/skills/digest-format/SKILL.md` still describes the old layout. It is agent configuration, which agents do not edit. SPEC and PLAN are the source of truth, and the README notes the new layout.

- **D-261 "Search results" and "search parsing".** No search command existed. KF-29 adds `/search <words>`, a local search over scored postings from the last 30 days, with no LLM calls and no network.
  - **Expansion.** Each query word that matches a cue expands to that cue's whole group across en, ru and kk; that is the "search parsing" use of the cue file.
  - **Results.** The top 5 by adjusted score, in the new item format with keyboards.

- **D-262 Triage extraction, scams and repost dedupe (Part 4).**
  - **Extracted fields** (free-text items only) go into new columns `ex_title`, `ex_company`, `ex_location` and `ex_deadline`. The source's own `title` and `company` are never overwritten, so `fingerprint` and `content_hash` stay stable.
    - Rendering prefers the `ex_*` value when the source value is empty or the source is free-text.
    - Deadline precedence: source deadline > scorer deadline > triage deadline.
  - **Contact.** Never stored raw: SPEC Contacts forbids storing emails and phones. Only `contact_key = sha256(normalized contact)[:16]` is kept.
  - **Scams.** `scam` is one of `none|upfront_payment|personal_contact_only|other`.
    - upfront_payment and other are filtered with reason `scam` before scoring, except for user-added items, which are scored with the marker.
    - personal_contact_only is common in honest KZ channel posts, so it only gives the line-1 marker.
  - **Repost dedupe** runs in the post-triage step, so it can use the extracted fields.
    - `repost_key = sha1(norm(company or ex_company) | norm(title or ex_title) | contact_key)[:16]`, set only when the title is non-empty and company or contact is non-empty.
    - A newer posting whose key equals that of an older non-duplicate posting from the last 30 days, in a different channel or from manual input, becomes `duplicate` (reason `repost`) before scoring.
    - The global `fingerprint` is unchanged, so history is never re-deduped.

- **D-263 Pagewatch `type` key.**
  - **Config.** `[[sources.pagewatch.pages]]` gains an optional `type` (one of the 12 values), validated at load. Pagewatch is triage-exempt, so this is the only way its items get program types: `Kind` has no summer_school, grant or accelerator.
  - **Rows.** Postings carry `type_source='config'`. The existing `kind` stays for backwards compatibility.

- **D-264 FIX-50 closed by this planning commit.** SPEC "Delivery" gains an "Added 2026-11-08" bullet. It names baseline suppression (D-188), hard-blocker suppression (D-195), fresh alerts (score, window, daily cap; D-189, now regional per KF-13) and the digest band order (D-189, D-209). The FIX-50 acceptance asks for exactly that, so FIX-50 is marked `[x]` in the same commit.

- **D-265 Schema versions for section 12.**
  - **v13 (KF-07):**
    - postings gains `opportunity_type`, `type_source`, `language`, `ex_title`, `ex_company`, `ex_location`, `ex_deadline`, `contact_key`, `scam` and `repost_key`;
    - scores gains `contract` (default 1) and `reason_generic` (default 0);
    - an index on `repost_key`;
    - the backfill described in D-247.
  - **v14 (KF-19):** `tg_entities`.
  - **v15 (KF-22):** `tg_channel_candidates`.
  - **v16 (KF-32):** `cv_reviews`.
  - **Tests.** Each migration has a test that starts from the previous version and proves the backfill and idempotency.

- **D-266 Threshold keys.**
  - **New keys.** `[thresholds] fresh_alert_score` is replaced by `kz_fresh_alert_score = 7.0` and `intl_fresh_alert_score = 8.0`. `fresh_alert_max_per_day` becomes 6, a combined cap across regions.
  - **Old key.** A leftover `fresh_alert_score` fails config loading, with a message naming the two new keys, rather than being silently ignored.
  - **Region of an item.** The employer bucket: kz_* uses the kz threshold; intl_* and unknown use the intl threshold (D-248). The existing rules (base >= 9, deadline within 7 days) stay uncapped and are checked first.
  - **Thresholds use the base score** (SPEC).

- **D-267 Wildcard-aware robots matching (KF-04).**
  - **Problem.** `PoliteClient._robots_allowed` uses `urllib.robotparser`, which ignores `*` and `$`.
  - **Choice.** A small in-house matcher (`src/robots.py`) implementing RFC 9309 matching:
    - `*` wildcard, `$` end anchor;
    - longest match wins, and Allow wins ties;
    - the group for our agent token `oppo-tg` else `*`.
  - **Reason.** Every new source and page relies on robots being right, and two real files in this audit (enbek.kz, Remotive) use wildcard disallows the current parser would miss. No new dependency.

- **D-268 Planner-captured fixtures committed with the plan (KF-00).**
  - **Why.** The brief allows live calls only in the final smoke test. The previous round therefore hand-wrote the Workable and Remote OK fixtures, and the shapes had to be confirmed later (D-215, D-216).
  - **What.** The planner was already making the source checks live, so it saved real, trimmed responses for later tasks:
    - `tests/fixtures/arbeitnow_api.json` (4 jobs, see D-238 and its NOTE file);
    - `tests/fixtures/robots_enbek.txt` and `tests/fixtures/robots_remotive.txt` (full bodies, D-235, D-237);
    - `tests/fixtures/robots_pagewatch_seeds.json`: robots status and body for every seed host and every existing pagewatch host (D-245).
  - **Content.** None of them contains personal data.
  - **Who reads them.** No test reads them until KF-04, KF-25, KF-26 and KF-27, and implementers must not fetch these again.

- **D-269 KF-01 implementation.**
  - **Sub-cap via `call_json(sub_budget=...)`.** An optional second budget is checked before every attempt, the correction retry included. The main budget still records the call, with purpose `triage_baseline`, so the sub-cap counts the same rows the scorer's does. A retry stopped by the sub-cap returns `error_kind = "budget"`, so the posting counts as `capped` and keeps its attempt count, like the first-attempt case.
  - **No snapshot change.** `tests/fixtures/config_repo_snapshot.toml` has no `[triage]` section, so only the `config.toml` comment changed.

- **D-270 KF-02 implementation (partial first fetch, live channel state; closes FIX-44).**
  - **Carrying "partial".** `SourceContext.partial` is a set of `(source, target key)` pairs, like `validators` and `after_store`. A source calls `ctx.note_partial(...)` when `RequestTally.partial` (at least one failed request) is true. `run_target` turns it into `TargetResult.partial`. This avoids changing the `fetch` return type.
  - **Collector.** Items of a partial first fetch are stored with `baseline=1`, and `baselined_at` stays NULL, so the next fully ok fetch is still a baseline. Only `tr.ok and is_new and not tr.partial` baselines.
  - **Sources.** hh, jobicy and rss (per shared name) set it from their tally. Pagewatch sets it for "no text" (a page that returned HTML but no extractable text).
  - **Live listener.** `_subscribe` writes an ok `target_state` row and sets `baselined_at` (only if NULL) for each channel it is live on. Channels not joined stay on polling and get no row until polled. Tier is left to the collector.
  - **Test note.** The hh partial test fails one query with a 200 non-JSON body, because a 5xx makes the host back off and also fails the second query.

- **D-271 KF-03 implementation (Retry-After HTTP-date, global slot order; closes FIX-46).**
  - `_parse_retry_after(value, now)` takes delta-seconds first, then an HTTP-date via `email.utils.parsedate_to_datetime`, measured from the injected clock (system time only when the client has no clock). A past date gives 0, an unparseable value gives None. A naive date is treated as UTC.
  - `HostLimiter.slot` takes the host lock, sleeps for spacing, then takes the global semaphore. Lock order stays host then global, so there is no deadlock.

- **D-272 KF-04 implementation (robots matcher).**
  - `src/robots.py` parses groups (consecutive user-agent lines share a group), picks the `oppo-tg` group else `*` (no merging across groups), strips comments and leading whitespace, ignores empty Allow/Disallow lines, and uppercases percent escapes on both sides.
  - Longest pattern wins, Allow wins a tie, no matching rule allows. `PoliteClient._robots_allowed` calls it; caching and 4xx/5xx handling are unchanged.

- **D-273 KF-05 implementation (cue file and matcher).**
  - **Files.** `config/cues.toml`, `src/cues.py` (`load_cues`, `parse_cues_text`, `match_any`, `which`, `Cues.positive_terms/group_terms/expand/title_decision`, `CueError`). Nothing else in `src/` imports it yet (KF-06 does).
  - **Counts (types + fields entries):** 619 total (en 271, ru 196, kk 152), far above the 150/35/35 floor; a test enforces the floor.
  - **Matching.** A space inside a phrase matches spaces or hyphens ("back end" matches "back-end"). The `*` check follows D-257 on the whole entry: an entry of 3 characters or fewer with a `*` is a load error ("hack day*" is fine, "ai*" is not). Duplicate entries in one list are dropped at load.
  - **False-positive review (deliberate omissions).**
    - No bare `engineer*`, `sales*`, `quant*`, `modeling`, `старш*`, `физик*`, `academic`. Reasons: salesforce, quantity surveyor, fashion modeling, старшеклассник, физическое лицо.
    - Seniority uses a regex for the inflected "старший" forms instead of `старш*`, so "Олимпиада для старшеклассников" is kept.
    - Russian physics uses a regex over the inflected forms of "физика" for the same reason.
    - `protect` includes "device driver*" so "Linux Device Driver Developer" survives the negative "driver".
    - Bare `engineer*` is dropped from the positive cues (the old `[filters]` list had it). Engineering titles still pass through field cues (ml, data, backend, software, research, quant). KF-06 may revisit this if recall drops.
  - **Extra table shapes.** `qualifications` and `hh` hold named sub-groups (`qualifications.docker`, `hh.internships`) so KF-31 can count each group once and KF-20 can send one request per group. `hh.max_terms_per_query` (12) is the only scalar. `locations` allows only `kz`.
  - **`title_decision`** returns "keep" or `drop:no_positive`, `drop:seniority`, `drop:negative`. Intern and junior cues waive seniority only; protect cues waive negative only.
  - **`expand(word)`** returns the word plus every entry of each positive group it belongs to, stems with the `*` removed and regexes skipped.

- **D-274 KF-06 implementation (pre-filter reads the cue file).**
  - `filters.py` loads `Cues` once per `apply_filters` call (`load_filter_cues`, path from `[filters] cues_file`, relative to the repo root; absent key means `config/cues.toml`). `hard_filter`, `relevance_ok` and `hard_blocker` take an optional `cues` argument and load the file themselves when it is omitted (evals, tests). A bad file raises `CueError` and is not caught.
  - `senior_only` and `negative_title` come from `title_decision` (job and internship kinds only). Consequence: a title with no positive cue is not senior_only or negative_title any more; it falls through to `irrelevant`. Three `filter_cases.json` cases changed from `negative_title` to null for this reason: "Recruiter", "Legal Counsel", "Product Designer" (no positive cue; `relevance_ok` drops them). `test_internal_does_not_exempt_senior_only` uses "Head of International Data Science" instead of "...Sales" for the same reason.
  - **Engineer recall.** Bare `engineer*` (and Russian `инженер*`) restored to `fields.software`: the old list had it and dropping it turned "Senior Internal Tools Engineer", "Solutions Engineer", "Staff ML Engineer"-style titles into `irrelevant` instead of their precise reasons, and lost generic engineering titles. To keep the noise down, `[negative]` gained `support engineer*`, `solutions engineer*`, `sales engineer*`, `legal counsel*` and `finance operations` (the old negative phrases). The LLM triage handles the remaining non-software engineers (mechanical etc.). `evals/labeled.jsonl` recall unchanged (every positive passes).
  - `hard_blocker` returns the first matching cue entry in file order ("citizens only" before "us citizens only"); the old code returned the first configured keyword.
  - `AppConfig.from_dict` rejects the seven moved keys with "<key> moved to config/cues.toml" (`MOVED_FILTER_KEYS`). `max_age_days`, `hard_blocker_action` and `hard_blocker_penalty` stay in `[filters]`.
  - `test_cues.test_nothing_else_in_src_uses_cues` now also allows `filters.py` and `config.py`. `test_evals_runner` triage test uses "Fake Intern D" because "Nurse" is now a negative cue and was filtered before triage.

- **D-275 KF-07 implementation (schema v13).**
  - `OpportunityType`, `PROGRAM_TYPES`, `kind_to_type` live in `src/models.py`; an unknown kind maps to `other`.
  - `upsert_posting` writes `opportunity_type` and `type_source='kind'` in the INSERT only; the update branch never touches them.
  - `db.set_opportunity_type` ranks sources kind 0, config 1, triage 2, scorer 3. A lower or equal-rank-below source is ignored (returns False); an equal or higher source overwrites. An invalid type or source raises ValueError. A row with NULL `type_source` counts as lowest.
  - The new `triage_results` columns shifted positional `INSERT ... VALUES` in two older tests (`test_stats`, `test_migration_v12`); they now name their columns. Hard-coded `== 12` version asserts in older migration tests now compare to `db.SCHEMA_VERSION`.

- **D-276 KF-08 implementation (triage v2).**
  - `TriageVerdict`: `type` and `language` keys are required (a missing key is invalid and retried). A value outside the enum becomes None with no retry (D-247), so the posting keeps its mapped type and language stays NULL. An invalid `scam` value becomes `other`, a bad `deadline` becomes None; strings are whitespace-normalised, clipped and empty becomes None.
  - `Free-text item: yes|no` is a trusted line placed before the `<posting>` wrapper, not inside it. Free-text sources are `telegram_channels` and `manual`. For structured items any extraction keys the model returns are ignored.
  - `db.apply_triage_extraction` writes type (source `triage`, precedence rules unchanged), language and the `ex_*`, `contact_key`, `scam` columns. `contact_key` is computed in `triage.py`; the raw contact never reaches the DB or logs. A cached verdict re-applies only type and language (the extraction columns persist on the posting).
  - The eval's `expected_type` is optional in `parse_item` but present on all 29 items (6 new fictional: 2 ru, 2 kk, 2 Telegram free text). `type_accuracy` counts only items that reached triage without error. The live eval was not run (needs the API key).
  - Existing triage test replies gained `type` and `language` because they are now required.

- **D-277 KF-09 implementation (post-triage step).**
  - `apply_post_triage(conn, cues, clock, include_new=False)` takes one extra keyword: `new` rows are covered only when triage is off (`score_pending` passes `include_new=not triage_on`). With triage on, `new` rows still await triage, and pagewatch rows are already `triaged` (exempt). `score_pending` gained an optional `cues` argument (default `load_cues()`); `main.py` passes `load_filter_cues(cfg)`.
  - Effective type is the `opportunity_type` column, falling back to `kind_to_type(kind)` when NULL.
  - Repost: the older row must have the same `repost_key`, a status other than `duplicate`, `first_seen_at` within 30 days and earlier (rowid breaks ties), and a different `target`, or either side is the `manual` source. Rows are processed oldest first and the key is written before comparing, so two reposts in one batch resolve to one original. Filtered originals count as matches.
  - The eval runner (`src/evals/run.py`) used `hard_filter` for its unpaid check, so it now calls `post_triage.is_unpaid` on the kind-mapped type through a `_cheap_reason` helper. Its behaviour is unchanged.
  - The "unpaid internship" case left `filter_cases.json`: its title now hits `negative_title` in `hard_filter`, and unpaid is covered in `tests/test_post_triage.py`. The KF-06 guard test `test_nothing_else_in_src_uses_cues` also allows `post_triage.py` and `run.py`.

- **D-278 KF-10 implementation (KZ-first weights, kz_program).**
  - `classify_employer` gets a keyword `opportunity_type` (last parameter). A private `_kz()` decides the KZ category once for watchlist, llm and hint outcomes. The effective type is the given type, else `kind_to_type(kind)`. An unknown type string is ignored. PROGRAM_TYPES gives `kz_program`; `startup_role` counts as startup; otherwise the old startup/employer rule applies.
  - `recompute_adjusted` selects `p.opportunity_type`. The weights version is the YAML hash, so editing `config/weights.yaml` already forces one recompute; no extra version suffix.
  - Only `tests/fixtures/weights_snapshot/weights.yaml` changed in the snapshot (adjustments only; quotas stay 4/4/2 until KF-12). Existing tests that pinned the old bonuses (1.0/0.5/1.0) were updated to the new numbers.
  - The invariant test takes the product over KZ category (employer, startup, program), KZ fresh, intl category, intl fresh, visa, remote and intl base. Fresh KZ uses bases {3, 4, 5}; non-fresh KZ adds 5.5. `test_documented_exception_fresh_kz_55` pins the accepted exception from D-248.

- **D-279 KF-11 implementation (lang_loc region rule).**
  - `classify_employer` gets trailing parameters `language`, `location`, `cues`. The `lang_loc` step runs after the hint and before unknown; it reuses `_kz()` so programs and startups keep the KF-10 logic. Cues default to `load_cues()` (group `locations.kz`, all languages).
  - `recompute_adjusted` uses `postings.language`, else `detect_language(title + description)`, and `location` plus `ex_location` joined. It takes an optional `cues` argument and loads the default lazily, only when rows need recompute. The version string always ends in `+ll`, so existing rows recompute once.
  - `detect_language` counts letters only. Empty or letterless text is `en`. Kazakh letters win over the Cyrillic ratio.

- **D-280 KF-12 implementation (digest quotas 6/3/1).**
  - Defaults changed in `config/weights.yaml`, the `Quotas` model and the weights snapshot. `pick_with_quotas` is unchanged: the fall-through logic already handles short buckets, and the digest size stays 10 (the sum).
  - The best-remaining slot goes to the strongest leftover of any bucket, so 8 KZ plus 8 intl gives 6/4 or 7/3 depending on which side scores higher; both tests pin that.
  - `tests/test_weights.py::test_snapshot_values` also pinned 4/4/2, so it was updated to 6/3/1 (it reads the snapshot owned by this task).

- **D-281 KF-13 implementation (regional fresh-alert thresholds).**
  - `select_alerts` picks the threshold with `bucket(item.employer_category)`; kz uses `kz_fresh_alert_score`, everything else (intl, unknown) uses `intl_fresh_alert_score`. Category is the one recomputed by `recompute_adjusted` just before.
  - Old-key check lives in `AppConfig.from_dict` (not `Thresholds`) so the message can name both new keys before the unknown-key check.
  - Existing fresh tests moved to intl-range scores (8.x) and gained a `region` argument on `add`; tests that disable fresh alerts set both keys to 10.0. README paragraph updated.
