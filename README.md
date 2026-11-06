# oppo-tg

A personal Telegram bot that finds, ranks and delivers job, internship, startup, hackathon and
research-program opportunities (Kazakhstan and worldwide), tuned to one user. There is no web UI.

Pipeline: collect -> dedupe -> cheap filter -> LLM score (Claude) -> Telegram digest and instant
alerts -> your like/dislike/save/applied feedback tunes later scoring.

## Verification status

Read this first. Be honest about what has and has not been checked.

- Verified: the unit and end-to-end tests (all offline, with saved fixtures and fakes), lint and
  format. One live run of `probe` on 2026-11-06 reached all six counted source families with
  items (D-62).
- NOT verified: the bot has never run against the real Telegram API, and scoring has never run
  against the real Claude API. The evals in `evals/` have never been run (D-70), so the score
  thresholds in `config.toml` are untested guesses. `hh` and `telegram_channels` have never been
  run against live services (no credentials were available). The systemd unit below is an
  example that has not been tested.
- NOT verified, coverage and freshness (FC-01..FC-30, branch feature/coverage-and-freshness): the
  tiered scheduler, politeness limiter, fresh alerts, board discovery, Telethon live listener,
  triage and hard blockers were built and tested offline only (fakes and fixtures). The Workable
  and Remote OK fixtures are hand-written from each API's documented shape, not captured from a
  real response (D-215, D-216); a check against a real response is still pending (the final smoke
  test, FC-33, or you). The triage eval (`--triage`) and the live smoke run have not been run.
  Nothing in this section has run against the real Telegram, Anthropic or hh.kz services.
- NOT verified, outreach and weights: the CV, `/prepare`, `/contacts` and `/draft` features and the
  LLM region fields (`employer_region` and friends) were built and tested offline only (fakes and
  fixtures). They have never been run against the real Anthropic or Telegram APIs, and the scoring
  rubric changed (D-92) without an eval re-run. The capped live smoke run (F2) is not done yet.

### Definition of done status

Each SPEC.md "Definition of done" item, classified honestly:

- All tests green: verified offline (`.venv/bin/python -m pytest -q`, `ruff check .`, `ruff format --check .`).
- Bot starts and sends a real digest to the user's chat: user-only. Fill `.env`, then run
  `.venv/bin/python -m src.main once` and check your chat. Never run against real Telegram yet.
- At least 6 sources working: verified offline (6 credential-free families enabled, fixture tests).
  One live `probe` run on 2026-11-06 reached them, but that was a first-run result (D-62, D-72);
  user-only: re-run `.venv/bin/python -m src.main probe` (expect exit 0) as sources can change.
- Feedback buttons persist: verified offline (callbacks write to SQLite in tests); user-only: press
  the buttons in your real chat after `once`.
- README with setup and run instructions: verified offline (`tests/test_readme.py` checks it against code).
- `.env.example` complete: NOT MET until the user adds the block. Agents cannot edit `.env.example`
  (D-03), so copy the block from "First run / user actions" step 1 into it by hand.
- Scheduler runs unattended: verified offline only for the scheduler logic (fake clock tests);
  user-only: start `run` (systemd example below) and confirm digests arrive for a few days.
- DECISIONS.md explains every non-obvious choice: verified by review (entries D-01 onward); judgement call.

Outreach and weights (SPEC.md "Definition of done: outreach and weights"):

- Employer-region adjustments, clamping, digest quotas with fall-through and YAML-only edits are covered by tests: verified offline (weights, classifier, selection and digest tests).
- A clearly bad Kazakhstan posting cannot outrank a clearly good international one under the default weights (tested): verified offline (tested against the committed snapshot of the defaults, D-95).
- `/cv` ingests a PDF or pasted text, stores it only under `data/`, and never logs it: verified offline (tests plus a privacy sweep with canary strings, D-138); never run with a real CV.
- `/prepare` and `/draft` output passes the claims-vs-CV verification, including a fabrication test fixture: verified offline with a faked model; never run against the real Claude API.
- Contacts come only from the listed public sources, expire on schedule, and LinkedIn is never fetched (tested): verified offline (the HTTP client refuses LinkedIn hosts without any request, D-104).
- A live smoke run with at most 20 scoring calls is recorded in DECISIONS.md, or listed as a user action: recorded in D-166: `once` scored 20, sent a 10-item digest. Not seen live: KZ flags and a mixed quota batch, and /cv, /prepare, /draft (offline tests only).

