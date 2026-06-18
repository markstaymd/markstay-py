"""Quote / selector recovery (SPEC.md §9).

When a markstay marker detaches (the AI-regeneration failure mode: the agent
rewrites the document and drops the ``<!-- stay:... -->`` comment), the id has to
be re-found from *evidence about the text*, not from the marker. The spec's
recovery evidence is a W3C ``TextQuoteSelector``-style triple:

    quote   the block's own text (the exact selector)
    prefix  a little context immediately before the block
    suffix  a little context immediately after the block

This module scores how well a stored selector matches a candidate block in the
edited document. It is deliberately dependency-free (stdlib ``difflib``) so the
core has no install step.

Design notes
------------
* The dominant signal is body similarity (``difflib.SequenceMatcher.ratio`` over
  normalized text). It degrades gracefully: a small in-place paraphrase keeps a
  high ratio, a split keeps a partial ratio on the surviving half, an unrelated
  block scores near zero. That graded behaviour is exactly what lets a threshold
  sweep expose the precision/recall trade-off of the resolution model.
* prefix/suffix are a *tiebreaker*, not a primary key. Two structurally
  identical blocks (e.g. repeated boilerplate) are separated by which one's
  neighbours match the stored context. This is the W3C rationale for carrying
  context at all.
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass
from difflib import SequenceMatcher

# How much neighbour context to keep on each side. Short enough to stay cheap,
# long enough to disambiguate repeated blocks. SPEC.md §9 pins this value; the
# spec records the number the attachment eval measured.
CONTEXT_CHARS = 48

# §9 matching normalization is pinned to ASCII for exact cross-implementation
# agreement (SPEC.md §9, SPEC_DECISIONS.md): lowercase only ASCII A-Z and collapse
# only ASCII whitespace. Non-ASCII characters pass through unchanged and identical
# in every implementation. Recovery is evidence, not identity (§2.1), so an
# ASCII-only fold is sufficient and avoids the Unicode casefold / `\s` divergences
# between languages.
_ASCII_WS = " \t\n\r\f\v"
_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def normalize(text: str) -> str:
    """Lowercase ASCII letters and collapse ASCII whitespace runs to a single
    space, then trim (SPEC.md §9). Capitalization and reflowed line breaks (very
    common after an LLM edit) must not register as differences. ASCII-only so a
    second implementation reproduces it exactly without Unicode case data."""
    return re.sub(r"[ \t\n\r\f\v]+", " ", text.strip(_ASCII_WS)).translate(_ASCII_LOWER)


@dataclass
class Selector:
    """Recovery evidence stored for one block at annotation time."""
    quote: str            # the block body (the exact selector)
    prefix: str = ""      # trailing context of the previous block
    suffix: str = ""      # leading context of the next block

    @property
    def nquote(self) -> str:
        return normalize(self.quote)


def _ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def body_score(sel: Selector, candidate: str) -> float:
    """Similarity of a stored selector's quote to a candidate block body, in
    [0, 1]. Exact containment (the candidate is verbatim inside the quote or vice
    versa, the split / merge case) floors the score at the length ratio of the
    shorter to the longer, so a surviving half of a split paragraph cannot score
    arbitrarily low just because half its text went elsewhere."""
    q, c = sel.nquote, normalize(candidate)
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    base = _ratio(q, c)
    short, long = (q, c) if len(q) <= len(c) else (c, q)
    if short and short in long:
        base = max(base, len(short) / len(long))
    return base


def context_bonus(sel: Selector, prev_text: str, next_text: str) -> float:
    """Small additive bonus in [0, ~0.1] when the candidate's neighbours match
    the stored prefix/suffix. Used only to break near-ties between candidates
    that score equally on body."""
    bonus = 0.0
    if sel.prefix:
        bonus += 0.05 * _ratio(normalize(sel.prefix), normalize(prev_text[-CONTEXT_CHARS:]))
    if sel.suffix:
        bonus += 0.05 * _ratio(normalize(sel.suffix), normalize(next_text[:CONTEXT_CHARS]))
    return bonus


def best_match(sel: Selector, candidates: list[str]) -> tuple[int, float, float]:
    """Rank candidate block bodies against a selector.

    Returns (best_index, best_score, runner_up_score). The runner-up is returned
    so the resolver can require a margin: a confident recovery needs not just a
    high score but a *clear winner*, which is how "surface, don't guess" is
    enforced for genuinely ambiguous re-attachment.
    """
    scored = []
    for i, c in enumerate(candidates):
        s = body_score(sel, c)
        prev_text = candidates[i - 1] if i > 0 else ""
        next_text = candidates[i + 1] if i + 1 < len(candidates) else ""
        scored.append((s + context_bonus(sel, prev_text, next_text), i))
    if not scored:
        return -1, 0.0, 0.0
    scored.sort(reverse=True)
    best_score, best_index = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    # Clamp the context bonus back out of the reported score's ceiling at 1.0.
    return best_index, min(best_score, 1.0), min(runner_up, 1.0)
