"""The pipeline steps, each reading and writing files under ``data/``.

Notebooks and the command line call these same functions, so both behave identically.
Every step is safe to re-run: harvests resume and skip completed queries, and the
matching step keeps the verdicts already typed into the review file.

1. :func:`load_roster`             roster file           -> review/roster.csv
2. :func:`harvest_institution_step` OpenAlex             -> raw/institution_works.jsonl.gz
3. :func:`match_step`              roster + corpus       -> review/author_matches.csv,
                                                            review/people.csv
4. :func:`harvest_people_step`     accepted profiles     -> raw/people_works.jsonl.gz
5. :func:`tables_step`             raw stores + people   -> tables/*.parquet, exports/*.xlsx
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pandas as pd

from . import matching
from .config import Config, ConfigError, InstitutionSettings, RosterSettings
from .harvest import HarvestStats, harvest_institution, harvest_people
from .institution import AffiliationMatcher, build_affiliation_pattern
from .openalex import OpenAlexClient, short_id
from .roster import read_roster
from .store import Manifest, WorksStore
from .tables import build_tables, excel_safe, export_excel, write_tables

Progress = Callable[[str], None]


def make_client(config: Config) -> OpenAlexClient:
    """An OpenAlex client identified with the configured email and API key."""
    return OpenAlexClient(mailto=config.mailto, api_key=config.api_key())


def make_matcher(config: Config) -> AffiliationMatcher:
    return AffiliationMatcher(config.institution.id, config.institution.affiliation_pattern)


def people_path(config: Config) -> Path:
    return config.review_dir / "people.csv"


def load_roster(config: Config) -> pd.DataFrame:
    """Read the roster named in the config and save its tidy form."""
    roster = read_roster(
        config.roster_source(),
        config.roster.sheets,
        config.roster.columns,
        config.roster.lab_aliases,
    )
    if roster.attrs.get("merged"):
        print("Listed more than once (merged into one row):", ", ".join(roster.attrs["merged"]))
    config.review_dir.mkdir(parents=True, exist_ok=True)
    roster.to_csv(config.roster_table, index=False, encoding="utf-8-sig")
    return roster


def harvest_institution_step(
    config: Config, client: OpenAlexClient, refresh: bool = False, progress: Progress = print
) -> HarvestStats:
    """All works of the institution in the period."""
    return harvest_institution(
        client,
        config.institution.id,
        config.from_date,
        config.to_date,
        WorksStore(config.institution_store),
        Manifest(config.manifest_path),
        refresh=refresh,
        progress=progress,
    )


def match_step(
    config: Config,
    client: OpenAlexClient | None,
    roster: pd.DataFrame | None = None,
    search_missing: bool = True,
    progress: Progress = print,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Map roster people to OpenAlex profiles; returns (matches, consolidated roster).

    Needs the institution corpus (step 2). Pass ``client=None`` to recompute from
    cached answers only, e.g. after editing verdicts.
    """
    if not config.institution_store.exists():
        raise FileNotFoundError("Harvest the institution corpus first (step 2).")
    roster = load_roster(config) if roster is None else roster
    matcher = make_matcher(config)
    progress("Indexing the authors of the institution corpus…")
    corpus_authors = matching.index_corpus_authors(
        WorksStore(config.institution_store).iter_records(), matcher
    )
    cache = matching.LookupCache(config.review_dir / "lookup_cache.json")
    try:
        candidates = matching.gather_candidates(
            roster,
            matching.SurnameIndex(corpus_authors),
            client,
            matcher,
            cache=cache,
            search_missing=search_missing,
            progress=progress,
        )
        authors = matching.fetch_missing_authors(client, [c.author_id for c in candidates], cache)
        orcid_names = matching.fetch_orcid_names(
            matching.mismatched_orcids(roster, candidates, authors, corpus_authors),
            cache,
            online=client is not None,
        )
    finally:
        cache.save()
    scored = matching.score_candidates(
        roster, candidates, authors, corpus_authors, config.institution.id, orcid_names
    )
    matches = matching.apply_verdicts(scored, matching.read_verdicts(config.matches_review))
    matching.write_review(matches, config.matches_review)
    consolidated = matching.consolidate(roster, matches)
    consolidated.to_csv(people_path(config), index=False, encoding="utf-8-sig")
    return matches, consolidated


def load_consolidated(config: Config) -> pd.DataFrame:
    """The consolidated roster written by :func:`match_step`."""
    path = people_path(config)
    if not path.exists():
        raise FileNotFoundError("Run the matching step first (step 3).")
    return pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")


def harvest_people_step(
    config: Config, client: OpenAlexClient, refresh: bool = False, progress: Progress = print
) -> HarvestStats:
    """All works in the period of every accepted profile."""
    author_ids = matching.accepted_author_ids(load_consolidated(config))
    return harvest_people(
        client,
        author_ids,
        config.from_date,
        config.to_date,
        WorksStore(config.people_store),
        Manifest(config.manifest_path),
        refresh=refresh,
        progress=progress,
    )


