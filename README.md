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
- A live smoke run with at most 20 scoring calls is recorded in DECISIONS.md (D-166): `once` scored 20, sent a 10-item digest. Not seen live: KZ flags and a mixed quota batch, and /cv, /prepare, /draft (offline tests only).

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
| `COLLECT_CRON_HOURS` | no | `3` | Collection interval in hours |
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
| hn | hn | counted (no credentials) | Hacker News "Who is hiring" via the Algolia API |
| rss | rss | counted (no credentials) | RSS/Atom feeds listed in config.toml |
| pagewatch | pagewatch | counted (no credentials) | Change detection on fellowship/internship pages, robots.txt obeyed |
| mlh | mlh | counted (no credentials) | MLH season events page (www.mlh.com) |
| jobicy | jobicy | counted (no credentials) | Jobicy public API, polled at most hourly, credit and link back to Jobicy (D-58) |
| hh | hh | unlocks with HH_ACCESS_TOKEN | hh.kz, the only Kazakhstan job source: anonymous access gets 403 (D-47, D-57). Register an app at https://dev.hh.ru. Never run live yet |
| telegram_channels | telegram_channels | unlocks with TG_API_ID, TG_API_HASH | Public Kazakh job channels via Telethon with your own account; needs the one-time login and channels in config.toml (D-69). Never run live yet |
| devpost | devpost | dropped (D-50) | Terms forbid automated access; module kept disabled |
| euraxess | euraxess | dropped (D-59) | WAF returns 403 for every query-string request; no module |
| kaggle | kaggle | dropped (D-68) | Terms page unreadable, so no fetching code was written |
| yc | yc | dropped (D-54) | YC terms forbid scraping; no module |

Count rule (D-52): a source family counts toward the "at least 6 sources" goal only if it is
enabled in the committed `config.toml`, works with just the three required env vars and needs no
credentials, and Greenhouse, Lever and Ashby together count as one family (`ats`). Credential-gated
and dropped sources never count, so today there are exactly 6 counted families (ats, hn, rss,
pagewatch, mlh, jobicy) and no spare margin (D-60): if one fails, `HH_ACCESS_TOKEN` adds hh coverage but does
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
   - `HH_ACCESS_TOKEN` (recommended for Kazakhstan jobs): register an application at https://dev.hh.ru.
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
8. Capped live smoke run: `once` with `MAX_SCORES_PER_RUN=20` (at most 20 scoring calls), check the
   digest and flags, and record the result in DECISIONS.md.

## Running

```
.venv/bin/python -m src.main run       # unattended: scheduler plus bot polling
.venv/bin/python -m src.main collect   # collect, dedupe, filter, score, send instant alerts
.venv/bin/python -m src.main digest    # send the digest now
.venv/bin/python -m src.main once      # collect, score, alert and send the digest
.venv/bin/python -m src.main probe     # live source check (see Sources)
```

`run` collects every `COLLECT_CRON_HOURS` hours, sends the daily digest at `DIGEST_TIME`
(`TIMEZONE`), sends instant alerts and prunes old cache rows at 04:00. Bot commands: `/top`,
`/saved`, `/applied`, `/sources`, `/pause`, `/resume`, plus `/cv`, `/forgetcv`, `/cancel`,
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
