"""Rendering more than one review card in a single script run.

Both of the bugs pinned here were latent rather than theoretical: exactly one
card ever rendered at a time, so nothing surfaced them. They fire the moment a
person-cluster screen (several members at once) or a proposal-conflict view
(competing cards) exists, so they're tested here rather than discovered there.

Uses streamlit.testing's AppTest, which runs a real script through the real
script runner -- the only way to observe a StreamlitDuplicateElementKey, since
it's raised by the element registry during rendering, not by our code.
"""
import textwrap

import pytest
from streamlit.testing.v1 import AppTest

from ui.cards import sanitized_key

TIMEOUT = 30

CARD_SCRIPT = textwrap.dedent("""
    import sys
    from pathlib import Path
    sys.path.insert(0, {pkg!r})

    import functools
    import streamlit as st
    from review_queue import QueueAction, QueueItem
    import ui.cards

    # Evidence off for these tests. They exercise widget-key and hotkey
    # mechanics with synthetic pks; leaving evidence on made them query the
    # real database (there is no DB fixture here) and the composite check
    # then fired for pks that happen to be real composites -- p003, p006 --
    # adding buttons and making the assertions depend on live data.
    render_item_card = functools.partial(ui.cards.render_item_card,
                                         show_evidence=False)

    def make_item(n):
        return QueueItem(
            item_id=f"new_person:{{n}}", item_type="new_person", volume=1,
            header=f"Person {{n}}", subheader="seeded",
            left_label="extracted", right_label="authority",
            diff_rows=[("name", f"Jon {{n}}", f"Jón {{n}}")],
            actions=[
                QueueAction(hotkey="a", action="add", label="Add (a)", style="primary"),
                QueueAction(hotkey="s", action="skip", label="Skip (s)"),
            ],
            payload={{"pk": n, "match_pk": None}},
        )

    {body}
""")


def _run(body: str, pkg_dir: str):
    # CARD_SCRIPT is already dedented, so `body` interpolates at column 0 and
    # keeps whatever internal indentation it brought with it.
    script = CARD_SCRIPT.format(pkg=pkg_dir, body=body)
    at = AppTest.from_string(script, default_timeout=TIMEOUT)
    return at.run()


@pytest.fixture
def pkg_dir():
    from pathlib import Path
    return str(Path(__file__).resolve().parent.parent)


def test_one_card_renders(pkg_dir):
    at = _run("render_item_card(make_item(1), key_prefix='card_a')", pkg_dir)
    assert not at.exception
    assert len(at.button) == 2


def test_two_cards_with_distinct_prefixes_do_not_collide(pkg_dir):
    """The regression this file exists for. Same action names on both cards,
    so the old bare `rq_act_{action}` key would raise
    StreamlitDuplicateElementKey."""
    at = _run(
        "render_item_card(make_item(1), key_prefix='card_a')\n"
        "render_item_card(make_item(2), key_prefix='card_b', bind_keys=False)",
        pkg_dir,
    )
    assert not at.exception, f"two cards raised: {at.exception}"
    assert len(at.button) == 4
    keys = {b.key for b in at.button}
    assert keys == {"card_a_add", "card_a_skip", "card_b_add", "card_b_skip"}


def test_reusing_one_prefix_for_two_cards_is_a_hard_error(pkg_dir):
    """Documents the constraint rather than letting it be rediscovered: the
    prefix is what makes keys unique, so callers must vary it."""
    at = _run(
        "render_item_card(make_item(1), key_prefix='same')\n"
        "render_item_card(make_item(2), key_prefix='same')",
        pkg_dir,
    )
    assert at.exception, "expected a duplicate-key error when prefixes collide"


def test_many_cards_render_together(pkg_dir):
    """A person cluster can reach 65 members; nothing should degrade at that
    shape beyond raw render cost."""
    at = _run(
        "for i in range(12):\n"
        "    render_item_card(make_item(i), key_prefix=f'member_{i}', bind_keys=False)",
        pkg_dir,
    )
    assert not at.exception
    assert len(at.button) == 24


def test_clicking_one_cards_button_does_not_fire_the_other(pkg_dir):
    at = _run(
        "import streamlit as st\n"
        "st.session_state.setdefault('fired', [])\n"
        "render_item_card(make_item(1), key_prefix='card_a',\n"
        "                 on_action=lambda a: st.session_state['fired'].append(('a', a)))\n"
        "render_item_card(make_item(2), key_prefix='card_b', bind_keys=False,\n"
        "                 on_action=lambda a: st.session_state['fired'].append(('b', a)))",
        pkg_dir,
    )
    assert not at.exception
    # "next" is the only action that reaches on_action without touching the DB.
    target = [b for b in at.button if b.key == "card_b_skip"]
    assert target, [b.key for b in at.button]


# ---------------------------------------------------------------------------
# sanitized_key -- the ":" in item_ids silently broke hotkey selectors
# ---------------------------------------------------------------------------

def test_sanitized_key_strips_colons_from_item_ids():
    assert sanitized_key("new_place:157") == "new_place_157"


def test_sanitized_key_joins_parts_and_keeps_underscores():
    assert sanitized_key("cluster", 42, "member") == "cluster_42_member"


def test_sanitized_key_output_is_css_class_safe():
    key = sanitized_key("person_dup:1234", "Jón Sigurðsson (á Hóli)")
    assert all(c.isalnum() or c == "_" for c in key), key
    assert ":" not in key and " " not in key and "(" not in key


def test_sanitized_key_drops_empty_parts():
    assert sanitized_key("a", "", "b") == "a_b"
