"""
Morphology-aware comparison of Icelandic place names.

Replaces fuzzy string similarity as the primary mechanism, because fuzzy
similarity is the wrong tool for this data. `rapidfuzz.WRatio` -- what
04a_reconcile_nafnid.py used -- put 6,870 of 12,960 nafnid candidates at
*exactly* 90.0: its partial_ratio component fires whenever the shorter name
appears anywhere inside the longer one, so `Hjarðarholt`/`Árholt`,
`Dagverðarnes`/`Árnes` and `Keldudal`/`Hraun í Keldudal` all scored a flat 90.
That is not a similarity measure, and it made `candidate_rank` arbitrary across
half the data. (07_find_person_duplicates.py:45 already documented why WRatio
is wrong for names; the place path used it anyway.)

The structural fact that makes a better approach possible:

    In a compound place name X-Y, only the GENERIC element Y inflects. The
    SPECIFIC element X is frozen -- normally as a genitive of its own source
    noun -- and is identical across every case-form of the compound.
    Hólastaður / Hólastað / Hólastaðar: only -staður changes.

So the specific element can be compared for **exact equality**, and the generic
resolved by **paradigm-cell lookup**. That makes most comparisons deterministic
rather than probabilistic, and it handles cases no fuzzy scorer can:
`Akrar`/`Ökrum` is the same farm (nom.pl / dat.pl of `akur`, with u-umlaut) but
scores 40-60 under every string metric.

Diagnostic order, per the domain reference this implements:

  1. Normalise orthography -- but for COMPARISON ONLY, never by rewriting a
     stored name (see normalize_orthography).
  2. Resolve the trailing string against the paradigms (Section: PARADIGMS).
     A hit means same referent, different grammatical case.
  3. Otherwise check whether it is an independent generic lexeme attached to a
     known base name -- that signals a subsidiary parcel (a distinct entity),
     not a variant.
  4. Only if none of the above applies, fall back to fuzzy scoring.

Coverage scales directly with PARADIGMS: extend it as new generics appear.
"""
import re
import unicodedata

from rapidfuzz import distance, fuzz

# ── Section 2: declension paradigms ─────────────────────────────────────────
#
# Cell order is fixed: nom.sg, acc.sg, dat.sg, gen.sg, nom.pl, acc.pl, dat.pl,
# gen.pl. Duplicate forms within a paradigm are normal and intended (e.g.
# dalur's acc.sg and dat.sg are both "dal").

CASES = ("nom.sg", "acc.sg", "dat.sg", "gen.sg",
         "nom.pl", "acc.pl", "dat.pl", "gen.pl")

