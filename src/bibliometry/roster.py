"""Read a list of people (CSV or Excel, local or URL) into one tidy roster table.

Real rosters are messy: several sheets with different columns, French or English
headers, an e-mail column with no header, ORCIDs written as URLs, trailing spaces in
names. This module detects what it can and reports what it could not, so the user can
fix the file or name the columns in ``config.yaml`` (``roster.columns``).

Output columns, in order: ``person_id, last_name, first_name, orcid, openalex_ids, lab,
email, sheet, source_row``, then every other column of the source file unchanged.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

import httpx
import pandas as pd

from .institution import fold

CANONICAL = ("last_name", "first_name", "full_name", "orcid", "openalex_id", "lab", "email")

#: Folded header text -> canonical column. Matching is exact after folding and removing
#: punctuation, so "Unité thématique" never passes for the lab column.
HEADER_ALIASES: dict[str, str] = {
    **dict.fromkeys(
        [
            "nom",
            "nom de famille",
            "nom d usage",
            "last name",
            "lastname",
            "surname",
            "family name",
            "familyname",
            "last",
        ],
        "last_name",
    ),
    **dict.fromkeys(
        [
            "prenom",
            "prenoms",
            "first name",
            "firstname",
            "given name",
            "given names",
            "forename",
            "first",
        ],
        "first_name",
    ),
    **dict.fromkeys(
        [
            "name",
            "full name",
            "fullname",
            "nom complet",
            "nom prenom",
            "prenom nom",
            "author",
            "auteur",
            "person",
            "personne",
        ],
        "full_name",
    ),
    **dict.fromkeys(["orcid", "orcid id", "orcid fichier", "id orcid"], "orcid"),
    **dict.fromkeys(
        ["openalex", "openalex id", "id openalex", "openalex ids", "openalex author id"],
        "openalex_id",
    ),
    **dict.fromkeys(
        [
            "laboratoire",
            "labo",
            "lab",
            "laboratory",
            "unit",
            "unite",
            "unite de recherche",
            "research unit",
            "structure",
            "affectation",
        ],
        "lab",
    ),
    **dict.fromkeys(
        ["email", "e mail", "mail", "courriel", "adresse mail", "adresse email"], "email"
    ),
}

ORCID_RE = re.compile(r"(\d{4}-\d{4}-\d{4}-\d{3}[\dX])", re.IGNORECASE)
OPENALEX_AUTHOR_RE = re.compile(r"\b(A\d{6,})\b")
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
GOOGLE_SHEET_RE = re.compile(r"docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]+)")
#: How many leading rows to scan for the header row.
HEADER_SCAN_ROWS = 10


class RosterError(ValueError):
    """The roster could not be read or interpreted. The message says what to do."""


# ---------------------------------------------------------------------------
# Cleaning helpers
# ---------------------------------------------------------------------------


def clean_text(value: object) -> str:
    """Cell -> stripped string; NaN/None -> ``""``; collapses internal whitespace."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalise_orcid(value: object) -> str:
    """Extract a bare ORCID (``0000-0002-1825-0097``) from a URL or messy cell."""
    match = ORCID_RE.search(clean_text(value))
    return match.group(1).upper() if match else ""


def extract_openalex_ids(value: object) -> list[str]:
    """All OpenAlex author ids (``A…``) in a cell, e.g. ``"A123; A456"``."""
    return OPENALEX_AUTHOR_RE.findall(clean_text(value))


def name_key(text: str) -> str:
    """Folded, punctuation-free form used to compare names: ``"Martin-Lefèvre"`` -> ``"martin lefevre"``."""
    return re.sub(r"[^a-z0-9]+", " ", fold(text)).strip()


def slug(text: str) -> str:
    return name_key(text).replace(" ", "-")


def person_id(last_name: str, first_name: str) -> str:
    return f"{slug(last_name)}--{slug(first_name)}"


