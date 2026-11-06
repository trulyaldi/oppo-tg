# SPEC

A personal Telegram bot that finds, ranks and delivers opportunities tuned to one user. Items marked **[EDIT]** are for the user to fill in.

## User profile

- Master's student in physics at Nazarbayev University (Astana), BSc in Mathematics from the same university; expected graduation 2028.
- Research assistant since Jan 2025: computational physics, statistical modeling, numerical optimization, uncertainty quantification.
- Self-study: reimplementing ML architectures from scratch (GPT-2, DeepSeek-style), hackathons, Kaggle-style competitions.

## Goals, in priority order

1. Summer 2027 internship: Data Science / ML / AI engineer, or Quant Analyst / Quant Researcher. Named target: IMC; also similar firms (Optiver, Jane Street, Citadel, HRT, Jump, DRW, Two Sigma, Squarespace-tier tech, top AI labs and startups). **[EDIT: add more]**
2. Hackathons, competitions and fellowships with strong career upside (winning is a route to hiring).
3. Early-stage startups, in Kazakhstan or remote, in ML or quant-adjacent areas. Long-term goal: build a quant-related startup in Kazakhstan.
4. Research programs and visiting positions that could lead toward a PhD later.
5. Full-time junior roles are lower priority until 2028.

## Geography

Kazakhstan (Astana, Almaty) and fully remote worldwide score high. On-site in major quant hubs (London, Amsterdam, Chicago, New York, Singapore, Hong Kong) is acceptable for top-tier firms. Flag visa/work-authorization requirements in the output; do not filter on them.

## Scoring rubric (0-10)

| Criterion | Weight |
|---|---|
| Skill/background match | 35% |
| Career upside and prestige of the opportunity | 25% |
| Interest/novelty | 15% |
| Timing (deadline realistic, summer 2027 window) | 15% |
| Accessibility (location/visa/eligibility for a student) | 10% |

Output JSON: `score`, one-line `reason`, `deadline` if known, `red_flags`, `tags`.

Hard filters (applied before the LLM): obvious spam, unpaid "exposure" roles, senior-only roles, duplicates.

## Delivery

- Daily digest at 09:00 Asia/Almaty with the top 5-10 items, each with a reason line, link, deadline, and inline buttons: Like / Dislike / Save / Applied.
- Instant alert for items scoring >= 9 or with a deadline within 7 days.
- Commands: /top, /saved, /applied, /sources, /pause.
- Added 2026-11-06: the digest is composed by employer-region quotas (default 10 items: 4 Kazakhstan, 4 international, 2 best remaining, unused slots fall through), ranked by the adjusted score, with a flag tag per item (Kazakhstan / world). The digest quality floor and instant alerts use the base LLM score, never the adjusted one: alert when base score >= 9 or the deadline is within 7 days. See "Employer-region weighting".
- Added 2026-11-06: further commands /cv, /forgetcv, /prepare, /contacts, /addcontact, /draft, /cancel, and a Prepare button under liked or saved items. See the sections below.

## Sources (start small, add later)

- ATS JSON endpoints (Greenhouse, Lever, Ashby) for a company watchlist. (implemented: greenhouse in src/sources/greenhouse.py; lever in src/sources/lever.py; ashby in src/sources/ashby.py)
- Workable public accounts API for a company watchlist and discovered boards. (implemented: workable in src/sources/workable.py, see D-173, D-215)
- Hacker News "Who is hiring". (implemented: hn in src/sources/hn.py)
- YC Work at a Startup. (dropped: see D-54; YC startups are covered via HN Who is hiring, hnrss HN Jobs feed and Ashby/Lever boards)
- hh.kz. (implemented: hh in src/sources/hh.py; needs HH_ACCESS_TOKEN, disabled without it)
- Selected Kazakh Telegram job channels via Telethon. (implemented: src/sources/telegram_channels.py, credential-gated, see D-69)
- Hackathon/competition feeds (Devpost, MLH, Kaggle). (implemented: mlh in src/sources/mlh.py; devpost in src/sources/devpost.py but dropped: see D-50, disabled in config; kaggle dropped: see D-68)
- Fellowship and research-program pages. (implemented: rss in src/sources/rss.py for RSS/Atom feeds; pagewatch in src/sources/pagewatch.py for page change watching; EURAXESS research job listings dropped, see D-59, disabled in config)
- Remote job board API: Jobicy. (implemented: jobicy in src/sources/jobicy.py; added by agent, D-55)
- Remote job board API: Remote OK, with "via Remote OK" attribution. (implemented: remoteok in src/sources/remoteok.py, see D-178, D-216)

**[EDIT: add companies for the ATS watchlist and Telegram channels]**

## Feedback loop

Store every reaction. Include the 20 most recent liked and disliked items as examples in the scoring prompt. Keep a labeled eval set in `evals/` and re-run it after any prompt change.

## Employer-region weighting (added 2026-11-06)

