"""Merge semantics for persons.

The interesting cases here are all about person_duplicate_candidates, which
carries `CHECK (person_a_pk < person_b_pk)` and `UNIQUE(person_a_pk,
person_b_pk)`. Relinking a merged-away pk into that table can violate both:
a pair merged with its own counterpart collapses to (a, a), and any third
person holding edges to *both* sides collapses to two identical rows. At the
time these tests were written, 15,751 of the 15,862 live candidate pairs
shared a third person, so the second case is the common one, not the corner.
"""
import pytest


def test_merge_pair_unions_variant_names_and_keeps_lowest_pk(freshdb, db, seed, query):
    a = seed.person("Jón Sigurðsson", floruit_start=1340, floruit_end=1350)
    b = seed.person("Jon Sigurdsson", floruit_start=1345, floruit_end=1360)

    db.merge_persons(a, [b])

    rows = query("SELECT * FROM persons ORDER BY person_pk")
    assert len(rows) == 1
    assert rows[0]["person_pk"] == a
    assert "Jon Sigurdsson" in rows[0]["variant_names"]
    # floruit widens to cover both
    assert rows[0]["floruit_start"] == 1340
    assert rows[0]["floruit_end"] == 1360


def test_merge_relinks_charter_references(freshdb, db, seed, query):
    a = seed.person("Jón Sigurðsson")
    b = seed.person("Jon Sigurdsson")
    ch = seed.charter(volume=1, sequence=1)
    seed.charter_person(ch, a, ordinal=1)
    seed.charter_person(ch, b, ordinal=2)

    db.merge_persons(a, [b])

    links = query("SELECT person_pk FROM charter_persons ORDER BY ordinal")
    assert [r["person_pk"] for r in links] == [a, a]


def test_merge_pair_that_has_its_own_candidate_row(freshdb, db, seed, query):
    """The CHECK case: merging the two sides of candidate pair (a, b) would
    rewrite that row to (a, a), violating person_a_pk < person_b_pk."""
    a = seed.person("Jón Sigurðsson")
    b = seed.person("Jon Sigurdsson")
    seed.person_pair(a, b, name_score=97.0)

    db.merge_persons(a, [b])

    assert query("SELECT * FROM persons") != []
    # the pair is now internal to one entity, so it should be gone entirely
    assert query("SELECT * FROM person_duplicate_candidates") == []


def test_merge_three_clique(freshdb, db, seed, query):
    """The UNIQUE case, and the one the plan calls out explicitly: three
    mutually-linked persons, all folded into the lowest pk."""
    a = seed.person("Jón Sigurðsson")
    b = seed.person("Jon Sigurdsson")
    c = seed.person("Jónn Sigurðson")
    seed.person_pair(a, b)
    seed.person_pair(a, c)
    seed.person_pair(b, c)

    db.merge_persons(a, [b, c])

    persons = query("SELECT person_pk FROM persons")
    assert [r["person_pk"] for r in persons] == [a]
    assert query("SELECT * FROM person_duplicate_candidates") == []


def test_merge_dedupes_edges_to_a_shared_third_person(freshdb, db, seed, query):
    """A and B both have an edge to C. Merging A+B must leave exactly one
    (survivor, C) edge, not two identical rows."""
    a = seed.person("Jón Sigurðsson")
    b = seed.person("Jon Sigurdsson")
    c = seed.person("Þórunn Jónsdóttir")
    seed.person_pair(a, b)
    seed.person_pair(a, c, name_score=80.0)
    seed.person_pair(b, c, name_score=84.0)

    db.merge_persons(a, [b])

    edges = query(
        "SELECT person_a_pk, person_b_pk FROM person_duplicate_candidates")
    assert len(edges) == 1
    assert edges[0] == {"person_a_pk": min(a, c), "person_b_pk": max(a, c)}


def test_merge_keeps_a_recorded_decision_on_a_surviving_edge(freshdb, db, seed, query):
    """A reviewer's 'different' judgement about C must survive the merge --
    dropping it would silently resurface the pair in the queue."""
    a = seed.person("Jón Sigurðsson")
    b = seed.person("Jon Sigurdsson")
    c = seed.person("Þórunn Jónsdóttir")
    seed.person_pair(a, b)
    seed.person_pair(b, c, name_score=84.0, decision="different")

    db.merge_persons(a, [b])

    edges = query("SELECT person_a_pk, person_b_pk, decision "
                  "FROM person_duplicate_candidates")
    assert len(edges) == 1
    assert edges[0]["decision"] == "different"


def test_merge_ordering_is_normalised_when_survivor_is_higher(freshdb, db, seed, query):
    """merge_into_authority can pick a survivor with a *higher* pk than the
    dropped row's partner, which flips the required (a < b) ordering."""
    low = seed.person("Þórunn Jónsdóttir")        # will be the third party
    dropped = seed.person("Jon Sigurdsson")
    survivor = seed.person("Jón Sigurðsson", status="canonical")
    assert low < dropped < survivor
    seed.person_pair(low, dropped, name_score=82.0)

    db.merge_persons(survivor, [dropped])

    edges = query("SELECT person_a_pk, person_b_pk FROM person_duplicate_candidates")
    assert len(edges) == 1
    assert edges[0]["person_a_pk"] < edges[0]["person_b_pk"]
    assert {edges[0]["person_a_pk"], edges[0]["person_b_pk"]} == {low, survivor}


def test_merge_requires_at_least_one_dropped_pk(freshdb, db, seed):
    a = seed.person("Jón Sigurðsson")
    with pytest.raises(ValueError):
        db.merge_persons(a, [])
