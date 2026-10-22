import pandas as pd
import pytest
from conftest import MIT, authorship, work

from bibliometry import analysis as A
from bibliometry import venues as V
from bibliometry.lists import build_lists
from bibliometry.store import WorksStore
from bibliometry.tables import build_tables, excel_safe, venue_of


def test_venue_prefers_journal_over_repository():
    hal_first = work("W1", "t" * 40, [], source_type="repository")
    hal_first["locations"] = [
        {"source": {"id": "https://openalex.org/S9", "type": "repository"}},
        {"source": {"id": "https://openalex.org/S2", "type": "journal", "display_name": "J"}},
    ]
    venue, found = venue_of(hal_first)
    assert venue["id"].endswith("S2") and found == "other location"
    assert venue_of(work("W2", "t" * 40, []))[1] == "primary"


@pytest.fixture
def built(project, corpus):
    WorksStore(project.institution_store).add_many(corpus)
    WorksStore(project.people_store).add_many(
        [work("W5", "Elsewhere " * 5, [authorship("A1", "Ana Souza", [MIT])])]
    )
    consolidated = pd.DataFrame(
        {
            "person_id": ["souza--ana"],
            "author_ids": ["A1;A2"],
            "lab": ["WAVES"],
            "match_status": ["matched"],
        }
    )
    return project, build_tables(project, consolidated)["works"]


def test_two_lists_with_all_fields(built):
    config, works = built
    lists = build_lists(config, works)
    assert set(lists["list1_institution"].work_id) == {"W1", "W2", "W3"}
    assert set(lists["list2_roster"].work_id) == {"W1", "W2"}  # W5 is not signed
    full = lists["list1_institution"]
    assert "primary_location.source.display_name" in full.columns
    assert "abstract_inverted_index" not in full.columns
    assert full.abstract.iloc[0] == "Hello world"
    assert full.authorships.iloc[0].startswith("[")  # lists kept as JSON text


def test_top_cited_shares():
    works = pd.DataFrame(
        {
            "publication_year": [2020, 2020, 2021, 2021],
            "citation_percentile": [0.995, 0.5, 0.95, None],
            "top_10_percent": [True, False, True, None],
            "top_1_percent": [True, False, False, None],
        }
    )
    table = A.top_cited(works)
    assert table.loc["all", "with_percentile"] == 3
    assert table.loc["all", "share_top10"] == pytest.approx(2 / 3)
    assert table.loc["2020", "share_top1"] == pytest.approx(0.5)


def test_venue_ranking_and_shares():
    def source(i, kind, h):
        return {
            "id": f"https://openalex.org/S{i}",
            "display_name": f"V{i}",
            "type": kind,
            "summary_stats": {"h_index": h, "i10_index": h * 10, "2yr_mean_citedness": h / 10},
        }

    sources = [source(i, "journal", i) for i in range(1, 21)] + [source(99, "repository", 1000)]
    works = pd.DataFrame(
        {
            "venue_id": [f"S{i}" for i in range(1, 21)] + ["S20", "S99"],
            "in_list_institution": True,
            "in_list_roster": [False] * 20 + [True, True],
        }
    )
    venues = V.flag_top(V.venue_table(sources, works))
    assert venues.attrs["n_ranked"] == 20  # the repository is not ranked
    assert set(venues.loc[venues.top10_h_index, "venue_id"]) == {"S20", "S19"}
    assert not venues.set_index("venue_id").loc["S99", "top10_any"]
    roster = A.venue_indicators(works[works.in_list_roster], venues)
    assert roster["in_ranked_journal"] == 1 and roster["top10_h_index_share"] == 1.0


def test_excel_safe_strips_control_characters():
    frame = excel_safe(pd.DataFrame({"a": ["ok\x0bbad", "x" * 40_000]}))
    assert frame.a[0] == "okbad" and len(frame.a[1]) <= 32_767


def _topic(subfield, field, domain, count):
    return {
        "count": count,
        "subfield": {
            "id": f"https://openalex.org/subfields/{subfield}",
            "display_name": f"sub{subfield}",
        },
        "field": {"display_name": field},
        "domain": {"display_name": domain},
    }


