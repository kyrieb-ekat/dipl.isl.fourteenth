"""The evidence panes and composite warning as wired into the card.

Isolated from test_cards.py on purpose: those exercise widget-key and hotkey
mechanics with synthetic pks and run with evidence OFF, because leaving it on
made them read the real database.
"""
import textwrap

import pytest
from streamlit.testing.v1 import AppTest

from ui import evidence


# ---------------------------------------------------------------------------
# evidence_targets: which entities a card is comparing
# ---------------------------------------------------------------------------

class _Item:
    def __init__(self, item_type, payload, header="X"):
        self.item_type = item_type
        self.payload = payload
        self.header = header


def test_targets_for_a_person_pair():
    t = evidence.evidence_targets(_Item("person_dup", {"person_a_pk": 1, "person_b_pk": 2}))
    assert [(x["label"], x["pk"]) for x in t] == [("Side A", 1), ("Side B", 2)]
    assert {x["entity_type"] for x in t} == {"person"}


def test_targets_omit_a_missing_authority_match():
    t = evidence.evidence_targets(_Item("new_person", {"pk": 5, "match_pk": None}))
    assert [x["label"] for x in t] == ["This record"]


def test_targets_for_place_dup_have_only_the_di_side():
    """The other side is an external nafnid.is record with no charters."""
    t = evidence.evidence_targets(_Item("place_dup", {"candidate_pk": 9, "place_pk": 3}))
    assert [(x["label"], x["entity_type"]) for x in t] == [("DI place", "place")]


def test_targets_tolerate_a_payload_missing_its_ids():
    """Evidence is supplementary; a thin payload must not raise."""
    assert evidence.evidence_targets(_Item("new_person", {})) == []
    assert evidence.evidence_targets(_Item("person_dup", {})) == []
    assert evidence.evidence_targets(_Item("nonsense", {})) == []


def test_pk_zero_is_treated_as_a_real_id():
    t = evidence.evidence_targets(_Item("new_person", {"pk": 0, "match_pk": None}))
    assert [x["pk"] for x in t] == [0]


# ---------------------------------------------------------------------------
# Source text
# ---------------------------------------------------------------------------

def test_load_charter_text_returns_none_when_absent(monkeypatch, tmp_path):
    import config
    monkeypatch.setattr(config, "SEGMENTS_DIR", tmp_path)
    evidence.load_charter_text.clear()
    assert evidence.load_charter_text(1, 1) is None


def test_load_charter_text_reads_the_expected_filename(monkeypatch, tmp_path):
    import config
    (tmp_path / "vol04").mkdir()
    (tmp_path / "vol04" / "DI_04_0017.txt").write_text("Ökrum í Blönduhlíð",
                                                       encoding="utf-8")
    monkeypatch.setattr(config, "SEGMENTS_DIR", tmp_path)
    evidence.load_charter_text.clear()
    assert evidence.load_charter_text(4, 17) == "Ökrum í Blönduhlíð"


def test_highlight_marks_every_case_insensitive_occurrence():
    out = evidence._highlight("Jón og jón og JÓN", "jón")
    assert out.count("diff-add") == 3
    # original casing preserved inside the marks
    assert "Jón</mark>" in out and "JÓN</mark>" in out


def test_highlight_is_a_noop_for_an_empty_needle():
    assert evidence._highlight("text", "") == "text"


def test_highlight_does_not_treat_the_needle_as_a_pattern():
    """Extracted names contain characters a regex would read as syntax."""
    out = evidence._highlight("a (rex) b", "(rex)")
    assert "<mark" in out


# ---------------------------------------------------------------------------
# Rendered into a real card
# ---------------------------------------------------------------------------

CARD = textwrap.dedent("""
    import sys
    sys.path.insert(0, {pkg!r})
    import db
    db.DB_PATH = {dbp!r}
    db.invalidate_authority_cache()

    import streamlit as st
    from review_queue import QueueAction, QueueItem
    from ui.cards import render_item_card

    item = QueueItem(
        item_id="person_dup:1", item_type="person_dup", volume=1,
        header="A vs B", subheader="seeded",
        left_label="A", right_label="B",
        diff_rows=[("Name", "Jón", "Jón")],
        actions=[
            QueueAction(hotkey="s", action="same", label="Same (s)", style="primary"),
            QueueAction(hotkey="m", action="merge", label="Merge (m)", style="primary"),
            QueueAction(hotkey="d", action="different", label="Different (d)"),
        ],
        payload={{"candidate_pk": 1, "person_a_pk": {a}, "person_b_pk": {b}}},
    )
    render_item_card(item, key_prefix="ev")
""")


