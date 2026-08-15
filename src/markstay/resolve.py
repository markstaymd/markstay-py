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
    hash: str  # full sha256 of the normalized body
    selector: Selector  # quote + prefix/suffix recovery evidence


@dataclass
class Resolution:
    id: str
    method: str  # 'marker' | 'hash' | 'quote' | 'detached'
    target: int | None  # content-block index in the after-doc, or None
    score: float  # confidence in [0, 1] (1.0 for marker/hash)


@dataclass
class ChildAnchor:
    """Stored evidence for an experimental direct list-item stay."""

    id: str
    hash: str
    selector: Selector
    ordinal: int
    parent: Anchor | None
    parent_hash: str
    sibling_hash_count: int = 1
    document_hash_count: int = 1


@dataclass
class ChildResolution:
    id: str
    method: str  # marker | parent-hash | hash | document-hash | quote | detached
    target: int | None  # global ChildBlock.index in the edited document
    score: float
    parent_target: int | None = None


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
                anchors.append(
                    Anchor(
                        id=mk.id,
                        hash=L.body_hash(b.content),
                        selector=sel,
                    )
                )
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


def build_child_anchors(before_md: str, mode: str = "blank-line") -> list[ChildAnchor]:
    """Extract anchors for opt-in direct list-item identity.

    Child context is sibling-scoped. A parent anchor is recorded when the
    container carries a block-level stay; otherwise recovery is intentionally
    limited to the surviving marker and document-scoped exact child hash.
    """

    blocks = [
        b
        for b in L.parse_document(before_md, mode=mode, child_blocks=True)
        if b.index >= 0
    ]
    anchors: list[ChildAnchor] = []
    document_hash_counts: dict[str, int] = {}
    for block in blocks:
        for child in block.children:
            digest = L.body_hash(child.content)
            document_hash_counts[digest] = document_hash_counts.get(digest, 0) + 1
    for bi, block in enumerate(blocks):
        parent_marker = next(
            (
                mk
                for mk in block.markers
                if mk.id and not mk.malformed and mk.subhash is None
            ),
            None,
        )
        parent = None
        if parent_marker is not None:
            parent = Anchor(
                id=parent_marker.id,
                hash=L.body_hash(block.content),
                selector=Selector(
                    quote=block.content,
                    prefix=blocks[bi - 1].content if bi > 0 else "",
                    suffix=blocks[bi + 1].content if bi + 1 < len(blocks) else "",
                ),
            )
        for ci, child in enumerate(block.children):
            child_hash = L.body_hash(child.content)
            selector = Selector(
                quote=child.content,
                prefix=block.children[ci - 1].content if ci > 0 else "",
                suffix=(
                    block.children[ci + 1].content
                    if ci + 1 < len(block.children)
                    else ""
                ),
            )
            for mk in child.markers:
                if mk.id and not mk.malformed:
                    anchors.append(
                        ChildAnchor(
                            id=mk.id,
                            hash=child_hash,
                            selector=selector,
                            ordinal=child.ordinal,
                            parent=parent,
                            parent_hash=L.body_hash(block.content),
                            sibling_hash_count=sum(
                                1
                                for candidate in block.children
                                if L.body_hash(candidate.content) == child_hash
                            ),
                            document_hash_count=document_hash_counts[child_hash],
                        )
                    )
    return anchors


def _resolve_parents(
    anchors: list[ChildAnchor],
    blocks: list[L.Block],
    after_md: str,
    threshold: float = DEFAULT_THRESHOLD,
    margin: float = DEFAULT_MARGIN,
    mode: str = "blank-line",
) -> dict[str, tuple[int, str]]:
    """Resolve every distinct parent stay at once, assigning exclusively.

    Resolving each parent in isolation lets two anchors claim the same block: a
    deleted list whose near-duplicate sibling survives scores a large quote
    margin against it, because the only rival that would have contested the
    match is the one the edit removed. Running the tiers as global passes and
    consuming a block when a stronger tier claims it removes that whole class of
    wrong-parent recovery, and with it the child cascade underneath.
    """

    reps: dict[str, Anchor] = {}
    for anchor in anchors:
        if anchor.parent is not None:
            reps.setdefault(anchor.parent.id, anchor.parent)

    out: dict[str, tuple[int, str]] = {}
    claimed: set[int] = set()

    for pid in reps:
        for idx, block in enumerate(blocks):
            if any(mk.id == pid and not mk.malformed for mk in block.markers):
                out[pid] = (idx, "marker")
                claimed.add(idx)
                break

    for pid, parent in reps.items():
        if pid in out:
            continue
        hits = [
            idx
            for idx, block in enumerate(blocks)
            if L.body_hash(block.content) == parent.hash and idx not in claimed
        ]
        if len(hits) == 1:
            out[pid] = (hits[0], "hash")
            claimed.add(hits[0])

    # Quote claims run last and in descending score order, so the best-supported
    # parent picks from the unclaimed blocks first rather than whichever anchor
    # happened to be enumerated first.
    pending: list[tuple[float, str, int]] = []
    for pid, parent in reps.items():
        if pid in out:
            continue
        candidates = [idx for idx in range(len(blocks)) if idx not in claimed]
        idx, score, runner = best_match(
            parent.selector, [blocks[i].content for i in candidates]
        )
        if idx >= 0 and score >= threshold and (score - runner) >= margin:
            pending.append((score, pid, candidates[idx]))
    for _, pid, target in sorted(pending, key=lambda row: -row[0]):
        if target in claimed:
            continue
        out[pid] = (target, "quote")
        claimed.add(target)
    return out


