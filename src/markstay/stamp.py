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
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Callable, Iterable

from . import lint as _lint
from .id import ID_CHARSET, mint_id

from .lint import (
    Marker,
    _blank_frontmatter,
    body_hash,
    code_lines,
    fence_state,
    find_markers,
    parse_document,
    rewrite_markers,
    segment_blank_line,
    segment_commonmark,
)

# Default truncation for a freshly written hash (§8 permits any prefix). 12 hex =
# 48 bits, enough to make an accidental same-prefix collision within one document
# negligible, while staying lighter than the full 64-char digest.
DEFAULT_HASH_LENGTH = 12

_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")  # §4 attribute key grammar
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
_PRINTABLE_RE = re.compile(r"^[\x21-\x7e]+$")  # printable ASCII, no space (bare-value)
# §4 reader qchar also admits LF inside a quoted value. The §3.3 writer contract is
# deliberately narrower: writers emit markers on one line, so this serializer accepts
# printable ASCII (0x20-0x7E) only and rejects LF, tabs, other controls, and non-ASCII.
_VALUE_RE = re.compile(r"^[\x20-\x7e]*$")

# Host comment closers are forbidden even inside a quoted value. HTML recognizes
# both spellings, while JavaScript closes an MDX comment at bare ``*/``.
_FORBIDDEN = {"html": ("-->", "--!>"), "mdx": ("*/",)}


@dataclass
class StampResult:
    text: str
    minted: list[dict] = field(default_factory=list)  # [{"id":.., "line":..}]
    drifted: list[str] = field(default_factory=list)
    # Why the write path declined to change the document, or ``None`` when it did
    # not decline. A refusal returns the source byte for byte with nothing minted,
    # which is byte-identical to a document that had nothing to do, so without this
    # a caller cannot tell "I refused" from "there was nothing to stamp". The
    # transactional row write (SPEC.md §5.6) refuses on purpose and often, so the
    # difference is one a pre-commit hook has to be able to see.
    refused: str | None = None
    # SPEC.md §3.4: the child carriers this pass declined, as
    # ``[{"kind": "row"|"list", "line": ..}]``. Skipping one child is not a
    # whole-operation refusal (``refused`` stays ``None``, the container keeps
    # its own stay and every other child is stamped), so it needs a field of its
    # own. A refusal that nothing reports is indistinguishable from a document
    # with nothing to address, and the whole error direction of §3.4 rests on a
    # refusal being visible and countable.
    refused_carriers: list[dict] = field(default_factory=list)


@dataclass
class RestampResult:
    text: str
    refreshed: list[str] = field(default_factory=list)


@dataclass
class RepairResult:
    text: str
    renamed: list[dict] = field(default_factory=list)  # [{"from":.., "to":..}]
    cleaned: list[str] = field(default_factory=list)