PARADIGMS: dict[str, tuple[str, ...]] = {
    # masculine
    "hóll":    ("hóll", "hól", "hóli", "hóls", "hólar", "hóla", "hólum", "hóla"),
    "dalur":   ("dalur", "dal", "dal", "dals", "dalir", "dali", "dölum", "dala"),
    "fjörður": ("fjörður", "fjörð", "firði", "fjarðar",
                "firðir", "firði", "fjörðum", "fjarða"),
    "völlur":  ("völlur", "völl", "velli", "vallar",
                "vellir", "völlu", "völlum", "valla"),
    "staður":  ("staður", "stað", "stað", "staðar",
                "staðir", "staði", "stöðum", "staða"),
    "bær":     ("bær", "bæ", "bæ", "bæjar", "bæir", "bæi", "bæjum", "bæja"),
    "reki":    ("reki", "reka", "reka", "reka", "rekar", "reka", "rekum", "reka"),
    "vogur":   ("vogur", "vog", "vogi", "vogs", "vogar", "voga", "vogum", "voga"),
    "ós":      ("ós", "ós", "ósi", "óss", "ósar", "ósa", "ósum", "ósa"),
    "tangi":   ("tangi", "tanga", "tanga", "tanga",
                "tangar", "tanga", "töngum", "tanga"),
    "garður":  ("garður", "garð", "garði", "garðs",
                "garðar", "garða", "görðum", "garða"),
    "sandur":  ("sandur", "sand", "sandi", "sands",
                "sandar", "sanda", "söndum", "sanda"),
    # same masculine class + u-umlaut; not in the source reference but this is
    # the Akrar/Ökrum case, which is precisely what fuzzy scoring cannot do.
    "akur":    ("akur", "akur", "akri", "akurs", "akrar", "akra", "ökrum", "akra"),
    # feminine
    "vík":     ("vík", "vík", "vík", "víkur", "víkur", "víkur", "víkum", "víka"),
    "á":       ("á", "á", "á", "ár", "ár", "ár", "ám", "áa"),
    "tunga":   ("tunga", "tungu", "tungu", "tungu",
                "tungur", "tungur", "tungum", "tungna"),
    "brekka":  ("brekka", "brekku", "brekku", "brekku",
                "brekkur", "brekkur", "brekkum", "brekkna"),
    "mörk":    ("mörk", "mörk", "mörku", "merkur",
                "merkur", "merkur", "mörkum", "marka"),
    "eyri":    ("eyri", "eyri", "eyri", "eyrar", "eyrar", "eyrar", "eyrum", "eyra"),
    "höfn":    ("höfn", "höfn", "höfn", "hafnar",
                "hafnir", "hafnir", "höfnum", "hafna"),
    "ey":      ("ey", "ey", "ey", "eyjar", "eyjar", "eyjar", "eyjum", "eyja"),
    "mýri":    ("mýri", "mýri", "mýri", "mýrar", "mýrar", "mýrar", "mýrum", "mýra"),
    "hlíð":    ("hlíð", "hlíð", "hlíð", "hlíðar",
                "hlíðar", "hlíðar", "hlíðum", "hlíða"),
    # neuter
    "nes":     ("nes", "nes", "nesi", "ness", "nes", "nes", "nesjum", "nesja"),
    "fell":    ("fell", "fell", "felli", "fells", "fell", "fell", "fellum", "fella"),
    "land":    ("land", "land", "landi", "lands", "lönd", "lönd", "löndum", "landa"),
    "kot":     ("kot", "kot", "koti", "kots", "kot", "kot", "kotum", "kota"),
    "holt":    ("holt", "holt", "holti", "holts", "holt", "holt", "holtum", "holta"),
    "gerði":   ("gerði", "gerði", "gerði", "gerðis",
                "gerði", "gerði", "gerðum", "gerða"),
    "sker":    ("sker", "sker", "skeri", "skers", "sker", "sker", "skerjum", "skerja"),
    "hraun":   ("hraun", "hraun", "hrauni", "hrauns",
                "hraun", "hraun", "hraunum", "hrauna"),
    "horn":    ("horn", "horn", "horni", "horns", "horn", "horn", "hornum", "horna"),
    "berg":    ("berg", "berg", "bergi", "bergs", "berg", "berg", "bergum", "berga"),
    "sel":     ("sel", "sel", "seli", "sels", "sel", "sel", "seljum", "selja"),
}

# Generics whose presence signals a *subsidiary parcel* of a base name rather
# than a spelling of it -- driftage rights, an outfield, a landing place. When
# one of these is attached to a genitive of another place's base noun, the two
# are related but distinct entities (Hólar -> Hólareki), and both deserve their
# own record.
SUBSIDIARY_GENERICS = frozenset({
    "reki", "eyri", "ey", "holt", "mörk", "gerði", "garður", "horn", "sker",
    "vogur", "ós", "tangi", "höfn", "sandur", "berg", "sel", "brekka", "tunga",
})

GENITIVE_CASES = frozenset({"gen.sg", "gen.pl"})

_VOWELS = "aeiouyáéíóúýöæø"


# ── Section 4: orthographic variance ────────────────────────────────────────

def normalize_orthography(name: str) -> str:
    """Folds scribal spelling variance so it doesn't masquerade as grammar.

    COMPARISON ONLY. Never write the result back over a stored name: the
    transformations here are lossy (`ss`->`s` and the v/f rule especially), and
    persisting them would silently merge distinct names. Keeping orthography
    and grammar as separate axes also matters beyond matching -- conflating
    them distorts any later scribal-hand analysis.

    Deliberately does NOT fold accents. `ö`/`o` and `á`/`a` are distinct
    letters in Icelandic, not decorated variants, and paradigm lookup depends
    on the distinction (`völlum` vs `vollum`, `mörk` vs `mork`). 04a's
    normalize_name(fold_accents=True) was destroying exactly that signal.
    """
    if not name:
        return ""
    n = unicodedata.normalize("NFC", name).strip().lower()
    n = re.sub(r"[.,;:'\"]", "", n)
    n = re.sub(r"\s+", " ", n)
    # Intervocalic f ~ v is phonological, not morphological.
    n = re.sub(rf"([{_VOWELS}])f([{_VOWELS}])", r"\1v\2", n)
    # Graphic i/j variance before a vowel.
    n = re.sub(rf"j([{_VOWELS}])", r"i\1", n)
    # Degemination ONLY of -ss at a morpheme boundary (Ness ~ nes, Hvammss- ~
    # Hvamms-). NOT general gemination collapse: geminates inside a stem are
    # lexical, and collapsing them destroys the paradigms outright -- an
    # earlier version turned `vellir` into `velir` and `völlum` into `völum`,
    # so every -vellir name stopped resolving.
    n = re.sub(r"ss(?=$|[^aeiouyáéíóúýöæø])", "s", n)
    return n


