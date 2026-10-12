"""Consolidate the roster: map each person to their OpenAlex author profile(s).

Names are ambiguous and OpenAlex splits some people across several profiles, so every
person gets a list of *candidate* profiles, each with evidence and a confidence level.
Candidates come from four sources, cheapest first:

1. ``roster`` — an OpenAlex id written in the roster file;
2. ``orcid`` — the roster ORCID, looked up 50 at a time;
3. ``corpus`` — authors in the institution corpus whose name is compatible with the
   person's *and* whose own authorship carries the institution. This is free (offline)
   and is what catches split profiles;
4. ``search`` — an OpenAlex name search restricted to authors once affiliated with the
   institution. Ten times the price of a list request, so only for people still
   without a candidate.

Confidence:

* **high** — ORCID agrees, or the id came from the roster, or a full-name match with at
  least ``MIN_INST_WORKS_HIGH`` works carrying the institution in the period, no ORCID
  conflict and no other roster person compatible with that profile;
* **medium** — compatible name (initial at least) with some link to the institution;
* **low** — everything else (including an ORCID conflict).

High-confidence candidates are accepted automatically. Everything is written to
``author_matches.csv`` with a ``verdict`` column: ``ok`` accepts, ``wrong`` rejects,
blank leaves the automatic decision. Verdicts survive re-runs.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

from .institution import AffiliationMatcher
from .openalex import OpenAlexClient, batched, short_id
from .roster import name_key

AUTHOR_FIELDS = (
    "id,display_name,display_name_alternatives,orcid,works_count,cited_by_count,"
    "last_known_institutions,affiliations,topics"
)
#: Works carrying the institution needed for a name-only match to count as high. The
#: name may be an initial ("J. Smith") or one part of a compound surname
#: ("María García" for GARCÍA LÓPEZ): the institution's signature is what decides.
#: Checked against a hand-curated list on one institution (~100 people, five years):
#: every full-name match with one such work and no ORCID conflict was the right person.
MIN_INST_WORKS_HIGH = 1
#: Name score of a first-name match on the initial only.
INITIAL_SCORE = 0.7
#: ``conflated``: the profile merges this person with somebody else. It is harvested,
#: but a work counts for the person only where this profile signed with the institution.
VERDICTS = {"ok", "wrong", "conflated", ""}

CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}
#: Surname particles that never identify anyone on their own.
PARTICLES = frozenset(
    "le la les de du des di da del della van von der den ten el al ben bin dos das".split()
)


# ---------------------------------------------------------------------------
# Name comparison
# ---------------------------------------------------------------------------


def name_score(last: str, first: str, candidate: str) -> float:
    """How well ``candidate`` (a display name) fits a person, in ``[0, 1]``.

    * the surname must appear as consecutive tokens (``"martin lefevre"``); a compound
      surname reduced to one of its parts (``"Garcia"`` for ``"GARCIA LOPEZ"``,
      ``"Mercier"`` for ``"DURAND-MERCIER"``) scores 0.8x, but never on a particle alone
      (``"Le"`` from ``"LE GALL"`` would match every Le);
    * the first name must match the first given token in full (1.0) or by initial
      (:data:`INITIAL_SCORE`). An unknown first name scores 0.5.
    """
    tokens = name_key(candidate).split()
    last_tokens = name_key(last).split()
    first_tokens = name_key(first).split()
    if not tokens or not last_tokens:
        return 0.0

    def find(sub: list[str]) -> int:
        for start in range(len(tokens) - len(sub) + 1):
            if tokens[start : start + len(sub)] == sub:
                return start
        return -1

    start = find(last_tokens)
    surname_factor = 1.0
    if start < 0 and len(last_tokens) > 1:
        for part in surname_parts(last):
            start = find([part])
            if start >= 0:
                last_tokens, surname_factor = [part], 0.8
                break
    if start < 0:
        return 0.0
    rest = tokens[:start] + tokens[start + len(last_tokens) :]
    if not first_tokens:
        return 0.5 * surname_factor
    if not rest:
        return 0.0
    given = first_tokens[0]
    if given in rest:
        return surname_factor
    if any(len(t) == 1 and t == given[0] for t in rest):
        return INITIAL_SCORE * surname_factor
    return 0.0


def surname_parts(last: str) -> list[str]:
    """Tokens of a surname that can identify it on their own (no particles, ≥3 letters)."""
    return [t for t in name_key(last).split() if len(t) >= 3 and t not in PARTICLES]


# ---------------------------------------------------------------------------
# Corpus index
# ---------------------------------------------------------------------------


@dataclass
class CorpusAuthor:
    """What the institution corpus says about one OpenAlex author."""

    author_id: str
    names: Counter[str] = field(default_factory=Counter)
    orcid: str = ""
    n_works: int = 0
    n_inst_works: int = 0  # works where this author's own authorship carries the institution
    titles: list[str] = field(default_factory=list)


def index_corpus_authors(
    records: Iterable[dict[str, Any]], matcher: AffiliationMatcher
) -> dict[str, CorpusAuthor]:
    """One pass over the institution corpus, aggregated per author id."""
    authors: dict[str, CorpusAuthor] = {}
    for work in records:
        for authorship in work.get("authorships") or []:
            author = authorship.get("author") or {}
            author_id = short_id(author.get("id"))
            if not author_id:
                continue
            entry = authors.setdefault(author_id, CorpusAuthor(author_id))
            for name in (author.get("display_name"), authorship.get("raw_author_name")):
                if name:
                    entry.names[name] += 1
            entry.orcid = entry.orcid or orcid_of(author.get("orcid"))
            entry.n_works += 1
            if matcher.authorship_signal(authorship):
                entry.n_inst_works += 1
                if len(entry.titles) < 3 and work.get("title"):
                    entry.titles.append(work["title"])
    return authors


def orcid_of(url: str | None) -> str:
    return (url or "").rstrip("/").split("/")[-1].upper() if url else ""


class SurnameIndex:
    """Corpus authors indexed by every token of their names, for quick lookup."""

    def __init__(self, authors: dict[str, CorpusAuthor]) -> None:
        self.authors = authors
        self._by_token: dict[str, set[str]] = defaultdict(set)
        for author_id, entry in authors.items():
            for name in entry.names:
                for token in name_key(name).split():
                    if len(token) > 1:
                        self._by_token[token].add(author_id)

    def compatible(self, last: str, first: str) -> dict[str, float]:
        """Author ids whose best name scores > 0 for this person, with that score."""
        tokens = surname_parts(last) or name_key(last).split()
        pool = set().union(*(self._by_token.get(t, set()) for t in tokens)) if tokens else set()
        hits: dict[str, float] = {}
        for author_id in pool:
            best = max(name_score(last, first, n) for n in self.authors[author_id].names)
            if best > 0:
                hits[author_id] = best
        return hits


# ---------------------------------------------------------------------------
# Candidate gathering
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    person_id: str
    author_id: str
    sources: set[str] = field(default_factory=set)


def _lookup_orcids(client: OpenAlexClient, orcids: list[str]) -> dict[str, list[str]]:
    """ORCID -> OpenAlex author ids (several when OpenAlex split the person)."""
    found: dict[str, list[str]] = defaultdict(list)
    for chunk in batched(orcids):
        for page in client.iter_pages("/authors", f"orcid:{'|'.join(chunk)}", select="id,orcid"):
            for author in page:
                found[orcid_of(author.get("orcid"))].append(short_id(author["id"]))
    return found


def fetch_authors(client: OpenAlexClient, author_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Full-ish author records, 50 per request."""
    records: dict[str, dict[str, Any]] = {}
    for chunk in batched(sorted(set(author_ids))):
        for page in client.iter_pages(
            "/authors", f"ids.openalex:{'|'.join(chunk)}", select=AUTHOR_FIELDS
        ):
            for author in page:
                records[short_id(author["id"])] = author
    return records


