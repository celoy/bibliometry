"""OpenAlex HTTP layer: polite pool, API key, budget tracking, backoff, cursor paging.

OpenAlex meters usage per day. Anonymous callers share a small budget per IP address
(US$0.10/day at the time of writing, i.e. ~1000 list requests); a free API key raises
it. Costs differ by request kind: a filtered list is cheap, a full-text ``search`` costs
ten times more. The pipeline is written around that: filters are OR-batched 50 values
at a time, and name searches are the last resort.

Everything here is read-only.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

import httpx

BASE_URL = "https://api.openalex.org"
MAX_PER_PAGE = 200
#: OpenAlex accepts at most 50 values in a single OR (``|``) filter clause.
MAX_OR_VALUES = 50
#: Stay under the documented ~10 req/s ceiling with a margin.
MIN_INTERVAL_S = 0.12
DEFAULT_TIMEOUT = 60.0
MAX_ATTEMPTS = 5


def reconstruct_abstract(inverted_index: dict[str, list[int]] | None) -> str | None:
    """Rebuild readable text from OpenAlex's inverted-index abstract encoding."""
    if not inverted_index:
        return None
    positions = [(i, word) for word, indices in inverted_index.items() for i in indices]
    if not positions:
        return None
    positions.sort()
    return " ".join(word for _, word in positions)


def short_id(url: str | None) -> str | None:
    """``"https://openalex.org/W2755950973"`` -> ``"W2755950973"``."""
    if not url:
        return None
    return url.rstrip("/").split("/")[-1]


def batched(values: Iterable[str], size: int = MAX_OR_VALUES) -> Iterator[list[str]]:
    """Yield consecutive chunks, defaulting to OpenAlex's 50-value OR-filter limit."""
    chunk: list[str] = []
    for value in values:
        chunk.append(value)
        if len(chunk) == size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def date_window(from_date: str, to_date: str) -> list[str]:
    """Filter clauses restricting works to ``[from_date, to_date]`` (inclusive)."""
    return [f"from_publication_date:{from_date}", f"to_publication_date:{to_date}"]


def institution_works_filter(institution_id: str, from_date: str, to_date: str) -> str:
    """Works with at least one authorship affiliated to the institution or a child of it.

    ``lineage`` rather than ``id`` so that an institution's sub-units, when OpenAlex
    records them as children, are counted too.
    """
    return ",".join(
        [f"authorships.institutions.lineage:{institution_id}", *date_window(from_date, to_date)]
    )


def author_works_filter(author_ids: list[str], from_date: str, to_date: str) -> str:
    """Works by any of ``author_ids`` (at most 50) within a date window."""
    return ",".join([f"author.id:{'|'.join(author_ids)}", *date_window(from_date, to_date)])


class BudgetExhaustedError(RuntimeError):
    """OpenAlex refused the request because the daily budget is spent."""


class RateLimitedError(RuntimeError):
    """OpenAlex kept returning 429 or 5xx after every retry."""


@dataclass
class Budget:
    """Remaining daily budget as last reported by OpenAlex's ``X-RateLimit-*`` headers."""

    remaining_usd: float | None = None
    limit_usd: float | None = None
    reset_seconds: int | None = None

    def update(self, headers: httpx.Headers) -> None:
        def number(key: str) -> float | None:
            try:
                return float(headers[key])
            except (KeyError, ValueError):
                return None

        self.remaining_usd = number("x-ratelimit-remaining-usd")
        self.limit_usd = number("x-ratelimit-limit-usd")
        reset = number("x-ratelimit-reset")
        self.reset_seconds = int(reset) if reset is not None else None

    def __str__(self) -> str:
        if self.remaining_usd is None:
            return "budget unknown"
        hours = (self.reset_seconds or 0) / 3600
        return (
            f"US${self.remaining_usd:.4f} of US${self.limit_usd or 0:.2f} left today "
            f"(resets in {hours:.1f} h)"
        )


