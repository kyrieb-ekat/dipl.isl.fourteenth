"""Confirming a nafnid match, and refreshing candidates without losing work.

Two things this pins down:

1. A confirmed match must actually *land* -- coordinates copied onto the
   place, and the losing siblings closed out. 04a writes five ranked
   candidates per place, so a place is one pick-one-of-five question, not
   five independent yes/nos; leaving the other four open both quintuples the
   queue and permits two contradictory 'same' verdicts on one place.
2. Re-running 04a must not erase decisions already recorded.
"""


def test_confirming_a_match_copies_coordinates_onto_the_place(freshdb, db, seed, query):
    place = seed.place("Akrar")
    cand = seed.place_candidate(place, rank=1, nafnid="N123",
                                lat=65.1234, lng=-21.5678, name_score=100.0)

    db.record_place_duplicate_decision(cand, "same")

    row = query("SELECT * FROM places WHERE place_pk = ?", (place,))[0]
    assert row["nafnid_id"] == "N123"
    assert row["coordinates_lat"] == 65.1234
    assert row["coordinates_long"] == -21.5678


def test_confirming_one_candidate_closes_out_its_siblings(freshdb, db, seed, query):
    place = seed.place("Akrar")
    chosen = seed.place_candidate(place, rank=1, nafnid="N1", lat=65.0, lng=-21.0)
    others = [seed.place_candidate(place, rank=r, nafnid=f"N{r}") for r in (2, 3, 4, 5)]

    db.record_place_duplicate_decision(chosen, "same")

    rows = {r["candidate_pk"]: r["decision"]
            for r in query("SELECT candidate_pk, decision FROM place_duplicate_candidates")}
    assert rows[chosen] == "same"
    assert all(rows[pk] == "different" for pk in others)


def test_confirming_a_match_marks_the_place_reviewed(freshdb, db, seed, query):
    place = seed.place("Akrar")
    cand = seed.place_candidate(place, rank=1, nafnid="N1", lat=65.0, lng=-21.0)

    db.record_place_duplicate_decision(cand, "same")

    assert query("SELECT review_status FROM places WHERE place_pk = ?",
                 (place,))[0]["review_status"] == "ok"


def test_sibling_closeout_does_not_overwrite_an_explicit_decision(freshdb, db, seed, query):
    """A reviewer's own verdict on another candidate outranks the automatic
    close-out -- we only fill in the ones still blank."""
    place = seed.place("Akrar")
    chosen = seed.place_candidate(place, rank=1, nafnid="N1", lat=65.0, lng=-21.0)
    already = seed.place_candidate(place, rank=2, nafnid="N2", decision="same")
    blank = seed.place_candidate(place, rank=3, nafnid="N3")

    db.record_place_duplicate_decision(chosen, "same")

    rows = {r["candidate_pk"]: r["decision"]
            for r in query("SELECT candidate_pk, decision FROM place_duplicate_candidates")}
    assert rows[already] == "same"
    assert rows[blank] == "different"


def test_confirming_does_not_touch_other_places(freshdb, db, seed, query):
    mine = seed.place("Akrar")
    theirs = seed.place("Akur")
    cand = seed.place_candidate(mine, rank=1, nafnid="N1", lat=65.0, lng=-21.0)
    other = seed.place_candidate(theirs, rank=1, nafnid="N9")

    db.record_place_duplicate_decision(cand, "same")

    assert query("SELECT decision FROM place_duplicate_candidates WHERE candidate_pk = ?",
                 (other,))[0]["decision"] == ""
    assert query("SELECT coordinates_lat FROM places WHERE place_pk = ?",
                 (theirs,))[0]["coordinates_lat"] is None


def test_existing_coordinates_are_not_overwritten(freshdb, db, seed, query):
    place = seed.place("Akrar", coordinates_lat=64.0, coordinates_long=-20.0)
    cand = seed.place_candidate(place, rank=1, nafnid="N1", lat=65.0, lng=-21.0)

    db.record_place_duplicate_decision(cand, "same")

    row = query("SELECT * FROM places WHERE place_pk = ?", (place,))[0]
    assert row["coordinates_lat"] == 64.0
    assert row["coordinates_long"] == -20.0


def test_two_confirmed_candidates_leave_the_place_ungeocoded(freshdb, db, seed, query):
    """Two 'same' verdicts on one place contradict each other, so neither
    candidate's coordinates may be used -- picking one would be arbitrary.
    Live instance: 'Mýrar' was confirmed against five farms across five
    sýslur spanning ~250km.
    """
    place = seed.place("Mýrar")
    first = seed.place_candidate(place, rank=1, nafnid="N1", lat=64.93, lng=-23.33)
    second = seed.place_candidate(place, rank=2, nafnid="N2", lat=63.51, lng=-18.33)

    db.record_place_duplicate_decision(first, "same")
    db.record_place_duplicate_decision(second, "same")

    row = query("SELECT * FROM places WHERE place_pk = ?", (place,))[0]
    assert row["coordinates_lat"] is None
    assert row["coordinates_long"] is None


