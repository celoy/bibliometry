# Columns: what goes in, what comes out

This file describes:

1. the list of people you provide;
2. the review files;
3. the two publication lists;
4. the analysis tables;
5. the venue table;
6. the indicators workbook.

Paths are relative to the project folder (`My Drive/bibliometry` on Colab).

Conventions used throughout:

- **OpenAlex IDs** are written short: `W…` for a work, `A…` for an author, `I…` for an institution, `S…` for a source (venue). `https://openalex.org/W…` opens the record.
- **`person_id`** identifies a person of your list: `lastname--firstname`, lower case, without accents, with spaces and hyphens as `-` (e.g. `garcia-lopez--maria`).
- **Lists inside a cell** are separated by `;`.
- **"Signed with the institution"** means the author's own affiliation on that publication is the institution. It is detected either by OpenAlex's institution ID (including sub-units) or by the raw affiliation text (`institution.affiliation_pattern` in `config.yaml`).

---

## 1. The list of people (input)

The list is a CSV file, an Excel workbook (every sheet is read unless `roster.sheets` says otherwise), or a link to either, including a Google Sheets share link. Each row is one person. The header row may sit below a few title rows: the first row with at least two recognised headers is taken as the header.

Headers are recognised in English and French, ignoring case, accents and punctuation:

| Meaning | Recognised headers | Needed? |
|---|---|---|
| last name | `Last name`, `Lastname`, `Surname`, `Family name`, `Last`, `Nom`, `Nom de famille`, `Nom d'usage` | yes, or a full-name column |
| first name | `First name`, `Firstname`, `Given name(s)`, `Forename`, `First`, `Prénom(s)` | strongly recommended |
| full name (when there are no separate columns) | `Name`, `Full name`, `Author`, `Person`, `Nom complet`, `Nom prénom`, `Prénom nom`, `Auteur`, `Personne` | alternative to last + first name |
| ORCID | `ORCID`, `ORCID id`, `ID ORCID` | recommended: the most reliable match |
| OpenAlex author ID | `OpenAlex`, `OpenAlex id(s)`, `OpenAlex author id`, `ID OpenAlex` | optional: forces the match |
| lab / unit | `Lab`, `Laboratory`, `Unit`, `Research unit`, `Structure`, `Laboratoire`, `Labo`, `Unité`, `Unité de recherche`, `Affectation` | optional: used for per-lab counts |
| email | `Email`, `E-mail`, `Mail`, `Courriel`, `Adresse mail` | optional |

A full-name cell may be written `Last, First`, `LAST First` (the upper-case words are the surname) or `First Last`. Values are cleaned:

- ORCIDs may be bare (`0000-0002-1825-0097`) or URLs;
- several OpenAlex IDs can share one cell (`A123; A456`);
- spaces are trimmed.

A column with no header, or an unrecognised one, is still detected if at least half its cells look like e-mail addresses or ORCIDs. Every other column (rank, status, team, …) is kept unchanged and can be used in the analysis.

If a header is not recognised (another language, an unusual wording), either rename it or map it in `config.yaml`:

```yaml
roster:
  columns: {last_name: "Apellido", first_name: "Nombre", lab: "Departamento"}
  lab_aliases: {"Old Lab Name": "New Lab Name"}   # unify two spellings of a lab
```

A person listed twice (same last and first name) is merged into one row. A template is in [`examples/roster_template.csv`](../examples/roster_template.csv).

---

## 2. Review files: `data/review/`

### `roster.csv`

The list as it was understood. Columns:

- `person_id`, `last_name`, `first_name`, `orcid`, `openalex_ids`, `lab`, `email`;
- `sheet` and `source_row`: where the person was in your file;
- then your other columns, unchanged.

### `author_matches.csv`: one row per (person, candidate OpenAlex profile)

