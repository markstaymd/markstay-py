"""Behavioral unit tests, ported from the umbrella reference suites
(linter/test_lint.py + eval/attachment/test_attach.py). These complement the
conformance corpus with readable, intent-level assertions.

No network or credentials: the package is fully local and deterministic.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

import markstay as M


def codes(findings):
    return sorted(f.code for f in findings)


# --- well-formedness + intra-doc (linter/test_lint.py) --------------------


def test_clean_doc_with_correct_hash():
    body = "The order pipeline ingests messages and normalizes them."
    h = M.body_hash(body, 4)
    md = (
        f"{body}\n<!-- stay:8f24 hash=sha256:{h} -->\n\n"
        "A second paragraph that is also identified.\n<!-- stay:a1b2 -->\n"
    )
    _, findings = M.lint_document(md)
    assert findings == [], codes(findings)
    assert not M.has_errors(findings)


def test_hash_uppercase_hex_no_drift():
    body = "Users authenticate with an API key in the Authorization header."
    h = M.body_hash(body, 4).upper()
    md = f"{body}\n<!-- stay:8f24 hash=sha256:{h} -->\n"
    _, findings = M.lint_document(md)
    assert codes(findings) == []
    assert not M.has_errors(findings)


def test_marker_no_blank_line_attaches_to_block():
    blocks = M.parse_document("Just one paragraph.\n<!-- stay:p1 -->\n")
    assert len(blocks) == 1
    assert blocks[0].content == "Just one paragraph."
    assert [m.id for m in blocks[0].markers] == ["p1"]


def test_marker_only_chunk_attaches_to_previous():
    blocks = M.parse_document("Some content.\n\n<!-- stay:x -->\n")
    assert len(blocks) == 1
    assert blocks[0].content == "Some content."
    assert [m.id for m in blocks[0].markers] == ["x"]


def test_duplicate_id():
    md = "Block one.\n<!-- stay:dup -->\n\nBlock two.\n<!-- stay:dup -->\n"
    _, findings = M.lint_document(md)
    assert "DUPLICATE_ID" in codes(findings)
    assert M.has_errors(findings)


def test_malformed_marker():
    _, findings = M.lint_document("A paragraph.\n<!-- stay:note=hello -->\n")
    assert "MALFORMED_MARKER" in codes(findings)


def test_orphan_marker_at_top():
    _, findings = M.lint_document("<!-- stay:loose -->\n\nReal content below.\n")
    assert "ORPHAN_MARKER" in codes(findings)


def test_hash_drift_intradoc():
    _, findings = M.lint_document(
        "Edited content.\n<!-- stay:z9 hash=sha256:dead -->\n"
    )
    assert codes(findings) == ["HASH_DRIFT"]
    assert not M.has_errors(findings)  # drift is a warning, not an error
    assert all(f.level == "warn" for f in findings if f.code == "HASH_DRIFT")


def test_mdx_marker_parsed():
    blocks = M.parse_document("An MDX block.\n{/* stay:mdx1 hash=sha256:abcd */}\n")
    assert blocks[0].markers[0].id == "mdx1"
    assert blocks[0].markers[0].syntax == "mdx"


def test_strip_markers_removes_both_syntaxes():
    assert M.strip_markers("a<!-- stay:h -->b{/* stay:m */}c") == "abc"


# --- regeneration diff (§11) ----------------------------------------------


def test_diff_dropped():
    before = "A.\n<!-- stay:a -->\n\nB.\n<!-- stay:b -->\n"
    after = "A.\n<!-- stay:a -->\n\nB rewritten without its marker.\n"
    findings = M.lint_diff(before, after)
    assert [f.id for f in findings if f.code == "DROPPED_ID"] == ["b"]
    assert M.has_errors(findings)


def test_diff_duplicated():
    before = "A.\n<!-- stay:a -->\n"
    after = "A.\n<!-- stay:a -->\n\nCopy of A.\n<!-- stay:a -->\n"
    assert "DUPLICATED_ID" in codes(M.lint_diff(before, after))


def test_diff_new_id_is_info():
    before = "A.\n<!-- stay:a -->\n"
    after = "A.\n<!-- stay:a -->\n\nBrand new block.\n<!-- stay:c -->\n"
    findings = M.lint_diff(before, after)
    assert [f.id for f in findings if f.code == "NEW_ID"] == ["c"]
    assert not M.has_errors(findings)


def test_diff_relocation_swap():
    before = "Alpha content.\n<!-- stay:aaa -->\n\nBeta content.\n<!-- stay:bbb -->\n"
    after = "Beta content.\n<!-- stay:aaa -->\n\nAlpha content.\n<!-- stay:bbb -->\n"
    findings = M.lint_diff(before, after)
    assert sorted(f.id for f in findings if f.code == "RELOCATED_ID") == ["aaa", "bbb"]
    assert M.has_errors(findings)


def test_diff_inplace_edit_is_drift_not_relocation():
    before = "Alpha content.\n<!-- stay:aaa -->\n"
    after = "Alpha content, now revised.\n<!-- stay:aaa -->\n"
    findings = M.lint_diff(before, after)
    assert codes(findings) == ["HASH_DRIFT"]
    assert all(f.level == "warn" for f in findings if f.code == "HASH_DRIFT")


def test_hash_drift_stays_warn_in_return_tuples_guardrail():
    # Guardrail: the packaged markstay.lint must keep HASH_DRIFT at warn, because
    # the RAG chunker imports THIS API and treats a drift warning as fatal (a stale
    # hash means the cache's re-embed trigger is lying). Quieting the text render
    # must never strip drift, or downgrade its level, in the structured channel.
    # Mirror of the umbrella guardrail in linter/test_lint.py.
    _, doc = M.lint_document("Edited.\n<!-- stay:z9 hash=sha256:dead -->\n")
    doc_drift = [f for f in doc if f.code == "HASH_DRIFT"]
    assert doc_drift and all(f.level == "warn" for f in doc_drift)
    diff = M.lint_diff("Alpha.\n<!-- stay:a -->\n", "Alpha, revised.\n<!-- stay:a -->\n")
    diff_drift = [f for f in diff if f.code == "HASH_DRIFT"]
    assert diff_drift and all(f.level == "warn" for f in diff_drift)


def test_unknown_mode_rejected():
    with pytest.raises(ValueError):
        M.parse_document("x\n", mode="bogus")


# --- COLLECTION_SHRANK: opt-in within-collection loss check (SPEC.md §5.1) -----
# A stay binds the whole table/list, so a dropped row/bullet is normally only a
# non-blocking HASH_DRIFT. check_collections=True turns net item loss into a
# blocking finding; off by default so existing callers are unaffected.

_TBL = (
    "| Item | State |\n|------|-------|\n"
    "| auth | done |\n| orders | wip |\n<!-- stay:tbl -->\n"
)
_LST = "- alpha\n- beta\n- gamma\n<!-- stay:lst -->\n"


def test_collection_shrank_table_row_drop():
    after = _TBL.replace("| orders | wip |\n", "")
    on = M.lint_diff(_TBL, after, check_collections=True)
    assert "COLLECTION_SHRANK" in codes(on)
    assert M.has_errors(on)
    off = M.lint_diff(_TBL, after)  # off by default: only a non-blocking drift
    assert "COLLECTION_SHRANK" not in codes(off)
    assert not M.has_errors(off)


def test_collection_shrank_bullet_drop():
    after = _LST.replace("- beta\n", "")
    on = M.lint_diff(_LST, after, check_collections=True)
    assert "COLLECTION_SHRANK" in codes(on)
    assert M.has_errors(on)


def test_collection_shrank_silent_on_inplace_edit_and_growth():
    edited = _TBL.replace("| orders | wip |", "| orders | done |")
    grown = _TBL.replace("| orders | wip |\n", "| orders | wip |\n| billing | todo |\n")
    for after in (edited, grown):
        on = M.lint_diff(_TBL, after, check_collections=True)
        assert "COLLECTION_SHRANK" not in codes(on)
        assert not M.has_errors(on)


def test_collection_shrank_fires_on_consolidation_known_fp():
    after = _TBL.replace("| auth | done |\n| orders | wip |\n", "| auth+orders | done |\n")
    assert "COLLECTION_SHRANK" in codes(M.lint_diff(_TBL, after, check_collections=True))


def test_collection_shrank_distinct_from_dropped_block():
    after = "Some replacement paragraph.\n<!-- stay:other -->\n"
    cs = codes(M.lint_diff(_TBL, after, check_collections=True))
    assert "DROPPED_ID" in cs
    assert "COLLECTION_SHRANK" not in cs


# --- attachment resolver ladder (§9.1) ------------------------------------

BEFORE = (
    "The ingest stage retries failed operations three times.\n<!-- stay:a -->\n\n"
    "Users authenticate with a bearer token in the header.\n<!-- stay:b -->\n\n"
    "Prices are stored in minor units to avoid float error.\n<!-- stay:c -->\n"
)


def test_marker_tier():
    anchors = M.build_anchors(BEFORE)
    res = M.resolve(anchors, BEFORE)  # unchanged: every marker survives
    assert {r.method for r in res.values()} == {"marker"}
    assert all(r.score == 1.0 for r in res.values())


def test_hash_tier_marker_lost_body_verbatim():
    # drop b's marker but keep its body byte-identical -> hash tier recovers it
    after = BEFORE.replace("\n<!-- stay:b -->", "")
    res = M.resolve(M.build_anchors(BEFORE), after)
    assert res["b"].method == "hash"
    assert res["b"].target is not None


def test_quote_tier_recovers_paraphrase():
    after = (
        "The ingest stage retries failed operations three times.\n<!-- stay:a -->\n\n"
        "Users sign in with a bearer token supplied in the request header.\n\n"
        "Prices are stored in minor units to avoid float error.\n<!-- stay:c -->\n"
    )
    res = M.resolve(M.build_anchors(BEFORE), after)
    assert res["b"].method == "quote"
    assert res["b"].score >= M.DEFAULT_THRESHOLD


def test_deleted_block_detaches():
    after = (
        "The ingest stage retries failed operations three times.\n<!-- stay:a -->\n\n"
        "Prices are stored in minor units to avoid float error.\n<!-- stay:c -->\n"
    )
    res = M.resolve(M.build_anchors(BEFORE), after)
    assert res["b"].method == "detached"
    assert res["b"].target is None


def test_determinism():
    a = M.resolve(M.build_anchors(BEFORE), BEFORE)
    b = M.resolve(M.build_anchors(BEFORE), BEFORE)
    assert {k: (v.method, v.target) for k, v in a.items()} == {
        k: (v.method, v.target) for k, v in b.items()
    }


# --- CommonMark mode (§5.2, optional extra) -------------------------------


def test_commonmark_loose_list_is_one_block():
    pytest.importorskip("markdown_it")
    md = "- item one\n\n- item two\n\n- item three\n<!-- stay:mylist -->\n"
    cm = [b for b in M.parse_document(md, mode="commonmark") if b.index >= 0]
    assert len(cm) == 1
    assert all(x in cm[0].content for x in ("item one", "item two", "item three"))
    assert [m.id for m in cm[0].markers] == ["mylist"]


# --- CLI smoke ------------------------------------------------------------


def test_cli_lints_a_file(tmp_path):
    p = tmp_path / "doc.md"
    p.write_text("A clean paragraph.\n<!-- stay:ok -->\n")
    r = subprocess.run(
        [sys.executable, "-m", "markstay.cli", "lint", str(p)],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0
    assert "clean" in r.stdout


def test_cli_nonzero_on_error(tmp_path):
    p = tmp_path / "doc.md"
    p.write_text("Para.\n<!-- stay:dup -->\n\nPara two.\n<!-- stay:dup -->\n")
    r = subprocess.run(
        [sys.executable, "-m", "markstay.cli", "lint", str(p)],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 1
    assert "DUPLICATE_ID" in r.stdout


def _lint_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "markstay.cli", "lint", *args],
        capture_output=True,
        text=True,
    )


def test_cli_hides_drift_by_default_lists_with_flag(tmp_path):
    p = tmp_path / "doc.md"
    p.write_text("Edited.\n<!-- stay:z9 hash=sha256:dead -->\n")
    hidden = _lint_cli(str(p))
    assert hidden.returncode == 0  # drift is a warning, never gates
    assert "HASH_DRIFT" not in hidden.stdout
    assert "hash-drift" in hidden.stdout and "--show-drift" in hidden.stdout
    shown = _lint_cli("--show-drift", str(p))
    assert "HASH_DRIFT" in shown.stdout


def test_cli_json_byte_identical_regardless_of_show_drift(tmp_path):
    p = tmp_path / "doc.md"
    p.write_text("Edited.\n<!-- stay:z9 hash=sha256:dead -->\n")
    a = _lint_cli("--json", str(p))
    b = _lint_cli("--json", "--show-drift", str(p))
    assert a.stdout == b.stdout  # --json is the structured channel, untouched
    assert "HASH_DRIFT" in a.stdout


def test_cli_before_diff_text_path_hides_drift(tmp_path):
    before = tmp_path / "before.md"
    after = tmp_path / "after.md"
    before.write_text("Alpha content.\n<!-- stay:aaa -->\n")
    after.write_text("Alpha content, now revised.\n<!-- stay:aaa -->\n")
    hidden = _lint_cli("--before", str(before), str(after))
    assert "HASH_DRIFT" not in hidden.stdout
    assert "hash-drift" in hidden.stdout
    shown = _lint_cli("--show-drift", "--before", str(before), str(after))
    assert "HASH_DRIFT" in shown.stdout


def test_cli_stamp_writes_in_place_then_lints_clean(tmp_path):
    p = tmp_path / "doc.md"
    p.write_text("First paragraph.\n\nSecond paragraph.\n")
    stamp = subprocess.run(
        [sys.executable, "-m", "markstay.cli", "stamp", "-w", str(p)],
        capture_output=True,
        text=True,
    )
    assert stamp.returncode == 0
    assert "2 id(s) minted" in stamp.stderr
    lint = subprocess.run(
        [sys.executable, "-m", "markstay.cli", "lint", str(p)],
        capture_output=True,
        text=True,
    )
    assert lint.returncode == 0
    assert "clean" in lint.stdout


def test_cli_stamp_commonmark_keeps_marker_out_of_fence(tmp_path):
    pytest.importorskip("markdown_it")
    p = tmp_path / "doc.md"
    p.write_text("```txt\nalpha\n\nbeta\n```\n")
    stamp = subprocess.run(
        [sys.executable, "-m", "markstay.cli", "stamp", "--commonmark", "-w", str(p)],
        capture_output=True,
        text=True,
    )
    assert stamp.returncode == 0
    lines = p.read_text().splitlines()
    fence_open = False
    inside_markers = []
    for line in lines:
        if line.startswith("```"):
            fence_open = not fence_open
        elif fence_open and "stay:" in line:
            inside_markers.append(line)
    assert inside_markers == []
    assert lines[4].startswith("```")
    assert lines[5].startswith("<!-- stay:")
