"""Behavioral unit tests, ported from the umbrella reference suites
(linter/test_lint.py + eval/attachment/test_attach.py). These complement the
conformance corpus with readable, intent-level assertions.

No network or credentials: the package is fully local and deterministic.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import markstay as M


def test_readme_keeps_the_preservation_instruction_ahead_of_the_check_backstop():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    instruction = readme.index("## Keeping stays alive through an agent's edit")
    cli = readme.index("## CLI")
    preserve = readme.index("markstay preserve", cli)
    check = readme.index("markstay check-staged", cli)
    assert instruction < cli
    assert preserve < check


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


# --- leading YAML frontmatter is metadata, not a block (SPEC.md §5) -----------

_FM_DOC = "---\nstatus: active\nowner: tim\n---\n\n# Heading\n\nBody para.\n"


def _fm_blocks(md, mode):
    return [(b.index, b.line, b.content) for b in M.parse_document(md, mode=mode)]


def test_frontmatter_is_not_a_block():
    blocks = M.parse_document(_FM_DOC)
    assert [b.content for b in blocks] == ["# Heading", "Body para."]
    assert not any("status: active" in b.content for b in blocks)


def test_frontmatter_does_not_shift_line_numbers():
    # blanking is line-for-line, so reported lines stay true to the source
    assert [(b.index, b.line) for b in M.parse_document(_FM_DOC)] == [(0, 6), (1, 8)]


def test_frontmatter_segmenters_agree():
    """The regression this change exists for: before the frontmatter skip this
    document (no lists, no fences, squarely inside SPEC.md §5's stated agreement
    subset) segmented as 3 blocks under blank-line and 4 under CommonMark, because
    CommonMark reads the closing `---` as a setext underline."""
    pytest.importorskip("markdown_it")
    assert _fm_blocks(_FM_DOC, "blank-line") == _fm_blocks(_FM_DOC, "commonmark")


def test_frontmatter_metadata_edit_does_not_drift_a_hash():
    """A metadata-only edit must not read as a content edit: flipping `status:`
    used to drift the frontmatter block's hash."""
    before = [M.body_hash(b.content) for b in M.parse_document(_FM_DOC)]
    after = [
        M.body_hash(b.content)
        for b in M.parse_document(_FM_DOC.replace("status: active", "status: complete"))
    ]
    assert before == after


def test_frontmatter_with_no_closing_fence_is_a_thematic_break():
    md = "---\n\n# Heading\n\nBody para.\n"
    assert [b.content for b in M.parse_document(md)] == ["---", "# Heading", "Body para."]


def test_frontmatter_does_not_swallow_two_thematic_breaks():
    """Regression: a document opening with a horizontal rule and containing another
    one later must not have everything between them read as frontmatter. The naive
    first-closing-fence rule silently ate `Intro paragraph.`"""
    md = "---\n\nIntro paragraph.\n\n---\n\nBody.\n"
    contents = [b.content for b in M.parse_document(md)]
    assert contents == ["---", "Intro paragraph.", "---", "Body."]


def test_frontmatter_does_not_swallow_a_setext_heading():
    """Regression: `---` / `Title` / `---` is a thematic break followed by a setext
    H2, not frontmatter with the payload `Title`."""
    md = "---\nTitle\n---\n\nBody.\n"
    assert "Title" in "\n".join(b.content for b in M.parse_document(md))


def test_frontmatter_does_not_swallow_an_atx_heading():
    """Regression: a YAML comment and an ATX heading are byte-identical, so `#`
    cannot be the evidence that a span is frontmatter. The cost is that
    comment-only frontmatter is not skipped, which is the safe direction."""
    md = "---\n# Heading\n---\nBody.\n"
    assert "# Heading" in "\n".join(b.content for b in M.parse_document(md))
    md_comment_only = "---\n# just a comment\n---\n\nBody.\n"
    assert "# just a comment" in "\n".join(
        b.content for b in M.parse_document(md_comment_only)
    )


def test_frontmatter_payload_with_a_blank_line_is_not_skipped():
    """Fails towards ordinary Markdown: not skipping is a hash-drift warning, while
    over-skipping silently destroys content."""
    md = "---\nstatus: active\n\nowner: tim\n---\n\nBody.\n"
    assert any("status: active" in b.content for b in M.parse_document(md))


def test_frontmatter_empty_payload_is_not_skipped():
    md = "---\n---\n\nBody.\n"
    assert any("---" in b.content for b in M.parse_document(md))


def test_frontmatter_yamlish_forms_are_recognized():
    for payload in ("status: active", "- one\n- two", "empty:", "nested:\n  a: 1"):
        md = f"---\n{payload}\n---\n\nBody.\n"
        assert [b.content for b in M.parse_document(md)] == ["Body."], payload


def test_frontmatter_closing_fence_tolerates_trailing_whitespace():
    md = "---\nkey: v\n---   \n\nBody.\n"
    assert [b.content for b in M.parse_document(md)] == ["Body."]


def test_frontmatter_closed_by_yaml_end_marker():
    md = "---\ntitle: t\n...\n\n# Heading\n\nBody.\n"
    assert [b.content for b in M.parse_document(md)] == ["# Heading", "Body."]


def test_frontmatter_crlf_normalizes_before_detection():
    md = "---\r\nkey: v\r\n---\r\n\r\n# H\r\n\r\nBody.\r\n"
    assert [b.content for b in M.parse_document(md)] == ["# H", "Body."]


def test_frontmatter_only_at_document_start():
    md = "# Heading\n\n---\ntitle: not frontmatter\n---\n\nBody.\n"
    assert "title: not frontmatter" in "\n".join(
        b.content for b in M.parse_document(md)
    )


