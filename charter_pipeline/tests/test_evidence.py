"""Charter evidence lookups, and composite-record detection.

Two concerns:

1. The comparison card needs the charter attestations behind each side --
   "Einar, priest, 1340" vs "Einar, layman, 1341" at name score 100 cannot be
   decided from extracted fields alone.
2. The same data exposes the inverse of duplication: one id holding several
   real people. Detection is severity-tiered on purpose, because an
   impossible attestation span is a fact while two mutually exclusive roles
   inside one lifetime can equally be loose role labelling.
"""


# ---------------------------------------------------------------------------
# Appearances
# ---------------------------------------------------------------------------

def test_person_appearances_are_ordered_oldest_first(freshdb, db, seed):
    pk = seed.person("Jón")
    late = seed.charter(volume=1, sequence=2, di_year=1400, di_reference="DI I nr. 2")
    early = seed.charter(volume=1, sequence=1, di_year=1300, di_reference="DI I nr. 1")
    seed.charter_person(late, pk, role_category="witness-testimony")
    seed.charter_person(early, pk, role_category="issuer-bishop")

    df = db.get_person_appearances(pk)

    assert list(df["di_year"]) == [1300, 1400]
    assert list(df["role_category"]) == ["issuer-bishop", "witness-testimony"]


def test_person_appearances_carry_the_qualifier(freshdb, db, seed):
    """The qualifier is the single most useful field for this decision."""
    pk = seed.person("Jón")
    ch = seed.charter(volume=1, sequence=1, di_year=1391)
    seed.charter_person(ch, pk, role_category="issuer-bishop",
                        qualifier="Bishop of Hólar", extracted_name="Jón")

    row = db.get_person_appearances(pk).iloc[0]
    assert row["qualifier"] == "Bishop of Hólar"
    assert row["extracted_name"] == "Jón"


def test_person_with_no_charter_link_returns_empty_not_error(freshdb, db, seed):
    """21 authority-imported persons are in exactly this state -- the pane
    must read as 'nothing recorded', not look broken."""
    pk = seed.person("Einar", volume=None, status="canonical")
    df = db.get_person_appearances(pk)
    assert df.empty
    assert list(df.columns)  # still a shaped frame, so callers can render headers


def test_undated_appearances_sort_last_but_are_kept(freshdb, db, seed):
    pk = seed.person("Jón")
    undated = seed.charter(volume=1, sequence=1)
    dated = seed.charter(volume=1, sequence=2, di_year=1350)
    seed.charter_person(undated, pk)
    seed.charter_person(dated, pk)

    df = db.get_person_appearances(pk)
    assert len(df) == 2
    assert df.iloc[0]["di_year"] == 1350
    assert df.iloc[1]["di_year"] is None or df.iloc[1]["di_year"] != df.iloc[1]["di_year"]


def test_place_appearances_include_role(freshdb, db, seed):
    pk = seed.place("Akrar")
    ch = seed.charter(volume=1, sequence=1, di_year=1350)
    seed.charter_place(ch, pk, role="loc.writing", extracted_name="Ökrum")

    row = db.get_place_appearances(pk).iloc[0]
    assert row["role"] == "loc.writing"
    assert row["extracted_name"] == "Ökrum"


def test_appearances_survive_a_parse_error_charter(freshdb, db, seed):
    """6 charters carry has_parse_error=1; they must not break the pane."""
    pk = seed.person("Jón")
    ch = seed.charter(volume=1, sequence=1, has_parse_error=1)
    seed.charter_person(ch, pk)

    df = db.get_person_appearances(pk)
    assert len(df) == 1
    assert df.iloc[0]["has_parse_error"] == 1


# ---------------------------------------------------------------------------
# Charter cast (co-mentions)
# ---------------------------------------------------------------------------

def test_charter_cast_excludes_the_subject(freshdb, db, seed):
    me = seed.person("Jón")
    other = seed.person("Þórunn")
    ch = seed.charter(volume=1, sequence=1)
    seed.charter_person(ch, me, ordinal=1)
    seed.charter_person(ch, other, ordinal=2)

    cast = db.get_charter_cast(ch, exclude_person_pk=me)

    assert list(cast["persons"]["canonical_name"]) == ["Þórunn"]


