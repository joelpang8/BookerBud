"""Small JSON GET client for free context sources: retries, polite
User-Agent, and every response logged to data/raw like the odds provider."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from bookerbud.raw_log import RawLogger

USER_AGENT = "BookerBud/0.1 (personal, non-commercial sports research)"


class ContextSourceError(RuntimeError):
    pass


class JsonClient:
    def __init__(
        self,
        source: str,
        raw_logger: RawLogger,
        *,
        client: httpx.Client | None = None,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.source = source
        self.raw_logger = raw_logger
        self._client = client or httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT})
        self.max_retries = max_retries
        self._sleep = sleep

    def get(self, url: str, params: dict[str, Any], label: str) -> Any:
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._client.get(url, params=params)
            except httpx.TransportError as exc:
                if attempt == self.max_retries:
                    raise ContextSourceError(f"{self.source}: network error: {exc}") from exc
                self._sleep(2**attempt)
                continue
            try:
                body = resp.json()
            except ValueError:
                body = resp.text
            self.raw_logger.write(self.source, label, url=url, params=params, status=resp.status_code, body=body)
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == self.max_retries:
                    raise ContextSourceError(f"{self.source}: HTTP {resp.status_code}")
                self._sleep(2 ** (attempt + 1))
                continue
            if resp.status_code >= 400:
                raise ContextSourceError(f"{self.source}: HTTP {resp.status_code} for {url}")
            return body
        raise ContextSourceError(f"{self.source}: retries exhausted")  # pragma: no cover

    def close(self) -> None:
        self._client.close()
