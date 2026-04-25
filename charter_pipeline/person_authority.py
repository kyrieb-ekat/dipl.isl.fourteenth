"""
Loader for person_names_authority.csv.

Authority file column layout:
    person_id, canonical_name, wikidata_id, variants,
    patronymic, occupation, title, floruit_start, floruit_end, gender, notes

Provides a PersonAuthority object for exact and variant-name lookups,
mirroring the PlaceAuthority pattern in place_authority.py.
"""

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

AUTHORITY_PATH = Path(__file__).parent / "person_names_authority.csv"

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


@dataclass
class PersonEntry:
    person_id:      str
    canonical_name: str
    wikidata_id:    str = ""
    variants:       list[str] = field(default_factory=list)
    patronymic:     str = ""
    occupation:     str = ""
    title:          str = ""
    floruit_start:  str = ""
    floruit_end:    str = ""
    gender:         str = ""
    notes:          str = ""

    def all_names(self) -> list[str]:
        """All known name forms, lowercased, with parenthetical-stripped duplicates."""
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
        return list(dict.fromkeys(names))


class PersonAuthority:
    """
    In-memory index of person_names_authority.csv.
    Lookup is exact (case-insensitive) on canonical_name and all variant forms.
    """

    def __init__(self, path: Path = AUTHORITY_PATH):
        self.entries: list[PersonEntry] = []
        self._name_index: dict[str, PersonEntry] = {}
        self._wikidata_index: dict[str, PersonEntry] = {}
        if path.exists():
            self._load(path)
        else:
            print(f"[person_authority] {path.name} not found — run seed_person_names.py first.")

    def _load(self, path: Path):
        with open(path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            raw_fields = reader.fieldnames or []
            norm_fields = [c.strip().lower() for c in raw_fields]

            for raw_row in reader:
                row = {norm_fields[i]: (v or "").strip()
                       for i, (k, v) in enumerate(raw_row.items())
                       if i < len(norm_fields)}

                pid       = row.get("person_id", "").strip()
                canonical = row.get("canonical_name", "").strip()
                if not pid or not canonical:
                    continue

                variants = split_variants(row.get("variants", ""))

                entry = PersonEntry(
                    person_id=pid,
                    canonical_name=canonical,
                    wikidata_id=row.get("wikidata_id", ""),
                    variants=variants,
                    patronymic=row.get("patronymic", ""),
                    occupation=row.get("occupation", ""),
                    title=row.get("title", ""),
                    floruit_start=row.get("floruit_start", ""),
                    floruit_end=row.get("floruit_end", ""),
                    gender=row.get("gender", ""),
                    notes=row.get("notes", ""),
                )
                self.entries.append(entry)

                for name in entry.all_names():
                    if name and name not in self._name_index:
                        self._name_index[name] = entry

                if entry.wikidata_id and entry.wikidata_id not in self._wikidata_index:
                    self._wikidata_index[entry.wikidata_id] = entry

        print(f"[person_authority] Loaded {len(self.entries)} entries, "
              f"{len(self._name_index)} name forms, "
              f"{len(self._wikidata_index)} Wikidata QIDs.")

    def lookup(self, name: str) -> PersonEntry | None:
        return self._name_index.get((name or "").strip().lower())

    def lookup_wikidata(self, qid: str) -> PersonEntry | None:
        return self._wikidata_index.get((qid or "").strip())

    def find(self, canonical_name: str, wikidata_id: str = "",
             variant_names: list[str] | None = None) -> PersonEntry | None:
        """
        Multi-strategy lookup. Tries in order:
          1. canonical_name exact match
          1b. canonical_name with trailing parenthetical stripped
          2. wikidata_id match
          3. any variant_name exact match
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