def format_attr_value(value) -> str:
    """Serialize one attribute value (SPEC.md §4): a bare token when it has no
    whitespace or double quote and is all printable ASCII, otherwise a
    double-quoted string with ``\\`` and ``"`` escaped.

    Raises ``ValueError`` if the value falls outside the one-line writer set
    (printable ASCII 0x20-0x7E). LF is valid reader syntax inside a §4 quoted
    value, but §3.3 forbids a conforming writer from emitting a multiline marker.
    """
    s = str(value)
    if not _VALUE_RE.match(s):
        raise ValueError(
            f"format_attr_value: value {s!r} contains a character outside the "
            f"one-line writer set (printable ASCII 0x20-0x7E)"
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
    for closer in _FORBIDDEN[syntax]:
        if closer in body:
            raise ValueError(
                f"format_marker: a value contains the {syntax} terminator "
                f"{closer!r}, which would break the marker"
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


def _digest_attribute_span(marker: Marker, key: str):
    """The first scanner-approved bare ``key=sha256:<hex>`` attribute."""
    for attribute in marker._attribute_spans:
        if attribute.key != key or attribute.quoted:
            continue
        if not attribute.value.startswith("sha256:"):
            continue
        hex_ = attribute.value[len("sha256:") :]
        if hex_ and _HEX_RE.fullmatch(hex_):
            return attribute
    return None


def _apply_marker_edits(raw: str, edits: list[tuple[int, int, str]]) -> str:
    """Apply non-overlapping scanner-relative edits without reparsing marker text."""
    ordered = sorted(edits)
    if any(left[1] > right[0] for left, right in zip(ordered, ordered[1:])):
        raise ValueError("overlapping internal marker edits")
    out = raw
    for start, end, replacement in reversed(ordered):
        out = out[:start] + replacement + out[end:]
    return out


def _segments_for_mode(text: str, mode: str) -> list[tuple[int, str]]:
    """Segment for the write path exactly as :func:`parse_document` does, leading
    frontmatter included: it is blanked line-for-line first, so the write path
    never mints an id for document metadata. Skipping it here rather than filtering
    afterwards is what keeps stamp and lint agreeing , a marker stamped onto
    frontmatter would have no block to attach to and lint would (correctly) call it
    an ORPHAN_MARKER. Line numbers are preserved by the blanking, so the caller's
    insertion points still index the original text."""
    text = _blank_frontmatter(text)
    if mode == "commonmark":
        return segment_commonmark(text)
    if mode == "blank-line":
        return segment_blank_line(text)
    raise ValueError(f"unknown parse mode: {mode!r} (use 'blank-line' or 'commonmark')")


def _line_records(line: str):
    """One-line valid marker records, or ``None`` for an unsafe overlap."""
    records = [
        record
        for record in _lint._scan_marker_records(line)
        if not record.marker.malformed
    ]
    spans = [(record.start, record.end) for record in records]
    return None if _lint._spans_overlap(spans) else records


def _unescaped_delimiters(line: str, records) -> list[int]:
    """Unescaped pipe positions with complete markers treated as opaque."""
    positions: list[int] = []
    backslashes = 0
    pos = 0
    record_index = 0
    while pos < len(line):
        if record_index < len(records) and pos == records[record_index].start:
            pos = records[record_index].end
            record_index += 1
            backslashes = 0
            continue
        char = line[pos]
        if char == "\\":
            backslashes += 1
            pos += 1
            continue
        if char == "|" and backslashes % 2 == 0:
            positions.append(pos)
        backslashes = 0
        pos += 1
    return positions


def _remove_line_records(line: str, records) -> str:
    return _lint._remove_spans(line, [(record.start, record.end) for record in records])


def _container_suffix(line: str):
    """Complete marker suffix after the final delimiter, before legality checks.

    Returns the suffix records, the line they leave behind, and the offset the
    tail they were scanned in begins at. The caller needs that offset to lift
    each line-local record back to a full-document span, which is the only form
    a relocation can be checked in.
    """
    records = _line_records(line)
    if records is None:
        return None
    delimiters = _unescaped_delimiters(line, records)
    if not delimiters:
        return None
    final = delimiters[-1]
    tail = line[final + 1 :]
    suffix = _line_records(tail)
    if (
        suffix is None
        or not suffix
        or _remove_line_records(tail, suffix).strip(" \t\f\v")
    ):
        return None
    cleaned = line[: final + 1] + _remove_line_records(tail, suffix)
    if _lint._row_cells(cleaned) is None:
        return None
    return suffix, cleaned, final + 1


def _legacy_container_suffix(line: str):
    """The exact §5.6 migration suffix, or ``None`` when the probe is illegal."""
    suffix = _container_suffix(line)
    if suffix is None or any(record.marker.has_subhash for record in suffix[0]):
        return None
    return suffix


def _block_has_records(block, records) -> bool:
    available = [(marker.id, marker.raw) for marker in block.markers]
    for record in records:
        fact = (record.marker.id, record.marker.raw)
        if fact not in available:
            return False
        available.remove(fact)
    return True


def _line_offsets(text: str) -> list[int]:
    """The absolute offset at which each LF-split line begins."""
    offsets = [0]
    for line in text.split("\n")[:-1]:
        offsets.append(offsets[-1] + len(line) + 1)
    return offsets


def _dropped_line_spans(text: str, line0s) -> list[tuple[int, int]]:
    """The spans that delete these lines, each separator claimed exactly once.

    A line is not only its own bytes: dropping one has to take a newline with
    it, and which one depends on where the line sits. Every line but the last
    owns the separator that follows it; the last owns the one before it, because
    there is nothing after it left to join to.

    That is why a run of adjacent lines is ONE span rather than one span each.
    Take them separately and the two rules collide at the end of a document with
    no trailing newline: the second-to-last line claims the LF that follows it
    and the last line claims the LF that precedes it, which is the same byte. The
    overlapping pair then reads as an ill-formed plan and refuses a stamp that is
    perfectly legal.
    """
    offsets = _line_offsets(text)
    lines = text.split("\n")
    spans: list[tuple[int, int]] = []
    for run in _consecutive_runs(sorted(set(line0s))):
        first, last = run[0], run[-1]
        if last + 1 < len(lines):
            spans.append((offsets[first], offsets[last] + len(lines[last]) + 1))
        else:
            spans.append(((offsets[first] - 1) if first else 0, len(text)))
    return spans


def _consecutive_runs(values):
    """Group a sorted list of line indices into maximal adjacent runs."""
    run: list[int] = []
    for value in values:
        if run and value != run[-1] + 1:
            yield run
            run = []
        run.append(value)
    if run:
        yield run


def _metadata_lines(text: str) -> set[int]:
    """The 1-based line numbers of leading frontmatter (SPEC.md §5).

    Derived from :func:`_blank_frontmatter` rather than recognized a second
    time, so the write path's inventory of identities and the parser's cannot
    disagree about where metadata ends. The blanking is line-for-line, so a line
    it changed is a line inside the frontmatter.
    """
    return {
        number
        for number, (source, blanked) in enumerate(
            zip(text.split("\n"), _blank_frontmatter(text).split("\n")), 1
        )
        if source != blanked
    }


def _active_records(text: str) -> list[_lint._MarkerRecord]:
    """Every record the scanner reports as ACTIVE in ``text``, malformed ones
    included, with its exact document span and its exact bytes.

    This is what a relocation has to conserve, and identities are only part of
    it. A splice that fuses the fragments around a moved marker into
    `<!-- stay:hash=x -->` writes no identity, because §4 reads a key-first body
    as a diagnostic rather than a marker. It still turns a lint-clean document
    into one reporting MALFORMED_MARKER, and a guard that counts only identities
    cannot see it arrive.

    It reads the document the way :func:`parse_document` does and not the way
    :func:`find_markers` does. `find_markers` is a grammar primitive and
    deliberately code-blind: a marker-shaped string inside a fenced code block is
    content (§3.3) and one inside frontmatter is metadata (§5). Neither is a
    marker OR a diagnostic, so counting either here makes the guard refuse a
    relocation whose only crime is moving bytes past one.
    """
    blanked = _blank_frontmatter(text)
    code = code_lines(blanked)
    metadata = _metadata_lines(text)
    return [
        record
        for record in _lint._scan_marker_records(text)
        if _lint.marker_outside_code(record.marker, code)
        and record.marker.line not in metadata
    ]


def _identity_records(text: str) -> list[_lint._MarkerRecord]:
    """The active records that carry identity: well-formed markers (§4).

    Everything active must SURVIVE an edit; only these are things a document
    means to address, so only these may be moved.
    """
    return [
        record for record in _active_records(text) if not record.marker.malformed
    ]


# Host comment openers, the two the §4 grammar rides on.
_COMMENT_OPEN_RE = re.compile(r"<!--|\{/\*")


def _mask_metadata(text: str) -> str:
    """``text`` with leading frontmatter replaced by spaces, offsets kept.

    Comment structure is read on this rather than on the source. Frontmatter is
    masked because it is YAML: an angle bracket there is a value, not markup, and
    no reader treats it as a comment.

    **Fenced code is deliberately NOT masked here, though §3.3 masks it for
    markers.** The two questions are different and only one of them is safe to get
    wrong. Whether a marker is an identity is settled by the spec's own line-based
    fence rule, and being generous there costs a refusal. Whether a `<!--` opens a
    comment is settled by the renderer, whose fence recognition this project does
    not model: inside a raw HTML block a line of backticks opens nothing, so
    blanking it hides a live opener and lets an edit re-pair a real comment. That
    is content silently disappearing, against a document that merely shows an
    unclosed example inside a listing being refused. Fail closed.

    Spaces rather than empty lines, because every offset here indexes the real
    document.
    """
    metadata = _metadata_lines(text)
    if not metadata:
        return text
    return "\n".join(
        " " * len(line) if number in metadata else line
        for number, line in enumerate(text.split("\n"), 1)
    )


def _comment_spans(text: str) -> set[tuple[int, int]]:
    """Host comment spans, read left to right the way a renderer reads them.

    Each opener takes the FIRST closer after it, which is the rule the §4 scanner
    already uses, and the next scan resumes after that closer, so the spans do not
    overlap. An unclosed opener ends the scan: to any reader everything after it
    is inside the comment, and there is nothing further to pair.

    This is deliberately NOT the marker scanner, which considers every opener
    independently so that a rejected one cannot swallow a later valid marker. That
    is the right rule for discovering identities and the wrong one for asking what
    a reader can see.
    """
    spans: set[tuple[int, int]] = set()
    position = 0
    while (opener := _COMMENT_OPEN_RE.search(text, position)) is not None:
        if opener.group(0) == "<!--":
            closers = [
                (at, len(closer))
                for closer in ("-->", "--!>")
                if (at := text.find(closer, opener.end())) >= 0
            ]
            if not closers:
                break
            at, width = min(closers)
            end = at + width
        else:
            at = text.find("*/", opener.end())
            if at < 0:
                break
            end = at + (3 if text.startswith("}", at + 2) else 2)
        spans.add((opener.start(), end))
        position = end
    return spans


def _records_at(records, spans):
    """The document records occupying exactly ``spans``, or ``None``.

    A line-local scan can report a marker the document does not have. The tail
    of a row line is a slice, and a slice beginning inside a longer marker
    carries no opener of its own, so what is left over can parse as a marker on
    its own terms. Demanding an exact document span is what stops a probe
    relocating a piece of something else.
    """
    index = {(record.start, record.end): record for record in records}
    resolved = [index.get(span) for span in spans]
    return None if any(record is None for record in resolved) else resolved


def _resolve_records(records, markers):
    """Each marker's own document record, one to one, or ``None``.

    Two byte-identical markers on one line are indistinguishable by id and raw
    text, so a lookup matching on those alone hands both marker objects the SAME
    record. A caller relocating both then excises one span and re-emits two, and
    the document gains an occurrence nobody wrote. Consuming each record as it is
    claimed is what keeps the mapping one to one.
    """
    available = list(records)
    resolved: list[_lint._MarkerRecord] = []
    for marker in markers:
        index = next(
            (
                position
                for position, record in enumerate(available)
                if record.marker.line == marker.line
                and record.marker.id == marker.id
                and record.marker.raw == marker.raw
            ),
            None,
        )
        if index is None:
            return None
        resolved.append(available.pop(index))
    return resolved


@dataclass(frozen=True)
class _Relocation:
    """A marker relocation stated in full-document byte offsets.

    ``excisions`` are the spans the edit deletes; ``moves`` are the identity
    records it re-emits, in this order and concatenated after ``prefix``, at
    ``insert_at``. Every offset indexes the text the plan was built from.

    Stating a probe this way rather than as line surgery is the whole point: it
    names which occurrence goes where, so :func:`_relocate` can check where each
    surviving occurrence LANDS instead of only which raw bytes still exist
    somewhere in the result.
    """

    excisions: tuple[tuple[int, int], ...]
    moves: tuple[_lint._MarkerRecord, ...]
    insert_at: int
    prefix: str = ""


def _relocate(text: str, plan: _Relocation, origins: list[int] | None = None) -> str | None:
    """Apply ``plan`` if it preserves every marker occurrence, else ``None``.

    The invariant, stated once so the implementation stays free to change under
    it:

    1. every record the plan moves is resolved to a full-document span, and
       exactly those records leave their place;
    2. every record the plan does not move survives, as the same occurrence, at
       its edit-adjusted position;
    3. no record appears that the plan did not write, and no record's bytes
       change, where "record" means everything the scanner reads as active and
       not only the identities: a fused fragment that reads as a §4 diagnostic
       is a document the write path has damaged just as surely as an invented id;
    4. an edit never cuts into a record, and never writes inside one, so the
       plan describes a set of disjoint splices between records;
    5. every record it moves is a host comment in its own right, and the
       document's comment structure is otherwise unchanged, so the edit can
       neither re-pair an existing comment nor write a new one.

    Rules 1 and 4 are preconditions on the PLAN and are checked before anything
    is applied. They are not a second opinion on the result: an ill-formed plan
    is refused rather than applied and then judged, because a prediction of where
    each record lands is only meaningful once the edits are disjoint and whole.

    Rule 2 is the one a multiset of raw bytes cannot state, and it is where every
    identity defect this write path has had was hiding. A multiset answers "which
    markers exist"; a relocation needs "which occurrence survived, and where".
    The two part company exactly when one marker is destroyed and a
    byte-identical one is created elsewhere in the same edit: `subhash` marker
    gone, `subhash` marker back, the count unmoved, §16 rule 1 broken.
    """
    if not plan.moves:
        return None
    before = _active_records(text)
    inventory = {(record.start, record.end): record for record in before}
    # A diagnostic is not an identity and is not the write path's to relocate:
    # §4 leaves a malformed marker exactly where its author put it.
    if any(record.marker.malformed for record in plan.moves):
        return None
    moved_spans = [(record.start, record.end) for record in plan.moves]
    # One record named twice is one record removed and two written. The rest of
    # this function compares SETS of spans, where the second copy is invisible.
    if len(set(moved_spans)) != len(moved_spans):
        return None
    # A move must name the record that is actually at that span. Comparing spans
    # alone is not enough: a plan carrying a same-span record with different bytes
    # excises the real one and writes the impostor, and every later check agrees,
    # because they all read the bytes the PLAN declares. That is the substitution
    # this function exists to refuse, arriving through its own front door.
    if any(
        (source := inventory.get(span)) is None
        or source.marker.raw != record.marker.raw
        for span, record in zip(moved_spans, plan.moves)
    ):
        return None

    # Rule 5, on the source: a record may only move out of a position where it is
    # a host comment in its own right. Nested inside another one, its own closer is
    # what terminates the outer comment, so removing it hands the outer opener a
    # LATER closer and swallows the text in between. Every byte survives that and
    # no record changes, which is exactly why no other rule here can see it: what
    # changes is how much of the document a reader can still see.
    source_comments = _comment_spans(_mask_metadata(text))
    if any(span not in source_comments for span in moved_spans):
        return None

    # Rule 4, on both halves of the edit: an excision takes whole records or none
    # of one, and the insertion point sits between records rather than inside one.
    covered: set[tuple[int, int]] = set()
    for start, end in plan.excisions:
        if not 0 <= start <= end <= len(text):
            return None
        for record in before:
            if record.end <= start or record.start >= end:
                continue
            if record.start < start or record.end > end:
                return None
            covered.add((record.start, record.end))
    if not 0 <= plan.insert_at <= len(text):
        return None
    if any(record.start < plan.insert_at < record.end for record in before):
        return None
    # Rules 1 and 2 as one comparison: the records this edit destroys are exactly
    # the records it promises to write back.
    if covered != set(moved_spans):
        return None

    written = plan.prefix + "".join(record.marker.raw for record in plan.moves)
    splices = sorted(
        [(start, end, "", False) for start, end in plan.excisions]
        + [(plan.insert_at, plan.insert_at, written, True)],
        key=lambda splice: (splice[0], splice[1]),
    )
    # Overlapping edits have no single well-defined result, so there is nothing
    # to predict and nothing to compare a prediction against.
    if any(
        later[0] < earlier[1] for earlier, later in zip(splices, splices[1:])
    ):
        return None

    out: list[str] = []
    landing = 0
    source = 0
    produced = 0
    for start, end, replacement, is_insertion in splices:
        out.append(text[source:start])
        produced += start - source
        if is_insertion:
            landing = produced
        out.append(replacement)
        produced += len(replacement)
        source = end
    out.append(text[source:])
    result = "".join(out)

    def shift(offset: int) -> int:
        """Where a source offset outside every splice lands in the result."""
        return offset + sum(
            len(replacement) - (end - start)
            for start, end, replacement, _ in splices
            if end <= offset
        )

    landed: list[tuple[int, int, str]] = []
    position = landing + len(plan.prefix)
    for record in plan.moves:
        raw = record.marker.raw
        landed.append((position, position + len(raw), raw))
        position += len(raw)

    # Rule 5, on the result: comment structure is conserved, not merely intact
    # around the records that moved. Checking only the moved records asks whether
    # THEY are still comments and never asks what the seam left behind: joining
    # `a<` to `!--secret -->` writes a comment nobody opened, which hides text
    # while every record stays exactly as independent as it was. The source half
    # above is what makes this mapping definable; this is the half that checks it.
    moved_landing = {
        (record.start, record.end): (start, end)
        for record, (start, end, _) in zip(plan.moves, landed)
    }
    expected_comments: set[tuple[int, int]] = set()
    for span in source_comments:
        if span in moved_landing:
            expected_comments.add(moved_landing[span])
            continue
        if any(start < span[1] and span[0] < end for start, end, _, _ in splices):
            return None  # a comment entangled with the edit: nothing to predict
        expected_comments.add((shift(span[0]), shift(span[0]) + span[1] - span[0]))
    if _comment_spans(_mask_metadata(result)) != expected_comments:
        return None

    expected: list[tuple[int, int, str]] = list(landed)
    for record in before:
        if (record.start, record.end) in covered:
            continue
        at = shift(record.start)
        expected.append((at, at + (record.end - record.start), record.marker.raw))
    expected.sort()

    after = [
        (record.start, record.end, record.marker.raw)
        for record in _active_records(result)
    ]
    if after != expected:
        return None
    if origins is not None:
        mapped: list[int] = []
        source = 0
        for start, end, replacement, is_insertion in splices:
            mapped.extend(origins[source:start])
            if is_insertion:
                mapped.extend([origins[plan.insert_at]] * len(plan.prefix))
                for record in plan.moves:
                    mapped.extend(origins[record.start:record.end])
            source = end
        mapped.extend(origins[source:])  # includes the EOF boundary
        origins[:] = mapped
    return result


class _WriteSnapshot:
    """Source positions survive each provisional relocation without content matching."""

    def __init__(self, text: str, mode: str):
        self.text = text
        self.origins = list(range(len(text) + 1))
        offsets = _line_offsets(text)
        self.starts = [
            offsets[block.line - 1]
            for block in parse_document(text, mode=mode, child_blocks=True)
            if block.index >= 0
        ]
        self.refused_rows: set[int] = set()

    def fork(self):
        other = object.__new__(type(self))
        other.text, other.starts = self.text, self.starts
        other.origins = self.origins.copy()
        other.refused_rows = self.refused_rows.copy()
        return other

    def prefix(self, text: str, first: int, line: int, kind: str) -> str | None:
        lines = text.split("\n")
        carrier = _carrier_prefix(lines, first, line, kind)
        if carrier is None:
            return None
        offsets = _line_offsets(text)
        row_origin = self.origins[offsets[line]]
        owner = bisect_right(self.starts, row_origin) - 1
        if owner < 0:
            return None
        end = self.origins[offsets[first] + len(carrier)]
        return self.text[self.starts[owner]:end]

    def decline(self, probe: str, block):
        offsets = _line_offsets(probe)
        self.refused_rows.update(
            self.origins[offsets[child.marker_line - 1]]
            for child in block.children
            if child.kind == "row" and not child.markers and child.marker_line > 0
        )

    def refusals(self, text: str) -> list[dict]:
        wanted = self.refused_rows
        return [
            {"kind": "row", "line": line}
            for line, offset in enumerate(_line_offsets(text), 1)
            if self.origins[offset] in wanted
        ]


def _probe_legacy_row_suffix(
    text: str, mode: str, syntax: str, snapshot: _WriteSnapshot
) -> tuple[str | None, set[str]]:
    """Relocate legal legacy suffixes, one full-document probe at a time.

    Also reports the **content** of the containers whose move was declined,
    because §3.4 refuses every row it would have made addressable or refuses the
    position it would move the marker out of. Their rows must not be stamped
    either: §5.6 requires the container's own stay to end up on a marker-only line
    after the body, so a row stay written without the move would leave the
    container's stay inside a cell.

    The returned content set is diagnostic only. Snapshot source positions
    decide which rows are refused: neither an id nor identical content identifies
    a container occurrence, and coupling refusals can leave another container's
    provisional relocation committed without the row write it was for.
    """
    work = text
    declined: set[str] = set()
    for _ in range(len(work.split("\n")) + 1):
        chunks = _segments_for_mode(work, mode)
        blocks = {
            block.line: block
            for block in parse_document(work, mode=mode, child_blocks=True)
            if block.index >= 0
        }
        lines = work.split("\n")
        offsets = _line_offsets(work)
        migrated = False
        for start, chunk in chunks:
            block = blocks.get(start)
            if block is None or any(child.kind == "row" for child in block.children):
                continue
            line_number = start + len(chunk.split("\n")) - 1
            if not 0 < line_number <= len(lines):
                return None, declined
            attempted_suffix = _container_suffix(lines[line_number - 1])
            if attempted_suffix is None:
                continue
            suffix = _legacy_container_suffix(lines[line_number - 1])
            if suffix is None:
                return None, declined
            records, _cleaned, tail_start = suffix
            if not _block_has_records(block, records):
                return None, declined
            line_start = offsets[line_number - 1]
            spans = [
                (
                    line_start + tail_start + record.start,
                    line_start + tail_start + record.end,
                )
                for record in records
            ]
            moves = _records_at(_identity_records(work), spans)
            if moves is None:
                return None, declined
            probe_snapshot = snapshot.fork()
            probe = _relocate(
                work,
                _Relocation(
                    excisions=tuple(spans),
                    moves=tuple(moves),
                    insert_at=line_start + len(lines[line_number - 1]),
                    prefix="\n",
                ),
                origins=probe_snapshot.origins,
            )
            if probe is None:
                return None, declined
            probe_parsed = parse_document(probe, mode=mode, child_blocks=True)
            probe_offsets = _line_offsets(probe)
            candidate = next(
                (
                    parsed
                    for parsed in probe_parsed
                    if parsed.index >= 0
                    and any(
                        child.kind == "row" and child.line == line_number
                        for child in parsed.children
                    )
                    and _block_has_records(parsed, records)
                ),
                None,
            )
            if candidate is not None and not (
                _relocation_safe(work, block, moves, syntax)
                and _row_carrier_available(probe, candidate, syntax, probe_snapshot)
            ):
                declined.add(block.content)
                probe_snapshot.decline(probe, candidate)
                snapshot.refused_rows.update(probe_snapshot.refused_rows)
                continue
            if candidate is None:
                # Not a migration candidate, which is different from an unsafe
                # one. The relocation itself was checked and cleared above; what
                # this says is that moving the suffix did not produce a table, so
                # there was no §5.6 migration here to do. `| a text |<!-- stay:p
                # -->` is a stamped paragraph that happens to contain pipes.
                # Refusing the whole document for it reports work withheld when
                # none was ever available, and a caller reading the refusal (or
                # the CLI's exit code) is told a document it has nothing to do
                # with was declined. The probe is discarded and the block skipped.
                continue
            snapshot.origins = probe_snapshot.origins
            work = probe
            migrated = True
            break
        if not migrated:
            return work, declined
    return None, declined


def _row_bodies(blocks: list) -> list[str]:
    """Every table row's hash body in the document, in document order.

    The unit a row-container relocation must not disturb. Reading it off an existing
    parse rather than re-parsing keeps the guard off the quadratic child-attribution
    path more than once per pass.
    """
    return [
        child.content
        for block in blocks
        for child in block.children
        if child.kind == "row"
    ]


def _prepare_row_containers(
    text: str, mode: str, syntax: str, snapshot: _WriteSnapshot | None = None
) -> tuple[str | None, list[str], set[str]]:
    """Move row-container stays and abort on pre-existing container hash drift.

    The third value is the content of the containers whose move was declined,
    because §3.4 refuses every row the move was for or the position it would take
    the marker out of. See :func:`_probe_legacy_row_suffix` for why their rows are
    refused with it. The snapshot tracks those refusals by source occurrence;
    the returned content set is diagnostic only.
    """
    snapshot = snapshot or _WriteSnapshot(text, mode)
    work, declined = _probe_legacy_row_suffix(text, mode, syntax, snapshot)
    if work is None:
        return None, [], declined
    for _ in range(len(work.split("\n")) + 1):
        parsed = parse_document(work, mode=mode, child_blocks=True)
        targets = [
            block
            for block in parsed
            if block.index >= 0
            and any(
                child.kind == "row" and not child.markers for child in block.children
            )
        ]
        before_row_bodies = _row_bodies(parsed)
        if not targets:
            return work, [], declined
        lines = work.split("\n")
        offsets = _line_offsets(work)
        drifted = [
            marker.id
            for target in targets
            for marker in target.markers
            if marker.id
            and not marker.malformed
            and not marker.has_subhash
            and marker.hash is not None
            and body_hash(target.content, len(marker.hash)) != marker.hash
        ]
        if drifted:
            return None, list(dict.fromkeys(drifted)), declined
        relocated = False
        for target in targets:
            rows = [child for child in target.children if child.kind == "row"]
            parent_markers = [
                marker
                for marker in target.markers
                if marker.id and not marker.malformed and not marker.has_subhash
            ]
            if not parent_markers:
                continue

            last_row_line = max(child.line for child in rows)
            if all(
                marker.line > last_row_line
                and marker.line <= len(lines)
                and _lint._marker_only_line(lines[marker.line - 1])
                for marker in parent_markers
            ):
                continue
            if not 0 < last_row_line <= len(lines):
                return None, [], declined

            moves = _resolve_records(_identity_records(work), parent_markers)
            if moves is None:
                return None, [], declined
            grouped: dict[int, list] = {}
            for record in moves:
                # A marker whose raw spans lines cannot be re-emitted onto one
                # carrier line without rewriting the document's line structure
                # around it, which is more than a relocation is allowed to do.
                if "\n" in record.marker.raw:
                    return None, [], declined
                line0 = record.marker.line - 1
                if not 0 <= line0 < len(lines):
                    return None, [], declined
                grouped.setdefault(line0, []).append(record)

            excisions: list[tuple[int, int]] = []
            emptied: list[int] = []
            for line0, group in grouped.items():
                line_start = offsets[line0]
                local = [
                    (record.start - line_start, record.end - line_start)
                    for record in group
                ]
                if any(
                    start < 0 or end > len(lines[line0]) for start, end in local
                ):
                    return None, [], declined
                # A line the relocation empties must go with its markers. Leaving
                # it behind as an empty string inserts a §5 block boundary, which
                # splits the very container being prepared: the relocated stay
                # then binds to a shorter body and the next pass reports the
                # tool's own edit as pre-existing container drift.
                #
                # Emptiness is decided on what SURVIVES the removal, not on
                # whether the source line was marker-only. The two differ
                # whenever a movable parent marker shares its line with a marker
                # this probe does not move: a `subhash` marker is the case that
                # matters, and §16 rule 1 requires it to survive the write path
                # lexically. Asking `_marker_only_line` about the ORIGINAL line
                # answers yes there, so the line was deleted with a marker on it.
                if _lint._remove_spans(lines[line0], local).strip(" \t\f\v") == "":
                    emptied.append(line0)
                else:
                    excisions.extend((record.start, record.end) for record in group)
            excisions.extend(_dropped_line_spans(work, emptied))

            probe_snapshot = snapshot.fork()
            probe = _relocate(
                work,
                _Relocation(
                    excisions=tuple(excisions),
                    moves=tuple(moves),
                    insert_at=offsets[last_row_line - 1]
                    + len(lines[last_row_line - 1]),
                    prefix="\n",
                ),
                origins=probe_snapshot.origins,
            )
            if probe is None:
                return None, [], declined
            probe_parsed = parse_document(probe, mode=mode, child_blocks=True)
            probe_offsets = _line_offsets(probe)
            target_rows = [
                child.content for child in target.children if child.kind == "row"
            ]
            candidate = next(
                (
                    block
                    for block in probe_parsed
                    if block.index >= 0
                    and any(child.kind == "row" for child in block.children)
                    and all(
                        any(
                            current.id == marker.id
                            and current.raw == marker.raw
                            and not current.has_subhash
                            and probe_snapshot.origins[
                                probe_offsets[current.line - 1]
                            ] == snapshot.origins[moves[0].start]
                            for current in block.markers
                        )
                        for marker in parent_markers
                    )
                    # The origin check above identifies the moved occurrence,
                    # even when another table has identical markers AND rows.
                    # Row bodies still verify that its contents survived.
                    and [
                        child.content
                        for child in block.children
                        if child.kind == "row"
                    ] == target_rows
                ),
                None,
            )
            if candidate is None:
                return None, [], declined
            # Relocating a container stay must leave every row's hash body alone,
            # including rows this pass has no business touching. It normally does:
            # ``ChildBlock.content`` arrives with markers already cut, so lifting a
            # marker out of a cell leaves the same body behind. It does not when the
            # bytes around the marker change meaning once it goes. `a\<!-- stay:p -->|`
            # becomes `a\|`, where the backslash now escapes the delimiter, and the two
            # cells either side fuse into one: the row's body changes, so a `subhash`
            # already stored on that row covers a body that no longer exists. Nothing
            # downstream catches it. §11's diff sees the child marker still present at
            # the same id, the container refresh only re-checks children this pass
            # minted, and a block-level lint of the result is clean, so the document
            # goes out with row evidence that silently no longer matches.
            #
            # The comparison is over the WHOLE document rather than over the candidate,
            # because the candidate is chosen by matching the parent marker's id and
            # raw text and a duplicate id (§7, an error, but one `stamp` does not
            # refuse) makes that match land on the wrong table: the guard would then
            # compare an untouched table against itself and pass the corrupting write
            # through. Every row body in the document is the invariant, and a legitimate
            # relocation changes none of them.
            if _row_bodies(probe_parsed) != before_row_bodies:
                return None, [], declined
            if not (
                _relocation_safe(work, target, moves, syntax)
                and _row_carrier_available(probe, candidate, syntax, probe_snapshot)
            ):
                # §3.4 refuses every row this relocation was for, so the
                # relocation buys nothing and §5.6 does not permit keeping it:
                # moving a stay out of a cell is itself an edit, and the document
                # that motivated this gate is one the move re-renders. The
                # container's rows go with it, since a row stay written without
                # the move would leave the container's stay inside a cell.
                declined.add(target.content)
                probe_snapshot.decline(probe, candidate)
                snapshot.refused_rows.update(probe_snapshot.refused_rows)
                continue
            snapshot.origins = probe_snapshot.origins
            work = probe
            relocated = True
            break
        if not relocated:
            return work, [], declined
    return None, [], declined


def _row_marker_position(line: str) -> int | None:
    working = line.rstrip(" \t\f\v")
    records = _line_records(working)
    if records is None:
        return None
    delimiters = _unescaped_delimiters(working, records)
    if len(delimiters) < 2:
        return None
    start, end = delimiters[-2], delimiters[-1]
    cell = line[start + 1 : end]
    return start + 1 + len(cell.rstrip(" \t\f\v"))


# --- SPEC.md §3.4: plain-text state at a carrier position -------------------
#
# Two rules in the specification make a writer put a marker on a line that
# already carries content: §5.5's child carrier at the end of a list item's last
# paragraph, and §5.6's row carrier inside a row's last cell. In those positions
# the text already in the container, and the marker's own bytes, can capture the
# marker, and the document then shows something it did not show before.
#
# The rule refuses on the PRESENCE of a character, never on what that character
# means. A `<` inside a code span opens nothing and is refused anyway. The
# predicate that decided which `<` was live was written and withdrawn: fifteen
# documents across four review rounds broke it, and deciding them correctly needs
# tag and attribute state, per-element raw-text termination, processing
# instruction and CDATA closers, inline precedence by first opener, backslash
# escapes and GFM cell splitting, which is an HTML tokenizer that §14 declines and
# that three parser-free implementations cannot agree on byte for byte.

# `<` begins every HTML construct a marker can complete (a comment whose closer
# the marker supplies, a tag or declaration whose `>` it supplies, a processing
# instruction or CDATA section a marker's bytes can close, and a raw-text element
# that displays the marker instead of hiding it); a backslash can escape the
# marker's opening bracket; `{` begins an MDX expression. A backtick is
# deliberately absent, because refusing every carrier text containing one costs
# 52% of real carrier positions: the marker clause below carries that case, since
# only the marker's own bytes can close a span the carrier text left open.
CARRIER_CAPTURING = {"html": "<\\", "mdx": "<\\{"}

# A marker carrying nothing beyond its id and its digest, in §4's grammar rather
# than in `\s`: §4 admits space and tab between attributes and admits neither LF
# nor U+00A0, and three implementations reading `\s` would disagree about both.
# The delimiters are paired rather than alternated, so `<!-- stay:x */}` is not a
# marker in either host syntax.
_CARRIER_BODY = (
    r"[ \t]*stay:[A-Za-z0-9_-]+"
    r"(?:[ \t]+(?:hash|subhash)=sha256:[0-9a-fA-F]+)*"
    r"[ \t]*"
)
_PLAIN_MARKER = re.compile(
    rf"<!--{_CARRIER_BODY}-->\Z|\{{/\*{_CARRIER_BODY}\*/\}}\Z"
)

# A delimiter run whose meaning depends on the character after it. A flush
# insertion changes that character from whitespace to `<`, which makes a run that
# was closing alone both opening and closing, and CommonMark's multiple-of-three
# rule then refuses the match it used to make: `| *Hello!** |` stops rendering
# its emphasis. Nothing is captured and no refused character appears anywhere in
# the document, so no prefix of any length can see this. It is the insertion
# itself, which is why §5.5's separated child carrier is unaffected by the same
# text.
_TRAILING_DELIMITER = re.compile(r"[*_~]\Z")


def plain_marker(marker: str) -> bool:
    """Does this marker carry only its id and its digest (SPEC.md §3.4)?

    An unwritten marker counts: a caller asking about a carrier text alone passes
    the empty string.
    """
    return not marker or bool(_PLAIN_MARKER.fullmatch(marker.strip()))


def _outside_markers(text: str, syntax: str = "html") -> str:
    """``text`` with plain §4 markers masked, byte offsets preserved.

    A marker carrying only an id and a digest is not text that can capture the
    next one: it is a closed host comment, its own bytes hold no character any
    construct is built from, and §4 forbids the host closer inside it. Masking it
    is the lexical step §5.6's row scan already requires of every implementation
    (treat a marker as an opaque token) rather than a judgement about what a
    character means, and what counts as one is §4's own grammar.

    **Plain, not merely complete.** A marker's evidence can be reached from
    outside it: `` - `<!-- stay:x quote="`<textarea>" --> `` has an earlier
    backtick that pairs with the one inside the quoted value, which ends the code
    span inside the marker and makes the `<textarea>` after it live HTML, and a
    `|` in such a value splits its GFM cell before any inline parsing happens. So
    the same clause that keeps a writer from putting evidence at a carrier decides
    what may be masked in front of one, and a marker carrying evidence is text
    here like any other.

    Either host form counts. A plain MDX-form marker holds nothing that captures
    in the HTML profile and the reverse holds too, and refusing the other form
    would make the answer depend on which profile a pass happens to be writing,
    which a §5.6 preparation can then change by relocating one.

    §3.3 decides what a marker is before this does: a marker-shaped string inside
    a fenced code block is content, so it is not masked, exactly as the linter's
    §5.4 exclusion has it. Its bytes cannot capture either, being rendered
    literally, but the two paths answering the same question differently is how a
    later reader ends up with two rules.

    Spaces rather than deletion, so the bytes each side keep their positions: a
    backslash in front of a masked marker is still the last byte of what precedes
    it.
    """
    code = code_lines(_blank_frontmatter(text))
    out = list(text)
    for record in _lint._scan_marker_records(text):
        if not _lint.marker_outside_code(record.marker, code):
            continue
        if not plain_marker(record.marker.raw):
            continue
        for at in range(record.start, min(record.end, len(out))):
            # Line endings stay: a marker may span lines (§4 admits normalized LF
            # inside a quoted value) and masking its LF away would merge the
            # lines around it, which the flush clause reads the last byte of.
            if out[at] != "\n":
                out[at] = " "
    return "".join(out)


def plain_text_state(
    carrier_text: str,
    marker: str = "",
    syntax: str = "html",
    flush: bool = False,
) -> bool:
    """SPEC.md §3.4: may this marker be inserted after this carrier text?

    ``carrier_text`` is the container block's raw source from its first byte up
    to the position the marker will occupy, read from the document as the
    operation found it. Not the child's own span: a raw-text element opened in a
    table's header row captures a carrier written in a later body row, and an
    unclosed construct in one list item captures a carrier in the next one.

    ``flush`` marks a carrier written hard against the text rather than after a
    separator, which today is §5.6's row carrier and nothing else.
    """
    scanned = _outside_markers(carrier_text, syntax)
    if any(character in scanned for character in CARRIER_CAPTURING[syntax]):
        return False
    if flush and _TRAILING_DELIMITER.search(scanned):
        return False
    return plain_marker(marker)


def _carrier_prefix(
    lines: list[str], first_line0: int, marker_line0: int, kind: str
) -> str | None:
    """§3.4's carrier text for one child, or ``None`` when there is no position.

    The prefix ends where the marker goes rather than at the end of the line,
    which is what lets the flush clause see a row's last cell content instead of
    its closing pipe.
    """
    if not 0 <= first_line0 <= marker_line0 < len(lines):
        return None
    tail = lines[marker_line0]
    if kind == "row":
        position = _row_marker_position(tail)
        if position is None:
            return None
        tail = tail[:position]
    else:
        tail = tail.rstrip()
    return "\n".join(lines[first_line0:marker_line0] + [tail])


def _trailing_blank_lines(chunk: str) -> int:
    """§5 ASCII-only blank lines at the end of a segmenter chunk."""
    lines = chunk.split("\n")
    count = 0
    for line in reversed(lines[1:]):
        if line.strip(" \t\f\v"):
            break
        count += 1
    return count


def _relocation_safe(text: str, block, records, syntax: str) -> bool:
    """May this relocation take these markers OUT of where they sit?

    §3.4 asks whether inserting a marker changes what a reader sees; removing one
    is the same question from the other side, and a §5.6 preparation removes
    before it inserts. Two conditions, and each has a document:

    * **the marker's own bytes must hold no backtick and no `|`.** Those are the
      two characters a host reads as structure through a comment: a backtick pairs
      with one outside the marker and puts part of it in a code span, and a `|`
      splits the GFM cell the marker sits in, so removing the marker changes the
      cells the row has. Both are the mechanisms §3.4's marker clause already
      names, read backwards, and everything else inside a marker is inert unless
      something in front of it is not, which the next condition covers. The rest
      of a §4 body may move: a pre-v1.6 container stay carrying `x-note=legacy`
      relocates as it always did.
    * **the text in front of it, inside its container, must be in plain-text state
      and free of backticks.** `| *Hello!**<!-- stay:p --> |` renders literally
      because the delimiter run is followed by `<`; take the marker away and the
      emphasis appears. A backtick before the marker can be an open code span
      displaying it, and deciding which is the parse question §3.4 declines, so it
      is refused on presence like everything else here. Backticks cost nothing at
      this position: a relocation happens only where a container stay is not
      already on a marker-only line after its rows.
    """
    lines = text.split("\n")
    if not 0 < block.line <= len(lines):
        return False
    start = _line_offsets(text)[block.line - 1]
    for record in records:
        if "`" in record.marker.raw or "|" in record.marker.raw:
            return False
        if record.start < start:
            return False
        prefix = text[start:record.start]
        if "`" in prefix:
            return False
        flush = bool(prefix) and prefix[-1] not in " \t\f\v"
        if not plain_text_state(prefix, syntax=syntax, flush=flush):
            return False
    return True


def _row_carrier_available(
    probe: str, block, syntax: str, snapshot: _WriteSnapshot | None = None
) -> bool:
    """Would any unmarked row of this prepared container actually take a stay?

    A §5.6 preparation is provisional and §5.6 requires it to commit with the row
    write or not at all, so a container whose every row §3.4 refuses must not keep
    the relocation. Deciding it here rather than by rolling the document back
    afterwards is what keeps the answer independent of unrelated work elsewhere:
    a rollback conditioned on "nothing was minted anywhere" commits this
    relocation as soon as some other block in the document takes a stay.
    """
    lines = probe.split("\n")
    code = code_lines(_blank_frontmatter(probe))
    for child in block.children:
        if child.kind != "row" or child.markers:
            continue
        if child.marker_line <= 0 or child.marker_line in code:
            continue
        carrier = _carrier_prefix(lines, block.line - 1, child.marker_line - 1, "row")
        original = (
            snapshot.prefix(probe, block.line - 1, child.marker_line - 1, "row")
            if snapshot is not None else carrier
        )
        if carrier is not None and original is not None and all(
            plain_text_state(prefix, syntax=syntax, flush=True)
            for prefix in (carrier, original)
        ):
            return True
    return False


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
    child_blocks: bool = False,
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
    with ``text`` and ``minted`` ``[{"id", "line"}]``. Successful output,
    including a successful no-op, is LF-normalized. A transactional row-write
    refusal instead returns the original input byte-for-byte, including CRLF.
    """
    if child_blocks and not hash:
        raise ValueError("stamp: child_blocks requires subhash evidence")

    original_norm = md.replace("\r\n", "\n").replace("\r", "\n")
    norm = original_norm
    snapshot = _WriteSnapshot(norm, mode) if child_blocks else None
    if child_blocks:
        prepared, drifted, _declined = _prepare_row_containers(norm, mode, syntax, snapshot)
        if prepared is None:
            return StampResult(
                text=md,
                minted=[],
                drifted=drifted,
                refused="container-drift" if drifted else "unsafe-relocation",
            )
        norm = prepared
    lines = norm.split("\n")
    norm_offsets = _line_offsets(norm)
    # SPEC.md §3.3, computed on the blanked text so it agrees line-for-line with
    # what parse_document sees. `open_after` is the writer's half of the rule: a
    # marker appended after a line a fence is still open on lands *inside the
    # listing*, which is how this project's own §4 grammar block acquired a real
    # marker in the middle of its ABNF.
    code, fence_open_after = fence_state(_blank_frontmatter(norm))

    # Existing ids across the whole document, so a minted id can't collide. The
    # raw scan is deliberate: an id shown in a fenced example is not an id, but
    # minting the same token beside it would read as one to every human.
    used = {mk.id for mk in find_markers(norm) if mk.id and not mk.malformed}
    next_id = _unique_minter(used, _default_minter(new_id, length, alphabet, random))

    # Walk the selected segmenter, mirroring parse_document attachment, but keep
    # each content block's last source line so a marker can be inserted after it.
    needs_stamp: list[dict] = []
    current: dict | None = None
    parsed_blocks = {
        b.line: b
        for b in parse_document(norm, mode=mode, child_blocks=child_blocks)
        if b.index >= 0
    }
    for start, chunk in _segments_for_mode(norm, mode):
        parsed_block = parsed_blocks.get(start)
        if parsed_block is not None:
            content = parsed_block.content
            # §16 is exact-key based. An invalid or quoted `subhash` still cannot
            # make its containing block look stamped; `x-subhash` still can.
            has_id = any(
                mk.id and not mk.malformed and not mk.has_subhash
                for mk in parsed_block.markers
            )
            n_lines = len(chunk.split("\n"))
            children = []
            row_lines: set[int] = set()
            if child_blocks:
                row_lines = {
                    child.marker_line - 1
                    for child in parsed_block.children
                    if child.kind == "row" and child.marker_line > 0
                }
                for child in parsed_block.children:
                    if child.marker_line <= 0:
                        continue
                    children.append(
                        {
                            "content": child.content,
                            "marker_line0": child.marker_line - 1,
                            "has_id": any(
                                mk.id and not mk.malformed for mk in child.markers
                            ),
                            "kind": child.kind,
                        }
                    )
            current = {
                "first_line0": start - 1,
                # The block's last CONTENT line, not the last line its span
                # covers. A §5.2 node's source map runs to the start of the next
                # block, so a loose list's span ends on the blank line after it
                # and a marker written there begins the next run: §5.1 then binds
                # it to the block BELOW, and one marker means two things. Trimming
                # is a no-op under §5.1, whose runs have no trailing blank line.
                "last_line0": start + n_lines - 2 - _trailing_blank_lines(chunk),
                "content": content,
                "has_id": has_id,
                "children": children,
                "row_lines": row_lines,
                "row_last_line0": max(row_lines) if row_lines else None,
                # §3.3 writer rule, in the two halves it actually has. A block
                # is refused when a fence was already open *before* its first
                # line (its span lies inside a listing), or when one is still
                # open after its last (the marker would be written into the
                # listing). Both are needed and neither implies the other: the
                # baseline segmenter splits a blank-line fence into halves, and
                # the second half starts inside the fence while ending on the
                # closing line, where an insertion-point test alone would happily
                # stamp half a listing. "Before its first line" rather than "its
                # first line is code" is what keeps a complete fence stampable
                # under §5.2, where the block *is* the fence and takes its stay
                # after the closing line in the ordinary way.
                "in_fence": (start - 1) in fence_open_after
                or (start + n_lines - 1) in fence_open_after,
            }
            needs_stamp.append(current)

    insert_after: dict[int, str] = {}
    append_inline: dict[int, list[str]] = {}
    row_inline: dict[int, str] = {}
    minted: list[dict] = []
    refused_carriers: list[dict] = snapshot.refusals(norm) if snapshot else []
    expected_children: list[tuple[str, str]] = []
    expected_parents: list[str] = []
    for blk in needs_stamp:
        for child in blk["children"]:
            if child["has_id"] or (child["marker_line0"] + 1) in code:
                continue
            if child["kind"] == "list" and child["marker_line0"] in blk["row_lines"]:
                # A list paragraph and a nested row can nominate the same source
                # line. The row carrier is inside its last cell; appending the list
                # marker after the closing pipe would invalidate the table.
                continue
            # SPEC.md §3.4: the carrier text and the marker's own bytes can each
            # capture the marker at these two positions, and a writer that hits
            # either mints nothing for that child block. The container is
            # unaffected, and so is every other child. The carrier text is
            # checked before an id is minted, so a refusal spends nothing; the
            # marker is checked once its bytes exist.
            #
            # Check the original snapshot as well as prepared text. Relocation
            # may remove an extension-bearing marker that §3.4 does not mask.
            carrier = _carrier_prefix(
                lines, blk["first_line0"], child["marker_line0"], child["kind"]
            )
            if (
                child["kind"] == "row" and snapshot is not None
                and snapshot.origins[norm_offsets[child["marker_line0"]]]
                in snapshot.refused_rows
            ):
                carrier = None
            flush = child["kind"] == "row"
            original_carrier = snapshot.prefix(
                norm, blk["first_line0"], child["marker_line0"], child["kind"]
            ) if snapshot else carrier
            if carrier is None or original_carrier is None or not all(
                plain_text_state(prefix, syntax=syntax, flush=flush)
                for prefix in (carrier, original_carrier)
            ):
                refusal = {"kind": child["kind"], "line": child["marker_line0"] + 1}
                if refusal not in refused_carriers:
                    refused_carriers.append(refusal)
                continue
            new = next_id()
            hex_ = body_hash(child["content"], hash_length)
            marker = format_marker(
                id=new,
                attrs=[("subhash", f"sha256:{hex_}")],
                syntax=syntax,
            )
            if not plain_text_state(carrier, marker, syntax=syntax, flush=flush):
                refusal = {"kind": child["kind"], "line": child["marker_line0"] + 1}
                if refusal not in refused_carriers:
                    refused_carriers.append(refusal)
                continue
            if child["kind"] == "row":
                row_inline[child["marker_line0"]] = marker
            else:
                append_inline.setdefault(child["marker_line0"], []).append(marker)
            minted.append({"id": new, "line": child["marker_line0"] + 1})
            expected_children.append((new, child["kind"]))
        if blk["has_id"] or blk["in_fence"]:
            continue
        new = next_id()
        hex_ = body_hash(blk["content"], hash_length) if hash else None
        carrier_line0 = (
            blk["row_last_line0"]
            if blk["row_last_line0"] is not None
            else blk["last_line0"]
        )
        insert_after[carrier_line0] = format_marker(id=new, hash=hex_, syntax=syntax)
        minted.append({"id": new, "line": carrier_line0 + 1})
        expected_parents.append(new)

    # Refusals are discovered in prepared coordinates. Rollbacks report the
    # original document; successful writes include any new marker-only lines.
    offsets = _line_offsets(norm)
    original_refusals = [
        {"kind": item["kind"], "line": (
            original_norm.count("\n", 0, snapshot.origins[offsets[item["line"] - 1]]) + 1
            if snapshot else item["line"]
        )}
        for item in refused_carriers
    ]
    returned_refusals = sorted(
        ({"kind": item["kind"], "line": item["line"] + sum(
            at < item["line"] - 1 for at in insert_after
        )} for item in refused_carriers),
        key=lambda item: (item["line"], item["kind"]),
    )

    if not insert_after and not append_inline and not row_inline:
        # Nothing was minted, so nothing justifies the provisional relocation a
        # §5.6 preparation may have made: the document goes back as it arrived,
        # line endings aside. Reachable only through §3.4, since a container is
        # prepared exactly when it has a row to stamp.
        return StampResult(
            text=original_norm, minted=[], refused_carriers=original_refusals
        )

    out: list[str] = []
    for i, line in enumerate(lines):
        if i in row_inline:
            position = _row_marker_position(line)
            if position is None:
                return StampResult(
                text=md,
                minted=[],
                refused="no-row-carrier",
                refused_carriers=original_refusals,
            )
            line = line[:position] + row_inline[i] + line[position:]
        if i in append_inline:
            for marker in append_inline[i]:
                if line and not line.endswith((" ", "\t", "\f", "\v")):
                    line += " "
                line += marker
        out.append(line)
        if i in insert_after:
            out.append(insert_after[i])
    proposal = "\n".join(out)

    # Row writes and any provisional container relocation commit only after the
    # complete proposed document passes the selected segmenter and §5.6 scan.
    if child_blocks and (row_inline or norm != original_norm):
        proposed_blocks = [
            block
            for block in parse_document(proposal, mode=mode, child_blocks=True)
            if block.index >= 0
        ]
        for child_id, kind in expected_children:
            hits = [
                child
                for block in proposed_blocks
                for child in block.children
                if child.kind == kind
                and any(marker.id == child_id for marker in child.markers)
            ]
            if len(hits) != 1:
                return StampResult(
                    text=md,
                    minted=[],
                    refused="child-not-addressable",
                    refused_carriers=original_refusals,
                )
        for parent_id in expected_parents:
            hits = [
                (block, marker)
                for block in proposed_blocks
                for marker in block.markers
                if marker.id == parent_id and not marker.has_subhash
            ]
            if len(hits) != 1:
                return StampResult(
                    text=md,
                    minted=[],
                    refused="parent-not-addressable",
                    refused_carriers=original_refusals,
                )
            block, marker = hits[0]
            if (
                marker.hash is not None
                and body_hash(block.content, len(marker.hash)) != marker.hash
            ):
                return StampResult(
                    text=md,
                    minted=[],
                    refused="proposal-drifts",
                    refused_carriers=original_refusals,
                )
    return StampResult(
        text=proposal, minted=minted, refused_carriers=returned_refusals
    )


def restamp(
    md: str,
    hash_length: int | None = None,
    add_missing: bool = False,
    mode: str = "blank-line",
    child_blocks: bool = False,
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
    child_content_by_id: dict[str, str] = {}
    for b in parse_document(norm, mode=mode, child_blocks=child_blocks):
        if b.index < 0:
            continue
        for mk in b.markers:
            if (
                mk.id
                and not mk.malformed
                and not mk.has_subhash
                and mk.id not in content_by_id
            ):
                content_by_id[mk.id] = b.content
        if child_blocks:
            for child in b.children:
                for mk in child.markers:
                    if mk.id and not mk.malformed and mk.id not in child_content_by_id:
                        child_content_by_id[mk.id] = child.content

    refreshed: list[str] = []

    def transform(mk: Marker):
        if not mk.id:
            return None
        if child_blocks and mk.subhash is not None and mk.id in child_content_by_id:
            content = child_content_by_id[mk.id]
            length = hash_length if hash_length is not None else len(mk.subhash)
            now = body_hash(content, length)
            if now == mk.subhash:
                return None
            attribute = _digest_attribute_span(mk, "subhash")
            if attribute is None:
                return None
            refreshed.append(mk.id)
            return _apply_marker_edits(
                mk.raw,
                [
                    (
                        attribute.value_start + len("sha256:"),
                        attribute.value_end,
                        now,
                    )
                ],
            )
        if mk.has_subhash and mk.id not in child_content_by_id:
            return None
        if mk.id not in content_by_id:
            return None
        content = content_by_id[mk.id]
        if mk.hash is not None:
            length = hash_length if hash_length is not None else len(mk.hash)
            now = body_hash(content, length)
            if now == mk.hash:
                return None  # unchanged at this precision
            attribute = _digest_attribute_span(mk, "hash")
            if attribute is None:
                return None
            refreshed.append(mk.id)
            return _apply_marker_edits(
                mk.raw,
                [
                    (
                        attribute.value_start + len("sha256:"),
                        attribute.value_end,
                        now,
                    )
                ],
            )
        if add_missing:
            if mk.has_subhash:
                # SPEC.md §5.5: a marker carrying `subhash` addresses a child
                # block and never the container, so the container's digest must
                # not be added beside it. The guard is unconditional rather than
                # gated on ``child_blocks``: a tool asked to segment a loose list
                # without a CommonMark parser sees no children at all, and that
                # is exactly the run that would otherwise write the wrong digest
                # onto every item of a child-stamped list.
                return None
            now = body_hash(
                content, hash_length if hash_length is not None else DEFAULT_HASH_LENGTH
            )
            if mk._id_span is None:
                return None
            refreshed.append(mk.id)
            return _apply_marker_edits(
                mk.raw,
                [(mk._id_span[1], mk._id_span[1], f" hash=sha256:{now}")],
            )
        return None

    # §3.3: an illustrative marker in a fence is not this document's marker, and
    # rewriting its `hash=` to the digest of the fence around it is the defect
    # that opened the rule.
    return RestampResult(
        text=rewrite_markers(norm, transform, code_lines(_blank_frontmatter(norm))),
        refreshed=refreshed,
    )


def repair_duplicates(
    md: str,
    new_id: Callable[[], str] | None = None,
    length: int | None = None,
    alphabet: str | None = None,
    random: Callable[[int], bytes] | None = None,
    mode: str = "blank-line",
    child_blocks: bool = False,
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
    blocks = parse_document(norm, mode=mode, child_blocks=child_blocks)

    used: set[str] = set()
    count: dict[str, int] = {}  # id -> number of marker occurrences carrying it
    for b in blocks:
        if b.index < 0:
            continue
        for mk in b.markers:
            if mk.id and not mk.malformed:
                used.add(mk.id)
                count[mk.id] = count.get(mk.id, 0) + 1
        if child_blocks:
            for child in b.children:
                for mk in child.markers:
                    if mk.id and not mk.malformed:
                        used.add(mk.id)
                        count[mk.id] = count.get(mk.id, 0) + 1
    # A duplicate is any id on more than one marker, so two markers sharing an id
    # on the *same* block (which lint_document also flags) are repaired, not just
    # the copy-across-blocks case.
    dup = {i for i, c in count.items() if c > 1}
    injected: set[str] = set()
    if child_blocks:
        for b in blocks:
            if b.index < 0:
                continue
            for child in b.children:
                for mk in child.markers:
                    if (
                        mk.id
                        and mk.hash
                        and body_hash(b.content, len(mk.hash)) == mk.hash
                    ):
                        injected.add(mk.id)
    if not dup and not injected:
        return RepairResult(text=norm, renamed=[], cleaned=[])

    next_id = _unique_minter(used, _default_minter(new_id, length, alphabet, random))
    seen: dict[str, int] = {}  # id -> markers-with-this-id seen so far
    renamed: list[dict] = []
    cleaned: list[str] = []

    def transform(mk: Marker):
        if not mk.id:
            return None
        raw = mk.raw
        edits: list[tuple[int, int, str]] = []
        if mk.id in dup:
            c = seen.get(mk.id, 0) + 1
            seen[mk.id] = c
            if c > 1:
                fresh = next_id()
                renamed.append({"from": mk.id, "to": fresh})
                if mk._id_span is not None:
                    edits.append((mk._id_span[0], mk._id_span[1], fresh))
        if child_blocks and mk.has_subhash and mk.id in injected and mk.hash:
            attribute = _digest_attribute_span(mk, "hash")
            if attribute is not None:
                edits.append((attribute.start, attribute.end, ""))
                cleaned.append(mk.id)
        return _apply_marker_edits(raw, edits) if edits else None

    return RepairResult(
        text=rewrite_markers(norm, transform, code_lines(_blank_frontmatter(norm))),
        renamed=renamed,
        cleaned=cleaned,
    )
