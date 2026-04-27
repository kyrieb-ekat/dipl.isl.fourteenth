This project exists as a way to scan output transcriptions of the Diplomatarium Islandicum's charters from the fourteenth century (ish, 1275–1406) to support the end goal extraction of all names, positions/occupations, places, dates, mentioned persons and places, and purpose of the charters' writing into a spreadsheet.

Final format goal is for upload to nodegoat, which will require the locking in of precise coordinate data with the extracted locations.

Once uploaded with location data, visualize through nodegoat.

---

## Pipeline Walkthrough

All scripts live in `charter_pipeline/`. Edit `config.py` first to set your PDF directory, output path, and authority XLSX path.

### One-time setup

Before running the pipeline on a new machine or after a fresh clone:

```bash
cd charter_pipeline

# Seed the flat place authority CSV from the master XLSX
python3 seed_place_names.py

# Seed the flat person authority CSV from the master XLSX
python3 seed_person_names.py
```

These create `place_names_authority.csv` and `person_names_authority.csv` alongside the scripts. They are safe to re-run with `--overwrite` if the XLSX has changed significantly.

---

### Step 1 — Extract charter text from PDF

```bash
python3 01_extract_text.py --pdf ~/Desktop/Charters/pdfs/Bindi_4.pdf --vol 4
```

Splits the PDF into one `.txt` file per charter, saved to `output/segments/vol04/`. Also writes `charter_index.csv` mapping each file to its volume, sequence number, and date header. Run once per volume; re-running overwrites existing segments.

---

### Step 2 — Extract entities with Claude API

```bash
# Process all charters at once
python3 02_extract_entities.py --vol 4

# Or process in batches (results append incrementally)
python3 02_extract_entities.py --vol 4 --start 1 --end 100
python3 02_extract_entities.py --vol 4 --start 101 --end 200
```

Sends each charter segment to the Claude API and extracts structured JSON (persons, places, dates, document type, subject). Results are appended to `output/entities/vol04_raw_entities.json` after each batch, so you can pause and resume. Requires `ANTHROPIC_API_KEY` in your environment.

---

### Step 3 — Resolve entities against authority files

```bash
python3 03_resolve_entities.py --vol 4
```

Fuzzy-matches extracted names against the authority XLSX and the flat authority CSVs:

- **Score ≥ 85**: auto-assigned an existing ID
- **Score 60–84**: flagged `REVIEW:{matched_id}` — written to `output/review/vol04_review_queue.csv` for manual inspection
- **Score < 60**: treated as a new entity, assigned a temporary ID (`p###` / `l###`)

Writes `output/entities/vol04_resolved_entities.json` and `output/review/vol04_review_queue.csv`.

---

### Step 4 — Geocode new places via Wikidata

```bash
python3 04_lookup_coords.py --vol 4
```

Queries Wikidata SPARQL for coordinates of places that don't yet have them. Writes `output/review/vol04_places_new_geocoded.csv` with added `wikidata_id` and `geo_match_score` columns. Places with a fuzzy score below 70 are left blank for manual lookup. Only run after Step 3.

---

### Step 4b — Reconcile place names against the authority

```bash
# Annotate first (writes proposed changes into the CSV, does not apply them)
python3 04b_propagate_corrections.py --csv output/review/vol04_places_new_geocoded.csv --annotate

# Or preview in the terminal without touching the file
python3 04b_propagate_corrections.py --csv output/review/vol04_places_new_geocoded.csv --dry-run
```

The `--annotate` flag adds three columns to the CSV without changing any data:

| Column | Meaning |
|--------|---------|
| `proposed_place_id` | Authority place_id that would be assigned |
| `proposed_wikidata_id` | Authority Wikidata QID that would be assigned |
| `review_status` | Blank for matched rows; `no_match` for unmatched |

