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
HASH_RE = re.compile(r"\bhash\s*=\s*sha256:(?P<hash>[0-9a-fA-F]+)")

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


@dataclass
class Block:
    content: str          # marker(s) removed, normalized for display only
    markers: list = field(default_factory=list)
    line: int = 0         # 1-based start line of the content
    index: int = -1       # content-block index; -1 means an orphan marker chunk


@dataclass
class Finding:
    level: str            # 'error' | 'warn' | 'info'
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
        out.append(Marker(
            # Hex is stored canonically lowercase: SPEC.md §8 makes hash
            # comparison case-insensitive, so `hash=sha256:ABCD` must not read
            # as drift against a lowercase computed digest.
            id=idm.group("id") if idm else None,
            hash=hm.group("hash").lower() if hm else None,  # see ID_RE.match note
            raw=full, syntax=syntax, line=line, malformed=idm is None,
        ))
    return out


def strip_markers(text: str) -> str:
    return MDX_MARKER.sub("", HTML_MARKER.sub("", text))


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


def parse_document(md: str, mode: str = "blank-line") -> list[Block]:
    """Parse into content blocks with their attached markers.

    ``mode='blank-line'`` (default, dependency-free) splits on blank lines
    (SPEC.md §5). ``mode='commonmark'`` (v1.1, needs markdown-it-py) splits on
    the CommonMark block tree so loose lists and blank-line-containing fences
    attach as one block (SPEC.md §5.2). The two agree on every document that
    keeps lists tight and fences free of internal blank lines. In both modes a
    chunk that is only markers attaches to the previous content block."""
    text = md.replace("\r\n", "\n").replace("\r", "\n")
    if mode == "commonmark":
        chunks = segment_commonmark(text)
    elif mode == "blank-line":
        chunks = segment_blank_line(text)
    else:
        raise ValueError(f"unknown parse mode: {mode!r} (use 'blank-line' or 'commonmark')")

    blocks: list[Block] = []
    cidx = 0
    for start, chunk in chunks:
        markers = find_markers(chunk, line_offset=start - 1)
        content = strip_markers(chunk).strip(" \t\n\r\f\v")  # ASCII strip (SPEC.md §5/§8)
        if content == "":
            # marker-only chunk: attach to the previous content block if any
            if blocks and blocks[-1].index >= 0:
                blocks[-1].markers.extend(markers)
            else:
                blocks.append(Block(content="", markers=markers, line=start, index=-1))
        else:
            blocks.append(Block(content=content, markers=markers, line=start, index=cidx))
            cidx += 1
    return blocks


# --- checks ---------------------------------------------------------------

def lint_document(md: str, mode: str = "blank-line") -> tuple[list[Block], list[Finding]]:
    """Well-formedness and intra-document invariants for a single file."""
    blocks = parse_document(md, mode=mode)
    findings: list[Finding] = []
    seen: dict[str, int] = {}

    for b in blocks:
        orphan = b.index == -1
        for mk in b.markers:
            if mk.malformed:
                findings.append(Finding(
                    "error", "MALFORMED_MARKER",
                    f"marker has no parseable id: {mk.raw!r}", line=mk.line))
                continue
            if orphan:
                findings.append(Finding(
                    "error", "ORPHAN_MARKER",
                    f"marker {mk.id} has no preceding block to attach to",
                    id=mk.id, line=mk.line))
            if mk.id in seen:
                findings.append(Finding(
                    "error", "DUPLICATE_ID",
                    f"id {mk.id} appears more than once (first at line {seen[mk.id]})",
                    id=mk.id, line=mk.line))
            else:
                seen[mk.id] = mk.line
            if mk.hash and b.content:
                now = body_hash(b.content, len(mk.hash))
                if now != mk.hash:
                    findings.append(Finding(
                        "warn", "HASH_DRIFT",
                        f"id {mk.id}: stored sha256:{mk.hash} != current sha256:{now} "
                        f"(content edited since the hash was written)",
                        id=mk.id, line=mk.line))
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


def lint_diff(before_md: str, after_md: str, mode: str = "blank-line") -> list[Finding]:
    """Regeneration diff: what an edit did to the ids. Catches the AI-rewrite
    failure mode (dropped markers) plus duplication and exact-content relocation."""
    before = {mid: blks[0] for mid, blks in _id_index(parse_document(before_md, mode=mode)).items()
              if len(blks) == 1}
    after = _id_index(parse_document(after_md, mode=mode))
    findings: list[Finding] = []

    for mid in before:
        if mid not in after:
            findings.append(Finding(
                "error", "DROPPED_ID",
                f"id {mid} was in the baseline but is gone after the edit (silent loss)",
                id=mid))

    for mid, blks in after.items():
        if len(blks) > 1:
            findings.append(Finding(
                "error", "DUPLICATED_ID",
                f"id {mid} appears {len(blks)} times after the edit "
                f"(copy without re-mint, or a regeneration collision)",
                id=mid))

    for mid in after:
        if mid not in before:
            findings.append(Finding(
                "info", "NEW_ID", f"id {mid} is new (not in the baseline)", id=mid))

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
            findings.append(Finding(
                "error", "RELOCATED_ID",
                f"id {mid} now sits on content that previously carried id "
                f"{moved_from} (markers look swapped or relocated)", id=mid))
        else:
            findings.append(Finding(
                "warn", "HASH_DRIFT",
                f"id {mid}: content changed between versions (edited in place)",
                id=mid))
    return findings


# --- reporting ------------------------------------------------------------

def sort_findings(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (LEVELS.get(f.level, 9), f.line or 0, f.code))


def has_errors(findings: list[Finding]) -> bool:
    return any(f.level == "error" for f in findings)
