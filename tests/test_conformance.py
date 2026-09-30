"""Cross-implementation conformance: run the shared language-neutral corpus
(conformance/spec/ + conformance/gen/, plus the optional conformance/rows/
profile this package advertises) against this package.

The corpus is shared with the JavaScript reference (`markstay` on npm), whose
runner asserts the same vectors against the JS implementation. Together they are
the cross-impl regression sentinel: any change to either implementation that
breaks agreement fails one of them.

A `spec/` vector this reference fails is a REFERENCE BUG (the prose is
authority), not a corpus error. A `gen/` vector that fails means the reference
changed since generation.

The serialization helpers and per-category verifiers below are ported from the
umbrella's `conformance/generate.py` + `run_py.py` so a vendored clone verifies
standalone with no path to the umbrella.
"""

from __future__ import annotations

import json
from difflib import SequenceMatcher
from pathlib import Path

import pytest

import markstay as M
from markstay import lint as _lint
from markstay import quote as Q

CORPUS = Path(__file__).resolve().parent.parent / "conformance"
TOL = 1e-9


# --- canonical vector shapes (mirror generate.py) -------------------------

def marker_dict(mk) -> dict:
    return {
        "id": mk.id,
        "hash": mk.hash,
        "raw": mk.raw,
        "syntax": mk.syntax,
        "line": mk.line,
        "malformed": mk.malformed,
    }


def block_dict(b) -> dict:
    return {
        "content": b.content,
        "index": b.index,
        "ids": [mk.id for mk in b.markers],
        "line": b.line,
        "orphan": b.index == -1,
    }


def finding_dict(f, with_line: bool) -> dict:
    d = {"level": f.level, "code": f.code, "id": f.id}
    if with_line:
        d["line"] = f.line
    return d


def expect_hash(body: str) -> dict:
    return {
        "normalized": M.normalize_body(body),
        "sha256": M.body_hash(body),
        "truncations": {str(n): M.body_hash(body, n) for n in (4, 8, 12, 16)},
    }


def expect_resolve(before: str, after: str,
                   threshold: float | None = None,
                   margin: float | None = None) -> dict:
    """`threshold`/`margin` of None means the vector omitted them, so the
    package's own SPEC.md §9 defaults (0.5 / 0.05) must be what applies."""
    kw = {}
    if threshold is not None:
        kw["threshold"] = threshold
    if margin is not None:
        kw["margin"] = margin
    anchors = M.build_anchors(before)
    res = M.resolve(anchors, after, **kw)
    return {
        r.id: {"method": r.method, "target": r.target, "score": r.score}
        for r in res.values()
    }


# --- deep approx equality (mirror run_py.py) ------------------------------

