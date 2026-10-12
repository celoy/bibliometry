"""Resolve the target institution and decide whether an authorship belongs to it.

Two independent signals mark an authorship as the institution's:

1. **id** — one of the authorship's resolved institutions is the target, or has it in
   its ``lineage`` (a child unit);
2. **raw** — OpenAlex resolved nothing useful, but the raw affiliation string names the
   institution. About a fifth of authorships carry no resolved institution at all, and a
   few list partner institutions while the raw string names ours outright.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .openalex import OpenAlexClient, short_id

INSTITUTION_FIELDS = (
    "id,display_name,display_name_alternatives,display_name_acronyms,ror,country_code,"
    "type,works_count,lineage,geo"
)


def fold(text: str) -> str:
    """Lower-case and strip accents: ``"Université d’Été"`` -> ``"universite d'ete"``."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.casefold().replace("’", "'")


def normalise_institution_query(query: str) -> str:
    """Turn a URL, ROR or OpenAlex id into the path segment ``/institutions/{…}`` accepts.

    Returns an empty string when ``query`` is free text that needs a search instead.
    """
    query = query.strip()
    match = re.search(r"\b(I\d{4,})\b", query)
    if match:
        return match.group(1)
    match = re.search(r"(?:ror\.org/|^)(0[a-z0-9]{6}\d{2})$", query)
    if match:
        return f"ror:{match.group(1)}"
    return ""


def find_institutions(client: OpenAlexClient, query: str, limit: int = 8) -> list[dict[str, Any]]:
    """Candidate institutions for a name, OpenAlex id, ROR id or URL."""
    key = normalise_institution_query(query)
    if key:
        return [client.get(f"/institutions/{key}", {"select": INSTITUTION_FIELDS})]
    return client.search("/institutions", query, select=INSTITUTION_FIELDS, per_page=limit)


def describe_institution(record: dict[str, Any]) -> str:
    """One line for a candidate picker."""
    city = ((record.get("geo") or {}).get("city")) or ""
    alternatives = ", ".join((record.get("display_name_alternatives") or [])[:4])
    return (
        f"{record.get('display_name')} — {short_id(record.get('id'))}, "
        f"{city} {record.get('country_code') or ''}, {record.get('works_count', 0):,} works"
        + (f" (also: {alternatives})" if alternatives else "")
    )


def build_affiliation_pattern(record: dict[str, Any]) -> str:
    """Regex for raw affiliation strings naming this institution.

    Every name variant OpenAlex knows, folded, with flexible whitespace and a word
    boundary at the end, plus acronyms of three letters or more as whole words.

    The pattern can be edited in ``config.yaml`` (``institution.affiliation_pattern``),
    e.g. to add a local abbreviation. Two pitfalls when doing so: a generic stem
    ("institute of technology", "state university") also matches sister institutions in
    other cities, and a short acronym can match unrelated labs. Check the raw strings
    it matches (``authorships.raw_affiliations`` where ``institution_signal == "raw"``).
    """
    names = {record.get("display_name") or "", *(record.get("display_name_alternatives") or [])}
    parts = sorted(
        {r"\s*".join(map(re.escape, fold(name).split())) + r"\b" for name in names if name.strip()},
        key=len,
        reverse=True,
    )
    acronyms = sorted(
        {fold(a) for a in record.get("display_name_acronyms") or [] if len(a.strip()) >= 3}
    )
    parts += [rf"\b{re.escape(a)}\b" for a in acronyms]
    return "|".join(parts)


@dataclass
class AffiliationMatcher:
    """Decides whether an authorship carries the target institution."""

    institution_id: str
    pattern: str = ""

    def __post_init__(self) -> None:
        self._regex = re.compile(self.pattern) if self.pattern else None

    def institution_match(self, institutions: Iterable[dict[str, Any]]) -> bool:
        for inst in institutions or []:
            if short_id(inst.get("id")) == self.institution_id:
                return True
            if any(short_id(x) == self.institution_id for x in inst.get("lineage") or []):
                return True
        return False

    def raw_match(self, strings: Iterable[str]) -> bool:
        if self._regex is None:
            return False
        return any(self._regex.search(fold(s or "")) for s in strings or [])

    def authorship_signal(self, authorship: dict[str, Any]) -> str:
        """``"id"``, ``"raw"`` or ``""`` for one authorship of a work."""
        if self.institution_match(authorship.get("institutions") or []):
            return "id"
        if self.raw_match(authorship.get("raw_affiliation_strings") or []):
            return "raw"
        return ""
