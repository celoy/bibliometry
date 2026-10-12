import pandas as pd
import pytest
from conftest import HOME, MIT, authorship, work

from bibliometry import analysis as A
from bibliometry.config import Config, ConfigError, load_api_key, save_api_key
from bibliometry.store import WorksStore
from bibliometry.tables import build_tables, export_excel


@pytest.fixture
def stores(project, corpus):
    WorksStore(project.institution_store).add_many(corpus)
    people_works = [
        corpus[0],  # also in the institution corpus
        work(  # Souza at MIT only: a listed person's work not signed with the institution
            "W5",
            "A sabbatical paper written entirely from Boston about vortex rings",
            [authorship("A1", "Ana Souza", [MIT], countries=["US"])],
        ),
        work(  # the HAL copy of W1: same title, repository source, preprint
            "W6",
            "Swimming of fish in a turbulent flow at high Reynolds number",
            [authorship("A1", "Ana Souza", [HOME])],
            type_="preprint",
            source_type="repository",
        ),
    ]
    WorksStore(project.people_store).add_many(people_works)
    consolidated = pd.DataFrame(
        {
            "person_id": ["souza--ana", "martin--pierre"],
            "author_ids": ["A1;A2", "A3"],
            "lab": ["WAVES", "NEWLAB"],
            "match_status": ["matched", "matched"],
        }
    )
    return project, consolidated


def test_scope_flags(stores):
    config, consolidated = stores
    tables = build_tables(config, consolidated)
    works = tables["works"].set_index("work_id")
    assert works.loc["W1", "in_institution_corpus"] and works.loc["W1", "in_people_corpus"]
    assert works.loc["W2", "institution_signed"]  # raw affiliation only
    assert works.loc["W2", "roster_people_signed"] == "martin--pierre;souza--ana"
    assert not works.loc["W5", "institution_signed"]
    assert works.loc["W5", "roster_people"] == "souza--ana"
    assert works.loc["W5", "roster_people_signed"] == ""
    assert works.loc["W1", "international"]
    assert works.loc["W1", "abstract"] == "Hello world"

    authorships = tables["authorships"]
    a2 = authorships[(authorships.work_id == "W2") & (authorships.author_id == "A2")]
    assert (
        len(a2) == 1 and a2.iloc[0].institution_id == "" and a2.iloc[0].institution_signal == "raw"
    )
    homonym = authorships[authorships.author_id == "A4"].iloc[0]
    assert homonym.institution_signal == "" and homonym.person_id == ""

    people = tables["people"].set_index("person_id")
    assert people.loc["souza--ana", "works_in_period"] == 4  # W1, W2, W5, W6
    assert people.loc["souza--ana", "works_signed_with_institution"] == 3
    for name in ("topics", "keywords", "funders", "awards", "references", "counts_by_year"):
        assert not tables[name].empty, name


def test_scope_and_deduplication(stores):
    config, consolidated = stores
    works = build_tables(config, consolidated)["works"]
    scoped = A.in_scope(works)
    assert set(scoped.work_id) == {"W1", "W2", "W3", "W6"}
    assert set(A.in_scope(works, signed_only=False).work_id) == {"W1", "W2", "W3", "W5", "W6"}
    unique = A.deduplicate(scoped).set_index("work_id")
    assert "W6" not in unique.index  # the preprint copy goes, the article stays
    assert unique.loc["W1", "n_versions"] == 2
    assert len(unique) == 3


def test_indicators_run(stores):
    config, consolidated = stores
    tables = build_tables(config, consolidated)
    works = A.deduplicate(A.in_scope(tables["works"]))
    assert A.works_per_year(works)["works"].sum() == 3
    assert A.works_per_lab(works, tables["people"]).sum().sum() >= 2
    coverage = A.roster_coverage(works, tables["works"])
    assert coverage["roster_works_not_signed"] == 1
    partners = A.partners(tables["authorships"], works, config.institution.id)
    assert "I1000000001" not in set(partners.institution_id)
    assert A.citation_summary(works).loc[2022, "works"] == 2


