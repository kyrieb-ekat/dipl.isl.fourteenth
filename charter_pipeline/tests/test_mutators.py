"""Characterisation tests for the rest of db.py's mutators.

These pin current behaviour rather than asserting it is ideal. Their job is
to fail loudly if a later change (soft-delete merges, the WAL switch, the
propose-only write gate) alters something by accident. Where current
behaviour looks questionable it is recorded as-is with a comment, not
"fixed" here.
"""
import pytest


# ---------------------------------------------------------------------------
# Field updates
# ---------------------------------------------------------------------------

def test_update_person_sets_fields_and_bumps_updated_at(freshdb, db, seed, query):
    pk = seed.person("Jón Sigurðsson")
    before = query("SELECT updated_at FROM persons WHERE person_pk = ?", (pk,))[0]

    db.update_person(pk, occupation="lawman", review_status="add")

    row = query("SELECT * FROM persons WHERE person_pk = ?", (pk,))[0]
    assert row["occupation"] == "lawman"
    assert row["review_status"] == "add"
    assert row["updated_at"] >= before["updated_at"]


def test_update_place_sets_fields(freshdb, db, seed, query):
    pk = seed.place("Akrar")
    db.update_place(pk, region="Snæfellsnes", review_status="no_match")
    row = query("SELECT * FROM places WHERE place_pk = ?", (pk,))[0]
    assert row["region"] == "Snæfellsnes"
    assert row["review_status"] == "no_match"


def test_update_person_rejects_an_invalid_review_status(freshdb, db, seed):
    pk = seed.person("Jón Sigurðsson")
    with pytest.raises(Exception):
        db.update_person(pk, review_status="bogus")


# ---------------------------------------------------------------------------
# Place merges (no pair-table constraints, unlike persons)
# ---------------------------------------------------------------------------

def test_merge_places_unions_and_relinks(freshdb, db, seed, query):
    a = seed.place("Akrar", region="Snæfellsnes")
    b = seed.place("Akur")
    ch = seed.charter(volume=1, sequence=1)
    conn = db.get_connection()
    with conn:
        conn.execute("INSERT INTO charter_places "
                     "(charter_pk, place_pk, ordinal, role, extracted_name) "
                     "VALUES (?, ?, 1, 'loc.mentioned', 'Akur')", (ch, b))
    conn.close()

    db.merge_places(a, [b])

    assert [r["place_pk"] for r in query("SELECT place_pk FROM places")] == [a]
    assert query("SELECT place_pk FROM charter_places")[0]["place_pk"] == a
    assert "Akur" in query("SELECT variant_names FROM places")[0]["variant_names"]


def test_merge_places_keeps_first_non_null_coordinates(freshdb, db, seed, query):
    a = seed.place("Akrar")
    b = seed.place("Akur", coordinates_lat=65.0, coordinates_long=-21.0)

    db.merge_places(a, [b])

    row = query("SELECT * FROM places")[0]
    assert row["coordinates_lat"] == 65.0


def test_merge_records_the_folded_in_volume(freshdb, db, seed, query):
    a = seed.person("Jón Sigurðsson", volume=1)
    b = seed.person("Jon Sigurdsson", volume=4)

    db.merge_persons(a, [b])

    row = query("SELECT source_volume, merged_volumes FROM persons")[0]
    # source_volume is deliberately never overwritten; merged_volumes is the trail
    assert row["source_volume"] == 1
    assert "4" in row["merged_volumes"]


# ---------------------------------------------------------------------------
# Person duplicate-candidate upsert
# ---------------------------------------------------------------------------

def test_upsert_person_candidates_never_overwrites_a_decision(freshdb, db, seed, query):
    a = seed.person("Jón Sigurðsson")
    b = seed.person("Jon Sigurdsson")
    seed.person_pair(a, b, name_score=90.0, decision="different")

    db.upsert_person_duplicate_candidates(
        [{"person_a_pk": a, "person_b_pk": b, "name_score": 99.0,
          "classification": "likely_duplicate", "confidence": "high"}])

    row = query("SELECT decision, name_score FROM person_duplicate_candidates")[0]
    assert row["decision"] == "different"
    assert row["name_score"] == 90.0  # the guarded UPDATE skips decided rows entirely


