"""The markstay attachment resolver (SPEC.md §9.1).

The marker-survival eval answered "does the *id token* survive an LLM edit?".
This answers the harder question the spec actually rests on: after an edit that
moves, splits, merges, edits, or deletes blocks, can a tool re-attach each
original id to the *correct* block, and does it refuse to guess when it cannot?

The resolution model is the three-field split from SPEC.md §2.1:

    id     stable identity (answers *which block*)
    hash   drift detection (answers *did the body change*)
    quote  recovery evidence (answers *where did it go* when the marker is lost)

The resolver applies them as a priority ladder, strongest evidence first:

    1. MARKER  the id's marker is still present in the edited doc -> trust it.
    2. HASH    no marker, but exactly one block's body hash equals the stored
               hash -> the content survived verbatim, just lost its marker.
    3. QUOTE   no marker and no hash hit -> fuzzy-recover via the quote selector,
               but only commit to a *clear* winner (score over threshold AND a
               margin over the runner-up). Otherwise report DETACHED.

DETACHED is a first-class, correct outcome: the spec says a marker that cannot
be confidently placed must be surfaced as outdated, never silently reattached to
a nearby block.

Marker parsing and hashing are reused from the linter core, not reimplemented:
``parse_document``, ``body_hash``, ``normalize_body``.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import lint as L
from .quote import Selector, best_match

# Default thresholds for the QUOTE tier. A recovery is committed only when the
# best candidate clears `threshold` AND beats the runner-up by `margin`.
DEFAULT_THRESHOLD = 0.5
DEFAULT_MARGIN = 0.05


@dataclass
class Anchor:
    """Everything stored about one original block at annotation time. In a real
    markstay tool this is what the marker plus a side index would carry."""
    id: str
    hash: str            # full sha256 of the normalized body
    selector: Selector   # quote + prefix/suffix recovery evidence


@dataclass
class Resolution:
    id: str
    method: str          # 'marker' | 'hash' | 'quote' | 'detached'
    target: int | None   # content-block index in the after-doc, or None
    score: float         # confidence in [0, 1] (1.0 for marker/hash)


def build_anchors(before_md: str, mode: str = "blank-line") -> list[Anchor]:
    """Extract anchors from an annotated baseline document. Each non-orphan block
    with a well-formed marker contributes one anchor carrying the block's hash
    and a quote selector built from the block and its neighbours.

    ``mode`` selects the block segmenter (SPEC.md §5): 'blank-line' (default) or
    'commonmark' (§5.2, whole loose lists / blank-line fences). It MUST match the
    mode passed to ``resolve``."""
    blocks = [b for b in L.parse_document(before_md, mode=mode) if b.index >= 0]
    anchors: list[Anchor] = []
    for i, b in enumerate(blocks):
        prev_text = blocks[i - 1].content if i > 0 else ""
        next_text = blocks[i + 1].content if i + 1 < len(blocks) else ""
        sel = Selector(quote=b.content, prefix=prev_text, suffix=next_text)
        for mk in b.markers:
            if mk.id and not mk.malformed:
                anchors.append(Anchor(
                    id=mk.id,
                    hash=L.body_hash(b.content),
                    selector=sel,
                ))
    return anchors


def resolve(
    anchors: list[Anchor],
    after_md: str,
    threshold: float = DEFAULT_THRESHOLD,
    margin: float = DEFAULT_MARGIN,
    mode: str = "blank-line",
) -> dict[str, Resolution]:
    """Resolve every anchor id against the edited document via the evidence
    ladder. Returns id -> Resolution. ``mode`` selects the block segmenter and
    MUST match the mode ``build_anchors`` used (SPEC.md §5)."""
    after_blocks = [b for b in L.parse_document(after_md, mode=mode) if b.index >= 0]
    bodies = [b.content for b in after_blocks]

    # Tier 1 lookup: ids whose marker is still attached, mapped to block index.
    surviving: dict[str, int] = {}
    for idx, b in enumerate(after_blocks):
        for mk in b.markers:
            if mk.id and not mk.malformed:
                surviving.setdefault(mk.id, idx)

    # Tier 2 lookup: full-body hash -> block indices (list, to detect ambiguity).
    hash_to_idx: dict[str, list[int]] = {}
    for idx, body in enumerate(bodies):
        hash_to_idx.setdefault(L.body_hash(body), []).append(idx)

    out: dict[str, Resolution] = {}
    for a in anchors:
        # Tier 1: marker survived.
        if a.id in surviving:
            out[a.id] = Resolution(a.id, "marker", surviving[a.id], 1.0)
            continue
        # Tier 2: body hash uniquely identifies a surviving block.
        hits = hash_to_idx.get(a.hash, [])
        if len(hits) == 1:
            out[a.id] = Resolution(a.id, "hash", hits[0], 1.0)
            continue
        # Tier 3: quote recovery, committed only on a clear winner.
        idx, score, runner = best_match(a.selector, bodies)
        if idx >= 0 and score >= threshold and (score - runner) >= margin:
            out[a.id] = Resolution(a.id, "quote", idx, score)
        else:
            out[a.id] = Resolution(a.id, "detached", None, score)
    return out
