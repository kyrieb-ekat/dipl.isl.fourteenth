# nafnid.is place-name reconciliation

Supplementary place-name geocoding/verification source for the DI charter
pipeline, alongside the Wikidata SPARQL lookup in `04_lookup_coords.py`.
Pulls and reconciles data from [nafnid.is](https://nafnid.is), the Árnastofnun
(Árni Magnússon Institute) place-name registry for Iceland.

## Data provenance and usage caveat

`pull_nafnid.py` and `pull_endpoint.py` hit `nafnid.arnastofnun.is` directly
(the Django REST API backing the site), not the `nafnid.is` front end, which
disallows automated access via `robots.txt`. **Treat any pulled data as a
personal working copy for reconciliation/research use only until terms are
confirmed with Árnastofnun's onomastics department.** Both scripts are
polite by design (single-threaded, sequential, `DELAY_SECONDS = 1.0` between
paginated requests) but that does not substitute for a terms-of-use check
before any wider use or redistribution of this data.

## Layout

```
nafnid/
├── data/                  canonical pulls (see below for provenance)
│   ├── baeir.csv/.json    farm/settlement records — the main geocoding source
│   ├── nafnid.csv/.json   flat geoleit "farm"-type pull
│   ├── farm.csv/.json     same schema as nafnid.csv, earlier pull
│   ├── kirkja_search.csv/.json   textaleit search results for "kirkja"
│   └── _archive/          superseded pulls, kept for history, not read by anything
├── lookup_tables/
│   ├── diocese.csv        diocese → constituent sýslur (hand-authored, low/medium confidence — verify against a proper source, e.g. Kålund, before relying on it)
│   ├── quarters.csv       fjórðungur (quarter) → constituent sýslur
│   ├── sysla_abbrevs.csv  DI-style abbreviation ↔ modern sýsla name(s); handles the 1907 sýsla splits
│   └── valley_to_hreppur.py   matches a DI valley/fjord name against nafnid hreppur names by substring
├── pull_nafnid.py         pulls the geoleit endpoint by `type` (farm, natural, place, ...)
├── pull_endpoint.py       generic paginated puller for any nafnid REST endpoint
├── json_to_csv.py         repairs truncated/copy-pasted devtools JSON → CSV
└── find_tegundir.py, find_tegundir_flat.py   taxonomy-discovery helpers for place "tegund" categories
```

`lookup_tables/major_sites.csv` from the original prototype was empty (0
bytes) and was dropped during integration rather than carried forward as
dead weight — TODO: fill in or formally drop the idea.

## Data consolidation note

The original prototype accumulated three copies of `baeir.csv` (201 / 5,537 /
8,603 rows) because the pull scripts wrote wherever the working directory
happened to be at the time. `data/baeir.csv` (8,603 rows) is the freshest,
largest pull and is the one everything in this pipeline reads. The smaller,
earlier 201-row pull is kept at `data/_archive/baeir_201rows_2026-earlier-pull.csv`
for reference only — nothing here reads from `_archive/`.

## Reconciliation

`04a_reconcile_nafnid.py` (one level up, in `charter_pipeline/`) is the
refactored version of the original `reconcile.py` prototype. It fuzzy-matches
DI place mentions that Wikidata couldn't confidently geocode against
`data/baeir.csv`, blocking candidates by sýsla before scoring
(`rapidfuzz.fuzz.WRatio`) to cut down on false positives from repeated farm
names (Aðalból, Garður, Gafl, etc. all appear more than once nationally).
It never auto-accepts a match — output is a review CSV with a blank
`decision` column for manual triage, promoted into the place authority via
the existing `04c_add_to_authority.py` path.
