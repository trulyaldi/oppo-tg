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

## Requirements

- Python 3.11+ (developed on 3.12).
- A Telegram bot token, your Telegram chat id and an Anthropic API key (see "First run / user actions").

## Setup

Run from the repository root:

```
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill it in (never commit `.env`). Check the install:

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
| `LOG_LEVEL` | no | `INFO` | Python logging level |
| `HTTP_CONTACT` | no | `https://github.com/oppo-tg` | Email or URL placed in the HTTP User-Agent (set your own) |
| `HH_ACCESS_TOKEN` | no | - | hh.kz API token, if required (unlocks the hh source) |
| `KAGGLE_USERNAME` | no | - | Unused: the Kaggle source was dropped (D-68) |
| `KAGGLE_KEY` | no | - | Unused: the Kaggle source was dropped (D-68) |
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
  `css_selector`). Only add pages whose robots.txt allows it.
- `[sources.jobicy]`: `industries` slugs and optional `tags`.
- `[sources.telegram_channels] channels`: public channel handles, for example `["some_channel"]`.
  Empty by default: you must fill it in, and it only works after the Telethon login below.
- `[filters]`: title keywords, spam, unpaid and senior-only keywords, `max_age_days`.
- `[thresholds]`: `digest_min_score`, `digest_max_items`, `digest_min_items`, `alert_score`,
  `alert_deadline_days`, `alert_deadline_min_score`.

## Sources

| source | family | status | notes |
|---|---|---|---|
| greenhouse | ats | counted (no credentials) | Public boards API (boards-api.greenhouse.io); company watchlist in config.toml |
| lever | ats | counted (no credentials) | Public postings API (api.lever.co) |
| ashby | ats | counted (no credentials) | Public job-board API (api.ashbyhq.com) |
| hn | hn | counted (no credentials) | Hacker News "Who is hiring" via the Algolia API |
| rss | rss | counted (no credentials) | RSS/Atom feeds listed in config.toml |
| pagewatch | pagewatch | counted (no credentials) | Change detection on fellowship/internship pages, robots.txt obeyed |
| mlh | mlh | counted (no credentials) | MLH season events page (mlh.io) |
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
   KAGGLE_USERNAME=
   KAGGLE_KEY=
   TG_API_ID=
   TG_API_HASH=
   TG_SESSION_PATH=data/telethon.session
   ```

   `KAGGLE_*` are unused (the source was dropped, D-68); drop those two lines if you prefer.
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
6. Run the evals once (below) and record the pass rate in DECISIONS.md.

## Running

```
.venv/bin/python -m src.main run       # unattended: scheduler plus bot polling
.venv/bin/python -m src.main collect   # collect, dedupe, filter, score
.venv/bin/python -m src.main digest    # send the digest now
.venv/bin/python -m src.main once      # collect, score, alert and send the digest
.venv/bin/python -m src.main probe     # live source check (see Sources)
```

`run` collects every `COLLECT_CRON_HOURS` hours, sends the daily digest at `DIGEST_TIME`
(`TIMEZONE`), sends instant alerts and prunes old cache rows at 04:00. Bot commands: `/top`,
`/saved`, `/applied`, `/sources`, `/pause`, `/resume`. Each item has feedback buttons.

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
- Tests never use the network or `.env`; a failing test is not a credentials problem.