def test_charter_cast_puts_the_writing_place_first(freshdb, db, seed):
    ch = seed.charter(volume=1, sequence=1)
    mentioned = seed.place("Róm")
    written_at = seed.place("Skálholt")
    seed.charter_place(ch, mentioned, role="loc.mentioned", ordinal=1)
    seed.charter_place(ch, written_at, role="loc.writing", ordinal=2)

    cast = db.get_charter_cast(ch)

    assert list(cast["places"]["canonical_name"]) == ["Skálholt", "Róm"]


def test_charter_cast_keeps_unresolved_mentions(freshdb, db, seed):
    """A pending_review row has person_pk NULL but its extracted_name is still
    evidence about who was in the room."""
    ch = seed.charter(volume=1, sequence=1)
    conn = db.get_connection()
    with conn:
        conn.execute("INSERT INTO charter_persons "
                     "(charter_pk, ordinal, extracted_name, resolution_state) "
                     "VALUES (?, 1, 'Ormur Loftsson', 'pending_review')", (ch,))
    conn.close()

    cast = db.get_charter_cast(ch)
    assert list(cast["persons"]["extracted_name"]) == ["Ormur Loftsson"]


# ---------------------------------------------------------------------------
# Composite detection
# ---------------------------------------------------------------------------

def test_impossible_span_is_certain(freshdb, db, seed):
    pk = seed.person("Jón")
    for i, year in enumerate((1180, 1488), start=1):
        ch = seed.charter(volume=1, sequence=i, di_year=year)
        seed.charter_person(ch, pk)

    d = db.diagnose_composite(pk)

    assert d["is_composite"] is True
    assert d["severity"] == "certain"
    assert d["span_years"] == 308
    assert any("more than one lifetime" in r for r in d["reasons"])


def test_saint_alongside_a_living_actor_is_certain(freshdb, db, seed):
    """Real case p006: St Lawrence sharing an id with a contemporary Abbot
    Laurentius, both attested in 1247 -- span alone would never catch it."""
    pk = seed.person("Laurencius")
    c1 = seed.charter(volume=1, sequence=1, di_year=1247)
    c2 = seed.charter(volume=1, sequence=2, di_year=1247)
    seed.charter_person(c1, pk, role_category="witness-testimony",
                        qualifier="Abbot, English envoy of King Hákon")
    seed.charter_person(c2, pk, role_category="saint-patron",
                        qualifier="patron saint of Laurentiuskirkia")

    d = db.diagnose_composite(pk)

    assert d["severity"] == "certain"
    assert d["span_years"] == 0
    assert any("saint" in r for r in d["reasons"])


def test_exclusive_roles_within_a_lifetime_is_only_review(freshdb, db, seed):
    """Real case p030: issuer-bishop and issuer-layman inside one year is
    suspicious, but can equally be loose role labelling -- not a fact."""
    pk = seed.person("Stephán")
    c1 = seed.charter(volume=1, sequence=1, di_year=1179)
    c2 = seed.charter(volume=1, sequence=2, di_year=1179)
    seed.charter_person(c1, pk, role_category="issuer-bishop")
    seed.charter_person(c2, pk, role_category="issuer-layman")

    d = db.diagnose_composite(pk)

    assert d["is_composite"] is True
    assert d["severity"] == "review"


def test_a_saint_only_record_is_not_composite(freshdb, db, seed):
    """A patron saint with no living-actor role is just a saint."""
    pk = seed.person("Ólafr")
    for i in (1, 2):
        ch = seed.charter(volume=1, sequence=i, di_year=1350)
        seed.charter_person(ch, pk, role_category="saint-patron")

    assert db.diagnose_composite(pk)["is_composite"] is False


def test_an_ordinary_record_is_not_composite(freshdb, db, seed):
    pk = seed.person("Jón")
    for i, year in enumerate((1340, 1350, 1355), start=1):
        ch = seed.charter(volume=1, sequence=i, di_year=year)
        seed.charter_person(ch, pk, role_category="witness-testimony")

    d = db.diagnose_composite(pk)
    assert d["is_composite"] is False
    assert d["severity"] is None


