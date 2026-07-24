"""
Unified review queue: merges four structurally different pending-decision
sources (new persons/places awaiting curation, person duplicate candidates,
place duplicate candidates, open review-queue items) into one list of
QueueItem cards, so a reviewer can work through every kind of decision in one
keyboard-driven pass instead of switching between grid-based tabs.

Every mutation here calls straight into an existing db.py function -- no new
mutation codepaths, only new read/assembly logic on top of what the
grid-based tabs already use.
"""
from dataclasses import dataclass, field

import db
from diff_render import blank

# Name-similarity score (from db.search_authority's rapidfuzz token_sort_ratio)
# at or above which a New Entities match is flagged as high-confidence --
# merge is always offered regardless of score (a lower score can still be a
# correct match the reviewer can see for themselves in the diff; forcing
# "Add to authority" instead when merge wasn't offered was creating avoidable
# duplicate canonical entries), but this threshold controls how it's
# presented: primary-styled and ordered first when at/above it, secondary and
# ordered last (just before "next") otherwise.
MERGE_SUGGEST_THRESHOLD = 90

ALL_ITEM_TYPES = {"new_person", "new_place", "person_dup", "place_dup", "review_item"}


@dataclass
class QueueAction:
    hotkey: str          # single-char keyboard binding, e.g. "a"; unique within one item's action list
    action: str           # semantic name apply_action() dispatches on, e.g. "add" -- NOT the hotkey
    label: str            # button/legend text, e.g. "Add to authority (a)"
    style: str = "secondary"   # "primary" | "secondary" | "danger"


@dataclass
class QueueItem:
    item_id: str                  # globally unique, e.g. "new_person:123"
    item_type: str                 # one of ALL_ITEM_TYPES
    volume: int | None
    header: str
    subheader: str
    left_label: str
    right_label: str
    diff_rows: list                # list[(label, left_val_str, right_val_str)]
    actions: list                  # list[QueueAction]
    payload: dict                  # ids the action handlers need
    sort_score: float = 0.0        # higher = reviewed first within default sort


@dataclass
class QueueFilter:
    volumes: list | None = None                 # None = all volumes
    item_types: set = field(default_factory=lambda: set(ALL_ITEM_TYPES))
    search: str = ""
    confidence: str | None = None                # dup candidates only
    sort: str = "default"                         # "default" | "score_desc" | "score_asc" | "name"


def _matches_search(item: QueueItem, search: str) -> bool:
    if not search.strip():
        return True
    needle = search.strip().lower()
    haystack = " ".join([item.header, item.subheader] +
                         [v for _, lv, rv in item.diff_rows for v in (lv, rv)]).lower()
    return needle in haystack


def _person_diff_rows(left: dict, right: dict | None) -> list:
    from diff_render import COMPARE_ROWS, field_value
    right = right or {}
    return [(label, field_value(left, lk), field_value(right, rk))
            for label, lk, rk in COMPARE_ROWS["person"]]


def _place_diff_rows(left: dict, right: dict | None) -> list:
    from diff_render import COMPARE_ROWS, field_value
    right = right or {}
    return [(label, field_value(left, lk), field_value(right, rk))
            for label, lk, rk in COMPARE_ROWS["place"]]


def _to_int(v):
    """source_volume comes back from pandas as a genuine int only if every
    row in the whole result set happened to be non-null -- a single NULL
    anywhere in that column upcasts the WHOLE column to float64 (numpy int
    arrays can't hold NaN), so e.g. row 4 -> 4.0 even though its own value
    was never missing. QueueItem.volume must be a real int: review_app.py
    formats it with :02d, which raises ValueError on a float."""
    return None if v is None else int(v)


def _is_high_confidence(match: dict | None) -> bool:
    return bool(match) and match.get("_match_score", 0) >= MERGE_SUGGEST_THRESHOLD


def _match_subheader(match: dict | None) -> str:
    if not match:
        return "No plausible authority match found."
    score = match.get("_match_score", 0)
    confidence = "High-confidence" if _is_high_confidence(match) else "Possible"
    return f"{confidence} authority match: {match['display_id']}  (score {score:.0f})"


