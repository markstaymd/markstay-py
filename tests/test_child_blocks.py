"""Experimental direct list-item identity (PLAN_SUBBLOCK_IDENTITY.md, phase 1)."""

from __future__ import annotations

import json
from itertools import count
import subprocess
import sys

import pytest

import markstay as M
from markstay.resolve import _resolve_parents


def ids(prefix: str = "c"):
    seq = count()
    return lambda: f"{prefix}{next(seq)}"


def stamped(md: str, *, mode: str = "blank-line") -> str:
    return M.stamp(md, new_id=ids(), mode=mode, child_blocks=True).text


def test_child_body_ignores_bullet_glyph_indent_and_order_number():
    assert M.child_body("- Done") == "Done"
    assert M.child_body("* Done") == "Done"
    assert M.child_body("  1. Done") == "Done"
    assert M.body_hash(M.child_body("- Done")) == M.body_hash(M.child_body("93) Done"))
    assert M.child_body("9. Done\n   continued") == "Done\ncontinued"
    assert M.child_body("10. Done\n    continued") == "Done\ncontinued"


def test_restricted_profile_agrees_with_commonmark_and_fails_closed_elsewhere():
    pytest.importorskip("markdown_it")
    inside = "1. Alpha\n2. Beta\n3. Gamma\n"
    blank = M.parse_document(inside, child_blocks=True)
    common = M.parse_document(inside, mode="commonmark", child_blocks=True)
    assert [c.content for c in blank[0].children] == ["Alpha", "Beta", "Gamma"]
    assert [c.content for c in common[0].children] == ["Alpha", "Beta", "Gamma"]

    multiline = "- Alpha\n  continuation\n- Beta\n"
    blank_multi = M.parse_document(multiline, child_blocks=True)[0].children
    common_multi = M.parse_document(multiline, mode="commonmark", child_blocks=True)[
        0
    ].children
    assert [c.content for c in blank_multi] == [c.content for c in common_multi]

    lazy = "- Alpha\nlazy continuation\n- Beta\n"
    assert M.parse_document(lazy, child_blocks=True)[0].children == []

    loose = "- Alpha\n\n- Beta\n"
    assert all(not b.children for b in M.parse_document(loose, child_blocks=True))
    assert (
        len(M.parse_document(loose, mode="commonmark", child_blocks=True)[0].children)
        == 2
    )


def test_nested_list_is_not_a_sibling_child_in_either_segmenter():
    # SPEC.md §5.5: only direct items are child blocks, and a nested list belongs
    # to its ancestor item's body. _LIST_PREFIX_RE allows three leading spaces, so
    # the restricted profile used to emit "  - Nested" as child 2 and push Beta to
    # 3, disagreeing with CommonMark about which item every later child stay
    # addresses, which is the granularity failure §13 names.
    pytest.importorskip("markdown_it")
    nested = "- Alpha\n  - Nested\n- Beta\n"
    assert M.parse_document(nested, child_blocks=True)[0].children == []
    common = M.parse_document(nested, mode="commonmark", child_blocks=True)[0].children
    assert [(c.ordinal, c.content) for c in common] == [
        (1, "Alpha\n- Nested"),
        (2, "Beta"),
    ]


def test_child_stamping_mints_children_and_parent_in_one_idempotent_pass():
    result = M.stamp("- Alpha\n- Beta\n", new_id=ids(), child_blocks=True)
    assert len(result.minted) == 3
    assert result.text.count("subhash=sha256:") == 2
    assert result.text.count(" hash=sha256:") == 1
    blocks, findings = M.lint_document(result.text, child_blocks=True)
    assert findings == []
    assert len(blocks[0].children) == 2
    assert [len(c.markers) for c in blocks[0].children] == [1, 1]
    again = M.stamp(result.text, new_id=ids("new"), child_blocks=True)
    assert again.minted == []
    assert again.text == result.text


def test_child_stamping_does_not_mistake_child_marker_for_parent_stay():
    digest = M.body_hash("Alpha", 12)
    md = f"- Alpha <!-- stay:child subhash=sha256:{digest} -->\n"
    result = M.stamp(md, new_id=lambda: "parent", child_blocks=True)
    assert result.text.count("stay:child") == 1
    assert "stay:parent hash=sha256:" in result.text