def test_a_contradiction_does_not_discard_wikidata_coordinates(freshdb, db, seed, query):
    """Retracting contested coordinates must not take independent evidence
    with it -- 04_lookup_coords.py's Wikidata result is not a nafnid claim."""
    place = seed.place("Mýrar", coordinates_lat=64.10, coordinates_long=-22.10)
    first = seed.place_candidate(place, rank=1, nafnid="N1", lat=64.93, lng=-23.33)
    second = seed.place_candidate(place, rank=2, nafnid="N2", lat=63.51, lng=-18.33)

    db.record_place_duplicate_decision(first, "same")
    db.record_place_duplicate_decision(second, "same")

    row = query("SELECT * FROM places WHERE place_pk = ?", (place,))[0]
    assert row["coordinates_lat"] == 64.10
    assert row["coordinates_long"] == -22.10


def test_a_single_confirmation_still_geocodes_after_the_guard(freshdb, db, seed, query):
    place = seed.place("Staðarhraun")
    only = seed.place_candidate(place, rank=1, nafnid="N1", lat=64.741417, lng=-22.0)
    seed.place_candidate(place, rank=2, nafnid="N2", lat=63.0, lng=-18.0)

    db.record_place_duplicate_decision(only, "same")

    assert query("SELECT coordinates_lat FROM places WHERE place_pk = ?",
                 (place,))[0]["coordinates_lat"] == 64.741417


def test_marking_different_changes_nothing_on_the_place(freshdb, db, seed, query):
    place = seed.place("Akrar")
    cand = seed.place_candidate(place, rank=1, nafnid="N1", lat=65.0, lng=-21.0)
    sibling = seed.place_candidate(place, rank=2, nafnid="N2")

    db.record_place_duplicate_decision(cand, "different")

    row = query("SELECT * FROM places WHERE place_pk = ?", (place,))[0]
    assert row["nafnid_id"] == ""
    assert row["coordinates_lat"] is None
    assert query("SELECT decision FROM place_duplicate_candidates WHERE candidate_pk = ?",
                 (sibling,))[0]["decision"] == ""


# ---------------------------------------------------------------------------
# Re-running 04a must preserve recorded decisions
# ---------------------------------------------------------------------------

def test_refresh_preserves_a_recorded_decision(freshdb, db, seed, query):
    place = seed.place("Akrar", volume=1)
    seed.place_candidate(place, rank=1, nafnid="N1", decision="same",
                         candidate_name="Akrar", name_score=100.0)

    db.replace_place_duplicate_candidates(1, [
        {"place_pk": place, "candidate_rank": 1, "name_score": 100.0,
         "candidate_nafnid": "N1", "candidate_name": "Akrar"},
    ])

    rows = query("SELECT candidate_nafnid, decision FROM place_duplicate_candidates")
    assert len(rows) == 1
    assert rows[0]["decision"] == "same"


def test_refresh_updates_scores_on_undecided_rows(freshdb, db, seed, query):
    place = seed.place("Akrar", volume=1)
    seed.place_candidate(place, rank=1, nafnid="N1", name_score=80.0,
                         candidate_name="Akrar")

    db.replace_place_duplicate_candidates(1, [
        {"place_pk": place, "candidate_rank": 1, "name_score": 97.0,
         "candidate_nafnid": "N1", "candidate_name": "Akrar"},
    ])

    rows = query("SELECT name_score FROM place_duplicate_candidates")
    assert len(rows) == 1
    assert rows[0]["name_score"] == 97.0


def test_refresh_drops_candidates_no_longer_proposed(freshdb, db, seed, query):
    place = seed.place("Akrar", volume=1)
    seed.place_candidate(place, rank=1, nafnid="N1", candidate_name="Akrar")
    seed.place_candidate(place, rank=2, nafnid="N2", candidate_name="Akur")

    db.replace_place_duplicate_candidates(1, [
        {"place_pk": place, "candidate_rank": 1, "name_score": 100.0,
         "candidate_nafnid": "N1", "candidate_name": "Akrar"},
    ])

    rows = query("SELECT candidate_nafnid FROM place_duplicate_candidates")
    assert [r["candidate_nafnid"] for r in rows] == ["N1"]


def test_refresh_keeps_a_decided_row_even_if_no_longer_proposed(freshdb, db, seed, query):
    """A reviewer already ruled on this candidate; a re-scored 04a run that
    no longer surfaces it must not erase that judgement."""
    place = seed.place("Akrar", volume=1)
    seed.place_candidate(place, rank=2, nafnid="N2", candidate_name="Akur",
                         decision="different")

    db.replace_place_duplicate_candidates(1, [
        {"place_pk": place, "candidate_rank": 1, "name_score": 100.0,
         "candidate_nafnid": "N1", "candidate_name": "Akrar"},
    ])

    rows = {r["candidate_nafnid"]: r["decision"]
            for r in query("SELECT candidate_nafnid, decision FROM place_duplicate_candidates")}
    assert rows["N2"] == "different"
    assert rows["N1"] == ""


def test_refresh_only_touches_the_named_volume(freshdb, db, seed, query):
    mine = seed.place("Akrar", volume=1)
    theirs = seed.place("Akur", volume=4)
    seed.place_candidate(mine, rank=1, nafnid="N1")
    seed.place_candidate(theirs, rank=1, nafnid="N9")

    db.replace_place_duplicate_candidates(1, [])

    remaining = query("SELECT candidate_nafnid FROM place_duplicate_candidates")
    assert [r["candidate_nafnid"] for r in remaining] == ["N9"]
