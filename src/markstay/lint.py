"""markstay reference linter (parser-free core).

Checks that markstay markers in a Markdown document are well-formed and, given a
baseline version, that no ids were silently dropped, duplicated, or relocated by
an edit. This is the post-edit safety net the marker-survival eval showed is
mandatory: a regenerating agent that is not told about markstay strips nearly
every marker, so silent loss has to become a caught error rather than a quiet
break of every downstream reference.

Scope: the canonical HTML-comment marker

    <!-- stay:ID [hash=sha256:HEX] [k=v ...] -->

and the MDX profile

    {/* stay:ID [hash=sha256:HEX] [k=v ...] */}

(SPEC.md §3). Markers attach to the block immediately above them (after-block
placement, SPEC.md §5). A chunk that is *only* markers attaches to the previous
content block; a marker with no preceding block is an orphan.

Hash normalization is SPEC.md §8. ``normalize_body`` implements that rule, and
the linter always compares at the precision recorded in the marker, so it never
reports drift merely because a freshly computed hash is longer than a short
stored one.

What it does NOT do: detect block split/merge relocations where content only
partially moved. Exact-content marker swaps are caught (RELOCATED_ID); partial
relocation is the domain of the attachment resolver (quote/selector recovery),
not this deterministic linter.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, replace

# --- marker grammar -------------------------------------------------------

# Compatibility regexes for callers that imported them before v1.6. Discovery,
# stripping, and rewriting use the strict host-first scanner below.
HTML_MARKER = re.compile(r"<!--\s*(?P<body>stay:.*?)\s*-->", re.DOTALL)
MDX_MARKER = re.compile(r"\{/\*\s*(?P<body>stay:.*?)\s*\*/\}", re.DOTALL)
COMBINED_MARKER = re.compile(
    r"<!--\s*(?P<html>stay:.*?)\s*-->|\{/\*\s*(?P<mdx>stay:.*?)\s*\*/\}", re.DOTALL
)

_MARKER_OPEN_RE = re.compile(r"<!--|\{/\*")
_MARKER_ID_RE = re.compile(r"[A-Za-z0-9_-]+")
_MARKER_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
_MARKER_HASH_RE = re.compile(r"sha256:([0-9A-Fa-f]+)")

LEVELS = {"error": 0, "warn": 1, "info": 2}


# --- data model -----------------------------------------------------------


@dataclass(frozen=True)
class _MarkerAttributeSpan:
    """One scanner-approved attribute with offsets relative to ``Marker.raw``."""

    key: str
    value: str
    quoted: bool
    start: int
    end: int
    key_start: int
    value_start: int
    value_end: int


@dataclass
class Marker:
    id: str | None
    hash: str | None
    raw: str
    syntax: str  # 'html' | 'mdx'
    line: int
    malformed: bool = False
    subhash: str | None = None
    # §16 routes on exact parsed key presence, not on digest validity.
    has_subhash: bool = False
    # Private scanner-retained spans used by writer surgery. They deliberately
    # stay off the language-neutral marker projection.
    _id_span: tuple[int, int] | None = field(default=None, repr=False, compare=False)
    _attribute_spans: tuple[_MarkerAttributeSpan, ...] = field(
        default_factory=tuple, repr=False, compare=False
    )


@dataclass
class Block:
    content: str  # marker(s) removed, normalized for display only
    markers: list = field(default_factory=list)
    line: int = 0  # 1-based start line of the content
    index: int = -1  # content-block index; -1 means an orphan marker chunk
    children: list = field(default_factory=list)


@dataclass
class ChildBlock:
    """Experimental direct list-item or table-row child of a container block.

    ``content`` is the source slice with stay markers and the syntactic list
    prefix removed. ``ordinal`` is evidence only, never identity.
    """

    content: str
    markers: list = field(default_factory=list)
    line: int = 0
    index: int = -1
    ordinal: int = 0
    parent_index: int = -1
    marker_line: int = 0  # preferred source line for inline child stamping
    kind: str = "list"  # ``list`` (§5.5) or ``row`` (§5.6)


@dataclass
class Finding:
    level: str  # 'error' | 'warn' | 'info'
    code: str
    message: str
    id: str | None = None
    line: int | None = None

    def to_dict(self):
        return {k: v for k, v in self.__dict__.items() if v is not None}


# --- hashing (SPEC.md §8) -------------------------------------------------


def normalize_body(text: str) -> str:
    """Normalization for hashing (SPEC.md §8): LF endings, per-line trailing
    ASCII whitespace stripped, leading/trailing blank lines dropped. Markers are
    excluded upstream (they are stripped before a block's content is hashed).

    The trailing-whitespace set is ASCII (space, tab, form feed, vertical tab),
    not Python's Unicode ``str.rstrip()``, so a second implementation reproduces
    the hash exactly without an ICU table (SPEC.md §8; see SPEC_DECISIONS.md)."""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip(" \t\f\v") for ln in t.split("\n")]
    while lines and lines[0] == "":
        lines.pop(0)
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def body_hash(text: str, length: int | None = None) -> str:
    h = hashlib.sha256(normalize_body(text).encode("utf-8")).hexdigest()
    return h[:length] if length else h


# --- parsing --------------------------------------------------------------


@dataclass
class _MarkerRecord:
    start: int
    end: int
    marker: Marker


def _normalize_lf_with_raw_boundaries(text: str) -> tuple[str, list[int]]:
    """Normalize line endings and map normalized boundaries to raw offsets."""
    normalized: list[str] = []
    raw_boundaries = [0]
    pos = 0
    while pos < len(text):
        if text[pos] == "\r":
            pos += 2 if pos + 1 < len(text) and text[pos + 1] == "\n" else 1
            normalized.append("\n")
        else:
            normalized.append(text[pos])
            pos += 1
        raw_boundaries.append(pos)
    return "".join(normalized), raw_boundaries


def _parse_marker_body(
    body: str, syntax: str
) -> tuple[str, int, int, list[_MarkerAttributeSpan]] | None:
    """Parse one complete normalized-LF §4 marker body."""
    if not body.startswith("stay:"):
        return None
    pos = len("stay:")
    id_match = _MARKER_ID_RE.match(body, pos)
    if id_match is None:
        return None
    marker_id = id_match.group(0)
    id_start = id_match.start()
    pos = id_match.end()
    id_end = pos
    if pos < len(body) and body[pos] not in " \t":
        return None

    attributes: list[_MarkerAttributeSpan] = []
    while pos < len(body):
        separator_start = pos
        while pos < len(body) and body[pos] in " \t":
            pos += 1
        if pos == len(body):
            break
        if pos == separator_start:
            return None
        key_match = _MARKER_KEY_RE.match(body, pos)
        if key_match is None:
            return None
        key = key_match.group(0)
        key_start = key_match.start()
        pos = key_match.end()
        if pos == len(body) or body[pos] != "=":
            return None
        pos += 1
        if pos == len(body):
            return None

        quoted = body[pos] == '"'
        if quoted:
            pos += 1
            value_start = pos
            value: list[str] = []
            while pos < len(body) and body[pos] != '"':
                char = body[pos]
                if char == "\\":
                    if pos + 1 == len(body) or body[pos + 1] not in '\\"':
                        return None
                    value.extend((char, body[pos + 1]))
                    pos += 2
                    continue
                codepoint = ord(char)
                if char != "\n" and not (0x20 <= codepoint <= 0x7E):
                    return None
                value.append(char)
                pos += 1
            if pos == len(body):
                return None
            value_end = pos
            pos += 1
            attribute_value = "".join(value)
        else:
            value_start = pos
            while pos < len(body) and body[pos] not in " \t":
                codepoint = ord(body[pos])
                if body[pos] == '"' or not (0x21 <= codepoint <= 0x7E):
                    return None
                pos += 1
            if pos == value_start:
                return None
            value_end = pos
            attribute_value = body[value_start:pos]
        attributes.append(
            _MarkerAttributeSpan(
                key=key,
                value=attribute_value,
                quoted=quoted,
                start=separator_start,
                end=pos,
                key_start=key_start,
                value_start=value_start,
                value_end=value_end,
            )
        )
    return marker_id, id_start, id_end, attributes


def _malformed_key_first(body: str) -> bool:
    """Whether §4 still requires a no-positional-id diagnostic for this body."""
    if not body.startswith("stay:"):
        return False
    key = _MARKER_KEY_RE.match(body, len("stay:"))
    return key is not None and key.end() < len(body) and body[key.end()] == "="


def _scan_marker_records(text: str, line_offset: int = 0) -> list[_MarkerRecord]:
    """Discover complete §4 markers using each host comment's first closer.

    Every opener is considered independently, so a rejected opener cannot swallow
    a later valid marker. Recognition uses normalized LF; offsets and ``raw`` map
    back to the caller's exact source serialization.
    """
    normalized, raw_boundaries = _normalize_lf_with_raw_boundaries(text)
    records: list[_MarkerRecord] = []
    for opener in _MARKER_OPEN_RE.finditer(normalized):
        syntax = "html" if opener.group(0) == "<!--" else "mdx"
        pos = opener.end()
        while pos < len(normalized) and normalized[pos] in " \t":
            pos += 1
        if not normalized.startswith("stay:", pos):
            continue

        body_start = pos
        search_start = pos + len("stay:")
        if syntax == "html":
            closers = [
                (at, closer)
                for closer in ("-->", "--!>")
                if (at := normalized.find(closer, search_start)) >= 0
            ]
            if not closers:
                continue
            close_start, closer = min(closers, key=lambda item: item[0])
            host_valid = closer == "-->"
            normalized_end = close_start + len(closer)
        else:
            close_start = normalized.find("*/", search_start)
            if close_start < 0:
                continue
            host_valid = normalized.startswith("}", close_start + 2)
            normalized_end = close_start + (3 if host_valid else 2)

        body = normalized[body_start:close_start]
        parsed = _parse_marker_body(body, syntax) if host_valid else None
        malformed = parsed is None and _malformed_key_first(body)
        if parsed is None and not malformed:
            continue

        attributes = parsed[3] if parsed is not None else []
        block_hash = None
        child_hash = None
        for attribute in attributes:
            digest = (
                None if attribute.quoted else _MARKER_HASH_RE.fullmatch(attribute.value)
            )
            if attribute.key == "hash" and digest is not None and block_hash is None:
                block_hash = digest.group(1).lower()
            if attribute.key == "subhash" and digest is not None and child_hash is None:
                child_hash = digest.group(1).lower()

        raw_start = raw_boundaries[opener.start()]
        raw_end = raw_boundaries[normalized_end]

        def raw_relative(body_offset: int) -> int:
            return raw_boundaries[body_start + body_offset] - raw_start

        raw_attributes = tuple(
            replace(
                attribute,
                start=raw_relative(attribute.start),
                end=raw_relative(attribute.end),
                key_start=raw_relative(attribute.key_start),
                value_start=raw_relative(attribute.value_start),
                value_end=raw_relative(attribute.value_end),
            )
            for attribute in attributes
        )
        records.append(
            _MarkerRecord(
                raw_start,
                raw_end,
                Marker(
                    id=parsed[0] if parsed is not None else None,
                    hash=block_hash,
                    subhash=child_hash,
                    has_subhash=any(
                        attribute.key == "subhash" for attribute in attributes
                    ),
                    raw=text[raw_start:raw_end],
                    syntax=syntax,
                    line=line_offset + normalized.count("\n", 0, opener.start()) + 1,
                    malformed=malformed,
                    _id_span=(
                        (
                            raw_relative(parsed[1]),
                            raw_relative(parsed[2]),
                        )
                        if parsed is not None
                        else None
                    ),
                    _attribute_spans=raw_attributes,
                ),
            )
        )
    records.sort(key=lambda record: (record.start, record.end))
    return records


def find_markers(text: str, line_offset: int = 0) -> list[Marker]:
    """All markstay markers in ``text``, ordered by position. ``line_offset`` is
    the 0-based line index where ``text`` begins in the full document.

    A raw grammar-level primitive, and deliberately code-blind: it answers "is
    this a well-formed marker" for a string with no document around it, which is
    what the conformance corpus needs. SPEC.md §3.3 (a marker inside a fenced
    code block is content) is a *document*-level rule and cannot be applied here,
    because this function is handed chunks and a chunk that begins inside a fence
    carries no opener. Callers that segment a whole document filter the result
    against :func:`code_lines`."""
    return [record.marker for record in _scan_marker_records(text, line_offset)]


def strip_markers(text: str) -> str:
    """Remove every marker-shaped string. A raw grammar-level primitive: it is
    code-blind, so a caller that must honour SPEC.md §3.3 passes a document-level
    mask to :func:`strip_markers_outside_code` instead."""
    records = [
        record for record in _scan_marker_records(text) if not record.marker.malformed
    ]
    return _strip_record_ranges(text, records)


def _merged_record_ranges(
    records: list[_MarkerRecord], start: int = 0, end: int | None = None
) -> list[tuple[int, int]]:
    """Merged record ranges, optionally clipped to one source slice."""
    limit = (
        max((record.end for record in records), default=start) if end is None else end
    )
    spans: list[tuple[int, int]] = []
    for record in records:
        left = max(start, record.start)
        right = min(limit, record.end)
        if left >= right:
            continue
        if spans and left < spans[-1][1]:
            spans[-1] = (spans[-1][0], max(spans[-1][1], right))
        else:
            spans.append((left, right))
    return spans


def _strip_record_ranges(
    text: str,
    records: list[_MarkerRecord],
    *,
    source_start: int = 0,
) -> str:
    """Remove record spans from a source slice without reparsing that slice."""
    source_end = source_start + len(text)
    spans = _merged_record_ranges(records, source_start, source_end)
    if not spans:
        return text
    out: list[str] = []
    previous = source_start
    for start, end in spans:
        out.append(text[previous - source_start : start - source_start])
        previous = end
    out.append(text[previous - source_start :])
    return "".join(out)


# --- fenced code blocks (SPEC.md §3.3, v1.5) ------------------------------

# An opening fence may carry an info string; a *closing* fence may not, and a
# backtick fence's info string may not contain a backtick (CommonMark 4.5).
_FENCE_OPEN_RE = re.compile(r"^ {0,3}(?P<run>`{3,}|~{3,})(?P<info>.*)$")
_FENCE_CLOSE_RE = re.compile(r"^ {0,3}(?P<run>`{3,}|~{3,})[ \t]*$")


def fence_state(text: str) -> tuple[set[int], set[int]]:
    """Fence geometry for one document (SPEC.md §3.3, v1.5): the 1-based line
    numbers that lie inside a fenced code block, and the 1-based line numbers a
    fence is still open *after*.

    The second set is what the write path needs and it is not derivable from the
    first: a marker appended after line L lands on a new line inside the fence
    exactly when a fence is open at the end of L, and an unclosed fence runs to
    the end of the document, where there is no later line to test.

    Recognition is line-based and deliberately narrow, so both segmenters (§5)
    and every tool agree on it without a block parser:

    * the scan runs on LF-split lines, so a CRLF document and its LF twin give
      the same answer (§8);
    * an opening fence has at most three leading **spaces** and then three or
      more backticks or tildes. A tab is not one of the three: CommonMark
      expands it to the next four-column stop, which needs a column model this
      rule deliberately does not have. A backtick fence's info string may not
      contain a backtick;
    * it closes at the first later line with at most three leading spaces that
      is a run of the **same** character, **at least as long** as the opener,
      followed by nothing but spaces and tabs. A longer opener is what lets a
      fence contain a shorter one, and the whitespace set is named rather than
      left to "whitespace" because three implementations picking three sets is
      the way this rule fails quietly;
    * an unclosed fence runs to the end of the document.

    The fence lines themselves are inside the block, deliberately rather than as
    an edge case: a marker-shaped string can sit in an opening fence's info
    string, where before §3.3 it was read as a marker and bound to whatever block
    preceded it."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    inside: set[int] = set()
    open_after: set[int] = set()
    fence: str | None = None
    for num, line in enumerate(lines, 1):
        if fence is None:
            opener = _FENCE_OPEN_RE.match(line)
            if opener is None or (
                opener.group("run")[0] == "`" and "`" in opener.group("info")
            ):
                continue
            fence = opener.group("run")
            inside.add(num)
            open_after.add(num)
            continue
        inside.add(num)
        closer = _FENCE_CLOSE_RE.match(line)
        if (
            closer
            and closer.group("run")[0] == fence[0]
            and len(closer.group("run")) >= len(fence)
        ):
            fence = None
        else:
            open_after.add(num)
    return inside, open_after


def code_lines(text: str) -> set[int]:
    """The 1-based line numbers inside a fenced code block (SPEC.md §3.3). Text
    there is content: a marker-shaped string on one of these lines identifies no
    block, is hashed with the body (§8), and does not make its block stamped."""
    return fence_state(text)[0]


def marker_outside_code(marker: Marker, code: set[int] | None) -> bool:
    """Whether SPEC.md §3.3's fenced-code mask leaves ``marker`` active.

    §3.3 judges a marker by the line it *opens* on, which is the only line a
    reader can see it start on; the grammar is DOTALL, so one marker can span
    lines and close inside a fence it opened outside. Every path that separates
    active markers from marker-shaped content asks here, so the reading is one
    decision rather than a copy of the same expression at each seam."""
    return not code or marker.line not in code


def strip_markers_outside_code(text: str, code: set[int], line_offset: int = 0) -> str:
    """Remove markers from ``text``, leaving marker-shaped strings inside a fenced
    code block in place (SPEC.md §3.3: they are content, and §8 hashes them with
    the body). ``line_offset`` is the 0-based line index at which ``text`` begins
    in the document ``code`` was computed over.

    A marker is judged by the line it *opens* on, which is the only line a reader
    can see it start on; the grammar is DOTALL, so one can span lines."""
    records = [
        record
        for record in _scan_marker_records(text, line_offset)
        if not record.marker.malformed and marker_outside_code(record.marker, code)
    ]
    return _strip_record_ranges(text, records)


def rewrite_markers(text: str, transform, code: set[int] | None = None) -> str:
    """Rewrite markers in place, in document order, without disturbing
    surrounding text. ``transform(marker)`` receives a :class:`Marker` whose
    ``line`` is its 1-based document line, and returns a replacement string, or
    ``None`` to leave the marker unchanged. The write helpers (restamp,
    repair_duplicates) build on this so marker edits reuse the one canonical
    grammar instead of re-deriving it.

    ``code`` is the SPEC.md §3.3 mask for ``text`` (1-based line numbers inside a
    fenced code block). Matches opening on one of those lines are left byte-for-
    byte alone: they are an example, not a marker, and rewriting one is how a
    restamp overwrites a document's illustrative ``hash=`` values."""

    # Malformed diagnostic candidates and marker-shaped text opening in fenced
    # code are not active records. Filter them before overlap detection so they
    # cannot suppress an independently recognized valid marker (§4).
    records = [
        record
        for record in _scan_marker_records(text)
        if not record.marker.malformed and marker_outside_code(record.marker, code)
    ]
    overlapping = {
        index
        for index, record in enumerate(records)
        if any(
            other_index != index
            and record.start < other.end
            and other.start < record.end
            for other_index, other in enumerate(records)
        )
    }

    out: list[str] = []
    previous = 0
    for index, record in enumerate(records):
        # Independently recognized overlapping markers cannot both be rewritten
        # in one source slice. Leaving both raw is the only non-destructive result.
        if index in overlapping or record.start < previous:
            continue
        out.append(text[previous : record.start])
        marker = record.marker
        replacement = transform(marker)
        out.append(marker.raw if replacement is None else replacement)
        previous = record.end
    out.append(text[previous:])
    return "".join(out)


_FRONTMATTER_OPEN_RE = re.compile(r"^---[ \t]*$")
_FRONTMATTER_CLOSE_RE = re.compile(r"^(?:---|\.\.\.)[ \t]*$")
# One payload line that could only be YAML, never Markdown prose: a mapping key or
# a list item. Used to tell real frontmatter from a leading thematic break that
# happens to be followed by another one. A YAML comment (`# ...`) is deliberately
# NOT accepted: it is byte-identical to an ATX heading, so accepting it lets
# `---` / `# Heading` / `---` be read as frontmatter and silently destroys the
# heading. Comment-only frontmatter therefore is not skipped, which is the safe
# direction to be wrong in.
#
# `[^\x00-\x20\x7f]` is "not an ASCII control character and not a space", written
# out rather than as `\S`. Whitespace is ASCII-pinned here exactly as it is for
# hashing (§8) and matching (§9), because Python, ECMAScript and Rust each define
# Unicode whitespace differently: U+001C is whitespace to Python only, U+0085 to
# Python and Rust only, U+00A0 to Python and ECMAScript only, U+FEFF to ECMAScript
# only. A `\S` here therefore makes four conforming implementations skip different
# spans, which for a rule that DELETES a span from the document is the one kind of
# divergence that loses data.
_YAMLISH_LINE_RE = re.compile(
    r"^[ \t]*(?:-[ \t]+[^\x00-\x20\x7f]|[^\x00-\x20\x7f:#][^:]*:(?:[ \t]|$))"
)


def _blank_frontmatter(text: str) -> str:
    """Blank a leading YAML frontmatter block so neither segmenter sees it as
    content (SPEC.md §5).

    Frontmatter is document metadata, not a block: it carries no prose to identify,
    and hashing it makes a metadata edit (``status: draft`` -> ``status: done``)
    drift a content hash. It also has to be removed *before* segmentation rather
    than filtered after, because the two segmenters disagree about what it is: the
    baseline reads the whole fenced span as one block, while CommonMark reads the
    opening ``---`` as a thematic break and the closing one as a setext underline,
    turning the metadata into an H2. Leaving it in puts a document that is inside
    §5.4's agreement subset outside the set the two segmenters actually agree on.

    Recognition is deliberately conservative, because ``---`` is also a thematic
    break and a setext underline, so a loose rule silently eats real content. All
    four must hold:

    1. line 1 is exactly ``---``;
    2. a later line is exactly ``---`` or ``...`` (the closing fence). Without one
       the opener is an ordinary thematic break;
    3. the payload between the fences is non-empty and contains **no blank line**.
       This is what stops ``---`` / blank / ``Intro.`` / blank / ``---`` (two
       thematic breaks around a paragraph) from being read as frontmatter that
       swallows the paragraph;
    4. at least one payload line is unambiguously YAML (a ``key:`` or a ``- item``).
       This is what stops ``---`` / ``Title`` / ``---`` (a thematic break followed
       by a setext heading) from being read as frontmatter. A YAML comment does not
       count, because ``# x`` is also an ATX heading and accepting it would swallow
       ``---`` / ``# Heading`` / ``---``.

    Conditions 3 and 4 confine the ambiguity rather than removing it. Any blank-free
    payload that reads as YAML is *also* ordinary Markdown: `---` / `- Keep this` /
    `---` is a list between two thematic breaks, `---` / `title: v` / `---` is a
    setext heading under one. Both satisfy all four conditions and their content *is*
    excluded. Frontmatter wins, the same call every mainstream site generator makes.
    A document that fails any of the four conditions falls through to ordinary
    Markdown, where the worst case is that frontmatter is not skipped (a hash-drift
    warning) rather than content being silently discarded.

    Lines are replaced one-for-one with empty lines, so every line number the caller
    reports is unchanged."""
    lines = text.split("\n")
    if not lines or not _FRONTMATTER_OPEN_RE.match(lines[0]):
        return text
    for i in range(1, len(lines)):
        if _FRONTMATTER_CLOSE_RE.match(lines[i]):
            payload = lines[1:i]
            if not payload or any(ln.strip(" \t\f\v") == "" for ln in payload):
                return text
            if not any(_YAMLISH_LINE_RE.match(ln) for ln in payload):
                return text
            return "\n".join([""] * (i + 1) + lines[i + 1 :])
    return text


def segment_blank_line(text: str) -> list[tuple[int, str]]:
    """Baseline segmenter (SPEC.md §5): a block is a maximal run of non-blank
    lines bounded by blank lines or the document edges. Dependency-free. Returns
    (start_line_1based, chunk_text) spans in document order."""
    chunks: list[tuple[int, str]] = []
    cur, start = [], None
    for idx, ln in enumerate(text.split("\n")):
        if ln.strip(" \t\f\v") == "":  # blank = only ASCII whitespace (SPEC.md §5)
            if cur:
                chunks.append((start, "\n".join(cur)))
                cur, start = [], None
        else:
            if not cur:
                start = idx + 1
            cur.append(ln)
    if cur:
        chunks.append((start, "\n".join(cur)))
    return chunks


def segment_commonmark(text: str) -> list[tuple[int, str]]:
    """CommonMark-tree segmenter (SPEC.md §5.2, v1.1): a block is a node of the
    CommonMark block tree, so a loose list, a fence with internal blank lines, or
    a blockquote with internal blank lines is one span regardless of the blank
    lines inside it. A marker on its own line is its own (html_block) span, which
    the caller folds into the preceding content block exactly as it folds a
    blank-line marker-only chunk, so the attach layer above is identical.

    markdown-it-py is imported lazily so the default blank-line path keeps the
    core dependency-free; CommonMark mode is the optional ``commonmark`` extra."""
    from markdown_it import MarkdownIt  # lazy: optional extra, see SPEC.md §5.2

    lines = text.split("\n")
    chunks: list[tuple[int, str]] = []
    for t in MarkdownIt("commonmark").parse(text):
        # Top-level block tokens carry a source line `map`; container openers
        # (nesting=1) span the whole container, self-contained tokens (nesting=0)
        # span themselves. Skip close tokens (nesting<0) and nested children
        # (level>0) so each block contributes exactly one span.
        if t.level == 0 and t.nesting >= 0 and t.map is not None:
            s, e = t.map
            chunks.append((s + 1, "\n".join(lines[s:e])))
    return chunks


_LIST_PREFIX_RE = re.compile(
    r"^(?P<indent> {0,3})(?P<marker>(?:[*+-]|[0-9]{1,9}[.)]))(?P<gap>[ \t]+)(?=\S)"
)
_THEMATIC_RE = re.compile(
    r"^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$"
)
_UNSAFE_ITEM_BODY_RE = re.compile(
    r"^(?:#{1,6}(?:[ \t]|$)|>|```|~~~|(?:[*+-]|[0-9]{1,9}[.)])[ \t]+)"
)


@dataclass
class _ChildSpan:
    start_line: int
    end_line: int
    text: str
    marker_line: int
    excluded_lines: set[int] = field(default_factory=set)
    kind: str = "list"


def child_body(
    text: str,
    code: set[int] | None = None,
    line_offset: int = 0,
    kind: str = "list",
    markers_already_stripped: bool = False,
) -> str:
    """Return a list item's or table row's hash body.

    Stay markers are cut first, then the first line's indentation, list marker,
    and following syntactic gap are removed. The rest of the source slice stays
    byte-for-byte subject to normal §8 normalization, including nested content.

    ``code`` is the SPEC.md §3.3 mask for the document ``text`` was sliced from,
    and ``line_offset`` the 0-based line index at which the slice begins. Without
    them the cut is code-blind, which lets a fenced example inside a list item be
    removed from the child's body while the container holding the same fence keeps
    it: one document, two §8 answers. Only the CommonMark child profile can reach
    that shape, since the dependency-free profile refuses any item carrying a
    fence, but the parameter is threaded from both.
    """
    if kind == "row":
        cells = _row_cells(text)
        if cells is None:
            return ""
        return "|".join(
            cell.replace("\\", "\\\\").replace("|", "\\|") for cell in cells
        )
    if kind != "list":
        raise ValueError(f"unknown child kind: {kind!r}")

    clean = text
    if not markers_already_stripped:
        clean = (
            strip_markers(text)
            if not code
            else strip_markers_outside_code(text, code, line_offset)
        )
    lines = clean.split("\n")
    if lines:
        match = _LIST_PREFIX_RE.match(lines[0])
        if match:
            prefix = match.group(0)
            width = 0
            for char in prefix:
                width = width + 1 if char != "\t" else width + (4 - width % 4)
            lines[0] = lines[0][len(prefix) :]
            for i in range(1, len(lines)):
                col = 0
                cut = 0
                for cut, char in enumerate(lines[i], 1):
                    if char == " ":
                        col += 1
                    elif char == "\t":
                        col += 4 - col % 4
                    else:
                        cut = 0
                        break
                    if col >= width:
                        break
                if col == width:
                    lines[i] = lines[i][cut:]
    return "\n".join(lines).strip(" \t\n\r\f\v")


_DELIMITER_CELL_RE = re.compile(r"^:?-+:?$")


def _line_marker_spans(line: str) -> list[tuple[int, int]]:
    return [
        (record.start, record.end)
        for record in _scan_marker_records(line)
        if not record.marker.malformed
    ]


def _spans_overlap(spans: list[tuple[int, int]]) -> bool:
    return any(
        start < previous_end for (_, previous_end), (start, _) in zip(spans, spans[1:])
    )


def _remove_spans(text: str, spans: list[tuple[int, int]]) -> str:
    out: list[str] = []
    previous = 0
    for start, end in spans:
        out.append(text[previous:start])
        previous = end
    out.append(text[previous:])
    return "".join(out)


def _row_cells(line: str) -> list[str] | None:
    """Return §5.6 cells after fixing boundaries around opaque marker tokens."""
    leading_spaces = len(line) - len(line.lstrip(" "))
    if leading_spaces > 3:
        return None
    working = line[leading_spaces:].rstrip(" \t\f\v")
    if not working:
        return None
    spans = _line_marker_spans(working)
    if _spans_overlap(spans):
        return None

    delimiters: list[int] = []
    backslashes = 0
    pos = 0
    span_index = 0
    while pos < len(working):
        if span_index < len(spans) and pos == spans[span_index][0]:
            pos = spans[span_index][1]
            span_index += 1
            backslashes = 0
            continue
        char = working[pos]
        if char == "\\":
            backslashes += 1
            pos += 1
            continue
        if char == "|" and backslashes % 2 == 0:
            delimiters.append(pos)
        backslashes = 0
        pos += 1
    if len(delimiters) < 2 or delimiters[0] != 0 or delimiters[-1] != len(working) - 1:
        return None

    cells: list[str] = []
    for start, end in zip(delimiters, delimiters[1:]):
        cell_spans = [
            (
                max(marker_start, start + 1) - (start + 1),
                min(marker_end, end) - (start + 1),
            )
            for marker_start, marker_end in spans
            if start < marker_start and marker_end <= end
        ]
        cell = _remove_spans(working[start + 1 : end], cell_spans)
        cells.append(cell.strip(" \t\f\v"))
    return cells


def _marker_only_line(line: str) -> bool:
    spans = _line_marker_spans(line)
    return (
        bool(spans)
        and not _spans_overlap(spans)
        and _remove_spans(line, spans).strip(" \t\f\v") == ""
    )


def _row_refused_marker_lines(text: str, code: set[int]) -> set[int]:
    records = [
        record
        for record in _scan_marker_records(text)
        if not record.marker.malformed and marker_outside_code(record.marker, code)
    ]
    refused: set[int] = set()
    spans: list[tuple[int, int, int, int]] = []
    for record in records:
        first = text.count("\n", 0, record.start) + 1
        last = text.count("\n", 0, max(record.start, record.end - 1)) + 1
        spans.append((record.start, record.end, first, last))
        if first != last:
            refused.update(range(first, last + 1))
    for index, (start, end, first, last) in enumerate(spans):
        for other_start, other_end, other_first, other_last in spans[index + 1 :]:
            if other_start >= end:
                break
            if other_end > start:
                refused.update(range(first, last + 1))
                refused.update(range(other_first, other_last + 1))
    return refused


@dataclass
class _TableCandidate:
    header_line: int
    end_line: int
    rows: list[_ChildSpan]


def _table_candidates(
    text: str, code: set[int], provenance: set[int] | None = None
) -> list[_TableCandidate]:
    """Complete document-level §5.6 scan before selected-container filtering."""
    lines = text.split("\n")
    refused_marker_lines = _row_refused_marker_lines(text, code)
    candidates: list[_TableCandidate] = []
    i = 0
    while i + 1 < len(lines):
        header = (
            None
            if i + 1 in code or i + 1 in refused_marker_lines
            else _row_cells(lines[i])
        )
        delimiter = (
            None
            if i + 2 in code or i + 2 in refused_marker_lines
            else _row_cells(lines[i + 1])
        )
        delimiter_shaped = (
            delimiter is not None
            and bool(delimiter)
            and all(_DELIMITER_CELL_RE.fullmatch(cell) for cell in delimiter)
        )
        if provenance is not None and header is not None and delimiter_shaped:
            provenance.update((i + 1, i + 2))
        if (
            header is None
            or delimiter is None
            or bool(_line_marker_spans(lines[i + 1]))
            or "\f" in lines[i + 1]
            or "\v" in lines[i + 1]
            or len(header) != len(delimiter)
            or not delimiter_shaped
        ):
            i += 1
            continue

        rows: list[_ChildSpan] = []
        refused = False
        j = i + 2
        while j < len(lines):
            line_number = j + 1
            line = lines[j]
            if line_number in code or line_number in refused_marker_lines:
                if provenance is not None:
                    provenance.add(line_number)
                refused = True
                j += 1
                continue
            if line.strip(" \t\f\v") == "" or _marker_only_line(line):
                break
            if provenance is not None:
                provenance.add(line_number)
            cells = _row_cells(line)
            if cells is None:
                refused = True
            else:
                rows.append(
                    _ChildSpan(
                        line_number,
                        line_number,
                        line,
                        line_number,
                        kind="row",
                    )
                )
            j += 1
        if not refused:
            candidates.append(_TableCandidate(i + 1, max(i + 2, j), rows))
        if j >= len(lines):
            break
        i = j + 1
    return candidates


def _table_spans_by_container(
    text: str, chunks: list[tuple[int, str]], code: set[int]
) -> dict[int, list[_ChildSpan]]:
    """Map accepted candidates to §5 blocks, then enforce one per container."""
    candidates_by_start: dict[int, list[_TableCandidate]] = {}
    for candidate in _table_candidates(text, code):
        for start, chunk in chunks:
            end = start + len(chunk.split("\n")) - 1
            if start <= candidate.header_line and candidate.end_line <= end:
                candidates_by_start.setdefault(start, []).append(candidate)
                break
    return {
        start: candidates[0].rows
        for start, candidates in candidates_by_start.items()
        if len(candidates) == 1
    }


def _restricted_child_spans(chunk: str, start: int) -> list[_ChildSpan]:
    """Dependency-free, fail-closed child profile.

    Only flat, tight lists whose items contain one paragraph are accepted. A
    continuation must use the item's exact content indentation; lazy lines,
    tabs, nested blocks, fences, and other ambiguous constructs fail closed. A
    final run of marker-only lines is allowed for the parent stay.
    """

    lines = chunk.split("\n")
    items: list[tuple[int, int, str]] = []
    item_start: int | None = None
    item_lines: list[str] = []
    content_indent = 0
    saw_parent_marker = False
    signature: tuple[tuple[str, str], str] | None = None
    for off, raw in enumerate(lines):
        clean = strip_markers(raw).strip(" \t\r\f\v")
        markers = find_markers(raw, line_offset=start + off - 1)
        if clean == "" and markers:
            # A marker-only line indented to the open item's content column is
            # that item's, not the container's: §5.5 lets a child stay take a
            # line of its own rather than share the item's last one. Only the
            # container's own indentation ends the list.
            indent = len(raw) - len(raw.lstrip(" "))
            if item_start is not None and content_indent and indent >= content_indent:
                item_lines.append(raw)
                continue
            saw_parent_marker = True
            continue
        if saw_parent_marker or _THEMATIC_RE.match(strip_markers(raw)):
            return []
        m = _LIST_PREFIX_RE.match(strip_markers(raw))
        if m:
            prefix = m.group(0)
            body = strip_markers(raw)[len(prefix) :]
            if "\t" in prefix or _UNSAFE_ITEM_BODY_RE.match(body):
                return []
            if item_start is not None:
                items.append((item_start, off - 1, "\n".join(item_lines)))
            marker = m.group("marker")
            kind = (
                ("ordered", marker[-1]) if marker[0].isdigit() else ("bullet", marker)
            )
            # The marker's own indentation is part of the signature, not just the
            # kind: `_LIST_PREFIX_RE` allows up to three leading spaces, so an
            # indented `  - Nested` matches as happily as a top-level one and
            # would be emitted as a *sibling* of the item that contains it. That
            # is a child block SPEC.md §5.5 says does not exist (nested content
            # belongs to its ancestor's body), and it would also shift every
            # later ordinal, so the two segmenters would disagree about which
            # item a child stay addresses.
            current = (kind, m.group("indent"))
            if signature is None:
                signature = current
            elif signature != current:
                return []
            item_start, item_lines, content_indent = off, [raw], len(prefix)
            continue
        clean_raw = strip_markers(raw)
        indent = " " * content_indent
        if (
            item_start is None
            or not clean_raw.startswith(indent)
            or clean_raw[len(indent) :].startswith(" ")
            or _UNSAFE_ITEM_BODY_RE.match(clean_raw[len(indent) :])
        ):
            return []
        item_lines.append(raw)
    if not items:
        if item_start is None:
            return []
    if item_start is not None:
        items.append(
            (item_start, item_start + len(item_lines) - 1, "\n".join(item_lines))
        )
    return [
        _ChildSpan(start + first, start + last, raw, start + last)
        for first, last, raw in items
    ]


def _commonmark_child_spans(chunk: str, start: int) -> list[_ChildSpan]:
    from markdown_it import MarkdownIt

    tokens = MarkdownIt("commonmark").parse(chunk)
    roots = [
        t
        for t in tokens
        if t.level == 0
        and t.nesting == 1
        and t.map is not None
        and t.type in ("bullet_list_open", "ordered_list_open")
    ]
    if len(roots) != 1:
        return []
    raw_lines = chunk.split("\n")
    root = roots[0]
    spans: list[_ChildSpan] = []
    for t in tokens:
        if t.type != "list_item_open" or t.level != 1 or t.map is None:
            continue
        s, e = t.map
        # A nested list item's range overlaps its direct ancestor. Excluding
        # those lines prevents a depth-2 subhash from binding to the depth-1
        # child while nesting remains experimental.
        excluded: set[int] = set()
        for nested in tokens:
            if (
                nested.type == "list_item_open"
                and nested.level > 1
                and nested.map is not None
                and s <= nested.map[0] < nested.map[1] <= e
            ):
                excluded.update(range(start + nested.map[0], start + nested.map[1]))
        marker_line = 0
        paragraph_ends = [
            x.map[1]
            for x in tokens
            if (
                x.type == "paragraph_open"
                and x.level == 2
                and x.map is not None
                and s <= x.map[0] < x.map[1] <= e
            )
        ]
        if paragraph_ends:
            marker_line = start + paragraph_ends[-1] - 1
        spans.append(
            _ChildSpan(
                start + s,
                start + e - 1,
                "\n".join(raw_lines[s:e]),
                marker_line,
                excluded,
            )
        )
    # Be strict about the direct structural relationship: the root list must
    # own every emitted item and contain at least one.
    return spans if root.map and spans else []


def segment_child_items(chunk: str, start_line: int, mode: str) -> list[_ChildSpan]:
    """Experimental direct-list-item segmentation used by ``child_blocks``.

    CommonMark mode follows ``listItem`` source maps. Blank-line mode exposes a
    named restricted subset and emits no children when any boundary is unclear.
    """

    if mode == "commonmark":
        return _commonmark_child_spans(chunk, start_line)
    if mode == "blank-line":
        return _restricted_child_spans(chunk, start_line)
    raise ValueError(f"unknown parse mode: {mode!r} (use 'blank-line' or 'commonmark')")


def _line_starts(text: str) -> list[int]:
    starts = [0]
    starts.extend(match.end() for match in re.finditer("\n", text))
    return starts


def _document_marker_records(md: str, text: str) -> list[_MarkerRecord]:
    """Records over normalized, frontmatter-blanked ``text`` with raw source bytes.

    Segmentation is LF-normalized, but marker preservation is not. Pairing the
    normalized records with the same host-first scan over the original document
    lets blocks retain CRLF or CR inside ``Marker.raw`` without giving discovery a
    second grammar.
    """
    normalized = md.replace("\r\n", "\n").replace("\r", "\n")
    normalized_lines = normalized.split("\n")
    blanked_lines = text.split("\n")
    frontmatter_lines = {
        line
        for line, (source, blanked) in enumerate(
            zip(normalized_lines, blanked_lines), 1
        )
        if source != blanked
    }
    raw_records = [
        record
        for record in _scan_marker_records(md)
        if record.marker.line not in frontmatter_lines
    ]
    normalized_records = _scan_marker_records(text)

    def facts(record: _MarkerRecord) -> tuple:
        marker = record.marker
        return (
            marker.line,
            marker.syntax,
            marker.id,
            marker.hash,
            marker.subhash,
            marker.has_subhash,
            marker.malformed,
        )

    if [facts(record) for record in raw_records] != [
        facts(record) for record in normalized_records
    ]:
        # This should only be reachable if frontmatter blanking changes a marker
        # whose opener is outside the excluded span. Failing closed keeps parsing
        # useful while refusing to associate the wrong raw serialization.
        return normalized_records
    return [
        _MarkerRecord(
            normalized_record.start,
            normalized_record.end,
            replace(
                normalized_record.marker,
                raw=raw_record.marker.raw,
                _id_span=raw_record.marker._id_span,
                _attribute_spans=raw_record.marker._attribute_spans,
            ),
        )
        for normalized_record, raw_record in zip(normalized_records, raw_records)
    ]


def _record_line_bounds(text: str, record: _MarkerRecord) -> tuple[int, int]:
    """Inclusive source lines occupied by one complete marker record."""
    return (
        record.marker.line,
        text.count("\n", 0, max(record.start, record.end - 1)) + 1,
    )


def _record_belongs_to_child(
    text: str, record: _MarkerRecord, span: _ChildSpan
) -> bool:
    first, last = _record_line_bounds(text, record)
    return (
        span.start_line <= first
        and last <= span.end_line
        and not any(line in span.excluded_lines for line in range(first, last + 1))
    )


def parse_document(
    md: str, mode: str = "blank-line", child_blocks: bool = False
) -> list[Block]:
    """Parse into content blocks with their attached markers.

    ``mode='blank-line'`` (default, dependency-free) splits on blank lines
    (SPEC.md §5). ``mode='commonmark'`` (v1.1, needs markdown-it-py) splits on
    the CommonMark block tree so loose lists and blank-line-containing fences
    attach as one block (SPEC.md §5.2). The two agree on every document in
    SPEC.md §5.4's agreement subset. In both modes a
    leading YAML frontmatter block is metadata rather than content and is skipped
    before segmentation (see :func:`_blank_frontmatter`, without which frontmatter
    is a counterexample to that agreement), and a chunk that is only markers
    attaches to the previous content block."""
    text = _blank_frontmatter(md.replace("\r\n", "\n").replace("\r", "\n"))
    # SPEC.md §3.3: text inside a fenced code block is content. The rule is
    # computed once over the whole document and threaded, on the
    # :func:`_blank_frontmatter` precedent, because neither segmenter has a
    # concept of a fence and :func:`find_markers` is handed chunks. Blanking
    # preserves line numbers, so this mask indexes the caller's text too.
    code = code_lines(text)
    if mode == "commonmark":
        chunks = segment_commonmark(text)
    elif mode == "blank-line":
        chunks = segment_blank_line(text)
    else:
        raise ValueError(
            f"unknown parse mode: {mode!r} (use 'blank-line' or 'commonmark')"
        )

    document_records = _document_marker_records(md, text)
    active_records = [
        record for record in document_records if marker_outside_code(record.marker, code)
    ]
    removable_records = [
        record for record in active_records if not record.marker.malformed
    ]
    line_starts = _line_starts(text)

    child_spans: dict[int, list[_ChildSpan]] = {}
    if child_blocks:
        for start, chunk in chunks:
            child_spans[start] = segment_child_items(chunk, start, mode)
        if mode == "blank-line":
            # A loose list arrives as consecutive list-shaped chunks separated
            # only by blank lines. Neither chunk may emit children in the
            # dependency-free profile because CommonMark joins them into one
            # container. Fail closed for the whole run.
            run: list[int] = []
            for start, _ in chunks + [(-1, "")]:
                if start >= 0 and child_spans.get(start):
                    run.append(start)
                    continue
                if len(run) > 1:
                    for loose_start in run:
                        child_spans[loose_start] = []
                run = []
        table_spans = _table_spans_by_container(text, chunks, code)
        for start, spans in table_spans.items():
            # Row ownership wins when a table sits inside a direct list item.
            # The child kinds keep separate ordinals and sibling scopes.
            child_spans.setdefault(start, []).extend(spans)
        for spans in child_spans.values():
            spans.sort(
                key=lambda span: (span.start_line, 0 if span.kind == "row" else 1)
            )

    blocks: list[Block] = []
    cidx = 0
    child_idx = 0
    for start, chunk in chunks:
        chunk_start = line_starts[start - 1]
        chunk_end = chunk_start + len(chunk)
        chunk_records = [
            record
            for record in active_records
            if chunk_start <= record.start < chunk_end
        ]
        markers = [record.marker for record in chunk_records]
        record_by_marker = {id(record.marker): record for record in chunk_records}
        content = _strip_record_ranges(
            chunk, removable_records, source_start=chunk_start
        ).strip(
            " \t\n\r\f\v"
        )  # ASCII strip (SPEC.md §5/§8)
        if content == "":
            # marker-only chunk: attach to the previous content block if any
            if blocks and blocks[-1].index >= 0:
                blocks[-1].markers.extend(markers)
            else:
                blocks.append(Block(content="", markers=markers, line=start, index=-1))
        else:
            children: list[ChildBlock] = []
            child_marker_ids: set[int] = set()
            if child_blocks:
                ordinals: dict[str, int] = {}
                row_marker_ids = {
                    id(mk)
                    for span in child_spans.get(start, [])
                    if span.kind == "row"
                    for mk in markers
                    if mk.has_subhash
                    and _record_belongs_to_child(text, record_by_marker[id(mk)], span)
                }
                for span in child_spans.get(start, []):
                    ordinals[span.kind] = ordinals.get(span.kind, 0) + 1
                    owned = [
                        mk
                        for mk in markers
                        if mk.has_subhash
                        and id(mk) not in child_marker_ids
                        and (span.kind == "row" or id(mk) not in row_marker_ids)
                        and _record_belongs_to_child(
                            text, record_by_marker[id(mk)], span
                        )
                    ]
                    child_marker_ids.update(id(mk) for mk in owned)
                    span_text = span.text
                    if span.kind == "list":
                        # Full-document clipping prevents boundary-crossing
                        # markers leaving dangling halves in list child hashes.
                        # Rows keep the opaque marker tokens while fixing pipe
                        # boundaries, since a token breaks a backslash run.
                        span_text = _strip_record_ranges(
                            span_text,
                            removable_records,
                            source_start=line_starts[span.start_line - 1],
                        )
                    children.append(
                        ChildBlock(
                            content=child_body(
                                span_text,
                                code,
                                span.start_line - 1,
                                span.kind,
                                markers_already_stripped=span.kind == "list",
                            ),
                            markers=owned,
                            line=span.start_line,
                            index=child_idx,
                            ordinal=ordinals[span.kind],
                            parent_index=cidx,
                            marker_line=span.marker_line,
                            kind=span.kind,
                        )
                    )
                    child_idx += 1
            parent_markers = [mk for mk in markers if id(mk) not in child_marker_ids]
            blocks.append(
                Block(
                    content=content,
                    markers=parent_markers,
                    line=start,
                    index=cidx,
                    children=children,
                )
            )
            cidx += 1
    return blocks


# --- checks ---------------------------------------------------------------


def _mask_markers(text: str) -> str:
    """``text`` with every active marker span replaced by spaces, in place.

    SPEC.md §5.4 excludes marker spans from both sides of its comparison. Spaces
    rather than deletion, so the bytes around a marker stay where they were and a
    line that is nothing but marker text is recognisable by being blank here while
    it was not blank in the source.

    Either host form, and a marker carrying evidence counts: §5.4 asks which lines
    are content, and a marker is not content whatever it carries. §3.4's
    writer-side mask is a different question, "can these bytes capture the marker I
    am about to write", and is narrower for that reason: there a marker's own
    quoted evidence is reachable from outside it, so only a plain one is masked.
    §3.3 binds both: a marker-shaped string inside a fenced code block is content.
    """
    code = code_lines(_blank_frontmatter(text))
    out = list(text)
    for record in _scan_marker_records(text):
        if record.marker.malformed:
            # Diagnostic records have no marker identity; both segmenters keep
            # their source as content, so §5.4 must keep it too.
            continue
        if not marker_outside_code(record.marker, code):
            continue
        closer = "-->" if record.marker.syntax == "html" else "*/}"
        if not record.marker.raw.endswith(closer):
            # Not a closed host comment for a CommonMark renderer, whatever an
            # HTML parser does with `--!>`. It is text, and it stays text here.
            continue
        for at in range(record.start, min(record.end, len(out))):
            # Every byte but the line endings, which stay where they are: a
            # marker may span lines (§4 admits normalized LF inside a quoted
            # value), and masking its LF away would merge the lines around it and
            # change the very line accounting this exists to feed.
            if out[at] != "\n":
                out[at] = " "
    return "".join(out)


def _content_lines(text: str) -> tuple[set[int], set[int]]:
    """0-based (content lines, marker-only lines) for §5.4's comparison.

    A line whose marker spans are all of it is **transparent**: §5.4 counts it as
    neither content nor a boundary. Blanking it instead would manufacture a run
    boundary the baseline segmenter never sees, which certifies
    ``foo`` / marker / ``bar`` as agreeing when the two segmenters give one block
    and two; deleting it would join the runs each side of it.
    """
    masked = _mask_markers(text).split("\n")
    source = text.split("\n")
    content, transparent = set(), set()
    after_content = False
    for i, line in enumerate(source):
        # §5's blank line is ASCII-only, and so is this. A bare `.strip()` folds
        # U+00A0 in with the spaces, so a line holding one reads as blank here and
        # as content to both segmenters, and the comparison then certifies a
        # document whose blocks differ.
        if masked[i].strip(" \t\f\v"):
            content.add(i)
            after_content = True
        elif line.strip(" \t\f\v"):
            # A marker-only line is transparent only where it FOLLOWS content in
            # its run. One that begins a run is where the two profiles part
            # company: `A.` / blank / marker / `B.` gives the marker to `B.`
            # under §5.1, because the run starts at the marker line, and to `A.`
            # under §5.2, because an html_block folds into the block before it.
            # Counting such a line as content is what makes the comparison see
            # that, and it is why this is not simply "exclude marker spans".
            if after_content:
                transparent.add(i)
            else:
                content.add(i)
                after_content = True
        else:
            after_content = False
    return content, transparent


def _nonblank_runs(text: str) -> list[frozenset]:
    """Maximal runs of content lines, as sets of 0-based line numbers.

    Sets rather than spans, because a transparent line inside a run is a hole in
    it: the run continues across the line without covering it, and a node that
    covers the same lines has the same hole.
    """
    content, transparent = _content_lines(text)
    runs: list[frozenset] = []
    current: set[int] = set()
    for i in range(len(text.split("\n"))):
        if i in transparent:
            continue
        if i in content:
            current.add(i)
        elif current:
            runs.append(frozenset(current))
            current = set()
    if current:
        runs.append(frozenset(current))
    return runs


def _top_level_nodes(text: str) -> list[frozenset]:
    """Top-level CommonMark block nodes, as sets of the content lines they cover.

    Parsed from the source rather than from the masked copy, so the node structure
    is the one a CommonMark reader sees; the marker lines are then taken out of
    each node's line set, and a node made only of marker text (a marker-only line
    is its own `html_block`) drops out entirely.
    """
    from markdown_it import MarkdownIt  # lazy: optional extra, see SPEC.md §5.2

    content, _ = _content_lines(text)
    spans: list[tuple[int, int]] = []
    depth = 0
    # The same configuration §5.2's segmenter uses. The `commonmark` preset
    # already enables HTML blocks, which §5.4 needs and which passing `html`
    # again would only appear to add.
    for token in MarkdownIt("commonmark").parse(text):
        if token.nesting == 1:
            if depth == 0 and token.map:
                spans.append(tuple(token.map))
            depth += 1
        elif token.nesting == -1:
            depth -= 1
        elif depth == 0 and token.map:
            spans.append(tuple(token.map))
    nodes = []
    for start, end in spans:
        covered = frozenset(i for i in range(start, end) if i in content)
        if covered:
            nodes.append(covered)
    return nodes


def in_agreement_subset(md: str) -> bool | None:
    """SPEC.md §5.4: do the two segmenters draw the same block boundaries here?

    ``None`` when the CommonMark parser is not installed, which is the answer a
    dependency-free tool has to give: the condition is a statement about a parse,
    and §5.4 says checking it needs the parser. Frontmatter (§5.3) is excluded and
    marker spans are transparent on both sides.

    One-directional by design (§13): outside the subset a §5.1 write is more
    likely to change what a document shows, while inside it the measurement is a
    better bet rather than a guarantee (§3.4).
    """
    import importlib.util

    if importlib.util.find_spec("markdown_it") is None:
        return None
    text = _blank_frontmatter(md.replace("\r\n", "\n").replace("\r", "\n"))
    return sorted(_nonblank_runs(text)) == sorted(_top_level_nodes(text))


def lint_document(
    md: str, mode: str = "blank-line", child_blocks: bool = False
) -> tuple[list[Block], list[Finding]]:
    """Well-formedness and intra-document invariants for a single file."""
    blocks = parse_document(md, mode=mode, child_blocks=child_blocks)
    findings: list[Finding] = []
    seen: dict[str, int] = {}

    # Lexical diagnostics are one source-ordered pass before attachment. Child
    # ownership can reorder markers (row ownership beats an enclosing list item),
    # but it must not reorder MALFORMED or which duplicate occurrence is first.
    lexical_text = _blank_frontmatter(md.replace("\r\n", "\n").replace("\r", "\n"))
    lexical_code = code_lines(lexical_text)
    table_scan_lines: set[int] = set()
    _table_candidates(lexical_text, lexical_code, table_scan_lines)
    for record in _document_marker_records(md, lexical_text):
        mk = record.marker
        if not marker_outside_code(mk, lexical_code):
            continue
        if mk.malformed:
            findings.append(
                Finding(
                    "error",
                    "MALFORMED_MARKER",
                    f"marker has no parseable id: {mk.raw!r}",
                    line=mk.line,
                )
            )
            continue
        if mk.id in seen:
            findings.append(
                Finding(
                    "error",
                    "DUPLICATE_ID",
                    f"id {mk.id} appears more than once (first at line {seen[mk.id]})",
                    id=mk.id,
                    line=mk.line,
                )
            )
        else:
            seen[mk.id] = mk.line

    def check_marker(mk: Marker, body: str, orphan: bool = False, child: bool = False):
        if mk.malformed:
            return
        if orphan:
            findings.append(
                Finding(
                    "error",
                    "ORPHAN_MARKER",
                    f"marker {mk.id} has no preceding block to attach to",
                    id=mk.id,
                    line=mk.line,
                )
            )
            # An orphan has no body to attribute or hash. Exact `subhash`
            # presence excludes containing-block attribution (§16), but does
            # not waive the required orphan diagnostic (§5).
            return
        if mk.has_subhash and not child:
            return
        # §16: exact `subhash` key presence prevents every block-level
        # attribution even when the value is not a usable digest. Lexical
        # malformed/duplicate checks above remain unconditional.
        stored = mk.subhash if child else mk.hash
        if stored and (child or body):
            now = body_hash(body, len(stored))
            if now != stored:
                key = "subhash" if child else "hash"
                findings.append(
                    Finding(
                        "warn",
                        "HASH_DRIFT",
                        f"id {mk.id}: stored {key}=sha256:{stored} != current "
                        f"sha256:{now} (content edited since the hash was written)",
                        id=mk.id,
                        line=mk.line,
                    )
                )

    for b in blocks:
        orphan = b.index == -1
        for mk in b.markers:
            check_marker(mk, b.content, orphan=orphan)
        if child_blocks and b.index >= 0:
            # SPEC.md §5.5: a `subhash` marker that no child block owns addresses
            # nothing. It reaches here from an item nested inside another item,
            # which v1.3 does not address, and it is not the container's stay
            # either. Reporting it is the SHOULD in §5.5: silence is
            # indistinguishable from a marker that resolved.
            for mk in b.markers:
                if mk.has_subhash and mk.id and not mk.malformed:
                    if mk.line in table_scan_lines:
                        target = "table row"
                        why = "the complete §5.6 scan did not accept this as a body row"
                    else:
                        target = "list item"
                        why = (
                            "nested items are not child blocks in v1.3"
                            if any(child.kind == "list" for child in b.children)
                            else "this segmenter emitted no list child blocks for the block"
                        )
                    findings.append(
                        Finding(
                            "warn",
                            "CHILD_UNADDRESSED",
                            f"child id {mk.id} addresses no {target} ({why})",
                            id=mk.id,
                            line=mk.line,
                        )
                    )
            has_parent = any(
                mk.id and not mk.malformed and not mk.has_subhash for mk in b.markers
            )
            for child in b.children:
                for mk in child.markers:
                    check_marker(mk, child.content, child=True)
                    if not has_parent and mk.id and not mk.malformed:
                        findings.append(
                            Finding(
                                "warn",
                                "ORPHAN_CHILD",
                                f"child id {mk.id} has no stayed parent container",
                                id=mk.id,
                                line=mk.line,
                            )
                        )
    # SPEC.md §13: a linter that implements §5.2 already carries the parser this
    # question needs, so it SHOULD say when the two segmenters would draw
    # different block boundaries here. One-directional and therefore `info`: a
    # document outside the subset is one a §5.1 write is more likely to change
    # (159 of 1397 measured, against 0 of 1020 inside it), while being inside is
    # a better bet rather than a promise (§3.4). Silent when the parser is absent,
    # because then the tool is not one §13 is talking about.
    if in_agreement_subset(md) is False:
        findings.append(
            Finding(
                "info",
                "OUTSIDE_SUBSET",
                "the two segmenters of §5 draw different block boundaries here "
                "(§5.4); a §5.1 write is more likely to change what this document "
                "shows, and §5.4 lists the three shapes and the fix",
            )
        )
    return blocks, findings


def _id_index(blocks: list[Block]) -> dict[str, list[Block]]:
    out: dict[str, list[Block]] = {}
    for b in blocks:
        if b.index < 0:
            continue
        for mk in b.markers:
            if mk.id and not mk.malformed and not mk.has_subhash:
                out.setdefault(mk.id, []).append(b)
    return out


def _child_id_index(blocks: list[Block]) -> dict[str, list[ChildBlock]]:
    out: dict[str, list[ChildBlock]] = {}
    for b in blocks:
        if b.index < 0:
            continue
        for child in b.children:
            for mk in child.markers:
                if mk.id and not mk.malformed:
                    out.setdefault(mk.id, []).append(child)
    return out


# --- within-collection items (SPEC.md §5.1): the opt-in COLLECTION_SHRANK check --
# A stay binds a whole block, so a table is one stay and a list is one stay. A row
# or bullet dropped from inside that block leaves the stay in place and only drifts
# the block hash (a non-blocking warning). When lint_diff is asked to check
# collections, it counts the table data-rows + list bullets a kept block carries
# before vs after and reports a blocking COLLECTION_SHRANK when that count falls, so
# a silently pruned row/bullet is caught the way a dropped block already is.

_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_ROW_SEP_RE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")  # the |---|:--| divider row
_BULLET_RE = re.compile(r"^\s*[-*+]\s+\S")


def _item_count(content: str) -> int:
    """Count the table data-rows + list bullets in a block's content. The table
    header (the line directly above a ``|---|`` divider) and the divider itself are
    excluded; what remains is the within-collection data an edit can prune. Markers
    are already stripped from ``Block.content``, so a per-item marker line never
    counts."""
    lines = content.split("\n")
    n = len(lines)
    count = 0
    for i, ln in enumerate(lines):
        if _ROW_RE.match(ln):
            if _ROW_SEP_RE.match(ln):
                continue
            if i + 1 < n and _ROW_SEP_RE.match(lines[i + 1]):
                continue  # header row sits directly above the divider
            if ln.strip().strip("|").strip():
                count += 1
        elif _BULLET_RE.match(ln):
            count += 1
    return count


def lint_diff(
    before_md: str,
    after_md: str,
    mode: str = "blank-line",
    check_collections: bool = False,
    child_blocks: bool = False,
) -> list[Finding]:
    """Regeneration diff: what an edit did to the ids. Catches the AI-rewrite
    failure mode (dropped markers) plus duplication and exact-content relocation.

    With ``check_collections=True`` it additionally reports a blocking
    COLLECTION_SHRANK when a kept stay's block lost table rows or list bullets
    (SPEC.md §5.1: a stay binds the whole table/list, so a pruned row/bullet is
    otherwise only a non-blocking hash drift). Off by default, so existing callers
    and the conformance corpus are unaffected."""
    before_blocks = parse_document(before_md, mode=mode, child_blocks=child_blocks)
    after_blocks = parse_document(after_md, mode=mode, child_blocks=child_blocks)
    before = {
        mid: blks[0] for mid, blks in _id_index(before_blocks).items() if len(blks) == 1
    }
    after = _id_index(after_blocks)
    findings: list[Finding] = []

    for mid in before:
        if mid not in after:
            findings.append(
                Finding(
                    "error",
                    "DROPPED_ID",
                    f"id {mid} was in the baseline but is gone after the edit (silent loss)",
                    id=mid,
                )
            )

    for mid, blks in after.items():
        if len(blks) > 1:
            findings.append(
                Finding(
                    "error",
                    "DUPLICATED_ID",
                    f"id {mid} appears {len(blks)} times after the edit "
                    f"(copy without re-mint, or a regeneration collision)",
                    id=mid,
                )
            )

    for mid in after:
        if mid not in before:
            findings.append(
                Finding(
                    "info", "NEW_ID", f"id {mid} is new (not in the baseline)", id=mid
                )
            )

    # content-keyed before index, for exact-swap relocation detection
    before_by_content = {}
    for mid, b in before.items():
        if b.content:
            before_by_content.setdefault(body_hash(b.content), mid)

    for mid, blks in after.items():
        if mid not in before or len(blks) != 1:
            continue
        a, b0 = blks[0], before[mid]
        if not a.content or not b0.content:
            continue
        if body_hash(a.content) == body_hash(b0.content):
            continue  # unchanged
        moved_from = before_by_content.get(body_hash(a.content))
        if moved_from and moved_from != mid:
            findings.append(
                Finding(
                    "error",
                    "RELOCATED_ID",
                    f"id {mid} now sits on content that previously carried id "
                    f"{moved_from} (markers look swapped or relocated)",
                    id=mid,
                )
            )
        else:
            findings.append(
                Finding(
                    "warn",
                    "HASH_DRIFT",
                    f"id {mid}: content changed between versions (edited in place)",
                    id=mid,
                )
            )

    if check_collections:
        for mid, blks in after.items():
            if mid not in before or len(blks) != 1:
                continue
            n0 = _item_count(before[mid].content)
            n1 = _item_count(blks[0].content)
            if n0 > 0 and n1 < n0:
                findings.append(
                    Finding(
                        "error",
                        "COLLECTION_SHRANK",
                        f"id {mid}: collection shrank from {n0} to {n1} items "
                        f"(a table row or list bullet was dropped from inside the block)",
                        id=mid,
                    )
                )

    if child_blocks:
        # Imported lazily to avoid a module cycle: resolve owns the experimental
        # child recovery ladder and already depends on this parser.
        from .resolve import build_child_anchors, resolve_children

        before_children = _child_id_index(before_blocks)
        after_children = _child_id_index(after_blocks)
        anchors = build_child_anchors(before_md, mode=mode)
        resolved = resolve_children(anchors, after_md, mode=mode)
        for mid, children in before_children.items():
            if len(children) != 1:
                continue
            result = resolved.get(mid)
            if result is None or result.method == "detached":
                findings.append(
                    Finding(
                        "error",
                        "CHILD_DROPPED",
                        f"child id {mid} could not be recovered after the edit "
                        f"(the item was dropped or its remaining evidence is ambiguous)",
                        id=mid,
                    )
                )
        for mid, children in after_children.items():
            if len(children) > 1:
                findings.append(
                    Finding(
                        "error",
                        "DUPLICATED_ID",
                        f"child id {mid} appears {len(children)} times after the edit",
                        id=mid,
                    )
                )
            elif mid not in before_children:
                findings.append(
                    Finding(
                        "info",
                        "NEW_ID",
                        f"child id {mid} is new (not in the baseline)",
                        id=mid,
                    )
                )
    return findings


# --- reporting ------------------------------------------------------------


def sort_findings(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (LEVELS.get(f.level, 9), f.line or 0, f.code))


def has_errors(findings: list[Finding]) -> bool:
    return any(f.level == "error" for f in findings)
