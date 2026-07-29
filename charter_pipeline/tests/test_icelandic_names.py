"""Morphological comparison of Icelandic place names.

The point of this module is that most comparisons are DETERMINISTIC, so most of
these tests assert exact verdicts rather than score ranges. Where a score is
asserted it is a ranking property, never a gate.
"""
import pytest

import icelandic_names as il

# ── paradigm integrity ──────────────────────────────────────────────────────


def test_every_paradigm_has_eight_cells():
    for lemma, forms in il.PARADIGMS.items():
        assert len(forms) == len(il.CASES), f"{lemma} has {len(forms)} cells"


@pytest.mark.parametrize("lemma", sorted(il.PARADIGMS))
def test_every_cell_resolves_back_to_its_own_lemma(lemma):
    """A free round-trip that catches a mistyped paradigm form immediately --
    the failure mode otherwise looks like a coverage gap, not a typo."""
    for form in il.PARADIGMS[lemma]:
        parsed = il.parse(form)
        assert parsed.lemma == lemma, f"{form!r} resolved to {parsed.lemma!r}"
        assert parsed.specific == "", f"{form!r} left specific={parsed.specific!r}"
        assert parsed.is_simplex


def test_subsidiary_generics_are_all_real_paradigms():
    assert il.SUBSIDIARY_GENERICS <= set(il.PARADIGMS)


# ── the labelled pairs ──────────────────────────────────────────────────────

SAME_PAIRS = [
    ("Akrar", "Ökrum"),               # nom.pl / dat.pl of akur, with u-umlaut
    ("Möðruvellir", "Möðruvöllum"),
    ("Hólastaður", "Hólastaðar"),
    ("Flugumýrar", "Flugumýri"),
    ("Hólar", "Hólum"),
]
DIFFERENT_PAIRS = [
    ("Hjarðarholt", "Árholt"),
    ("reynivellir", "viðivellir"),    # the case that prompted this work
    ("Dagverðarnes", "Árnes"),
    ("Staðarhraun", "Árhraun"),
]


@pytest.mark.parametrize("a,b", SAME_PAIRS)
def test_paradigm_variants_are_same(a, b):
    r = il.compare(a, b)
    assert r["verdict"] == il.SAME, r["reason"]
    assert r["score"] == 100.0


@pytest.mark.parametrize("a,b", DIFFERENT_PAIRS)
def test_shared_generic_with_different_specific_is_different(a, b):
    r = il.compare(a, b)
    assert r["verdict"] == il.DIFFERENT, r["reason"]
    assert "different specific element" in r["reason"]


def test_akrar_okrum_is_the_case_no_fuzzy_scorer_gets():
    """Same farm; every string metric scores it 40-60. Paradigms make it exact."""
    from rapidfuzz import fuzz
    assert fuzz.WRatio("Akrar", "Ökrum") < 65
    assert il.compare("Akrar", "Ökrum")["verdict"] == il.SAME


def test_subsidiary_parcel_is_neither_same_nor_different():
    r = il.compare("Hólar", "Hólareki")
    assert r["verdict"] == il.DERIVED
    assert "parcel" in r["reason"]


def test_subsidiary_detection_is_order_independent():
    assert il.compare("Hólareki", "Hólar")["verdict"] == il.DERIVED


def test_unknown_generic_falls_back_to_unresolved():
    r = il.compare("Skálholt", "Kirkjubæjarklaustur")
    assert r["verdict"] in (il.DIFFERENT, il.UNRESOLVED)


def test_unrelated_names_with_no_generic_are_unresolved_not_same():
    r = il.compare("Róm", "París")
    assert r["verdict"] == il.UNRESOLVED
    assert r["score"] < 60


# ── orthography: comparison-only, and conservative ──────────────────────────

def test_normalisation_never_mutates_the_input():
    """It is a comparison key, not a rewrite. Persisting it would over-merge."""
    original = "Möðruvellir"
    il.normalize_orthography(original)
    assert original == "Möðruvellir"


def test_accents_are_not_folded():
    """ö/o and á/a are distinct letters in Icelandic, and the paradigms depend
    on the distinction. 04a's normalize_name(fold_accents=True) destroyed it."""
    assert il.normalize_orthography("völlum") != il.normalize_orthography("vollum")
    assert il.normalize_orthography("mörk") != il.normalize_orthography("mork")
    assert il.normalize_orthography("á") != il.normalize_orthography("a")


