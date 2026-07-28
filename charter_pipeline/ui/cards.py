"""The shared review card: header, diff table, action buttons, hotkeys.

Extracted from review_app.py so more than one screen can render a card, and
so it can be exercised by streamlit.testing's AppTest without executing the
whole app at import time.
"""
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import diff_render  # noqa: E402
import hotkeys  # noqa: E402
import review_queue  # noqa: E402


def sanitized_key(*parts) -> str:
    """Builds a Streamlit widget key safe to interpolate into a CSS class
    selector. hotkeys.bind_hotkeys targets `.st-key-{key}`, so anything that
    isn't alphanumeric or underscore has to go -- notably the ":" in queue
    item_ids like "new_place:157", which silently produced a selector matching
    nothing (the button rendered fine and its hotkey just never worked)."""
    raw = "_".join(str(p) for p in parts if str(p) != "")
    return "".join(c if (c.isalnum() or c == "_") else "_" for c in raw)


def render_item_card(rq_item, on_action=None, key_prefix: str = "rq_act",
                     bind_keys: bool = True) -> None:
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

        btn_cols = st.columns(len(rq_item.actions))
        action_hotkeys = {}
        for col, rq_action in zip(btn_cols, rq_item.actions):
            with col:
                widget_key = f"{key_prefix}_{rq_action.action}"
                clicked = st.button(
                    rq_action.label, key=widget_key,
                    type="primary" if rq_action.style == "primary" else "secondary",
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