def test_upsert_person_candidates_normalises_pair_order(freshdb, db, seed, query):
    a = seed.person("Jón Sigurðsson")
    b = seed.person("Jon Sigurdsson")

    db.upsert_person_duplicate_candidates(
        [{"person_a_pk": b, "person_b_pk": a, "name_score": 95.0}])

    row = query("SELECT person_a_pk, person_b_pk FROM person_duplicate_candidates")[0]
    assert row["person_a_pk"] == min(a, b)
    assert row["person_b_pk"] == max(a, b)


def test_record_person_duplicate_decision_is_flag_only(freshdb, db, seed, query):
    """Deliberate design: marking 'same' here never merges or relinks."""
    a = seed.person("Jón Sigurðsson")
    b = seed.person("Jon Sigurdsson")
    cand = seed.person_pair(a, b)

    db.record_person_duplicate_decision(cand, "same")

    assert len(query("SELECT * FROM persons")) == 2
    assert query("SELECT decision FROM person_duplicate_candidates")[0]["decision"] == "same"


# ---------------------------------------------------------------------------
# Review queue
# ---------------------------------------------------------------------------

def test_set_review_decision_records_without_applying(freshdb, db, seed, query):
    ch = seed.charter(volume=1, sequence=1)
    match = seed.person("Jón Sigurðsson", status="canonical")
    conn = db.get_connection()
    with conn:
        cp = conn.execute(
            "INSERT INTO charter_persons (charter_pk, ordinal, extracted_name, resolution_state) "
            "VALUES (?, 1, 'Jone Sigurdsson', 'pending_review')", (ch,)).lastrowid
    conn.close()
    item = db.create_review_item("person", ch, "Jone Sigurdsson", match, 72.0,
                                 charter_person_pk=cp)

    db.set_review_decision(item, "accept")

    row = query("SELECT decision, status, outcome_pk FROM review_queue_items")[0]
    assert row["decision"] == "accept"
    assert row["status"] == "open"      # not applied yet
    assert row["outcome_pk"] is None
    assert query("SELECT person_pk FROM charter_persons")[0]["person_pk"] is None


def test_apply_review_decision_accept_links_the_match(freshdb, db, seed, query):
    ch = seed.charter(volume=1, sequence=1)
    match = seed.person("Jón Sigurðsson", status="canonical")
    conn = db.get_connection()
    with conn:
        cp = conn.execute(
            "INSERT INTO charter_persons (charter_pk, ordinal, extracted_name, resolution_state) "
            "VALUES (?, 1, 'Jone Sigurdsson', 'pending_review')", (ch,)).lastrowid
    conn.close()
    item = db.create_review_item("person", ch, "Jone Sigurdsson", match, 72.0,
                                 charter_person_pk=cp)
    db.set_review_decision(item, "accept")

    db.apply_review_decision(item)

    assert query("SELECT person_pk FROM charter_persons")[0]["person_pk"] == match
    row = query("SELECT status, outcome_pk FROM review_queue_items")[0]
    assert row["status"] == "resolved"
    assert row["outcome_pk"] == match