class LookupCache:
    """API answers kept between runs, so re-running the matching step is free.

    Re-running is the normal workflow (edit verdicts, run again), and the name searches
    are the expensive part of the budget. Only new ORCIDs, new people and new author ids
    trigger requests. Stored as JSON in the review directory.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else None
        data: dict[str, Any] = {}
        if self.path and self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
        self.orcids: dict[str, list[str]] = data.get("orcids", {})
        self.searches: dict[str, list[dict[str, Any]]] = data.get("searches", {})
        self.authors: dict[str, dict[str, Any]] = data.get("authors", {})
        self.orcid_names: dict[str, str] = data.get("orcid_names", {})

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "orcids": self.orcids,
            "searches": self.searches,
            "authors": self.authors,
            "orcid_names": self.orcid_names,
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def gather_candidates(
    roster: pd.DataFrame,
    corpus_index: SurnameIndex,
    client: OpenAlexClient | None,
    matcher: AffiliationMatcher,
    cache: LookupCache | None = None,
    search_missing: bool = True,
    progress: Callable[[str], None] | None = None,
) -> list[Candidate]:
    """Collect candidate profiles for every person from the four sources.

    With ``client=None`` only the offline sources (roster ids, corpus, cached answers)
    are used.
    """
    say = progress or (lambda _msg: None)
    cache = cache or LookupCache()
    candidates: dict[tuple[str, str], Candidate] = {}

    def add(pid: str, aid: str, source: str) -> None:
        candidates.setdefault((pid, aid), Candidate(pid, aid)).sources.add(source)

    for row in roster.itertuples():
        for aid in filter(None, str(row.openalex_ids).split(";")):
            add(row.person_id, aid, "roster")

    new_orcids = sorted({o for o in roster.orcid if o} - set(cache.orcids))
    if client is not None and new_orcids:
        say(f"Looking up {len(new_orcids)} ORCIDs…")
        found = _lookup_orcids(client, new_orcids)
        for orcid in new_orcids:
            cache.orcids[orcid] = found.get(orcid, [])
    for row in roster.itertuples():
        for aid in cache.orcids.get(row.orcid, []) if row.orcid else []:
            add(row.person_id, aid, "orcid")

    for row in roster.itertuples():
        for aid in corpus_index.compatible(row.last_name, row.first_name):
            if corpus_index.authors[aid].n_inst_works > 0:
                add(row.person_id, aid, "corpus")

    have = {pid for pid, _ in candidates}
    missing = roster[~roster.person_id.isin(have)]
    to_search = missing[~missing.person_id.isin(set(cache.searches))]
    if client is not None and search_missing and len(to_search):
        say(f"Searching OpenAlex by name for {len(to_search)} people without a candidate…")
        for row in to_search.itertuples():
            text = f"{row.first_name} {row.last_name}".strip()
            cache.searches[row.person_id] = client.search(
                "/authors",
                text,
                filter_string=f"affiliations.institution.id:{matcher.institution_id}",
                select="id,display_name,display_name_alternatives",
            )
    for row in missing.itertuples():
        for author in cache.searches.get(row.person_id, []):
            names = [author.get("display_name") or ""] + list(
                author.get("display_name_alternatives") or []
            )
            if max(name_score(row.last_name, row.first_name, n) for n in names) > 0:
                add(row.person_id, short_id(author["id"]), "search")
    return list(candidates.values())


def fetch_missing_authors(
    client: OpenAlexClient | None, author_ids: Iterable[str], cache: LookupCache
) -> dict[str, dict[str, Any]]:
    """Author records for ``author_ids``, fetching only those not cached yet."""
    missing = sorted(set(author_ids) - set(cache.authors))
    if client is not None and missing:
        cache.authors.update(fetch_authors(client, missing))
    return cache.authors


ORCID_PUBLIC_API = "https://pub.orcid.org/v3.0"


def same_orcid_holder(first: str, second: str, orcid_names: dict[str, str]) -> bool:
    """True when two ORCID records carry the same name (order and accents ignored)."""
    a, b = orcid_names.get(first, ""), orcid_names.get(second, "")
    return bool(a and b) and sorted(name_key(a).split()) == sorted(name_key(b).split())


def fetch_orcid_names(
    orcids: Iterable[str], cache: LookupCache, online: bool = True
) -> dict[str, str]:
    """Names on the public ORCID registry, for settling ORCID mismatches. Cached."""
    missing = sorted(set(orcids) - set(cache.orcid_names))
    if online and missing:
        with httpx.Client(timeout=30, headers={"Accept": "application/json"}) as client:
            for orcid in missing:
                try:
                    response = client.get(f"{ORCID_PUBLIC_API}/{orcid}/person")
                    response.raise_for_status()
                    name = response.json().get("name") or {}
                except (httpx.HTTPError, ValueError):
                    continue  # unknown stays unknown; the mismatch remains a conflict
                given = (name.get("given-names") or {}).get("value") or ""
                family = (name.get("family-name") or {}).get("value") or ""
                cache.orcid_names[orcid] = f"{given} {family}".strip()
    return cache.orcid_names


def mismatched_orcids(
    roster: pd.DataFrame,
    candidates: list[Candidate],
    authors: dict[str, dict[str, Any]],
    corpus_authors: dict[str, CorpusAuthor] | None = None,
) -> set[str]:
    """Both ORCIDs of every (person, candidate) pair whose ORCIDs differ.

    The candidate's ORCID is read as in :func:`score_candidates`: author record first,
    then the institution corpus.
    """
    corpus_authors = corpus_authors or {}
    orcid_by_person = dict(zip(roster.person_id, roster.orcid, strict=True))
    out: set[str] = set()
    for cand in candidates:
        mine = orcid_by_person.get(cand.person_id, "")
        corpus = corpus_authors.get(cand.author_id)
        theirs = orcid_of((authors.get(cand.author_id) or {}).get("orcid")) or (
            corpus.orcid if corpus else ""
        )
        if mine and theirs and mine != theirs:
            out |= {mine, theirs}
    return out


# ---------------------------------------------------------------------------
# Evidence and confidence
# ---------------------------------------------------------------------------


def _top_topics(author: dict[str, Any], n: int = 3) -> str:
    return "; ".join(t.get("display_name", "") for t in (author.get("topics") or [])[:n])


def _last_institution(author: dict[str, Any]) -> str:
    insts = author.get("last_known_institutions") or []
    return "; ".join(i.get("display_name", "") for i in insts[:2])


def _ever_affiliated(author: dict[str, Any], institution_id: str) -> bool:
    for aff in author.get("affiliations") or []:
        inst = aff.get("institution") or {}
        if short_id(inst.get("id")) == institution_id or any(
            short_id(x) == institution_id for x in inst.get("lineage") or []
        ):
            return True
    return False


def score_candidates(
    roster: pd.DataFrame,
    candidates: list[Candidate],
    authors: dict[str, dict[str, Any]],
    corpus_authors: dict[str, CorpusAuthor],
    institution_id: str,
    orcid_names: dict[str, str] | None = None,
) -> pd.DataFrame:
    """One row per (person, candidate) with evidence, confidence and auto decision.

    ``orcid_names`` (ORCID -> name on orcid.org) settles ORCID mismatches: some people
    hold two ORCID records, and when both carry the same name the mismatch is
    reported as ``"match (two ORCIDs)"`` instead of ``"conflict"``.
    """
    orcid_names = orcid_names or {}
    people = roster.set_index("person_id")
    claimed_by: dict[str, set[str]] = defaultdict(set)  # author -> compatible people
    rows = []
    for cand in candidates:
        person = people.loc[cand.person_id]
        author = authors.get(cand.author_id, {})
        corpus = corpus_authors.get(cand.author_id, CorpusAuthor(cand.author_id))
        names = (
            [author.get("display_name") or ""]
            + list(author.get("display_name_alternatives") or [])
            + list(corpus.names)
        )
        score = max(
            (name_score(person.last_name, person.first_name, n) for n in names if n), default=0.0
        )
        if score > 0:
            claimed_by[cand.author_id].add(cand.person_id)
        cand_orcid = orcid_of(author.get("orcid")) or corpus.orcid
        if person.orcid and cand_orcid:
            if person.orcid == cand_orcid:
                orcid_status = "match"
            elif same_orcid_holder(person.orcid, cand_orcid, orcid_names):
                orcid_status = "match (two ORCIDs)"
            else:
                orcid_status = "conflict"
        else:
            orcid_status = "unknown"
        rows.append(
            {
                "person_id": cand.person_id,
                "last_name": person.last_name,
                "first_name": person.first_name,
                "lab": person.lab,
                "author_id": cand.author_id,
                "author_name": author.get("display_name")
                or next(iter(corpus.names.most_common(1)), ("",))[0],
                "sources": "+".join(sorted(cand.sources)),
                "name_score": round(score, 2),
                "orcid_roster": person.orcid,
                "orcid_openalex": cand_orcid,
                "orcid_status": orcid_status,
                "works_in_period_with_institution": corpus.n_inst_works,
                "works_total": author.get("works_count", ""),
                "ever_affiliated": _ever_affiliated(author, institution_id),
                "last_known_institution": _last_institution(author),
                "top_topics": _top_topics(author),
                "sample_titles": " | ".join(corpus.titles[:2]),
                "openalex_url": f"https://openalex.org/{cand.author_id}",
            }
        )
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame

    def confidence(row: pd.Series) -> str:
        if row.orcid_status == "conflict":
            return "low"
        if row.orcid_status.startswith("match") or "roster" in row.sources:
            return "high"
        shared = len(claimed_by.get(row.author_id, ())) > 1
        if (
            row.name_score >= INITIAL_SCORE
            and row.works_in_period_with_institution >= MIN_INST_WORKS_HIGH
            and not shared
        ):
            return "high"
        if row.name_score >= INITIAL_SCORE * 0.8 and (
            row.works_in_period_with_institution > 0 or row.ever_affiliated
        ):
            return "medium"
        return "low"

    frame["confidence"] = frame.apply(confidence, axis=1)
    frame["shared_with"] = frame.apply(
        lambda r: "; ".join(sorted(claimed_by.get(r.author_id, set()) - {r.person_id})), axis=1
    )
    frame["auto_accept"] = frame.confidence == "high"
    frame["_rank"] = frame.confidence.map(CONFIDENCE_ORDER)
    frame = frame.sort_values(
        ["last_name", "first_name", "_rank", "works_in_period_with_institution"],
        ascending=[True, True, True, False],
    ).drop(columns="_rank")
    return frame.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Review file
# ---------------------------------------------------------------------------


def read_verdicts(path: Path) -> dict[tuple[str, str], tuple[str, str]]:
    """(person_id, author_id) -> (verdict, comment) from an existing review file."""
    if not path.exists():
        return {}
    previous = pd.read_csv(path, dtype=str).fillna("")
    bad = sorted(
        set(previous.get("verdict", pd.Series(dtype=str)).str.strip().str.lower()) - VERDICTS
    )
    if bad:
        raise ValueError(f"{path}: unknown verdict(s) {bad}; use 'ok', 'wrong' or leave blank.")
    return {
        (r.person_id, r.author_id): (r.verdict.strip().lower(), r.get("comment", ""))
        for _, r in previous.iterrows()
    }


def apply_verdicts(
    frame: pd.DataFrame, verdicts: dict[tuple[str, str], tuple[str, str]]
) -> pd.DataFrame:
    """Attach preserved verdicts and compute the final ``accepted`` column."""
    frame = frame.copy()
    pairs = list(zip(frame.person_id, frame.author_id, strict=True))
    frame["verdict"] = [verdicts.get(p, ("", ""))[0] for p in pairs]
    frame["comment"] = [verdicts.get(p, ("", ""))[1] for p in pairs]
    frame["accepted"] = frame.verdict.isin({"ok", "conflated"}) | (
        (frame.verdict == "") & frame.auto_accept
    )
    return frame


def write_review(frame: pd.DataFrame, path: Path) -> None:
    """Write the review CSV (UTF-8 with BOM, so Excel shows accents correctly)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = ["verdict", "comment", "accepted", "confidence", "auto_accept"]
    ordered = columns + [c for c in frame.columns if c not in columns]
    frame[ordered].to_csv(path, index=False, encoding="utf-8-sig")


