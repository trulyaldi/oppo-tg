---
name: digest-format
description: Telegram message layout, length limits, escaping rules and inline-button callbacks for the daily digest and instant alerts. Use when writing or changing digest, alert, or command-output formatting or button handlers.
---

Source of truth: `src/delivery/format.py` (`render_item`, `feedback_keyboard`), D-260 and D-283, and the snapshots `tests/fixtures/render_item_*_expected.txt`.

- Item layout, exactly two lines plus the link line (parse mode HTML):
  1. `<flag> <b>Title</b> — Company · City · until YYYY-MM-DD · ⚠ marker`. Flag (KZ or intl emoji) comes from `config/weights.yaml` and may be empty. City, deadline and marker are each omitted when unknown. Alerts prefix `<b>ALERT</b> · `, fresh alerts `<b>NEW</b> · `.
  2. `Relevance N/10: <reason>`. N is the base score (LLM relevance, no trailing `.0`); the adjusted score only orders items. Old rows without the v2 contract get the reason clipped to 110 characters at a word boundary.
  3. The link (http/https only, validated with `safe_url`). Aggregator items append ` · via Remote OK`, `via Jobicy` or `via Arbeitnow` on this same line (credit required by their terms); it is not a separate line.
- Marker: at most one, clipped to 24 characters, priority hard blocker > scam / personal contact only > visa barrier > first model red flag. There is no `Deadline:` line, no `Red flags:` paragraph and no score-adjustment detail in the item (`/prepare` still shows it).
- Limits: line 1 is at most 220 characters (fields shrink on raw text, so no tag or entity is cut); title 90, company 50, city 30. An item is at most 3500 characters. A Telegram message is max 4096 characters. Digest header: `Daily digest <date> · N items`, then one message per item. Callback data max 64 bytes.
- Parse mode: HTML. Escape `&`, `<`, `>` in all dynamic text with `html.escape`; never interpolate raw scraped text.
- Buttons: one inline row of five: Like, Dislike, Save, Applied, Prepare. Callbacks are `fb:<action>:<posting_id>` with action `like|dislike|save|applied`, and `pr:<posting_id>` for Prepare (always present). Handlers persist the reaction in SQLite (idempotent, last action wins per action type), answer the callback query, and mark the active button with a leading check mark. Discovery candidates use `tc:add:<handle>` and `tc:ign:<handle>`.
- Used by the digest, alerts, `/top`, `/search` and `/add` replies. Digest size is the sum of `digest_quotas` in `config/weights.yaml` (6 KZ, 3 intl, 1 best remaining).
- Instant alerts (base score >= `alert_score`, a near deadline, or a fresh item over the regional threshold) use the same item layout with the ALERT or NEW prefix. Do not repeat an item already alerted or digested.
- Commands: /top, /search, /saved, /applied, /sources, /stats, /add, /boards, /channels, /findchannels, /pause, /resume, /cv, /cvreview, /forgetcv, /cancel, /prepare, /contacts, /addcontact, /draft. Digest sends at 09:00 Asia/Almaty (`DIGEST_TIME`) and respects /pause.
- Tests assert exact rendered output, escaping, and length limits against fixtures. Change a snapshot only in the task that owns it (D-250).
