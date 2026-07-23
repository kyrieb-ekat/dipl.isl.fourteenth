-- DI charter pipeline -- canonical SQLite schema.
--
-- Replaces the per-volume CSV files + the two independently-mutable
-- "authority" stores (person_names_authority.csv/place_names_authority.csv
-- vs. CHARTER_authority_file.xlsx's persons_authority/Places_Authority
-- sheets) that had already diverged in practice. One row per person/place,
-- distinguished by `status` rather than by which file it happens to sit in.
--
-- Applied via db.init_db(path). See migrate_to_sqlite.py for the one-time
-- population from the pre-existing CSV/xlsx sources.

PRAGMA foreign_keys = ON;

CREATE TABLE persons (
    person_pk         INTEGER PRIMARY KEY AUTOINCREMENT,
    display_id        TEXT NOT NULL UNIQUE,
    legacy_id         TEXT NOT NULL,
    source_volume     INTEGER,
    status            TEXT NOT NULL DEFAULT 'provisional'
                          CHECK (status IN ('provisional','canonical')),
    review_status     TEXT NOT NULL DEFAULT ''
                          CHECK (review_status IN ('', 'ok', 'add', 'skip')),
    canonical_name    TEXT NOT NULL,
    variant_names     TEXT NOT NULL DEFAULT '',
    wikidata_id       TEXT NOT NULL DEFAULT '',
    patronymic        TEXT NOT NULL DEFAULT '',
    occupation        TEXT NOT NULL DEFAULT '',
    title             TEXT NOT NULL DEFAULT '',
    floruit_start     INTEGER,
    floruit_end       INTEGER,
    gender            TEXT NOT NULL DEFAULT '',
    associated_places TEXT NOT NULL DEFAULT '',
    notes             TEXT NOT NULL DEFAULT '',
    sources           TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX ix_persons_source_legacy ON persons(source_volume, legacy_id);
CREATE INDEX ix_persons_canonical_name ON persons(canonical_name);
CREATE INDEX ix_persons_wikidata ON persons(wikidata_id) WHERE wikidata_id != '';
CREATE INDEX ix_persons_status ON persons(status);

CREATE TABLE places (
    place_pk             INTEGER PRIMARY KEY AUTOINCREMENT,
    display_id           TEXT NOT NULL UNIQUE,
    legacy_id            TEXT NOT NULL,
    source_volume        INTEGER,
    status               TEXT NOT NULL DEFAULT 'provisional'
                              CHECK (status IN ('provisional','canonical')),
    review_status        TEXT NOT NULL DEFAULT ''
                              CHECK (review_status IN ('', 'ok', 'add', 'skip', 'no_match')),
    canonical_name       TEXT NOT NULL,
    variant_names        TEXT NOT NULL DEFAULT '',
    place_type           TEXT NOT NULL DEFAULT '',
    coordinates_lat      REAL,
    coordinates_long     REAL,
    region               TEXT NOT NULL DEFAULT '',
    district             TEXT NOT NULL DEFAULT '',
    -- unifies "modern country" (CSV authority) and "modern_equivalent"
    -- (xlsx sheet / provisional CSV) -- same concept, two names today.
    modern_equivalent    TEXT NOT NULL DEFAULT '',
    wikidata_id          TEXT NOT NULL DEFAULT '',
    -- confirmed nafnid.is external id, mirrors wikidata_id; backfilled by
    -- db.record_place_duplicate_decision() when a candidate is confirmed 'same'.
    nafnid_id            TEXT NOT NULL DEFAULT '',
    geo_match_score      REAL,
    proposed_place_id    TEXT NOT NULL DEFAULT '',
    proposed_wikidata_id TEXT NOT NULL DEFAULT '',
    notes                TEXT NOT NULL DEFAULT '',
    sources              TEXT NOT NULL DEFAULT '',
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at           TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX ix_places_source_legacy ON places(source_volume, legacy_id);
CREATE INDEX ix_places_canonical_name ON places(canonical_name);
CREATE INDEX ix_places_wikidata ON places(wikidata_id) WHERE wikidata_id != '';
CREATE INDEX ix_places_status ON places(status);

-- No grantor_id/recipient_id/persons_by_role columns here -- those were
-- denormalized strings computed once by 05_export_csvs.py from
-- resolved_persons/resolved_locations, i.e. a second copy of the same info
-- charter_persons/charter_places stores properly below. Recreating them as
-- stored columns would rebuild a two-writer problem inside one table; they
-- become read-time query/view concerns in the export script instead.
CREATE TABLE charters (
    charter_pk             INTEGER PRIMARY KEY AUTOINCREMENT,
    charter_id_placeholder TEXT NOT NULL UNIQUE,
    volume                  INTEGER NOT NULL,
    sequence                INTEGER NOT NULL,
    shelfmark_auto          TEXT NOT NULL DEFAULT '',
    di_reference             TEXT NOT NULL DEFAULT '',
    -- kept as TEXT: real values include "1341", "1341-06-14", "1265/1449"
    date                     TEXT NOT NULL DEFAULT '',
    di_year                  INTEGER,
    date_uncertain           TEXT NOT NULL DEFAULT '',
    date_header              TEXT NOT NULL DEFAULT '',
    doc_type                 TEXT NOT NULL DEFAULT '',
    subject                  TEXT NOT NULL DEFAULT '',
    outcome                  TEXT NOT NULL DEFAULT '',
    scribe                   TEXT NOT NULL DEFAULT '',
    scribe_source            TEXT NOT NULL DEFAULT '',
    seal_info                TEXT NOT NULL DEFAULT '',
    language                 TEXT NOT NULL DEFAULT '',
    notes                    TEXT NOT NULL DEFAULT '',
    has_parse_error          INTEGER NOT NULL DEFAULT 0,
    -- maintained ONLY by db.rescan_review_flags(), never hand-set
    has_review_persons       INTEGER NOT NULL DEFAULT 0,
    has_review_places        INTEGER NOT NULL DEFAULT 0,
    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at               TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(volume, sequence)
);
CREATE INDEX ix_charters_volume ON charters(volume);

CREATE TABLE charter_persons (
    charter_person_pk      INTEGER PRIMARY KEY AUTOINCREMENT,
    charter_pk              INTEGER NOT NULL REFERENCES charters(charter_pk) ON DELETE CASCADE,
    ordinal                  INTEGER NOT NULL,
    role_category            TEXT NOT NULL DEFAULT '',
    qualifier                TEXT NOT NULL DEFAULT '',
    extracted_name           TEXT NOT NULL,
    person_pk                INTEGER REFERENCES persons(person_pk),
    match_score              REAL,
    resolution_state         TEXT NOT NULL DEFAULT 'resolved'
                                 CHECK (resolution_state IN ('resolved','pending_review','new')),
    review_match_person_pk   INTEGER REFERENCES persons(person_pk)
);
CREATE INDEX ix_charter_persons_charter ON charter_persons(charter_pk);
CREATE INDEX ix_charter_persons_person ON charter_persons(person_pk);

CREATE TABLE charter_places (
    charter_place_pk        INTEGER PRIMARY KEY AUTOINCREMENT,
    charter_pk                INTEGER NOT NULL REFERENCES charters(charter_pk) ON DELETE CASCADE,
    ordinal                    INTEGER NOT NULL,
    -- loc.writing | loc.hearing | loc.mentioned
    role                       TEXT NOT NULL DEFAULT '',
    region                     TEXT NOT NULL DEFAULT '',
    extracted_name             TEXT NOT NULL,
    place_pk                   INTEGER REFERENCES places(place_pk),
    match_score                REAL,
    resolution_state           TEXT NOT NULL DEFAULT 'resolved'
                                   CHECK (resolution_state IN ('resolved','pending_review','new')),
    review_match_place_pk      INTEGER REFERENCES places(place_pk)
);
CREATE INDEX ix_charter_places_charter ON charter_places(charter_pk);
CREATE INDEX ix_charter_places_place ON charter_places(place_pk);

-- Direct FK to the exact charter_persons/charter_places row this concerns --
-- replaces resolve_review_queue.py's fragile positional join by
-- (charter_filename, type) in file order.
CREATE TABLE review_queue_items (
    review_item_pk    INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type       TEXT NOT NULL CHECK (entity_type IN ('person','place')),
    charter_person_pk INTEGER REFERENCES charter_persons(charter_person_pk),
    charter_place_pk  INTEGER REFERENCES charter_places(charter_place_pk),
    charter_pk        INTEGER NOT NULL REFERENCES charters(charter_pk),
    extracted_name    TEXT NOT NULL,
    closest_match     TEXT NOT NULL DEFAULT '',
    match_pk          INTEGER,
    score             REAL,
    role_category     TEXT NOT NULL DEFAULT '',
    role              TEXT NOT NULL DEFAULT '',
    charter_date      TEXT NOT NULL DEFAULT '',
    decision          TEXT NOT NULL DEFAULT '' CHECK (decision IN ('', 'accept', 'reject')),
    outcome_pk        INTEGER,
    status            TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved')),
    resolved_at       TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK ( (entity_type = 'person' AND charter_person_pk IS NOT NULL AND charter_place_pk IS NULL)
         OR (entity_type = 'place'  AND charter_place_pk  IS NOT NULL AND charter_person_pk IS NULL) )
);
CREATE INDEX ix_review_queue_charter ON review_queue_items(charter_pk);
CREATE INDEX ix_review_queue_status ON review_queue_items(status);

CREATE TABLE person_duplicate_candidates (
    candidate_pk   INTEGER PRIMARY KEY AUTOINCREMENT,
    person_a_pk    INTEGER NOT NULL REFERENCES persons(person_pk),
    person_b_pk    INTEGER NOT NULL REFERENCES persons(person_pk),
    name_score     REAL NOT NULL,
    date_status    TEXT NOT NULL DEFAULT '',
    classification TEXT NOT NULL DEFAULT '',
    confidence     TEXT NOT NULL DEFAULT '',
    decision       TEXT NOT NULL DEFAULT '' CHECK (decision IN ('', 'same', 'different')),
    decided_at     TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (person_a_pk < person_b_pk),
    UNIQUE(person_a_pk, person_b_pk)
);

-- New vs. today's CSV: has a `decision` column (today's nafnid candidates
-- file has no confirmed-same signal at all -- see promote_to_authority.py's
-- _place_duplicate_status(), which is permanently 'warning' for that reason).
CREATE TABLE place_duplicate_candidates (
    candidate_pk      INTEGER PRIMARY KEY AUTOINCREMENT,
    place_pk          INTEGER NOT NULL REFERENCES places(place_pk),
    di_name           TEXT NOT NULL,
    di_sysla_given    TEXT NOT NULL DEFAULT '',
    di_place_type     TEXT NOT NULL DEFAULT '',
    di_region         TEXT NOT NULL DEFAULT '',
    wikidata_status   TEXT NOT NULL DEFAULT '',
    candidate_rank    INTEGER,
    name_score        REAL,
    distance_km       REAL,
    flag              TEXT NOT NULL DEFAULT '',
    match_sources     TEXT NOT NULL DEFAULT '',
    candidate_name    TEXT NOT NULL DEFAULT '',
    candidate_nafnid  TEXT NOT NULL DEFAULT '',
    candidate_hreppur TEXT NOT NULL DEFAULT '',
    candidate_sysla   TEXT NOT NULL DEFAULT '',
    candidate_lat     REAL,
    candidate_lng     REAL,
    decision          TEXT NOT NULL DEFAULT '' CHECK (decision IN ('', 'same', 'different')),
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX ix_place_dup_place ON place_duplicate_candidates(place_pk);

PRAGMA user_version = 1;
