"""The write path (SPEC.md §3 / §4 / §6 / §7 / §8): mint ids, serialize markers,
stamp an unmarked corpus, refresh drifted hashes, and repair duplicate ids.

String-level by default like the rest of the core: blank-line mode stays
parser-free, while opt-in CommonMark mode reuses the optional markdown-it-py
segmenter (§5.2). Port of the JavaScript reference (`impl/js/src/stamp.js`); the
default path is gated by the shared conformance corpus.

Every operation is idempotent in the obvious sense: stamping an already-stamped
document is a no-op, restamping an undrifted document is a no-op, and repairing a
document with no duplicates is a no-op.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .id import ID_CHARSET, mint_id
from .lint import (
    Marker,
    body_hash,
    find_markers,
    parse_document,
    rewrite_markers,
    segment_blank_line,
    segment_commonmark,
    strip_markers,
)

# Default truncation for a freshly written hash (§8 permits any prefix). 12 hex =
# 48 bits, enough to make an accidental same-prefix collision within one document
# negligible, while staying lighter than the full 64-char digest.
DEFAULT_HASH_LENGTH = 12

_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")  # §4 attribute key grammar
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
_PRINTABLE_RE = re.compile(r"^[\x21-\x7e]+$")  # printable ASCII, no space (bare-value)
# §4 qchar: a value may only contain printable ASCII (0x20-0x7E); `"` and `\` are
# escaped in the quoted form. Newlines, tabs, other control chars, and non-ASCII
# are not representable and must be rejected rather than emitted into a marker.
_VALUE_RE = re.compile(r"^[\x20-\x7e]*$")

# Closing delimiter per syntax: a written value must never contain it, or it would
# terminate the marker early.
_TERMINATOR = {"html": "-->", "mdx": "*/}"}

# Content-strip: ASCII whitespace at both ends, mirroring JS asciiTrim and the
# Python reference's parse_document content strip (SPEC.md §5/§8).
_ASCII_TRIM = " \t\n\r\f\v"


@dataclass
class StampResult:
    text: str
    minted: list[dict] = field(default_factory=list)  # [{"id":.., "line":..}]


@dataclass
class RestampResult:
    text: str
    refreshed: list[str] = field(default_factory=list)


@dataclass
class RepairResult:
    text: str
    renamed: list[dict] = field(default_factory=list)  # [{"from":.., "to":..}]


def format_attr_value(value) -> str:
    """Serialize one attribute value (SPEC.md §4): a bare token when it has no
    whitespace or double quote and is all printable ASCII, otherwise a
    double-quoted string with ``\\`` and ``"`` escaped.

    Raises ``ValueError`` if the value contains a character outside the §4 qchar
    set (printable ASCII 0x20-0x7E) , a newline or other control character has no
    representation and would corrupt the marker."""
    s = str(value)
    if not _VALUE_RE.match(s):
        raise ValueError(
            f"format_attr_value: value {s!r} contains a character outside the §4 "
            f"qchar set (printable ASCII 0x20-0x7E)"
        )
    if s and _PRINTABLE_RE.match(s) and '"' not in s:
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def format_marker(id: str, hash=None, attrs=None, syntax: str = "html") -> str:
    """Serialize a marker (SPEC.md §3 / §4).

    ``id``      required, matches the §6 charset
    ``hash``    optional hex; emitted as ``hash=sha256:<hex>`` (folded lowercase)
    ``attrs``   optional extra attributes, a dict or iterable of (key, value)
                pairs; keys must satisfy the §4 key grammar (callers namespace
                extensions with ``x-`` themselves)
    ``syntax``  ``"html"`` (default) or ``"mdx"``

    Raises ``ValueError`` if the id/hash/keys are malformed, or if a serialized
    value would contain the syntax's closing delimiter (which would break the
    marker).
    """
    if not id or not ID_CHARSET.match(id):
        raise ValueError(
            f"format_marker: invalid id {id!r} (must match [A-Za-z0-9_-]+)"
        )
    if syntax not in ("html", "mdx"):
        raise ValueError(f"format_marker: unknown syntax {syntax!r}")
    body = f"stay:{id}"
    if hash is not None and hash is not False:
        hex_ = str(hash)
        if not _HEX_RE.match(hex_):
            raise ValueError(f"format_marker: hash must be hex, got {hex_!r}")
        body += f" hash=sha256:{hex_.lower()}"
    pairs: Iterable = attrs.items() if isinstance(attrs, dict) else (attrs or [])
    for k, v in pairs:
        if not _KEY_RE.match(k):
            raise ValueError(f"format_marker: invalid attribute key {k!r}")
        body += f" {k}={format_attr_value(v)}"
    if _TERMINATOR[syntax] in body:
        raise ValueError(
            f"format_marker: a value contains the {syntax} terminator "
            f"{_TERMINATOR[syntax]!r}, which would break the marker"
        )
    return f"{{/* {body} */}}" if syntax == "mdx" else f"<!-- {body} -->"


def _unique_minter(used: set, new_id: Callable[[], str]) -> Callable[[], str]:
    """A minting function that never returns an id already present in ``used``."""

    def mint() -> str:
        while True:
            i = new_id()
            if i not in used:
                used.add(i)
                return i

    return mint


def _default_minter(new_id, length, alphabet, random):
    if new_id is not None:
        return new_id
    kwargs = {}
    if length is not None:
        kwargs["length"] = length
    if alphabet is not None:
        kwargs["alphabet"] = alphabet
    if random is not None:
        kwargs["random"] = random
    return lambda: mint_id(**kwargs)


def _segments_for_mode(text: str, mode: str) -> list[tuple[int, str]]:
    if mode == "commonmark":
        return segment_commonmark(text)
    if mode == "blank-line":
        return segment_blank_line(text)
    raise ValueError(f"unknown parse mode: {mode!r} (use 'blank-line' or 'commonmark')")


def stamp(
    md: str,
    syntax: str = "html",
    hash: bool = True,
    hash_length: int = DEFAULT_HASH_LENGTH,
    new_id: Callable[[], str] | None = None,
    length: int | None = None,
    alphabet: str | None = None,
    random: Callable[[int], bytes] | None = None,
    mode: str = "blank-line",
) -> StampResult:
    """Stamp every unmarked content block (SPEC.md §5/§6): for each block with no
    well-formed id, mint one and append its marker on a new line directly after
    the block (the §3.1 trailing form, no blank line, so it binds to that block).
    Blocks that already carry a well-formed id are left untouched.

    ``mode`` selects the block segmenter: ``"blank-line"`` is the dependency-free
    default; ``"commonmark"`` uses the CommonMark block tree so fences, lists, and
    blockquotes with internal blank lines are stamped as one block.

    ``new_id`` overrides the id factory; otherwise ``length``/``alphabet``/
    ``random`` are forwarded to :func:`mint_id`. Returns a :class:`StampResult`
    with ``text`` (LF-normalized) and ``minted`` ``[{"id", "line"}]``.
    """
    norm = md.replace("\r\n", "\n").replace("\r", "\n")
    lines = norm.split("\n")

    # Existing ids across the whole document, so a minted id can't collide.
    used = {mk.id for mk in find_markers(norm) if mk.id and not mk.malformed}
    next_id = _unique_minter(used, _default_minter(new_id, length, alphabet, random))

    # Walk the selected segmenter, mirroring parse_document attachment, but keep
    # each content block's last source line so a marker can be inserted after it.
    needs_stamp: list[dict] = []
    current: dict | None = None
    for start, chunk in _segments_for_mode(norm, mode):
        content = strip_markers(chunk).strip(_ASCII_TRIM)
        has_id = any(mk.id and not mk.malformed for mk in find_markers(chunk))
        if content != "":
            n_lines = len(chunk.split("\n"))
            current = {
                "last_line0": start + n_lines - 2,
                "content": content,
                "has_id": has_id,
            }
            needs_stamp.append(current)
        elif current is not None:
            # marker-only chunk: its id (if any) identifies the preceding block
            if has_id:
                current["has_id"] = True

    insert_after: dict[int, str] = {}
    minted: list[dict] = []
    for blk in needs_stamp:
        if blk["has_id"]:
            continue
        new = next_id()
        hex_ = body_hash(blk["content"], hash_length) if hash else None
        insert_after[blk["last_line0"]] = format_marker(
            id=new, hash=hex_, syntax=syntax
        )
        minted.append({"id": new, "line": blk["last_line0"] + 1})

    if not insert_after:
        return StampResult(text=norm, minted=[])

    out: list[str] = []
    for i, line in enumerate(lines):
        out.append(line)
        if i in insert_after:
            out.append(insert_after[i])
    return StampResult(text="\n".join(out), minted=minted)


def restamp(
    md: str,
    hash_length: int | None = None,
    add_missing: bool = False,
    mode: str = "blank-line",
) -> RestampResult:
    """Refresh hashes that no longer match their block (SPEC.md §8): the
    deliberate "I edited this block on purpose, accept the new content" operation.
    For each well-formed marker whose stored ``hash`` differs from the current
    body hash (at the stored precision), rewrite it to the current value. With
    ``add_missing``, markers that carry no hash gain one.

    ``mode`` selects the same block segmenter accepted by :func:`stamp`.
    ``hash_length=None`` preserves each marker's stored precision. Returns a
    :class:`RestampResult` with ``text`` (LF-normalized) and ``refreshed`` ids.
    """
    norm = md.replace("\r\n", "\n").replace("\r", "\n")

    # id -> the block body it identifies (first occurrence wins; a duplicate id is
    # a separate lint error and is left for repair_duplicates).
    content_by_id: dict[str, str] = {}
    for b in parse_document(norm, mode=mode):
        if b.index < 0:
            continue
        for mk in b.markers:
            if mk.id and not mk.malformed and mk.id not in content_by_id:
                content_by_id[mk.id] = b.content

    refreshed: list[str] = []

    def transform(mk: Marker):
        if not mk.id or mk.id not in content_by_id:
            return None
        content = content_by_id[mk.id]
        if mk.hash is not None:
            length = hash_length if hash_length is not None else len(mk.hash)
            now = body_hash(content, length)
            if now == mk.hash:
                return None  # unchanged at this precision
            refreshed.append(mk.id)
            # \b mirrors the read-path HASH_RE: without it the sub false-matches the
            # `hash` inside a custom key like `rehash` and corrupts a §4-preserved key.
            return re.sub(
                r"\bhash\s*=\s*sha256:[0-9a-fA-F]+", f"hash=sha256:{now}", mk.raw, count=1
            )
        if add_missing:
            now = body_hash(
                content, hash_length if hash_length is not None else DEFAULT_HASH_LENGTH
            )
            refreshed.append(mk.id)
            return re.sub(
                r"(stay:\s*[A-Za-z0-9_-]+)", rf"\1 hash=sha256:{now}", mk.raw, count=1
            )
        return None

    return RestampResult(text=rewrite_markers(norm, transform), refreshed=refreshed)


def repair_duplicates(
    md: str,
    new_id: Callable[[], str] | None = None,
    length: int | None = None,
    alphabet: str | None = None,
    random: Callable[[int], bytes] | None = None,
    mode: str = "blank-line",
) -> RepairResult:
    """Repair duplicate ids (SPEC.md §7: copy mints a new stay). The first block
    to carry a duplicated id keeps it; every later marker carrying that id is
    given a fresh, collision-free id. A copied block's content is unchanged, so
    its hash stays valid and is left as-is.

    ``mode`` selects the same block segmenter accepted by :func:`stamp`. Returns
    a :class:`RepairResult` with ``text`` (LF-normalized) and ``renamed``
    ``[{"from", "to"}]``.
    """
    norm = md.replace("\r\n", "\n").replace("\r", "\n")
    blocks = parse_document(norm, mode=mode)

    used: set[str] = set()
    count: dict[str, int] = {}  # id -> number of marker occurrences carrying it
    for b in blocks:
        if b.index < 0:
            continue
        for mk in b.markers:
            if mk.id and not mk.malformed:
                used.add(mk.id)
                count[mk.id] = count.get(mk.id, 0) + 1
    # A duplicate is any id on more than one marker, so two markers sharing an id
    # on the *same* block (which lint_document also flags) are repaired, not just
    # the copy-across-blocks case.
    dup = {i for i, c in count.items() if c > 1}
    if not dup:
        return RepairResult(text=norm, renamed=[])

    next_id = _unique_minter(used, _default_minter(new_id, length, alphabet, random))
    seen: dict[str, int] = {}  # id -> markers-with-this-id seen so far
    renamed: list[dict] = []

    def transform(mk: Marker):
        if not mk.id or mk.id not in dup:
            return None
        c = seen.get(mk.id, 0) + 1
        seen[mk.id] = c
        if c == 1:
            return None  # first occurrence keeps the id
        fresh = next_id()
        renamed.append({"from": mk.id, "to": fresh})
        return re.sub(r"stay:\s*[A-Za-z0-9_-]+", f"stay:{fresh}", mk.raw, count=1)

    return RepairResult(text=rewrite_markers(norm, transform), renamed=renamed)