@pytest.fixture
def pkg_dir():
    from pathlib import Path
    return str(Path(__file__).resolve().parent.parent)


def _run(pkg_dir, dbp, a, b):
    at = AppTest.from_string(CARD.format(pkg=pkg_dir, dbp=str(dbp), a=a, b=b),
                             default_timeout=60)
    return at.run()


def test_card_renders_evidence_without_error(freshdb, db, seed, pkg_dir):
    a = seed.person("Jón")
    b = seed.person("Jon")
    ch = seed.charter(volume=1, sequence=1, di_year=1350, di_reference="DI I nr. 1")
    seed.charter_person(ch, a, qualifier="prestr at Ingunarstaðir")

    at = _run(pkg_dir, freshdb, a, b)

    assert not at.exception, at.exception
    assert len(at.button) == 3          # the three actions, no split button
    assert len(at.expander) == 3        # the three evidence panes


def test_a_clean_pair_shows_no_composite_warning(freshdb, db, seed, pkg_dir):
    a = seed.person("Jón")
    b = seed.person("Jon")
    ch = seed.charter(volume=1, sequence=1, di_year=1350)
    seed.charter_person(ch, a)

    at = _run(pkg_dir, freshdb, a, b)

    assert not at.error
    assert not at.warning


def test_a_composite_side_produces_an_error_and_a_split_button(freshdb, db, seed, pkg_dir):
    a = seed.person("Jón")
    b = seed.person("Jon")
    for i, year in enumerate((1180, 1488), start=1):
        ch = seed.charter(volume=1, sequence=i, di_year=year)
        seed.charter_person(ch, a)

    at = _run(pkg_dir, freshdb, a, b)

    assert not at.exception
    blob = " ".join(e.value for e in at.error)
    assert "not one person" in blob
    assert "more than one lifetime" in blob
    assert "Split it before merging" in blob
    # merge stays available, plus a split-intent button
    assert len(at.button) == 4
    assert any("splitting" in b.label for b in at.button)


def test_merge_is_demoted_not_removed_for_a_composite(freshdb, db, seed, pkg_dir):
    """A composite can still contain a genuine duplicate of the other side, so
    merging must stay possible -- just no longer the obvious default."""
    a = seed.person("Jón")
    b = seed.person("Jon")
    for i, year in enumerate((1180, 1488), start=1):
        ch = seed.charter(volume=1, sequence=i, di_year=year)
        seed.charter_person(ch, a)

    at = _run(pkg_dir, freshdb, a, b)

    merge = [x for x in at.button if x.key == "ev_merge"]
    assert merge, [x.key for x in at.button]
    # .type is the element type; the styling lives on the proto.
    assert merge[0].proto.type == "secondary"
    assert "more than one person" in merge[0].proto.help


def test_merge_is_primary_when_nothing_is_flagged(freshdb, db, seed, pkg_dir):
    a = seed.person("Jón")
    b = seed.person("Jon")
    at = _run(pkg_dir, freshdb, a, b)

    merge = [x for x in at.button if x.key == "ev_merge"]
    assert merge[0].proto.type == "primary"
    assert not merge[0].proto.help


def test_an_arguable_composite_warns_rather_than_errors(freshdb, db, seed, pkg_dir):
    a = seed.person("Stephán")
    b = seed.person("Stephan")
    c1 = seed.charter(volume=1, sequence=1, di_year=1179)
    c2 = seed.charter(volume=1, sequence=2, di_year=1179)
    seed.charter_person(c1, a, role_category="issuer-bishop")
    seed.charter_person(c2, a, role_category="issuer-layman")

    at = _run(pkg_dir, freshdb, a, b)

    assert not at.error
    blob = " ".join(w.value for w in at.warning)
    assert "may not be one person" in blob