def test_main_subfield_sums_topic_counts():
    source = {
        "topics": [  # first topic is not the largest subfield once counts are summed
            _topic(1702, "Computer Science", "Physical Sciences", 50),
            _topic(1312, "Biochemistry", "Life Sciences", 40),
            _topic(1312, "Biochemistry", "Life Sciences", 30),
            _topic(2204, "Engineering", "Physical Sciences", 40),
        ]
    }
    main = V.main_subfield(source)
    assert main["subfield_id"] == "1312"
    assert main["subfield_share"] == pytest.approx(70 / 160)
    assert main["domain_share"] == pytest.approx(90 / 160)  # Physical Sciences 50 + 40
    assert V.main_subfield({})["subfield_id"] is None


def test_world_percentiles_within_subfield():
    world = pd.DataFrame(
        {
            "subfield_id": ["A"] * 20 + ["B"] * 20 + ["D"] * 5,
            "2yr_mean_citedness": list(range(1, 21)) + [x * 10 for x in range(1, 21)] + [1] * 5,
            "h_index": 1,
            "i10_index": 1,
        }
    )
    venues = pd.DataFrame(
        {
            "subfield_id": ["A", "B", "A", "C", "D"],
            "ranked": [True, True, False, True, True],
            "2yr_mean_citedness": [20.0, 20.0, 20.0, 5.0, 5.0],
            "h_index": 1,
            "i10_index": 1,
        }
    )
    out = V.world_percentiles(venues, world)
    assert out.loc[0, "world_pct_2yr_mean_citedness"] == 0.0  # best of subfield A
    assert bool(out.loc[0, "world_top10_2yr_mean_citedness"])
    assert out.loc[1, "world_pct_2yr_mean_citedness"] == pytest.approx(0.9)  # weak in B
    assert not bool(out.loc[1, "world_top10_2yr_mean_citedness"])
    assert pd.isna(out.loc[2, "world_pct_2yr_mean_citedness"])  # not a ranked venue
    assert pd.isna(out.loc[3, "world_pct_2yr_mean_citedness"])  # no reference subfield
    assert pd.isna(out.loc[4, "world_pct_2yr_mean_citedness"])  # reference set too small


def test_top_papers_lists_authors_with_marks(built):
    config, works = built
    works = works.assign(top_1_percent=works.work_id == "W2")
    authorships = build_tables(
        config,
        pd.DataFrame(
            {
                "person_id": ["souza--ana"],
                "author_ids": ["A1;A2"],
                "lab": ["WAVES"],
                "match_status": ["matched"],
            }
        ),
    )["authorships"]
    papers = A.top_papers(works, authorships)
    assert papers.work_id.tolist() == ["W2"]
    assert papers.authors.iloc[0] == "A. Souza* [souza--ana]; Pierre Martin*"


def test_every_output_column_is_documented(built):
    """docs/columns.md must mention every column the toolkit itself creates."""
    from pathlib import Path

    doc = (Path(__file__).parents[1] / "docs" / "columns.md").read_text(encoding="utf-8")
    config, works = built
    tables = build_tables(config, None)
    world = pd.DataFrame(
        {"subfield_id": ["1"] * 25, "2yr_mean_citedness": 1.0, "h_index": 1, "i10_index": 1}
    )
    source = {
        "id": "https://openalex.org/S1",
        "type": "journal",
        "summary_stats": {"2yr_mean_citedness": 1, "h_index": 1, "i10_index": 1},
        "topics": [_topic(1, "F", "D", 1)],
    }
    venues = V.ordered(V.world_percentiles(V.flag_top(V.venue_table([source], works)), world))
    lists = build_lists(config, works)["list1_institution"]
    from bibliometry.lists import FLAG_COLUMNS
    from bibliometry.people_output import PUBLICATION_COLUMNS, person_indicators

    people = pd.DataFrame(
        {
            "person_id": ["souza--ana"],
            "last_name": ["SOUZA"],
            "first_name": ["Ana"],
            "lab": ["WAVES"],
            "match_status": ["matched"],
        }
    )

    columns = {
        *tables["works"].columns,
        *tables["authorships"].columns,
        *tables["topics"].columns,
        *tables["keywords"].columns,
        *tables["funders"].columns,
        *tables["awards"].columns,
        *tables["counts_by_year"].columns,
        *tables["references"].columns,
        *venues.columns,
        *FLAG_COLUMNS,
        *PUBLICATION_COLUMNS,
        *person_indicators(works, people, venues).columns,
    }
    assert lists.columns[0] == "work_id"
    missing = sorted(c for c in columns if f"`{c}`" not in doc and c not in doc)
    assert not missing, f"undocumented columns: {missing}"
