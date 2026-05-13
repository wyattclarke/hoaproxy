"""HOA name quality utilities — shared across discovery, bank, and ingest.

Canonical implementations of:
  - ``is_dirty(name)`` — regex-based dirty-name detection (18 patterns)
  - ``derive_clean_slug(name, source_url)`` — four deterministic recovery
    strategies drawn from ``state_scrapers/ga/scripts/ga_slug_cleanup.py``

These functions have no dependencies beyond ``re`` and stdlib so they can
be imported anywhere without pulling in network or DB dependencies.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Dirty-name detection  (ported verbatim from clean_dirty_hoa_names.py)
# ---------------------------------------------------------------------------

_BAD_PREFIX = re.compile(
    r"^(?:by-?laws?|declarations?|articles?|covenants?|deed|amendment|supplement|"
    r"plat|this |consideration|common|accordance|city of|county of|members of|"
    r"voting|the property|is the |a homeowners|page |section |exhibit|schedule|"
    r"such as|in addition|unless |any |all |or other|or by|a typical|recorded |"
    r"submitted |squarespace|attachment |appendix |of for |of to |amended and )",
    re.I,
)

# Phrases that almost always indicate a doc-title fragment leaked into the
# HOA name — even when they appear mid-string (caught the Lake Laceola case).
_DOC_FRAGMENT_RE = re.compile(
    r"\b(?:exhibit\s+[A-Za-z](?:\b|-\d)|"
    r"supplemental\s+dec(?:laration)?|amended\s+and\s+restated|"
    r"architectural\s+design\s+guidelines?|"
    r"declaration\s+of(?:\s+covenants)?|"
    r"by-?laws\s+of|articles\s+of\s+incorporation\s+of|"
    r"protective\s+covenants?|wetland(?:-|\s)mitigation)\b",
    re.I,
)

# Trailing "<stop word> HOA" — names like "Bridgeberry Amenity and HOA" where
# OCR truncated mid-phrase and "HOA" got stuck on the end.
_TAIL_TRUNCATION_RE = re.compile(
    r"\b(?:and|or|of|to|the|for|with|on|in|by|both)\s+HOA$", re.I
)

# "<County> County OF.<Name>" or "<County> County of <Name>" — county leaked
# in as a prefix (e.g. "Gwinnett County OF. FAITH HOLLOW Homeowners …").
_COUNTY_PREFIX_RE = re.compile(
    r"^[A-Z][a-z]+\s+County\s+(?:of|OF\.?)\s+", re.I
)

# Doubled-name pattern: "<X> POA <X-CAPS> PROPERTY OWNERS ASSOCIATION".
_DOUBLED_NAME_RE = re.compile(
    r"\b(POA|HOA)\s+[A-Z][A-Z &]{3,}\s+(?:PROPERTY|HOMEOWNERS|OWNERS)\s+ASSOCIATION\b"
)

# Project / phase / unit-numbering codes that leak in as the front of the name.
# Catches "TE1-12 Townhouses…", "TE-1-12 Townhouses…", "Phase II Foo HOA",
# "Block A Foo Condominium Association", "Building 4 Foo …".
_PROJECT_CODE_PREFIX_RE = re.compile(
    r"^(?:"
    r"[A-Z]{2,5}-?\d+(?:-\d+)+|"          # TE1-12, TE-1-12, AB-3-7
    r"[A-Z]{2,5}\d+|"                      # TE5, AB12 (single-segment code)
    r"phase\s+(?:[ivx]+|\d+)|"             # Phase II, Phase 3
    r"block\s+[a-z\d]+|"                   # Block A, Block 4
    r"building\s+\d+|"                     # Building 7
    r"unit\s+\d+|"                         # Unit 12
    r"lot\s+\d+|"                          # Lot 14
    r"parcel\s+[a-z\d]+"                   # Parcel A
    r")\b",
    re.I,
)

# HOA suffixes used to peel off the legal-suffix and inspect the community-name
# stem. Matches at end-of-name only.
_HOA_SUFFIX_TAIL_RE = re.compile(
    r"\s*,?\s*(?:"
    r"(?:home|land|property|unit|condominium|condo)\s*owners?\s*(?:'\s*)?\s*"
    r"association(?:,?\s+inc\.?)?|"
    r"homes\s+association|"
    r"home\s+owners\s+association|"
    r"community\s+association(?:,?\s+inc\.?)?|"
    r"condominium\s+association(?:,?\s+inc\.?)?|"
    r"condominium\s+trust|"
    r"townhome\s+association|"
    r"townhouse\s+association|"
    r"hoa|poa|coa"
    r")\.?\s*$",
    re.I,
)

# Roman numerals or numeric/phase qualifiers that follow a single-token stem
# without adding a real community identifier ("Willows IV", "Slopeside II",
# "Building 3", "Phase 2"). These are stripped before checking generic-stem.
_TRAILING_QUALIFIER_RE = re.compile(
    r"\s+(?:"
    r"[ivx]{1,5}|"                          # roman numeral
    r"\d+|"                                  # bare number
    r"phase\s+(?:[ivx]+|\d+)|"
    r"building\s+\d+|"
    r"section\s+(?:[ivx]+|\d+)|"
    r"unit\s+\d+"
    r")$",
    re.I,
)

# Generic geographic / topographic stems that, on their own, do not identify a
# community. Real HOAs almost always combine one of these with a place-name
# specifier ("Sunset Cove", "Hilltop Acres at Foo"). When the entire stem
# before the HOA suffix is just one of these tokens we treat it as a name
# extracted from a doc-fragment, not a real association name.
_GENERIC_GEOGRAPHIC_STEMS = frozenset(
    {
        "sunrise", "sunset", "mountainside", "slopeside", "hillside",
        "hilltop", "lakeside", "riverside", "lakeview", "riverview",
        "mountain", "lake", "river", "creek", "pond", "brook",
        "intervale", "willows", "willow", "oaks", "oak", "pines", "pine",
        "maples", "maple", "birches", "birch", "elms", "elm",
        "meadow", "meadows", "valley", "valleys", "ridge", "ridges",
        "summit", "summits", "peak", "peaks", "hill", "hills",
        "wood", "woods", "woodland", "woodlands", "forest", "forests",
        "glen", "glens", "shore", "shores", "cove", "coves",
        "harbor", "harbors", "harbour", "point", "pointe",
        "garden", "gardens", "terrace", "terraces", "manor", "manors",
        "court", "courts", "place", "places", "plaza", "square",
        "downs", "field", "fields", "commons", "village", "villages",
        "highland", "highlands", "plantation", "plantations",
        "grove", "groves", "park", "parks", "estate", "estates",
        "acres", "trace", "knoll", "knolls", "heights",
        "crossing", "crossings", "landing", "landings",
        "village", "villages", "town", "towne", "townhouse", "townhouses",
        # 1- to 4-letter cardinals/ordinals that occasionally leak in
        "north", "south", "east", "west", "central",
        "old", "new", "upper", "lower",
    }
)


def is_dirty(name: str) -> tuple[bool, str | None]:
    """Return ``(True, reason_code)`` when the name looks like an OCR fragment
    or document-title artifact, or ``(False, None)`` for a clean name.

    Reason codes are stable strings — downstream tooling (ledger entries,
    the post-hoc cleanup pass) depends on them.
    """
    n = name or ""
    if " - " in n and len(n) > 50:
        return True, "long_dashed_phrase"
    if n[:1].islower():
        return True, "starts_lowercase"
    if re.match(r"^\d+\s*[-)]\s*", n):
        return True, "numeric_prefix"
    # Year prefix like "2018 Exhibit A …" or "1985 Amended Bylaws of …"
    if re.match(r"^(?:19|20)\d{2}\s+[A-Za-z]", n):
        return True, "year_prefix"
    # Long digit run prefix like "5021942267194390towne park pooler"
    if re.match(r"^\d{6,}", n):
        return True, "longdigit_prefix"
    # Street-address prefix like "6318 Suwanee Dam Rd HOA"
    if re.match(
        r"^\d+\s+[A-Z][a-z]+(?:\s+[A-Z][a-zA-Z]+){0,4}\s+"
        r"(?:Rd|Road|St|Street|Ave|Avenue|Dr|Drive|Lane|Ln|Way|Cir|"
        r"Ct|Court|Blvd|Boulevard|Place|Pl|Pkwy|Parkway|Trail|Tr|Hwy|Highway)\b",
        n,
        re.I,
    ):
        return True, "street_address_prefix"
    if re.match(r"^[A-Z][A-Z &\-]{3,}\s+", n) and len(n) > 40:
        return True, "shouting_prefix"
    if len(n) <= 4 and not re.search(r"hoa|poa", n, re.I):
        return True, "too_short"
    if _BAD_PREFIX.match(n):
        return True, "stopword_prefix"
    if _COUNTY_PREFIX_RE.match(n):
        return True, "county_prefix"
    if _DOC_FRAGMENT_RE.search(n):
        return True, "doc_fragment_anywhere"
    if _TAIL_TRUNCATION_RE.search(n):
        return True, "tail_truncation"
    if _DOUBLED_NAME_RE.search(n):
        return True, "doubled_name"
    # Garbled hyphenated acronym like "GL-LB-BAR HOA"
    if re.search(r"\b[A-Z]{2,}-[A-Z]{2,}-[A-Z]{2,}\b", n):
        return True, "garbled_acronym"
    # Project / phase / unit-numbering code as prefix ("TE1-12 Townhouses…",
    # "Phase II Foo HOA"). These come from filename stems, not real names.
    if _PROJECT_CODE_PREFIX_RE.match(n):
        return True, "project_code_prefix"
    # Single-token generic stem before the HOA suffix. "Sunrise Homeowners
    # Association", "Mountainside Condominium Association", "Willows IV
    # Condominium Association" are all doc-fragment artifacts: a real
    # community name would add a place specifier ("Sunset Cove", "Sunrise at
    # Mendon"). We strip the legal suffix and any trailing roman/phase/number
    # qualifier, then flag if the remaining stem is a single common geographic
    # token.
    suffix_match = _HOA_SUFFIX_TAIL_RE.search(n)
    if suffix_match:
        stem = n[: suffix_match.start()].strip(" \t,.-")
        # Strip trailing roman / phase / number qualifier ("Slopeside II",
        # "Willows 2", "Foo Phase III").
        stripped_qualifier = _TRAILING_QUALIFIER_RE.sub("", stem).strip()
        if stripped_qualifier:
            stem_tokens = [t for t in re.split(r"\s+", stripped_qualifier) if t]
            if len(stem_tokens) == 1:
                only = stem_tokens[0].lower().rstrip(".,;:")
                if only in _GENERIC_GEOGRAPHIC_STEMS:
                    return True, "generic_single_stem"
    if len(n) > 70:
        return True, "very_long"
    if re.search(r"\bbook \d|page \d|paragraph", n, re.I):
        return True, "citation_in_name"
    if re.search(r"\bcc&?rs?\b", n, re.I) and len(n) > 30:
        return True, "ccr_in_name_long"
    return False, None


# ---------------------------------------------------------------------------
# Slug-level helpers  (ported from ga_slug_cleanup.py)
# ---------------------------------------------------------------------------

# Tokens that signal legalese-extracted slugs.  Keep as a frozen set — never
# trim past them in the strip_leading_stopwords pass.
_STOP_TOKENS = frozenset(
    {
        "a", "an", "the", "and", "or", "of", "for", "to", "in", "with", "by",
        "is", "that", "this", "which", "if", "as", "be", "are", "was", "were",
        "hereinafter", "whereas", "wherein", "whereof", "thereof", "thereto",
        "amount", "restated", "amended", "articles", "article", "declaration",
        "declarations", "bylaws", "covenants", "certain", "first", "called",
        "made", "between", "among", "shall", "having", "hereby", "incorporation",
        "section", "subsection", "paragraph", "exhibit", "page", "pages",
        "all", "any", "each", "every", "no", "not", "such", "said",
        "you", "we", "us", "our", "your", "their", "his", "her", "its",
        "mr", "mrs", "ms", "mrhoa",
        "georgia", "ga", "tn", "nc", "north", "south", "east", "west",
        "nonprofit", "corporation", "company", "association", "associations",
        "agree", "agreed", "ated", "ation", "assiter", "subject", "date",
        "name", "names", "address", "addresses", "laws", "law",
    }
)

_MIN_CLEAN_SLUG_LEN = 3
_MIN_CLEAN_SLUG_TOKENS = 1
_LONG_SLUG_THRESHOLD = 60
_RESULT_MAX_TOKENS = 5

_LEADING_JUNK_PREFIXES = sorted(
    {
        "a", "an", "the", "and", "or", "of", "for", "to", "in", "with", "by",
        "is", "that", "this", "which", "as",
        "hereinafter", "whereas", "wherein", "witnesseth",
        "amount", "restated", "amended", "articles", "article",
        "declaration", "declarations", "covenants", "certain",
        "between", "having", "hereby", "incorporation",
        "section", "exhibit", "page", "agree", "agreed",
        "ated", "ation", "assiter",
        "subject",
    },
    key=len,
    reverse=True,
)

_JUNK_SLUG_PATTERNS = [
    re.compile(r"^[a-z]$"),
    re.compile(r"^[a-z]{1,3}$"),
    re.compile(r"^(" + "|".join(_LEADING_JUNK_PREFIXES) + r")-"),
    re.compile(
        r"-(hereinafter|whereas|wherein|witnesseth|thereto|thereof|"
        r"by-laws|board-of|secretary-of|committee-of|directors|"
        r"as-of-date|nonprofit-corporation|with-georgia|of-georgia|"
        r"this-second|that-parc)-"
    ),
    re.compile(r"\b\d+-\d+-\d+\b"),
    re.compile(r"\b([a-z]{4,})\b.*-\1$"),
    re.compile(r"^(article-)?[ivx]{1,5}$"),
]

_DOC_SECTION_TOKENS = frozenset(
    {
        "mandatory", "age", "directors", "officers", "members", "membership",
        "meetings", "meeting", "voting", "votes", "vote", "dues", "fines",
        "assessments", "assessment", "liens", "lien", "easements", "easement",
        "definitions", "definition", "amendments", "arbitration", "permanent",
        "lots", "lot", "properties", "property", "subject", "purpose",
        "purposes", "notice", "notices", "rules", "regulations", "owner",
        "owners", "homeowners", "association", "associations", "corporation",
        "incorporation", "covenants", "declaration", "declarations", "bylaws",
        "articles", "amendment", "rights", "powers", "duties", "indemnity",
        "indemnification", "fees", "expenses", "books", "records",
        "consent", "approval", "approvals", "second", "first", "third",
        "preliminary", "general", "special", "annual", "regular",
        "recording", "recorded",
    }
)

_NOISE_SUBSTRINGS = (
    "covenant", "bylaws", "articleof", "articlesof", "amendment",
    "declaration", "incorporation", "subdivision",
)

_FILENAME_NOISE_RE = re.compile(
    r"\b(ccrs?|cc&rs?|covenants?|bylaws?|by-laws?|articles?|amendment|amendments?|"
    r"amended|restated|declaration|declarations|rules|regulations?|hoa|homeowners?|"
    r"association|incorporation|condominium|condo|original|final|copy|recorded|signed|"
    r"executed|exhibit|schedule|attachment|appendix|of|the|for|and|to|by)\b",
    re.IGNORECASE,
)

_SINGLE_TOKEN_REJECT = frozenset(
    {"incorporation", "declaration", "covenants", "bylaws", "amendment",
     "restated", "amended", "articles", "association", "homeowners",
     "subdivision", "condominium", "rules", "regulations"}
)

_AFTER_MARKER_RES = [
    re.compile(r"-(?:committee|secretary|board|directors)-of-(.+)$"),
    re.compile(r"-(?:for|of)-([a-z][a-z0-9\-]+)$"),
]

# Common doc-title / OCR-noise prefixes that prepend the real name.
_PREFIX_NOISE_RE = re.compile(
    r"^("
    r"(?:19|20)\d{2}\s+(?:exhibit\s+[a-z]\s+)?(?:supplemental\s+)?dec(?:laration)?\s+|"
    r"(?:19|20)\d{2}\s+exhibit\s+[a-z]\s+|"
    r"(?:19|20)\d{2}\s+(?:amended|restated|amended\s+and\s+restated)\s+|"
    r"and\s+restated(?:\s*-\s*|\s+)|"
    r"amended\s+and\s+restated\s+|"
    r"squarespace\s*\.?\s*of\.?\s*|"
    r"squarespace\s*[-.]\s*|"
    r"architectural\s+design\s+guidelines?\s+(?:for\s+)?|"
    r"design\s+guidelines?\s+(?:for\s+)?|"
    r"declaration\s+of(?:\s+covenants(?:,?\s+conditions(?:,?\s+and\s+restrictions)?)?\s+)?(?:for\s+|of\s+)?|"
    r"by-?laws\s+of\s+(?:the\s+)?|"
    r"articles\s+of\s+incorporation\s+of\s+(?:the\s+)?|"
    r"protective\s+covenants?\s+(?:for\s+|of\s+)?|"
    r"supplemental\s+declaration\s+(?:for\s+|of\s+)?|"
    r"\d{6,}|"
    r"[A-Z][a-z]+\s+county\s+(?:of|OF\.?)\s+"
    r")",
    re.I,
)

_HOA_SUFFIX_RE = re.compile(
    r"\b("
    r"homeowners(?:'?\s|\s+)?association(?:,?\s+inc\.?)?|"
    r"homes\s+association|home\s+owners\s+association|"
    r"property\s+owners(?:'?\s+|\s+)association(?:,?\s+inc\.?)?|"
    r"owners\s+association(?:,?\s+inc\.?)?|"
    r"community\s+association|"
    r"condominium\s+association(?:,?\s+inc\.?)?|"
    r"condominium\s+owners\s+association|"
    r"townhome\s+association|"
    r"hoa|poa"
    r")\b\.?\s*$",
    re.I,
)


# ---------------------------------------------------------------------------
# Internal slug helpers
# ---------------------------------------------------------------------------

def _slugify_simple(name: str) -> str:
    """Minimal slug without importing bank.slugify (avoids circular deps)."""
    from hoaware.bank import slugify  # late import — bank has no dep on us
    return slugify(name)


def _is_junk_slug(slug: str) -> tuple[bool, str]:
    if not slug:
        return True, "empty"
    if len(slug) > _LONG_SLUG_THRESHOLD:
        return True, f"long({len(slug)})"
    for pat in _JUNK_SLUG_PATTERNS:
        if pat.search(slug):
            return True, f"pattern:{pat.pattern[:40]}"
    return False, ""


def _is_clean_result(slug: str) -> bool:
    if not slug:
        return False
    if _is_junk_slug(slug)[0]:
        return False
    tokens = slug.split("-")
    if len(tokens) > _RESULT_MAX_TOKENS:
        return False
    if any(t in _STOP_TOKENS for t in tokens):
        return False
    if all(t in _DOC_SECTION_TOKENS or t.isdigit() for t in tokens):
        return False
    if len(tokens) == 1:
        only = tokens[0]
        if len(only) < 5 or only in _DOC_SECTION_TOKENS:
            return False
    for tok in tokens:
        for noise in _NOISE_SUBSTRINGS:
            if noise in tok and tok != noise:
                return False
    if tokens[-1] == "county":
        return False
    return True


def _slug_is_too_junky_to_clean(slug: str) -> bool:
    tokens = slug.split("-")
    stop_count = sum(1 for t in tokens if t in _STOP_TOKENS)
    return stop_count >= 4


# ---------------------------------------------------------------------------
# Public sub-strategies (also exported for tests)
# ---------------------------------------------------------------------------

def strip_leading_stopwords(slug: str) -> str | None:
    """Drop leading stop-token chunks until a real name remains.

    ``"and-restated-pebble-creek-farm"`` -> ``"pebble-creek-farm"``
    """
    tokens = slug.split("-")
    while tokens and tokens[0] in _STOP_TOKENS:
        tokens.pop(0)
    if not tokens:
        return None
    cleaned = "-".join(tokens)
    if len(cleaned) < _MIN_CLEAN_SLUG_LEN:
        return None
    if len(tokens) < _MIN_CLEAN_SLUG_TOKENS:
        return None
    is_junk, _ = _is_junk_slug(cleaned)
    if is_junk:
        return None
    return cleaned


def extract_after_marker(slug: str) -> str | None:
    """Pull the trailing name fragment from slugs like
    ``"architectural-review-committee-of-reserve-at-reid-plantation"``.
    """
    if not slug:
        return None
    for pat in _AFTER_MARKER_RES:
        matches = list(pat.finditer(slug))
        if not matches:
            continue
        tail = matches[-1].group(1)
        tokens = tail.split("-")
        while tokens and tokens[-1] in _STOP_TOKENS:
            tokens.pop()
        while tokens and tokens[0] in _STOP_TOKENS:
            tokens.pop(0)
        candidate = "-".join(tokens)
        if candidate and _is_clean_result(candidate):
            return candidate
    return None


def dedupe_tail(slug: str) -> str | None:
    """Find a duplicated trailing chunk and return it if clean.

    ``"1-wellington-walk-walk"`` -> ``"wellington-walk"``
    """
    if not slug:
        return None
    tokens = slug.split("-")
    for chunk_len in range(min(3, len(tokens) // 2), 0, -1):
        tail = tokens[-chunk_len:]
        prev = tokens[-2 * chunk_len: -chunk_len]
        if tail == prev and tail:
            candidate = "-".join(tail)
            if _is_clean_result(candidate):
                return candidate
    return None


def name_from_source_url(url: str | None) -> str | None:
    """Extract HOA name from a PDF URL's filename stem.

    ``"https://x.com/Walton-Reserve-Declaration-Recorded.pdf"`` -> ``"walton-reserve"``
    """
    if not url:
        return None
    last = url.rsplit("/", 1)[-1].split("?", 1)[0]
    if "." in last:
        last = last.rsplit(".", 1)[0]
    last = re.sub(r"[_\-]+", " ", last)
    last = re.sub(r"(\d)([A-Za-z])", r"\1 \2", last)
    last = re.sub(r"([A-Za-z])(\d)", r"\1 \2", last)
    last = re.sub(r"([a-z])([A-Z])", r"\1 \2", last)
    last = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", last)
    last = _FILENAME_NOISE_RE.sub(" ", last)
    last = re.sub(r"\b\d+\b", " ", last)
    last = re.sub(r"\s+", " ", last).strip()
    if not last:
        return None
    tokens = last.lower().split()
    if not any(len(t) >= 4 and t.isalpha() for t in tokens):
        return None
    try:
        candidate = _slugify_simple(last)
    except Exception:
        return None
    if not candidate or len(candidate) < _MIN_CLEAN_SLUG_LEN:
        return None
    if candidate in _SINGLE_TOKEN_REJECT:
        return None
    is_junk, _ = _is_junk_slug(candidate)
    if is_junk:
        return None
    candidate = strip_leading_stopwords(candidate) or candidate
    return candidate


# ---------------------------------------------------------------------------
# Smart title-casing  (fixes the most common "shouting_prefix" dirty pattern)
# ---------------------------------------------------------------------------

# Words to keep lowercase in the middle of a title (not first/last).
_SMALL_WORDS = frozenset({
    "a", "an", "the", "and", "or", "for", "to", "in", "on", "at", "by",
    "of", "with", "from", "as", "but", "nor", "yet", "so", "if", "via",
})

# Words to keep fully uppercase regardless of position. Roman numerals
# I, II, III … X are kept as-is when they appear as standalone tokens.
_KEEP_UPPERCASE = frozenset({
    "HOA", "POA", "COA", "LLC", "L.L.C.", "PLC", "PC", "P.C.",
    "USA", "US", "U.S.", "DC", "II", "III", "IV", "V", "VI",
    "VII", "VIII", "IX", "X", "XI", "XII",
})

# A trailing "THE" gets moved to leading "The " (English convention).
# Two separate patterns: comma form (consumes the comma) and period form
# (uses a lookbehind so the period before "Inc." is preserved when we
# slice the string).
_TRAILING_THE_AFTER_COMMA_RE = re.compile(r",\s+the\s*\.?\s*$", re.I)
_TRAILING_THE_AFTER_PERIOD_RE = re.compile(r"(?<=\.)\s+the\s*\.?\s*$", re.I)


def _titlecase_token(token: str, *, anchor: bool) -> str:
    """Title-case one whitespace-delimited token while preserving trailing
    punctuation and recognising keep-uppercase abbreviations.

    ``anchor`` is True for the first or last token of the title — anchor
    tokens are always capitalised even if they are in ``_SMALL_WORDS``.
    """
    if not token:
        return token
    # Pull off trailing punctuation cluster (.,;:!?")  — but keep apostrophes
    # inside the word ("OWNERS'", "O'BRIEN"). Accept both straight (') and
    # curly (’ U+2019 / ‘ U+2018) apostrophes; scraped registry data often
    # contains the curly form.
    m = re.match(r"^([A-Za-z'`‘’]+)([.,;:!?\"\)\]]*)$", token)
    if not m:
        # Mixed alphanumeric ("NO.24", "06831") — leave as-is.
        return token
    core, trailing = m.group(1), m.group(2)
    upper = core.upper()
    if upper in _KEEP_UPPERCASE:
        return upper + trailing
    lower = core.lower()
    if lower in _SMALL_WORDS and not anchor:
        return lower + trailing
    # Title-case: first char upper, remainder lower, apostrophes preserved.
    out = core[:1].upper() + core[1:].lower()
    return out + trailing


def smart_titlecase(name: str) -> str:
    """Title-case an all-caps (or mixed-case) HOA name with HOA-aware rules.

    Examples:
      "FOUNTAINWOOD CONDOMINIUM ASSOCIATION, INC."
        -> "Fountainwood Condominium Association, Inc."
      "WILSON POINT PROPERTY OWNERS' ASSOCIATION, INCORPORATED, THE"
        -> "The Wilson Point Property Owners' Association, Incorporated"
      "PROPERTY OWNERS ASSOCIATION OF LAKE HAYWARD"
        -> "Property Owners Association of Lake Hayward"
      "HILL-AN-DALE VILLAGE CONDOMINIUM ASSOCIATION, INC."
        -> "Hill-An-Dale Village Condominium Association, Inc."
      "GLEN OAKS CONDOMINIUM NO. 24, INC."
        -> "Glen Oaks Condominium No. 24, Inc."

    Idempotent: re-running on already-clean output produces the same string.
    """
    if not name:
        return name or ""
    s = name.strip()
    if not s:
        return s

    # Insert a space after commas / periods that are missing one — common
    # in scraped registry data ("ASSOCIATION,INC." or "ASSN.INC.") — so that
    # tokenisation by whitespace correctly isolates each word.
    s = re.sub(r"([,;])(\S)", r"\1 \2", s)

    # Move trailing "THE" to a leading "The ".
    leading_the = ""
    m = _TRAILING_THE_AFTER_COMMA_RE.search(s)
    if m:
        # Comma form: consume the comma along with " THE".
        s = s[: m.start()].rstrip(" ,")
        leading_the = "The "
    else:
        m = _TRAILING_THE_AFTER_PERIOD_RE.search(s)
        if m:
            # Period form: lookbehind keeps the period in s[:m.start()].
            s = s[: m.start()].rstrip()
            leading_the = "The "

    tokens = s.split()
    n = len(tokens)
    out: list[str] = []
    for i, w in enumerate(tokens):
        anchor = (i == 0 and not leading_the) or i == n - 1
        if "-" in w and not w.startswith("-") and not w.endswith("-"):
            # Title-case each hyphen segment independently. All segments are
            # treated as anchors so "HILL-AN-DALE" → "Hill-An-Dale", not
            # "Hill-an-Dale".
            parts = w.split("-")
            casted = [_titlecase_token(p, anchor=True) for p in parts]
            out.append("-".join(casted))
        else:
            out.append(_titlecase_token(w, anchor=anchor))
    return leading_the + " ".join(out)


def _try_titlecase_shouting(name: str) -> str | None:
    """Strategy: if a name only fails ``is_dirty`` because of all-caps
    shouting, smart-title-case it and accept iff the result is clean.
    """
    if not name:
        return None
    titled = smart_titlecase(name)
    if titled and titled != name and not is_dirty(titled)[0]:
        return titled
    return None


# ---------------------------------------------------------------------------
# Name-level prefix strip  (operates on raw names, not slugs)
# ---------------------------------------------------------------------------

def _try_strip_name_prefix(name: str) -> str | None:
    """Deterministically peel doc-title noise off a raw HOA name string.

    Returns the cleaned name only when a recognisable HOA-shaped suffix is
    present AND something junk-like was actually stripped from the front.
    Returns None if no safe strip is possible.
    """
    n = (name or "").strip()
    if not _HOA_SUFFIX_RE.search(n):
        return None
    cleaned = n
    changed = False
    for _ in range(4):
        m = _PREFIX_NOISE_RE.match(cleaned)
        if not m:
            break
        cleaned = cleaned[m.end():].lstrip(" -.,;:")
        changed = True
    if not changed:
        return None
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -.,;:")
    if len(cleaned) < 4:
        return None
    return cleaned


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def derive_clean_slug(name: str, source_url: str | None = None) -> str | None:
    """Try four deterministic strategies to recover a clean HOA name from a
    dirty ``name``.  Returns the first non-empty, non-dirty result as a
    human-readable name string (Title Cased), or ``None`` if all strategies fail.

    Strategies (in order):
      1. Name-level prefix strip (highest confidence when an HOA suffix is present)
      2. ``strip_leading_stopwords`` on the slugified form -> title-cased
      3. ``extract_after_marker`` on the slugified form -> title-cased
      4. ``dedupe_tail`` on the slugified form -> title-cased
      5. ``name_from_source_url`` when a URL is provided -> title-cased

    The slug-based strategies return lowercase hyphenated slugs internally;
    they are title-cased before the ``is_dirty`` guard so that the
    ``starts_lowercase`` check does not reject valid results.
    """
    def _slug_to_name(slug: str) -> str:
        """Convert a hyphenated slug to a title-cased name."""
        return " ".join(part.capitalize() for part in slug.split("-"))

    # 0a. Smart title-case for all-caps "shouting_prefix" names. This is the
    # most common dirty class on SoS-derived registries (RI, CT, NH, ME) where
    # entity names are exported in ALL CAPS. Smart title-casing recovers the
    # human-readable form deterministically without an LLM call.
    titled = _try_titlecase_shouting(name)
    if titled:
        return titled

    # 0b. Name-level strip (highest confidence for names with HOA suffix).
    stripped_name = _try_strip_name_prefix(name)
    if stripped_name and not is_dirty(stripped_name)[0]:
        return stripped_name

    try:
        slug = _slugify_simple(name)
    except Exception:
        slug = ""

    if not slug:
        return None

    # 1. Strip leading stopwords from slug.
    cleaned_slug = strip_leading_stopwords(slug)
    if cleaned_slug and cleaned_slug != slug and _is_clean_result(cleaned_slug):
        candidate = _slug_to_name(cleaned_slug)
        if not is_dirty(candidate)[0]:
            return candidate

    # Guard: if the slug is saturated with stop tokens, mid-slug extraction
    # is unreliable.
    if _slug_is_too_junky_to_clean(slug):
        url_slug = name_from_source_url(source_url)
        if url_slug and _is_clean_result(url_slug):
            candidate = _slug_to_name(url_slug)
            if not is_dirty(candidate)[0]:
                return candidate
        return None

    # 2. Extract after marker (e.g. "committee-of-X" -> X).
    cleaned_slug = extract_after_marker(slug)
    if cleaned_slug and _is_clean_result(cleaned_slug):
        candidate = _slug_to_name(cleaned_slug)
        if not is_dirty(candidate)[0]:
            return candidate

    # 3. Dedupe repeated tail chunk.
    cleaned_slug = dedupe_tail(slug)
    if cleaned_slug and _is_clean_result(cleaned_slug):
        candidate = _slug_to_name(cleaned_slug)
        if not is_dirty(candidate)[0]:
            return candidate

    # 4. Source URL filename.
    url_slug = name_from_source_url(source_url)
    if url_slug and _is_clean_result(url_slug):
        candidate = _slug_to_name(url_slug)
        if not is_dirty(candidate)[0]:
            return candidate

    return None
