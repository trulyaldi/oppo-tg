---
name: digest-format
description: Telegram message layout, length limits, escaping rules and inline-button callbacks for the daily digest and instant alerts. Use when writing or changing digest, alert, or command-output formatting or button handlers.
---

- Layout per item: `<b>Title</b> — Company`, then a reason line, then deadline (or "no deadline"), red-flag note if any, then the link. Digest header: date and item count. 5-10 items.
- Parse mode: HTML. Escape `&`, `<`, `>` in all dynamic text with `html.escape`; never interpolate raw scraped text. Validate URLs (http/https only).
- Limits: a Telegram message is max 4096 characters. Send one message per item, or chunk the digest below 4000 characters, never truncating mid-tag. Callback data max 64 bytes.
- Buttons (inline keyboard, one row): Like / Dislike / Save / Applied. Callback data format `fb:<action>:<posting_id>` where action is `like|dislike|save|applied`. Handlers persist the reaction in SQLite (idempotent, last action wins per action type), answer the callback query, and update the button state.
- Instant alerts (score >= 9 or deadline within 7 days) use the same item layout prefixed with an alert marker. Do not repeat an item already alerted or digested.
- Commands: /top, /saved, /applied, /sources, /pause. Digest sends at 09:00 Asia/Almaty and respects /pause.
- Tests assert exact rendered output, escaping, and length limits against fixtures.