def test_child_stamping_skips_item_without_safe_inline_paragraph_carrier():
    md = "- ```txt\n  code\n  ```\n"
    result = M.stamp(md, mode="commonmark", child_blocks=True, new_id=ids())
    assert "subhash=" not in result.text
    assert result.text.startswith(md.rstrip("\n"))
    # SPEC.md §3.3 takes the parent stay away as well, and the cause is the line
    # scan rather than this item's carrier. The item's opening fence shares a line
    # with the list marker, so the line rule cannot see it open; the closing
    # `  ``` ` is then the first fence-shaped line it does see, reads as an
    # *opener*, and runs unclosed to the end of the document. Every insertion
    # point after it is inside a fence, so the writer rule refuses. It fails
    # closed (nothing is stamped, nothing is corrupted) and it is the sharpest
    # case of the limit §3.3 names for a fence the line rule cannot see.
    assert result.text.count(" hash=sha256:") == 0


def test_marker_in_a_nested_item_addresses_nothing_and_says_so():
    # SPEC.md §5.5: a nested item is not a child block in v1.3, so a subhash
    # marker inside one is neither a child stay nor the container's. It used to
    # land in the container's marker list and lint clean, which is silence a
    # reader cannot tell apart from a marker that resolved.
    pytest.importorskip("markdown_it")
    md = "- Outer\n  - Inner <!-- stay:kid1 subhash=sha256:abcd -->\n"
    blocks, findings = M.lint_document(md, mode="commonmark", child_blocks=True)
    assert [c.content for c in blocks[0].children] == ["Outer\n- Inner"]
    assert [(f.level, f.code, f.id) for f in findings] == [
        ("warn", "CHILD_UNADDRESSED", "kid1")
    ]


def test_orphan_child_warns_but_does_not_error():
    digest = M.body_hash("Alpha", 4)
    md = f"- Alpha <!-- stay:child subhash=sha256:{digest} -->\n"
    _, findings = M.lint_document(md, child_blocks=True)
    assert [(f.code, f.level) for f in findings] == [("ORPHAN_CHILD", "warn")]


def test_dropped_child_blocks_while_reworded_child_recovers_by_quote():
    before = stamped("- Ship the linter\n- Document the command\n- Publish package\n")
    dropped = (
        "\n".join(
            line for line in before.splitlines() if not line.startswith("- Document")
        )
        + "\n"
    )
    assert "CHILD_DROPPED" in {
        f.code for f in M.lint_diff(before, dropped, child_blocks=True)
    }

    reworded = (
        "\n".join(
            "- Document the CLI clearly" if line.startswith("- Document") else line
            for line in before.splitlines()
        )
        + "\n"
    )
    findings = M.lint_diff(before, reworded, child_blocks=True)
    assert "CHILD_DROPPED" not in {f.code for f in findings}
    middle = M.build_child_anchors(before)[1]
    resolved = M.resolve_children([middle], reworded)[middle.id]
    assert resolved.method == "quote"
    assert resolved.runner_up_score > 0.0


def test_original_duplicate_child_hashes_can_resolve_by_quote_context():
    before = stamped(
        "- Alpha\n- Shared task\n- Beta\n- Shared task\n- Gamma\n"
    )
    parent_marker = before.splitlines()[-1]
    after = (
        "- Alpha\n"
        "- Shared task revised\n"
        "- Beta\n"
        "- Shared task revised\n"
        "- Gamma\n"
        f"{parent_marker}\n"
    )
    anchors = [
        anchor
        for anchor in M.build_child_anchors(before)
        if anchor.selector.quote == "Shared task"
    ]
    assert [anchor.sibling_hash_count for anchor in anchors] == [2, 2]

    resolved = M.resolve_children(anchors, after)
    assert [resolved[anchor.id].method for anchor in anchors] == ["quote", "quote"]
    assert [resolved[anchor.id].target for anchor in anchors] == [1, 3]


def test_original_duplicate_child_hash_cannot_regain_exact_hash_authority():
    before = (
        "- Alpha\n"
        f"- Shared task <!-- stay:s1 subhash=sha256:{M.body_hash('Shared task')} -->\n"
        "- Beta\n"
        "- Shared task\n"
        "- Gamma\n"
        "<!-- stay:parent -->\n"
    )
    after = (
        "- Alpha\n"
        "- Shared task\n"
        "- Beta\n"
        "- Shared task revised\n"
        "- Gamma\n"
        "<!-- stay:parent -->\n"
    )
    anchors = [
        anchor
        for anchor in M.build_child_anchors(before)
        if anchor.selector.quote == "Shared task"
    ]
    assert len(anchors) == 1
    assert anchors[0].sibling_hash_count == 2
    assert anchors[0].document_hash_count == 2
    current_children = [
        child
        for block in M.parse_document(after, child_blocks=True)
        for child in block.children
    ]
    current_exact = sum(
        M.body_hash(child.content) == anchors[0].hash for child in current_children
    )
    assert current_exact == 1

    resolved = M.resolve_children(anchors, after)
    assert resolved[anchors[0].id].method == "quote"
    assert resolved[anchors[0].id].target == 1