## Employer-region weights

The LLM base score is region-blind (D-92). Code then adds a small adjustment by employer, so
Kazakhstan employers are not buried under big international names. All of it lives in YAML, with
no code change needed:

- `config/weights.yaml`: `adjustments` (`intl_top_tier`, `intl_other`, `kz_employer`, `kz_startup`,
  and `intl_onsite_visa_barrier`, a modifier that stacks only with the two international categories
  and only for on-site roles), `region_min_confidence` (LLM region guesses below it are ignored),
  `digest_quotas` (`kz`, `intl`, `best_remaining`; their sum is the digest size, and unfilled
  slots fall through to the best remaining items), `flags` (the emoji shown before the title; `""`
  turns one off) and the watchlist paths.
- `config/watchlists/intl_top_tier.yaml` and `config/watchlists/kz_employers.yaml` (the latter has a
  `startup: true|false` flag per employer). They are starting points: extend them. Names match
  exactly after normalization (case, punctuation and legal forms like LLC/ТОО are ignored), so list
  spelling variants under `aliases`. Only the top-tier watchlist can make an employer top-tier.
- `[ranking] weights_file` in `config.toml` points at the YAML file.

How the score works: `adjusted = clamp(base + adjustment, 0, 10)`. Eligibility for the digest and
for instant alerts uses the base score, so a bonus never lifts a weak posting in and never fires an
alert (D-95, D-97); ordering uses the adjusted score. When an adjustment applies, the item shows
`adjusted/10 (base X, ...)`. Edits to the YAML or watchlists take effect on the next digest, alert
or `/top`, and existing items are re-ranked without any LLM call. An invalid file never stops the
bot: it falls back to the built-in defaults and notifies you at most once a day.

## CV, prepare and drafts

Commands (only your chat, `TELEGRAM_CHAT_ID`, can use them):

- `/cv`: the bot asks for your CV; send a PDF or paste the text within 15 minutes. `/cancel`
  clears any pending input.
- `/forgetcv`: deletes the stored CV and what was derived from it.
- `/prepare <id>`: tailoring advice for one posting (an id or a prefix of at least 6 characters,
  or tap the `/prepare_<id>` link or the Prepare button): keywords, what to emphasize from your CV
  and public contacts. Every claim is checked against your CV text. The check is a
  deterministic heuristic: it removes most invented employers, degrees, titles and numbers, but it
  cannot catch everything (unnamed or lowercase employers, dates and temporal status, soft
  inflation such as "several years"; see D-161 in DECISIONS.md). Read every draft and tailoring tip
  before you use it.
- `/contacts <id>`: stored contacts for a posting; `/addcontact <id> [text]`: add one from pasted
  text (see Contacts).
- `/draft <contact id>`: a short outreach draft (3-5 sentences) for a contact shown by `/contacts`.

Nothing is ever sent for you. Drafts are shown in your chat to copy yourself; the bot has no code
that emails or messages anyone except you, and the model gets no tools.

Privacy:

- Your CV text, and for `/prepare` and `/draft` the posting and contact text, is sent to the
  Anthropic API (the cheap model parses the CV, the tailor model writes advice and drafts). The
  `/cv` prompt says so before anything is sent. Do not upload a CV you would not send there.
- Stored: only in the SQLite DB (`DB_PATH`, default `data/oppo.db`, gitignored): the current CV
  text and its parsed sections (older versions are deleted), cached tailoring results, contacts
  and drafts. The uploaded PDF itself is read in memory and never written to disk. CV text, contact
  names and draft bodies are never logged. `/forgetcv` removes the CV.
