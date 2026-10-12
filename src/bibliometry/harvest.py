"""Harvest full OpenAlex work records into the raw stores.

Two corpora, kept in separate stores so that each keeps its meaning:

* ``institution_works`` — every work with at least one authorship affiliated to the
  institution in the period;
* ``people_works`` — every work in the period by the accepted author profiles of the
  roster, whatever their affiliation on it. Whether the person signed *as* a member of
  the institution is decided later, per authorship, in :mod:`bibliometry.tables`.

Both are resumable. Works are saved page by page, and completed queries are recorded
in a manifest so that re-running a finished step costs nothing. Adding people to the
roster later fetches only the new profiles.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .openalex import (
    MAX_PER_PAGE,
    OpenAlexClient,
    author_works_filter,
    batched,
    institution_works_filter,
)
from .store import Manifest, WorksStore


@dataclass
class HarvestStats:
    """Outcome of one harvest call."""

    expected: int = 0
    seen: int = 0
    new: int = 0
    requests: int = 0
    skipped: bool = False

    def __str__(self) -> str:
        if self.skipped:
            return "already complete — nothing fetched (use refresh=True to re-fetch)"
        return (
            f"{self.seen} works seen ({self.expected} expected), {self.new} new, "
            f"{self.requests} requests"
        )


def harvest_institution(
    client: OpenAlexClient,
    institution_id: str,
    from_date: str,
    to_date: str,
    store: WorksStore,
    manifest: Manifest,
    refresh: bool = False,
    progress: Callable[[str], None] | None = None,
    author_name: str = "",
) -> HarvestStats:
    """Fetch every work of the institution in ``[from_date, to_date]`` into ``store``.

    With ``author_name``, only works with an author of that name (single-person mode).
    """
    say = progress or (lambda _msg: None)
    key = f"institution:{institution_id}:{from_date}:{to_date}" + (
        f":{author_name}" if author_name else ""
    )
    if manifest.is_done(key) and not refresh:
        return HarvestStats(skipped=True)

    stats = HarvestStats()
    before = client.n_requests
    filter_string = institution_works_filter(institution_id, from_date, to_date, author_name)
    stats.expected = client.count("/works", filter_string)
    n_pages = -(-stats.expected // MAX_PER_PAGE)
    say(f"{stats.expected} works to fetch ({n_pages} pages)…")
    for index, page in enumerate(client.iter_pages("/works", filter_string), start=1):
        stats.seen += len(page)
        stats.new += store.add_many(page)
        if index % 5 == 0 or index == n_pages:
            say(f"page {index}/{n_pages}: {stats.seen} works ({client.budget})")
    stats.requests = client.n_requests - before
    manifest.mark_done(key, count=stats.expected, seen=stats.seen)
    return stats


def harvest_people(
    client: OpenAlexClient,
    author_ids: list[str],
    from_date: str,
    to_date: str,
    store: WorksStore,
    manifest: Manifest,
    refresh: bool = False,
    progress: Callable[[str], None] | None = None,
) -> HarvestStats:
    """Fetch every work by ``author_ids`` in the window, 50 authors per query."""
    say = progress or (lambda _msg: None)

    def key(author_id: str) -> str:
        return f"author:{author_id}:{from_date}:{to_date}"

    todo = [a for a in author_ids if refresh or not manifest.is_done(key(a))]
    if not todo:
        return HarvestStats(skipped=True)

    stats = HarvestStats()
    before = client.n_requests
    batches = list(batched(todo))
    say(f"{len(todo)} author profiles to harvest in {len(batches)} batch(es)…")
    for index, chunk in enumerate(batches, start=1):
        filter_string = author_works_filter(chunk, from_date, to_date)
        expected = client.count("/works", filter_string)
        stats.expected += expected
        seen = 0
        for page in client.iter_pages("/works", filter_string):
            seen += len(page)
            stats.new += store.add_many(page)
        stats.seen += seen
        for author_id in chunk:
            manifest.mark_done(key(author_id))
        say(f"batch {index}/{len(batches)}: {seen} works ({client.budget})")
    stats.requests = client.n_requests - before
    return stats
