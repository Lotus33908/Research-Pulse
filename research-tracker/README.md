# AKSOB Research Tracker

Keeps an up-to-date list of every publication by the faculty of the Adnan Kassar School of Business
(LAU), identifies each journal, and labels each paper with its Scimago quartile (Q1 to Q4). Produces an
Excel workbook and a self-contained web app (search a professor, profile pages, school analytics,
journals, what's new). Runs unattended every week. Everything it uses is free.

```
AKSOB website ──► faculty roster (data/faculty.csv)
                        │
OpenAlex + ORCID ───────┼──► publications ──► DOI / ISSN lookup ──► Scimago quartile ──► Excel + web app
Scholar BibTeX export ──┘   (SQLite)          (OpenAlex, Crossref)   (per year, 1999-)
(optional, 2 clicks)        flags "missing from Scholar export"
```

## What it does

| Stage | What happens | Source |
|---|---|---|
| `roster` | Reads the school's Faculty and Staff page and one profile page per person: name, title, department, category (full-time, adjunct...), email. Writes `data/faculty.csv`. People who leave are kept with `active=0`. | sb.lau.edu.lb |
| `resolve` | Finds each person's OpenAlex author record (name match plus LAU affiliation) and, through it, their ORCID. | OpenAlex |
| `fetch` | Downloads every work on the person's OpenAlex record and on their ORCID record. | OpenAlex, ORCID (both free) |
| `import-bibtex` | Optional. Imports Google Scholar exports dropped in `data/scholar_exports/` and compares them with what the indexes found. | the professor's own export |
| `enrich` | Titles without a DOI or ISSN are looked up to get the journal's ISSN. Journal versions are preferred over preprints. Results are cached. | OpenAlex, then Crossref |
| `rank` | Matches the journal to the Scimago Journal Rank list **of the paper's publication year** (nearest edition otherwise): by ISSN, else exact title, else strict fuzzy title (flagged for review). | scimagojr.com (one file per year, cached) |
| `report` | `output/AKSOB_Publications.xlsx`: Summary, Faculty Summary, Publications, Journals, Needs Review, Roster, About. Plus `output/publications.csv`. | |
| `dashboard` | `output/dashboard.html` (also `docs/index.html` for GitHub Pages): the web app. | |

Also recorded per paper: ABDC rating (via OpenAlex), citation counts, authors, DOI link. Per person:
citations and h-index (from OpenAlex; lower than Google Scholar's figures, which also count theses, books and working papers).

## Setup (once)

1. Python 3.11 or newer. In this folder:
   ```powershell
   python -m venv ..\.venv
   ..\.venv\Scripts\python -m pip install -r requirements.txt
   copy .env.example .env
   ```
2. Get a **free OpenAlex API key** at https://openalex.org (Settings, API) and put it in `.env` as
   `OPENALEX_API_KEY=...`, with `OPENALEX_MAILTO=your@lau.edu.lb`. Without a key, everyone on the same
   office network shares a small daily budget and a full pass will not finish in one day.
3. First run:
   ```powershell
   ..\.venv\Scripts\python -m aksob_pubs run
   ```
   The first pass downloads 27 Scimago editions (about 300 MB, cached in `data/scimago/`) and fetches
   every record, so allow 20 to 40 minutes. Later runs take a few minutes.

Open `output\AKSOB_Publications.xlsx` and `output\dashboard.html`.

## The web app

Open `output/dashboard.html` in any browser (no server needed) or publish `docs/index.html` with
GitHub Pages for a stable link.

* **Search box** (press `/`): type a professor's name, pick the person, land on their profile.
* **Overview**: school totals, journal articles per year by quartile, Q1+Q2 share trend, department
  table, most Q1 authors, journals the school publishes in most. A period selector (all time, last 10,
  5, 3 years, this year) applies everywhere.
* **Faculty**: the full list, filter by department or category, sort by Q1, Q2, Q3, Q4, Q1+Q2, papers,
  citations, h-index. Click a row for the profile.
* **Profile**: stats, chart by year, top journals, frequent co-authors, the complete publication list
  with search, quartile filters and sorting, CSV download, shareable link.
* **Journals**: every journal the school publishes in, with quartile, SJR, ABDC, paper count and the
  AKSOB authors; click to expand the papers.
* **What's new**: papers added by recent refreshes, this year's output, most cited papers of the year.

Light and dark theme follow the system setting (toggle top right). Monochrome by design; quartiles are
shown with labels, not only shades.

## Running it every week

* **Windows Task Scheduler (this PC):** `powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1`
  registers a job every Monday at 08:00. `scripts\run.ps1` is the runner.
* **GitHub Actions (no PC needed):** `.github/workflows/update-publications.yml` runs every Monday,
  commits the refreshed data and reports, and uploads the Excel file as an artifact. Add
  `OPENALEX_API_KEY` and `OPENALEX_MAILTO` as repository secrets. Enable GitHub Pages from `docs/`.

New papers appear within days of publication (Crossref and OpenAlex register them faster than Google
Scholar does). Quartiles are inherently yearly: Scimago publishes one edition per year, usually around
mid-year. The tracker checks for the new edition on every run and re-ranks everything when it arrives.

## Google Scholar, without scraping

Google blocks scripted access to Scholar, so the tracker does not scrape it. Instead a professor (or
the assistant) can export the profile in two clicks: tick the select-all box above the article list,
Export, BibTeX, save as `data/scholar_exports/<Full Name>.bib` (or `<slug>.bib`). The next run imports
it, counts it as the person's own list, and the Needs Review sheet lists anything the indexes found
that is missing from the export (so the professor can add it) and anything in the export the indexes
did not know (so nothing is lost).

## Keeping the data right

* `data/faculty.csv` is the roster you maintain. You may edit `openalex_id`, `orcid`, `scholar_id`,
  `include` (1/0) and `notes`; everything else is refreshed from the website. Run `python -m aksob_pubs sync`
  after editing if you do not run the full pipeline.
* `data/journal_overrides.csv` fixes venues the matcher cannot place: one row per venue name, with the
  ISSN (best) or a forced quartile (`Q1`..`Q4`) or `N/A` for non-journals. *Needs Review* lists exactly
  what to add.
* Re-rank after changing overrides: `python -m aksob_pubs rank --rerank`, then `report` and `dashboard`.
* `python -m aksob_pubs status` shows counts and the last runs. `output/run.log` has details.

## How quartiles are decided

* **Quartile** is Scimago's *SJR Best Quartile*: the best quartile across the journal's subject
  categories, in the edition matching the paper's year (a 2026 paper uses the latest edition until the
  2026 edition exists). Per-category quartiles and the SJR score are kept too.
* **n/a** marks books, chapters, theses, working papers, conference proceedings and reports.
* **Unranked** means a journal-like venue not found in Scimago (usually not indexed in Scopus). Check it
  in *Needs Review* and add an override if needed.
* Fuzzy title matches (score at least 94/100) are accepted but listed in *Needs Review*. Set
  `FUZZY_THRESHOLD=100` in `.env` to disable them.

## Commands

```
python -m aksob_pubs roster [--no-profiles]     scrape the faculty list
python -m aksob_pubs sync                       load a hand-edited faculty.csv
python -m aksob_pubs resolve [--force]          OpenAlex author ids and ORCIDs
python -m aksob_pubs fetch [--source openalex,orcid] [--faculty slug,slug] [--limit N]
python -m aksob_pubs import-bibtex              import data/scholar_exports/*.bib
python -m aksob_pubs enrich [--force] [--limit N]
python -m aksob_pubs scimago                    download / refresh Scimago editions
python -m aksob_pubs rank [--rerank]
python -m aksob_pubs report
python -m aksob_pubs dashboard
python -m aksob_pubs run [--skip-roster] [--source ...] [--faculty ...] [--rerank]
python -m aksob_pubs status
```

## Layout

```
research-tracker/
  aksob_pubs/             the package (roster, sources/, bibtex, enrich, scimago, rank, report, dashboard, CLI)
  aksob_pubs/templates/   dashboard.html, the web app template
  data/faculty.csv        roster (edit ids / include here)
  data/journal_overrides.csv
  data/scholar_exports/   optional Google Scholar BibTeX exports
  data/aksob_pubs.db      SQLite: faculty, publications, lookup cache, run history
  data/scimago/           cached Scimago CSVs (not committed)
  output/                 AKSOB_Publications.xlsx, publications.csv, dashboard.html, run.log
  scripts/                run.ps1, schedule_windows.ps1
```

## Known limits

* OpenAlex occasionally splits one person into two author records; the tracker takes the record with the
  most works. If someone's count looks low, check their OpenAlex page and put the right id in
  `faculty.csv`.
* Items without a DOI (local journals, some chapters, working papers) are only found through ORCID or a
  Scholar export. They are almost never in ranked journals, so quartile counts are unaffected.
* Scimago covers journals indexed in Scopus. Other journals show as *Unranked*; decide them once in
  `journal_overrides.csv`.
* The roster depends on the layout of sb.lau.edu.lb. If the page changes, `roster` stops with a clear
  error and the previous `faculty.csv` keeps working.
