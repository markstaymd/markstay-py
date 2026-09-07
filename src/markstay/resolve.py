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

from dataclasses import dataclass, field

from . import lint as L
from .quote import Candidate, Selector, rank_candidates, window_prefix, window_suffix

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
    reason: str | None = None
    candidates: list[Candidate] = field(default_factory=list)
    runner_up_score: float = 0.0
    proposed_target: int | None = None
    contested_with: list[str] = field(default_factory=list)
    proposal_provenance: str | None = None


@dataclass
class ChildAnchor:
    """Stored evidence for a direct list-item or table-row stay."""

    id: str
    hash: str
    selector: Selector
    ordinal: int
    parent: Anchor | None
    parent_hash: str
    sibling_hash_count: int = 1
    document_hash_count: int = 1
    kind: str = "list"


@dataclass
class ChildResolution:
    id: str
    method: str  # marker | parent-hash | hash | document-hash | quote | detached
    target: int | None  # global ChildBlock.index in the edited document
    score: float
    parent_target: int | None = None
    reason: str | None = None
    candidates: list[Candidate] = field(default_factory=list)
    runner_up_score: float = 0.0
    blocked_by: Resolution | None = None
    proposed_target: int | None = None
    contested_with: list[str] = field(default_factory=list)
    proposal_provenance: str | None = None


def _ambiguous_candidates(ranked: list[Candidate], margin: float) -> list[Candidate]:
    """Keep the contenders that explain a failed margin, not lower-ranked noise."""
    if not ranked:
        return []
    best = ranked[0].score
    return [candidate for candidate in ranked if best - candidate.score < margin]


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
        # SPEC.md §9: the stored prefix/suffix carry up to 48 characters of the
        # neighbour on each side. Storing whole blocks caps the achievable ratio
        # near 2*48/(len+48), because the candidate side is windowed at match time.
        sel = Selector(
            quote=b.content,
            prefix=window_prefix(prev_text),
            suffix=window_suffix(next_text),
        )
        for mk in b.markers:
            if mk.id and not mk.malformed and not mk.has_subhash:
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
            if mk.id and not mk.malformed and not mk.has_subhash:
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
        ranked = rank_candidates(a.selector, bodies)
        idx = ranked[0].target if ranked else -1
        score = ranked[0].score if ranked else 0.0
        runner = ranked[1].score if len(ranked) > 1 else 0.0
        if idx >= 0 and score >= threshold and (score - runner) >= margin:
            out[a.id] = Resolution(a.id, "quote", idx, score, runner_up_score=runner)
        elif idx >= 0 and score >= threshold:
            out[a.id] = Resolution(
                a.id,
                "detached",
                None,
                score,
                reason="ambiguous",
                candidates=_ambiguous_candidates(ranked, margin),
                runner_up_score=runner,
            )
        else:
            out[a.id] = Resolution(
                a.id,
                "detached",
                None,
                score,
                reason="unmatched",
                runner_up_score=runner,
            )
    return out