def tables_step(config: Config, progress: Progress = print) -> dict[str, pd.DataFrame]:
    """Flatten the raw stores into parquet tables and an Excel workbook."""
    consolidated = load_consolidated(config) if people_path(config).exists() else None
    progress("Building tables…")
    tables = build_tables(config, consolidated)
    write_tables(tables, config.tables_dir)
    name = f"{config.institution.id}_{config.from_date[:4]}-{config.to_date[:4]}_publications.xlsx"
    path = export_excel(tables, config.exports_dir / name)
    progress(f"Tables written to {config.tables_dir}; Excel export: {path}")
    return tables


def set_verdicts(config: Config, verdicts: dict[tuple[str, str], str]) -> pd.DataFrame:
    """Write verdicts into the review file without opening a spreadsheet.

    Args:
        verdicts: ``{(person_id, author_id): "ok" | "wrong" | ""}``.

    Re-run :func:`match_step` afterwards (``client=None`` is enough) to apply them.
    """
    review = pd.read_csv(config.matches_review, dtype=str, encoding="utf-8-sig").fillna("")
    keys = set(zip(review.person_id, review.author_id, strict=True))
    for (person, author), verdict in verdicts.items():
        if verdict not in matching.VERDICTS:
            raise ValueError(f"Verdict for {person}/{author} must be 'ok', 'wrong' or ''.")
        if (person, author) not in keys:
            raise KeyError(f"No candidate {author} for {person} in {config.matches_review}.")
        mask = (review.person_id == person) & (review.author_id == author)
        review.loc[mask, "verdict"] = verdict
    review.to_csv(config.matches_review, index=False, encoding="utf-8-sig")
    return review


def new_config(
    mailto: str,
    institution_record: dict,
    roster_path: str,
    from_date: str,
    to_date: str,
    root: Path,
    sheets: list[str] | None = None,
) -> Config:
    """A config for an institution record returned by ``find_institutions``.

    Hand-edited roster settings (``columns``, ``lab_aliases``) of an existing
    ``config.yaml`` are kept when the roster file is the same.
    """
    roster = RosterSettings(path=roster_path.strip(), sheets=list(sheets or []))
    try:
        previous = Config.load(Path(root) / "config.yaml")
    except ConfigError:
        previous = None
    if previous and previous.roster.path == roster.path:
        roster.columns = previous.roster.columns
        roster.lab_aliases = previous.roster.lab_aliases
    return Config(
        mailto=mailto.strip(),
        institution=InstitutionSettings(
            id=short_id(institution_record["id"]) or "",
            name=institution_record.get("display_name", ""),
            ror=institution_record.get("ror") or "",
            affiliation_pattern=build_affiliation_pattern(institution_record),
        ),
        roster=roster,
        from_date=from_date,
        to_date=to_date,
        root=Path(root),
    )


def lists_dir(config: Config) -> Path:
    return config.data_path / "lists"


def lists_step(
    config: Config, client: OpenAlexClient | None, progress: Progress = print
) -> dict[str, pd.DataFrame]:
    """The two publication lists with every OpenAlex field, the venues, the indicators.

    Writes ``data/lists/`` (``list1_institution``, ``list2_roster``, ``venues``, as
    parquet and xlsx) and ``data/lists/indicators.xlsx``. Needs :func:`tables_step`.
    Pass ``client=None`` to use cached venue records only.
    """
    from . import analysis, lists, venues

    works = pd.read_parquet(config.tables_dir / "works.parquet")
    progress("Building the two lists with all OpenAlex fields…")
    publication_lists = lists.build_lists(config, works)
    sources = venues.fetch_sources(
        client,
        works.loc[works.in_list_institution, "venue_id"].dropna(),
        WorksStore(config.raw_dir / "sources.jsonl.gz"),
        progress,
    )
    venue_table = venues.flag_top(venues.venue_table(sources, works))
    world_store = WorksStore(config.raw_dir / "world_journals.jsonl.gz")
    if client is not None:
        world = venues.fetch_world_journals(
            client,
            world_store,
            Manifest(config.manifest_path),
            active_since=int(config.to_date[:4]) - 1,
            progress=progress,
        )
    else:
        world = pd.DataFrame(list(world_store.iter_records()))
    if not world.empty:
        venue_table = venues.world_percentiles(venue_table, world)
    venue_table = venues.ordered(venue_table)
    authorships = pd.read_parquet(config.tables_dir / "authorships.parquet")
    report = analysis.indicator_report(works, venue_table, authorships)

    out = lists_dir(config)
    lists.write_lists(publication_lists, out)
    venue_table.to_parquet(out / "venues.parquet", index=False)
    excel_safe(venue_table).to_excel(out / "venues.xlsx", index=False)
    with pd.ExcelWriter(out / "indicators.xlsx", engine="openpyxl") as writer:
        report["top_cited"].to_excel(writer, sheet_name="top_cited")
        report["top_venues"].to_excel(writer, sheet_name="top_venues")
        venues.thresholds(venue_table).to_frame().to_excel(writer, sheet_name="venue_thresholds")
        for name in ("top1_papers_list1", "top1_papers_list2"):
            if name in report:
                excel_safe(report[name]).to_excel(writer, sheet_name=name, index=False)
    for name, frame in publication_lists.items():
        progress(f"{name}: {len(frame):,} works, {frame.shape[1]} columns")
    progress(f"{venue_table.attrs['n_ranked']} journals ranked; written to {out}")
    return {**publication_lists, "venues": venue_table, **report}
