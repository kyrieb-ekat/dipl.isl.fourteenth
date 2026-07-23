"""
Minimal keyboard-shortcut binder for the Review tab's card queue.

Replaces an earlier attempt built on the third-party `streamlit-shortcuts`
package: that library's keydown listener has no guard against firing while
the user is typing in a text input, AND no guard against firing when its
target tab isn't even the visible one. Streamlit renders every st.tabs()
body on every script run regardless of which tab is visually selected (see
render_pipeline_tab's comment in review_app.py), so the Review tab's hotkey
bindings exist in the DOM at all times -- without a visibility check, typing
a letter that happens to match a bound hotkey (e.g. "k" in "biskups") into a
search box on a completely different tab would silently click a hidden
button on the Review tab. Confirmed bug, not a hypothetical.

This module's bind_hotkeys() adds both guards. No third-party dependency.
"""
import json

import streamlit.components.v1 as components


def bind_hotkeys(mapping: dict) -> None:
    """mapping: {widget_key: single_char_hotkey}. Binds each hotkey to click
    the st.button rendered with that key (Streamlit tags its container with
    a `st-key-{key}` class), via one keydown listener attached to the parent
    document. Call once per render with the CURRENT set of live bindings --
    the JS replaces its whole binding map on every call (`doc.__hotkeyMap =
    mapping`), so there's no stale-entry accumulation the way a merge-only
    approach would have.

    Guards, both required by observed bugs in the third-party alternative:
    - Skips entirely while focus is in an INPUT/TEXTAREA/contenteditable
      element, so typing a letter that happens to be a bound hotkey into any
      search box or text field elsewhere in the app doesn't fire it.
    - Skips if the matched button isn't actually visible (`offsetParent ===
      null`, the standard cheap "is this element display:none" check) --
      covers the Review tab's buttons existing in the DOM but hidden while
      a different tab is the one currently selected.
    - Skips on any ctrl/alt/meta modifier combo (plain letter keys only),
      so it doesn't fight browser/OS shortcuts.
    """
    js = f"""
    <script>
    (function() {{
        const doc = window.parent.document;
        const mapping = {json.dumps(mapping)};
        if (!doc.__hotkeyListenerAttached) {{
            doc.addEventListener('keydown', function(e) {{
                const active = doc.activeElement;
                if (active) {{
                    const tag = active.tagName;
                    if (tag === 'INPUT' || tag === 'TEXTAREA' || active.isContentEditable) return;
                }}
                if (e.ctrlKey || e.altKey || e.metaKey) return;
                const key = e.key.toLowerCase();
                const map = doc.__hotkeyMap || {{}};
                const entry = Object.entries(map).find(([, v]) => v === key);
                if (!entry) return;
                const btn = doc.querySelector(`.st-key-${{entry[0]}} button`);
                if (!btn || btn.offsetParent === null) return;
                e.preventDefault();
                btn.click();
            }});
            doc.__hotkeyListenerAttached = true;
        }}
        doc.__hotkeyMap = mapping;
    }})();
    </script>
    """
    components.html(js, height=0, width=0)