def test_degemination_is_restricted_to_ss():
    """Regression: a general gemination collapse turned `vellir` into `velir`
    and `völlum` into `völum`, so every -vellir name stopped resolving."""
    assert il.normalize_orthography("vellir") == "vellir"
    assert il.normalize_orthography("völlum") == "völlum"
    assert il.normalize_orthography("Ness") == "nes"


def test_geminate_stems_still_resolve_after_normalisation():
    assert il.parse("vellir").lemma == "völlur"
    assert il.parse("Möðruvellir").lemma == "völlur"
    assert il.parse("Möðruvöllum").specific == "möðru"


def test_paradigm_index_is_keyed_on_normalised_forms():
    """Regression: the index was built from raw forms while lookups were
    normalised, so the i/j rule silently broke every -bær genitive."""
    assert il.parse("bæjar").lemma == "bær"
    assert il.parse("Hólabæjar").lemma == "bær"


def test_r_ur_variance_resolves_in_the_direction_sources_use_it():
    """A charter may write `dalr` where the dictionary form is `dalur`, so the
    -r spelling has to resolve. Transforming the lookup key instead only turns
    dictionary spellings into period ones, which is the wrong direction."""
    assert il.parse("dalur").lemma == "dalur"
    assert il.parse("dalr").lemma == "dalur"
    assert il.parse("vogr").lemma == "vogur"
    assert il.parse("Hvammsdalr").lemma == "dalur"
    assert il.parse("Hvammsdalr").specific == "hvamms"


def test_r_ur_variance_counts_as_the_same_place():
    assert il.compare("Hvammsdalr", "Hvammsdalur")["verdict"] == il.SAME


def test_empty_and_whitespace_are_safe():
    for junk in ("", "   ", None):
        r = il.parse(junk or "")
        assert not r.resolved
    assert il.compare("", "")["verdict"] in (il.SAME, il.UNRESOLVED)
    assert il.similarity("", "x") == 0.0


# ── similarity: ranking only ────────────────────────────────────────────────

def test_similarity_has_no_partial_ratio_plateau():
    """WRatio gave a flat 90 to anything containing anything -- 6,870 of 12,960
    real candidates sat at exactly 90.0, making candidate_rank meaningless."""
    from rapidfuzz import fuzz
    assert fuzz.WRatio("grund", "grundarfjörður") >= 89
    assert il.similarity("grund", "grundarfjörður") < 85


def test_similarity_is_symmetric():
    for a, b in SAME_PAIRS + DIFFERENT_PAIRS:
        assert il.similarity(a, b) == pytest.approx(il.similarity(b, a), abs=0.05)


def test_identical_names_score_100():
    assert il.similarity("Hvammur", "Hvammur") == 100.0


def test_shared_generic_scores_below_shared_specific():
    """The whole point: the distinguishing element must dominate."""
    shared_specific = il.similarity("Hólastaður", "Hólastaðar")
    shared_generic = il.similarity("Hjarðarholt", "Árholt")
    assert shared_specific > shared_generic + 20


def test_explain_is_human_readable():
    text = il.explain("reynivellir", "viðivellir")
    assert "Different places" in text
    assert "völlur" in text


# ── corpus-scale behaviour ──────────────────────────────────────────────────

def test_verdict_distribution_over_the_real_corpus(livedb, db):
    """Pins coverage so a paradigm edit that changes it is visible rather than
    silently shifting how much of the queue is auto-decided."""
    import collections
    conn = db.get_connection()
    try:
        pairs = conn.execute(
            "SELECT di_name, candidate_name FROM place_duplicate_candidates "
            "WHERE di_name != '' AND candidate_name != ''").fetchall()
    finally:
        conn.close()

    counts = collections.Counter(il.compare(a, b)["verdict"] for a, b in pairs)
    decided = sum(v for k, v in counts.items() if k != il.UNRESOLVED)

    assert len(pairs) > 12000
    # Deterministic verdicts for a meaningful share of the queue.
    assert decided / len(pairs) > 0.25, counts
    assert counts[il.SAME] > 400
    assert counts[il.DIFFERENT] > 2000


def test_no_real_place_name_crashes_the_parser(livedb, db):
    conn = db.get_connection()
    try:
        names = [r[0] for r in conn.execute(
            "SELECT canonical_name FROM places WHERE canonical_name != ''")]
    finally:
        conn.close()
    for n in names:
        il.parse(n)          # must not raise on anything in the corpus
    assert len(names) > 2000