def test_surviving_markers_beat_parent_hash_ordinal_inference():
    before = stamped("- Alpha\n- Beta\n")
    lines = before.splitlines()
    first_marker = lines[0].split(" <!--", 1)[1]
    second_marker = lines[1].split(" <!--", 1)[1]
    after = (
        f"- Alpha <!--{second_marker}\n" f"- Beta <!--{first_marker}\n" f"{lines[2]}\n"
    )
    anchors = M.build_child_anchors(before)
    resolved = M.resolve_children(anchors, after)
    assert [resolved[a.id].method for a in anchors] == ["marker", "marker"]
    assert [resolved[a.id].target for a in anchors] == [1, 0]


def test_surviving_markers_outlive_an_unresolvable_parent():
    """The parent's block-level marker sits on its own line, so an edit drops it
    easily; the child markers ride inline in the bullet text the model is
    rewriting. Stored child identity must not be discarded because an inference
    about the container failed."""

    before = stamped("- Deploy the alpha service\n- Verify the alpha service\n")
    after = (
        "\n".join(
            line
            for line in before.splitlines()
            if not (line.startswith("<!-- stay:") and line.endswith("-->"))
        )
        .replace("Deploy the alpha service", "Roll out the ingestion pipeline")
        .replace("Verify the alpha service", "Smoke-test downstream consumers")
        + "\n"
    )
    anchors = M.build_child_anchors(before)
    resolved = M.resolve_children(anchors, after)
    assert [resolved[a.id].method for a in anchors] == ["marker", "marker"]
    assert [resolved[a.id].target for a in anchors] == [0, 1]


def test_near_duplicate_parent_does_not_capture_a_deleted_sibling_list():
    """Exclusive assignment: the surviving list claims its own block by exact
    hash, so the deleted near-duplicate cannot quote-match onto it and drag its
    children along."""

    before = stamped(
        "- Deploy alpha service\n- Verify alpha service\n"
        "\n## Beta\n\n"
        "- Deploy beta service\n- Verify beta service\n",
        mode="commonmark",
    )
    keep = before.split("## Beta", 1)[1]
    after = "## Beta" + "\n".join(
        line for line in keep.splitlines() if "<!-- stay:" not in line
    )
    anchors = M.build_child_anchors(before, mode="commonmark")
    alpha = [a for a in anchors if "alpha" in a.selector.quote]
    resolved = M.resolve_children(anchors, after, mode="commonmark")
    assert [resolved[a.id].method for a in alpha] == ["detached", "detached"]


def test_ordered_renumber_recovers_markerless_children_by_child_hash():
    before = stamped("8. Alpha\n9. Beta\n", mode="commonmark")
    parent_marker = before.splitlines()[-1]
    after = f"1. Alpha\n2. Beta\n{parent_marker}\n"
    anchors = M.build_child_anchors(before, mode="commonmark")
    resolved = M.resolve_children(anchors, after, mode="commonmark")
    assert [resolved[anchor.id].method for anchor in anchors] == ["hash", "hash"]


def test_markerless_child_move_uses_document_scoped_exact_hash():
    before = stamped("- Alpha\n- Move me\n\nInterlude.\n\n- Gamma\n- Delta\n")
    moved_line = next(
        line for line in before.splitlines() if line.startswith("- Move me")
    )
    after_lines = [line for line in before.splitlines() if line != moved_line]
    insert_at = (
        next(i for i, line in enumerate(after_lines) if line.startswith("- Delta")) + 1
    )
    after_lines.insert(insert_at, "- Move me")
    after = "\n".join(after_lines) + "\n"
    anchor = next(
        a for a in M.build_child_anchors(before) if a.selector.quote == "Move me"
    )
    result = M.resolve_children([anchor], after)[anchor.id]
    assert result.method == "document-hash"


