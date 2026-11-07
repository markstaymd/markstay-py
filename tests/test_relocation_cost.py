"""Work bound for SPEC.md §11's relocation scan (v1.11).

The shape: N kept ids, each block rewritten in a vocabulary the old text does
not share (so clause 1 holds for every id and the window is always scanned),
and RUN unmarked blocks of the old vocabulary and length typed directly above
each marker (so every window holds RUN free blocks, none scoring 0.8). A
document-wide scan would score every drifted id against all N * RUN free
blocks; the window scores each against its own RUN. The documents come from a
fixed LCG, the same in all four cores' tests, so the shape is identical.

Runs under pytest. The canonical twin is linter/test_relocation_cost.py.
"""

import importlib
import time

L = importlib.import_module("markstay.lint")

WORDS = (
    "apple orchard fence river stone market window lantern harbor meadow "
    "copper silver ledger anchor thistle bramble cedar willow quarry summit "
    "garden basket candle mirror saddle kettle needle pebble ribbon timber"
).split()
OTHER = (
    "zebra quokka yak vulture jaguar ibex hyena gnu ferret dingo "
    "koala lemur mink narwhal ocelot puma quail raven sloth tapir"
).split()
RUN = 2


def relocation_docs(n: int, run: int = RUN) -> tuple[str, str]:
    """Before and after documents for N kept ids, RUN free blocks per window."""
    x = n

    def para(words):
        nonlocal x
        out = []
        for _ in range(30):
            x = (x * 1103515245 + 12345) & 0x7FFFFFFF
            out.append(words[(x >> 16) % len(words)])
        s = " ".join(out)
        return s[0].upper() + s[1:] + "."

    ids = [f"i{k:05d}" for k in range(n)]
    before = "".join(f"{para(WORDS)}\n<!-- stay:{i} -->\n\n" for i in ids)
    after = "".join(
        "".join(f"{para(WORDS)}\n\n" for _ in range(run))
        + f"{para(OTHER)}\n<!-- stay:{i} -->\n\n"
        for i in ids
    )
    return before, after


def scored_pairs(n: int) -> tuple[int, list]:
    """Body-score calls made by one lint_diff over the shape, and its findings."""
    before, after = relocation_docs(n)
    calls = 0
    real = L.body_score_norm

    def counted(q, c):
        nonlocal calls
        calls += 1
        return real(q, c)

    L.body_score_norm = counted
    try:
        findings = L.lint_diff(before, after)
    finally:
        L.body_score_norm = real
    return calls, findings


def test_window_scan_is_linear_in_the_free_blocks():
    for n in (100, 200, 400):
        calls, findings = scored_pairs(n)
        assert [f.code for f in findings] == ["HASH_DRIFT"] * n, n
        # One clause-1 score per id, then one per free block in its window.
        assert calls == n * (1 + RUN), (n, calls)


def test_window_scan_wall_time_roughly_doubles():
    def best(n):
        before, after = relocation_docs(n)
        out = float("inf")
        for _ in range(3):
            t = time.perf_counter()
            L.lint_diff(before, after)
            out = min(out, time.perf_counter() - t)
        return out

    small, large = best(200), best(400)
    # Linear doubles, quadratic quadruples; 3.0 leaves room for a loaded machine.
    assert large / small < 3.0, (small, large)

