"""Cross-implementation conformance: run the shared language-neutral corpus
(conformance/spec/ + conformance/gen/) against this package.

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


def expect_resolve(before: str, after: str, threshold: float, margin: float) -> dict:
    anchors = M.build_anchors(before)
    res = M.resolve(anchors, after, threshold=threshold, margin=margin)
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
    got = [finding_dict(f, with_line=True) for f in M.sort_findings(findings)]
    return approx(got, v["findings"]), f"got={got}"


def v_diff(v):
    findings = M.lint_diff(v["before"], v["after"])
    got = [finding_dict(f, with_line=False) for f in M.sort_findings(findings)]
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
    got = expect_resolve(v["before"], v["after"], v["threshold"], v["margin"])
    return approx(got, v["resolutions"]), f"got={got}"


VERIFIERS = {
    "hash": v_hash, "markers": v_markers, "parse": v_parse, "lint": v_lint,
    "diff": v_diff, "seqmatch": v_seqmatch, "score": v_score, "resolve": v_resolve,
}


# --- discover every vector at collection time -----------------------------

def _load_vectors():
    files = sorted((CORPUS / "spec").glob("*.json")) + sorted((CORPUS / "gen").glob("*.json"))
    cases = []
    for path in files:
        data = json.loads(path.read_text())
        category = data["category"]
        tier = path.parent.name
        for i, vec in enumerate(data["vectors"]):
            name = vec.get("name", str(i))
            cases.append(pytest.param(category, vec, id=f"{tier}/{category}:{name}"))
    return cases


VECTORS = _load_vectors()


def test_corpus_present():
    assert VECTORS, f"no corpus files found under {CORPUS}/spec or {CORPUS}/gen"


@pytest.mark.parametrize("category,vector", VECTORS)
def test_vector(category, vector):
    verify = VERIFIERS.get(category)
    assert verify is not None, f"unknown category {category!r}"
    ok, detail = verify(vector)
    assert ok, detail
