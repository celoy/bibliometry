"""Starter bibliometric indicators over the tables built by :mod:`bibliometry.tables`.

Every indicator takes the ``works`` table already restricted to a *scope* (see
:func:`in_scope`), so the choice of what counts is made once, visibly, at the top of
the analysis notebook rather than hidden in each function.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

import matplotlib.pyplot as plt
import pandas as pd

from .institution import fold

#: OpenAlex work types counted as research output by default. Excluded: datasets,
#: peer-review reports, errata, paratext, editorials, letters, "other".
RESEARCH_TYPES = frozenset(
    {
        "article",
        "review",
        "conference-paper",
        "book-chapter",
        "book",
        "preprint",
        "dissertation",
        "report",
    }
)

#: Categorical slots in fixed order (validated reference palette, light mode).
#: Series beyond eight are folded into "Other", never given a generated colour.
CATEGORICAL = [
    "#2a78d6",
    "#eb6834",
    "#1baf7a",
    "#eda100",
    "#e87ba4",
    "#008300",
    "#6250d6",
    "#e34948",
]
OTHER_COLOR = "#a3a29c"
TEXT_SECONDARY = "#52514e"
GRID = "#e4e3df"


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def in_scope(
    works: pd.DataFrame,
    signed_only: bool = True,
    types: Iterable[str] | None = RESEARCH_TYPES,
    exclude_retracted: bool = True,
) -> pd.DataFrame:
    """Works counted in the analysis.

    Args:
        works: the ``works`` table.
        signed_only: keep works where some author signed with the institution (by id
            or raw affiliation). ``False`` adds the listed people's works signed
            elsewhere.
        types: OpenAlex work types to keep; ``None`` keeps all.
        exclude_retracted: drop retracted works.
    """
    mask = pd.Series(True, index=works.index)
    if signed_only:
        mask &= works.institution_signed
    if types is not None:
        mask &= works.type.isin(set(types))
    if exclude_retracted:
        mask &= ~works.is_retracted.fillna(False).astype(bool)
    return works[mask]


#: Titles shorter than this (after folding) are too generic to deduplicate on
#: ("Editorial", "Introduction", "Preface"…).
MIN_TITLE_KEY = 30
#: Preferred version when several works share a title: published before deposited.
SOURCE_PREFERENCE = {
    "journal": 0,
    "conference": 1,
    "book series": 2,
    "ebook platform": 3,
    "repository": 5,
}
TYPE_PREFERENCE = {
    "article": 0,
    "review": 0,
    "conference-paper": 1,
    "book-chapter": 1,
    "book": 1,
    "preprint": 4,
}


def title_key(title: str | None) -> str:
    """Folded alphanumeric form of a title, for spotting the same work twice."""
    return re.sub(r"[^a-z0-9]", "", fold(title or ""))


def deduplicate(works: pd.DataFrame) -> pd.DataFrame:
    """Keep one work per title: OpenAlex often holds an article *and* its preprint or
    HAL/arXiv deposit as separate works (~6% of works in a test institution
    over five years). The published version is kept (journal > conference > repository,
    article > preprint, then with a DOI, then most cited); the scope flags of the
    dropped versions are merged into it. Adds ``n_versions``.
    """
    keyed = works.assign(_key=works.title.map(title_key))
    long_enough = keyed._key.str.len() >= MIN_TITLE_KEY
    keyed.loc[~long_enough, "_key"] = keyed.loc[~long_enough, "work_id"]
    keyed = keyed.assign(
        _rank_source=keyed.source_type.map(SOURCE_PREFERENCE).fillna(4),
        _rank_type=keyed.type.map(TYPE_PREFERENCE).fillna(2),
        _no_doi=keyed.doi.fillna("") == "",
    ).sort_values(
        ["_key", "_rank_type", "_rank_source", "_no_doi", "cited_by_count"],
        ascending=[True, True, True, True, False],
    )

    def merge_people(values: pd.Series) -> str:
        return ";".join(sorted({p for v in values for p in str(v).split(";") if p}))

    merged = keyed.groupby("_key").agg(
        n_versions=("work_id", "size"),
        roster_people=("roster_people", merge_people),
        roster_people_signed=("roster_people_signed", merge_people),
        institution_signed=("institution_signed", "any"),
    )
    kept = keyed.drop_duplicates("_key").set_index("_key")
    kept = kept.drop(columns=merged.columns.drop("n_versions")).join(merged)
    helper = [c for c in kept.columns if c.startswith("_")]
    return kept.drop(columns=helper).reset_index(drop=True)


def explode_people(works: pd.DataFrame, column: str = "roster_people_signed") -> pd.DataFrame:
    """One row per (work, roster person) from a ``;``-separated person column."""
    exploded = works.assign(person_id=works[column].str.split(";")).explode("person_id")
    return exploded[exploded.person_id.fillna("") != ""]


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------


def works_per_year(works: pd.DataFrame, by: str | None = None) -> pd.DataFrame:
    """Counts per publication year, optionally one column per value of ``by``."""
    if by is None:
        return works.groupby("publication_year").size().rename("works").to_frame()
    return works.groupby(["publication_year", by]).size().unstack(fill_value=0)


def works_per_lab(works: pd.DataFrame, people: pd.DataFrame) -> pd.DataFrame:
    """Works per year and roster lab (a work signed by two labs counts once in each)."""
    per_person = explode_people(works).merge(people[["person_id", "lab"]], on="person_id")
    per_lab = per_person.drop_duplicates(["work_id", "lab"])
    return per_lab.groupby(["publication_year", "lab"]).size().unstack(fill_value=0)


def type_counts(works: pd.DataFrame) -> pd.Series:
    return works.type.value_counts()


def open_access(works: pd.DataFrame) -> pd.DataFrame:
    """Share of each OA status per year (rows sum to 1)."""
    table = works.groupby(["publication_year", "oa_status"]).size().unstack(fill_value=0)
    return table.div(table.sum(axis=1), axis=0)


def top_venues(works: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    venues = works.dropna(subset=["source_name"])
    return (
        venues.groupby(["source_name", "source_type"])
        .agg(works=("work_id", "size"), citations=("cited_by_count", "sum"))
        .sort_values("works", ascending=False)
        .head(n)
        .reset_index()
    )


def citation_summary(works: pd.DataFrame) -> pd.DataFrame:
    """Per year: works, median citations, mean FWCI, share in the world top 10%/1%."""
    return works.groupby("publication_year").agg(
        works=("work_id", "size"),
        median_citations=("cited_by_count", "median"),
        mean_fwci=("fwci", "mean"),
        median_fwci=("fwci", "median"),
        share_top10=("top_10_percent", "mean"),
        share_top1=("top_1_percent", "mean"),
    )


def international_share(works: pd.DataFrame) -> pd.Series:
    """Share of works with authors in more than one country, per year."""
    return works.groupby("publication_year").international.mean().rename("international_share")


def partners(
    authorships: pd.DataFrame,
    works: pd.DataFrame,
    institution_id: str,
    n: int = 25,
) -> pd.DataFrame:
    """Most frequent co-signing institutions (number of distinct works)."""
    rows = authorships[
        authorships.work_id.isin(works.work_id)
        & (authorships.institution_id != "")
        & (authorships.institution_id != institution_id)
    ]
    return (
        rows.drop_duplicates(["work_id", "institution_id"])
        .groupby(["institution_id", "institution_name", "institution_country"], dropna=False)
        .size()
        .rename("works")
        .sort_values(ascending=False)
        .head(n)
        .reset_index()
    )


def partner_countries(authorships: pd.DataFrame, works: pd.DataFrame, n: int = 25) -> pd.Series:
    rows = authorships[
        authorships.work_id.isin(works.work_id) & authorships.institution_country.notna()
    ]
    return (
        rows.drop_duplicates(["work_id", "institution_country"])
        .institution_country.value_counts()
        .head(n)
    )


def roster_coverage(works: pd.DataFrame, all_works: pd.DataFrame) -> dict[str, float]:
    """How the listed people relate to the institution's output.

    * ``share_with_roster`` — institution-signed works with ≥1 listed person signing
      with the institution: how much of the output the list explains;
    * ``roster_works_not_signed`` — listed people's works where none of them signed
      with the institution (affiliated elsewhere, or affiliation missing in OpenAlex).
    """
    roster_works = all_works[all_works.roster_people != ""]
    return {
        "institution_works": len(works),
        "with_roster_person_signed": int((works.roster_people_signed != "").sum()),
        "share_with_roster": float((works.roster_people_signed != "").mean())
        if len(works)
        else 0.0,
        "roster_works": len(roster_works),
        "roster_works_not_signed": int((roster_works.roster_people_signed == "").sum()),
    }


def top_topics(
    topics: pd.DataFrame, works: pd.DataFrame, level: str = "field", n: int = 15
) -> pd.Series:
    """Works per OpenAlex topic level (``topic``, ``subfield``, ``field``, ``domain``),
    using each work's primary (rank-1) topic."""
    primary = topics[(topics["rank"] == 1) & topics.work_id.isin(works.work_id)]
    return primary[level].value_counts().head(n)


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------


