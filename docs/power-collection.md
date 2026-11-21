# Power-plant paper archive

`tmp/collect_papers.py` is a local archive job. It is not part of the installed Findpapers package. The `tmp/` directory is listed in `.gitignore`, so the script exists only in a working copy that has it.

The job searches for papers on hydropower and thermal power plants, keeps a paper only when a valid PDF is saved, and stores that PDF next to a JSON metadata file. Papers are kept from 2010-01-01 onward.

## Run

From the repository root, with the project virtual environment:

```bash
.venv/Scripts/python.exe -u tmp/collect_papers.py
```

On Unix, use `.venv/bin/python -u tmp/collect_papers.py`.

The archive root is `D:\20260929_Power`, set by `OUTPUT_ROOT` in the script. Change that path before running on another machine.

## What is searched

Each entry in the script is one technology-purpose pair. Phrases stay at three words or fewer so PubMed can still match them. The pairs are not merged into one `OR` query.

`engine.search()` uses:

| Setting | Value |
|---------|-------|
| Databases | `arxiv`, `pubmed`, `semantic_scholar` |
| `since` | `2010-01-01` |
| Enrichment | `crossref` |
| Workers | `3` |

`FINISHED_SEARCHES` holds the first batch (hydropower equipment, turbine types, and thermal power plants: efficiency, output, optimization, retrofit, modernization, maintenance, digital twin). `main()` does not run that batch, so papers that failed to download in it are not retried.

`SEARCHES` is the batch `main()` runs. It adds vocabulary that was not in the first batch: hydroelectric plants, pumped storage, pump turbines, water turbines, runner blades, draft tubes, guide vanes, combined cycle, coal-fired and gas-turbine plants, supercritical plants, feedwater, and the British spellings `optimisation` and `modernisation`.

A search result is cached under `OUTPUT_ROOT/_state/` only when every requested database succeeded. The cache key includes the query text, `since`, and the database list. Editing a query, or leaving `failed_databases` non-empty, causes that search to run again.

## What is stored

A paper is written only after its PDF is at least 1024 bytes and starts with `%PDF-`. HTML error pages and empty files are discarded.

```
D:\20260929_Power\
├── YYYY-MM\
│   └── DD\
│       ├── d<doi-hash>.pdf
│       └── d<doi-hash>.json
├── unknown\unknown\          # papers with no publication date
└── _state\
    ├── no_pdf.json           # stems that had no download URL
    └── <search>_<token>.json # completed search cache
```

The file stem is a hash of the lowercased DOI. A paper with no DOI uses a hash of the title instead (`t` plus the hash). The JSON file records the search name, the query, the PDF file name, and `paper.to_dict()`.

Papers are attempted newest first. The seed paper's publication date picks the `YYYY-MM/DD` folder.

## Duplicates

The same DOI always maps to the same file name. If that PDF is already valid, the script counts it as already saved and does not download it again. This applies across search names, including papers saved by `FINISHED_SEARCHES`.

`no_pdf.json` is also global. A paper with no URL is skipped for every later query.

Two cases can still produce two files:

- The same DOI is seen with two different publication dates, so the folders differ.
- A paper has no DOI and the title text differs, so the title hash differs.

The JSON keeps the search name of the run that first wrote the file. A later overlapping query does not add its name to that JSON.

## Download outcomes

`engine.download()` does not raise when a publisher returns no PDF. The script reads `download_log.txt` in a temporary directory and classifies the paper:

| Outcome | Meaning |
|---------|---------|
| `saved` | A valid PDF and its JSON were written |
| `already` | A valid PDF for this DOI (or title) was already on disk |
| `no_pdf` | The downloader had no URL. The stem is stored in `no_pdf.json` and later runs skip it |
| `retry` | A URL was tried and the download failed (timeout, block, or paywall). The stem is not recorded, so a later run tries again |
| `failed` | The search or the store step raised an exception |

A run can finish with `failures=0` while many papers are `no_pdf` or `retry`. That means every active query was processed, not that every hit has a PDF.