def test_two_stays_on_one_item_contest_it_and_neither_gets_it():
    # SPEC.md §9.2: a tier is evaluated for every stay before any of it is
    # committed, so a contested item goes to neither. Committing per anchor gave
    # it to whichever one the loop saw first, which is a resolution that depends
    # on enumeration order rather than on evidence.
    digest = M.body_hash(M.child_body("1. Alpha"), 12)
    before = (
        f"1. Alpha <!-- stay:a1 subhash=sha256:{digest} -->"
        f" <!-- stay:a2 subhash=sha256:{digest} -->\n2. Beta\n"
    )
    container = next(
        b for b in M.parse_document(before, child_blocks=True) if b.children
    )
    before += f"<!-- stay:P hash=sha256:{M.body_hash(container.content, 12)} -->\n"
    after = before.replace(f" <!-- stay:a1 subhash=sha256:{digest} -->", "").replace(
        f" <!-- stay:a2 subhash=sha256:{digest} -->", ""
    )
    resolved = M.resolve_children(M.build_child_anchors(before), after)
    assert resolved["a1"].method == "detached"
    assert resolved["a2"].method == "detached"
    assert resolved["a1"].reason == "contested"
    assert resolved["a2"].reason == "contested"
    assert resolved["a1"].proposed_target == resolved["a2"].proposed_target
    assert resolved["a1"].contested_with == ["a2"]
    assert resolved["a2"].contested_with == ["a1"]
    assert resolved["a1"].candidates == []
    assert (
        resolved["a1"].proposal_provenance
        == "child-parent-hash-tier-snapshot"
    )


def test_parent_quote_collision_is_contested_and_order_invariant():
    parents = [
        M.Anchor("p1", "not-a-real-hash-1", M.Selector("Shared parent body")),
        M.Anchor("p2", "not-a-real-hash-2", M.Selector("Shared parent body")),
    ]
    anchors = [
        M.ChildAnchor(
            f"c{index}",
            f"child-hash-{index}",
            M.Selector("child"),
            1,
            parent,
            parent.hash,
        )
        for index, parent in enumerate(parents, 1)
    ]
    after = "- Shared parent body\n- Tail\n"
    blocks = [
        block
        for block in M.parse_document(after, child_blocks=True)
        if block.index >= 0
    ]

    forward = _resolve_parents(anchors, blocks, after)
    reverse = _resolve_parents(list(reversed(anchors)), blocks, after)
    for result in (forward, reverse):
        assert result["p1"].reason == "contested"
        assert result["p2"].reason == "contested"
        assert result["p1"].target is None
        assert result["p2"].target is None
        assert result["p1"].proposed_target == result["p2"].proposed_target
        assert result["p1"].contested_with == ["p2"]
        assert result["p2"].contested_with == ["p1"]
        assert result["p1"].candidates == []
        assert result["p1"].proposal_provenance == "parent-snapshot"


def test_attached_parent_quote_preserves_its_actual_runner_up_score():
    after = (
        "The deployment retries failed work three times.\n\n"
        "Rollback uses the previous image.\n"
    )
    blocks = [
        block
        for block in M.parse_document(after, child_blocks=True)
        if block.index >= 0
    ]
    parent = M.Anchor(
        "p",
        "not-a-real-hash",
        M.Selector("The deploy retries three times."),
    )
    anchor = M.ChildAnchor(
        "c",
        "not-a-real-child-hash",
        M.Selector("child"),
        1,
        parent,
        parent.hash,
    )
    expected = M.best_match(parent.selector, [block.content for block in blocks])
    result = _resolve_parents([anchor], blocks, after)[parent.id]
    assert result.method == "quote"
    assert result.runner_up_score == expected[2]
    assert result.runner_up_score > 0.0


def test_parent_hash_contest_is_retained_if_weaker_tiers_do_not_attach():
    after = "- Shared parent body\n- Tail\n"
    blocks = [
        block
        for block in M.parse_document(after, child_blocks=True)
        if block.index >= 0
    ]
    digest = M.body_hash(blocks[0].content)
    parents = [
        M.Anchor("p1", digest, M.Selector("Shared parent body")),
        M.Anchor("p2", digest, M.Selector("Shared parent body")),
    ]
    anchors = [
        M.ChildAnchor(
            f"c{index}",
            f"child-hash-{index}",
            M.Selector("child"),
            1,
            parent,
            parent.hash,
        )
        for index, parent in enumerate(parents, 1)
    ]
    resolved = _resolve_parents(anchors, blocks, after)
    assert resolved["p1"].reason == "contested"
    assert resolved["p2"].reason == "contested"
    assert resolved["p1"].proposal_provenance == "parent-hash-tier-snapshot"
    assert resolved["p1"].contested_with == ["p2"]


