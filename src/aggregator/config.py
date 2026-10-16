"""Load and validate `config/settings.yaml` and `config/sources.yaml`."""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

# src/aggregator/config.py -> project root, so the default does not depend on the working directory
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_DIR = PROJECT_ROOT / "config"
CONFIG_DIR_ENV_VAR = "AGGREGATOR_CONFIG_DIR"
SETTINGS_FILE = "settings.yaml"
SOURCES_FILE = "sources.yaml"
RECIPIENT_ENV_VAR = "RECIPIENT_EMAIL"


class ConfigError(Exception):
    """The configuration is missing, unreadable or invalid. The message lists every problem."""


class _Model(BaseModel):
    # Reject unknown keys so that a typo in a YAML file is an error, not a silently ignored value.
    model_config = ConfigDict(extra="forbid", frozen=True)


def _non_blank(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("must not be empty")
    return value


def _http_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("must be an http(s) URL")
    return value.strip()


def _email(value: str) -> str:
    value = value.strip()
    local, _, domain = value.partition("@")
    if not local or "." not in domain or " " in value:
        raise ValueError("must be an email address")
    return value


# --- settings ---------------------------------------------------------------------------------


class Models(_Model):
    """Claude model ID per digest level."""

    daily: str = "claude-sonnet-5-5"
    weekly: str = "claude-sonnet-5-5"
    monthly: str = "claude-sonnet-5-5"
    yearly: str = "claude-sonnet-5-5"

    _check = field_validator("*")(_non_blank)


class Limits(_Model):
    """Length limits per level: bullets for daily, words for the others."""

    daily_bullets: int = Field(10, gt=0)
    weekly_words: int = Field(300, gt=0)
    monthly_words: int = Field(500, gt=0)
    yearly_words: int = Field(2500, gt=0)


class Settings(_Model):
    timezone: str
    categories: list[str] = Field(min_length=1)
    models: Models = Models()
    limits: Limits = Limits()
    stale_after_days: int = Field(30, gt=0)

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError, ValueError:
            raise ValueError(f"unknown IANA timezone {value!r}, e.g. 'Europe/Berlin'") from None
        return value

    @field_validator("categories")
    @classmethod
    def _valid_categories(cls, values: list[str]) -> list[str]:
        values = [_non_blank(v) for v in values]
        for value in values:
            if "/" in value:
                # '/' separates a topic's category from its sub-category
                raise ValueError(f"category {value!r} must not contain '/'")
        duplicates = sorted(v for v, n in Counter(values).items() if n > 1)
        if duplicates:
            raise ValueError(f"duplicate categories: {', '.join(duplicates)}")
        return values


# --- sources ----------------------------------------------------------------------------------


class _SourceBase(_Model):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str
    topic: str | None = None
    enabled: bool = True

    _check_name = field_validator("name")(_non_blank)

    @field_validator("topic")
    @classmethod
    def _valid_topic(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = [p.strip() for p in value.split("/")]
        if not all(parts):
            raise ValueError(
                f"topic {value!r} has an empty part, expected 'Category' or 'Category/Sub'"
            )
        return "/".join(parts)

    @property
    def category(self) -> str | None:
        """Top-level part of the topic tag. Sub-categories are free-form and not validated."""
        return self.topic.split("/", 1)[0] if self.topic else None


class RssSource(_SourceBase):
    type: Literal["rss"]
    url: str

    _check_url = field_validator("url")(_http_url)


class WebSource(_SourceBase):
    type: Literal["web"]
    url: str
    selector: str | None = None  # CSS selector for article links on the index page

    _check_url = field_validator("url")(_http_url)


class NewsletterSource(_SourceBase):
    type: Literal["newsletter"]
    sender: str = Field(alias="from")  # `from` is a Python keyword

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @field_validator("sender")
    @classmethod
    def _valid_sender(cls, value: str) -> str:
        value = value.strip()
        if "@" not in value or " " in value:
            raise ValueError("must be a sender email address")
        return value.lower()


Source = Annotated[RssSource | WebSource | NewsletterSource, Field(discriminator="type")]


class _SourcesFile(_Model):
    sources: list[Source]


# --- loading ----------------------------------------------------------------------------------


class Config(_Model):
    settings: Settings
    sources: list[Source]
    recipient: str  # from the RECIPIENT_EMAIL environment variable, never from the repo

    @property
    def enabled_sources(self) -> list[Source]:
        return [s for s in self.sources if s.enabled]


def _read_yaml(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(
            f"{path}: file not found (the config directory can be set with {CONFIG_DIR_ENV_VAR})"
        ) from None
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read file ({exc})") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML\n{exc}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    return data


def _format_errors(path: Path, exc: ValidationError, data: dict[str, Any]) -> list[str]:
    problems = []
    for error in exc.errors():
        loc = list(error["loc"])
        # For the source union, the first location element after the index is the type tag
        # (e.g. sources.2.rss.url); drop it, and name the source by id for readability.
        if loc[:1] == ["sources"] and len(loc) > 1 and isinstance(loc[1], int):
            entry = data["sources"][loc[1]]
            label = (
                f"source {entry['id']!r}"
                if isinstance(entry, dict) and "id" in entry
                else (f"source #{loc[1] + 1}")
            )
            rest = [p for p in loc[2:] if p not in ("rss", "web", "newsletter")]
            where = ".".join([label, *map(str, rest)]) if rest else label
        else:
            where = ".".join(map(str, loc)) or "(root)"
        problems.append(f"{path}: {where}: {error['msg']}")
    return problems


def _validate[T: BaseModel](model: type[T], path: Path, data: dict[str, Any]) -> T:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise ConfigError("\n".join(_format_errors(path, exc, data))) from None


def _read_recipient(env: Mapping[str, str]) -> str:
    value = env.get(RECIPIENT_ENV_VAR, "").strip()
    if not value:
        raise ConfigError(
            f"{RECIPIENT_ENV_VAR}: environment variable is not set; it holds the address that "
            "receives digests and alerts (a GitHub Actions secret, kept out of the repo)"
        )
    try:
        return _email(value)
    except ValueError as exc:
        raise ConfigError(f"{RECIPIENT_ENV_VAR}: {exc}") from None


def load_config(
    config_dir: Path | str | None = None, env: Mapping[str, str] | None = None
) -> Config:
    """Load both config files and the recipient, and validate them, including cross-file rules.

    `config_dir` defaults to the `AGGREGATOR_CONFIG_DIR` variable, then to the `config/` directory
    of the project, never to the current working directory. The recipient comes from the
    `RECIPIENT_EMAIL` variable. Both variables are read from `env` (default: the process
    environment). Raises `ConfigError` with all problems found in the cross-file checks, or with
    the problems of the first file or variable that fails validation.
    """
    env = os.environ if env is None else env
    config_dir = Path(config_dir or env.get(CONFIG_DIR_ENV_VAR) or DEFAULT_CONFIG_DIR)
    settings_path = config_dir / SETTINGS_FILE
    sources_path = config_dir / SOURCES_FILE

    recipient = _read_recipient(env)
    settings = _validate(Settings, settings_path, _read_yaml(settings_path))
    sources_data = _read_yaml(sources_path)
    sources = _validate(_SourcesFile, sources_path, sources_data).sources

    problems = []
    for source_id, count in Counter(s.id for s in sources).items():
        if count > 1:
            problems.append(f"{sources_path}: duplicate source id {source_id!r} ({count} times)")
    for source in sources:
        if source.category is not None and source.category not in settings.categories:
            problems.append(
                f"{sources_path}: source {source.id!r}: topic {source.topic!r} has unknown "
                f"top-level category {source.category!r}; configured categories "
                f"({settings_path.name}): {', '.join(settings.categories)}"
            )
    if problems:
        raise ConfigError("\n".join(problems))

    return Config(settings=settings, sources=sources, recipient=recipient)