- Contacts expire after `contact_ttl_days` (60) or a day after the posting deadline, whichever is
  first, and are pruned daily with their drafts.
- Limits: PDF or text only (no DOCX, no OCR, so a scanned PDF is refused), at most 5 MB, 20 pages
  and 30000 characters. Outreach LLM calls are capped per day (`daily_llm_call_cap`).

`[outreach]` in `config.toml` (unknown keys are rejected): `cheap_model`, `tailor_model`,
`daily_llm_call_cap` (all outreach calls per day, retries included), `max_page_fetches_per_prepare`,
`max_page_fetches_per_day`, `max_contacts_per_posting`, `contact_ttl_days`, `rediscover_after_days`,
`cv_max_bytes`, `cv_max_pages`, `cv_max_chars`, `pending_input_minutes`.

## Contacts

Contacts are found only on demand, for one posting, when you run `/prepare` (`/contacts` only lists
what is already stored and fetches nothing); never during collection or in the scheduler. Sources are public only: the posting text, the Hacker News
comment for `hn` items, the employer's own `/team` then `/about` page, and the event page for
hackathons. robots.txt is obeyed, at most 2 page fetches per `/prepare`, and the bot stores no
emails or phone numbers.

The bot never fetches LinkedIn (nor ycombinator.com and a list of aggregators and paid contact
finders; the HTTP client refuses them). Instead `/prepare` gives you LinkedIn people-search
buttons to open in your own browser, and a YC companies link for HN postings. If you find a person,
paste their name and headline (or the profile text) with `/addcontact <id>`; a pasted LinkedIn URL
is stored but never opened.

## Requirements

- Python 3.11+ (developed on 3.12).
- A Telegram bot token, your Telegram chat id and an Anthropic API key (see "First run / user actions").

## Setup

Run from the repository root:

```
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill it in (never commit `.env`). Note that `.env.example` is only complete after step 1 of "First run / user actions" below. Check the install:

```
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
```

## Environment variables

Read from `.env` (loaded by `src.main`) or the process environment. Defaults are in `ENV_VARS`
in `src/config.py`; this table is checked against it by a test.

| Name | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | - | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | yes | - | Your chat id; the only chat the bot answers |
| `ANTHROPIC_API_KEY` | yes | - | Claude API key used for scoring |
| `SCORING_MODEL` | no | `claude-haiku-4-5` | Claude model id used for scoring (empty uses the default) |
| `DB_PATH` | no | `data/oppo.db` | SQLite database path |
| `OPPO_CONFIG` | no | `config.toml` | Path to the non-secret config file |
| `TIMEZONE` | no | `Asia/Almaty` | IANA timezone for digest scheduling |
| `DIGEST_TIME` | no | `09:00` | Daily digest time (HH:MM, in TIMEZONE) |
| `COLLECT_CRON_HOURS` | no | `3` | ignored; see [scheduler] in config.toml |
| `MAX_SCORES_PER_RUN` | no | `150` | Maximum LLM scoring calls per run |
| `LOG_LEVEL` | no | `INFO` | Python logging level. App loggers follow it; the `anthropic`, `httpx`, `httpx2`, `httpcore`, `aiogram.event` and `telethon` loggers stay at WARNING even at DEBUG so request bodies (CV, postings, contacts) are never logged, even if `ANTHROPIC_LOG=debug` is set (the bot removes that variable at startup) |
| `HTTP_CONTACT` | no | `https://github.com/trulyaldi/oppo-tg` | Contact (email or URL) placed in the HTTP User-Agent. Default is the project repo; set it to your own contact |
| `HH_ACCESS_TOKEN` | no | - | hh.kz API token, if required (unlocks the hh source) |
| `TG_API_ID` | no | - | Telegram API id for Telethon channel reading |
| `TG_API_HASH` | no | - | Telegram API hash for Telethon channel reading |
| `TG_SESSION_PATH` | no | `data/telethon.session` | Telethon session file path |

