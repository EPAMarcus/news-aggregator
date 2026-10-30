# News Aggregator: Architecture (v0.3, draft)

See `SPEC.md` for requirements and decisions.

## 1. Overview

```
                      GitHub Actions (cron, UTC, private repo)
                                     │
        ┌────────────┬───────────────┼───────────────┬──────────────┐
        │ daily.yml  │ weekly.yml    │ monthly.yml   │ (job chain)  │
        │ 06:00 UTC  │ Sun 17:00 UTC │ month end 20h │ yearly job   │
        └─────┬──────┴───────┬───────┴───────┬───────┴──────┬───────┘
              ▼              ▼               ▼              ▼
        python -m aggregator run --level <daily|weekly|monthly|yearly>
              │
              │  1. guard: due today? output exists? (idempotency)
              │  2. collect inputs
              │       daily  : fetchers → dedupe → items
              │       others : read lower-level .md files from the repo
              │  3. summarize with Claude (per-level prompt, validation, 1 retry)
              │  4. write digests/<level>/... .md
              │  5. commit + push (rebase, concurrency-locked)
              │  6. create GitHub release (gh)
              │  on failure: alert issue (gh), exit 1
              ▼
   ┌─────────────────────────┐   ┌──────────────────────────┐
   │ Git repo (source of     │   │ External                 │
   │ truth)                  │   │  RSS / web pages         │
   │  config/  digests/      │   │  Gmail IMAP (deferred)   │
   │  state/   prompts/      │   │  GitHub releases, issues │
   └───────────┬─────────────┘   │  Anthropic API           │
               │ git pull        └──────────────────────────┘
               ▼
        Owner's device
```

## 2. Repository layout