def test_excel_export(stores, tmp_path):
    config, consolidated = stores
    path = export_excel(build_tables(config, consolidated), tmp_path / "out.xlsx")
    sheets = pd.read_excel(path, sheet_name=None)
    assert {"README", "works", "people", "authorships"} <= set(sheets)
    assert "abstract" not in sheets["works"].columns


def test_config_validation_and_secrets(tmp_path, project):
    assert Config.load(project.root / "config.yaml").institution.id == "I1000000001"
    broken = Config(root=tmp_path)
    with pytest.raises(ConfigError) as info:
        broken.validate()
    message = str(info.value)
    for key in ("mailto", "institution.id", "from_date", "roster.path"):
        assert key in message
    save_api_key("secret-1", tmp_path)
    save_api_key("secret-2", tmp_path)
    assert load_api_key(tmp_path) == "secret-2"
    assert "secret" not in (project.root / "config.yaml").read_text()


def test_conflated_profiles_and_unassigned_authorships(project):
    homonym = work(  # the merged profile A1 on a paper by the other person
        "W7",
        "Plasma physics paper by the other person behind the merged profile",
        [authorship("A1", "A. Souza", [MIT], countries=["US"])],
    )
    signed = work(
        "W8",
        "Fluid-structure interaction of flexible plates in axial flow",
        [authorship("A1", "Ana Souza", [HOME])],
    )
    unassigned = work(  # OpenAlex attached no profile to this authorship
        "W10",
        "Wind-induced reconfiguration of flexible branches and leaves",
        [{**authorship("A0", "Pierre Martin", [HOME]), "author": {}}],
    )
    elsewhere = work(
        "W11",
        "An unassigned Pierre Martin who did not sign with the institution",
        [{**authorship("A0", "Pierre Martin", [MIT]), "author": {}}],
    )
    WorksStore(project.people_store).add_many([homonym, signed, unassigned, elsewhere])
    consolidated = pd.DataFrame(
        {
            "person_id": ["souza--ana", "martin--pierre"],
            "last_name": ["SOUZA", "MARTIN"],
            "first_name": ["Ana", "Pierre"],
            "author_ids": ["A1", ""],
            "conflated_ids": ["A1", ""],
            "lab": ["WAVES", "NEWLAB"],
            "match_status": ["matched", "not found"],
        }
    )
    tables = build_tables(project, consolidated)
    works = tables["works"].set_index("work_id")
    assert works.loc["W7", "roster_people"] == ""  # merged profile, not signed: not ours
    assert works.loc["W8", "roster_people_signed"] == "souza--ana"
    assert works.loc["W10", "roster_people_signed"] == "martin--pierre"
    assert works.loc["W11", "roster_people"] == ""
    links = tables["authorships"].set_index("work_id").person_link
    assert links["W8"] == "profile" and links["W10"] == "name"


def test_single_person_mode(tmp_path):
    from conftest import HOME_RECORD

    from bibliometry import pipeline
    from bibliometry.openalex import institution_works_filter
    from bibliometry.roster import single_person_roster

    roster = single_person_roster("Ana Souza", "https://orcid.org/0000-0002-1825-0097")
    assert roster.person_id.tolist() == ["souza--ana"]
    assert roster.orcid.iloc[0] == "0000-0002-1825-0097"
    assert "raw_author_name.search:Souza" in institution_works_filter("I1", "a", "b", "Souza")

    (tmp_path / ".env").write_text("OPENALEX_API_KEY=k-123\n")
    config = pipeline.person_config(tmp_path, "a@b.org", HOME_RECORD, "Souza, Ana")
    assert config.root == tmp_path / "people" / "souza--ana"
    assert config.single_person and config.from_date == "1900-01-01"
    config.validate()  # no roster file needed
    loaded = Config.load(config.root / "config.yaml")
    assert loaded.cache_path == (tmp_path / "cache").resolve()
    assert loaded.api_key() == "k-123"  # found in a parent folder
    assert pipeline.load_roster(loaded).person_id.tolist() == ["souza--ana"]