## config.toml guide

`config.toml` holds everything that is not a secret. Edit it, then restart `run`.

- `[sources.greenhouse] boards`, `[sources.lever] companies`, `[sources.ashby] boards`: the ATS
  company watchlists (board slugs); the matching `.names` tables give display names.
- `[sources.hh]`: `area` (40 = Kazakhstan), `period` in days and search `queries`. Needs `HH_ACCESS_TOKEN`.
- `[[sources.rss.feeds]]`: `url`, `name`, `kind` (job, internship, research, ...).
- `[[sources.pagewatch.pages]]`: pages to watch for changes (`url`, `name`, `kind`, optional
  `css_selector`, optional `company`: the employer name used for weighting, e.g. `IMC`). Only add pages whose robots.txt allows it.
- `[sources.jobicy]`: `industries` slugs and optional `tags`.
- `[sources.telegram_channels] channels`: public channel handles, for example `["some_channel"]`.
  Empty by default: you must fill it in, and it only works after the Telethon login below.
- `[filters]`: title keywords, spam, unpaid and senior-only keywords, `max_age_days`.
- `[thresholds]`: `digest_min_score`, `alert_score`,
  `alert_deadline_days`, `alert_deadline_min_score`. The digest size comes from `digest_quotas` in
  `config/weights.yaml`.
- `[ranking] weights_file`: path to the weights YAML (see "Employer-region weights").
- `[outreach]`: models, daily cap and limits for CV, `/prepare` and `/draft` (see "CV, prepare and drafts").

## Sources

| source | family | status | notes |
|---|---|---|---|
| greenhouse | ats | counted (no credentials) | Public boards API (boards-api.greenhouse.io); company watchlist in config.toml |
| lever | ats | counted (no credentials) | Public postings API (api.lever.co) |
| ashby | ats | counted (no credentials) | Public job-board API (api.ashbyhq.com) |
| workable | ats | counted once an account or discovered board exists | Public accounts API (www.workable.com/api/accounts), hand-written fixture pending real-response check (D-215) |
| hn | hn | counted (no credentials) | Hacker News "Who is hiring" via the Algolia API |
| rss | rss | counted (no credentials) | RSS/Atom feeds listed in config.toml |
| pagewatch | pagewatch | counted (no credentials) | Change detection on fellowship/internship pages, robots.txt obeyed |
| mlh | mlh | counted (no credentials) | MLH season events page (www.mlh.com) |
| jobicy | jobicy | counted (no credentials) | Jobicy public API, polled at most hourly, credit and link back to Jobicy (D-58) |
| remoteok | remoteok | counted (no credentials) | Remote OK public API, one GET per run and at most hourly, items say "via Remote OK" and link to the Remote OK page (D-178, D-216). Hand-written fixture pending real-response check |
| hh | hh | unlocks with HH_ACCESS_TOKEN | hh.kz, the only Kazakhstan job source: anonymous access gets 403 (D-47, D-57). Register an app at https://dev.hh.ru. Never run live yet |
| telegram_channels | telegram_channels | unlocks with TG_API_ID, TG_API_HASH | Public Kazakh job channels via Telethon with your own account; needs the one-time login and channels in config.toml (D-69). Never run live yet |
| smartrecruiters | - | dropped (D-174) | API host's robots.txt disallows every crawler except LinkedInBot; no module |
| recruitee | - | dropped (D-175) | Careers API needs a per-company token (unauthenticated access is against the documented contract); no module |
| personio | - | dropped (D-176) | Terms could not be read (HTTP 429); no module. Re-check later (user action) |
| remotive | - | dropped (D-177) | robots.txt has `Disallow: /api/*`; no module |
| weworkremotely | - | dropped (D-180) | API terms prohibit storing the data and the terms page returned 403; no module |
| devpost | devpost | dropped (D-50) | Terms forbid automated access; module kept disabled |
| euraxess | euraxess | dropped (D-59) | WAF returns 403 for every query-string request; no module |
| kaggle | kaggle | dropped (D-68) | Terms page unreadable, so no fetching code was written |
| yc | yc | dropped (D-54) | YC terms forbid scraping; no module |