def consolidate(roster: pd.DataFrame, matches: pd.DataFrame) -> pd.DataFrame:
    """The roster with its accepted author ids and a per-person status."""
    accepted = (
        matches[matches.accepted].groupby("person_id")["author_id"].agg(lambda s: ";".join(s))
        if not matches.empty
        else pd.Series(dtype=str)
    )
    pending = (
        matches[~matches.accepted & (matches.verdict == "")].groupby("person_id").size()
        if not matches.empty
        else pd.Series(dtype=int)
    )
    conflated = (
        matches[matches.verdict == "conflated"]
        .groupby("person_id")["author_id"]
        .agg(lambda s: ";".join(s))
        if not matches.empty
        else pd.Series(dtype=str)
    )
    out = roster.copy()
    out["author_ids"] = out.person_id.map(accepted).fillna("")
    out["conflated_ids"] = out.person_id.map(conflated).fillna("")
    out["n_pending"] = out.person_id.map(pending).fillna(0).astype(int)

    def status(row: pd.Series) -> str:
        if row.author_ids and row.n_pending:
            return "matched (more to review)"
        if row.author_ids:
            return "matched"
        if row.n_pending:
            return "to review"
        return "not found"

    out["match_status"] = out.apply(status, axis=1)
    return out


def accepted_author_ids(consolidated: pd.DataFrame) -> list[str]:
    ids = {a for cell in consolidated.author_ids for a in str(cell).split(";") if a}
    return sorted(ids)
