"""
Loader for place_names_authority.csv.

Authority file column layout (column names stripped of whitespace):
    place_id, canonical_name, wikidata_id, variants,
    x(N) coords, y(W) coords, modern country, notes

Provides a PlaceAuthority object for exact and variant-name lookups used by
03_resolve_entities.py and 04b_propagate_corrections.py.
"""

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

# Matches a trailing parenthetical qualifier, e.g. " (farm church)" or " (Hún.)"
_PAREN_TAIL = re.compile(r'\s*\([^)]*\)\s*$')


def split_variants(raw: str) -> list[str]:
    """Split on semicolons that are NOT inside parentheses."""
    parts: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in (raw or ""):
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth = max(0, depth - 1)
        elif ch == ';' and depth == 0:
            part = ''.join(buf).strip()
            if part:
                parts.append(part)
            buf = []
            continue
        buf.append(ch)
    part = ''.join(buf).strip()
    if part:
        parts.append(part)
    return parts

AUTHORITY_PATH = Path(__file__).parent / "place_names_authority.csv"

# Map authority file column names (after strip) → PlaceEntry fields
_COL_MAP = {
    "place_id":       "place_id",
    "canonical_name": "canonical_name",
    "wikidata_id":    "wikidata_id",
    "variants":       "variants_raw",
    "x(n) coords":    "lat",
    "y(w) coords":    "lng",
    "modern country": "modern_country",
    "notes":          "notes",
}


@dataclass
class PlaceEntry:
    place_id:       str
    canonical_name: str
    wikidata_id:    str = ""
    variants:       list[str] = field(default_factory=list)
    lat:            str = ""
    lng:            str = ""
    modern_country: str = ""
    notes:          str = ""

    def all_names(self) -> list[str]:
        """All known name forms, lowercased, for index building.

        Each name is indexed twice when it carries a parenthetical qualifier:
        once as-is and once with the qualifier stripped, so that a bare lookup
        for e.g. 'Þverá' still finds 'Þverá (farm church)'.
        """
        def _add(names: list[str], s: str) -> None:
            s = s.strip().strip('"').strip("'").lower()
            if s:
                names.append(s)
                base = _PAREN_TAIL.sub('', s).strip()
                if base and base != s:
                    names.append(base)

        names: list[str] = []
        _add(names, self.canonical_name)
        for v in self.variants:
            _add(names, v)
        return list(dict.fromkeys(names))  # deduplicated, order-preserving


class PlaceAuthority:
    """
    In-memory index of place_names_authority.csv.
    Lookup is exact (case-insensitive) on canonical_name and all variant forms.
    """

    def __init__(self, path: Path = AUTHORITY_PATH):
        self.entries: list[PlaceEntry] = []
        self._name_index:  dict[str, PlaceEntry] = {}  # name form → entry
        self._wikidata_index: dict[str, PlaceEntry] = {}  # QID → first entry
        if path.exists():
            self._load(path)
        else:
            print(f"[place_authority] {path.name} not found — run seed_place_names.py first.")

    def _load(self, path: Path):
        with open(path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            # Normalize header names: strip whitespace and lowercase
            raw_fields = reader.fieldnames or []
            norm_fields = [c.strip().lower() for c in raw_fields]

            for raw_row in reader:
                # Re-key the row with normalized column names
                row = {norm_fields[i]: (v or "").strip()
                       for i, (k, v) in enumerate(raw_row.items())
                       if i < len(norm_fields)}

                pid       = row.get("place_id", "").strip()
                canonical = row.get("canonical_name", "").strip()
                if not pid or not canonical:
                    continue

                variants_raw = row.get("variants", "") or row.get("variants_raw", "")
                variants = split_variants(variants_raw)

                entry = PlaceEntry(
                    place_id=pid,
                    canonical_name=canonical,
                    wikidata_id=(row.get("wikidata_id") or "").strip(),
                    variants=variants,
                    lat=(row.get("x(n) coords") or "").strip(),
                    lng=(row.get("y(w) coords") or "").strip(),
                    modern_country=(row.get("modern country") or "").strip(),
                    notes=(row.get("notes") or "").strip(),
                )
                self.entries.append(entry)

                # Index all name forms
                for name in entry.all_names():
                    if name and name not in self._name_index:
                        self._name_index[name] = entry

                # Index by wikidata_id
                if entry.wikidata_id and entry.wikidata_id not in self._wikidata_index:
                    self._wikidata_index[entry.wikidata_id] = entry

        print(f"[place_authority] Loaded {len(self.entries)} entries, "
              f"{len(self._name_index)} name forms, "
              f"{len(self._wikidata_index)} Wikidata QIDs.")

    def lookup(self, name: str) -> PlaceEntry | None:
        """Exact case-insensitive lookup by any known name form."""
        return self._name_index.get((name or "").strip().lower())

    def lookup_wikidata(self, qid: str) -> PlaceEntry | None:
        """Lookup by Wikidata QID."""
        return self._wikidata_index.get((qid or "").strip())

    def find(self, canonical_name: str, wikidata_id: str = "",
             variant_names: list[str] | None = None) -> PlaceEntry | None:
        """
        Multi-strategy lookup used by 04b. Tries in order:
          1. canonical_name exact match
          1b. canonical_name with trailing parenthetical stripped
              (e.g. "Hamburg (Hamaburg/Hammaburg)" → "Hamburg")
          2. wikidata_id match
          3. any variant_name exact match
        Returns first match or None.
        """
        entry = self.lookup(canonical_name)
        if entry:
            return entry

        canonical_stripped = _PAREN_TAIL.sub("", canonical_name).strip()
        if canonical_stripped and canonical_stripped != canonical_name:
            entry = self.lookup(canonical_stripped)
            if entry:
                return entry

        if wikidata_id:
            entry = self.lookup_wikidata(wikidata_id)
            if entry:
                return entry

        for v in (variant_names or []):
            entry = self.lookup(v)
            if entry:
                return entry

        return None

    def __len__(self):
        return len(self.entries)
