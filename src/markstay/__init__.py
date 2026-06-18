"""markstay , Python reference implementation of the markstay spec (v1.1).

A source-level identity primitive for Markdown blocks: an id token that *stays*
bound to its block across edits. This package is the parser-free core (everything
string-level and parser-independent) plus the attachment resolver. It mirrors the
JavaScript reference (`markstay` on npm); both are gated by a shared
language-neutral conformance corpus.

Public API (mirrors the JS `index.js` surface):

  hashing (§8)    normalize_body, body_hash
  markers (§3/§4) Marker, find_markers, strip_markers
  segment (§5)    segment_blank_line, segment_commonmark
  parse (§5)      Block, parse_document
  lint (§7/§11)   Finding, lint_document, lint_diff, sort_findings, has_errors
  quote (§9)      Selector, normalize, body_score, context_bonus, best_match,
                  CONTEXT_CHARS
  resolve (§9.1)  Anchor, Resolution, build_anchors, resolve,
                  DEFAULT_THRESHOLD, DEFAULT_MARGIN
"""

from __future__ import annotations

from .lint import (
    Block,
    Finding,
    Marker,
    body_hash,
    find_markers,
    has_errors,
    lint_diff,
    lint_document,
    normalize_body,
    parse_document,
    segment_blank_line,
    segment_commonmark,
    sort_findings,
    strip_markers,
)
from .quote import (
    CONTEXT_CHARS,
    Selector,
    best_match,
    body_score,
    context_bonus,
    normalize,
)
from .resolve import (
    DEFAULT_MARGIN,
    DEFAULT_THRESHOLD,
    Anchor,
    Resolution,
    build_anchors,
    resolve,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # hashing
    "normalize_body",
    "body_hash",
    # markers
    "Marker",
    "find_markers",
    "strip_markers",
    # segmentation
    "segment_blank_line",
    "segment_commonmark",
    # parse
    "Block",
    "parse_document",
    # lint
    "Finding",
    "lint_document",
    "lint_diff",
    "sort_findings",
    "has_errors",
    # quote / §9 recovery
    "Selector",
    "normalize",
    "body_score",
    "context_bonus",
    "best_match",
    "CONTEXT_CHARS",
    # resolve / §9.1 ladder
    "Anchor",
    "Resolution",
    "build_anchors",
    "resolve",
    "DEFAULT_THRESHOLD",
    "DEFAULT_MARGIN",
]