def test_apply_review_decision_reject_mints_a_new_person(freshdb, db, seed, query):
    """Rejecting creates a brand-new provisional entity -- the reason a bulk
    reject pass is partly a transfer of work rather than an elimination."""
    ch = seed.charter(volume=1, sequence=1)
    match = seed.person("Jón Sigurðsson", status="canonical")
    conn = db.get_connection()
    with conn:
        cp = conn.execute(
            "INSERT INTO charter_persons (charter_pk, ordinal, extracted_name, resolution_state) "
            "VALUES (?, 1, 'Ormur Loftsson', 'pending_review')", (ch,)).lastrowid
    conn.close()
    item = db.create_review_item("person", ch, "Ormur Loftsson", match, 63.0,
                                 charter_person_pk=cp)
    db.set_review_decision(item, "reject")

    db.apply_review_decision(item)

    persons = query("SELECT person_pk, canonical_name FROM persons ORDER BY person_pk")
    assert len(persons) == 2
    minted = persons[-1]
    assert minted["canonical_name"] == "Ormur Loftsson"
    assert query("SELECT person_pk FROM charter_persons")[0]["person_pk"] == minted["person_pk"]


def test_apply_review_decision_skips_a_blank_decision(freshdb, db, seed):
    ch = seed.charter(volume=1, sequence=1)
    match = seed.person("Jón Sigurðsson", status="canonical")
    conn = db.get_connection()
    with conn:
        cp = conn.execute(
            "INSERT INTO charter_persons (charter_pk, ordinal, extracted_name, resolution_state) "
            "VALUES (?, 1, 'x', 'pending_review')", (ch,)).lastrowid
    conn.close()
    item = db.create_review_item("person", ch, "x", match, 70.0, charter_person_pk=cp)

    assert db.apply_review_decision(item).get("skipped") is True


# ---------------------------------------------------------------------------
# Promotion
# ---------------------------------------------------------------------------

def test_promote_persons_batch_flips_status(freshdb, db, seed, query):
    pk = seed.person("Jón Sigurðsson", review_status="add")
    db.promote_persons_batch([pk])
    assert query("SELECT status FROM persons")[0]["status"] == "canonical"


def test_final_review_candidates_only_include_add(freshdb, db, seed):
    seed.person("Accepted", review_status="add")
    seed.person("Untouched", review_status="")
    seed.person("Skipped", review_status="skip")

    names = {c["canonical_name"] for c in db.get_final_review_candidates()}
    assert names == {"Accepted"}


def test_final_review_blocks_a_confirmed_duplicate(freshdb, db, seed):
    a = seed.person("Jón Sigurðsson", review_status="add")
    b = seed.person("Jon Sigurdsson", review_status="add")
    cand = seed.person_pair(a, b)
    db.record_person_duplicate_decision(cand, "same")

    statuses = {c["canonical_name"]: c["duplicate_status"]
                for c in db.get_final_review_candidates()}
    assert statuses["Jón Sigurðsson"] == "blocked"


# ---------------------------------------------------------------------------
# Authority search + its module-level cache
# ---------------------------------------------------------------------------

def test_search_authority_finds_a_canonical_match(freshdb, db, seed):
    seed.person("Jón Sigurðsson", status="canonical")
    hits = db.search_authority("person", "Jon Sigurdsson")
    assert hits and hits[0]["canonical_name"] == "Jón Sigurðsson"
    assert hits[0]["_match_score"] > 70


def test_search_authority_ignores_provisional_rows(freshdb, db, seed):
    seed.person("Jón Sigurðsson", status="provisional")
    assert db.search_authority("person", "Jon Sigurdsson") == []


def test_authority_cache_is_invalidated_by_a_write(freshdb, db, seed):
    pk = seed.person("Jón Sigurðsson", status="canonical")
    db.search_authority("person", "Jon")           # populates the cache

    db.update_person(pk, canonical_name="Jón Loftsson")

    hits = db.search_authority("person", "Jón Loftsson")
    assert hits[0]["canonical_name"] == "Jón Loftsson"


def test_merge_invalidates_the_authority_cache(freshdb, db, seed):
    a = seed.person("Jón Sigurðsson", status="canonical")
    b = seed.person("Jon Sigurdsson", status="canonical")
    assert len(db.search_authority("person", "Jon Sigurdsson", limit=5)) == 2

    db.merge_persons(a, [b])

    assert len(db.search_authority("person", "Jon Sigurdsson", limit=5)) == 1
