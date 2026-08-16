"""Experimental direct list-item identity (PLAN_SUBBLOCK_IDENTITY.md, phase 1)."""

from __future__ import annotations

from itertools import count
import subprocess
import sys

import pytest

import markstay as M


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
    assert result.text.count(" hash=sha256:") == 1  # parent list stay still minted


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
    assert M.resolve_children([middle], reworded)[middle.id].method == "quote"


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


def test_identical_siblings_detach_after_one_disappears():
    before = stamped("- Done\n- Done\n- Pending\n")
    first_done = next(line for line in before.splitlines() if line.startswith("- Done"))
    after = before.replace(first_done + "\n", "", 1)
    done_anchors = [
        a for a in M.build_child_anchors(before) if a.selector.quote == "Done"
    ]
    methods = [M.resolve_children([a], after)[a.id].method for a in done_anchors]
    assert "detached" in methods


def test_child_restamp_uses_subhash_and_add_missing_does_not_inject_hash():
    before = stamped("- Alpha\n- Beta\n")
    edited = before.replace("- Alpha ", "- Alpha revised ")
    result = M.restamp(edited, child_blocks=True, add_missing=True)
    assert result.refreshed
    alpha = result.text.splitlines()[0]
    assert "subhash=sha256:" in alpha
    assert " hash=sha256:" not in alpha


def test_child_repair_removes_v11_add_missing_parent_hash_signature():
    before = stamped("- Alpha\n- Beta\n")
    contaminated = M.restamp(before, add_missing=True).text
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
