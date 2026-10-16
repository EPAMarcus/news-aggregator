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

## Configuration

Two files in [config/](config/) control what is collected and how it is summarized:

- [config/settings.yaml](config/settings.yaml): top-level categories, model per level, length limits, `stale_after_days`.
- [config/sources.yaml](config/sources.yaml): the source list (`rss`, `web` or `newsletter`), each with an optional `topic` and `enabled` flag. The field reference is in the file's header comment.

The recipient of the digest and alert emails is not in the repo. Set it in the environment as `RECIPIENT_EMAIL` (locally `export RECIPIENT_EMAIL=you@example.com`; in GitHub Actions as a repository secret). Loading the config fails when it is missing or not an email address.

The config directory is the project's `config/`, whatever the working directory is. To use another one, set `AGGREGATOR_CONFIG_DIR`.

A source `topic` must start with one of the configured categories (`Tech` or `Tech/AI`); the part after `/` is free-form. Loading fails with a message listing every problem for unknown categories, duplicate source IDs, missing type-specific fields and unknown keys. The sample sources are placeholders; the example newsletter and web entries are disabled.

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