def test_frontmatter_no_blank_line_before_content():
    """A document with no blank line after the closing fence still splits
    correctly, which a filter-the-chunks-afterwards implementation gets wrong."""
    pytest.importorskip("markdown_it")
    md = "---\ntitle: t\n---\n# Heading\n\nBody.\n"
    assert [b.content for b in M.parse_document(md)] == ["# Heading", "Body."]
    assert _fm_blocks(md, "blank-line") == _fm_blocks(md, "commonmark")


def test_frontmatter_yamlish_whitespace_is_ascii_pinned():
    """Cross-language agreement, found by external review of the port: `\\S` means
    three different things in Python, ECMAScript and Rust, so the rule spells the
    ASCII set out. An ASCII control character is not a key start (the span stays
    ordinary Markdown); an exotic non-ASCII space is, exactly as for hashing (§8),
    where NBSP is content rather than whitespace."""
    # not a key start -> not frontmatter -> the span survives as content
    md = "---\n\x1ckey: v\n---\n\nBody.\n"
    assert any("key: v" in b.content for b in M.parse_document(md)), md

    # a key start -> frontmatter -> skipped. Each of these is Unicode whitespace to
    # at least one of the three runtimes and not to the others.
    for ch in ("\xa0", "\x85", "\ufeff"):
        md = f"---\n{ch}key: v\n---\n\nBody.\n"
        assert [b.content for b in M.parse_document(md)] == ["Body."], repr(ch)
        md_item = f"---\n- {ch}\n---\n\nBody.\n"
        assert [b.content for b in M.parse_document(md_item)] == ["Body."], repr(ch)


def test_marker_after_closing_fence_is_an_orphan():
    """The visible consequence for a document stamped before this change: its
    frontmatter marker now has no block to attach to, and says so loudly."""
    _, findings = M.lint_document("---\nkey: v\n---\n<!-- stay:x -->\n\nBody.\n")
    assert "ORPHAN_MARKER" in codes(findings)


def test_marker_inside_frontmatter_payload_is_dropped():
    """Pins actual behaviour: a marker *inside* the payload is blanked with the rest
    of the frontmatter and raises nothing. No tool puts a marker there (the stamper
    writes after the block), so this is documented rather than defended."""
    _, findings = M.lint_document("---\nkey: v\n<!-- stay:x -->\n---\n\nBody.\n")
    assert codes(findings) == []


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


# --- preserve (SPEC.md §11) -----------------------------------------------
#
# The verb the eval says matters most: an instructed rewrite keeps ~96-100% of
# markers against ~5% for a naive one. Its CLI contract is deliberately dull, no
# parsing and no git, so these pin the shape rather than the content (the text
# itself is held byte-identical to the JS and Rust copies by the conformance
# corpus, in test_conformance.py).


def _cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "markstay.cli", *args], capture_output=True, text=True
    )


def test_cli_preserve_prints_the_instruction_verbatim():
    r = _cli("preserve")
    assert r.returncode == 0
    assert r.stdout == M.PRESERVE_INSTRUCTION + "\n"


def test_cli_preserve_wrap_composes_the_measured_prompt_shape(tmp_path):
    p = tmp_path / "doc.md"
    p.write_text("# Title\n\nA paragraph.\n")
    r = _cli("preserve", "--wrap", str(p), "--task", "Tighten it.")
    assert r.returncode == 0
    assert r.stdout == M.preserve_wrap("# Title\n\nA paragraph.\n", "Tighten it.") + "\n"
    # task first, then the instruction, then the document behind the rule
    assert (
        r.stdout.index("Tighten it.")
        < r.stdout.index(M.PRESERVE_INSTRUCTION)
        < r.stdout.index("A paragraph.")
    )


def test_cli_preserve_rejects_a_task_without_wrap_and_a_missing_file(tmp_path):
    # A bare FILE is the plausible mistake (every other verb takes one), so it
    # has to fail loudly rather than print the instruction and ignore the doc.
    assert _cli("preserve", "--task", "Tighten it.").returncode == 2
    assert _cli("preserve", str(tmp_path / "nope.md")).returncode == 2
    assert _cli("preserve", "--wrap", str(tmp_path / "nope.md")).returncode == 2


def test_cli_preserve_rejects_input_that_is_not_utf8(tmp_path):
    # Python's text streams surrogate-escape and Node substitutes U+FFFD, so a
    # lenient read is precisely how three implementations stop emitting the same
    # bytes for the same document. All three exit 2 instead.
    p = tmp_path / "bad.md"
    p.write_bytes(b"Body \xff byte.\n")
    r = _cli("preserve", "--wrap", str(p))
    assert r.returncode == 2
    assert "not valid UTF-8" in r.stderr
    assert "Traceback" not in r.stderr

    r = subprocess.run(
        [sys.executable, "-m", "markstay.cli", "preserve", "--wrap", "-"],
        input=b"Body \xff byte.\n",
        capture_output=True,
    )
    assert r.returncode == 2
    assert b"not valid UTF-8" in r.stderr


def test_cli_commonmark_without_the_parser_is_an_error_not_a_traceback(tmp_path):
    """`--commonmark` is the one optional extra. Without it the CLI must exit 2
    with the install line, not surface a ModuleNotFoundError from the segmenter."""
    p = tmp_path / "doc.md"
    p.write_text("Body.\n<!-- stay:a1b2 -->\n")
    shim = (
        "import sys; sys.modules['markdown_it'] = None; "
        "from markstay.cli import main; sys.exit(main(sys.argv[1:]))"
    )
    r = subprocess.run(
        [sys.executable, "-c", shim, "lint", "--commonmark", str(p)],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 2, r.stderr
    assert "markstay[commonmark]" in r.stderr
    assert "Traceback" not in r.stderr