**Open the CSV in a spreadsheet (or the review app's New Entities > Places tab) and fill in `review_status` for each row:**

| Value | Effect when applied |
|-------|-------------------|
| *(blank)* | Apply the proposed change (default) |
| `ok` | Apply the proposed change |
| `skip` | Leave the row unchanged |
| `add` | Apply the change AND flag this row for promotion into the authority |
| `no_match` | Set by `--annotate`; leave blank or fill in manually |

Then apply your decisions:

```bash
python3 04b_propagate_corrections.py --csv output/review/vol04_places_new_geocoded.csv
```

---

### Step 4c — Promote new places into the authority

```bash
# Preview first
python3 04c_add_to_authority.py --csv output/review/vol04_places_new_geocoded.csv --dry-run

# Then write
python3 04c_add_to_authority.py --csv output/review/vol04_places_new_geocoded.csv
```

Appends rows tagged `review_status=add` to `place_names_authority.csv`. Skips any `place_id` already in the authority. Creates a `.bak` backup before writing. The new entries will be picked up automatically on the next run of Step 3 or 4b.

---

### Step 5 — Export review CSVs

```bash
python3 05_export_csvs.py --vol 4
```

Reads `output/entities/vol04_resolved_entities.json` and writes four CSVs to `output/review/`:

- `vol04_charters.csv` — one row per charter; charters with unresolved `REVIEW:` flags are marked and will be skipped by Step 6
- `vol04_persons_new.csv` — candidate new persons to add to the authority
- `vol04_places_new.csv` — candidate new places to add to the authority
- `vol04_review_queue.csv` — ambiguous fuzzy matches (score 60–84) for manual inspection

**Review these files before proceeding** using the review app (see below) or manually in a spreadsheet.

---

### Review app

The review app provides a browser UI for working through the outputs of Step 5. Launch it from the `charter_pipeline/` directory:

```bash
conda activate dic
streamlit run review_app.py
# opens at http://localhost:8501
```

The app has three tabs:

**Review Queue** — lists all fuzzy matches (score 60–84) from `vol_review_queue.csv`. Set **Decision** for each row:

| Value | Effect |
|-------|--------|
| `accept` | Use the proposed authority ID |
| `reject` | Treat as a new entity (will get a fresh ID) |

**New Entities** — sub-tabs for Persons and Places. Inline-edit canonical names, Wikidata IDs, coordinates, and other fields. Set **Status** per row:

| Value | Effect |
|-------|--------|
| `ok` | Include in charter data |
| `skip` | Exclude entirely |
| `add` | Include AND promote to the authority file |

Wikidata IDs entered here become clickable links so you can verify them without leaving the app.

**Authority Browser** — searchable read-only view of both authority CSVs, useful as a reference while triaging.

All edits auto-save to the underlying CSVs immediately. The sidebar volume selector auto-detects whatever volumes have been processed, so switching between volumes requires no configuration.

---

### Step 5b — Clear charter review flags after manual resolution

If you have edited `charters.csv` to replace `REVIEW:xxx` prefixes with real IDs:

```bash
python3 05b_rescan_flags.py --vol 4
```

Re-reads `vol04_charters.csv`, checks which rows still contain `REVIEW:` in any ID column, and rewrites `_has_review_persons` and `_has_review_places` accordingly. Charters where all flags are cleared will be picked up by Step 6. Run this after any manual resolution in the charters CSV.

---

### Step 4d — Promote new persons into the authority

```bash
# Preview
python3 04d_add_to_person_authority.py --csv output/review/vol04_persons_new.csv --dry-run

# Write
python3 04d_add_to_person_authority.py --csv output/review/vol04_persons_new.csv
```

Same pattern as 4c — appends `review_status=add` persons to `person_names_authority.csv`. Skips existing `person_id`s and creates a `.bak` backup.

---

### Step 6 — Merge into the authority XLSX

```bash
# Preview row counts
python3 06_merge_into_xlsx.py --vol 4 --dry-run

# Write
python3 06_merge_into_xlsx.py --vol 4
```

Merges approved charters, persons, and places into a copy of the authority XLSX (`CHARTER_authority_file_updated.xlsx`). The original is never modified.

- **Charters**: rows with `_has_review_persons=Y`, `_has_review_places=Y`, or `_has_parse_error=Y` are skipped
- **Persons**: rows with `review_status=skip` are skipped; blank/ok/add rows are merged
- **Places**: all rows in `places_new.csv` are merged (use 4c to manage the flat authority separately)

Also writes `output/review/vol04_nodegoat_export.csv` — a flattened CSV for import into nodegoat.

---

### Full run sequence (reference)

```bash
# One-time (per machine)
python3 seed_place_names.py
python3 seed_person_names.py

# Per volume
python3 01_extract_text.py --pdf ~/Desktop/Charters/pdfs/Bindi_4.pdf --vol 4
python3 02_extract_entities.py --vol 4 --start 1 --end 100   # repeat for more batches
python3 03_resolve_entities.py --vol 4
python3 04_lookup_coords.py --vol 4
python3 04b_propagate_corrections.py --csv output/review/vol04_places_new_geocoded.csv --annotate
# → review CSV, fill in review_status
python3 04b_propagate_corrections.py --csv output/review/vol04_places_new_geocoded.csv
python3 04c_add_to_authority.py --csv output/review/vol04_places_new_geocoded.csv
python3 05_export_csvs.py --vol 4
# → open the review app (streamlit run review_app.py) to triage review_queue.csv,
#   persons_new.csv, and places_new.csv; then resolve any REVIEW: prefixes in charters.csv
python3 05b_rescan_flags.py --vol 4
python3 04d_add_to_person_authority.py --csv output/review/vol04_persons_new.csv
python3 06_merge_into_xlsx.py --vol 4
```