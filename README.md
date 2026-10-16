# news-aggregator

Contains a configurable news aggreagtor that collects daily, weekly, monthly and yearly news.

See [docs/SPEC.md](docs/SPEC.md) for requirements and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design.

> **Status:** early development. Only the project skeleton exists so far; the digest pipeline is tracked in the [GitHub issues](https://github.com/EPAMarcus/news-aggregator/issues).

## Setup

Requirements: [uv](https://docs.astral.sh/uv/) (it installs the pinned Python 3.14 automatically).

```sh
git clone git@github.com:EPAMarcus/news-aggregator.git
cd news-aggregator
uv sync
```

`uv sync` creates `.venv` and installs the project with its dev dependencies (pytest, ruff).

## Usage

```sh
uv run aggregator --help          # console script
uv run python -m aggregator --help  # same, as a module
uv run aggregator --version
```

The `run --level <daily|weekly|monthly|yearly>` command is not implemented yet.

## Development

```sh
uv run pytest                 # tests
uv run ruff check .           # lint
uv run ruff format .          # format
```

## AI code review

The **Claude Code Review** workflow ([.github/workflows/claude-review.yml](.github/workflows/claude-review.yml)) runs `/code-review` on a pull request and posts the findings as inline PR comments. It runs only when triggered manually.

One-time setup:

1. Install the [Claude GitHub App](https://github.com/apps/claude) on this repository.
2. Add the repository secret `ANTHROPIC_API_KEY` (Settings → Secrets and variables → Actions), or from the terminal: `gh secret set ANTHROPIC_API_KEY`.

Run a review once a PR is ready:

```sh
gh workflow run claude-review.yml -f pr_number=<PR number> -f effort=high
```

or use **Actions → Claude Code Review → Run workflow**. Effort is one of `low`, `medium`, `high`, `xhigh`, `max`. Treat the findings as advisory; a human decides what to act on.

## Project layout

```
src/aggregator/   # application code
tests/            # tests
docs/             # spec and architecture
```

`config/`, `prompts/`, `digests/` and `state/` will be added as the pipeline is built, as described in the architecture doc.
