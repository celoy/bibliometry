from collections import Counter

import pandas as pd
import pytest
from conftest import HOME_PATTERN, INST

from bibliometry import matching as M
from bibliometry.institution import AffiliationMatcher


@pytest.mark.parametrize(
    ("last", "first", "candidate", "expected"),
    [
        ("SOUZA", "Ana", "Ana Souza", 1.0),
        ("SOUZA", "Ana", "A. Souza", M.INITIAL_SCORE),
        ("SOUZA", "Ana", "Alice Souza", 0.0),
        ("MARTIN-LEFEVRE", "Helene", "Hélène Martin-Lefèvre", 1.0),
        ("GARCIA LOPEZ", "Pablo", "Pablo Garcia", 0.8),
        ("DURAND-MERCIER", "Anne", "Anne Mercier", 0.8),
        ("LE GALL", "Yann", "Yves Le Roux", 0.0),  # particle alone never matches
        ("LE GALL", "Yann", "Yann Le Gall", 1.0),
        ("BERNARD", "Jean Philippe", "J.-P. Bernard", M.INITIAL_SCORE),
        ("SOUZA", "", "Ana Souza", 0.5),
        ("SOUZA", "Ana", "Souza", 0.0),
    ],
)
def test_name_score(last, first, candidate, expected):
    assert M.name_score(last, first, candidate) == pytest.approx(expected)


@pytest.fixture
def matcher():
    return AffiliationMatcher(INST, HOME_PATTERN)


@pytest.fixture
def roster():
    return pd.DataFrame(
        {
            "person_id": ["souza--ana", "martin--pierre", "dupont--jeanne"],
            "last_name": ["SOUZA", "MARTIN", "DUPONT"],
            "first_name": ["Ana", "Pierre", "Jeanne"],
            "orcid": ["0000-0002-1825-0097", "", ""],
            "openalex_ids": ["", "", ""],
            "lab": ["WAVES", "NEWLAB", "MECHLAB"],
        }
    )


def test_corpus_index_counts_institution_signatures(corpus, matcher):
    authors = M.index_corpus_authors(corpus, matcher)
    assert authors["A1"].n_inst_works == 1  # resolved id
    assert authors["A2"].n_inst_works == 1  # raw string only
    assert authors["A4"].n_inst_works == 0  # a sister institution
    assert authors["A3"].n_inst_works == 2
    assert authors["A1"].orcid == "0000-0002-1825-0097"


def test_offline_candidates_and_confidence(corpus, matcher, roster):
    authors = M.index_corpus_authors(corpus, matcher)
    candidates = M.gather_candidates(roster, M.SurnameIndex(authors), None, matcher)
    pairs = {(c.person_id, c.author_id) for c in candidates}
    # Alice Souza (A4) is excluded twice over: wrong first name, no signature with the institution.
    assert pairs == {
        ("souza--ana", "A1"),
        ("souza--ana", "A2"),
        ("martin--pierre", "A3"),
    }
    scored = M.score_candidates(roster, candidates, {}, authors, INST).set_index("author_id")
    assert scored.loc["A1", "orcid_status"] == "match"
    assert scored.loc["A1", "confidence"] == "high"
    assert scored.loc["A2", "confidence"] == "high"  # initial only, but signed with the institution
    assert scored.loc["A3", "confidence"] == "high"  # full name + institution works


def test_orcid_conflict_is_low(corpus, matcher, roster):
    roster = roster.copy()
    roster.loc[0, "orcid"] = "0000-0000-0000-0001"
    authors = M.index_corpus_authors(corpus, matcher)
    candidates = [M.Candidate("souza--ana", "A1", {"corpus"})]
    scored = M.score_candidates(roster, candidates, {}, authors, INST)
    assert scored.loc[0, "orcid_status"] == "conflict"
    assert scored.loc[0, "confidence"] == "low"


def test_shared_profile_is_not_high(matcher):
    roster = pd.DataFrame(
        {
            "person_id": ["martin--pierre", "martin--paul"],
            "last_name": ["MARTIN", "MARTIN"],
            "first_name": ["Pierre", "Paul"],
            "orcid": ["", ""],
            "openalex_ids": ["", ""],
            "lab": ["", ""],
        }
    )
    authors = {"A7": M.CorpusAuthor("A7", names=Counter({"P. Martin": 3}), n_inst_works=3)}
    candidates = M.gather_candidates(roster, M.SurnameIndex(authors), None, matcher)
    scored = M.score_candidates(roster, candidates, {}, authors, INST)
    assert set(scored.person_id) == {"martin--pierre", "martin--paul"}
    assert (scored.confidence != "high").all()
    assert set(scored.shared_with) == {"martin--pierre", "martin--paul"}


