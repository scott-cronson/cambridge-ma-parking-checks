"""Polite HTTP: one session per run, fixed timeout, one retry, minimum spacing between requests."""

from __future__ import annotations

import time
from collections.abc import Callable

import requests


class DataSourceError(Exception):
    """A data source could not be reached or returned something unusable."""


class PoliteSession:
    def __init__(
        self,
        user_agent: str,
        *,
        timeout: float = 20.0,
        min_interval: float = 1.0,
        retries: int = 1,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self.timeout = timeout
        self.min_interval = min_interval
        self.retries = retries
        self.sleep = sleep
        self.monotonic = monotonic
        self._last = float("-inf")
        self.request_count = 0

    def _wait(self) -> None:
        gap = self.monotonic() - self._last
        if gap < self.min_interval:
            self.sleep(self.min_interval - gap)

    def request(self, method: str, url: str, **kw) -> requests.Response:
        """Returns the response for any HTTP status; retries once on connection errors and 5xx."""
        last_exc: Exception | None = None
        resp: requests.Response | None = None
        for attempt in range(self.retries + 1):
            self._wait()
            self._last = self.monotonic()
            self.request_count += 1
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kw)
            except requests.RequestException as e:
                last_exc = e
                continue
            if resp.status_code >= 500 and attempt < self.retries:
                continue
            return resp
        if resp is not None:
            return resp
        raise DataSourceError(f"{method} {url}: {type(last_exc).__name__}: {last_exc}")

    def get(self, url: str, **kw) -> requests.Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw) -> requests.Response:
        return self.request("POST", url, **kw)
