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
from dataclasses import dataclass, field

# --- marker grammar -------------------------------------------------------

# A marker body always begins with the `stay:` namespace. We capture the body
# lazily up to the closing delimiter, then pull id/hash out of it. Capturing the
# whole body (rather than a fixed attribute order) tolerates reordered or extra
# attributes, which the spec's free-order attribute grammar allows (SPEC.md §4).
HTML_MARKER = re.compile(r"<!--\s*(?P<body>stay:.*?)\s*-->", re.DOTALL)
MDX_MARKER = re.compile(r"\{/\*\s*(?P<body>stay:.*?)\s*\*/\}", re.DOTALL)

# The id is positional: the first token right after the `stay:` namespace
# (`stay:8f24`). A first token that contains `=` (a bare k=v with no id) leaves
# the marker without an id, which is malformed.
ID_RE = re.compile(r"stay:\s*(?P<id>[A-Za-z0-9_-]+)(?=\s|$)")
# The boundary is whitespace, not `\b`: an attribute is a whitespace-separated token
# (SPEC.md §4), and a word boundary accepts a custom key merely ENDING in a reserved
# one, since a hyphen is not a word character. Under `\b`, `x-hash=sha256:ab` reads as
# the block hash and `restamp` rewrites its value, destroying a key §4 requires to be
# preserved verbatim. `rehash` was already refused; the hyphenated form was not.
HASH_RE = re.compile(r"(?<![^\s])hash\s*=\s*sha256:(?P<hash>[0-9a-fA-F]+)")
# SPEC.md §4: `subhash` is a reserved key, and an attribute is a whitespace-separated
# token, so the boundary that identifies it is whitespace (or the body's start), not a
# word boundary. `\b` would accept a custom key ENDING in the reserved one, because a
# hyphen is not a word character: `x-subhash=sha256:ab` would read as the reserved key
# and a tool would act on an attribute §4 tells it to preserve and ignore.
SUBHASH_RE = re.compile(r"(?<![^\s])subhash\s*=\s*sha256:(?P<hash>[0-9a-fA-F]+)")

LEVELS = {"error": 0, "warn": 1, "info": 2}


# --- data model -----------------------------------------------------------


@dataclass
class Marker:
    id: str | None
    hash: str | None
    raw: str
    syntax: str  # 'html' | 'mdx'
    line: int
    malformed: bool = False
    subhash: str | None = None


@dataclass
class Block:
    content: str  # marker(s) removed, normalized for display only
    markers: list = field(default_factory=list)
    line: int = 0  # 1-based start line of the content
    index: int = -1  # content-block index; -1 means an orphan marker chunk
    children: list = field(default_factory=list)


