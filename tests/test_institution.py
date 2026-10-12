import re

import pytest
from conftest import CNRS, HOME, HOME_PATTERN, INST, institution

from bibliometry.institution import (
    AffiliationMatcher,
    build_affiliation_pattern,
    fold,
    normalise_institution_query,
)


@pytest.mark.parametrize(
    "text",
    [
        "Waves Lab, Universite d'Exemple, Exempleville",
        "CNRS, Université d’Exemple, Institute of Optics",  # typographic apostrophe
        "Dept of Physics, University of Exemple, Exempleville",
        "Exemple  University, School of Engineering",
        "UDEX, Laboratory of Mechanics",
    ],
)
def test_home_pattern_matches_name_variants(text):
    assert re.search(HOME_PATTERN, fold(text))


@pytest.mark.parametrize(
    "text",
    [
        "Universite d'Autreville, France",
        "University of Exempleton, UK",  # a longer name sharing the stem
        "Laboratoire UDEXA, Paris",  # acronym inside another word
    ],
)
def test_home_pattern_rejects_other_institutions(text):
    assert not re.search(HOME_PATTERN, fold(text))


def test_generic_pattern_built_from_name_variants():
    record = {
        "id": "https://openalex.org/I1",
        "display_name": "Université de Test",
        "display_name_alternatives": ["Test University"],
        "display_name_acronyms": ["UT", "UDT"],
    }
    pattern = build_affiliation_pattern(record)
    assert re.search(pattern, fold("Lab X, Université  de Test, France"))
    assert re.search(pattern, fold("Dept of Y, TEST UNIVERSITY"))
    assert re.search(pattern, fold("UDT, Paris"))
    assert not re.search(pattern, fold("UT Austin"))  # two-letter acronyms are skipped
    assert not re.search(pattern, fold("Test Universityville"))


def test_query_normalisation():
    assert normalise_institution_query("https://openalex.org/I1000000001") == "I1000000001"
    assert normalise_institution_query("https://ror.org/012345678") == "ror:012345678"
    assert normalise_institution_query("012345678") == "ror:012345678"
    assert normalise_institution_query("University of Exemple") == ""


def test_matcher_signals():
    matcher = AffiliationMatcher(INST, HOME_PATTERN)
    child = institution("I999", "A lab of the university", lineage=[INST, "I999"])
    assert matcher.authorship_signal({"institutions": [CNRS, HOME]}) == "id"
    assert matcher.authorship_signal({"institutions": [child]}) == "id"
    raw = {"institutions": [], "raw_affiliation_strings": ["University of Exemple"]}
    assert matcher.authorship_signal(raw) == "raw"
    assert matcher.authorship_signal({"institutions": [CNRS]}) == ""
