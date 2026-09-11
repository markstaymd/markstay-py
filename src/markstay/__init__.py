"""markstay , Python reference implementation of the markstay spec (v1.7).

A source-level identity primitive for Markdown blocks: an id token that *stays*
bound to its block across edits. This package is the parser-free core (everything
string-level and parser-independent) plus the attachment resolver. It mirrors the
JavaScript reference (`markstay` on npm); both are gated by a shared
language-neutral conformance corpus.

Public API (the spec'd portion mirrors the JS `index.js` surface; child-block
names are experimental Python-only):

  hashing (§8)    normalize_body, body_hash
  markers (§3/§4) Marker, find_markers, strip_markers
  code (§3.3)     code_lines, fence_state, strip_markers_outside_code
  segment (§5)    segment_blank_line, segment_commonmark
  parse (§5)      Block, parse_document
  lint (§7/§11)   Finding, lint_document, lint_diff, sort_findings, has_errors
  rewrite (§3/§4) rewrite_markers
  id (§6)         mint_id, DEFAULT_ALPHABET, DEFAULT_ID_LENGTH, ID_CHARSET
  write (§3-§8)   format_marker, format_attr_value, stamp, restamp,
                  repair_duplicates, DEFAULT_HASH_LENGTH
  quote (§9)      Selector, normalize, body_score, context_bonus, best_match,
                  CONTEXT_CHARS
  resolve (§9.1)  Anchor, Resolution, build_anchors, resolve,
                  DEFAULT_THRESHOLD, DEFAULT_MARGIN
  preserve (§11)  PRESERVE_INSTRUCTION, PRESERVE_RETURN_ONLY, preserve_wrap
  commit check    CommitEntry, StagedCheck, check_entries
"""

from __future__ import annotations

from .id import (
    DEFAULT_ALPHABET,
    DEFAULT_ID_LENGTH,
    ID_CHARSET,
    mint_id,
)
from .lint import (
    Block,
    ChildBlock,
    Finding,
    Marker,
    body_hash,
    child_body,
    code_lines,
    fence_state,
    find_markers,
    has_errors,
    lint_diff,
    lint_document,
    normalize_body,
    parse_document,
    rewrite_markers,
    segment_blank_line,
    segment_child_items,
    segment_commonmark,
    sort_findings,
    strip_markers,
    strip_markers_outside_code,
)
from .stamp import (
    DEFAULT_HASH_LENGTH,
    RepairResult,
    RestampResult,
    StampResult,
    format_attr_value,
    format_marker,
    repair_duplicates,
    restamp,
    stamp,
)
from .quote import (
    CONTEXT_CHARS,
    Candidate,
    Evidence,
    Selector,
    best_match,
    body_score,
    context_bonus,
    normalize,
    rank_candidates,
)
from .resolve import (
    DEFAULT_MARGIN,
    DEFAULT_THRESHOLD,
    Anchor,
    ChildAnchor,
    ChildResolution,
    Resolution,
    build_anchors,
    build_child_anchors,
    resolve,
    resolve_children,
)
from .preserve import (
    INSTRUCTION as PRESERVE_INSTRUCTION,
    RETURN_ONLY as PRESERVE_RETURN_ONLY,
    preserve_wrap,
)
from .staged import CommitEntry, StagedCheck, check_entries

__version__ = "0.11.0"

__all__ = [
    "__version__",
    # hashing
    "normalize_body",
    "body_hash",
    "child_body",
    # markers
    "Marker",
    "find_markers",
    "strip_markers",
    "code_lines",
    "fence_state",
    "strip_markers_outside_code",
    "rewrite_markers",
    # segmentation
    "segment_blank_line",
    "segment_commonmark",
    "segment_child_items",
    # parse
    "Block",
    "ChildBlock",
    "parse_document",
    # lint
    "Finding",
    "lint_document",
    "lint_diff",
    "sort_findings",
    "has_errors",
    # id (§6)
    "mint_id",
    "DEFAULT_ALPHABET",
    "DEFAULT_ID_LENGTH",
    "ID_CHARSET",
    # write path (§3-§8)
    "format_marker",
    "format_attr_value",
    "stamp",
    "restamp",
    "repair_duplicates",
    "StampResult",
    "RestampResult",
    "RepairResult",
    "DEFAULT_HASH_LENGTH",
    # quote / §9 recovery
    "Selector",
    "Evidence",
    "Candidate",
    "normalize",
    "body_score",
    "context_bonus",
    "best_match",
    "rank_candidates",
    "CONTEXT_CHARS",
    # resolve / §9.1 ladder
    "Anchor",
    "ChildAnchor",
    "ChildResolution",
    "Resolution",
    "build_anchors",
    "build_child_anchors",
    "resolve",
    "resolve_children",
    "DEFAULT_THRESHOLD",
    "DEFAULT_MARGIN",
    # preserve / §11 AI editing contract
    "PRESERVE_INSTRUCTION",
    "PRESERVE_RETURN_ONLY",
    "preserve_wrap",
    # git-independent commit-check core
    "CommitEntry",
    "StagedCheck",
    "check_entries",
]