def test_child_contest_history_names_the_strongest_tier():
    pytest.importorskip("markdown_it")
    digest = M.body_hash(M.child_body("1. Alpha"), 12)
    before = (
        f"1. Alpha <!-- stay:a1 subhash=sha256:{digest} -->"
        f" <!-- stay:a2 subhash=sha256:{digest} -->\n2. Beta\n"
    )
    container = next(
        block
        for block in M.parse_document(before, child_blocks=True)
        if block.children
    )
    before += f"<!-- stay:P hash=sha256:{M.body_hash(container.content, 12)} -->\n"
    after = before.replace("1. Alpha", "7. Alpha").replace("2. Beta", "8. Beta").replace(
        f" <!-- stay:a1 subhash=sha256:{digest} -->", ""
    ).replace(f" <!-- stay:a2 subhash=sha256:{digest} -->", "")
    sibling = M.resolve_children(M.build_child_anchors(before), after)
    assert sibling["a1"].reason == "contested"
    assert (
        sibling["a1"].proposal_provenance
        == "child-sibling-hash-tier-snapshot"
    )

    parent = M.Anchor("p", "unused", M.Selector("Control"))
    document_anchors = [
        M.ChildAnchor(
            anchor_id,
            M.body_hash("Alpha"),
            M.Selector("Alpha"),
            1,
            parent,
            "not-the-parent-hash",
        )
        for anchor_id in ("d1", "d2")
    ]
    document = M.resolve_children(
        document_anchors,
        "- Control\n- Other\n<!-- stay:p -->\n\n- Alpha\n- Tail\n",
        mode="commonmark",
    )
    assert document["d1"].reason == "contested"
    assert (
        document["d1"].proposal_provenance
        == "child-document-hash-tier-snapshot"
    )

    quote_anchors = [
        M.ChildAnchor(
            anchor_id,
            "not-a-child-hash",
            M.Selector("Alpha"),
            1,
            parent,
            "not-the-parent-hash",
        )
        for anchor_id in ("q1", "q2")
    ]
    quote = M.resolve_children(
        quote_anchors,
        "- Alpha\n- Control\n<!-- stay:p -->\n",
    )
    assert quote["q1"].reason == "contested"
    assert quote["q1"].proposal_provenance == "child-tier-snapshot"


def test_child_contest_history_does_not_block_a_weaker_attachment():
    after = "- Alpha\n- Beta\n<!-- stay:p -->\n"
    parent = M.Anchor("p", "unused", M.Selector("Alpha Beta"))
    parent_hash = M.body_hash("- Alpha\n- Beta")
    anchors = [
        M.ChildAnchor(
            "a1",
            M.body_hash("Alpha"),
            M.Selector("Alpha"),
            1,
            parent,
            parent_hash,
        ),
        M.ChildAnchor(
            "a2",
            M.body_hash("Beta"),
            M.Selector("Beta"),
            1,
            parent,
            parent_hash,
        ),
    ]
    resolved = M.resolve_children(anchors, after)
    assert resolved["a1"].method == "hash"
    assert resolved["a2"].method == "hash"
    assert resolved["a1"].reason is None
    assert resolved["a2"].reason is None


def test_child_detached_reasons_cover_ambiguity_and_unmatched():
    before = stamped("- Same body\n- Control\n")
    anchors = M.build_child_anchors(before)
    same = next(anchor for anchor in anchors if anchor.selector.quote == "Same body")
    parent_marker = before.splitlines()[-1]

    ambiguous_after = "- Same body\n- Same body\n- xxxxx\n" + parent_marker + "\n"
    ambiguous = M.resolve_children([same], ambiguous_after)[same.id]
    assert ambiguous.reason == "ambiguous"
    assert {candidate.target for candidate in ambiguous.candidates} == {0, 1}
    assert all(
        candidate.provenance == "child-tier-snapshot"
        for candidate in ambiguous.candidates
    )
    context_codes = {
        evidence.code
        for candidate in ambiguous.candidates
        for evidence in candidate.evidence
    }
    assert "candidate_suffix_context" in context_codes
    assert all(
        "candidate" in evidence.label
        for candidate in ambiguous.candidates
        for evidence in candidate.evidence
        if evidence.code.endswith("_context")
    )

    unmatched_after = "- xxxxxxxxxxxxx\n- yyyyyyyyyyyyy\n" + parent_marker + "\n"
    unmatched = M.resolve_children([same], unmatched_after)[same.id]
    assert unmatched.reason == "unmatched"
    assert unmatched.candidates == []

    duplicate_before = stamped("- Done\n- Done\n")
    duplicate = M.build_child_anchors(duplicate_before)[0]
    duplicate_parent = duplicate_before.splitlines()[-1]
    duplicate_result = M.resolve_children(
        [duplicate], "- xxxxxxxxxxxxx\n- yyyyyyyyyyyyy\n" + duplicate_parent + "\n"
    )[duplicate.id]
    assert duplicate_result.reason == "unmatched"