Count rule (D-52): a source family counts toward the "at least 6 sources" goal only if it is
enabled in the committed `config.toml`, works with just the three required env vars and needs no
credentials, and Greenhouse, Lever and Ashby together count as one family (`ats`). Credential-gated
and dropped sources never count, so today there are 7 counted families (ats, hn, rss,
pagewatch, mlh, jobicy, remoteok), one above the minimum (D-60, D-216): if one fails, `HH_ACCESS_TOKEN` adds hh coverage but does
not raise the counted total (credential-gated sources never count, D-52). The only counted buffer is a new
credential-free source that passes the D-31/D-53 checks.

Check live status at any time (real network, no secrets needed, uses a separate DB `data/probe.db`):

```
.venv/bin/python -m src.main probe
```

It prints one line per source and exits 0 only when at least 6 counted families returned items.

## First run / user actions

Only you can do these.

1. Add these lines to `.env.example` (agents cannot edit it, D-03):

   ```
   TELEGRAM_BOT_TOKEN=
   TELEGRAM_CHAT_ID=
   ANTHROPIC_API_KEY=
   SCORING_MODEL=
   DB_PATH=data/oppo.db
   OPPO_CONFIG=config.toml
   TIMEZONE=Asia/Almaty
   DIGEST_TIME=09:00
   COLLECT_CRON_HOURS=3
   MAX_SCORES_PER_RUN=150
   LOG_LEVEL=INFO
   HTTP_CONTACT=
   HH_ACCESS_TOKEN=
   TG_API_ID=
   TG_API_HASH=
   TG_SESSION_PATH=data/telethon.session
   ```

   If your `.env` still has `KAGGLE_USERNAME`/`KAGGLE_KEY` lines (the Kaggle source was dropped, D-68), they are ignored; you can delete them.
   Empty optional values fall back to their defaults.
2. Put real values for `TELEGRAM_BOT_TOKEN` (from @BotFather), `TELEGRAM_CHAT_ID` and
   `ANTHROPIC_API_KEY` in `.env`. To get the chat id: send any message to your bot, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `message.chat.id` (or message
   @userinfobot). The bot answers only that chat.
3. Optional credentials (they unlock extra sources and are not needed for the 6-family minimum):
   - `HH_ACCESS_TOKEN` (recommended for Kazakhstan jobs): see the "hh.kz" section below.
   - `TG_API_ID` and `TG_API_HASH` from https://my.telegram.org, then do the one-time Telethon login
     (it reads the environment, not `.env`, so export the variables first):

     ```
     set -a; . ./.env; set +a
     .venv/bin/python -m src.tools.telethon_login
     ```

     It asks for your phone, the login code and your 2FA password and writes the session file
     (`TG_SESSION_PATH`, gitignored). Keep it private: it grants access to your account.
4. Fill your own items in `config.toml`: extra target companies in the ATS watchlists, and Kazakh
   Telegram channels in `[sources.telegram_channels].channels`.
5. Check the live sources with `.venv/bin/python -m src.main probe` (expect exit 0). Then verify the real
   digest with `.venv/bin/python -m src.main once`, send `/sources` to the bot and check that the counted
   families show ok runs, then start unattended mode with `run`.
6. Run the evals once (below) and record the pass rate in DECISIONS.md. Re-run them after the
   rubric change that made the LLM score region-blind (D-92) and record the result.
7. Outreach and weights:
   - re-run `.venv/bin/python -m pip install -r requirements.txt` (adds pyyaml and pypdf);
   - review and extend `config/watchlists/*.yaml` and `config/weights.yaml`;
   - upload your real CV with `/cv` and try `/prepare` on a saved item (the CV text goes to the
     Anthropic API, see "CV, prepare and drafts");
   - optional: set `SCORING_MODEL` in `.env` to choose the scoring model (empty uses `claude-haiku-4-5`).