def _style(ax: plt.Axes, title: str, ylabel: str = "") -> None:
    ax.set_title(title, loc="left", fontsize=12)
    ax.set_ylabel(ylabel, color=TEXT_SECONDARY)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=TEXT_SECONDARY, length=0)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def fold_series(table: pd.DataFrame, max_series: int = 7) -> pd.DataFrame:
    """Keep the ``max_series`` largest columns; sum the rest into ``Other``."""
    order = table.sum().sort_values(ascending=False).index
    if len(order) <= max_series + 1:
        return table[order]
    kept = table[order[:max_series]].copy()
    kept["Other"] = table[order[max_series:]].sum(axis=1)
    return kept


def plot_lines(table: pd.DataFrame, title: str, ylabel: str = "works") -> plt.Figure:
    """One line per column, colours in fixed slot order, legend plus end labels."""
    table = fold_series(table) if table.shape[1] > 1 else table
    fig, ax = plt.subplots(figsize=(8, 4))
    for index, column in enumerate(table.columns):
        color = OTHER_COLOR if column == "Other" else CATEGORICAL[index % len(CATEGORICAL)]
        ax.plot(
            table.index,
            table[column],
            color=color,
            linewidth=2,
            marker="o",
            markersize=4,
            label=str(column),
        )
    if table.shape[1] > 1:
        ax.legend(frameon=False, fontsize=9, loc="upper left", bbox_to_anchor=(1, 1))
    ax.set_xticks(list(table.index))
    _style(ax, title, ylabel)
    fig.tight_layout()
    return fig


