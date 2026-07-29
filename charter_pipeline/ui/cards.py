"""The shared review card: header, diff table, action buttons, hotkeys.

Extracted from review_app.py so more than one screen can render a card, and
so it can be exercised by streamlit.testing's AppTest without executing the
whole app at import time.
"""
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402
import diff_render  # noqa: E402
import hotkeys  # noqa: E402
import review_queue  # noqa: E402
from ui import evidence  # noqa: E402


def sanitized_key(*parts) -> str:
    """Builds a Streamlit widget key safe to interpolate into a CSS class
    selector. hotkeys.bind_hotkeys targets `.st-key-{key}`, so anything that
    isn't alphanumeric or underscore has to go -- notably the ":" in queue
    item_ids like "new_place:157", which silently produced a selector matching
    nothing (the button rendered fine and its hotkey just never worked)."""
    raw = "_".join(str(p) for p in parts if str(p) != "")
    return "".join(c if (c.isalnum() or c == "_") else "_" for c in raw)


def _composite_warning(rq_item) -> list[dict]:
    """Diagnoses each person side of this card for being an over-merged
    composite -- one id holding several real people.

    Worth doing at decision time rather than only in a batch script: all such
    records found are canonical authority imports, so search_authority offers
    them as merge targets at score 100 to every new extraction sharing the bare
    given name. That is how p027 "Jón" accumulated 52 charters spanning
    1180-1488. Merging into an unsplit composite always makes it worse.
    """
    out = []
    for tgt in evidence.evidence_targets(rq_item):
        if tgt["entity_type"] != "person":
            continue
        d = db.diagnose_composite(tgt["pk"])
        if d["is_composite"]:
            out.append({**d, "label": tgt["label"]})
    return out


def _safe(fn, *args, what: str = "evidence", **kwargs):
    """Runs a supplementary render, turning any failure into a caption.

    Evidence panes and composite warnings must never be able to stop a
    reviewer deciding: the action buttons are the point of the card, and an
    exception anywhere above them takes the whole screen down. Failing loudly
    but locally is the right trade here.
    """
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
        st.caption(f"_Could not load {what}: {type(exc).__name__}: {exc}_")
        return None


def render_item_card(rq_item, on_action=None, key_prefix: str = "rq_act",
                     bind_keys: bool = True, show_evidence: bool = True) -> None:
    """Renders one queue item and its action buttons.

    Calls review_queue.apply_action() for any non-"next" click, then
    on_action(action_key) if given (callers advance/reset differently after an
    action -- single-card mode moves a position counter, list+detail lets its
    own row-set-changed check remount the list -- so that's left to them),
    then st.rerun().

    `key_prefix` namespaces the button widget keys. It used to be the bare
    string "rq_act", i.e. the action name alone was the whole key -- fine
    while exactly one card ever rendered per script run, but a
    StreamlitDuplicateElementKey crash the moment two do (a person-cluster
    screen, a proposal-conflict view). Callers rendering more than one card in
    a run MUST pass distinct prefixes. Run them through sanitized_key() if
    deriving them from item_ids.

    `bind_keys=False` suppresses hotkey binding for a card that isn't the
    focused one: two cards binding the same letter is inherently ambiguous, so
    a multi-card screen should bind only the card being acted on.

    One bind_hotkeys() call per card (not per button) keeps this to a single
    extra invisible iframe -- with one iframe per button (an earlier attempt
    using the third-party streamlit-shortcuts package), keyboard focus
    intermittently got captured by one of the many 0-height iframes and real
    keydown events then never reached the page-level listener at all.
    """
    with st.container(border=True):
        st.markdown(f"#### {rq_item.header}")
        st.caption(rq_item.subheader)
        diff_render.render_diff_table(rq_item.diff_rows, rq_item.left_label,
                                      rq_item.right_label)

        composites = (_safe(_composite_warning, rq_item,
                            what="the composite-record check") or []) if show_evidence else []
        for c in composites:
            certain = c["severity"] == "certain"
            body = (f"**{c['label']} is not one person.**" if certain
                    else f"**{c['label']} may not be one person.**")
            body += (f" Attested in {c['charters']} charter(s)"
                     + (f", {c['year_min']}–{c['year_max']}" if c["year_min"] else "")
                     + ".")
            for reason in c["reasons"]:
                body += f"\n- {reason}"
            body += ("\n\nSplit it before merging into it, or the merge target is "
                     "still wrong afterwards.")
            (st.error if certain else st.warning)(body, icon="⚠️")

        btn_cols = st.columns(len(rq_item.actions))
        action_hotkeys = {}
        for col, rq_action in zip(btn_cols, rq_item.actions):
            with col:
                widget_key = f"{key_prefix}_{rq_action.action}"
                # Merge is DEMOTED, never removed, when a side is a composite:
                # a composite can still contain a genuine duplicate of the
                # other side, so this has to stay possible -- just deliberate
                # rather than the obvious default.
                demote = composites and rq_action.action in ("merge", "same")
                clicked = st.button(
                    rq_action.label, key=widget_key,
                    type="secondary" if demote else
                         ("primary" if rq_action.style == "primary" else "secondary"),
                    help=("This side holds more than one person — splitting it "
                          "first is usually the right move." if demote else None),
                )
                action_hotkeys[widget_key] = rq_action.hotkey
                if clicked:
                    if rq_action.action != "next":
                        review_queue.apply_action(rq_item, rq_action.action)
                        st.toast(f"{rq_action.label.split(' (')[0]} — done.")
                    if on_action:
                        on_action(rq_action.action)
                    st.rerun()
        if bind_keys:
            hotkeys.bind_hotkeys(action_hotkeys, scope=key_prefix)

        if composites:
            for c in composites:
                already = db.COMPOSITE_FLAG in (
                    db.get_person_by_pk(c["person_pk"])["data_quality_flag"] or "")
                st.button(
                    f"Flag {c['label'].lower()} as needing splitting"
                    + (" ✓ already flagged" if already else ""),
                    key=f"{key_prefix}_split_{c['person_pk']}",
                    disabled=already,
                    help="Records the intent. Actually partitioning the record "
                         "into separate people is a separate operation.",
                    on_click=db.add_data_quality_flag,
                    args=(c["person_pk"], db.COMPOSITE_FLAG),
                )

        if show_evidence:
            _safe(evidence.render_panes, rq_item, key_prefix,
                  what="the charter-evidence panes")