def _merge_action(match: dict | None):
    """Merge is always offered when any match exists, regardless of score --
    only its styling/position signals confidence (primary + first when
    high-confidence, secondary + last-before-next otherwise), so a reviewer
    can still merge a lower-scoring but visibly-correct match instead of
    being forced into "Add to authority", which would mint a duplicate
    canonical entry alongside the real one."""
    if not match:
        return None
    high = _is_high_confidence(match)
    label = f"Merge into {match['display_id']} (m)" if high else f"Merge into {match['display_id']}? (m)"
    return QueueAction("m", "merge", label, "primary" if high else "secondary")


def _build_new_person_items(volumes: list | None) -> list:
    items = []
    df = db.get_persons(status="provisional", review_status="")
    if volumes:
        df = df[df["source_volume"].isin(volumes)]
    for _, row in df.iterrows():
        row = row.to_dict()
        matches = db.search_authority("person", row["canonical_name"], limit=1)
        match = matches[0] if matches else None
        merge_action = _merge_action(match)
        high_confidence = _is_high_confidence(match)

        actions = []
        if merge_action and high_confidence:
            actions.append(merge_action)
        actions += [
            QueueAction("o", "ok", "OK (o)"),
            QueueAction("a", "add", "Add to authority (a)", "primary"),
            QueueAction("k", "skip", "Skip (k)"),
        ]
        if merge_action and not high_confidence:
            actions.append(merge_action)
        actions.append(QueueAction("n", "next", "Next / not sure yet (n)"))
        items.append(QueueItem(
            item_id=f"new_person:{row['person_pk']}",
            item_type="new_person",
            volume=_to_int(row["source_volume"]),
            header=f"{row['canonical_name']}  ({row['display_id']})",
            subheader=_match_subheader(match),
            left_label="New person", right_label="Authority match",
            diff_rows=_person_diff_rows(row, match),
            actions=actions,
            payload={"pk": row["person_pk"], "match_pk": match["person_pk"] if match else None},
            sort_score=match.get("_match_score", 0) if match else 0,
        ))
    return items


def _build_new_place_items(volumes: list | None) -> list:
    items = []
    df = db.get_places(status="provisional", review_status="")
    if volumes:
        df = df[df["source_volume"].isin(volumes)]
    for _, row in df.iterrows():
        row = row.to_dict()
        matches = db.search_authority("place", row["canonical_name"], limit=1)
        match = matches[0] if matches else None
        merge_action = _merge_action(match)
        high_confidence = _is_high_confidence(match)

        actions = []
        if merge_action and high_confidence:
            actions.append(merge_action)
        actions += [
            QueueAction("o", "ok", "OK (o)"),
            QueueAction("a", "add", "Add to authority (a)", "primary"),
            QueueAction("k", "skip", "Skip (k)"),
            QueueAction("x", "no_match", "No match / not a real place (x)"),
        ]
        if merge_action and not high_confidence:
            actions.append(merge_action)
        actions.append(QueueAction("n", "next", "Next / not sure yet (n)"))
        items.append(QueueItem(
            item_id=f"new_place:{row['place_pk']}",
            item_type="new_place",
            volume=_to_int(row["source_volume"]),
            header=f"{row['canonical_name']}  ({row['display_id']})",
            subheader=_match_subheader(match),
            left_label="New place", right_label="Authority match",
            diff_rows=_place_diff_rows(row, match),
            actions=actions,
            payload={"pk": row["place_pk"], "match_pk": match["place_pk"] if match else None},
            sort_score=match.get("_match_score", 0) if match else 0,
        ))
    return items