def plot_bars(series: pd.Series, title: str, xlabel: str = "works") -> plt.Figure:
    """Horizontal bars for a ranked list (largest on top), single hue."""
    series = series.sort_values()
    fig, ax = plt.subplots(figsize=(8, max(2.5, 0.3 * len(series) + 1)))
    ax.barh([str(i) for i in series.index], series.values, color=CATEGORICAL[0], height=0.7)
    for y, value in enumerate(series.values):
        ax.text(value, y, f" {value:,.0f}", va="center", fontsize=8, color=TEXT_SECONDARY)
    _style(ax, title)
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.set_xlabel(xlabel, color=TEXT_SECONDARY)
    fig.tight_layout()
    return fig


def plot_stacked_shares(table: pd.DataFrame, title: str) -> plt.Figure:
    """Stacked bars of shares per year (rows sum to 1), e.g. open-access status."""
    table = fold_series(table)
    fig, ax = plt.subplots(figsize=(8, 4))
    bottom = pd.Series(0.0, index=table.index)
    for index, column in enumerate(table.columns):
        color = OTHER_COLOR if column == "Other" else CATEGORICAL[index % len(CATEGORICAL)]
        ax.bar(
            table.index,
            table[column],
            bottom=bottom,
            color=color,
            width=0.7,
            edgecolor="white",
            linewidth=1,
            label=str(column),
        )
        bottom += table[column]
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0%}"))
    ax.legend(frameon=False, fontsize=9, loc="upper left", bbox_to_anchor=(1, 1))
    ax.set_xticks(list(table.index))
    _style(ax, title)
    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# Most-cited shares and venue indicators
# ---------------------------------------------------------------------------


def top_cited(works: pd.DataFrame, by: str | None = "publication_year") -> pd.DataFrame:
    """Number and share of works in the world's top 10% / 1% most cited.

    Uses OpenAlex's ``citation_normalized_percentile``, which compares a work with works
    of the same field, year and type. Works without a percentile (OpenAlex computes none
    for some types and very recent works) are excluded from the shares; ``coverage``
    says how many have one. Expected shares for an average institution: 10% and 1%.
    """

    def summary(group: pd.DataFrame) -> pd.Series:
        known = group.citation_percentile.notna()
        n_known = int(known.sum())
        top10 = int(group.top_10_percent.fillna(False).astype(bool).sum())
        top1 = int(group.top_1_percent.fillna(False).astype(bool).sum())
        return pd.Series(
            {
                "works": len(group),
                "with_percentile": n_known,
                "coverage": n_known / len(group) if len(group) else float("nan"),
                "top10": top10,
                "share_top10": top10 / n_known if n_known else float("nan"),
                "top1": top1,
                "share_top1": top1 / n_known if n_known else float("nan"),
            }
        )

    total = summary(works).rename("all").to_frame().T
    if by is None:
        return total
    per_group = works.groupby(by).apply(summary, include_groups=False)
    per_group.index = per_group.index.astype(str)
    return pd.concat([per_group, total])


