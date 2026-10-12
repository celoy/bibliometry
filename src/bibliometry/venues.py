"""Publication venues: classification, impact metrics and a top-10% flag.

Each work's venue is resolved by :func:`bibliometry.tables.venue_of`: the primary
location, unless that is a repository deposit and the work also appeared in a journal
or conference. For every venue, OpenAlex gives:

* ``h_index`` — the venue's h-index over all its works;
* ``i10_index`` — how many of its works have at least 10 citations;
* ``2yr_mean_citedness`` — mean citations in a year of the works it published in the
  two previous years (the OpenAlex analogue of the Journal Impact Factor).

All three grow with a venue's size and differ between fields: a top-10% flag computed
across all fields mostly selects large physics and medicine journals. That is why the
venue's main field is reported next to the flag.

Two top-10% flags, both on journals only (conferences, book series and repositories
are classified but not ranked):

* ``top10_<metric>`` — among the *journals used by list 1*: a relative position in this
  institution's publication landscape;
* ``world_top10_<metric>`` — among *all active journals in the world with the same main
  subfield* (≥ 100 works, still publishing). ``world_pct_<metric>`` is the share of
  those journals that score higher (0.03 = better than 97% of its subfield).

A journal's main subfield is the subfield holding most of its works, from the counts of
its OpenAlex topics. For broad journals (Scientific Reports, PLOS ONE, Nature…) this is
an arbitrary choice; they are marked ``broad_scope`` (main domain < 60% of their works).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from typing import Any

import pandas as pd

from .openalex import OpenAlexClient, batched, short_id
from .store import WorksStore

#: In order of prominence: 2-year mean citedness is the headline metric; h-index and
#: i10-index are reported as secondary, size-dependent indicators.
METRICS = ("2yr_mean_citedness", "h_index", "i10_index")
MAIN_METRIC = METRICS[0]
#: Smallest world reference set for which a subfield top-10% is computed.
MIN_WORLD_N = 20
#: World reference: active journals with at least this many works.
WORLD_MIN_WORKS = 100
#: A journal whose main domain holds less than this share of its works is ``broad_scope``.
BROAD_SCOPE_DOMAIN_SHARE = 0.6
WORLD_FIELDS = "id,display_name,works_count,summary_stats,topics,last_publication_year"
RANKED_TYPES = ("journal",)
TOP_SHARE = 0.10


def fetch_sources(
    client: OpenAlexClient | None,
    source_ids: Iterable[str],
    store: WorksStore,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """Full source records for ``source_ids``, 50 per request, cached in ``store``."""
    wanted = {s for s in source_ids if s}
    missing = sorted(wanted - store.ids)
    if client is not None and missing:
        if progress:
            progress(f"Fetching {len(missing)} venue records…")
        for chunk in batched(missing):
            for page in client.iter_pages("/sources", f"ids.openalex:{'|'.join(chunk)}"):
                store.add_many(page)
    return [r for r in store.iter_records() if short_id(r.get("id")) in wanted]


def main_subfield(source: dict[str, Any]) -> dict[str, Any]:
    """The subfield holding most of a source's works, with its field and domain.

    OpenAlex lists a source's topics with work counts, not in order of size, so the
    counts are summed per subfield. Also returns the shares used for ``broad_scope``.
    """
    totals: dict[str, float] = defaultdict(float)
    domains: dict[str, float] = defaultdict(float)
    labels: dict[str, tuple[str, str, str]] = {}
    for topic in source.get("topics") or []:
        subfield = topic.get("subfield") or {}
        key = short_id(subfield.get("id")) or ""
        if not key:
            continue
        count = float(topic.get("count") or 0)
        totals[key] += count
        domain = (topic.get("domain") or {}).get("display_name") or ""
        domains[domain] += count
        labels[key] = (
            subfield.get("display_name") or "",
            (topic.get("field") or {}).get("display_name") or "",
            domain,
        )
    total = sum(totals.values())
    if not total:
        return {
            "subfield_id": None,
            "main_subfield": None,
            "main_field": None,
            "domain": None,
            "subfield_share": None,
            "domain_share": None,
        }
    best = max(totals, key=totals.get)
    subfield, field, domain = labels[best]
    return {
        "subfield_id": best,
        "main_subfield": subfield,
        "main_field": field,
        "domain": domain,
        "subfield_share": totals[best] / total,
        "domain_share": max(domains.values()) / total,
    }


def venue_table(sources: list[dict[str, Any]], works: pd.DataFrame) -> pd.DataFrame:
    """One row per venue used by the works, with metrics and publication counts."""
    rows = []
    for source in sources:
        stats = source.get("summary_stats") or {}
        main = main_subfield(source)
        rows.append(
            {
                "venue_id": short_id(source.get("id")),
                "venue_name": source.get("display_name"),
                "venue_type": source.get("type"),
                "issn_l": source.get("issn_l"),
                "publisher": source.get("host_organization_name"),
                "country": source.get("country_code"),
                **main,
                "broad_scope": main["domain_share"] is not None
                and main["domain_share"] < BROAD_SCOPE_DOMAIN_SHARE,
                "is_oa": source.get("is_oa"),
                "is_in_doaj": source.get("is_in_doaj"),
                "is_core": source.get("is_core"),
                "works_count": source.get("works_count"),
                "cited_by_count": source.get("cited_by_count"),
                **{metric: stats.get(metric) for metric in METRICS},
                "apc_usd": source.get("apc_usd"),
                "listed_in": "; ".join(source.get("listed_in") or []),
                "homepage_url": source.get("homepage_url"),
            }
        )
    venues = pd.DataFrame(rows)
    if venues.empty:
        return venues
    for name, column in (("works_list1", "in_list_institution"), ("works_list2", "in_list_roster")):
        counts = works[works[column]].venue_id.value_counts()
        venues[name] = venues.venue_id.map(counts).fillna(0).astype(int)
    return venues.sort_values("works_list1", ascending=False).reset_index(drop=True)


def flag_top(
    venues: pd.DataFrame,
    metrics: Iterable[str] = METRICS,
    share: float = TOP_SHARE,
    types: Iterable[str] = RANKED_TYPES,
    reference: str = "works_list1",
) -> pd.DataFrame:
    """Add ``rank_<metric>``, ``top10_<metric>`` and ``top10_any``.

    The reference set is the venues of ``types`` with at least one work in
    ``reference`` (list 1 by default). Rank 1 is the best; ties share the best rank, so
    the top set can be slightly larger than ``share`` of the reference set.
    """
    venues = venues.copy()
    ranked = venues.venue_type.isin(set(types)) & (venues[reference] > 0)
    n_ranked = int(ranked.sum())
    venues["ranked"] = ranked
    flags = []
    for metric in metrics:
        values = venues[metric].where(ranked)
        rank = values.rank(ascending=False, method="min")
        venues[f"rank_{metric}"] = rank.astype("Int64")
        flag = f"top10_{metric}"
        venues[flag] = (rank <= max(1, round(share * n_ranked))).fillna(False) & ranked
        flags.append(flag)
    venues["top10_any"] = venues[flags].any(axis=1)
    venues.attrs["n_ranked"] = n_ranked
    return venues


def thresholds(venues: pd.DataFrame, metrics: Iterable[str] = METRICS) -> pd.Series:
    """Lowest value of each metric that is still in the top 10%."""
    return pd.Series(
        {m: venues.loc[venues[f"top10_{m}"], m].min() for m in metrics}, name="top10_threshold"
    )


# ---------------------------------------------------------------------------
# World reference: all active journals, by main subfield
# ---------------------------------------------------------------------------


def world_filter(active_since: int, min_works: int = WORLD_MIN_WORKS) -> str:
    return f"type:journal,works_count:>{min_works - 1},last_publication_year:>{active_since - 1}"


def fetch_world_journals(
    client: OpenAlexClient,
    store: WorksStore,
    manifest: Any,
    active_since: int,
    progress: Callable[[str], None] | None = None,
) -> pd.DataFrame:
    """Every active journal in OpenAlex with its metrics and main subfield.

    About 64 000 journals, ~320 requests (a few minutes, ~US$0.03) the first time;
    afterwards read from ``store`` (``data/raw/world_journals.jsonl.gz``). Only compact
    rows are stored, not full records.
    """
    key = f"world_journals:{active_since}:{WORLD_MIN_WORKS}"
    if not manifest.is_done(key):
        filter_string = world_filter(active_since)
        expected = client.count("/sources", filter_string)
        if progress:
            progress(f"Downloading the world reference: {expected:,} active journals…")
        seen = 0
        for index, page in enumerate(
            client.iter_pages("/sources", filter_string, select=WORLD_FIELDS), start=1
        ):
            store.add_many(_compact(source) for source in page)
            seen += len(page)
            if progress and index % 50 == 0:
                progress(f"  {seen:,} / {expected:,} ({client.budget})")
        manifest.mark_done(key, count=expected, seen=seen)
    return pd.DataFrame(list(store.iter_records()))


def _compact(source: dict[str, Any]) -> dict[str, Any]:
    stats = source.get("summary_stats") or {}
    return {
        "id": source.get("id"),
        "venue_name": source.get("display_name"),
        "works_count": source.get("works_count"),
        **{metric: stats.get(metric) for metric in METRICS},
        **main_subfield(source),
    }


def world_percentiles(
    venues: pd.DataFrame,
    world: pd.DataFrame,
    metrics: Iterable[str] = METRICS,
    share: float = TOP_SHARE,
) -> pd.DataFrame:
    """Add, for ranked journals, their position among the world journals of their subfield.

    ``world_pct_<metric>`` is the share of reference journals with a strictly higher
    value; ``world_top10_<metric>`` is ``world_pct < 0.10``; ``world_n`` is the size of
    the subfield's reference set and ``world_threshold_<metric>`` its 90th percentile.
    Subfields with fewer than :data:`MIN_WORLD_N` reference journals are left empty.
    """
    venues = venues.copy()
    groups = {key: frame for key, frame in world.groupby("subfield_id")}
    venues["world_n"] = venues.subfield_id.map(lambda k: len(groups.get(k, ())) or None)
    for metric in metrics:
        pct, threshold = [], []
        for subfield, ranked, value in zip(
            venues.subfield_id, venues.ranked, venues[metric], strict=True
        ):
            reference = groups.get(subfield)
            if not ranked or reference is None or len(reference) < MIN_WORLD_N or pd.isna(value):
                pct.append(None)
                threshold.append(None)
                continue
            values = reference[metric].dropna()
            pct.append(float((values > value).mean()))
            threshold.append(float(values.quantile(1 - share)))
        venues[f"world_pct_{metric}"] = pd.to_numeric(pd.Series(pct, index=venues.index))
        venues[f"world_threshold_{metric}"] = pd.to_numeric(
            pd.Series(threshold, index=venues.index)
        )
        venues[f"world_top10_{metric}"] = venues[f"world_pct_{metric}"].lt(share)
    venues["world_top10_any"] = venues[[f"world_top10_{m}" for m in metrics]].any(axis=1)
    return venues


#: Column order of the venue table: identity, classification, the headline metric and
#: its flags, then the secondary metrics.
def ordered(venues: pd.DataFrame) -> pd.DataFrame:
    head = [
        "venue_id",
        "venue_name",
        "venue_type",
        "main_subfield",
        "main_field",
        "domain",
        "broad_scope",
        "works_list1",
        "works_list2",
        "ranked",
        MAIN_METRIC,
        f"top10_{MAIN_METRIC}",
        f"world_pct_{MAIN_METRIC}",
        f"world_top10_{MAIN_METRIC}",
        f"world_threshold_{MAIN_METRIC}",
        "world_n",
    ]
    secondary = [
        c
        for m in METRICS[1:]
        for c in (
            m,
            f"rank_{m}",
            f"top10_{m}",
            f"world_pct_{m}",
            f"world_top10_{m}",
            f"world_threshold_{m}",
        )
    ]
    head = [c for c in head if c in venues.columns]
    secondary = [c for c in secondary if c in venues.columns]
    rest = [c for c in venues.columns if c not in head + secondary]
    return venues[head + secondary + rest]
