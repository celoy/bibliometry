"""Per-person output: each listed person's publications as short references, and
their indicators.

Only publications where *that person* signed with the institution are kept (list 2,
split by person). Papers by other signers of the institution, such as staff of a
partner university in a joint lab, and the person's papers signed elsewhere, are
left out. A paper co-signed by two listed people appears under each of them.

Within each person, an article and its preprint or repository copy are counted once
(:func:`bibliometry.analysis.deduplicate`). Paratext and published referee reports
are dropped (:data:`EXCLUDED_TYPES`); every other OpenAlex type is kept and shown in
``type``.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .analysis import RESEARCH_TYPES, author_lists, deduplicate, top_cited, venue_indicators
from .tables import excel_safe

#: Columns of the per-person sheets, in order.
REFERENCE_COLUMNS = [
    "publication_year",
    "authors",
    "title",
    "journal",
    "volume",
    "issue",
    "pages",
    "doi",
    "type",
    "co_listed",
    "reference",
]
#: Columns of the long table: who, then the reference, then a few indicators.
PUBLICATION_COLUMNS = [
    "person_id",
    "last_name",
    "first_name",
    "lab",
    *REFERENCE_COLUMNS,
    "doi_url",
    "cited_by_count",
    "top_10_percent",
    "top_1_percent",
    "work_id",
]
#: OpenAlex types that are not the person's publications: published referee reports
#: and paratext (covers, tables of contents).
EXCLUDED_TYPES = frozenset({"peer-review", "paratext"})
#: Excel sheet names: at most 31 characters, and none of these.
_SHEET_FORBIDDEN = str.maketrans({c: "-" for c in "[]:*?/\\"})


def _text(value: object) -> str:
    return "" if pd.isna(value) else str(value).strip()


def pages(first: object, last: object) -> str:
    """``"12–25"``, or just the first page (article number) when there is no last."""
    first, last = _text(first), _text(last)
    if first and last and last != first:
        return f"{first}–{last}"
    return first or last


def reference(row: pd.Series) -> str:
    """One-line reference: Authors (year). Title. Journal vol(issue), pages. doi."""
    head = f"{row.authors} ({_text(row.publication_year)}). {_text(row.title)}."
    volume = _text(row.volume)
    if volume and _text(row.issue):
        volume += f"({_text(row.issue)})"
    where = ", ".join(
        p for p in (" ".join(filter(None, (_text(row.journal), volume))), row.pages) if p
    )
    parts = [head.strip()]
    if where:
        parts.append(where + ".")
    if row.doi:
        parts.append(f"doi:{row.doi}")
    return " ".join(parts)


def _has(works: pd.DataFrame, column: str, person: str) -> pd.Series:
    """Rows whose ``;``-separated ``column`` holds ``person``."""
    return works[column].fillna("").str.split(";").map(lambda ids: person in ids)


def _signed_by(works: pd.DataFrame, person: str) -> pd.DataFrame:
    """Works where ``person`` signed with the institution, one per title."""
    mask = _has(works, "roster_people_signed", person) & ~works.type.isin(EXCLUDED_TYPES)
    mine = works[mask & ~works.is_paratext.fillna(False).astype(bool)]
    return deduplicate(mine) if not mine.empty else mine


def publications_by_person(
    works: pd.DataFrame,
    people: pd.DataFrame,
    authorships: pd.DataFrame,
    max_authors: int = 10,
) -> pd.DataFrame:
    """One row per (listed person, publication they signed with the institution).

    Sorted by person, then most recent year first.
    """
    per_person = [_signed_by(works, person).assign(person_id=person) for person in people.person_id]
    per_person = [frame for frame in per_person if not frame.empty]
    if not per_person:
        return pd.DataFrame(columns=PUBLICATION_COLUMNS)
    pubs = pd.concat(per_person, ignore_index=True)
    authors = author_lists(authorships, pubs.work_id, max_authors, marks=False)
    pubs = pubs.merge(authors, on="work_id", how="left").fillna({"authors": ""})
    who = people.set_index("person_id")[["last_name", "first_name", "lab"]]
    pubs = pubs.join(who, on="person_id")
    pubs["journal"] = pubs.venue_name
    pubs["pages"] = [pages(f, la) for f, la in zip(pubs.first_page, pubs.last_page, strict=True)]
    pubs["doi"] = pubs.doi.fillna("")
    pubs["doi_url"] = pubs.doi.map(lambda d: f"https://doi.org/{d}" if d else "")
    pubs["co_listed"] = [
        ";".join(p for p in signed.split(";") if p and p != person)
        for signed, person in zip(pubs.roster_people_signed, pubs.person_id, strict=True)
    ]
    pubs["reference"] = pubs.apply(reference, axis=1)
    order = {person: i for i, person in enumerate(people.person_id)}
    pubs = pubs.assign(_order=pubs.person_id.map(order)).sort_values(
        ["_order", "publication_year", "title"], ascending=[True, False, True]
    )
    return pubs[PUBLICATION_COLUMNS].reset_index(drop=True)


def person_year_counts(pubs: pd.DataFrame) -> pd.DataFrame:
    """Publications per person (rows) and year (columns)."""
    if pubs.empty:
        return pd.DataFrame()
    counts = pubs.groupby(["person_id", "publication_year"]).size().unstack(fill_value=0)
    counts.columns = [str(int(year)) for year in counts.columns]
    return counts


def person_indicators(
    works: pd.DataFrame, people: pd.DataFrame, venues: pd.DataFrame | None = None
) -> pd.DataFrame:
    """One row per listed person: the indicators of the one-person run, for everyone.

    ``signed`` counts every publication signed with the institution (one per title);
    the most-cited and journal figures are over research outputs only, as in
    ``indicators.xlsx``. ``signed_elsewhere`` counts the person's works not signed
    with the institution: many of them can mean a wrong profile match, or a person
    who joined recently.
    """
    venue_flags = [
        f"{prefix}top10_2yr_mean_citedness"
        for prefix in ("world_", "")
        if venues is not None and f"{prefix}top10_2yr_mean_citedness" in venues.columns
    ]
    rows = []
    for person in people.person_id:
        signed = _signed_by(works, person)
        research = signed[signed.type.isin(RESEARCH_TYPES)] if not signed.empty else signed
        row = {
            "person_id": person,
            "signed": len(signed),
            "research": len(research),
            "first_year": signed.publication_year.min() if len(signed) else None,
            "last_year": signed.publication_year.max() if len(signed) else None,
            "signed_elsewhere": int(
                (
                    _has(works, "roster_people", person)
                    & ~_has(works, "roster_people_signed", person)
                ).sum()
            ),
        }
        if len(research):
            cited = top_cited(research, by=None).iloc[0]
            row.update(
                {k: cited[k] for k in ("coverage", "top10", "share_top10", "top1", "share_top1")}
            )
            if venues is not None and venue_flags:
                in_venues = venue_indicators(research, venues)
                row["in_ranked_journal"] = in_venues["in_ranked_journal"]
                for flag in venue_flags:
                    row[f"{flag}_works"] = in_venues[f"{flag}_works"]
                    row[f"{flag}_share"] = in_venues[f"{flag}_share"]
        rows.append(row)
    out = pd.DataFrame(rows)
    counts = ["first_year", "last_year", "top10", "top1", "in_ranked_journal"]
    counts += [f"{flag}_works" for flag in venue_flags]
    for column in counts:
        if column in out.columns:
            out[column] = pd.to_numeric(out[column]).astype("Int64")
    who = people.set_index("person_id")[["last_name", "first_name", "lab", "match_status"]]
    return who.join(out.set_index("person_id"), how="right").reset_index()


def person_summary(
    works: pd.DataFrame,
    people: pd.DataFrame,
    pubs: pd.DataFrame,
    venues: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """:func:`person_indicators` followed by the publications per year (one column each)."""
    counts = person_year_counts(pubs)
    summary = person_indicators(works, people, venues).join(counts, on="person_id")
    summary[list(counts.columns)] = summary[list(counts.columns)].fillna(0).astype(int)
    return summary


def _sheet_names(person_ids: list[str]) -> dict[str, str]:
    """A distinct valid Excel sheet name for each person."""
    names: dict[str, str] = {}
    used = {"README", "summary", "all"}
    for person in person_ids:
        base = person.translate(_SHEET_FORBIDDEN)[:31]
        name, n = base, 2
        while name in used:
            suffix = f"~{n}"
            name, n = base[: 31 - len(suffix)] + suffix, n + 1
        used.add(name)
        names[person] = name
    return names


README = [
    (
        "summary",
        "One row per person of the list: publications signed with the institution "
        "(total, research outputs, first and last year), share in the world's top 10% / 1% "
        "most cited, share in top-10% journals, works signed elsewhere, then the count per "
        "year.",
    ),
    ("all", "Every (person, publication) pair: one table to filter by person or year."),
    (
        "<person_id>",
        "One sheet per person with at least one publication, most recent first.",
    ),
    (
        "scope",
        "Only publications where that person signed with the institution. Papers by "
        "other signers of the institution (e.g. partner-university staff in a joint lab) "
        "and the person's papers signed elsewhere are not included. A preprint or "
        "repository copy of a paper is counted once.",
    ),
    (
        "co_listed",
        "Other people of the list who signed the same paper with the institution: the "
        "paper also appears in their sheet, so do not add up sheets to count the "
        "institution's output.",
    ),
]


def write_workbook(pubs: pd.DataFrame, summary: pd.DataFrame, directory: Path) -> list[Path]:
    """``publications_by_person.xlsx`` (sheets README, summary, all, one per person)
    and ``publications_by_person.parquet`` (the long table)."""
    directory.mkdir(parents=True, exist_ok=True)
    parquet = directory / "publications_by_person.parquet"
    pubs.to_parquet(parquet, index=False)
    xlsx = directory / "publications_by_person.xlsx"
    sheets = _sheet_names(list(dict.fromkeys(pubs.person_id)))
    with pd.ExcelWriter(xlsx, engine="openpyxl") as writer:
        pd.DataFrame(README, columns=["sheet", "content"]).to_excel(
            writer, sheet_name="README", index=False
        )
        excel_safe(summary).to_excel(writer, sheet_name="summary", index=False)
        excel_safe(pubs).to_excel(writer, sheet_name="all", index=False)
        for person, frame in pubs.groupby("person_id", sort=False):
            excel_safe(frame[REFERENCE_COLUMNS]).to_excel(
                writer, sheet_name=sheets[person], index=False
            )
    return [parquet, xlsx]