def split_full_name(full: str) -> tuple[str, str]:
    """Split one name cell into (last, first).

    Handles ``"Last, First"``, ``"LAST First"`` (French administrative style: the
    upper-case tokens are the surname) and ``"First Last"`` (last token is the surname).
    """
    full = clean_text(full)
    if "," in full:
        last, _, first = full.partition(",")
        return last.strip(), first.strip()
    tokens = full.split()
    upper = [t for t in tokens if t.isupper() and len(t) > 1]
    if upper and len(upper) < len(tokens):
        return " ".join(upper), " ".join(t for t in tokens if t not in upper)
    if len(tokens) == 1:
        return tokens[0], ""
    return tokens[-1], " ".join(tokens[:-1])


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _download(url: str) -> tuple[bytes, str]:
    """Fetch a roster URL. Google Sheets share links are rewritten to an xlsx export."""
    match = GOOGLE_SHEET_RE.search(url)
    if match:
        url = f"https://docs.google.com/spreadsheets/d/{match.group(1)}/export?format=xlsx"
    try:
        response = httpx.get(url, follow_redirects=True, timeout=60)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise RosterError(
            f"Could not download the roster from {url}: {exc}. If it is a shared "
            "document, make sure the link is readable by anyone who has it."
        ) from exc
    content = response.content
    kind = "xlsx" if content[:2] == b"PK" else "csv"
    return content, kind


def read_raw_tables(source: str, sheets: list[str] | None = None) -> dict[str, pd.DataFrame]:
    """Read every requested sheet as a header-less table of strings, keyed by sheet name."""
    if re.match(r"^https?://", source):
        content, kind = _download(source)
        handle: io.BytesIO | Path = io.BytesIO(content)
    else:
        path = Path(source)
        if not path.exists():
            raise RosterError(f"Roster file not found: {path}")
        handle = path
        kind = "csv" if path.suffix.lower() in {".csv", ".tsv", ".txt"} else "xlsx"

    if kind == "csv":
        raw = handle.read_bytes() if isinstance(handle, Path) else handle.getvalue()
        for encoding in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        frame = pd.read_csv(io.StringIO(text), sep=None, engine="python", header=None, dtype=str)
        return {"csv": frame}

    try:
        book = pd.read_excel(handle, sheet_name=None, header=None, dtype=object)
    except Exception as exc:  # noqa: BLE001 - surface any reader failure with context
        raise RosterError(f"Could not read {source} as an Excel workbook: {exc}") from exc
    if sheets:
        missing = [s for s in sheets if s not in book]
        if missing:
            raise RosterError(f"Sheet(s) {missing} not in the workbook. Available: {list(book)}")
        book = {name: book[name] for name in sheets}
    return book


def _header_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", fold(clean_text(value))).strip()


def find_header_row(frame: pd.DataFrame) -> int:
    """Index of the first row that looks like a header (≥2 recognised column names)."""
    for index in range(min(HEADER_SCAN_ROWS, len(frame))):
        keys = [_header_key(v) for v in frame.iloc[index].tolist()]
        if sum(k in HEADER_ALIASES for k in keys) >= 2:
            return index
    return 0


