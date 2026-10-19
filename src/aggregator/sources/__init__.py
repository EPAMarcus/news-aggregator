"""Fetchers for the source types in `sources.yaml` (spec section 3, architecture 3.2).

Every fetcher turns one configured source into a `FetchResult` holding `Item`s. `fetch_all` runs
them with isolated error handling, so one failing source never fails the run.
"""

from aggregator.sources.base import FetchResult, FetchStatus, Item, fetch_all, fetch_source

__all__ = ["FetchResult", "FetchStatus", "Item", "fetch_all", "fetch_source"]