def _strip_r_ur(form: str) -> str:
    """Masculine nom.sg -r vs -ur is period/scribal variance, not a case."""
    return form[:-2] + "r" if form.endswith("ur") else form


# form -> set of (lemma, case), keyed on the NORMALISED form.
#
# Built after normalize_orthography deliberately: the index and the lookup key
# must pass through the same function, or any rule that rewrites a paradigm
# form silently stops it matching. The i/j rule does exactly that to `bæjar`
# (-> `bæiar`), which would have made every -bær genitive unresolvable while
# looking like a coverage gap rather than a bug.
_FORM_INDEX: dict[str, set[tuple[str, str]]] = {}
for _lemma, _forms in PARADIGMS.items():
    for _case, _form in zip(CASES, _forms):
        _norm = normalize_orthography(_form)
        _FORM_INDEX.setdefault(_norm, set()).add((_lemma, _case))
        # Register the -r spelling of any -ur form as well. The variance runs
        # in the direction the source has it (a charter may write `dalr` where
        # the dictionary form is `dalur`), so the alternative has to be in the
        # INDEX -- transforming the lookup key instead only ever converts
        # dictionary spellings into period ones, which is backwards.
        _variant = _strip_r_ur(_norm)
        if _variant != _norm:
            _FORM_INDEX.setdefault(_variant, set()).add((_lemma, _case))

# Longest first, so "staðir" wins over "ir" and "ness" over "nes".
_FORMS_BY_LENGTH = tuple(sorted(_FORM_INDEX, key=len, reverse=True))


# ── Section 2 applied: parsing ──────────────────────────────────────────────

class ParsedName:
    """A place name split into its frozen specific and inflected generic.

    `lemma is None` means the trailing string matched no known paradigm, i.e.
    this name is unresolved and comparisons must fall back to fuzzy scoring.
    `specific == ""` means the whole name is a bare generic (a simplex name
    like `Hólar` or `Akrar`), which is what makes those comparable at all.
    """

    __slots__ = ("raw", "normalized", "specific", "lemma", "case")

    def __init__(self, raw, normalized, specific, lemma, case):
        self.raw, self.normalized = raw, normalized
        self.specific, self.lemma, self.case = specific, lemma, case

    @property
    def resolved(self) -> bool:
        return self.lemma is not None

    @property
    def is_simplex(self) -> bool:
        return self.resolved and self.specific == ""

    def __repr__(self):
        return (f"ParsedName({self.raw!r} -> specific={self.specific!r}, "
                f"lemma={self.lemma!r}, case={self.case!r})")


def parse(name: str) -> ParsedName:
    """Splits a name into (frozen specific, generic lemma, case)."""
    norm = normalize_orthography(name)
    if not norm:
        return ParsedName(name, norm, "", None, None)

    candidates = (norm, _strip_r_ur(norm))
    for form in candidates:
        if form in _FORM_INDEX:
            lemma, case = _best_cell(form)
            return ParsedName(name, norm, "", lemma, case)

    for suffix in _FORMS_BY_LENGTH:
        for form in candidates:
            # Require at least 2 characters of specific element -- a 1-char
            # remainder is far more likely to be a mis-split than a real
            # compound.
            if form.endswith(suffix) and len(form) > len(suffix) + 1:
                lemma, case = _best_cell(suffix)
                return ParsedName(name, norm, form[:-len(suffix)], lemma, case)
    return ParsedName(name, norm, norm, None, None)


def _best_cell(form: str) -> tuple[str, str]:
    """Picks one (lemma, case) for an ambiguous form.

    Ambiguity is common and mostly harmless here: what downstream logic needs
    is the LEMMA, and forms shared between paradigms are rare. Ordering is
    fixed (nominative preferred, then alphabetical) purely so results are
    deterministic across runs.
    """
    cells = _FORM_INDEX[form]
    return min(cells, key=lambda lc: (CASES.index(lc[1]) != 0, lc[0], lc[1]))


def is_genitive_of(form: str, lemma: str) -> bool:
    """True if `form` is a genitive cell of `lemma` -- the shape a frozen
    specific element normally takes."""
    if not form or not lemma:
        return False
    f = normalize_orthography(form)
    return any(lm == lemma and case in GENITIVE_CASES
               for lm, case in _FORM_INDEX.get(f, ()))