8. Coverage and freshness: see "User actions" in PLAN.md section 10 (hh token and contact email,
   join Telegram channels, Personio terms, triage eval, Workable and Remote OK real-response check).
9. Capped live smoke run: `once` with `MAX_SCORES_PER_RUN=20` (at most 20 scoring calls), check the
   digest and flags, and record the result in DECISIONS.md.

## Coverage and freshness

Everything here is in `config.toml` and was tested offline only (see Verification status).

### Tiers, politeness and floors

`run` no longer collects everything every few hours. Each source target belongs to a tier, and each
tier has its own job (`[scheduler]`, D-186, D-207):

- tier1, every 15 min (`tier1_minutes`, jitter up to `tier1_jitter_s` = 60 s): the Greenhouse, Lever
  and Ashby boards you list in `config.toml`.
- tier2, every 90 min (`tier2_minutes`, jitter 300 s): discovered boards, Workable, Remote OK,
  Jobicy, RSS, HN, MLH, hh and Telegram channel polling.
- tier3, every 60 min (`tier3_minutes`, jitter 300 s): pagewatch pages.
- A `[sources.x] tier` value overrides the default. Remote OK has an hourly floor
  (`min_poll_minutes = 60`): it is not fetched twice within 60 minutes. Jobicy responses are cached
  for an hour (D-58).
- Revalidation: if a cached copy exists, requests send `If-None-Match` / `If-Modified-Since`, so an
  unchanged board answers 304. A tier1 target whose host sends no ETag or Last-Modified is polled
  at most every `no_validator_min_minutes` (60).
- `[politeness]`: at least `default_min_interval_s` (2 s) between requests to a host, plus random
  jitter of up to `jitter_fraction` (25%) that is only ever added; `max_concurrent_requests` (4);
  per-host minimums in `[politeness.hosts]` (`remoteok.com` = 5 s).
- Backoff: after a 429, 5xx or 403 the host is skipped for `backoff_base_s` (300 s), doubling per
  consecutive failure up to `backoff_max_s` (6 h); `Retry-After` is honored within that cap. The
  state is stored in SQLite, so it survives a restart. robots.txt stays on for every fetch.
- A newly seen target (a new board or channel) is "baselined" on its first successful fetch: its
  existing postings may appear in the digest, but are never sent as instant or fresh alerts and
  may use at most half of each daily LLM cap (D-188).

### Fresh alerts and digest order

- Existing alert rules are unchanged: base score >= `alert_score` (9.0), or a deadline within
  `alert_deadline_days` (7).
- Fresh alert (header NEW): the item was first seen within `fresh_window_hours` (48), is not a
  baseline item, has a base score >= `fresh_alert_score` (7.5), and at most `fresh_alert_max_per_day`
  (5) are sent per Asia/Almaty day, best base score first. An item that meets an existing rule is
  sent as a normal alert and does not use that cap (D-189, D-208).
- Digest order: items are grouped in score bands of `digest_score_band` (1.0) on the adjusted
  score, higher band first, fresh items first within a band. Quotas apply after sorting.

### Commands added

- `/stats`: median and p90 delivery latency over 7 days (alerts and digest, overall and per
  source), fresh alerts today against the cap, scoring and triage calls today against their caps,
  new postings in the last 24 hours per tier, and median source lag (D-210).
- `/boards`: lists discovered boards (numbered); `/boards remove <n|source:token>` removes one for
  good (it is never rediscovered).
- `/channels`: lists Telegram channels with their status (live, polling, or why not);
  `/channels add <handle>` and `/channels remove <handle>` change the list at runtime without
  editing `config.toml`; at most 50 channels.
- `/sources` now also shows each source's tier.

### Board discovery

- Only already-collected postings are scanned: their URL and the URLs in their descriptions. The bot
  recognizes Greenhouse, Lever, Ashby and Workable board links, rebuilds the public API URL from the
  validated token, and never fetches the extracted URL itself (D-190).
