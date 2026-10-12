"""Flatten raw OpenAlex records into analysis tables (parquet) and one Excel export.

Tables, all keyed on ``work_id``:

``works``           one row per work: bibliographic data, venue, open access, citations,
                    primary topic, abstract, and the scope flags below
``authorships``     one row per (work, author, institution) — an author with no resolved
                    institution still gets one row, with empty institution columns
``topics``, ``keywords``, ``funders``, ``awards``, ``sdgs``, ``references``,
``counts_by_year``  one row per (work, item)
``people``          the consolidated roster, with accepted author ids and work counts

Scope flags on ``works``:

* ``in_institution_corpus`` — returned by the institution query (an authorship's
  resolved institution is the target or one of its children);
* ``institution_signed`` — some authorship carries the institution, by id *or* by the
  raw-affiliation pattern (this also recovers works OpenAlex did not resolve);
* ``roster_people`` — roster people among the authors (``;``-separated person ids);
* ``roster_people_signed`` — those of them whose *own* authorship carries the
  institution. Non-empty means "a listed person published this as a member".

The two publication lists (:mod:`bibliometry.lists`):

* ``in_list_institution`` — list 1, every work signed with the institution (same as
  ``institution_signed``);
* ``in_list_roster`` — list 2, works where a listed person signed with the
  institution (``roster_people_signed`` non-empty); a subset of list 1.

How an authorship is linked to a listed person (``authorships.person_link``):

* ``profile`` — its OpenAlex author id is an accepted profile of that person. For a
  profile marked ``conflated`` (two people merged by OpenAlex) only authorships signed
  with the institution are linked;
* ``name`` — OpenAlex attached the authorship to no profile at all (it happens, e.g.
  after OpenAlex splits a merged profile), it is signed with the institution, and
  exactly one listed person has a compatible name.

The raw records hold every field OpenAlex returns; anything not flattened here can still
be read from ``data/raw`` with :func:`iter_all_works`.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

from .config import Config
from .institution import AffiliationMatcher
from .matching import INITIAL_SCORE, name_score
from .openalex import reconstruct_abstract, short_id
from .store import WorksStore

#: Excel's hard row limit, minus the header.
EXCEL_MAX_ROWS = 1_048_575
#: Excel refuses longer cells.
EXCEL_MAX_CELL = 32_767


def excel_safe(frame: pd.DataFrame) -> pd.DataFrame:
    """Drop the control characters Excel refuses and cut cells at its length limit."""

    def clean(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        value = ILLEGAL_CHARACTERS_RE.sub("", value)
        if len(value) > EXCEL_MAX_CELL:
            value = value[: EXCEL_MAX_CELL - 20] + " …[truncated]"
        return value

    return frame.head(EXCEL_MAX_ROWS).map(clean)


def iter_all_works(config: Config) -> Iterator[tuple[dict[str, Any], set[str]]]:
    """Yield each distinct work once with the set of stores it came from."""
    seen: dict[str, set[str]] = defaultdict(set)
    records: dict[str, dict[str, Any]] = {}
    for label, path in (("institution", config.institution_store), ("people", config.people_store)):
        if not path.exists():
            continue
        for record in WorksStore(path).iter_records():
            work_id = short_id(record.get("id"))
            if work_id:
                seen[work_id].add(label)
                records.setdefault(work_id, record)
    for work_id, record in records.items():
        yield record, seen[work_id]


#: Source types that count as a publication venue, best first; a repository (HAL,
#: arXiv, institutional archives) is a deposit, not a venue.
VENUE_TYPES = ("journal", "conference", "book series", "ebook platform")


def venue_of(work: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """The venue of a work and where it was found (``primary`` / ``other location``).

    The primary location, unless it is a repository and another location of the same
    work is a journal or conference: ~1/4 of the institution's works have a HAL or
    arXiv deposit as primary location although they appeared in a journal.
    """
    primary = (work.get("primary_location") or {}).get("source") or {}
    if primary.get("type") != "repository":
        return primary, "primary" if primary else ""
    for venue_type in VENUE_TYPES:
        for location in work.get("locations") or []:
            source = location.get("source") or {}
            if source.get("type") == venue_type:
                return source, "other location"
    return primary, "primary"


def _clean_doi(doi: str | None) -> str:
    return (doi or "").replace("https://doi.org/", "")


def _names(items: list[dict[str, Any]] | None, key: str = "display_name") -> str:
    return "; ".join(str(i.get(key) or "") for i in items or [] if i.get(key))


def build_tables(
    config: Config,
    consolidated: pd.DataFrame | None = None,
) -> dict[str, pd.DataFrame]:
    """Build every table from the raw stores.

    Args:
        config: run configuration (paths, institution, affiliation pattern).
        consolidated: the consolidated roster (``matching.consolidate``); when given,
            authorships are linked to roster people.
    """
    matcher = AffiliationMatcher(config.institution.id, config.institution.affiliation_pattern)
    person_of: dict[str, str] = {}
    conflated: set[str] = set()
    names: list[tuple[str, str, str]] = []  # (person_id, last, first)
    if consolidated is not None:
        for row in consolidated.itertuples():
            for author_id in filter(None, str(row.author_ids).split(";")):
                person_of[author_id] = row.person_id
            conflated.update(filter(None, str(getattr(row, "conflated_ids", "")).split(";")))
            if getattr(row, "last_name", ""):
                names.append((row.person_id, str(row.last_name), str(row.first_name)))

    def link(author_id: str, authorship: dict[str, Any], signal: str) -> tuple[str, str]:
        """(person_id, how) for one authorship; see the module docstring."""
        if author_id in person_of:
            if author_id in conflated and not signal:
                return "", ""
            return person_of[author_id], "profile"
        if author_id or not signal or not names:
            return "", ""
        raw = (
            authorship.get("raw_author_name")
            or (authorship.get("author") or {}).get("display_name")
            or ""
        )
        fits = [pid for pid, last, first in names if name_score(last, first, raw) >= INITIAL_SCORE]
        return (fits[0], "name") if len(fits) == 1 else ("", "")

    rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for work, stores in iter_all_works(config):
        work_id = short_id(work["id"])
        primary = work.get("primary_location") or {}
        source = primary.get("source") or {}
        venue, venue_found_in = venue_of(work)
        oa = work.get("open_access") or {}
        topic = work.get("primary_topic") or {}
        percentile = work.get("citation_normalized_percentile") or {}
        biblio = work.get("biblio") or {}

        people: set[str] = set()
        people_signed: set[str] = set()
        signed = False
        countries: set[str] = set()
        institutions: set[str] = set()
        authorships = work.get("authorships") or []
        for position, authorship in enumerate(authorships, start=1):
            author = authorship.get("author") or {}
            author_id = short_id(author.get("id")) or ""
            signal = matcher.authorship_signal(authorship)
            signed = signed or bool(signal)
            person, how = link(author_id, authorship, signal)
            if person:
                people.add(person)
                if signal:
                    people_signed.add(person)
            countries.update(authorship.get("countries") or [])
            base = {
                "work_id": work_id,
                "author_position_index": position,
                "author_position": authorship.get("author_position"),
                "author_id": author_id,
                "author_name": author.get("display_name"),
                "raw_author_name": authorship.get("raw_author_name"),
                "orcid": (author.get("orcid") or "").replace("https://orcid.org/", ""),
                "is_corresponding": bool(authorship.get("is_corresponding")),
                "institution_signal": signal,
                "person_id": person,
                "person_link": how,
                "raw_affiliations": " | ".join(authorship.get("raw_affiliation_strings") or []),
                "countries": ";".join(authorship.get("countries") or []),
            }
            insts = authorship.get("institutions") or []
            for inst in insts or [{}]:
                inst_id = short_id(inst.get("id")) or ""
                if inst_id:
                    institutions.add(inst_id)
                rows["authorships"].append(
                    {
                        **base,
                        "institution_id": inst_id,
                        "institution_name": inst.get("display_name"),
                        "institution_ror": (inst.get("ror") or "").replace("https://ror.org/", ""),
                        "institution_country": inst.get("country_code"),
                        "institution_type": inst.get("type"),
                        "is_target_institution": matcher.institution_match([inst])
                        if inst
                        else False,
                    }
                )

        rows["works"].append(
            {
                "work_id": work_id,
                "doi": _clean_doi(work.get("doi")),
                "title": work.get("title"),
                "publication_year": work.get("publication_year"),
                "publication_date": work.get("publication_date"),
                "type": work.get("type"),
                "language": work.get("language"),
                "source_id": short_id(source.get("id")),
                "source_name": source.get("display_name"),
                "source_type": source.get("type"),
                "issn_l": source.get("issn_l"),
                "publisher": source.get("host_organization_name"),
                "venue_id": short_id(venue.get("id")),
                "venue_name": venue.get("display_name"),
                "venue_type": venue.get("type"),
                "venue_found_in": venue_found_in,
                "volume": biblio.get("volume"),
                "issue": biblio.get("issue"),
                "first_page": biblio.get("first_page"),
                "last_page": biblio.get("last_page"),
                "is_oa": oa.get("is_oa"),
                "oa_status": oa.get("oa_status"),
                "oa_url": oa.get("oa_url"),
                "license": primary.get("license"),
                "cited_by_count": work.get("cited_by_count"),
                "fwci": work.get("fwci"),
                "citation_percentile": percentile.get("value"),
                "top_1_percent": percentile.get("is_in_top_1_percent"),
                "top_10_percent": percentile.get("is_in_top_10_percent"),
                "n_authors": len(authorships),
                "n_institutions": work.get("institutions_distinct_count"),
                "n_countries": work.get("countries_distinct_count"),
                "countries": ";".join(sorted(countries)),
                "international": len(countries) > 1,
                "primary_topic": topic.get("display_name"),
                "subfield": (topic.get("subfield") or {}).get("display_name"),
                "field": (topic.get("field") or {}).get("display_name"),
                "domain": (topic.get("domain") or {}).get("display_name"),
                "funders": _names(work.get("funders")),
                "referenced_works_count": work.get("referenced_works_count"),
                "is_retracted": work.get("is_retracted"),
                "is_paratext": work.get("is_paratext"),
                "in_institution_corpus": "institution" in stores,
                "in_people_corpus": "people" in stores,
                "institution_signed": signed,
                "roster_people": ";".join(sorted(people)),
                "roster_people_signed": ";".join(sorted(people_signed)),
                "in_list_institution": signed,
                "in_list_roster": bool(people_signed),
                "abstract": reconstruct_abstract(work.get("abstract_inverted_index")),
                "openalex_url": work.get("id"),
                "updated_date": work.get("updated_date"),
            }
        )

        for rank, t in enumerate(work.get("topics") or [], start=1):
            rows["topics"].append(
                {
                    "work_id": work_id,
                    "rank": rank,
                    "topic_id": short_id(t.get("id")),
                    "topic": t.get("display_name"),
                    "score": t.get("score"),
                    "subfield": (t.get("subfield") or {}).get("display_name"),
                    "field": (t.get("field") or {}).get("display_name"),
                    "domain": (t.get("domain") or {}).get("display_name"),
                }
            )
        for k in work.get("keywords") or []:
            rows["keywords"].append(
                {"work_id": work_id, "keyword": k.get("display_name"), "score": k.get("score")}
            )
        for f in work.get("funders") or []:
            rows["funders"].append(
                {
                    "work_id": work_id,
                    "funder_id": short_id(f.get("id")),
                    "funder": f.get("display_name"),
                    "ror": (f.get("ror") or "").replace("https://ror.org/", ""),
                }
            )
        # ``awards`` replaced ``grants`` in the 2025 OpenAlex schema; read both.
        for a in (work.get("awards") or []) + (work.get("grants") or []):
            rows["awards"].append(
                {
                    "work_id": work_id,
                    "funder_id": short_id(a.get("funder_id") or a.get("funder")),
                    "funder": a.get("funder_display_name"),
                    "award_id": a.get("funder_award_id") or a.get("award_id"),
                }
            )
        for g in work.get("sustainable_development_goals") or []:
            rows["sdgs"].append(
                {"work_id": work_id, "sdg": g.get("display_name"), "score": g.get("score")}
            )
        for ref in work.get("referenced_works") or []:
            rows["references"].append({"work_id": work_id, "referenced_work_id": short_id(ref)})
        for c in work.get("counts_by_year") or []:
            rows["counts_by_year"].append(
                {
                    "work_id": work_id,
                    "year": c.get("year"),
                    "cited_by_count": c.get("cited_by_count"),
                }
            )

    tables = {name: pd.DataFrame(data) for name, data in rows.items()}
    for name in (
        "works",
        "authorships",
        "topics",
        "keywords",
        "funders",
        "awards",
        "sdgs",
        "references",
        "counts_by_year",
    ):
        tables.setdefault(name, pd.DataFrame())
    if consolidated is not None:
        tables["people"] = _people_table(consolidated, tables["works"])
    return tables


def _people_table(consolidated: pd.DataFrame, works: pd.DataFrame) -> pd.DataFrame:
    """The consolidated roster with per-person work counts in the period."""
    people = consolidated.copy()

    def count(column: str) -> pd.Series:
        if works.empty:
            return pd.Series(dtype=int)
        exploded = works[column].str.split(";").explode()
        return exploded[exploded != ""].value_counts()

    people["works_in_period"] = people.person_id.map(count("roster_people")).fillna(0).astype(int)
    people["works_signed_with_institution"] = (
        people.person_id.map(count("roster_people_signed")).fillna(0).astype(int)
    )
    return people


def write_tables(tables: dict[str, pd.DataFrame], directory: Path) -> list[Path]:
    """Write each table as parquet; returns the paths written."""
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, frame in tables.items():
        path = directory / f"{name}.parquet"
        frame.to_parquet(path, index=False)
        written.append(path)
    return written


def read_tables(directory: Path) -> dict[str, pd.DataFrame]:
    """Load every parquet table from ``directory``."""
    return {path.stem: pd.read_parquet(path) for path in sorted(Path(directory).glob("*.parquet"))}


EXCEL_README = [
    (
        "works",
        "One row per publication. Filter on 'institution_signed' = TRUE to keep only works "
        "where at least one author signed with the institution.",
    ),
    (
        "people",
        "The list of people, with the OpenAlex profiles accepted for each and their work counts.",
    ),
    (
        "authorships",
        "One row per (publication, author, institution), restricted to roster people "
        "and to authors affiliated with the institution.",
    ),
    ("topics", "OpenAlex topics of each publication (up to 3), with subfield/field/domain."),
    ("funders", "Funders acknowledged by each publication."),
    (
        "columns",
        "roster_people = listed people among the authors; roster_people_signed = those who "
        "signed with the institution. in_institution_corpus = found by the institution query.",
    ),
]


def export_excel(tables: dict[str, pd.DataFrame], path: Path) -> Path:
    """One workbook for people who do not use Python. Large tables are trimmed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    authorships = tables.get("authorships", pd.DataFrame())
    if not authorships.empty:
        authorships = authorships[
            (authorships.person_id != "") | (authorships.institution_signal != "")
        ]
    sheets = {
        "README": pd.DataFrame(EXCEL_README, columns=["sheet", "content"]),
        "works": tables.get("works", pd.DataFrame()).drop(columns=["abstract"], errors="ignore"),
        "people": tables.get("people", pd.DataFrame()),
        "authorships": authorships,
        "topics": tables.get("topics", pd.DataFrame()),
        "funders": tables.get("funders", pd.DataFrame()),
    }
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for name, frame in sheets.items():
            if frame is None or frame.empty:
                continue
            excel_safe(frame).to_excel(writer, sheet_name=name, index=False)
    return path
