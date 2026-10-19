"""HTTP download with a total time limit and a size limit."""

from __future__ import annotations

import time
import urllib.request
from collections.abc import Callable

from aggregator import __version__

USER_AGENT = f"news-aggregator/{__version__} (personal news digest; polite, low volume)"
TIMEOUT_SECONDS = 20.0
MAX_BYTES = 10 * 1024 * 1024
_CHUNK = 64 * 1024

# (url, timeout) -> response body; tests replace it to serve fixtures
Get = Callable[[str, float], bytes]


class FetchTimeout(TimeoutError):
    pass


class TooLarge(ValueError):
    pass


def get(url: str, timeout: float = TIMEOUT_SECONDS, max_bytes: int = MAX_BYTES) -> bytes:
    """GET `url` and return the body. Raises on HTTP errors (status >= 400), on a body larger
    than `max_bytes`, and when the whole download takes longer than `timeout` seconds.

    `timeout` is also the socket timeout, so a stalled connection fails after at most `timeout`
    seconds, and a slowly dripping one after at most about twice that.
    """
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5",
        },
    )
    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    size = 0
    with urllib.request.urlopen(request, timeout=timeout) as response:
        while chunk := response.read(_CHUNK):
            size += len(chunk)
            if size > max_bytes:
                raise TooLarge(f"response larger than {max_bytes} bytes")
            if time.monotonic() > deadline:
                raise FetchTimeout(f"download took longer than {timeout:g}s")
            chunks.append(chunk)
    return b"".join(chunks)