def test_cached_searches_are_reused(matcher, roster):
    cache = M.LookupCache()
    cache.searches["dupont--jeanne"] = [
        {"id": "https://openalex.org/A8", "display_name": "Jeanne Dupont"},
        {"id": "https://openalex.org/A10", "display_name": "Marc Dupont"},
    ]
    candidates = M.gather_candidates(roster, M.SurnameIndex({}), None, matcher, cache=cache)
    assert {(c.person_id, c.author_id) for c in candidates} == {("dupont--jeanne", "A8")}


def test_verdicts_survive_rewrites(tmp_path):
    frame = pd.DataFrame(
        {
            "person_id": ["p", "p", "q"],
            "author_id": ["A1", "A2", "A3"],
            "auto_accept": [True, False, False],
            "confidence": ["high", "medium", "low"],
        }
    )
    path = tmp_path / "review.csv"
    M.write_review(M.apply_verdicts(frame, {}), path)
    edited = pd.read_csv(path, dtype=str).fillna("")
    edited.loc[edited.author_id == "A1", "verdict"] = "wrong"
    edited.loc[edited.author_id == "A2", "verdict"] = "OK "
    edited.to_csv(path, index=False)

    again = M.apply_verdicts(frame, M.read_verdicts(path)).set_index("author_id")
    assert again.loc["A1", "accepted"] == False  # noqa: E712 - numpy bool
    assert again.loc["A2", "accepted"] == True  # noqa: E712
    assert again.loc["A3", "accepted"] == False  # noqa: E712

    roster = pd.DataFrame({"person_id": ["p", "q", "r"]})
    consolidated = M.consolidate(roster, again.reset_index()).set_index("person_id")
    assert consolidated.loc["p", "author_ids"] == "A2"
    assert consolidated.loc["q", "match_status"] == "to review"
    assert consolidated.loc["r", "match_status"] == "not found"


def test_unknown_verdict_is_rejected(tmp_path):
    path = tmp_path / "review.csv"
    pd.DataFrame({"person_id": ["p"], "author_id": ["A1"], "verdict": ["maybe"]}).to_csv(path)
    with pytest.raises(ValueError, match="maybe"):
        M.read_verdicts(path)


def test_two_orcids_of_one_person_are_not_a_conflict(corpus, matcher, roster):
    roster = roster.copy()
    roster.loc[0, "orcid"] = "0000-0000-0000-0001"
    authors = M.index_corpus_authors(corpus, matcher)
    candidates = [M.Candidate("souza--ana", "A1", {"corpus"})]
    names = {"0000-0000-0000-0001": "souza ana", "0000-0002-1825-0097": "Ana Souza"}
    scored = M.score_candidates(roster, candidates, {}, authors, INST, names)
    assert scored.loc[0, "orcid_status"] == "match (two ORCIDs)"
    assert scored.loc[0, "confidence"] == "high"
    names["0000-0000-0000-0001"] = "Someone Else"
    scored = M.score_candidates(roster, candidates, {}, authors, INST, names)
    assert scored.loc[0, "confidence"] == "low"


def test_mismatched_orcids_lists_both_sides(roster):
    candidates = [M.Candidate("souza--ana", "A1", {"orcid"})]
    authors = {"A1": {"orcid": "https://orcid.org/0000-0000-0000-0002"}}
    assert M.mismatched_orcids(roster, candidates, authors) == {
        "0000-0002-1825-0097",
        "0000-0000-0000-0002",
    }


def test_conflated_verdict_is_accepted_and_listed():
    frame = pd.DataFrame(
        {"person_id": ["p"], "author_id": ["A1"], "auto_accept": [False], "confidence": ["low"]}
    )
    applied = M.apply_verdicts(frame, {("p", "A1"): ("conflated", "")})
    assert applied.accepted.all()
    consolidated = M.consolidate(pd.DataFrame({"person_id": ["p"]}), applied)
    assert consolidated.loc[0, "conflated_ids"] == "A1"
    assert consolidated.loc[0, "author_ids"] == "A1"
