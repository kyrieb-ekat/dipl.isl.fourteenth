"""Merge behaviour against a copy of the real database.

The synthetic tests in test_merge_persons.py pin the semantics; these pin the
fact that it survives real corpus shape -- specifically persons carrying
dozens of candidate edges, which is where the UNIQUE collisions came from.
Skips automatically when charter_pipeline.db isn't on disk.
"""


def _highest_degree_person(query):
    rows = query(
        """SELECT pk, COUNT(*) AS degree FROM (
               SELECT person_a_pk AS pk FROM person_duplicate_candidates
               UNION ALL
               SELECT person_b_pk AS pk FROM person_duplicate_candidates)
           GROUP BY pk ORDER BY degree DESC LIMIT 1"""
    )
    return rows[0]["pk"], rows[0]["degree"]


def test_merging_two_high_degree_persons_succeeds(livedb, db, query, scalar):
    """The exact operation that used to raise IntegrityError on every click."""
    pk, degree = _highest_degree_person(query)
    assert degree > 20, f"expected a high-degree person to exercise this, got {degree}"

    partner = scalar(
        """SELECT CASE WHEN person_a_pk = ? THEN person_b_pk ELSE person_a_pk END
           FROM person_duplicate_candidates
           WHERE person_a_pk = ? OR person_b_pk = ? LIMIT 1""",
        (pk, pk, pk),
    )
    survivor, dropped = min(pk, partner), max(pk, partner)
    before = scalar("SELECT COUNT(*) FROM persons")

    db.merge_persons(survivor, [dropped])

    assert scalar("SELECT COUNT(*) FROM persons") == before - 1
    assert scalar("SELECT COUNT(*) FROM persons WHERE person_pk = ?", (dropped,)) == 0


def test_merge_leaves_no_constraint_violations_at_scale(livedb, db, query, scalar):
    pk, _ = _highest_degree_person(query)
    partners = [
        r["other"] for r in query(
            """SELECT CASE WHEN person_a_pk = ? THEN person_b_pk ELSE person_a_pk END AS other
               FROM person_duplicate_candidates
               WHERE person_a_pk = ? OR person_b_pk = ? LIMIT 5""",
            (pk, pk, pk),
        )
    ]
    group = sorted({pk, *partners})
    survivor, dropped = group[0], group[1:]

    db.merge_persons(survivor, dropped)

    # The two constraints that used to be violated, checked directly.
    assert scalar(
        "SELECT COUNT(*) FROM person_duplicate_candidates WHERE person_a_pk >= person_b_pk"
    ) == 0
    assert scalar(
        """SELECT COUNT(*) FROM (SELECT person_a_pk, person_b_pk
           FROM person_duplicate_candidates
           GROUP BY person_a_pk, person_b_pk HAVING COUNT(*) > 1)"""
    ) == 0
    # No edge may still point at a person that no longer exists.
    assert scalar(
        """SELECT COUNT(*) FROM person_duplicate_candidates c
           WHERE NOT EXISTS (SELECT 1 FROM persons p WHERE p.person_pk = c.person_a_pk)
              OR NOT EXISTS (SELECT 1 FROM persons p WHERE p.person_pk = c.person_b_pk)"""
    ) == 0
    assert scalar("PRAGMA foreign_key_check") is None


def test_no_self_edges_remain_after_merging_a_linked_pair(livedb, db, query, scalar):
    pair = query(
        "SELECT person_a_pk, person_b_pk FROM person_duplicate_candidates LIMIT 1"
    )[0]
    db.merge_persons(pair["person_a_pk"], [pair["person_b_pk"]])

    assert scalar(
        "SELECT COUNT(*) FROM person_duplicate_candidates WHERE person_a_pk = person_b_pk"
    ) == 0