def venue_indicators(works: pd.DataFrame, venues: pd.DataFrame) -> pd.DataFrame:
    """Number and share of works published in a top-10% venue, per metric.

    The share is over works whose venue was ranked (a journal of the reference set);
    works in conferences, books or repositories only are counted in ``unranked``.
    """
    from .venues import METRICS

    flags = [
        f"{prefix}top10_{m}"
        for m in (*METRICS, "any")
        for prefix in ("", "world_")
        if f"{prefix}top10_{m}" in venues.columns
    ]
    ranked_ids = set(venues.loc[venues.ranked, "venue_id"])
    in_ranked = works.venue_id.isin(ranked_ids)
    out = {
        "works": len(works),
        "in_ranked_journal": int(in_ranked.sum()),
        "unranked": int((~in_ranked).sum()),
    }
    for flag in flags:
        top_ids = set(venues.loc[venues[flag], "venue_id"])
        n = int(works.venue_id.isin(top_ids).sum())
        out[f"{flag}_works"] = n
        out[f"{flag}_share"] = (
            n / out["in_ranked_journal"] if out["in_ranked_journal"] else float("nan")
        )
    return pd.Series(out)


def indicator_report(
    works: pd.DataFrame, venues: pd.DataFrame, authorships: pd.DataFrame | None = None
) -> dict[str, pd.DataFrame]:
    """Both lists × two scopes: every work, and research outputs deduplicated.

    Returns ``{"top_cited": …, "top_venues": …}`` (rows are (list, scope[, year])) and,
    when ``authorships`` is given, ``top1_papers_list1`` / ``top1_papers_list2``: the
    world top-1% papers of each list (research outputs, deduplicated) with authors.
    """
    lists = {"list1_institution": "in_list_institution", "list2_roster": "in_list_roster"}
    cited, venue_rows, papers = [], {}, {}
    for name, column in lists.items():
        selected = works[works[column]]
        scopes = {
            "all works": selected,
            "research, deduplicated": deduplicate(in_scope(selected, signed_only=False)),
        }
        for scope, frame in scopes.items():
            table = top_cited(frame)
            table.index = pd.MultiIndex.from_product(
                [[name], [scope], table.index], names=["list", "scope", "year"]
            )
            cited.append(table)
            venue_rows[(name, scope)] = venue_indicators(frame, venues)
            if authorships is not None and scope.startswith("research"):
                papers[name] = top_papers(frame, authorships)
    top_venues = pd.DataFrame(venue_rows).T
    top_venues.index.names = ["list", "scope"]
    out = {"top_cited": pd.concat(cited), "top_venues": top_venues}
    out.update({f"top1_papers_{name.split('_')[0]}": frame for name, frame in papers.items()})
    return out


def top_papers(
    works: pd.DataFrame,
    authorships: pd.DataFrame,
    flag: str = "top_1_percent",
    max_authors: int = 20,
) -> pd.DataFrame:
    """The works carrying ``flag`` (default: world top 1% most cited), with their authors.

    Authors are listed in order; those signing with the institution are marked ``*``
    and the people of the list are followed by their person id in brackets. Lists
    longer than ``max_authors`` are cut with "et al. (n authors)".
    """
    selected = works[works[flag].fillna(False).astype(bool)]
    rows = authorships[authorships.work_id.isin(selected.work_id)].drop_duplicates(
        ["work_id", "author_position_index"]
    )

    def label(row: pd.Series) -> str:
        name = row.author_name or row.raw_author_name or "?"
        if row.institution_signal:
            name += "*"
        if row.person_id:
            name += f" [{row.person_id}]"
        return name

    def join(group: pd.DataFrame) -> str:
        names = [label(r) for _, r in group.sort_values("author_position_index").iterrows()]
        if len(names) > max_authors:
            return "; ".join(names[:max_authors]) + f"; et al. ({len(names)} authors)"
        return "; ".join(names)

    authors = rows.groupby("work_id").apply(join, include_groups=False).rename("authors")
    columns = [
        "work_id",
        "publication_year",
        "title",
        "venue_name",
        "type",
        "doi",
        "cited_by_count",
        "fwci",
        "citation_percentile",
        "roster_people_signed",
    ]
    out = selected[columns].merge(authors, on="work_id", how="left")
    return out.sort_values(["publication_year", "cited_by_count"], ascending=[True, False])