- Each candidate gets one polite validation request. `[discovery]` caps: `max_validations_per_day`
  (40) and `max_new_boards_per_day` (20); invalid candidates are not retried for
  `negative_cache_days` (30). It runs daily at `run_time` (05:30 Asia/Almaty) inside `run`, or on
  demand with `.venv/bin/python -m src.main discover` (needs no Telegram or Claude secrets).
- Valid boards are written to `data/discovered_boards.toml` (runtime data, gitignored, one per
  install) and polled in tier2. Remove one with `/boards remove`. Set `[discovery] enabled = false`
  to turn it off.
- A source is only active when its config list has at least one board, so discovered Workable
  boards need one account in `[sources.workable] accounts` first (D-212).

### Telethon live listening

With `TG_API_ID`, `TG_API_HASH` and the one-time Telethon login done, `run` listens live: new
messages from channels your account has joined arrive within about `live_flush_s` (30 s) and go
through scoring and alerts. You must join the channels yourself in Telegram: `auto_join` is false
by default and the bot will not join for you. Channels that are not joined stay on tier2 polling,
which is also the fallback while the listener is disconnected. Without credentials or a session
the listener does not start (logged once; `/channels` shows why). In `run` mode, polling reuses the
listener's client (D-193, D-218). hh and Telegram postings with an unknown employer region count as
Kazakhstan employers (`region_hint = "kz"`, D-194).

### Triage and daily caps

- Before full scoring, each new posting gets one cheap relevance call (`[triage]`: `model`,
  `max_desc_chars` 500). A "no" is filtered (`triage_no`) and never reaches the scorer; pagewatch
  is exempt (`exempt_sources`). Verdicts are cached; after 3 failed attempts a posting is marked
  `triage_error` (D-196, D-223).
- Daily caps per Asia/Almaty day, retries included: `[scoring] daily_call_cap` (200),
  `[triage] daily_call_cap` (600), and outreach keeps its own 40. Baseline postings may use at most
  `baseline_max_share` (0.5) of the scoring or triage cap. Reaching a cap sends one warning that
  day and is not an error.
- If every triage (or every scoring) call in a run fails, you get one throttled error notice naming
  the stage (D-224). Set `[triage] enabled = false` to skip triage.
- `python -m src.evals.run --triage` measures triage recall on the labeled set (see Evals).

### Hard blockers

`[filters] hard_blocker_keywords` (citizens only, security clearance, polygraph, ...) are searched
in the title and description. By default (`hard_blocker_action = "lower"`) the posting stays in
the digest and /top with a "Hard blocker" flag, its adjusted score is reduced by
`hard_blocker_penalty` (2.0), and it is never sent as an alert (SPEC says to flag, not filter,
work-authorization requirements). `"filter"` drops it before triage instead (D-195, D-222).
Also new in `[filters]`: `negative_title_keywords` (with `protect_title_keywords` as exceptions)
and `sr` as a seniority keyword.

### Sources left out of this round

Dropped sources are in the Sources table with their D-refs (SmartRecruiters D-174, Recruitee D-175,
Personio D-176, Remotive D-177, We Work Remotely D-180). Arbeitnow (D-179) and web-search board
discovery (D-181, D-192) were left out of this round (D-202): there is no Arbeitnow module and no
web-search discovery code, so no web-search cost exists. The earlier verdicts stay on record.

### hh and attribution

hh needs an application token and a contact email (see "hh.kz"). Postings from Remote OK and Jobicy
render "via Remote OK" or "via Jobicy" and link to the aggregator's own page (D-191, D-216).

## Running

```
.venv/bin/python -m src.main run       # unattended: scheduler plus bot polling
.venv/bin/python -m src.main collect   # collect, dedupe, filter, score, send instant alerts
.venv/bin/python -m src.main digest    # send the digest now
.venv/bin/python -m src.main once      # collect, score, alert and send the digest
.venv/bin/python -m src.main probe     # live source check (see Sources)
```