class OpenAlexClient:
    """Thin synchronous OpenAlex client with retry, backoff, pacing and budget tracking.

    Args:
        mailto: contact address for the polite pool. Falls back to ``OPENALEX_MAILTO``.
        api_key: free OpenAlex API key. Falls back to ``OPENALEX_API_KEY``.
        timeout: per-request timeout in seconds.
        min_interval: minimum seconds between requests.
        transport: optional httpx transport (tests pass a ``MockTransport``).
    """

    def __init__(
        self,
        mailto: str | None = None,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        min_interval: float = MIN_INTERVAL_S,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.mailto = (mailto or os.environ.get("OPENALEX_MAILTO", "")).strip()
        self.api_key = (api_key or os.environ.get("OPENALEX_API_KEY", "")).strip()
        self.min_interval = min_interval
        #: First retry delay in seconds, doubled at each attempt.
        self.backoff = 2.0
        user_agent = f"bibliometry/0.1 (mailto:{self.mailto})" if self.mailto else "bibliometry/0.1"
        self._client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            follow_redirects=True,
            transport=transport,
        )
        self._last_request = 0.0
        self.n_requests = 0
        self.budget = Budget()

    def __enter__(self) -> OpenAlexClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _pace(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_request = time.monotonic()

    def _budget_error(self, response: httpx.Response) -> BudgetExhaustedError:
        try:
            message = str(response.json().get("message") or "")
        except Exception:  # noqa: BLE001 - the body is only used for the message
            message = ""
        hint = (
            ""
            if self.api_key
            else " No API key is set: get a free one at https://openalex.org/settings/api "
            "and enter it in the setup notebook (or set OPENALEX_API_KEY)."
        )
        return BudgetExhaustedError(
            f"OpenAlex daily budget exhausted ({self.budget}). {message}{hint} "
            "Everything harvested so far is saved; re-run the same step later to resume."
        )

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET ``path``, retrying transient failures.

        Retries 429 (too fast), 5xx and timeouts with exponential backoff. A 429 that
        reports a spent daily budget fails at once: retrying cannot help until it resets.
        Other 4xx fail immediately, since retrying a malformed filter never helps.
        """
        query = dict(params or {})
        if self.mailto:
            query["mailto"] = self.mailto
        if self.api_key:
            query["api_key"] = self.api_key

        last_error: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            self._pace()
            try:
                response = self._client.get(f"{BASE_URL}{path}", params=query)
                self.n_requests += 1
                self.budget.update(response.headers)
                if response.status_code == 429:
                    if self.budget.remaining_usd is not None and self.budget.remaining_usd <= 0:
                        raise self._budget_error(response)
                    last_error = RateLimitedError("OpenAlex returned 429")
                elif response.status_code >= 500:
                    last_error = httpx.HTTPStatusError(
                        f"server error {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                else:
                    response.raise_for_status()
                    return response.json()
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc

            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(self.backoff * (2**attempt))

        raise RateLimitedError(f"GET {path} failed after {MAX_ATTEMPTS} attempts") from last_error

    def count(self, path: str, filter_string: str) -> int:
        """Return how many records match ``filter_string`` without fetching them."""
        data = self.get(path, {"filter": filter_string, "per-page": 1, "select": "id"})
        return int(data["meta"]["count"])

    def group_by(self, path: str, filter_string: str, key: str) -> list[dict[str, Any]]:
        """Return OpenAlex ``group_by`` buckets."""
        data = self.get(path, {"filter": filter_string, "group_by": key})
        return list(data.get("group_by", []))

    def iter_records(
        self,
        path: str,
        filter_string: str,
        select: str | None = None,
        per_page: int = MAX_PER_PAGE,
    ) -> Iterator[dict[str, Any]]:
        """Yield every record matching ``filter_string``, paging with a cursor.

        ``select=None`` returns every field OpenAlex has.
        """
        for page in self.iter_pages(path, filter_string, select=select, per_page=per_page):
            yield from page

    def iter_pages(
        self,
        path: str,
        filter_string: str,
        select: str | None = None,
        per_page: int = MAX_PER_PAGE,
    ) -> Iterator[list[dict[str, Any]]]:
        """Yield one list of records per page, so callers can save as they go.

        Cursor paging is the only way past OpenAlex's 10 000-record offset ceiling.
        """
        cursor: str | None = "*"
        while cursor:
            params: dict[str, Any] = {
                "filter": filter_string,
                "per-page": per_page,
                "cursor": cursor,
            }
            if select:
                params["select"] = select
            data = self.get(path, params)
            results = data.get("results", [])
            if not results:
                return
            yield results
            cursor = data.get("meta", {}).get("next_cursor")

    def search(
        self,
        path: str,
        text: str,
        filter_string: str | None = None,
        select: str | None = None,
        per_page: int = 10,
    ) -> list[dict[str, Any]]:
        """Full-text search (the expensive kind of request): first page only."""
        params: dict[str, Any] = {"search": text, "per-page": per_page}
        if filter_string:
            params["filter"] = filter_string
        if select:
            params["select"] = select
        return list(self.get(path, params).get("results", []))