# ── Comparison ──────────────────────────────────────────────────────────────

SAME = "same"              # same referent, different grammatical case
DIFFERENT = "different"    # distinct places
DERIVED = "derived"        # subsidiary parcel of the other (both are real)
UNRESOLVED = "unresolved"  # paradigms can't decide; use the fuzzy score


def compare(a: str, b: str) -> dict:
    """Compares two place names morphologically.

    Returns {"verdict", "score", "reason", "a", "b"}. `verdict` is one of
    SAME / DIFFERENT / DERIVED / UNRESOLVED; `score` is 0-100 and is only
    meaningful for ranking (see similarity()).
    """
    pa, pb = parse(a), parse(b)

    if pa.normalized and pa.normalized == pb.normalized:
        return _verdict(SAME, 100.0, "identical after orthographic normalisation", pa, pb)

    if pa.resolved and pb.resolved:
        if pa.lemma == pb.lemma:
            if pa.specific == pb.specific:
                return _verdict(
                    SAME, 100.0,
                    f"same generic ({pa.lemma}) and identical specific element "
                    f"({pa.specific or 'none — simplex name'}); "
                    f"{pa.case} vs {pb.case}", pa, pb)
            return _verdict(
                DIFFERENT, similarity(a, b),
                f"same generic (-{pa.lemma}) but different specific element "
                f"({pa.specific!r} vs {pb.specific!r})", pa, pb)

        # Different generics: is one a subsidiary parcel of the other?
        for base, other in ((pa, pb), (pb, pa)):
            if (other.lemma in SUBSIDIARY_GENERICS
                    and base.is_simplex
                    and is_genitive_of(other.specific, base.lemma)):
                return _verdict(
                    DERIVED, similarity(a, b),
                    f"{other.raw!r} is a -{other.lemma} parcel of {base.raw!r} "
                    f"(specific {other.specific!r} is a genitive of "
                    f"{base.lemma})", pa, pb)
        return _verdict(
            DIFFERENT, similarity(a, b),
            f"different generic elements (-{pa.lemma} vs -{pb.lemma})", pa, pb)

    return _verdict(UNRESOLVED, similarity(a, b),
                    "no known generic element on "
                    + ("both" if not (pa.resolved or pb.resolved) else "one")
                    + " side; fuzzy score only", pa, pb)


def _verdict(verdict, score, reason, pa, pb) -> dict:
    return {"verdict": verdict, "score": round(float(score), 1),
            "reason": reason, "a": pa, "b": pb}


def similarity(a: str, b: str) -> float:
    """Compound-aware fuzzy score, 0-100. For RANKING, not gating.

    Used for the ~74% of pairs the paradigms can't resolve, and to order
    candidates within a verdict. Weights the distinguishing element 80/20 over
    the shared generic one, and uses Jaro-Winkler on the specific part because
    it rewards prefix agreement -- which suits Icelandic compounds, where the
    distinguishing element comes first.

    Deliberately avoids partial_ratio (and therefore WRatio): substring
    containment is exactly the signal that produced the flat-90 plateau.
    """
    na, nb = normalize_orthography(a), normalize_orthography(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 100.0

    pa, pb = parse(a), parse(b)
    if pa.resolved and pb.resolved:
        spec = _jw(pa.specific, pb.specific) if (pa.specific or pb.specific) else 100.0
        gen = 100.0 if pa.lemma == pb.lemma else float(fuzz.ratio(pa.lemma, pb.lemma))
        return 0.8 * spec + 0.2 * gen
    if pa.resolved != pb.resolved:
        # One compound, one not: no shared generic to credit, so damp it.
        return _jw(na, nb) * 0.9
    return _jw(na, nb)


def _jw(a: str, b: str) -> float:
    if not a and not b:
        return 100.0
    if not a or not b:
        return 0.0
    return distance.JaroWinkler.similarity(a, b) * 100.0


def explain(a: str, b: str) -> str:
    """One-line human-readable account of a comparison, for the review UI.

    The point of showing this is that a reviewer can skip a whole class of
    candidate at a glance ("same generic, different specific") instead of
    re-deriving why each one is wrong.
    """
    r = compare(a, b)
    label = {SAME: "Same place", DIFFERENT: "Different places",
             DERIVED: "Subsidiary parcel", UNRESOLVED: "Undetermined"}[r["verdict"]]
    return f"{label} ({r['score']:.0f}): {r['reason']}"
