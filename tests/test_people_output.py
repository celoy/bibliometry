import pandas as pd
import pytest
from conftest import HOME, MIT, authorship, work

from bibliometry import people_output as P
from bibliometry.store import WorksStore
from bibliometry.tables import build_tables

PEOPLE = pd.DataFrame(
    {
        "person_id": ["souza--ana", "martin--pierre", "dupont--jeanne"],
        "last_name": ["SOUZA", "MARTIN", "DUPONT"],
        "first_name": ["Ana", "Pierre", "Jeanne"],
        "author_ids": ["A1;A2", "A3", ""],
        "lab": ["WAVES", "OLDLAB", "MECHLAB"],
        "match_status": ["matched", "matched", "not found"],
    }
)


@pytest.fixture
def tables(project, corpus):
    """W1–W3 from the corpus; W5 signed elsewhere; W6 the HAL copy of W2; W7 a referee report."""
    WorksStore(project.institution_store).add_many(corpus)
    w2 = corpus[1]
    WorksStore(project.people_store).add_many(
        [
            work("W5", "Elsewhere " * 5, [authorship("A1", "Ana Souza", [MIT])]),
            work("W6", w2["title"], w2["authorships"], year=2023, source_type="repository"),
            work(
                "W7",
                "Review of a manuscript " * 3,
                [authorship("A1", "Ana Souza", [HOME])],
                type_="peer-review",
            ),
        ]
    )
    return build_tables(project, PEOPLE)


def test_only_works_the_person_signed(tables):
    pubs = P.publications_by_person(tables["works"], PEOPLE, tables["authorships"])
    by_person = pubs.groupby("person_id").work_id.apply(set).to_dict()
    # W5 signed elsewhere, W6 a copy of W2, W7 a referee report: none listed.
    assert by_person == {"souza--ana": {"W1", "W2"}, "martin--pierre": {"W2", "W3"}}
    assert pubs.person_id.iloc[0] == "souza--ana"  # roster order
    assert pubs[pubs.person_id == "souza--ana"].publication_year.tolist() == [2023, 2022]


def test_co_signed_work_listed_under_both(tables):
    pubs = P.publications_by_person(tables["works"], PEOPLE, tables["authorships"])
    w2 = pubs[pubs.work_id == "W2"].set_index("person_id")
    assert w2.loc["souza--ana", "co_listed"] == "martin--pierre"
    assert w2.loc["martin--pierre", "co_listed"] == "souza--ana"
    assert w2.loc["souza--ana", "authors"] == "A. Souza; Pierre Martin"
    assert w2.loc["souza--ana", "doi_url"] == "https://doi.org/10.1/w2"


@pytest.mark.parametrize(
    ("first", "last", "expected"),
    [("12", "25", "12–25"), ("e1234", None, "e1234"), ("7", "7", "7"), (None, None, "")],
)
def test_pages(first, last, expected):
    assert P.pages(first, last) == expected


def test_reference_skips_missing_parts():
    row = pd.Series(
        {
            "authors": "A. Souza",
            "publication_year": 2022,
            "title": "Fish",
            "journal": "J. Fluid Mech.",
            "volume": "950",
            "issue": None,
            "pages": "A12",
            "doi": "10.1/x",
        }
    )
    assert P.reference(row) == "A. Souza (2022). Fish. J. Fluid Mech. 950, A12. doi:10.1/x"
    bare = row.copy()
    bare[["journal", "volume", "pages", "doi"]] = [None, None, "", ""]
    assert P.reference(bare) == "A. Souza (2022). Fish."


def test_summary_counts_and_indicators(tables):
    works = tables["works"]
    pubs = P.publications_by_person(works, PEOPLE, tables["authorships"])
    summary = P.person_summary(works, PEOPLE, pubs).set_index("person_id")
    souza = summary.loc["souza--ana"]
    assert (souza.signed, souza.research, souza.signed_elsewhere) == (2, 2, 1)
    assert souza.top10 == 2  # every synthetic work is in the top 10%
    assert (souza["2022"], souza["2023"]) == (1, 1)
    dupont = summary.loc["dupont--jeanne"]
    assert dupont.signed == 0 and dupont["2022"] == 0


def test_workbook_has_one_sheet_per_person(tables, tmp_path):
    works = tables["works"]
    pubs = P.publications_by_person(works, PEOPLE, tables["authorships"])
    P.write_workbook(pubs, P.person_summary(works, PEOPLE, pubs), tmp_path)
    sheets = pd.ExcelFile(tmp_path / "publications_by_person.xlsx").sheet_names
    assert sheets == ["README", "summary", "all", "souza--ana", "martin--pierre"]
    assert (tmp_path / "publications_by_person.parquet").exists()


def test_sheet_names_are_valid_and_distinct():
    long = "a" * 40
    names = P._sheet_names([long + "x", long + "y", "summary", "x[1]"])
    assert len(set(names.values())) == 4
    assert all(len(n) <= 31 for n in names.values())
    assert names["summary"] != "summary" and names["x[1]"] == "x-1-"