def _build_person_dup_items(volumes: list | None) -> list:
    items = []
    df = db.get_person_duplicate_candidates(decision="")
    if volumes:
        df = df[df["a_source"].isin([f"vol{v:02d}" for v in volumes]) |
                 df["b_source"].isin([f"vol{v:02d}" for v in volumes])]
    for _, row in df.iterrows():
        row = row.to_dict()
        left = {"canonical_name": row["a_canonical_name"], "occupation": row["a_occupation"],
                "title": row["a_title"], "floruit_start": row["a_floruit_start"],
                "floruit_end": row["a_floruit_end"]}
        right = {"canonical_name": row["b_canonical_name"], "occupation": row["b_occupation"],
                 "title": row["b_title"], "floruit_start": row["b_floruit_start"],
                 "floruit_end": row["b_floruit_end"]}
        diff_rows = [
            ("Name", blank(left["canonical_name"]), blank(right["canonical_name"])),
            ("Source", row["a_source"], row["b_source"]),
            ("Occupation", blank(left["occupation"]), blank(right["occupation"])),
            ("Title", blank(left["title"]), blank(right["title"])),
            ("Floruit", f"{blank(left['floruit_start'])} -- {blank(left['floruit_end'])}",
                        f"{blank(right['floruit_start'])} -- {blank(right['floruit_end'])}"),
        ]
        items.append(QueueItem(
            item_id=f"person_dup:{row['candidate_pk']}",
            item_type="person_dup",
            volume=None,
            header=f"{row['a_canonical_name']}  ({row['a_display_id']})  vs.  "
                   f"{row['b_canonical_name']}  ({row['b_display_id']})",
            subheader=f"name_score={row['name_score']:.0f}  ·  dates={row['date_status']}  ·  "
                      f"{row['classification']} ({row['confidence']})",
            left_label=f"A · {row['a_display_id']}", right_label=f"B · {row['b_display_id']}",
            diff_rows=diff_rows,
            actions=[
                QueueAction("s", "same", "Same — flag only (s)"),
                QueueAction("m", "merge", "Same — merge now (m)", "primary"),
                QueueAction("d", "different", "Different (d)"),
                QueueAction("n", "next", "Next / not sure yet (n)"),
            ],
            payload={"candidate_pk": row["candidate_pk"], "person_a_pk": row["person_a_pk"],
                     "person_b_pk": row["person_b_pk"]},
            sort_score=row["name_score"] or 0,
        ))
    return items


def _build_place_dup_items(volumes: list | None) -> list:
    items = []
    df = db.get_place_duplicate_candidates(decision="")
    if volumes:
        df = df[df["source_volume"].isin(volumes)]
    for _, row in df.iterrows():
        row = row.to_dict()
        diff_rows = [
            ("Name", blank(row.get("place_canonical_name") or row.get("di_name")), blank(row["candidate_name"])),
            ("Type / sysla", blank(row["di_place_type"]), blank(row["candidate_sysla"])),
            ("Region / hreppur", blank(row["di_region"]), blank(row["candidate_hreppur"])),
            ("Coordinates", "", f"{blank(row['candidate_lat'])}, {blank(row['candidate_lng'])}"),
        ]
        items.append(QueueItem(
            item_id=f"place_dup:{row['candidate_pk']}",
            item_type="place_dup",
            volume=_to_int(row["source_volume"]),
            header=f"{row['place_canonical_name']}  ({row['display_id']})  vs.  nafnid: {row['candidate_name']}",
            subheader=f"name_score={row['name_score']:.1f}  ·  distance={blank(row['distance_km'])} km  ·  "
                      f"flag={row['flag'] or 'none'}",
            left_label="DI place", right_label="nafnid.is candidate",
            diff_rows=diff_rows,
            actions=[
                QueueAction("s", "same", "Same (s)", "primary"),
                QueueAction("d", "different", "Different (d)"),
                QueueAction("n", "next", "Next / not sure yet (n)"),
            ],
            payload={"candidate_pk": row["candidate_pk"]},
            sort_score=row["name_score"] or 0,
        ))
    return items


def _build_review_items(volumes: list | None) -> list:
    items = []
    for vn in (volumes or db.get_volumes()):
        df = db.get_open_review_items(vn)
        df = df[df["decision"].fillna("") == ""]
        for _, row in df.iterrows():
            row = row.to_dict()
            diff_rows = [
                ("Extracted name", blank(row["extracted_name"]), blank(row["closest_match"])),
                ("Role", blank(row["role_category"]), blank(row["role"])),
                ("Score", "", blank(row["score"])),
            ]
            items.append(QueueItem(
                item_id=f"review_item:{row['review_item_pk']}",
                item_type="review_item",
                volume=vn,
                header=f"{row['extracted_name']}  ({row['entity_type']})",
                subheader=f"Proposed match: {row['closest_match']}  (score {blank(row['score'])})",
                left_label="Extracted", right_label="Proposed match",
                diff_rows=diff_rows,
                actions=[
                    QueueAction("a", "accept", "Accept — use proposed match (a)", "primary"),
                    QueueAction("r", "reject", "Reject — treat as new entity (r)"),
                    QueueAction("n", "next", "Next / not sure yet (n)"),
                ],
                payload={"review_item_pk": row["review_item_pk"]},
                sort_score=row["score"] or 0,
            ))
    return items