```
config/
  sources.yaml            # source list (owner-edited)
  settings.yaml           # models, categories, limits, stale_after_days
prompts/
  daily.md  weekly.md  monthly.md  yearly.md
src/aggregator/
  cli.py                  # entry point: run --level --date --force
  schedule.py             # run guards (month end, year end), period math (ISO weeks)
  sources/                # rss.py, web.py, newsletter.py (deferred) (common Item model)
  dedupe.py               # seen-state handling
  health.py               # source health counters and flags
  summarize.py            # Claude calls (delimited untrusted data, backoff, revision turn)
  validate.py             # output validation (daily structure and bullet limit, truncation)
  daily.py                # daily prompt, validation, one retry
  rollup.py               # input selection for weekly/monthly/yearly (rule 4.1, 4.2)
  render.py               # front matter, digest Markdown
  release.py              # GitHub release per digest (gh CLI)
  alerts.py               # run wrapper, alert issues (gh CLI)
  store.py                # read/write digests
  gitsync.py              # git commit/push of digests/ and state/
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
- All schedules are in UTC, with one cron entry per workflow. GitHub cron has no daylight-saving support, so in Berlin the runs shift by an hour between summer and winter, which is accepted. The times are chosen so that the UTC date equals the Berlin date; a digest is labelled with the UTC date its run starts on.
- GitHub may start scheduled runs 10 to 60 minutes late. No time check is needed for that; the idempotency check makes duplicate triggers harmless.
- "Last day of month" is not expressible in cron, so `monthly.yml` triggers on days 28 to 31 at 20:00 UTC and the script proceeds only if tomorrow is the 1st (`schedule.is_due`).
- **Yearly** is a dependent job in `monthly.yml` (`needs: monthly`) that runs only on 31.12. This guarantees the December monthly is committed first, which a separate 21:00 cron could not.
- All workflows share one `concurrency` group (`digests`, queued, never cancelled) so that pushes never race.
- `daily.yml` runs at 06:00 UTC and has `workflow_dispatch` inputs `level`, `date`, `force` and `dry_run` (default true) for manual runs and backfills. Inputs reach the run script as environment variables, never interpolated into it. The checkout has the full history for the rebase before the push.

### 3.2 Fetchers (daily only)
All fetchers produce a common `Item` (source id, source topic tag, title, text, published time, stable ID).
- **rss**: `feedparser`; full text from the feed if present, otherwise the summary.
- **web**: fetch the index page, extract article links (optional CSS selector), fetch unseen articles, extract the text with `trafilatura`. Best effort, with timeouts and a polite user agent.
- **newsletter** (deferred, #12): IMAP (`imaplib`/`imap-tools`) into the aggregator inbox, filter by sender per source, HTML to text. Message-ID is the stable ID.
- Fetchers run with a per-source timeout and isolated error handling; each result feeds `health.py`.

### 3.3 Dedupe and window
`state/seen.json` holds item IDs. An item is included if it is unseen and not older than 48 hours, so a missed day does not lose items, and nothing is summarized twice.

### 3.4 Summarizer
- Anthropic Python SDK; model per level from `settings.yaml` (default `claude-sonnet-5-5`; verify the current model ID before first run).
- Inputs are passed as clearly delimited data; the prompt states that source text is untrusted content and must never be treated as instructions. The call uses no tools, and the output only goes to a release in the private repo, so the injection impact is limited to bad text in a digest.
- The daily prompt receives the item list (each item carries its source's optional `topic` tag as a clustering hint) and the category list. Weekly, monthly and yearly prompts receive only lower-level digests; daily inputs are passed without their Sources section and article links.
- Output validation: bullet count for daily, word count (headings not counted) for the other levels. Weekly may not exceed `weekly_words`; monthly and yearly, whose limits are approximate, may exceed theirs by 10%. One retry with the violation described; if it still fails, the daily output is truncated and the others are kept as they are, and both are logged as warnings. The final policy is decided in #26.
- The Sources section for daily digests is rendered by code from the actual contributing sources and health state, not written by the LLM.
- Each daily bullet must end with the index numbers of the input documents it is based on (`[3]`, `[3, 7]`); a missing citation or an unknown index is a validation problem (one retry). Code then replaces the citation with the source names linked to the item URLs (`render.link_citations`). The LLM never sees or writes URLs, so links cannot be invented or injected through source content; feed URLs are accepted only as plain http(s) URLs.

### 3.5 Rollup selection
`rollup.py` implements the spec: weekly reads the week's dailies; monthly reads existing fully-contained weeklies plus dailies for all other days (rule 4.1); yearly reads existing monthlies. Missing files are skipped. If nothing is available, no digest is produced and an alert is raised.

### 3.6 Store and sync
`gitsync.py` commits only `digests/` and `state/` (as the `github-actions[bot]` user in Actions, with the user's own git identity locally), then runs `git pull --rebase --autostash` and pushes to the current branch, using `GITHUB_TOKEN` with `contents: write`. A rejected push is retried with a new rebase, up to 3 attempts; a rebase conflict is aborted and fails the step. The commit step returns the SHA of the pushed commit, which the release step uses as the tag target. The owner's device pulls on demand. An optional launchd job can pull automatically later.

### 3.7 Release
After the push, `release.py` creates the release with the `gh` CLI (`GH_TOKEN` is the workflow's `GITHUB_TOKEN`): tag `<level>-<period>` on the pushed commit, title `[Digest] Daily 2026-10-15`, body the digest without front matter (spec 6). The `gh` call is injected so that tests can stub it. The step is idempotent: an existing release is left alone. When the guard finds the digest already written but no release for it, the run creates the release, so a failed publish is repaired by rerunning. A dry run creates no release. The owner is notified by GitHub's release notifications; nothing is sent by email.

### 3.8 Alerts
A `try/except` wrapper around the whole run (in `cli.py`) turns every error outcome (no inputs, LLM failure after retries, all sources failing, failed commit or release), a configuration error and any unexpected exception into an alert: the run exits non-zero, which triggers GitHub's workflow-failure email, and `alerts.py` opens an issue `[Alert] Daily 2026-10-15` labelled `alert`, assigned to the owner, with the level, period, errors, warnings and the run log. The label is created if it is missing. The assignee is `ALERT_ASSIGNEE` if set, else the repository owner (`GITHUB_REPOSITORY_OWNER` in Actions), else the logged-in `gh` user. A repeated failure for the same digest comments on the open alert issue instead of opening a new one. If creating the issue fails, the non-zero exit and GitHub's failure email remain. A dry run raises no alert. Warnings alone (one failing source, a truncated daily digest) raise no alert.

## 4. Secrets and access

| Secret | Use |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API |
| `GITHUB_TOKEN` | Provided by Actions, not stored. Permissions `contents: write` (commit, release) and `issues: write` (alerts); passed to `gh` as `GH_TOKEN` |

If the newsletter fetcher (#12) resumes, `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` (IMAP, requires 2FA on the account) are added. Secrets live in GitHub Actions secrets, never in the repo. The repo is private. Please check GitHub's current free-tier limits for private repos (Actions minutes); expected usage is a few minutes per day.

## 5. Failure modes

| Failure | Behavior |
|---|---|
| One source fails | Run continues; counter incremented; flagged after 3 consecutive failures |
| All sources fail / no items | No daily digest; alert |
| Claude API error | Retry with backoff, then skip the digest and alert |
| Output validation fails twice | Per 3.4 |
| Missing lower-level digests | Skipped, per spec |
| Duplicate or late cron trigger | Idempotency guard |
| Git push conflict | Rebase and retry; concurrency group prevents most cases |
| Commit or release fails after the write | Digest file stays; alert; a rerun creates the missing release |
| Alert issue cannot be created | Non-zero exit; GitHub's workflow-failure email |
| Workflow disabled by GitHub | Not detected from inside; the missing daily release notification is the signal |

## 6. Testing

- Unit tests: period math (ISO weeks, month and year boundaries, the 4.1 selection rule), dedupe, health thresholds, digest validation.
- Fetcher tests use recorded fixtures (sample feed, sample HTML; sample email once #12 resumes).
- Release and alert steps tested with a stubbed `gh`.
- Summarizer tested with a stubbed client; one manual end-to-end run via `workflow_dispatch` with a `--dry-run` flag (no commit, no release) before enabling the schedule.
- Walk-through scenarios as test cases: 31.12. on a Wednesday, a month ending mid-week, a month ending on Sunday, a missing weekly.

## 7. Implementation phases

1. Skeleton: repo, config loading, period math, tests.
2. Daily pipeline end to end locally (RSS first, then web; newsletter deferred), dry run.
3. GitHub Actions daily workflow, secrets, releases, alerts.
4. Weekly and monthly rollups.
5. Yearly rollup and the year-end chain.
6. Source health reporting and polish (cost measurement after the first week).

## 8. Trade-offs and alternatives considered

- **GitHub Actions vs. BTP/VM:** Actions is free, has no server to maintain, and the git repo gives sync and history. The cost is imprecise cron timing and UTC-only schedules.
- **Email via SMTP vs. GitHub release:** Sending email from the workflow is not possible for the owner, so each digest is a release and GitHub's release notification is the email. No mail credentials are needed and the digest is browsable in the repo. The trade-off is less control over the email's look, and delivery depends on the owner's GitHub notification settings.
- **IMAP app password vs. Gmail OAuth** (newsletters, deferred): The app password does not expire; OAuth tokens for apps in testing mode may expire after 7 days (verify in Google's docs). The trade-off is a less granular permission model.
- **Markdown as source of truth vs. a database:** It is simple and diffable, with no extra infrastructure. Rollup input selection must therefore read and parse files, which is acceptable at this scale.