def test_child_blocked_by_unmatched_parent_is_unscored_with_the_parent_cause(tmp_path):
    before = stamped("- Deploy alpha service\n")
    anchor = M.build_child_anchors(before)[0]
    after = "- xxxxxxxxxxxxxxxxxxxxx\n"
    result = M.resolve_children([anchor], after)[anchor.id]
    assert result.reason == "unscored"
    assert result.blocked_by is not None
    assert result.blocked_by.reason == "unmatched"

    before_path = tmp_path / "before.md"
    after_path = tmp_path / "after.md"
    before_path.write_text(before)
    after_path.write_text(after)
    command = [
        sys.executable,
        "-m",
        "markstay.cli",
        "resolve",
        "--child-blocks",
        "--json",
        "--before",
        str(before_path),
        str(after_path),
    ]
    payload = json.loads(
        subprocess.run(command, capture_output=True, text=True, check=True).stdout
    )
    structured = next(
        row for row in payload["resolutions"] if row["id"] == anchor.id
    )
    assert structured["blocked_by"]["id"] == result.blocked_by.id
    assert structured["blocked_by"]["reason"] == "unmatched"
    assert structured["blocked_by"]["committed"] is False


def test_identical_siblings_detach_after_one_disappears():
    before = stamped("- Done\n- Done\n- Pending\n")
    first_done = next(line for line in before.splitlines() if line.startswith("- Done"))
    after = before.replace(first_done + "\n", "", 1)
    done_anchors = [
        a for a in M.build_child_anchors(before) if a.selector.quote == "Done"
    ]
    resolved = M.resolve_children(done_anchors, after)
    assert [resolved[anchor.id].method for anchor in done_anchors] == [
        "detached",
        "marker",
    ]
    assert resolved[done_anchors[0].id].reason == "unmatched"


def test_child_restamp_uses_subhash_and_add_missing_does_not_inject_hash():
    before = stamped("- Alpha\n- Beta\n")
    edited = before.replace("- Alpha ", "- Alpha revised ")
    result = M.restamp(edited, child_blocks=True, add_missing=True)
    assert result.refreshed
    alpha = result.text.splitlines()[0]
    assert "subhash=sha256:" in alpha
    assert " hash=sha256:" not in alpha


def test_restamp_add_missing_never_injects_beside_a_subhash():
    # SPEC.md §5.5: the guard is unconditional, so the flag being off is not a
    # licence to write the container's digest onto a child marker.
    before = stamped("- Alpha\n- Beta\n")
    assert M.restamp(before, add_missing=True).text == before


def test_stamp_without_child_blocks_still_stamps_a_child_stamped_container():
    # SPEC.md §16, the shim's second rule, and the half that used to be conditional:
    # a run with child support OFF must not read child markers as evidence the list
    # is stamped, or the container never gets a stay of its own and every child in it
    # resolves through §9.2 tier 4 on weaker evidence. Same document as the
    # child_blocks=True test above, so the two profiles are visibly compared.
    digest = M.body_hash("Alpha", 12)
    md = f"- Alpha <!-- stay:child subhash=sha256:{digest} -->\n"
    result = M.stamp(md, new_id=lambda: "parent")
    assert result.text.count("stay:child") == 1
    assert "stay:parent hash=sha256:" in result.text
    assert [m["id"] for m in result.minted] == ["parent"]


def test_child_restamp_refreshes_the_reserved_key_not_a_custom_one():
    # The write path needs the same whitespace boundary as the read path. Under a word
    # boundary this rewrote the value of `x-subhash`, a §4 custom key, and left the real
    # `subhash` stale: the corruption and the missed refresh are the same bug.
    md = (
        "- Item body <!-- stay:c1 x-subhash=sha256:abcd subhash=sha256:1234 -->\n"
        "<!-- stay:p1 hash=sha256:0000 -->\n"
    )
    result = M.restamp(md, child_blocks=True)
    assert "x-subhash=sha256:abcd" in result.text
    assert "subhash=sha256:1234" not in result.text
    assert "c1" in result.refreshed