| Column | Meaning |
|---|---|
| `verdict` | **yours to fill**: `ok` (this person), `wrong` (somebody else), `conflated` (OpenAlex merged this person with a namesake; only the works this profile signed with the institution count), or blank (automatic decision) |
| `comment` | free text, kept across re-runs |
| `accepted` | final decision used by the next steps |
| `confidence` | `high` (accepted automatically when no verdict is given), `medium`, `low` |
| `auto_accept` | the automatic decision, before your verdict |
| `person_id`, `last_name`, `first_name`, `lab` | the person |
| `author_id`, `author_name` | the candidate OpenAlex profile |
| `sources` | how the candidate was found: `roster` (ID in your list), `orcid`, `corpus` (an author of the institution's publications), `search` (name search) |
| `name_score` | name fit, 0 to 1: 1 = full first name, 0.7 = initial only, 0.8× when only part of a compound surname matches |
| `orcid_roster`, `orcid_openalex` | the two ORCIDs |
| `orcid_status` | `match`, `match (two ORCIDs)` (two ORCID records with the same name on orcid.org), `conflict`, `unknown` |
| `works_in_period_with_institution` | works of this profile in the period signed with the institution |
| `works_total` | all works of the profile in OpenAlex |
| `ever_affiliated` | OpenAlex lists the institution among the profile's affiliations at some point |
| `last_known_institution`, `top_topics`, `sample_titles` | help for deciding |
| `openalex_url` | open the profile |
| `shared_with` | other people of your list whose name also fits this profile |

### `people.csv`

The list with the matching result. It holds the columns of `roster.csv`, plus:

| Column | Meaning |
|---|---|
| `author_ids` | accepted OpenAlex profiles |
| `conflated_ids` | those marked `conflated` |
| `n_pending` | candidates still waiting for a verdict |
| `match_status` | `matched`, `matched (more to review)`, `to review`, `not found` |

---

## 3. The two publication lists: `data/lists/`

The lists are `list1_institution` and `list2_roster`, each as `.xlsx` and `.parquet`; one row per publication.

- **List 1 (institution):** every publication in the period with at least one author signing with the institution.
- **List 2 (people of the list):** publications where a person of your list signed with the institution. It is a subset of list 1.

The first columns are computed by this toolkit:

| Column | Meaning |
|---|---|
| `work_id` | OpenAlex work ID |
| `in_list_institution`, `in_list_roster` | membership of list 1 / list 2 |
| `roster_people_signed` | people of your list who signed with the institution |
| `roster_people` | people of your list among the authors, whatever their affiliation |
| `in_institution_corpus` | returned by OpenAlex's institution query. When it is false, the work was recognised only from the raw affiliation text. |
| `venue_id`, `venue_name`, `venue_type` | the venue (see [§5](#5-venues-datalistsvenues)) |
| `venue_found_in` | `primary` = OpenAlex's primary location; `other location` = the primary location was a repository deposit, and a journal or conference was found among the other locations |
| `abstract` | plain-text abstract. It replaces OpenAlex's `abstract_inverted_index`, which is not included. |

Every other column is a field of the OpenAlex work record, with the same name. Nested fields are joined with dots (`primary_location.source.display_name`), and fields holding lists are kept as JSON text (`authorships`, `topics`, `locations`, …). The full reference is the [OpenAlex work object](https://docs.openalex.org/api-entities/works/work-object). The most useful ones:

| Group | Columns |
|---|---|
| identifiers | `doi`, `ids.openalex`, `ids.doi`, `ids.pmid`, `ids.mag` |
| bibliographic | `title`, `display_name`, `publication_year`, `publication_date`, `type`, `language`, `biblio.volume`, `biblio.issue`, `biblio.first_page`, `biblio.last_page` |
| where published | `primary_location.*` (incl. `primary_location.source.*` for the journal), `best_oa_location.*`, `locations` (all copies, JSON), `locations_count`, `indexed_in` |
| open access | `open_access.is_oa`, `open_access.oa_status` (gold, green, hybrid, bronze, diamond, closed), `open_access.oa_url`, `has_fulltext` |
| authors | `authorships` (JSON: each author with position, ID, name, ORCID, institutions, countries, raw affiliation text, corresponding flag), `corresponding_author_ids`, `corresponding_institution_ids`, `institutions_distinct_count`, `countries_distinct_count` |
| citations | `cited_by_count`, `counts_by_year` (JSON), `fwci` (field-weighted citation impact, 1 = world average), `citation_normalized_percentile.value` / `.is_in_top_1_percent` / `.is_in_top_10_percent`, `cited_by_percentile_year.*` |
| subject | `primary_topic.*` (topic, subfield, field, domain), `topics` (JSON, up to 3), `keywords`, `concepts` (older classification), `mesh`, `sustainable_development_goals` |
| funding | `funders`, `awards` (JSON) |
| costs | `apc_list.*`, `apc_paid.*` (article processing charges) |
| references | `referenced_works_count`, `referenced_works` (JSON list of work IDs), `related_works` |
| status | `is_retracted`, `is_paratext`, `updated_date`, `created_date` |

The Excel files cut any cell longer than Excel's limit (32,767 characters), which happens for some `authorships`, `referenced_works` or `locations` cells; the parquet files are complete.

---

## 4. Analysis tables: `data/tables/*.parquet`

These are lighter, flat versions, used by the analysis notebooks. Every table is keyed by `work_id`. They cover every downloaded work: the institution's works and every work of the listed people, including those not signed with the institution.

### `works`: one row per publication

| Columns | Meaning |
|---|---|
| `doi`, `title`, `publication_year`, `publication_date`, `type`, `language` | bibliographic |
| `source_id`, `source_name`, `source_type`, `issn_l`, `publisher` | OpenAlex's primary location (may be a repository) |
| `venue_id`, `venue_name`, `venue_type`, `venue_found_in` | the venue (journal preferred over a repository deposit) |
| `volume`, `issue`, `first_page`, `last_page` | |
| `is_oa`, `oa_status`, `oa_url`, `license` | open access |
| `cited_by_count`, `fwci`, `citation_percentile`, `top_10_percent`, `top_1_percent` | citations; the percentile is normalised by field, year and type |
| `n_authors`, `n_institutions`, `n_countries`, `countries`, `international` | collaboration (`international` = authors in more than one country) |
| `primary_topic`, `subfield`, `field`, `domain` | subject |
| `funders`, `referenced_works_count`, `is_retracted`, `is_paratext` | |
| `in_institution_corpus`, `in_people_corpus` | which download the work came from |
| `institution_signed` | some author signed with the institution (= `in_list_institution`) |
| `roster_people`, `roster_people_signed`, `in_list_roster` | as in the lists |
| `abstract`, `openalex_url`, `updated_date` | |

### `authorships`: one row per (publication, author, institution)

An author with no recognised institution still gets one row, with empty institution columns.

| Column | Meaning |
|---|---|
| `author_position_index`, `author_position` | 1, 2, …; `first` / `middle` / `last` |
| `author_id`, `author_name`, `raw_author_name`, `orcid` | the author as OpenAlex knows them, and as written on the paper |
| `is_corresponding` | corresponding author |
| `institution_signal` | `id` / `raw` = this author signed with the institution, by ID or by raw text; empty = no |
| `person_id` | the person of your list, if any |
| `person_link` | `profile` = via an accepted OpenAlex profile; `name` = the authorship has no OpenAlex profile, is signed with the institution, and exactly one person of your list has a compatible name |
| `raw_affiliations` | affiliation text as printed (`\|`-separated) |
| `countries` | |
| `institution_id`, `institution_name`, `institution_ror`, `institution_country`, `institution_type` | one institution of this author on this paper |
| `is_target_institution` | that institution is the target or one of its sub-units |

### `people`

As `people.csv` (§2), plus:

- `works_in_period`: works with this person among the authors;
- `works_signed_with_institution`: those they signed with the institution.

### One row per (publication, item)

| Table | Columns |
|---|---|
| `topics` | `rank` (1 = primary), `topic_id`, `topic`, `score`, `subfield`, `field`, `domain` |
| `keywords` | `keyword`, `score` |
| `funders` | `funder_id`, `funder`, `ror` |
| `awards` | `funder_id`, `funder`, `award_id` (grant number) |
| `sdgs` | `sdg` (UN Sustainable Development Goal), `score` |
| `references` | `referenced_work_id` |
| `counts_by_year` | `year`, `cited_by_count` (citations received that year) |

---

## 5. Venues: `data/lists/venues.*`

One row per venue (journal, conference, book series, repository…) used by the downloaded publications.

**Identity and classification**

| Column | Meaning |
|---|---|
| `venue_id`, `venue_name`, `venue_type` | |
| `issn_l`, `publisher`, `country`, `homepage_url` | |
| `main_subfield`, `main_field`, `domain`, `subfield_id` | the subfield holding most of the venue's works, from its OpenAlex topics |
| `subfield_share`, `domain_share` | share of its works in that subfield / domain |
| `broad_scope` | main domain under 60% of its works (multidisciplinary journals); their subfield is somewhat arbitrary |
| `is_oa`, `is_in_doaj`, `is_core`, `apc_usd`, `listed_in` | open access, DOAJ, CWTS core, publication charge, national lists |
| `works_count`, `cited_by_count` | totals over the venue's history |
| `works_list1`, `works_list2` | publications of each list in this venue |
| `ranked` | a journal with at least one work in list 1. Only these get the flags below. |

**Headline metric: 2-year mean citedness** (`2yr_mean_citedness`): the mean citations received in a year by the venue's papers of the two previous years, OpenAlex's analogue of the impact factor.

| Column | Meaning |
|---|---|
| `top10_2yr_mean_citedness` | among the top 10% of the journals used in list 1 (all fields together) |
| `rank_2yr_mean_citedness` | rank among those journals (1 = best) |
| `world_pct_2yr_mean_citedness` | share of the world's active journals of the same main subfield that score higher (0.03 = better than 97%) |
| `world_top10_2yr_mean_citedness` | `world_pct` below 0.10 |
| `world_threshold_2yr_mean_citedness` | the value needed to be in that world top 10% |
| `world_n` | number of reference journals in the subfield. The world flags are empty below 20. |

The world reference is every journal in OpenAlex with at least 100 works that published in the year before the period's end or later. It is downloaded once (~70,000 journals).

**Secondary metrics:** `h_index` (the venue's h-index) and `i10_index` (number of its papers with ≥ 10 citations). Both grow with a journal's size and age, so they reflect volume as much as impact. Each comes with the same columns as the headline metric:

| h-index | i10-index |
|---|---|
| `rank_h_index` | `rank_i10_index` |
| `top10_h_index` | `top10_i10_index` |
| `world_pct_h_index` | `world_pct_i10_index` |
| `world_top10_h_index` | `world_top10_i10_index` |
| `world_threshold_h_index` | `world_threshold_i10_index` |

`top10_any` and `world_top10_any` are true when the venue is flagged on at least one of the three metrics.

---

## 6. Indicators: `data/lists/indicators.xlsx`

Each figure is given for both lists and for two scopes:

- *all works*;
- *research, deduplicated*: articles, reviews, conference papers, chapters, books, preprints, theses and reports, with a paper and its preprint or repository copy counted once.

| Sheet | Content |
|---|---|
| `top_cited` | per list, scope and year (and `all`): `works`; `with_percentile` (works OpenAlex gives a citation percentile to); `coverage`; `top10` / `share_top10` and `top1` / `share_top1` (works in the world's top 10% / 1% most cited of their field, year and type, as a share of `with_percentile`). The expected shares for an average institution are 10% and 1%. |
| `top_venues` | per list and scope: `works`; `in_ranked_journal`; `unranked` (conferences, books, repository-only); then for each flag of §5, `<flag>_works` and `<flag>_share` (share of `in_ranked_journal`) |
| `venue_thresholds` | lowest value of each metric still in the list-1 top 10% |
| `top1_papers_list1`, `top1_papers_list2` | the world top-1% papers (research, deduplicated): `work_id`, `publication_year`, `title`, `venue_name`, `type`, `doi`, `cited_by_count`, `fwci`, `citation_percentile`, `roster_people_signed`, `authors`. In `authors`, `*` marks authors signing with the institution and `[person_id]` the people of your list; beyond 20 authors the list ends with "et al." |

---

## 7. Quick-look workbook: `data/exports/<institution>_<years>_publications.xlsx`

This workbook gives a summary for spreadsheet users. Its sheets:

- `README`;
- `works` (§4, without abstracts);
- `people`;
- `authorships` (only rows for people of your list or for authors signing with the institution);
- `topics`;
- `funders`.