- Region is decided by where the employer is headquartered or legally registered, not by where the job is. A remote role at a Kazakhstan company counts as Kazakhstan. A Kazakhstan office of a foreign company counts as international.
- Categories and default additive adjustments, all editable in `config/weights.yaml`:

  | Category | Adjustment |
  |---|---|
  | intl_top_tier | +1.0 |
  | intl_other | 0 |
  | kz_employer | +0.5 |
  | kz_startup | +1.0 |
  | intl_onsite_visa_barrier | -0.5 |

  intl_onsite_visa_barrier is a modifier on international roles that are on-site and need a work permit.
- Watchlist files list the top-tier international employers (IMC, Optiver, Jane Street, Citadel, HRT, Jump, DRW, Two Sigma, top AI labs) and known Kazakhstan employers, flagged startup or not. Watchlist matches win.
- For unknown employers the scorer also returns a region guess with a confidence. An unsure guess gets no adjustment.
- The LLM score stays region-blind: the employer's home country never changes it, though job-location accessibility still counts. Adjustments are applied in code after scoring. The result is clamped to 0-10, and both the base score and the adjusted score are stored. Existing scored items are migrated.
- Digest quotas, editable in the same YAML file: 4 Kazakhstan, 4 international, 2 best remaining. Slots a group cannot fill fall through to the best remaining items.
- Editing the YAML files changes the ranking and the quotas on the next digest, with no code change.

## CV (added 2026-11-06)

- `/cv` asks for a PDF or pasted text.
- Before upload and in the confirmation, the bot says that the CV text is sent to the Anthropic API.
- What is stored:
  - The extracted text, its sections (education, experience, projects, skills, parsed by the LLM and checked against the text), a version number and a content hash.
  - All of it lives in the local database under the untracked `data/` folder.
  - The original file is never written to disk or committed.
- A new upload with different text bumps the version and invalidates every cached tailoring and draft. Re-sending identical text changes nothing.
- `/forgetcv` deletes the CV and everything derived from it.
- Tests use only a fictional fixture CV.

## Contacts (added 2026-11-06)

- Contacts come only from public sources, looked up on demand when the user runs `/prepare` (`/contacts` lists the stored ones), never in bulk:
  - the posting text;
  - Hacker News "Who is hiring" contact lines and posters;
  - the employer's own team or about page;
  - hackathon or conference organizer pages.
- Fetching respects robots.txt and rate limits, and uses the shared polite HTTP client.
- YC company pages are never fetched (YC terms forbid scraping). The bot offers a link for the user to open instead.
- LinkedIn is human-assisted only:
  - The bot builds a LinkedIn people-search link per company and role type for the user to open.
  - The user may paste profile text or a profile URL into the chat.
  - The bot never fetches LinkedIn.
- Only name, role, company, source URL, a short note and an expiry are stored. Each contact is classified as recruiter, hiring manager, founder, engineer or other, with a confidence.
- No email addresses or phone numbers are stored, no emails are guessed, and no paid contact finders are used.
- Contacts are deleted when the opportunity's deadline passes or after 60 days, whichever comes first.

## Prepare and draft (added 2026-11-06)

- `/prepare <id>`, or the Prepare button under a liked or saved item, returns a compact message with:
  - a fit summary;
  - what to emphasize, cut or reorder in the CV;
  - keywords that truthfully match both the CV and the posting;
  - honest gaps and how to address them;
  - the contacts found, with their type.
- `/draft <contact>` writes a 3-5 sentence outreach message adjusted to the contact type, with one specific hook and one clear ask.
- Hard rule: never invent or inflate. Every factual claim about the user must trace to the CV text. A verification step checks claims against the CV and removes anything unsupported, and the output says how many statements were removed.
- Nothing is ever sent to anyone. Drafts are shown in the user's own chat for copying.
- Posting, page and pasted-profile text is untrusted data, never instructions. Model outputs are schema-validated. The model gets no tools or actions that could contact anyone.
- Cost control:
  - Results are cached per posting and CV version (drafts per contact and CV version) and never regenerated.
  - A daily cap limits outreach LLM calls.
  - A cheap model is used where adequate.

## Privacy (added 2026-11-06)

- CV text, contacts, pasted profiles and drafts are never logged and never committed. They live only in the local database under `data/`.
- Data sent to the Anthropic API: posting text for scoring, plus CV text, posting text and contact details for `/cv`, `/prepare` and `/draft`.
- The bot ignores every chat except the configured user's chat.

## Definition of done

- All tests green.
- Bot starts and sends a real digest to the user's chat.
- At least 6 sources working.
- Feedback buttons persist.
- README with setup and run instructions.
- `.env.example` complete.
- Scheduler runs unattended.
- DECISIONS.md explains every non-obvious choice.

## Definition of done: outreach and weights (added 2026-11-06)

- Employer-region adjustments, clamping, digest quotas with fall-through and YAML-only edits are covered by tests.
- A clearly bad Kazakhstan posting cannot outrank a clearly good international one under the default weights (tested).
- `/cv` ingests a PDF or pasted text, stores it only under `data/`, and never logs it.
- `/prepare` and `/draft` output passes the claims-vs-CV verification, including a fabrication test fixture.
- Contacts come only from the listed public sources, expire on schedule, and LinkedIn is never fetched (tested).
- A live smoke run with at most 20 scoring calls is recorded in DECISIONS.md, or listed as a user action.