def test_custom_key_ending_in_subhash_is_not_the_reserved_key():
    # SPEC.md §4: an attribute is a whitespace-separated token, so the boundary that
    # identifies the reserved key is whitespace. A word boundary would accept
    # `x-subhash`, because a hyphen is not a word character, and the marker would stop
    # counting as its block's stay: a second, spurious stay gets minted onto a block
    # that already has one, and the read path reports a child that is not there.
    md = "A paragraph.\n<!-- stay:x1 x-subhash=sha256:abcd -->\n"
    assert M.find_markers(md)[0].subhash is None
    result = M.stamp(md, new_id=lambda: "spurious")
    assert result.minted == []
    assert result.text == md


def test_child_repair_removes_v11_add_missing_parent_hash_signature():
    # The contamination is written by hand rather than produced by this build's
    # restamp, which no longer emits it (above). What is under test is the
    # repair of a document an older tool already damaged.
    before = stamped("- Alpha\n- Beta\n")
    container = next(
        b for b in M.parse_document(before, child_blocks=True) if b.children
    )
    digest = M.body_hash(container.content)
    contaminated = before.replace(
        "stay:c0 ", f"stay:c0 hash=sha256:{digest} ", 1
    ).replace("stay:c1 ", f"stay:c1 hash=sha256:{digest} ", 1)
    assert "stay:c0 hash=sha256:" in contaminated
    result = M.repair_duplicates(contaminated, child_blocks=True)
    assert result.cleaned == ["c0", "c1"]
    assert "stay:c0 hash=sha256:" not in result.text
    assert "stay:c0 subhash=" in result.text


def test_default_v11_path_remains_unchanged_for_child_carrier():
    md = "- Alpha <!-- stay:child subhash=sha256:dead -->\n- Beta\n"
    blocks, findings = M.lint_document(md)
    assert blocks[0].children == []
    assert findings == []


