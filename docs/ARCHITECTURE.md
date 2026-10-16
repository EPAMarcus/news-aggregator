# News Aggregator: Architecture (v0.2, draft)

See `SPEC.md` for requirements and decisions.

## 1. Overview

```
                      GitHub Actions (cron, UTC, private repo)
                                     │
        ┌────────────┬───────────────┼───────────────┬──────────────┐
        │ daily.yml  │ weekly.yml    │ monthly.yml   │ (job chain)  │
        │ 06:00      │ Sun 17:00     │ month end 20h │ yearly job   │
        └─────┬──────┴───────┬───────┴───────┬───────┴──────┬───────┘
              ▼              ▼               ▼              ▼
        python -m aggregator run --level <daily|weekly|monthly|yearly>
              │
              │  1. guard: right local time? output exists? (idempotency)
              │  2. collect inputs
              │       daily  : fetchers → dedupe → items
              │       others : read lower-level .md files from the repo
              │  3. summarize with Claude (per-level prompt, validation, 1 retry)
              │  4. write digests/<level>/... .md
              │  5. commit + push (rebase, concurrency-locked)
              │  6. send email (SMTP)
              ▼
   ┌─────────────────────────┐   ┌──────────────────────────┐
   │ Git repo (source of     │   │ External                 │
   │ truth)                  │   │  RSS / web pages         │
   │  config/  digests/      │   │  Gmail IMAP (newsletters)│
   │  state/   prompts/      │   │  Gmail SMTP (outbound)   │
   └───────────┬─────────────┘   │  Anthropic API           │
               │ git pull        └──────────────────────────┘
               ▼
        Owner's device
```

## 2. Repository layout

```
config/
  sources.yaml            # source list (owner-edited)
  settings.yaml           # timezone, schedule windows, models, categories, limits, recipient
prompts/
  daily.md  weekly.md  monthly.md  yearly.md
src/aggregator/
  cli.py                  # entry point: run --level --date --force
  schedule.py             # local-time guards, period math (ISO weeks, month ends)
  sources/                # rss.py, web.py, newsletter.py (common Item model)
  dedupe.py               # seen-state handling
  health.py               # source health counters and flags
  summarize.py            # Claude calls, output validation
  rollup.py               # input selection for weekly/monthly/yearly (rule 4.1, 4.2)
  render.py               # front matter, Markdown to HTML email
  mailer.py               # SMTP send, alert emails
  store.py                # read/write digests, git commit/push
digests/                  # generated, committed
state/
  seen.json               # item IDs, pruned after 30 days
  source-health.json
tests/
.github/workflows/
  daily.yml  weekly.yml  monthly.yml
pyproject.toml
```

## 3. Components

### 3.1 Scheduling
- GitHub cron runs in UTC and has no daylight-saving support. Each workflow lists the cron entries for both UTC offsets of the owner's timezone. The script then checks local time and proceeds only if it is within a 2-hour window after the target time. Combined with the idempotency check, the second trigger is a no-op.
- GitHub may start scheduled runs 10 to 60 minutes late, which the window absorbs.
- "Last day of month" is not expressible in cron, so `monthly.yml` triggers daily at 20:00 local and the script proceeds only if tomorrow is the 1st.
- **Yearly** is a dependent job in `monthly.yml` (`needs: monthly`) that runs only on 31.12. This guarantees the December monthly is committed first, which a separate 21:00 cron could not.
- All workflows share one `concurrency` group so that pushes never race.

### 3.2 Fetchers (daily only)
All fetchers produce a common `Item` (source id, source topic tag, title, text, published time, stable ID).
- **rss**: `feedparser`; full text from the feed if present, otherwise the summary.
- **web**: fetch the index page, extract article links (optional CSS selector), fetch unseen articles, extract the text with `trafilatura`. Best effort, with timeouts and a polite user agent.
- **newsletter**: IMAP (`imaplib`/`imap-tools`) into the aggregator inbox, filter by sender per source, HTML to text. Message-ID is the stable ID.
- Fetchers run with a per-source timeout and isolated error handling; each result feeds `health.py`.

### 3.3 Dedupe and window
`state/seen.json` holds item IDs. An item is included if it is unseen and not older than 48 hours, so a missed day does not lose items, and nothing is summarized twice.