def approx(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) < TOL
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(approx(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(approx(x, y) for x, y in zip(a, b))
    return a == b


def corpus_findings(findings, *, with_line):
    """Exclude only §5.4's optional parser advisory from core equality."""
    return [
        finding_dict(f, with_line=with_line)
        for f in findings
        if not (f.code == "OUTSIDE_SUBSET" and f.level == "info")
    ]


# --- per-category verifiers: (vector) -> (ok, detail) ---------------------

def v_hash(v):
    got = expect_hash(v["body"])
    want = {"normalized": v["normalized"], "sha256": v["sha256"],
            "truncations": v["truncations"]}
    return approx(got, want), f"got={got}"


def v_markers(v):
    got = [marker_dict(mk) for mk in M.find_markers(v["text"])]
    return approx(got, v["markers"]), f"got={got}"


def v_parse(v):
    got = [block_dict(b) for b in M.parse_document(v["doc"])]
    return approx(got, v["blocks"]), f"got={got}"


def v_lint(v):
    _, findings = M.lint_document(v["doc"])
    got = corpus_findings(M.sort_findings(findings), with_line=True)
    return approx(got, v["findings"]), f"got={got}"


def v_diff(v):
    findings = M.lint_diff(v["before"], v["after"])
    got = corpus_findings(M.sort_findings(findings), with_line=False)
    return approx(got, v["findings"]), f"got={got}"


def v_seqmatch(v):
    sm = SequenceMatcher(None, v["a"], v["b"], autojunk=False)
    got = {"ratio": sm.ratio(),
           "matching_blocks": [list(x) for x in sm.get_matching_blocks()]}
    want = {"ratio": v["ratio"], "matching_blocks": v["matching_blocks"]}
    return approx(got, want), f"got={got}"


def v_score(v):
    fn = v["fn"]
    if fn == "ratio":
        got = Q._ratio(v["a"], v["b"])
        return approx(got, v["score"]), f"got={got}"
    if fn == "body_score":
        got = M.body_score(M.Selector(quote=v["quote"]), v["candidate"])
        return approx(got, v["score"]), f"got={got}"
    if fn == "context_bonus":
        sel = M.Selector(quote="q", prefix=v["prefix"], suffix=v["suffix"])
        got = M.context_bonus(sel, v["prev"], v["next"])
        return approx(got, v["bonus"]), f"got={got}"
    if fn == "best_match":
        sel = M.Selector(quote=v["quote"], prefix=v["prefix"], suffix=v["suffix"])
        idx, score, runner = M.best_match(sel, v["candidates"])
        got = {"index": idx, "score": score, "runner_up": runner}
        want = {"index": v["index"], "score": v["score"], "runner_up": v["runner_up"]}
        return approx(got, want), f"got={got}"
    return False, f"unknown score fn: {fn!r}"


def v_resolve(v):
    """An ABSENT threshold/margin asserts the package's own SPEC.md §9 default,
    and each field defaults independently. A field that is present is used as
    given, including a null, which fails loudly rather than reading as omitted."""
    got = expect_resolve(
        v["before"], v["after"],
        v["threshold"] if "threshold" in v else None,
        v["margin"] if "margin" in v else None,
    )
    return approx(got, v["resolutions"]), f"got={got}"


def v_anchors(v):
    """What `build_anchors` STORES (SPEC.md §9), as opposed to what `resolve`
    decides. Resolution cannot see it: an implementation storing whole neighbour
    blocks and one storing the 48-character window resolve identically, because
    both window at match time. §9 constrains the stored field, so this category
    asserts the stored field."""
    got = [
        {"id": a.id, "hash": a.hash, "quote": a.selector.quote,
         "prefix": a.selector.prefix, "suffix": a.selector.suffix}
        for a in M.build_anchors(v["document"])
    ]
    return approx(got, v["anchors"]), f"got={got}"


def _id_factory(ids):
    it = iter(ids)
    return lambda: next(it)


def v_stamp(v):
    op = v["op"]
    o = v.get("options", {})
    if op == "stamp":
        r = M.stamp(v["input"], syntax=o.get("syntax", "html"), hash=o.get("hash", True),
                    hash_length=o.get("hashLength", M.DEFAULT_HASH_LENGTH),
                    new_id=_id_factory(v["ids"]))
        got = {"text": r.text, "minted": r.minted}
    elif op == "restamp":
        r = M.restamp(v["input"], hash_length=o.get("hashLength"),
                      add_missing=o.get("addMissing", False))
        got = {"text": r.text, "refreshed": r.refreshed}
    elif op == "repair":
        r = M.repair_duplicates(v["input"], new_id=_id_factory(v["ids"]))
        got = {"text": r.text, "renamed": r.renamed}
    else:
        return False, f"unknown stamp op: {op!r}"
    return approx(got, v["expected"]), f"got={got}"


def _cursor_random(byte_list):
    """A byte source over a fixed list, consumed in order across random(n) draws."""
    state = {"i": 0}

    def r(n):
        i = state["i"]
        out = bytes(byte_list[i:i + n])
        state["i"] = i + n
        return out

    return r


def v_mint(v):
    """Id-minting vectors (§6): a fixed byte array is the injected source, so the
    rejection loop runs deterministically and identically across all three impls."""
    kwargs = {}
    if "alphabet" in v:
        kwargs["alphabet"] = v["alphabet"]
    got = M.mint_id(v["length"], random=_cursor_random(v["bytes"]), **kwargs)
    return approx(got, v["expected"]), f"got={got}"


def v_preserve(v):
    """§11 preservation-instruction vectors. The instruction text ships as a
    constant in this package (an installed wheel has no corpus on disk), so this
    is what holds that copy byte-identical to the JS and Rust ones."""
    fn = v["fn"]
    if fn == "instruction":
        got = M.PRESERVE_INSTRUCTION
    elif fn == "return_only":
        got = M.PRESERVE_RETURN_ONLY
    elif fn == "wrap":
        got = M.preserve_wrap(v["doc"], v.get("task"))
    else:
        raise AssertionError(f"unknown preserve fn: {fn!r}")
    return approx(got, v["expected"]), f"got={got!r}"


def v_check(v):
    """Commit-shaped baseline pairing and findings, with Git already materialized."""
    entries = [M.CommitEntry(
        e["status"], e["src"], e["dst"], e.get("before"), e.get("after")
    ) for e in v["entries"]]
    result = M.check_entries(entries, v.get("scope"))
    got = {
        "pairings": [{"path": path, "baseline": baseline}
                     for path, baseline in result.pairings],
        "reports": [
            {"label": label,
             "findings": corpus_findings(M.sort_findings(findings),
                                         with_line=True)}
            for label, findings in result.reports
        ],
        "notes": result.notes,
        "hasErrors": result.has_errors,
    }
    return approx(got, v["expected"]), f"got={got}"


def v_rows(v) -> tuple[bool, str]:
    """SPEC.md §5.6 table-row identity: the optional `rows` profile.

    One category with three operation shapes, because row identity is not one
    function: recognition and attachment (`children`), the transactional write
    path with its migration probe (`stamp`), and §9.2 child recovery
    (`resolve`). Splitting them would let a runner advertise the profile while
    implementing only the half it found easy.
    """
    op = v["op"]
    if op == "children":
        mode = v.get("mode", "blank-line")
        blocks = [
            b for b in M.parse_document(v["doc"], mode=mode, child_blocks=True)
            if b.index >= 0
        ]
        got = {
            "blockIds": sorted(_lint._id_index(blocks)),
            "childIds": sorted(_lint._child_id_index(blocks)),
            "rows": [
                {"container": b.index, "ordinal": c.ordinal, "body": c.content,
                 "ids": [mk.id for mk in c.markers]}
                for b in blocks for c in b.children if c.kind == "row"
            ],
        }
    elif op == "stamp":
        ids = iter(v["ids"])
        r = M.stamp(v["doc"], child_blocks=True, new_id=lambda: next(ids))
        got = {"text": r.text, "minted": r.minted, "drifted": list(r.drifted)}
    elif op == "resolve":
        mode = v.get("mode", "blank-line")
        resolved = M.resolve_children(
            M.build_child_anchors(v["before"], mode=mode), v["after"], mode=mode
        )
        got = {k: {"method": r.method, "target": r.target}
               for k, r in sorted(resolved.items())}
    else:
        return False, f"unknown rows op: {op!r}"
    return approx(got, v["expected"]), f"got={got}"


VERIFIERS = {
    "hash": v_hash, "markers": v_markers, "parse": v_parse, "lint": v_lint,
    "diff": v_diff, "seqmatch": v_seqmatch, "score": v_score, "resolve": v_resolve,
    "stamp": v_stamp, "mint": v_mint, "preserve": v_preserve, "check": v_check,
    "anchors": v_anchors, "rows": v_rows,
}

# Optional profiles a corpus file may declare with a top-level `profile` key.
# Every full runner knows the whole set; each advertises only what it
# implements. A profile this runner has never HEARD of is a failure rather than
# a skip, so a category added to the corpus without touching the runners cannot
# pass as silence. This package implements SPEC.md §5.6, so it advertises
# `rows`; the JavaScript and Rust references decline it and run the core alone.
KNOWN_PROFILES = {"rows"}
ADVERTISED_PROFILES = {"rows"}
PROFILE_CATEGORIES = {"rows": "rows"}
CORE_VECTORS = 430
# Advertised profiles are pinned too. A count that is only reported cannot
# catch a vector going missing, because the denominator shrinks with it.
PROFILE_VECTORS = {"rows": 32}


# --- discover every vector at collection time -----------------------------

def _load_vectors():
    files = (
        sorted((CORPUS / "spec").glob("*.json"))
        + sorted((CORPUS / "gen").glob("*.json"))
        + sorted((CORPUS / "rows").glob("*.json"))
    )
    cases = []
    unknown: list[str] = []
    core = 0
    profiles: dict[str, int] = {}
    for path in files:
        data = json.loads(path.read_text())
        category = data["category"]
        tier = path.parent.name
        profile = data.get("profile")
        if profile is not None and profile not in KNOWN_PROFILES:
            unknown.append(f"{tier}/{path.name}:{profile}")
            continue
        if profile is not None and profile not in ADVERTISED_PROFILES:
            continue
        if profile is None:
            core += len(data["vectors"])
        else:
            profiles[profile] = profiles.get(profile, 0) + len(data["vectors"])
        for i, vec in enumerate(data["vectors"]):
            name = vec.get("name", str(i))
            cases.append(pytest.param(category, vec, id=f"{tier}/{category}:{name}"))
    return cases, unknown, core, profiles


VECTORS, UNKNOWN_PROFILES, CORE_COUNT, PROFILE_COUNTS = _load_vectors()


def test_corpus_present():
    assert VECTORS, f"no corpus files found under {CORPUS}/spec or {CORPUS}/gen"


def test_no_unknown_profile():
    """A new optional profile must be added to KNOWN_PROFILES, then advertised or
    deliberately declined. Silently skipping one is the failure this asserts."""
    assert not UNKNOWN_PROFILES, (
        f"corpus files declare profiles this runner has never heard of: "
        f"{UNKNOWN_PROFILES}"
    )


def test_core_vector_count():
    """Declining an optional profile is conforming (§16 keeps child segmentation
    optional). Running fewer than every core vector is not, and a count is the
    only thing that notices a whole file dropping out of collection."""
    assert CORE_COUNT == CORE_VECTORS


def test_advertised_profile_vector_count():
    """An advertised profile is run, so its inventory is pinned like the core's.
    Declining a profile stays conforming; quietly running 22 of its 23 vectors and
    reporting success is the failure this notices."""
    assert set(PROFILE_VECTORS) == ADVERTISED_PROFILES, (
        "every advertised profile must declare a count; reading the declaration "
        "with .get() and skipping a missing one reinstates the hole this closes"
    )
    for profile in sorted(ADVERTISED_PROFILES):
        assert PROFILE_COUNTS.get(profile, 0) == PROFILE_VECTORS[profile], (
            f"{profile} profile vector count"
        )


def test_every_verifier_has_vectors():
    """A verifier with no vectors is a check that silently is not running, and the
    hundreds of unrelated vectors keep the suite green while it does nothing. That
    is the failure class this project exists to catch, so a category missing from
    the vendored corpus fails here rather than passing quietly."""
    seen = {case.values[0] for case in VECTORS}
    expected = {
        c for c in VERIFIERS
        if PROFILE_CATEGORIES.get(c) in (None, *ADVERTISED_PROFILES)
    }
    missing = sorted(expected - seen)
    assert not missing, f"verifiers with no vectors in {CORPUS}: {missing}"


@pytest.mark.parametrize("category,vector", VECTORS)
def test_vector(category, vector):
    verify = VERIFIERS.get(category)
    assert verify is not None, f"unknown category {category!r}"
    ok, detail = verify(vector)
    assert ok, detail