def build_child_anchors(before_md: str, mode: str = "blank-line") -> list[ChildAnchor]:
    """Extract anchors for direct list-item and table-row identity (§5.5/§5.6).

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
                if mk.id and not mk.malformed and not mk.has_subhash
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
                    prefix=window_prefix(blocks[bi - 1].content) if bi > 0 else "",
                    suffix=(
                        window_suffix(blocks[bi + 1].content)
                        if bi + 1 < len(blocks)
                        else ""
                    ),
                ),
            )
        for child in block.children:
            siblings = [
                candidate
                for candidate in block.children
                if candidate.kind == child.kind
            ]
            ci = siblings.index(child)
            child_hash = L.body_hash(child.content)
            selector = Selector(
                quote=child.content,
                prefix=(window_prefix(siblings[ci - 1].content) if ci > 0 else ""),
                suffix=(
                    window_suffix(siblings[ci + 1].content)
                    if ci + 1 < len(siblings)
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
                                for candidate in siblings
                                if L.body_hash(candidate.content) == child_hash
                            ),
                            document_hash_count=document_hash_counts[child_hash],
                            kind=child.kind,
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
) -> dict[str, Resolution]:
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

    out: dict[str, Resolution] = {}
    claimed: set[int] = set()

    for pid in reps:
        for idx, block in enumerate(blocks):
            if any(
                mk.id == pid and not mk.malformed and not mk.has_subhash
                for mk in block.markers
            ):
                out[pid] = Resolution(pid, "marker", idx, 1.0)
                claimed.add(idx)
                break

    # Every stay proposes against the same tier-start snapshot. A block reached
    # by two stays at one tier goes to neither (§9.2), so hash assignment cannot
    # depend on parent enumeration order.
    hash_proposals: dict[str, int] = {}
    for pid, parent in reps.items():
        if pid in out:
            continue
        hits = [
            idx
            for idx, block in enumerate(blocks)
            if L.body_hash(block.content) == parent.hash and idx not in claimed
        ]
        if len(hits) == 1:
            hash_proposals[pid] = hits[0]
    hash_claimants: dict[int, list[str]] = {}
    for pid, target in hash_proposals.items():
        hash_claimants.setdefault(target, []).append(pid)
    hash_contests: dict[str, tuple[int, list[str]]] = {}
    for pid, target in hash_proposals.items():
        claimants = hash_claimants[target]
        if len(claimants) == 1:
            out[pid] = Resolution(pid, "hash", target, 1.0)
            claimed.add(target)
        else:
            hash_contests[pid] = (
                target,
                [other for other in claimants if other != pid],
            )

    # Quote scoring also uses one snapshot. The former score-priority arbitration
    # silently awarded a contested block to one stay; §9.2 requires neither.
    quote_proposals: dict[str, Candidate] = {}
    quote_rankings: dict[str, list[Candidate]] = {}
    quote_failed: dict[str, Resolution] = {}
    for pid, parent in reps.items():
        if pid in out:
            continue
        candidates = [idx for idx in range(len(blocks)) if idx not in claimed]
        ranked = rank_candidates(
            parent.selector,
            [blocks[i].content for i in candidates],
            targets=candidates,
            provenance="parent-snapshot",
        )
        quote_rankings[pid] = ranked
        score = ranked[0].score if ranked else 0.0
        runner = ranked[1].score if len(ranked) > 1 else 0.0
        if ranked and score >= threshold and score - runner >= margin:
            quote_proposals[pid] = ranked[0]
        elif ranked and score >= threshold:
            quote_failed[pid] = Resolution(
                pid,
                "detached",
                None,
                score,
                reason="ambiguous",
                candidates=_ambiguous_candidates(ranked, margin),
                runner_up_score=runner,
            )
        else:
            quote_failed[pid] = Resolution(
                pid,
                "detached",
                None,
                score,
                reason="unmatched",
                runner_up_score=runner,
            )
    quote_claimants: dict[int, list[str]] = {}
    quote_contests: dict[str, tuple[Candidate, list[str], float]] = {}
    for pid, candidate in quote_proposals.items():
        quote_claimants.setdefault(candidate.target, []).append(pid)
    for pid, candidate in quote_proposals.items():
        ranked = quote_rankings[pid]
        runner = ranked[1].score if len(ranked) > 1 else 0.0
        claimants = quote_claimants[candidate.target]
        if len(claimants) == 1:
            out[pid] = Resolution(
                pid,
                "quote",
                candidate.target,
                candidate.score,
                runner_up_score=runner,
            )
            claimed.add(candidate.target)
        else:
            quote_contests[pid] = (
                candidate,
                [other for other in claimants if other != pid],
                runner,
            )

    # A contest does not stop the ladder: §9.2 sends the stay to weaker tiers.
    # If no weaker tier attaches it, retain the strongest contest as the most
    # useful explanation of the final detachment.
    for pid in reps:
        if pid in out:
            continue
        if pid in hash_contests:
            target, contested_with = hash_contests[pid]
            out[pid] = Resolution(
                pid,
                "detached",
                None,
                1.0,
                reason="contested",
                proposed_target=target,
                contested_with=contested_with,
                proposal_provenance="parent-hash-tier-snapshot",
            )
        elif pid in quote_contests:
            candidate, contested_with, runner = quote_contests[pid]
            out[pid] = Resolution(
                pid,
                "detached",
                None,
                candidate.score,
                reason="contested",
                runner_up_score=runner,
                proposed_target=candidate.target,
                contested_with=contested_with,
                proposal_provenance=candidate.provenance,
            )
        else:
            out[pid] = quote_failed[pid]
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

    # SPEC.md §5.5: a `subhash` marker that no direct child owns addresses
    # nothing, and "nobody's stay" is not a licence to recover the id from
    # weaker evidence. The marker is still in the document, so a tool honouring
    # the reader rule reports it unaddressed (`CHILD_UNADDRESSED`) while a
    # resolver walking the ladder would bind the same id to a *different* item:
    # the two halves of one implementation disagreeing about one document, which
    # is the §13 failure the rule exists to prevent. Reaches here from an item
    # nested inside another item, and from a segmenter that emitted no children
    # for the block at all. Both fail closed.
    # Narrower than "a `subhash` marker sits block-level somewhere": an id whose
    # marker is *also* owned by a direct child still has a marker doing its job,
    # and a stray nested copy of it is a §7 duplicate for the linter to report
    # (`DUPLICATE_ID`), not a reason to lose an anchor that never moved.
    unaddressed: set[str] = {
        mk.id
        for block in blocks
        for mk in block.markers
        if mk.has_subhash and mk.id and not mk.malformed
    } - set(by_marker)

    parents = _resolve_parents(
        anchors, blocks, after_md, threshold=threshold, margin=margin, mode=mode
    )
    out: dict[str, ChildResolution] = {}
    claimed: set[int] = set()

    for anchor in anchors:
        if anchor.id in unaddressed:
            out[anchor.id] = ChildResolution(
                anchor.id,
                "detached",
                None,
                0.0,
                None,
                reason="unaddressed",
            )

    # Tier 1 runs ahead of the parent gate, not behind it. A surviving child
    # marker is stored identity; where the parent lives is an inference about
    # its container. Letting a failed container inference discard the marker
    # would make the commonest LLM edit shape unrecoverable: the parent's
    # block-level marker sits on its own line and is easy to drop, while the
    # child markers ride inline inside the bullet text being rewritten.
    for anchor in anchors:
        if anchor.id in out:
            continue
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
        return found.target if found is not None else None

    def _gated(anchor: ChildAnchor) -> bool:
        """A child whose parent stay exists but could not be found has no
        sibling scope, so every structural tier below is unavailable to it."""
        return anchor.parent is not None and (
            anchor.parent.id not in parents or parents[anchor.parent.id].target is None
        )

    def _parent_block_of(anchor: ChildAnchor) -> tuple[int | None, L.Block | None]:
        target = _parent_target(anchor)
        block = (
            blocks[target] if target is not None and 0 <= target < len(blocks) else None
        )
        return target, block

    contest_history: dict[str, tuple[int, int | None, float, float, list[str], str]] = (
        {}
    )

    def _commit(
        tier: str,
        proposals: dict[str, tuple[int, int | None, float]],
        provenance: str,
        runners: dict[str, float] | None = None,
    ) -> None:
        """Commit a tier's proposals, dropping any candidate two stays reached.

        SPEC.md §9.2: assignment is exclusive, and a contested candidate is
        taken by neither stay. Committing as each anchor is visited instead
        would hand the item to whichever one this loop happened to see first,
        so two implementations agreeing about every hash would still disagree
        about the document.
        """
        claimants: dict[int, list[str]] = {}
        for anchor_id, (candidate, _, _) in proposals.items():
            claimants.setdefault(candidate, []).append(anchor_id)
        for anchor_id, (candidate, parent_target, score) in proposals.items():
            if len(claimants[candidate]) != 1:
                contest_history.setdefault(
                    anchor_id,
                    (
                        candidate,
                        parent_target,
                        score,
                        runners.get(anchor_id, 0.0) if runners else 0.0,
                        [other for other in claimants[candidate] if other != anchor_id],
                        provenance,
                    ),
                )
                continue
            out[anchor_id] = ChildResolution(
                anchor_id,
                tier,
                candidate,
                score,
                parent_target,
                runner_up_score=(runners.get(anchor_id, 0.0) if runners else 0.0),
            )
            claimed.add(candidate)

    # Tier 2, the container is unchanged so markerless children map by ordinal.
    ordinal_proposals: dict[str, tuple[int, int | None, float]] = {}
    for anchor in anchors:
        if anchor.id in out or _gated(anchor):
            continue
        parent_target, parent_block = _parent_block_of(anchor)
        if parent_block is None:
            continue
        if L.body_hash(parent_block.content) != anchor.parent_hash:
            continue
        siblings = [
            child for child in parent_block.children if child.kind == anchor.kind
        ]
        ordinal = anchor.ordinal - 1
        if not 0 <= ordinal < len(siblings):
            continue
        candidate = siblings[ordinal]
        if candidate.markers or candidate.index in claimed:
            continue
        ordinal_proposals[anchor.id] = (candidate.index, parent_target, 1.0)
    _commit(
        "parent-hash",
        ordinal_proposals,
        "child-parent-hash-tier-snapshot",
    )

    # Tier 3, the child hash against its own siblings.
    sibling_proposals: dict[str, tuple[int, int | None, float]] = {}
    for anchor in anchors:
        if anchor.id in out or _gated(anchor):
            continue
        parent_target, parent_block = _parent_block_of(anchor)
        if parent_block is None or anchor.sibling_hash_count != 1:
            continue
        sibling_hits = [
            child
            for child in parent_block.children
            if child.kind == anchor.kind
            and L.body_hash(child.content) == anchor.hash
            and child.index not in claimed
        ]
        if len(sibling_hits) == 1:
            sibling_proposals[anchor.id] = (sibling_hits[0].index, parent_target, 1.0)
    _commit("hash", sibling_proposals, "child-sibling-hash-tier-snapshot")

    # Tier 4, the child hash anywhere in the document (§7's move guarantee).
    document_proposals: dict[str, tuple[int, int | None, float]] = {}
    for anchor in anchors:
        if anchor.id in out or _gated(anchor):
            continue
        if anchor.document_hash_count != 1:
            continue
        document_hits = [
            child
            for child in by_hash.get(anchor.hash, [])
            if child.index not in claimed
        ]
        if len(document_hits) == 1:
            hit = document_hits[0]
            document_proposals[anchor.id] = (hit.index, hit.parent_index, 1.0)
    _commit(
        "document-hash",
        document_proposals,
        "child-document-hash-tier-snapshot",
    )

    # Tier 5, quote scoring over siblings, and only over what no stronger tier
    # took. Proposed then committed like the tiers above, so a candidate two
    # stays both score onto goes to neither of them (§9.2).
    quote_proposals: dict[str, tuple[int, int | None, float]] = {}
    quote_failed: dict[str, ChildResolution] = {}
    quote_runners: dict[str, float] = {}
    for anchor in anchors:
        if anchor.id in out or _gated(anchor):
            continue
        parent_target, parent_block = _parent_block_of(anchor)
        if parent_block is None:
            quote_failed[anchor.id] = ChildResolution(
                anchor.id,
                "detached",
                None,
                0.0,
                parent_target,
                reason="unscored",
            )
            continue
        candidates = [
            child
            for child in parent_block.children
            if child.kind == anchor.kind and child.index not in claimed
        ]
        ranked = rank_candidates(
            anchor.selector,
            [child.content for child in candidates],
            targets=[child.index for child in candidates],
            provenance="child-tier-snapshot",
        )
        score = ranked[0].score if ranked else 0.0
        runner = ranked[1].score if len(ranked) > 1 else 0.0
        quote_runners[anchor.id] = runner
        if ranked and score >= threshold and (score - runner) >= margin:
            quote_proposals[anchor.id] = (
                ranked[0].target,
                parent_target,
                score,
            )
        elif ranked and score >= threshold:
            quote_failed[anchor.id] = ChildResolution(
                anchor.id,
                "detached",
                None,
                score,
                parent_target,
                reason="ambiguous",
                candidates=_ambiguous_candidates(ranked, margin),
                runner_up_score=runner,
            )
        else:
            quote_failed[anchor.id] = ChildResolution(
                anchor.id,
                "detached",
                None,
                score,
                parent_target,
                reason="unmatched",
                runner_up_score=runner,
            )
    _commit(
        "quote",
        quote_proposals,
        "child-tier-snapshot",
        runners=quote_runners,
    )
    for anchor_id, resolution in quote_failed.items():
        if anchor_id not in contest_history:
            out.setdefault(anchor_id, resolution)
    for anchor_id, contest in contest_history.items():
        if anchor_id in out:
            continue
        (
            proposed_target,
            parent_target,
            score,
            runner,
            contested_with,
            provenance,
        ) = contest
        out[anchor_id] = ChildResolution(
            anchor_id,
            "detached",
            None,
            score,
            parent_target,
            reason="contested",
            runner_up_score=runner,
            proposed_target=proposed_target,
            contested_with=contested_with,
            proposal_provenance=provenance,
        )

    for anchor in anchors:
        if anchor.id in out:
            continue
        blocked_by = (
            parents.get(anchor.parent.id) if anchor.parent is not None else None
        )
        out[anchor.id] = ChildResolution(
            anchor.id,
            "detached",
            None,
            0.0,
            None,
            reason="unscored",
            blocked_by=blocked_by,
        )
    return out
