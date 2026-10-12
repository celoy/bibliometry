"""Shared fixtures: synthetic OpenAlex records and a configured project folder.

Everything is offline. Records carry only the fields the code reads, in OpenAlex's
shape (full URLs as ids, nested authorships).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bibliometry.config import Config, InstitutionSettings, RosterSettings
from bibliometry.institution import build_affiliation_pattern

INST = "I1000000001"
OA = "https://openalex.org/"


def institution(inst_id: str, name: str, country: str = "FR", lineage: list[str] | None = None):
    return {
        "id": OA + inst_id,
        "display_name": name,
        "ror": "https://ror.org/0abcdef12",
        "country_code": country,
        "type": "education",
        "lineage": [OA + i for i in (lineage or [inst_id])],
    }


HOME = institution(INST, "Université d'Exemple")
HOME_RECORD = {
    "id": OA + INST,
    "display_name": "Université d'Exemple",
    "display_name_alternatives": ["University of Exemple", "Exemple University"],
    "display_name_acronyms": ["UDEX"],
}
HOME_PATTERN = build_affiliation_pattern(HOME_RECORD)
CNRS = institution("I1294671590", "Centre National de la Recherche Scientifique")
MIT = institution("I63966007", "Massachusetts Institute of Technology", "US")


def authorship(author_id, name, insts=(), raw=(), orcid=None, countries=None):
    insts = list(insts)
    return {
        "author_position": "middle",
        "author": {
            "id": OA + author_id,
            "display_name": name,
            "orcid": f"https://orcid.org/{orcid}" if orcid else None,
        },
        "institutions": insts,
        "countries": countries
        if countries is not None
        else sorted({i["country_code"] for i in insts}),
        "is_corresponding": False,
        "raw_author_name": name,
        "raw_affiliation_strings": list(raw),
    }


def work(work_id, title, authorships, year=2022, type_="article", source_type="journal", **extra):
    record = {
        "id": OA + work_id,
        "doi": f"https://doi.org/10.1/{work_id.lower()}",
        "title": title,
        "publication_year": year,
        "publication_date": f"{year}-05-01",
        "type": type_,
        "language": "en",
        "primary_location": {
            "source": {
                "id": OA + "S1",
                "display_name": "Journal of Tests" if source_type == "journal" else "HAL",
                "type": source_type,
                "issn_l": "1234-5678",
                "host_organization_name": "Test Press",
            },
            "license": "cc-by",
        },
        "open_access": {"is_oa": True, "oa_status": "gold", "oa_url": None},
        "authorships": authorships,
        "cited_by_count": 10,
        "fwci": 1.5,
        "citation_normalized_percentile": {
            "value": 0.9,
            "is_in_top_1_percent": False,
            "is_in_top_10_percent": True,
        },
        "countries_distinct_count": len({c for a in authorships for c in a["countries"]}),
        "institutions_distinct_count": 2,
        "primary_topic": {
            "display_name": "Fluid Dynamics",
            "subfield": {"display_name": "Mechanics"},
            "field": {"display_name": "Engineering"},
            "domain": {"display_name": "Physical Sciences"},
        },
        "topics": [
            {
                "id": OA + "T1",
                "display_name": "Fluid Dynamics",
                "score": 0.9,
                "subfield": {"display_name": "Mechanics"},
                "field": {"display_name": "Engineering"},
                "domain": {"display_name": "Physical Sciences"},
            }
        ],
        "keywords": [{"display_name": "turbulence", "score": 0.6}],
        "funders": [{"id": OA + "F1", "display_name": "ANR", "ror": "https://ror.org/00rbzpz17"}],
        "awards": [
            {"funder_id": OA + "F1", "funder_display_name": "ANR", "funder_award_id": "X-1"}
        ],
        "sustainable_development_goals": [],
        "referenced_works": [OA + "W9"],
        "counts_by_year": [{"year": 2023, "cited_by_count": 10}],
        "abstract_inverted_index": {"Hello": [0], "world": [1]},
        "is_retracted": False,
        "is_paratext": False,
    }
    record.update(extra)
    return record


@pytest.fixture
def corpus() -> list[dict]:
    """Institution corpus: Souza signs with the institution twice; a homonym at another university never does."""
    return [
        work(
            "W1",
            "Swimming of fish in a turbulent flow at high Reynolds number",
            [
                authorship("A1", "Ana Souza", [HOME, CNRS], orcid="0000-0002-1825-0097"),
                authorship("A9", "Jane Smith", [MIT]),
            ],
        ),
        work(
            "W2",
            "Branching of trees and the allometry of wind loads on branches",
            [
                authorship(
                    "A2", "A. Souza", [], raw=["Waves Lab, Universite d'Exemple, Exempleville"]
                ),
                authorship("A3", "Pierre Martin", [HOME]),
            ],
            year=2023,
        ),
        work(
            "W3",
            "A paper from another university by a homonym with the same surname as ours",
            [
                authorship("A4", "Alice Souza", [], raw=["Universite d'Autreville, France"]),
                authorship("A3", "Pierre Martin", [HOME]),
            ],
        ),
    ]


@pytest.fixture
def project(tmp_path: Path) -> Config:
    config = Config(
        mailto="someone@example.org",
        institution=InstitutionSettings(
            id=INST, name="Université d'Exemple", affiliation_pattern=HOME_PATTERN
        ),
        roster=RosterSettings(path="people.csv"),
        from_date="2020-01-01",
        to_date="2024-12-31",
        root=tmp_path,
    )
    (tmp_path / "people.csv").write_text(
        "Nom;Prénom;ORCID;Laboratoire\n"
        "SOUZA;Ana;https://orcid.org/0000-0002-1825-0097;WAVES\n"
        "MARTIN;Pierre;;OLDLAB\n"
        "DUPONT;Jeanne;;MECHLAB\n",
        encoding="utf-8",
    )
    config.save(tmp_path / "config.yaml")
    return Config.load(tmp_path / "config.yaml")