### 3.4 Summarizer
- Anthropic Python SDK; model per level from `settings.yaml` (default `claude-sonnet-5-5`; verify the current model ID before first run).
- Inputs are passed as clearly delimited data; the prompt states that source text is untrusted content and must never be treated as instructions. The call uses no tools, and the only recipient is the fixed address, so the injection impact is limited to bad text in a digest.
- The daily prompt receives the item list (each item carries its source's optional `topic` tag as a clustering hint) and the category list. Weekly, monthly and yearly prompts receive only lower-level digests.
- Output validation: bullet count for daily, word count for the other levels. One retry with the violation described; if it still fails, the output is accepted with truncation for the daily case and flagged in the alert for the others (decision to confirm during implementation).
- The Sources section for daily digests is rendered by code from the actual contributing sources and health state, not written by the LLM.

### 3.5 Rollup selection
`rollup.py` implements the spec: weekly reads the week's dailies; monthly reads existing fully-contained weeklies plus dailies for all other days (rule 4.1); yearly reads existing monthlies. Missing files are skipped. If nothing is available, no digest is produced and an alert is sent.

### 3.6 Store and sync
The workflow commits only `digests/` and `state/` as the `github-actions[bot]` user, using `GITHUB_TOKEN` with `contents: write`, after `git pull --rebase`. The owner's device pulls on demand. An optional launchd job can pull automatically later.

### 3.7 Mailer
SMTP over TLS to Gmail with an app password. It sends the digest email (HTML plus plain text) and failure alerts. Failure alerts are sent from a `try/except` wrapper around the whole run; if SMTP itself is the failure, GitHub's workflow-failure email covers it.

## 4. Secrets and access

| Secret | Use |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API |
| `GMAIL_ADDRESS` | IMAP login and SMTP sender |
| `GMAIL_APP_PASSWORD` | IMAP + SMTP (requires 2FA on the account) |
| `RECIPIENT_EMAIL` | Digest and alert recipient |

All of them live in GitHub Actions secrets, never in the repo. The repo is private. Please check GitHub's current free-tier limits for private repos (Actions minutes); expected usage is a few minutes per day.

## 5. Failure modes

| Failure | Behavior |
|---|---|
| One source fails | Run continues; counter incremented; flagged after 3 consecutive failures |
| All sources fail / no items | No daily digest; alert email |
| Claude API error | Retry with backoff, then skip the digest and alert |
| Output validation fails twice | Per 3.4 |
| Missing lower-level digests | Skipped, per spec |
| Duplicate or late cron trigger | Idempotency guard and time window |
| Git push conflict | Rebase and retry; concurrency group prevents most cases |
| Gmail credentials expired | Newsletter sources fail health check; alert |
| Workflow disabled by GitHub | Not detected from inside; the missing daily email is the signal |

## 6. Testing

- Unit tests: period math (ISO weeks, month and year boundaries, daylight-saving transitions, the 4.1 selection rule), dedupe, health thresholds, digest validation.
- Fetcher tests use recorded fixtures (sample feed, sample HTML, sample email).
- Summarizer tested with a stubbed client; one manual end-to-end run via `workflow_dispatch` with a `--dry-run` flag (no commit, no email) before enabling the schedule.
- Walk-through scenarios as test cases: 31.12. on a Wednesday, a month ending mid-week, a month ending on Sunday, a missing weekly.

## 7. Implementation phases

1. Skeleton: repo, config loading, period math, tests.
2. Daily pipeline end to end locally (RSS first, then newsletter, then web), dry run.
3. GitHub Actions daily workflow, secrets, email, alerts.
4. Weekly and monthly rollups.
5. Yearly rollup and the year-end chain.
6. Source health reporting and polish (cost measurement after the first week).

## 8. Trade-offs and alternatives considered

- **GitHub Actions vs. BTP/VM:** Actions is free, has no server to maintain, and the git repo gives sync and history. The cost is imprecise cron timing and UTC-only schedules.
- **IMAP app password vs. Gmail OAuth:** The app password does not expire; OAuth tokens for apps in testing mode may expire after 7 days (verify in Google's docs). The trade-off is a less granular permission model.
- **Markdown as source of truth vs. a database:** It is simple and diffable, with no extra infrastructure. Rollup input selection must therefore read and parse files, which is acceptable at this scale.