_BUILDERS = {
    "new_person": _build_new_person_items,
    "new_place": _build_new_place_items,
    "person_dup": _build_person_dup_items,
    "place_dup": _build_place_dup_items,
    "review_item": _build_review_items,
}


def build_queue(filt: QueueFilter) -> list:
    """Assembles the live, undecided-only queue for the given filter. Called
    fresh on every render (cheap at this data scale, and the only way an
    already-decided item reliably drops out immediately)."""
    items = []
    for item_type in filt.item_types:
        items.extend(_BUILDERS[item_type](filt.volumes))

    if filt.confidence:
        items = [i for i in items if filt.confidence.lower() in i.subheader.lower()]
    if filt.search:
        items = [i for i in items if _matches_search(i, filt.search)]

    if filt.sort == "score_desc":
        items.sort(key=lambda i: i.sort_score, reverse=True)
    elif filt.sort == "score_asc":
        items.sort(key=lambda i: i.sort_score)
    elif filt.sort == "name":
        items.sort(key=lambda i: i.header.lower())
    # "default": item-type groups in ALL_ITEM_TYPES iteration order, each
    # already sorted highest-confidence-first by its own db.py query.

    return items


def apply_action(item: QueueItem, action_key: str) -> dict:
    """Dispatches action_key to the right db.py mutator for item.item_type.
    "next" (available on every item) is a pure no-op -- the caller advances
    the queue position without this function mutating anything."""
    p = item.payload

    if action_key == "next":
        return {"action": "next"}

    if item.item_type == "new_person":
        if action_key == "ok":
            db.update_person(p["pk"], review_status="ok")
        elif action_key == "add":
            db.update_person(p["pk"], review_status="add")
        elif action_key == "skip":
            db.update_person(p["pk"], review_status="skip")
        elif action_key == "merge":
            db.merge_into_authority("person", p["pk"], p["match_pk"])
        else:
            raise ValueError(f"Unknown action {action_key!r} for new_person")
        return {"action": action_key, "pk": p["pk"]}

    if item.item_type == "new_place":
        if action_key == "ok":
            db.update_place(p["pk"], review_status="ok")
        elif action_key == "add":
            db.update_place(p["pk"], review_status="add")
        elif action_key == "skip":
            db.update_place(p["pk"], review_status="skip")
        elif action_key == "no_match":
            db.update_place(p["pk"], review_status="no_match")
        elif action_key == "merge":
            db.merge_into_authority("place", p["pk"], p["match_pk"])
        else:
            raise ValueError(f"Unknown action {action_key!r} for new_place")
        return {"action": action_key, "pk": p["pk"]}

    if item.item_type == "person_dup":
        if action_key == "same":
            db.record_person_duplicate_decision(p["candidate_pk"], "same")
        elif action_key == "different":
            db.record_person_duplicate_decision(p["candidate_pk"], "different")
        elif action_key == "merge":
            survivor, dropped_pk = sorted((p["person_a_pk"], p["person_b_pk"]))
            result = db.merge_persons(survivor, [dropped_pk])
            # Mark the decision too -- merge_persons already relinked this
            # candidate row's pks to the survivor on both sides, so leaving
            # decision blank would otherwise resurface it as a nonsensical
            # "duplicate of itself" pending item.
            db.record_person_duplicate_decision(p["candidate_pk"], "same")
            return {"action": action_key, "survivor_pk": survivor, **result}
        else:
            raise ValueError(f"Unknown action {action_key!r} for person_dup")
        return {"action": action_key, "candidate_pk": p["candidate_pk"]}

    if item.item_type == "place_dup":
        if action_key == "same":
            db.record_place_duplicate_decision(p["candidate_pk"], "same")
        elif action_key == "different":
            db.record_place_duplicate_decision(p["candidate_pk"], "different")
        else:
            raise ValueError(f"Unknown action {action_key!r} for place_dup")
        return {"action": action_key, "candidate_pk": p["candidate_pk"]}

    if item.item_type == "review_item":
        if action_key == "accept":
            db.set_review_decision(p["review_item_pk"], "accept")
            result = db.apply_review_decision(p["review_item_pk"])
        elif action_key == "reject":
            db.set_review_decision(p["review_item_pk"], "reject")
            result = db.apply_review_decision(p["review_item_pk"])
        else:
            raise ValueError(f"Unknown action {action_key!r} for review_item")
        return {"action": action_key, **result}

    raise ValueError(f"Unknown item_type {item.item_type!r}")