def test_cli_child_blocks_stamp_then_lint(tmp_path):
    path = tmp_path / "items.md"
    path.write_text("- Alpha\n- Beta\n")
    stamped = subprocess.run(
        [
            sys.executable,
            "-m",
            "markstay.cli",
            "stamp",
            "--child-blocks",
            "-w",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    assert stamped.returncode == 0, stamped.stderr
    assert path.read_text().count("subhash=sha256:") == 2
    linted = subprocess.run(
        [
            sys.executable,
            "-m",
            "markstay.cli",
            "lint",
            "--child-blocks",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    assert linted.returncode == 0, linted.stderr
    assert "clean (no findings)" in linted.stdout


def test_cli_child_contest_names_competing_stays_without_a_candidate_list(tmp_path):
    digest = M.body_hash(M.child_body("- Alpha"), 12)
    before_text = (
        f"- Alpha <!-- stay:a1 subhash=sha256:{digest} -->"
        f" <!-- stay:a2 subhash=sha256:{digest} -->\n- Beta\n"
    )
    container = next(
        block
        for block in M.parse_document(before_text, child_blocks=True)
        if block.children
    )
    before_text += (
        f"<!-- stay:P hash=sha256:{M.body_hash(container.content, 12)} -->\n"
    )
    after_text = before_text.replace(
        f" <!-- stay:a1 subhash=sha256:{digest} -->", ""
    ).replace(f" <!-- stay:a2 subhash=sha256:{digest} -->", "")
    before = tmp_path / "before.md"
    after = tmp_path / "after.md"
    before.write_text(before_text)
    after.write_text(after_text)

    command = [
        sys.executable,
        "-m",
        "markstay.cli",
        "resolve",
        "--child-blocks",
        "--before",
        str(before),
        str(after),
    ]
    structured = subprocess.run(
        command + ["--json"], capture_output=True, text=True, check=True
    )
    resolutions = {
        result["id"]: result for result in json.loads(structured.stdout)["resolutions"]
    }
    for anchor_id, other_id in (("a1", "a2"), ("a2", "a1")):
        result = resolutions[anchor_id]
        assert result["reason"] == "contested"
        assert result["committed"] is False
        assert result["diagnostics"]["candidates"] == []
        assert result["diagnostics"]["contest"]["contested_with"] == [other_id]
        assert "assigned to none" in result["diagnostics"]["contest"]["outcome"]

    rendered = subprocess.run(
        command + ["--show-candidates"], capture_output=True, text=True, check=True
    ).stdout
    assert "contested proposal" in rendered
    assert "assigned to no stay" in rendered
    assert "diagnostic candidate" not in rendered


def test_cli_rejects_hashless_child_stamping_without_traceback(tmp_path):
    path = tmp_path / "items.md"
    path.write_text("- Alpha\n- Beta\n")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "markstay.cli",
            "stamp",
            "--child-blocks",
            "--no-hash",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "requires child subhash evidence" in result.stderr
    assert "Traceback" not in result.stderr


def test_child_anchor_context_is_windowed_to_48_characters():
    """SPEC.md §9's 48-character limit, applied to the child ladder as well.

    `resolve_children` is exported from this package, so the storage asymmetry
    corrected in the block path was shipped here too. The child ladder has no
    conformance vectors (it is opt-in and its spec text is unmerged), so this
    test is what holds the two paths consistent.
    """
    long_sibling = (
        "Ship the linter and then "
        + "wait for the release train " * 3
        + "wait for the release train"
    )
    long_before = "A preceding block far longer than forty-eight characters, easily."
    long_after = "A following block also far longer than forty-eight characters here."
    md = (
        f"{long_before}\n\n"
        f"- {long_sibling}\n"
        f"- Document the command\n\n"
        f"{long_after}\n"
    )
    stamped_md = stamped(md)
    anchors = list(M.build_child_anchors(stamped_md))
    second = next(a for a in anchors if a.selector.quote == "Document the command")
    sibling_body = next(
        a.selector.quote for a in anchors if a.selector.quote.startswith("Ship the")
    )
    assert len(second.selector.prefix) == 48
    assert second.selector.prefix == sibling_body[-48:]
    # The parent selector is block context and IS governed by §9.
    parent = second.parent
    assert len(parent.selector.prefix) == 48
    assert parent.selector.prefix == long_before[-48:]
    assert len(parent.selector.suffix) == 48
    assert parent.selector.suffix == long_after[:48]
    # 48 is a cap, not a fixed width: a short neighbour is stored whole.
    first = next(a for a in anchors if a.selector.quote.startswith("Ship the"))
    assert first.selector.suffix == "Document the command"


def test_a_child_demoted_to_a_nested_item_detaches_rather_than_moving():
    """SPEC.md §5.5: a `subhash` marker inside a nested item is nobody's stay.

    The marker is still in the document, so the ladder must not recover the id
    from weaker evidence: quote scoring lands it on the *enclosing* direct item,
    which is a false attachment onto a bullet the note was never about, and it
    makes the linter (`CHILD_UNADDRESSED`) and the resolver disagree about the
    same id in the same document. Alpha carries no child stay of its own, which
    is what leaves it unclaimed and reachable by that tier.
    """
    beta_hash = M.body_hash("Beta", 12)
    before = (
        "- Alpha\n"
        f"- Beta <!-- stay:bbbb subhash=sha256:{beta_hash} -->\n"
        "<!-- stay:pppp -->\n"
    )
    after = before.replace("- Beta <!--", "  - Beta <!--", 1)
    anchors = M.build_child_anchors(before, mode="commonmark")
    resolved = M.resolve_children(anchors, after, mode="commonmark")
    assert resolved["bbbb"].method == "detached"
    assert resolved["bbbb"].target is None
    assert resolved["bbbb"].reason == "unaddressed"
    _, findings = M.lint_document(after, mode="commonmark", child_blocks=True)
    codes = {f.code for f in findings if f.id == "bbbb"}
    assert "CHILD_UNADDRESSED" in codes


def test_a_nested_copy_does_not_detach_the_marker_that_never_moved():
    """The unaddressed gate is about an id with nowhere to be, not an id with a
    stray copy. The original marker is still where it was; the nested copy is a
    §7 duplicate for the linter to report, not a reason to lose the anchor.
    """
    stayed = f"- Beta <!-- stay:bbbb subhash=sha256:{M.body_hash('Beta', 12)} -->"
    before = (
        f"- Alpha\n{stayed}\n<!-- stay:p1 -->\n\nInterlude.\n\n"
        "- Gamma\n<!-- stay:p2 -->\n"
    )
    after = before.replace(
        "- Gamma\n<!-- stay:p2 -->", f"- Gamma\n  {stayed}\n<!-- stay:p2 -->", 1
    )
    anchors = M.build_child_anchors(before, mode="commonmark")
    resolved = M.resolve_children(anchors, after, mode="commonmark")
    assert resolved["bbbb"].method == "marker"
    assert resolved["bbbb"].target == 1
    _, findings = M.lint_document(after, mode="commonmark", child_blocks=True)
    assert "DUPLICATE_ID" in {f.code for f in findings if f.id == "bbbb"}
