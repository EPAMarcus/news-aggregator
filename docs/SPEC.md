# News Aggregator: Specification (v0.3, draft)

## 1. Goal

A personal, hands-off news aggregator. It collects items from a configurable list of sources, produces concise high-level digests (daily, weekly, monthly, yearly), publishes each digest as a GitHub release, and stores each one as a Markdown file in a git repository that syncs to the owner's device. It runs in the cloud on a fixed schedule and does not depend on the owner's machine being on.

## 2. Decisions so far

| Topic | Decision |
|---|---|
| Hosting / scheduler | GitHub Actions (cron), **private** repo |
| LLM | Claude API (owner's existing key), model configurable per level, default Sonnet |
| Language | Python |
| Source of truth | The Markdown files in the git repo (no database) |
| Device sync | `git pull` from the repo |
| Source list | `config/sources.yaml` in the repo |
| Newsletter intake | Deferred (#12). Planned: dedicated Gmail account (`marcus.newsletters.aggregator@gmail.com`), read via IMAP with an app password |
| Notification | GitHub's release notification email; the aggregator sends no email itself |
| Content scope | Only sources listed in `sources.yaml`; public content only; English output |
| Delivery | One GitHub release per digest, at every level |
| Gaps | A missing or failed period is skipped, never blocks later runs |

## 3. Sources

Types:
- **rss**: RSS/Atom feed URL (also used for blogs and news sites that have a feed).
- **web**: Best-effort scraping of a page that has no feed (index page URL, optional CSS selector for article links).
- **newsletter** (deferred, #12): Mail received in the aggregator inbox, matched by sender address.

Each entry has: `id`, `name` (shown in the digest), `type`, type-specific fields, optional `topic` tag, optional `enabled` flag.

### Topic tags
- `topic` is an optional string in `sources.yaml`, either a top-level category (`Tech`) or a category path (`Tech/AI`).
- The tag is passed to the LLM together with each item as a **strong hint** for clustering. The LLM may still place an item elsewhere when its content clearly belongs to another topic (a general news site that covers many topics).
- The top-level part must be one of the categories in `settings.yaml` (see 5), each a single word; a source with an unknown top-level category makes config validation fail with a clear error. Sub-categories are free-form.
- Untagged sources are clustered by the LLM alone.

Example:
```yaml
sources:
  - id: sap-news
    name: SAP News Center
    type: rss
    url: https://news.sap.com/feed/
    topic: SAP
  - id: some-ai-newsletter
    name: Example AI Newsletter
    type: newsletter
    from: news@example.com
    topic: Tech/AI
  - id: general-news
    name: Example News
    type: web
    url: https://example.com/news
    # no topic: clustered by the LLM
```

### Source health
A source is **failing** when a fetch errors, times out, or returns no parseable entries at all. Having no new items is not a failure. After **3 consecutive failing runs**, the source is flagged in the daily Sources section (and so in the release) with the suggestion to remove or fix it. A source that has produced no items at all for `stale_after_days` (default 30) is flagged as **stale**, which catches an unconfirmed newsletter subscription. The health counters live in `state/source-health.json`.

## 4. Schedule

All schedules are in UTC (GitHub cron has no timezone or daylight-saving support). A run may start up to an hour late, and a digest is labelled with the UTC date its run starts on. In Berlin the daily digest arrives at 07:00 or 08:00.

| Level | When | Input |
|---|---|---|
| Daily | every day 06:00 UTC | New items from all sources since the previous run (06:00 to 06:00 UTC window) |
| Weekly | Sunday 17:00 UTC | The daily digests of the week (Monday to Sunday, by date label) |
| Monthly | last day of month 20:00 UTC | See 4.1 |
| Yearly | 31.12., runs right after the December monthly (~20:00 UTC+) | The monthly digests of the year |

A daily digest is labelled with its run date and covers the 24 hours before the run. News after the run on the last day of a period therefore falls into the next period. This is accepted.

### 4.1 Monthly input rule
Monthly input = every **existing weekly digest whose Monday-to-Sunday week lies fully inside the month**, plus the **daily digests of all remaining days** of the month (edge days of partial weeks, and days of any week whose weekly digest is missing). Missing dailies are skipped.

### 4.2 Yearly input rule
All existing monthly digests of the year. A missing month is skipped.

## 5. Digest content

Common rules: English; clustered by topic with `##` sections and optional `###` sub-sections when a topic has enough items (for example Tech → AI); concise and high level, no per-article detail. Source `topic` tags (see 3) steer the clustering. Weekly, monthly and yearly digests keep the same top-level sections.

Suggested top-level categories, one word each (adjustable in `settings.yaml`; the LLM may add sub-sections but keeps top-level names stable):
Tech (AI, Cloud & Dev), SAP, Business, Science, World, Other.

| Level | Length limit | Sources section |
|---|---|---|
| Daily | at most 10 bullets in total | Yes, source names only, at the bottom, plus health warnings |
| Weekly | at most 300 words | No |
| Monthly | about 1 page (about 500 words) | No |
| Yearly | about 5 pages (about 2500 words), focus on bigger trends | No |

The same story reported by several sources is merged into one bullet.

## 6. Output

### Files
```
digests/daily/YYYY/YYYY-MM-DD.md
digests/weekly/YYYY/YYYY-Www.md       (ISO week number, Monday start)
digests/monthly/YYYY/YYYY-MM.md
digests/yearly/YYYY.md
```
Each file starts with YAML front matter: `level`, `period`, `generated_at`, `model`, `inputs` (what it was built from).

### Release
- One GitHub release per digest, created after the digest is committed and pushed; the tag points at that commit.
- Tag format: `<level>-<period>`, so `daily-2026-10-15`, `weekly-2026-W42`, `monthly-2026-10`, `yearly-2026`.
- Title format: `[Digest] Daily 2026-10-15` and so on.
- Body: the digest Markdown without front matter, rendered by GitHub.
- The owner watches the repo for releases (Watch → Custom → Releases), and GitHub emails each release to the owner's notification address (`marcus_szymanski@epam.com`). The first notifications should be checked for spam or external-sender filtering on the EPAM side.

## 7. Reliability and alerts

- Runs are **idempotent**: if the output file for a period already exists, the run exits (unless forced), so duplicate cron triggers and retries are harmless.
- Manual runs and backfills via `workflow_dispatch` with level and date inputs.
- On failure, the run exits non-zero, which triggers GitHub's workflow-failure email, and opens an issue labelled `alert`, assigned to the owner so that GitHub notifies them. A repeated failure for the same digest comments on the open alert issue. If the issue cannot be created, the failure email alone is the alert.
- Failure of one source never fails the run; failure of the LLM call or an empty input set skips the digest and raises an alert.
- If the commit or the release fails after the digest is written, the file stays and an alert is raised. A rerun finds the digest without a release and creates the release.

## 8. Non-goals (first version)

Authenticated or paywalled sources, relevance ranking by interest profile, translation, multiple recipients, sending email from the aggregator, a web UI, article links in digests.

## 9. Open items

1. Rough monthly LLM cost estimate (to be measured after the first week of real runs).
2. Behavior when the LLM exceeds the length limit twice (draft: truncate for daily, alert for the other levels).
