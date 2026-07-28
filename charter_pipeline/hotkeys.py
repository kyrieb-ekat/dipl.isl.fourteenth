"""
Minimal keyboard-shortcut binder for the review card queue.

Replaces an earlier attempt built on the third-party `streamlit-shortcuts`
package: that library's keydown listener has no guard against firing while
the user is typing in a text input, AND no guard against firing when its
target isn't even visible. Under the old st.tabs() layout Streamlit rendered
every tab body on every script run regardless of which tab was selected, so
the Review tab's bindings existed in the DOM at all times -- without a
visibility check, typing a letter that happened to match a bound hotkey (e.g.
"k" in "biskups") into a search box on a completely different tab would
silently click a hidden button. Confirmed bug, not a hypothetical.

bind_hotkeys() adds both guards, and keeps bindings in per-scope maps so more
than one renderer can bind in a single script run (see the `scope` docstring).
No third-party dependency.
"""
import json

import streamlit.components.v1 as components


def bind_hotkeys(mapping: dict, scope: str = "default") -> None:
    """mapping: {widget_key: single_char_hotkey}. Binds each hotkey to click
    the st.button rendered with that key (Streamlit tags its container with a
    `st-key-{key}` class), via one keydown listener on the parent document.

    `scope` namespaces the binding map. This used to be a single
    `doc.__hotkeyMap = mapping` assignment, which meant the LAST caller in a
    script run silently won and every earlier renderer lost its keys with no
    error at all. Harmless while exactly one card ever rendered at a time;
    broken the moment two do (a person-cluster screen showing several members,
    or a proposal-conflict view showing competing cards). Each renderer now
    owns a scope and replaces only its own entries, so bindings from different
    renderers coexist.

    Stale scopes are self-cleaning: a scope whose renderer stopped running has
    no matching buttons in the DOM, so the lookup below finds nothing and the
    keypress is ignored.

    Two renderers binding the SAME letter in one run is inherently ambiguous;
    the first visible match wins, in scope-insertion order. Give concurrent
    renderers distinct letters, or bind only the focused one.

    Guards, all three required by observed bugs:
    - Skips while focus is in an INPUT/TEXTAREA/contenteditable element, so
      typing a bound letter into a search box doesn't fire it.
    - Skips buttons that aren't actually visible (`offsetParent === null`,
      the standard cheap "is this display:none" check).
    - Skips on any ctrl/alt/meta combo (plain letter keys only), so it doesn't
      fight browser/OS shortcuts.
    """
    js = f"""
    <script>
    (function() {{
        const doc = window.parent.document;
        const scope = {json.dumps(scope)};
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
                const maps = doc.__hotkeyMaps || {{}};
                // Collect every widget key bound to this letter across all
                // scopes, then click the first one actually on screen.
                for (const scopeName of Object.keys(maps)) {{
                    for (const [widgetKey, letter] of Object.entries(maps[scopeName] || {{}})) {{
                        if (letter !== key) continue;
                        const buttons = doc.querySelectorAll(`.st-key-${{widgetKey}} button`);
                        for (const btn of buttons) {{
                            if (btn.offsetParent === null) continue;
                            e.preventDefault();
                            btn.click();
                            return;
                        }}
                    }}
                }}
            }});
            doc.__hotkeyListenerAttached = true;
        }}
        doc.__hotkeyMaps = doc.__hotkeyMaps || {{}};
        doc.__hotkeyMaps[scope] = mapping;
    }})();
    </script>
    """
    components.html(js, height=0, width=0)
