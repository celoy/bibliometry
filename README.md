# bibliometry

This toolkit downloads from [OpenAlex](https://openalex.org) the publications of an institution (a university, a research organisation, in any country), together with those of a list of its people, and gives a first bibliometric overview. No programming knowledge is needed: you run one notebook on Google Colab, or a series of notebooks on your own computer.

You provide four things:

1. your **email** address;
2. the **institution**;
3. a **CSV or Excel list of people**;
4. a **period**.

You get back:

- every publication of the institution in the period;
- every publication of the listed people in the period, flagged according to whether they signed it as members of the institution;
- an Excel workbook;
- tables ready for analysis.

## Before you start: an OpenAlex account and API key (free, 2 minutes, once)

OpenAlex is free, but it limits how much each user can download per day. A free **API key** raises that limit tenfold: from US$0.10 to US$1 of usage per day. US$1 is enough for several full runs.

1. **Create an account.** Go to [openalex.org](https://openalex.org) and click **Sign up** (top right). Register with your email address and confirm it from the email you receive.
2. **Copy your API key.** Signed in, open [openalex.org/settings/api](https://openalex.org/settings/api). The page shows your key, a string such as `AbCd1234EfGh5678IjKl`. Copy it.
3. **Give it to the toolkit**, depending on how you run it:
   - **Google Colab:** click the **🔑 key icon** (*Secrets*) in the left sidebar → **Add new secret**. Set the name to `OPENALEX_API_KEY`, paste the key as the value, and switch on **Notebook access**.
   - **On your computer:** paste it into the *OpenAlex API key* field of notebook `00_setup`, or answer the question asked by `bibliometry init`. It is saved in a file named `.env`.

Treat the key like a password: OpenAlex also uses it to sign you in to its website. Never paste it into a notebook cell or a file you share. If it leaks, get a new one on the same settings page; this signs you out of your other devices.

Your daily usage is shown at [openalex.org/settings/usage](https://openalex.org/settings/usage). The budget refills every day at midnight UTC.

## Quick start

### Option 1 — Google Colab (nothing to install; recommended for a first try)

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/celoy/bibliometry/blob/main/notebooks/colab_bibliometry.ipynb)

Click the badge to open `notebooks/colab_bibliometry.ipynb`. This single notebook does everything:

- settings;
- the institution's publications;
- matching people to OpenAlex profiles, with your verdicts;
- the people's publications;
- the two publication lists, the most-cited shares and the venue flags;
- tables, the Excel workbook and a first analysis.

Run its cells from top to bottom. It asks for permission to use your Google Drive, and keeps every file in **`My Drive/bibliometry`**: settings, downloads, your verdicts and results. You can close it and come back later; each step resumes where it stopped.

To use your own list of people, either:

- put the file in `My Drive/bibliometry` and type its name;
- paste a link (a Google Sheets *share* link works);
- tick *upload from my computer*.

### Option 2 — on your computer

You need Python 3.10 or newer.

```bash
git clone https://github.com/celoy/bibliometry.git && cd bibliometry
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e .
jupyter lab notebooks/        # or open the notebooks in VS Code
```

Then run the notebooks in order. They save their settings and results in the project folder (`config.yaml`, `data/`):

| Notebook | What it does | Time |
|---|---|---|
| `00_setup` | A form asks for your email, API key, the institution (searched by name), the list of people and the period. It saves `config.yaml`. | 1 min |
| `01_roster_matching` | Downloads the institution's publications, then matches each person to their OpenAlex profile(s). You confirm the uncertain matches. | 2–5 min |
| `02_harvest_people` | Downloads the people's publications, then builds the tables and the Excel workbook. | 1–2 min |
| `03_starter_analysis` | Shows output per year and per lab, publication types, open access, venues, citations, collaborations and topics. | 1 min |
| `04_lists_and_indicators` | Builds the two publication lists with all OpenAlex fields, the top 10% / 1% most-cited shares, and venue classification with top-10% flags. | 1 min |

You can re-run any notebook. Downloads resume where they stopped and skip what is already complete, and the verdicts you have already entered are kept.

The same steps also run from a terminal: `bibliometry init`, then `bibliometry run`. Each step is also available separately; see `bibliometry --help`.

## The list of people

The list can be a CSV or Excel file (all sheets are read), a link to one, or a Google Sheets share link.

- **Required:** a column of last names, e.g. `Last name`, `Surname`, `Family name` (or the French `Nom`).
- **Recommended:**
  - first names (`First name`, `Given name`, `Prénom`);
  - **ORCID**, which gives the most reliable match;
  - the lab (`Lab`, `Unit`, `Laboratory`, `Laboratoire`);
  - an `OpenAlex` column, if you already know someone's author ID.

Other columns, such as rank or status, are kept as they are, so you can use them in the analysis. Headers are recognised in English and French. In any other language, or if a column is not recognised, rename it, or map it under `roster.columns` in `config.yaml`. A lab written two ways, such as an old and a new name, can be unified with `roster.lab_aliases`. See `examples/roster_template.csv` and `config.example.yaml`. Every recognised header is listed in [docs/columns.md](docs/columns.md#1-the-list-of-people-input).

## How people are matched to OpenAlex profiles

OpenAlex sometimes merges two people with the same name, and sometimes splits one person over several profiles. Each person therefore gets *candidate* profiles. These come from:

- the list itself;
- the ORCID;
- the authors of the institution's publications whose name fits and who signed with the institution;
- for people still without a candidate, a name search.

A candidate is **accepted automatically** when one of these holds:

- the ORCID agrees. A person with two ORCID records is recognised when both records carry the same name on orcid.org;
- the ID was given in the list;
- the name fits and the profile has publications **signed with the institution** in the period, and no other listed person has a compatible name. "Fits" includes a first-name initial ("J. Smith" for John SMITH) and one part of a compound surname ("María García" for María GARCÍA LÓPEZ).

A name match alone is not enough, even when the profile was affiliated with the institution at some point: every name-search result is, by construction. These candidates wait for your verdict, which you enter in notebook 01 or in `data/review/author_matches.csv`:

| Verdict | Meaning |
|---|---|
| `ok` | the profile is this person |
| `wrong` | somebody else |
| `conflated` | OpenAlex merged this person with a namesake. The profile is used, but a work counts for the person only where this profile signed with the institution. |
| *(blank)* | automatic decision |

**Publications attached to no profile.** OpenAlex leaves some authorships without any author ID, for example after splitting a merged profile. Such an authorship is linked to a listed person when it is signed with the institution and exactly one person on the list has a compatible name (`authorships.person_link = "name"`). On a test institution (about 6,000 publications over five years), this recovered about 20 works, including most of the output of a person whose former profile had been merged with a namesake's.

On the same test institution, the automatic decision was compared with a hand-curated list of about 70 people. The profiles it accepted matched that list, apart from split profiles the hand-curated list had missed.

## What is "a publication of the institution"

A publication counts if **at least one author signed with the institution**. The institution is recognised in either of two ways:

- **By the OpenAlex institution ID**, including the institution's sub-units.
- **By the raw affiliation text**, when OpenAlex did not resolve it; this applies to about a fifth of authorships. The text pattern is built automatically from the names OpenAlex knows for the institution, such as "University of X", "X University" and the acronym. You can extend it in `config.yaml` (`institution.affiliation_pattern`), for example with a local abbreviation.
  - Check what it catches: a generic stem such as "institute of technology" also matches sister institutions in other cities, and a short acronym can match unrelated labs.

Every publication of the listed people is downloaded, including those signed elsewhere. They are kept but flagged:

- `roster_people`: listed people among the authors;
- `roster_people_signed`: those who signed with the institution.

By default, the analysis keeps signed research publications and merges the duplicates OpenAlex holds for one paper, such as an article and its arXiv, HAL or institutional-repository deposit (about 6% of works in our test).

## The two publication lists and their indicators

| List | Content |
|---|---|
| **1 — institution** | every publication of the period with at least one author signing with the institution |
| **2 — people of the list** | publications where a listed person signed **as a member of the institution**. This is a subset of list 1: what people publish under another affiliation only is not counted. |

Both are written with **every OpenAlex field**: nested fields become dotted columns (`primary_location.source.display_name`, …), lists are kept as JSON text, and the abstract is given as plain text. Notebook 04, the Colab notebook, or `bibliometry lists` then compute:

- **Top 10% / 1% most cited.** The number and share of publications in the world's top 10% and top 1%, per year and in total, using OpenAlex's `citation_normalized_percentile` (same field, year and type). The share is taken over publications that have a percentile, and the coverage is reported. Each figure is given for all works and for research outputs, deduplicated.
- **The top-1% papers** of each list, with their authors. Authors signing with the institution are marked `*`, and listed people are followed by their ID.
- **Venues.** Every venue is classified by type, publisher, country, main subfield, field and domain, open access and DOAJ. A HAL/arXiv deposit is replaced by the journal when OpenAlex knows both.
  - **Headline metric: 2-year mean citedness**, OpenAlex's analogue of the impact factor. It comes with two flags:
    - `world_top10_…`: the journal is in the top 10% of **all active journals in the world with the same main subfield**. This needs a one-time download of ~64,000 journals, which takes a few minutes and costs about US$0.03.
    - `top10_…`: the journal is in the top 10% of the journals used in list 1, all fields together.
  - **Secondary metrics: h-index and i10-index.** They are reported with the same flags, but they grow with a journal's size and age.
  - A journal's main subfield is the one holding most of its works. Journals spanning several domains (Scientific Reports, PLOS ONE, Nature…) are marked `broad_scope`, because their subfield is somewhat arbitrary.

## What you get

Every column of every output file is described in **[docs/columns.md](docs/columns.md)**.

```
data/
  raw/            full OpenAlex records (works, venues), compressed; never edited
  review/         roster.csv, author_matches.csv (your verdicts), people.csv
  tables/         works, authorships, people, topics, keywords, funders, awards,
                  sdgs, references, counts_by_year  (parquet)
  lists/          list1_institution, list2_roster (all OpenAlex fields), venues
                  (.xlsx and .parquet), indicators.xlsx
  exports/        <institution>_<years>_publications.xlsx
```

## The OpenAlex budget

The daily budget is US$0.10 without a key and US$1 with a free key; see [Before you start](#before-you-start-an-openalex-account-and-api-key-free-2-minutes-once). For scale, a full run for a test institution over five years (about 6,000 publications, 100 people) took about 75 requests and cost about US$0.035. Most of that went on name searches. Re-runs reuse cached answers and cost almost nothing. If the budget runs out, the run stops with a message saying so. Everything downloaded so far is kept, and re-running the same step the next day resumes it.

## Confidentiality

The list of people contains personal data (names, emails, ORCIDs). The `data/` folder, `config.yaml`, `.env` and every `.xlsx` file are excluded from git: **do not commit them**. Results that circulate should be aggregated, for example at lab level.

## For maintainers

```bash
pip install -e ".[dev]"
pytest              # offline tests, synthetic records
ruff check . && ruff format .
```

The package code is in `src/bibliometry/`:

| Module | Role |
|---|---|
| `openalex.py` | HTTP client: polite pool, API key, budget headers, retries, cursor paging |
| `store.py` | resumable gzip JSONL store, plus the manifest of completed queries |
| `config.py` | `config.yaml` and `.env` |
| `institution.py` | institution search and the affiliation signals (ID / raw text) |
| `roster.py` | reading messy CSV/XLSX lists |
| `matching.py` | candidates, evidence, confidence, verdicts |
| `harvest.py` | institution and people harvests |
| `tables.py` | flattening to parquet, Excel export |
| `analysis.py` | scope, deduplication, indicators (incl. top 10%/1%), plots |
| `lists.py` | the two publication lists with all OpenAlex fields |
| `venues.py` | venue records, main subfield, world reference, top-10% flags |
| `pipeline.py` | the steps used by both the notebooks and the CLI |
| `cli.py` | the CLI |
| `forms.py` | the setup form |
| `env.py` | local vs. Colab folders, uploads and downloads |

The notebooks only call `pipeline` and `analysis`. Strip notebook outputs before committing.