@dataclass
class ChildBlock:
    """Experimental direct list-item child of a container block.

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


def find_markers(text: str, line_offset: int = 0) -> list[Marker]:
    """All markstay markers in ``text``, ordered by position. ``line_offset`` is
    the 0-based line index where ``text`` begins in the full document."""
    raw = []
    for pat, syntax in ((HTML_MARKER, "html"), (MDX_MARKER, "mdx")):
        for m in pat.finditer(text):
            raw.append((m.start(), m.group(0), syntax, m.group("body")))
    raw.sort(key=lambda t: t[0])
    out = []
    for start, full, syntax, body in raw:
        line = line_offset + text[:start].count("\n") + 1
        # `.match` anchors the id to the FIRST token after `stay:` (SPEC.md §4:
        # the id is positional). `.search` would rescue a later `stay:ID` in a
        # body whose first token is a bare `k=v` (e.g. `stay:note=hello stay:ok`),
        # wrongly reading it as well-formed; the first token containing `=` is
        # malformed and the marker has no id.
        idm = ID_RE.match(body)
        hm = HASH_RE.search(body)
        shm = SUBHASH_RE.search(body)
        out.append(
            Marker(
                # Hex is stored canonically lowercase: SPEC.md §8 makes hash
                # comparison case-insensitive, so `hash=sha256:ABCD` must not read
                # as drift against a lowercase computed digest.
                id=idm.group("id") if idm else None,
                hash=hm.group("hash").lower() if hm else None,  # see ID_RE.match note
                subhash=shm.group("hash").lower() if shm else None,
                raw=full,
                syntax=syntax,
                line=line,
                malformed=idm is None,
            )
        )
    return out


def strip_markers(text: str) -> str:
    return MDX_MARKER.sub("", HTML_MARKER.sub("", text))


# One combined HTML|MDX pattern so a single ordered pass sees every marker in
# document order (rather than all HTML then all MDX). Group 1 is the HTML body,
# group 2 the MDX body; exactly one is set per match. Mirrors markers.js.
COMBINED_MARKER = re.compile(
    r"<!--\s*(?P<html>stay:.*?)\s*-->|\{/\*\s*(?P<mdx>stay:.*?)\s*\*/\}", re.DOTALL
)


def rewrite_markers(text: str, transform) -> str:
    """Rewrite markers in place, in document order, without disturbing
    surrounding text. ``transform(marker)`` receives a :class:`Marker` (``line``
    is 0 here, position is not tracked) and returns a replacement string, or
    ``None`` to leave the marker unchanged. The write helpers (restamp,
    repair_duplicates) build on this so marker edits reuse the one canonical
    grammar instead of re-deriving it."""

    def repl(m: "re.Match[str]") -> str:
        body = m.group("html")
        syntax = "html"
        if body is None:
            body, syntax = m.group("mdx"), "mdx"
        idm = ID_RE.match(body)
        hm = HASH_RE.search(body)
        shm = SUBHASH_RE.search(body)
        marker = Marker(
            id=idm.group("id") if idm else None,
            hash=hm.group("hash").lower() if hm else None,
            subhash=shm.group("hash").lower() if shm else None,
            raw=m.group(0),
            syntax=syntax,
            line=0,
            malformed=idm is None,
        )
        out = transform(marker)
        return m.group(0) if out is None else out

    return COMBINED_MARKER.sub(repl, text)


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


def child_body(text: str) -> str:
    """Return a list item's hash body.

    Stay markers are cut first, then the first line's indentation, list marker,
    and following syntactic gap are removed. The rest of the source slice stays
    byte-for-byte subject to normal §8 normalization, including nested content.
    """

    clean = strip_markers(text)
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
            kind = ("ordered", marker[-1]) if marker[0].isdigit() else ("bullet", marker)
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
        raw_lines = chunk.split("\n")
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
    if mode == "commonmark":
        chunks = segment_commonmark(text)
    elif mode == "blank-line":
        chunks = segment_blank_line(text)
    else:
        raise ValueError(
            f"unknown parse mode: {mode!r} (use 'blank-line' or 'commonmark')"
        )

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

    blocks: list[Block] = []
    cidx = 0
    child_idx = 0
    for start, chunk in chunks:
        markers = find_markers(chunk, line_offset=start - 1)
        content = strip_markers(chunk).strip(
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
                for ordinal, span in enumerate(child_spans.get(start, []), 1):
                    owned = [
                        mk
                        for mk in markers
                        if mk.subhash is not None
                        and span.start_line <= mk.line <= span.end_line
                        and mk.line not in span.excluded_lines
                    ]
                    child_marker_ids.update(id(mk) for mk in owned)
                    children.append(
                        ChildBlock(
                            content=child_body(span.text),
                            markers=owned,
                            line=span.start_line,
                            index=child_idx,
                            ordinal=ordinal,
                            parent_index=cidx,
                            marker_line=span.marker_line,
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


def lint_document(
    md: str, mode: str = "blank-line", child_blocks: bool = False
) -> tuple[list[Block], list[Finding]]:
    """Well-formedness and intra-document invariants for a single file."""
    blocks = parse_document(md, mode=mode, child_blocks=child_blocks)
    findings: list[Finding] = []
    seen: dict[str, int] = {}

    def check_marker(mk: Marker, body: str, orphan: bool = False, child: bool = False):
        if mk.malformed:
            findings.append(
                Finding(
                    "error",
                    "MALFORMED_MARKER",
                    f"marker has no parseable id: {mk.raw!r}",
                    line=mk.line,
                )
            )
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
        stored = mk.subhash if child else mk.hash
        if stored and body:
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
                if mk.subhash is not None and mk.id and not mk.malformed:
                    why = (
                        "nested items are not child blocks in v1.3"
                        if b.children
                        else "this segmenter emitted no child blocks for the block"
                    )
                    findings.append(
                        Finding(
                            "warn",
                            "CHILD_UNADDRESSED",
                            f"child id {mk.id} addresses no list item ({why})",
                            id=mk.id,
                            line=mk.line,
                        )
                    )
            has_parent = any(
                mk.id and not mk.malformed and mk.subhash is None for mk in b.markers
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
    return blocks, findings


def _id_index(blocks: list[Block]) -> dict[str, list[Block]]:
    out: dict[str, list[Block]] = {}
    for b in blocks:
        if b.index < 0:
            continue
        for mk in b.markers:
            if mk.id and not mk.malformed:
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
