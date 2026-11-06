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

## Sources (start small, add later)

- ATS JSON endpoints (Greenhouse, Lever, Ashby) for a company watchlist. (implemented: greenhouse in src/sources/greenhouse.py; lever in src/sources/lever.py; ashby in src/sources/ashby.py)
- Hacker News "Who is hiring". (implemented: hn in src/sources/hn.py)
- YC Work at a Startup. (dropped: see D-54; YC startups are covered via HN Who is hiring, hnrss HN Jobs feed and Ashby/Lever boards)
- hh.kz. (implemented: hh in src/sources/hh.py; needs HH_ACCESS_TOKEN, disabled without it)
- Selected Kazakh Telegram job channels via Telethon. (implemented: src/sources/telegram_channels.py, credential-gated, see D-69)
- Hackathon/competition feeds (Devpost, MLH, Kaggle). (implemented: mlh in src/sources/mlh.py; devpost in src/sources/devpost.py but dropped: see D-50, disabled in config; kaggle dropped: see D-68)
- Fellowship and research-program pages. (implemented: rss in src/sources/rss.py for RSS/Atom feeds; pagewatch in src/sources/pagewatch.py for page change watching; EURAXESS research job listings dropped, see D-59, disabled in config)
- Remote job board API: Jobicy. (implemented: jobicy in src/sources/jobicy.py; added by agent, D-55)

**[EDIT: add companies for the ATS watchlist and Telegram channels]**

## Feedback loop

Store every reaction. Include the 20 most recent liked and disliked items as examples in the scoring prompt. Keep a labeled eval set in `evals/` and re-run it after any prompt change.

## Definition of done

- All tests green.
- Bot starts and sends a real digest to the user's chat.
- At least 6 sources working.
- Feedback buttons persist.
- README with setup and run instructions.
- `.env.example` complete.
- Scheduler runs unattended.
- DECISIONS.md explains every non-obvious choice.