`run` collects per tier (see "Coverage and freshness"; `COLLECT_CRON_HOURS` is ignored), sends the daily digest at `DIGEST_TIME`
(`TIMEZONE`), sends instant alerts and prunes old cache rows at 04:00. Bot commands: `/top`,
`/saved`, `/applied`, `/sources`, `/stats`, `/boards`, `/channels`, `/pause`, `/resume`, plus `/cv`, `/forgetcv`, `/cancel`,
`/prepare`, `/contacts`, `/addcontact` and `/draft` (see "CV, prepare and drafts"). Each item has
feedback buttons.

### Running unattended

Example systemd user unit, `~/.config/systemd/user/oppo-tg.service` (untested example, adjust paths):

```
[Unit]
Description=oppo-tg opportunity bot

[Service]
WorkingDirectory=%h/oppo-tg
ExecStart=%h/oppo-tg/.venv/bin/python -m src.main run
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
```

Then `systemctl --user daemon-reload && systemctl --user enable --now oppo-tg` (and
`loginctl enable-linger $USER` to keep it running after logout). Without systemd:

```
nohup .venv/bin/python -m src.main run >> data/run.log 2>&1 &
```

## Evals

Manual only: this calls the real Claude API and costs a little. `evals/labeled.jsonl` is a set of
23 labeled postings. Export your key (the runner reads the environment) and run:

```
set -a; . ./.env; set +a
.venv/bin/python -m src.evals.run
```

`.venv/bin/python -m src.evals.run --triage` checks the triage model instead: recall on labeled
items with `expected_min >= 5` (default threshold 0.9, saved to `evals/last_triage_run.json`). It has
never been run live (D-183, D-223).

Options: `--threshold 0.8` (default pass rate) and `--limit N`. Exit code 0 on pass, 1 below the
threshold, 2 without `ANTHROPIC_API_KEY`. Output is also saved to `evals/last_run.json` (gitignored).
These evals have never been run yet. Re-run them after any prompt, profile or rubric change.

## Troubleshooting

- "Missing required environment variables": fill the three required values in `.env`.
- `probe` exits 1: a counted family returned no items; read its line and `error:` output, and
  retry later. Never work around a 403 or a robots.txt disallow. Setting `HH_ACCESS_TOKEN` does not
  fix this: credential-gated sources never count (D-52); only a new credential-free source adds
  a counted family. `probe` clears its own page snapshots each run, so re-runs are repeatable.
- The bot does not answer: `TELEGRAM_CHAT_ID` must be your own chat id; other chats are ignored silently.
- hh or telegram_channels shown as `credentials:` or `disabled:` in `/sources`: set the variables,
  complete the Telethon login and add channels (see above).
- Too many or too few results: tune `[filters]` and `[thresholds]` in `config.toml`.
- Scoring stops early: `MAX_SCORES_PER_RUN` caps Claude calls per run.
- `test_source_count` failing after you edited `config.toml`: it reads the real file on purpose and
  fails by design when fewer than 6 credential-free families are enabled. Re-enable a source.
  Other tests use `tests/fixtures/config_repo_snapshot.toml`, so channel or board edits are safe.
- Tests never use the network or `.env`; a failing test is not a credentials problem.

## hh.kz

hh.kz vacancies need an hh application token (D-182).

1. Register an application at https://dev.hh.ru (hh may moderate new apps).
2. Request the application token once with `grant_type=client_credentials` at
   `https://api.hh.ru/token`. Requesting it again revokes the old one, and it has no expiry.
   User tokens expire after 14 days and are not supported.
3. Put it in `HH_ACCESS_TOKEN` and set `HTTP_CONTACT` to your email. hh requires a User-Agent
   naming the app and a contact; the bot sends `oppo-tg/0.1 (<HTTP_CONTACT>)`.

On HTTP 400 or 401/403 the source stops with a plain message (shown in `/sources`) and you get
one Telegram notice per day. D-47 correction: the "terms" link `hh.ru/article/1175` is the
search query-language page; the real terms are at `dev.hh.ru/admin/developer_agreement` (login
needed, not read).