def resolve_children(
    anchors: list[ChildAnchor],
    after_md: str,
    threshold: float = DEFAULT_THRESHOLD,
    margin: float = DEFAULT_MARGIN,
    mode: str = "blank-line",
) -> dict[str, ChildResolution]:
    """Resolve child stays using the containment-scoped evidence ladder.

    The surviving child marker always wins after the parent has resolved.
    Ordinal is used only by the exact parent-hash fast path and never changes a
    quote score or its commit margin.
    """

    blocks = [
        b
        for b in L.parse_document(after_md, mode=mode, child_blocks=True)
        if b.index >= 0
    ]
    all_children = [child for block in blocks for child in block.children]
    by_marker: dict[str, L.ChildBlock] = {}
    for child in all_children:
        for mk in child.markers:
            if mk.id and not mk.malformed:
                by_marker.setdefault(mk.id, child)

    by_hash: dict[str, list[L.ChildBlock]] = {}
    for child in all_children:
        by_hash.setdefault(L.body_hash(child.content), []).append(child)

    parents = _resolve_parents(
        anchors, blocks, after_md, threshold=threshold, margin=margin, mode=mode
    )
    out: dict[str, ChildResolution] = {}
    claimed: set[int] = set()

    # Tier 1 runs ahead of the parent gate, not behind it. A surviving child
    # marker is stored identity; where the parent lives is an inference about
    # its container. Letting a failed container inference discard the marker
    # would make the commonest LLM edit shape unrecoverable: the parent's
    # block-level marker sits on its own line and is easy to drop, while the
    # child markers ride inline inside the bullet text being rewritten.
    for anchor in anchors:
        hit = by_marker.get(anchor.id)
        if hit is not None:
            out[anchor.id] = ChildResolution(
                anchor.id, "marker", hit.index, 1.0, hit.parent_index
            )
            claimed.add(hit.index)

    def _parent_target(anchor: ChildAnchor) -> int | None:
        if anchor.parent is None:
            return None
        found = parents.get(anchor.parent.id)
        return found[0] if found is not None else None

    def _gated(anchor: ChildAnchor) -> bool:
        """A child whose parent stay exists but could not be found has no
        sibling scope, so every structural tier below is unavailable to it."""
        return anchor.parent is not None and anchor.parent.id not in parents

    for anchor in anchors:
        if anchor.id in out or _gated(anchor):
            continue
        parent_target = _parent_target(anchor)
        parent_block = (
            blocks[parent_target]
            if parent_target is not None and 0 <= parent_target < len(blocks)
            else None
        )
        if parent_block is not None:
            if L.body_hash(parent_block.content) == anchor.parent_hash:
                ordinal = anchor.ordinal - 1
                if 0 <= ordinal < len(parent_block.children):
                    candidate = parent_block.children[ordinal]
                    if not candidate.markers and candidate.index not in claimed:
                        out[anchor.id] = ChildResolution(
                            anchor.id,
                            "parent-hash",
                            candidate.index,
                            1.0,
                            parent_target,
                        )
                        claimed.add(candidate.index)
                        continue

            sibling_hits = [
                child
                for child in parent_block.children
                if L.body_hash(child.content) == anchor.hash
                and child.index not in claimed
            ]
            if len(sibling_hits) == 1 and anchor.sibling_hash_count == 1:
                out[anchor.id] = ChildResolution(
                    anchor.id, "hash", sibling_hits[0].index, 1.0, parent_target
                )
                claimed.add(sibling_hits[0].index)
                continue

        document_hits = [
            child
            for child in by_hash.get(anchor.hash, [])
            if child.index not in claimed
        ]
        if len(document_hits) == 1 and anchor.document_hash_count == 1:
            hit = document_hits[0]
            out[anchor.id] = ChildResolution(
                anchor.id, "document-hash", hit.index, 1.0, hit.parent_index
            )
            claimed.add(hit.index)

    # Quote scoring runs only over what no stronger tier took.
    for anchor in anchors:
        if anchor.id in out or _gated(anchor):
            continue
        parent_target = _parent_target(anchor)
        parent_block = (
            blocks[parent_target]
            if parent_target is not None and 0 <= parent_target < len(blocks)
            else None
        )
        if parent_block is None or anchor.sibling_hash_count != 1:
            out[anchor.id] = ChildResolution(anchor.id, "detached", None, 0.0, None)
            continue
        candidates = [
            child for child in parent_block.children if child.index not in claimed
        ]
        idx, score, runner = best_match(
            anchor.selector, [child.content for child in candidates]
        )
        if idx >= 0 and score >= threshold and (score - runner) >= margin:
            hit = candidates[idx]
            out[anchor.id] = ChildResolution(
                anchor.id, "quote", hit.index, score, parent_target
            )
            claimed.add(hit.index)
        else:
            out[anchor.id] = ChildResolution(
                anchor.id, "detached", None, score, parent_target
            )

    for anchor in anchors:
        out.setdefault(
            anchor.id, ChildResolution(anchor.id, "detached", None, 0.0, None)
        )
    return out
