"""The two publication lists, with every OpenAlex field.

* **list 1 — institution**: every work in the period where at least one authorship
  carries the institution (by id or raw affiliation);
* **list 2 — roster**: works where a person of the list signed *as a member of the
  institution*. A subset of list 1: works the listed people signed elsewhere are not
  in it.

Each list is the full raw OpenAlex record of every work, flattened: nested objects
become dotted columns (``primary_location.source.display_name``), lists are kept as
JSON text, and the abstract is given as plain text instead of OpenAlex's inverted
index. The scope flags, the people and the venue computed by :mod:`bibliometry.tables`
come first.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from .config import Config
from .openalex import short_id
from .tables import excel_safe, iter_all_works

LISTS = {
    "list1_institution": "in_list_institution",
    "list2_roster": "in_list_roster",
}
#: Columns of the ``works`` table put first in each list.
FLAG_COLUMNS = [
    "work_id",
    "in_list_institution",
    "in_list_roster",
    "roster_people_signed",
    "roster_people",
    "in_institution_corpus",
    "venue_id",
    "venue_name",
    "venue_type",
    "venue_found_in",
    "abstract",
]


def _flatten(record: dict[str, Any]) -> dict[str, Any]:
    """Nested dicts -> dotted keys; lists -> JSON text."""
    out: dict[str, Any] = {}

    def walk(prefix: str, value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                walk(f"{prefix}.{key}" if prefix else key, item)
        elif isinstance(value, list):
            out[prefix] = json.dumps(value, ensure_ascii=False) if value else None
        else:
            out[prefix] = value

    walk("", record)
    return out


def full_records(config: Config, work_ids: set[str]) -> pd.DataFrame:
    """Every OpenAlex field of the given works, one row per work."""
    rows = []
    for record, _stores in iter_all_works(config):
        work_id = short_id(record.get("id"))
        if work_id not in work_ids:
            continue
        record = dict(record)
        record.pop("abstract_inverted_index", None)  # replaced by the plain abstract
        rows.append({"work_id": work_id, **_flatten(record)})
    frame = pd.DataFrame(rows)
    # A field that is null in some records and an object in others yields both "x"
    # (always empty) and "x.y" columns; keep only the dotted ones.
    redundant = [
        c
        for c in frame.columns
        if frame[c].isna().all() and any(other.startswith(f"{c}.") for other in frame.columns)
    ]
    return frame.drop(columns=["id", *redundant], errors="ignore")


def build_lists(
    config: Config, works: pd.DataFrame, names: list[str] | None = None
) -> dict[str, pd.DataFrame]:
    """``{list name: full-record table}`` for the lists in ``names`` (default: both)."""
    out = {}
    flags = works[FLAG_COLUMNS]
    for name, column in LISTS.items():
        if names is not None and name not in names:
            continue
        ids = set(works.loc[works[column], "work_id"])
        full = full_records(config, ids)
        merged = flags[flags.work_id.isin(ids)].merge(full, on="work_id", how="left")
        out[name] = merged.sort_values(["publication_year", "work_id"]).reset_index(drop=True)
    return out


def write_lists(lists: dict[str, pd.DataFrame], directory: Path) -> list[Path]:
    """Each list as parquet (complete) and xlsx (long cells cut at Excel's limit)."""
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, frame in lists.items():
        parquet = directory / f"{name}.parquet"
        frame.to_parquet(parquet, index=False)
        xlsx = directory / f"{name}.xlsx"
        excel_safe(frame).to_excel(xlsx, index=False, sheet_name=name[:31])
        written += [parquet, xlsx]
    return written


def read_lists(directory: Path) -> dict[str, pd.DataFrame]:
    return {
        name: pd.read_parquet(Path(directory) / f"{name}.parquet")
        for name in LISTS
        if (Path(directory) / f"{name}.parquet").exists()
    }