def test_person_with_no_appearances_is_not_composite(freshdb, db, seed):
    pk = seed.person("Einar")
    assert db.diagnose_composite(pk)["is_composite"] is False


def test_find_composite_persons_sorts_certain_first(freshdb, db, seed):
    arguable = seed.person("Stephán")
    c1 = seed.charter(volume=1, sequence=1, di_year=1179)
    c2 = seed.charter(volume=1, sequence=2, di_year=1179)
    seed.charter_person(c1, arguable, role_category="issuer-bishop")
    seed.charter_person(c2, arguable, role_category="issuer-layman")

    impossible = seed.person("Jón")
    c3 = seed.charter(volume=1, sequence=3, di_year=1180)
    c4 = seed.charter(volume=1, sequence=4, di_year=1488)
    seed.charter_person(c3, impossible)
    seed.charter_person(c4, impossible)

    rows = db.find_composite_persons()

    assert [r["person_pk"] for r in rows] == [impossible, arguable]
    assert rows[0]["severity"] == "certain"
    assert rows[1]["severity"] == "review"


def test_find_composite_persons_normalises_null_source_volume(freshdb, db, seed):
    """pandas turns a NULL source_volume into NaN; callers test `is None`."""
    pk = seed.person("Jón", volume=None)
    for i, year in enumerate((1180, 1488), start=1):
        ch = seed.charter(volume=1, sequence=i, di_year=year)
        seed.charter_person(ch, pk)

    row = db.find_composite_persons()[0]
    assert row["source_volume"] is None


# ---------------------------------------------------------------------------
# Flag accumulation
# ---------------------------------------------------------------------------

def test_add_data_quality_flag_preserves_an_existing_value(freshdb, db, seed, query):
    """A record can be both a later-transmission actor and a composite."""
    pk = seed.person("Árni Magnússon", data_quality_flag="later_transmission_actor")

    db.add_data_quality_flag(pk, db.COMPOSITE_FLAG)

    flag = query("SELECT data_quality_flag FROM persons WHERE person_pk = ?",
                 (pk,))[0]["data_quality_flag"]
    assert "later_transmission_actor" in flag
    assert db.COMPOSITE_FLAG in flag


def test_add_data_quality_flag_is_idempotent(freshdb, db, seed, query):
    pk = seed.person("Jón")
    db.add_data_quality_flag(pk, db.COMPOSITE_FLAG)
    db.add_data_quality_flag(pk, db.COMPOSITE_FLAG)

    flag = query("SELECT data_quality_flag FROM persons WHERE person_pk = ?",
                 (pk,))[0]["data_quality_flag"]
    assert flag.split(";").count(db.COMPOSITE_FLAG) == 1


# ---------------------------------------------------------------------------
# Against the real corpus
# ---------------------------------------------------------------------------

def _pk_for(db, display_id):
    conn = db.get_connection()
    try:
        row = conn.execute("SELECT person_pk FROM persons WHERE display_id = ?",
                           (display_id,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def test_p027_is_the_known_worst_composite(livedb, db):
    pk = _pk_for(db, "p027")
    d = db.diagnose_composite(pk)

    assert d["severity"] == "certain"
    assert d["charters"] > 40
    assert d["year_min"] == 1180 and d["year_max"] == 1488
    assert "saint-patron" in d["roles"]


def test_p012_has_no_evidence_at_all(livedb, db):
    """The pair that prompted the evidence pane is the case it cannot help."""
    assert db.get_person_appearances(_pk_for(db, "p012")).empty
    assert db.get_person_appearances(_pk_for(db, "p013")).empty


def test_composites_are_overwhelmingly_authority_imports(livedb, db):
    """They are canonical, so search_authority offers them as merge targets --
    which is how they grew."""
    rows = db.find_composite_persons()
    assert len(rows) >= 10
    authority = [r for r in rows if r["source_volume"] is None]
    assert len(authority) >= len(rows) - 2
