"""Behavioral unit tests, ported from the umbrella reference suites
(linter/test_lint.py + eval/attachment/test_attach.py). These complement the
conformance corpus with readable, intent-level assertions.

No network or credentials: the package is fully local and deterministic.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import markstay as M
from markstay import lint as L


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


def test_strict_marker_scanner_uses_host_first_closers_and_later_openers():
    html = '<!-- stay:lost quote="x--!> tail <!-- stay:kept -->'
    assert [marker.id for marker in M.find_markers(html)] == ["kept"]

    html_normal = '<!-- stay:lost quote="x--> tail <!-- stay:kept -->'
    assert [marker.id for marker in M.find_markers(html_normal)] == ["kept"]

    mdx = '{/* stay:lost quote="x*/ tail {/* stay:kept */}'
    assert [marker.id for marker in M.find_markers(mdx)] == ["kept"]


def test_strict_marker_scanner_validates_complete_attributes_and_lf_quotes():
    assert M.find_markers("<!-- stay:a note -->") == []
    assert M.find_markers(r'<!-- stay:a note="bad\q" -->') == []
    malformed = M.find_markers("<!-- stay:note=hello -->")
    assert len(malformed) == 1 and malformed[0].malformed

    raw = '<!-- stay:a quote="one\r\ntwo" -->'
    marker = M.find_markers(raw)[0]
    assert marker.id == "a"
    assert marker.raw == raw


def test_key_first_malformed_survives_invalid_host_closer_diagnostics():
    html = M.find_markers("<!-- stay:note=hello --!>")[0]
    mdx = M.find_markers("{/* stay:note=hello */x")[0]
    assert html.malformed and html.raw == "<!-- stay:note=hello --!>"
    assert mdx.malformed and mdx.raw == "{/* stay:note=hello */"
    assert M.find_markers("<!-- stay:valid --!>") == []


def test_rewrite_valid_nested_marker_inside_malformed_diagnostic_candidate():
    md = "<!-- stay:note=hello x=<!-- stay:inner hash=sha256:dead --> -->"
    rewritten = M.rewrite_markers(
        md,
        lambda marker: (
            "<!-- stay:inner hash=sha256:cafe -->" if marker.id == "inner" else None
        ),
    )
    assert rewritten == (
        "<!-- stay:note=hello x=<!-- stay:inner hash=sha256:cafe --> -->"
    )


def test_rewrite_valid_marker_overlapping_fenced_code_opener():
    md = (
        "```md\n"
        '<!-- stay:outer x-note="start\n'
        "```\n"
        "{/* stay:inner hash=sha256:dead */}\n"
        'tail" -->\n'
    )
    rewritten = M.rewrite_markers(
        md,
        lambda marker: (
            "{/* stay:inner hash=sha256:cafe */}" if marker.id == "inner" else None
        ),
        code=M.code_lines(md),
    )
    assert rewritten == md.replace(
        "{/* stay:inner hash=sha256:dead */}",
        "{/* stay:inner hash=sha256:cafe */}",
    )


def test_lexical_diagnostics_precede_attachment_in_source_order():
    md = (
        "| h |\n|---|\n"
        "| value<!-- stay:dup subhash=sha256:dead --> |\n"
        "<!-- stay:dup -->\n"
    )
    _, findings = M.lint_document(md, child_blocks=True)
    duplicate = next(finding for finding in findings if finding.code == "DUPLICATE_ID")
    assert duplicate.line == 4
    assert "first at line 3" in duplicate.message
    assert findings.index(duplicate) < next(
        index
        for index, finding in enumerate(findings)
        if finding.code in {"HASH_DRIFT", "ORPHAN_CHILD"}
    )


def test_hashed_orphan_does_not_report_empty_body_drift():
    _, findings = M.lint_document("<!-- stay:orphan hash=sha256:dead -->\n")
    assert [(finding.code, finding.id) for finding in findings] == [
        ("ORPHAN_MARKER", "orphan")
    ]


def test_orphan_subhash_is_reported_without_block_attribution_or_hash_drift():
    for value in ("sha256:dead", "bogus", '"sha256:dead"'):
        _, findings = M.lint_document(
            f"<!-- stay:child subhash={value} hash=sha256:dead -->"
        )
        assert [(finding.code, finding.id) for finding in findings] == [
            ("ORPHAN_MARKER", "child")
        ]
    _, control = M.lint_document("<!-- stay:block x-subhash=bogus -->")
    assert codes(control) == ["ORPHAN_MARKER"]


def test_parse_document_retains_raw_crlf_marker_serialization():
    raw = 'Body.\r\n<!-- stay:a quote="one\r\ntwo" -->\r\n'
    marker = M.parse_document(raw)[0].markers[0]
    assert marker.raw == '<!-- stay:a quote="one\r\ntwo" -->'


def test_exact_subhash_presence_is_separate_from_digest_validity():
    invalid = M.find_markers("<!-- stay:a subhash=bogus -->")[0]
    quoted = M.find_markers('<!-- stay:b subhash="sha256:abcd" -->')[0]
    extension = M.find_markers("<!-- stay:c x-subhash=sha256:abcd -->")[0]
    assert (invalid.has_subhash, invalid.subhash) == (True, None)
    assert (quoted.has_subhash, quoted.subhash) == (True, None)
    assert (extension.has_subhash, extension.subhash) == (False, None)


def test_subhash_is_lexical_for_duplicates_but_never_a_block_stay():
    md = (
        "<!-- stay:dup subhash=bogus -->\n\n"
        'Body.\n<!-- stay:dup hash=sha256:dead subhash="bogus" -->\n'
    )
    blocks, findings = M.lint_document(md)
    assert codes(findings) == ["DUPLICATE_ID", "ORPHAN_MARKER"]
    assert L._id_index(blocks) == {}

    extension = "Body.\n<!-- stay:block hash=sha256:dead x-subhash=bogus -->\n"
    _, extension_findings = M.lint_document(extension)
    assert codes(extension_findings) == ["HASH_DRIFT"]


def test_block_diff_ignores_subhash_but_keeps_x_subhash():
    child = "Body.\n<!-- stay:child subhash=bogus -->\n"
    # §16 v1.9: the loss is reported, as a child drop, never as the container's.
    assert [(f.code, f.id) for f in M.lint_diff(child, "Body edited.\n")] == [
        ("DROPPED_CHILD_ID", "child")
    ]

    extension = "Body.\n<!-- stay:block x-subhash=bogus -->\n"
    assert codes(M.lint_diff(extension, "Body edited.\n")) == ["DROPPED_ID"]


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
    diff = M.lint_diff(
        "Alpha.\n<!-- stay:a -->\n", "Alpha, revised.\n<!-- stay:a -->\n"
    )
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
    after = _TBL.replace(
        "| auth | done |\n| orders | wip |\n", "| auth+orders | done |\n"
    )
    assert "COLLECTION_SHRANK" in codes(
        M.lint_diff(_TBL, after, check_collections=True)
    )


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


def test_block_anchors_and_marker_tier_ignore_exact_subhash_presence():
    after = (
        "Child-looking body.\n<!-- stay:child subhash=bogus -->\n\n"
        "Extension body.\n<!-- stay:extension x-subhash=bogus -->\n"
    )
    assert [anchor.id for anchor in M.build_anchors(after)] == ["extension"]

    anchor = M.Anchor(
        id="child",
        hash="not-a-body-hash",
        selector=M.Selector("text absent from the edited document"),
    )
    assert M.resolve([anchor], after)["child"].method != "marker"


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


def test_detached_reasons_split_ambiguity_from_no_match():
    before = "Repeated body.\n<!-- stay:a -->\n"
    anchors = M.build_anchors(before)
    ambiguous = M.resolve(anchors, "Repeated body.\n\nRepeated body.\n")["a"]
    assert ambiguous.reason == "ambiguous"
    assert [candidate.target for candidate in ambiguous.candidates] == [1, 0]
    assert ambiguous.runner_up_score == 1.0
    assert all(
        candidate.provenance == "independent-per-anchor"
        for candidate in ambiguous.candidates
    )
    assert all(
        candidate.evidence[0].code == "body_similarity"
        for candidate in ambiguous.candidates
    )

    unmatched = M.resolve(anchors, "xxxxxxxxxxxxxxxxxxxxxxxx\n")["a"]
    assert unmatched.reason == "unmatched"
    assert unmatched.candidates == []


def test_attached_resolutions_keep_empty_diagnostics():
    resolved = M.resolve(M.build_anchors(BEFORE), BEFORE)
    assert all(result.reason is None for result in resolved.values())
    assert all(result.candidates == [] for result in resolved.values())


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
    assert [b.content for b in M.parse_document(md)] == [
        "---",
        "# Heading",
        "Body para.",
    ]


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


def test_cli_resolve_surfaces_ambiguity_without_recommending_attachment(tmp_path):
    before = tmp_path / "before.md"
    after = tmp_path / "after.md"
    before.write_text("Repeated body.\n<!-- stay:a -->\n")
    after.write_text("Repeated body.\n\nRepeated body.\n")

    hidden = _cli("resolve", "--before", str(before), str(after))
    assert hidden.returncode == 0, hidden.stderr
    assert "a: detached (ambiguous" in hidden.stdout
    assert "no attachment committed" in hidden.stdout
    assert "diagnostic candidate" not in hidden.stdout

    shown = _cli("resolve", "--show-candidates", "--before", str(before), str(after))
    assert shown.returncode == 0, shown.stderr
    assert shown.stdout.count("diagnostic candidate") == 2
    for language in ("winner", "best", "recommended", "attach to"):
        assert language not in shown.stdout.lower()

    structured = _cli("resolve", "--json", "--before", str(before), str(after))
    with_display_flag = _cli(
        "resolve",
        "--json",
        "--show-candidates",
        "--before",
        str(before),
        str(after),
    )
    assert structured.stdout == with_display_flag.stdout
    payload = json.loads(structured.stdout)
    assert payload["schema"] == "markstay.resolve/v1"
    result = payload["resolutions"][0]
    assert result["committed"] is False
    assert result["reason"] == "ambiguous"
    assert result["threshold"] == M.DEFAULT_THRESHOLD
    assert result["required_margin"] == M.DEFAULT_MARGIN
    assert result["diagnostics"]["schema"] == "markstay.resolve-diagnostics/v1"
    assert result["diagnostics"]["diagnostic"] is True
    assert len(result["diagnostics"]["candidates"]) == 2
    assert all(
        candidate["provenance"] == "independent-per-anchor"
        for candidate in result["diagnostics"]["candidates"]
    )
    assert all(
        candidate["evidence"][0]["code"] == "body_similarity"
        for candidate in result["diagnostics"]["candidates"]
    )


def test_cli_resolve_requires_a_baseline_and_hides_unmatched_noise(tmp_path):
    before = tmp_path / "before.md"
    after = tmp_path / "after.md"
    before.write_text("Repeated body.\n<!-- stay:a -->\n")
    after.write_text("xxxxxxxxxxxxxxxxxxxxxxxx\n")
    assert _cli("resolve", str(after)).returncode == 2
    payload = json.loads(
        _cli("resolve", "--json", "--before", str(before), str(after)).stdout
    )
    result = payload["resolutions"][0]
    assert result["reason"] == "unmatched"
    assert result["diagnostics"]["candidates"] == []


def test_cli_resolve_reports_the_actual_committed_quote_margin(tmp_path):
    before_text = (
        "The deploy retries three times.\n<!-- stay:a -->\n\n"
        "Rollback uses the previous image.\n<!-- stay:b -->\n"
    )
    after_text = (
        "The deployment retries failed work three times.\n\n"
        "Rollback uses the previous image.\n<!-- stay:b -->\n"
    )
    before = tmp_path / "before.md"
    after = tmp_path / "after.md"
    before.write_text(before_text)
    after.write_text(after_text)
    expected = M.resolve(M.build_anchors(before_text), after_text)["a"]
    assert expected.method == "quote"
    assert expected.runner_up_score > 0.0

    payload = json.loads(
        _cli("resolve", "--json", "--before", str(before), str(after)).stdout
    )
    result = next(row for row in payload["resolutions"] if row["id"] == "a")
    assert result["runner_up_score"] == expected.runner_up_score
    assert result["observed_margin"] == expected.score - expected.runner_up_score


def test_cli_preserve_prints_the_instruction_verbatim():
    r = _cli("preserve")
    assert r.returncode == 0
    assert r.stdout == M.PRESERVE_INSTRUCTION + "\n"


def test_cli_preserve_wrap_composes_the_measured_prompt_shape(tmp_path):
    p = tmp_path / "doc.md"
    p.write_text("# Title\n\nA paragraph.\n")
    r = _cli("preserve", "--wrap", str(p), "--task", "Tighten it.")
    assert r.returncode == 0
    assert (
        r.stdout == M.preserve_wrap("# Title\n\nA paragraph.\n", "Tighten it.") + "\n"
    )
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


def test_the_agreement_subset_ignores_marker_spans():
    """SPEC.md §5.4: marker spans are excluded from both sides of the comparison.

    Counted, they put every stamped document outside the subset: a trailing
    comment interrupts a paragraph in CommonMark while blank-line segmentation
    keeps the run whole, so `Body.` with a marker under it is one run and two
    nodes. The subset would then be empty exactly when the guarantee is wanted.
    """
    stamped = "Body.\n<!-- stay:x hash=sha256:521b25cc4586 -->\n"
    assert L.in_agreement_subset("Body.\n") is True
    assert L.in_agreement_subset(stamped) is True
    # Case 1 of the three §5.4 lists: a block boundary with no blank line at it.
    assert L.in_agreement_subset("# Heading\nBody.\n") is False
    # Case 2: a blank line inside one node, here a loose list.
    assert L.in_agreement_subset("- a\n\n- b\n") is False
    # Frontmatter is excluded (§5.3), so it does not read as a run of its own.
    assert L.in_agreement_subset("---\ntitle: x\n---\nBody.\n") is True


def test_a_document_outside_the_subset_is_reported_as_advice():
    """SPEC.md §13: a §5.2 linter SHOULD say so, one-directionally.

    `info` rather than `warn`: nothing about the document is wrong, and the
    signal only points one way. A §5.1 write is measurably more likely to change
    what such a document shows; being inside the subset is a better bet rather
    than a promise (§3.4).
    """
    _, findings = M.lint_document("# Heading\nBody.\n")
    advisory = [f for f in findings if f.code == "OUTSIDE_SUBSET"]
    assert [f.level for f in advisory] == ["info"]
    assert not M.has_errors(findings)

    _, clean = M.lint_document("# Heading\n\nBody.\n")
    assert [f for f in clean if f.code == "OUTSIDE_SUBSET"] == []


def test_a_marker_only_line_is_transparent_rather_than_blank():
    """SPEC.md §5.4, corrected in review round 10.

    Blanking a marker-only line manufactures a run boundary no segmenter draws:
    `foo` / marker / `bar` reads as two runs and two nodes, while the blank-line
    segmenter gives ONE block and the tree segmenter gives two. Deleting the line
    instead joins the runs each side of it and certifies the same document for the
    opposite reason.
    """
    assert L.in_agreement_subset("Body.\n<!-- stay:x hash=sha256:521b25cc4586 -->\n") is True
    assert L.in_agreement_subset("foo\n<!-- stay:s -->\nbar\n") is False
    # Transparent only AFTER content. A marker-only line that begins a run is
    # where the profiles part company: this one binds to `B.` under §5.1, whose
    # run starts at the marker line, and to `A.` under §5.2, where an html_block
    # folds into the block before it. Round 11 found this certified.
    assert L.in_agreement_subset("A.\n\n<!-- stay:m -->\nB.\n") is False
    assert L.in_agreement_subset("<!-- stay:m -->\nBody.\n") is False
    # Alone between blank lines it binds to `A.` under both, so it agrees.
    assert L.in_agreement_subset("A.\n\n<!-- stay:m -->\n\nB.\n") is True
    # A marker may span lines; the mask keeps the line endings so the accounting
    # sees the same lines the source has.
    crossing = 'a <!-- stay:x quote="one\n  two" -->\n# b\n'
    assert L.in_agreement_subset(crossing) is False


def test_malformed_diagnostics_remain_content_in_the_agreement_subset():
    for malformed in ("<!-- stay:hash=x -->", "<!-- stay:note=hello -->"):
        doc = f"A.\n{malformed}\n<!-- stay:m -->\n"

        def blocks(mode):
            return [
                (b.line, b.content, [m.id for m in b.markers if not m.malformed])
                for b in L.parse_document(doc, mode=mode)
                if b.index >= 0
            ]

        # The malformed comment remains a block in CommonMark, and the real
        # marker binds to it rather than to the baseline's combined paragraph.
        assert blocks("blank-line") == [(1, f"A.\n{malformed}", ["m"])]
        assert blocks("commonmark") == [(1, "A.", []), (2, malformed, ["m"])]
        assert L.in_agreement_subset(doc) is False

    # MDX-shaped malformed text stays paragraph content under both profiles.
    mdx = "A.\n{/* stay:hash=x */}\n<!-- stay:m -->\n"
    assert L.in_agreement_subset(mdx) is True


def test_the_subset_predicate_agrees_with_the_two_segmenters():
    """The property it is a predicate FOR, rather than its own reasoning.

    Checked here on the shapes that decide it and over the 2417-document corpus in
    `eval/write_safety` (0 certified that segment differently, 0 refused that
    segment identically).
    """
    for doc in (
        "Body.\n",
        "Body.\n<!-- stay:x hash=sha256:521b25cc4586 -->\n",
        "foo\n<!-- stay:s -->\nbar\n",
        "# Heading\nBody.\n",
        "- a\n\n- b\n",
        "A.\n\nB.\n",
        "---\ntitle: x\n---\nBody.\n",
        "Para.\n```\ncode\n```\n",
        "[label]: /url\nBody.\n",
        "A.\n\n<!-- stay:m -->\nB.\n",
        "A.\n\n<!-- stay:m -->\n\nB.\n",
        "<!-- stay:m -->\nBody.\n",
        "Body.\n<!-- stay:a -->\n<!-- stay:b -->\n",
        "A.\n\n<!-- stay:a -->\n<!-- stay:b -->\nB.\n",
    ):
        def blocks(mode):
            return [
                (b.line, b.content)
                for b in M.parse_document(doc, mode=mode)
                if b.index >= 0
            ]

        assert L.in_agreement_subset(doc) is (
            blocks("blank-line") == blocks("commonmark")
        ), doc


def test_a_line_of_unicode_whitespace_is_content_to_both_segmenters():
    """§5's blank line is ASCII-only, and so is this comparison.

    A bare `.strip()` folds U+00A0 in with the spaces, so a line holding one read
    as blank to the predicate and as content to both segmenters, and the document
    was certified while the baseline gave one block and the tree gave two.
    """
    nbsp = "A.\n<!-- stay:x -->\n\u00a0\n"
    assert L.in_agreement_subset(nbsp) is False