def detect_columns(frame: pd.DataFrame, overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Map canonical names to the frame's column labels.

    Header aliases first, then content sniffing for the two columns whose values are
    unmistakable (e-mails, ORCIDs) — the example roster's e-mail column has no header.
    ``overrides`` ({canonical: header}) win over everything.
    """
    mapping: dict[str, str] = {}
    for column in frame.columns:
        canonical = HEADER_ALIASES.get(_header_key(column))
        if canonical and canonical not in mapping:
            mapping[canonical] = column

    def share(column: str, pattern: re.Pattern[str]) -> float:
        values = [clean_text(v) for v in frame[column] if clean_text(v)]
        return sum(bool(pattern.search(v)) for v in values) / len(values) if values else 0.0

    taken = set(mapping.values())
    for canonical, pattern in (("email", EMAIL_RE), ("orcid", ORCID_RE)):
        if canonical in mapping:
            continue
        for column in frame.columns:
            if column not in taken and share(column, pattern) >= 0.5:
                mapping[canonical] = column
                taken.add(column)
                break

    for canonical, header in (overrides or {}).items():
        if canonical not in CANONICAL:
            raise RosterError(f"roster.columns: unknown key {canonical!r}; use one of {CANONICAL}")
        if header not in frame.columns:
            continue  # this sheet lacks it; other sheets may have it
        mapping[canonical] = header
    return mapping


def _tidy_sheet(raw: pd.DataFrame, sheet: str, overrides: dict[str, str]) -> pd.DataFrame:
    header_row = find_header_row(raw)
    headers = []
    for position, value in enumerate(raw.iloc[header_row].tolist()):
        label = clean_text(value) or f"column_{position + 1}"
        while label in headers:
            label += "_"
        headers.append(label)
    frame = raw.iloc[header_row + 1 :].copy()
    frame.columns = headers
    frame = frame.dropna(how="all")
    if frame.empty:
        return pd.DataFrame()

    mapping = detect_columns(frame, overrides)
    if "last_name" not in mapping and "full_name" not in mapping:
        raise RosterError(
            f"Sheet {sheet!r}: no column for names. Headers found: {headers}. "
            "Rename the column (e.g. 'Nom'/'Last name') or set roster.columns.last_name "
            "in config.yaml."
        )

    def column(name: str) -> pd.Series:
        return (
            frame[mapping[name]].map(clean_text)
            if name in mapping
            else pd.Series("", index=frame.index)
        )

    if "last_name" in mapping:
        last, first = column("last_name"), column("first_name")
    else:
        split = column("full_name").map(split_full_name)
        last, first = split.str[0], split.str[1]

    tidy = pd.DataFrame(
        {
            "last_name": last,
            "first_name": first,
            "orcid": column("orcid").map(normalise_orcid),
            "openalex_ids": column("openalex_id").map(lambda v: ";".join(extract_openalex_ids(v))),
            "lab": column("lab"),
            "email": column("email").map(lambda v: v.lower()),
            "sheet": sheet,
            "source_row": frame.index + 1,  # 1-based spreadsheet row
        }
    )
    used = set(mapping.values())
    extras = frame[[c for c in frame.columns if c not in used]].map(clean_text)
    tidy = pd.concat([tidy, extras], axis=1)
    return tidy[tidy["last_name"] != ""]


def read_roster(
    source: str,
    sheets: list[str] | None = None,
    columns: dict[str, str] | None = None,
    lab_aliases: dict[str, str] | None = None,
) -> pd.DataFrame:
    """Read and tidy a roster file; see the module docstring for the output columns.

    People listed more than once are merged into one row (first non-empty value wins);
    their ids are in ``roster.attrs["merged"]``. ``lab_aliases`` renames labs.
    """
    tables = read_raw_tables(source, sheets)
    parts = [_tidy_sheet(raw, name, columns or {}) for name, raw in tables.items()]
    parts = [p for p in parts if not p.empty]
    if not parts:
        raise RosterError(f"No people found in {source}.")
    roster = pd.concat(parts, ignore_index=True).fillna("")
    roster.insert(
        0,
        "person_id",
        [
            person_id(last, first)
            for last, first in zip(roster.last_name, roster.first_name, strict=True)
        ],
    )

    if lab_aliases:
        roster["lab"] = roster["lab"].replace(lab_aliases)
    duplicated = sorted(set(roster.person_id[roster.person_id.duplicated()]))
    if duplicated:

        def first_non_empty(values: pd.Series) -> str:
            return next((v for v in values if v != ""), "")

        roster = roster.groupby("person_id", sort=False, as_index=False).agg(first_non_empty)
    roster = roster.reset_index(drop=True)
    roster.attrs["merged"] = duplicated
    return roster


def single_person_roster(name: str, orcid: str = "") -> pd.DataFrame:
    """A one-row roster for single-person mode (``"First Last"`` or ``"Last, First"``)."""
    last, first = split_full_name(name)
    if not last:
        raise RosterError("Enter the person's name, e.g. 'Ada Lovelace' or 'Lovelace, Ada'.")
    roster = pd.DataFrame(
        {
            "person_id": [person_id(last, first)],
            "last_name": [last],
            "first_name": [first],
            "orcid": [normalise_orcid(orcid)],
            "openalex_ids": [""],
            "lab": [""],
            "email": [""],
            "sheet": ["single person"],
            "source_row": [1],
        }
    )
    roster.attrs["merged"] = []
    return roster


def summarise_roster(roster: pd.DataFrame) -> pd.DataFrame:
    """Per-sheet counts of people and of the identifiers that make matching reliable."""
    return roster.groupby("sheet", sort=False).agg(
        people=("person_id", "size"),
        with_orcid=("orcid", lambda s: int((s != "").sum())),
        with_openalex_id=("openalex_ids", lambda s: int((s != "").sum())),
        with_lab=("lab", lambda s: int((s != "").sum())),
    )
