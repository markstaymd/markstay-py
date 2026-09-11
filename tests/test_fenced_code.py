"""SPEC.md §3.3 (v1.5): text inside a fenced code block is content, not markup.

The four symptoms below were all observed on this project's own SPEC.md, which is
the document the rule exists for: a tool's own specification is exactly where
marker examples live.
"""

from __future__ import annotations

from itertools import count

import pytest

import markstay as M


def ids(prefix: str = "n"):
    seq = count()
    return lambda: f"{prefix}{next(seq)}"


# --- fence recognition (§3.3, the line rule) ------------------------------


@pytest.mark.parametrize(
    "md, expected",
    [
        ("a\n```\ncode\n```\nb\n", {2, 3, 4}),
        ("a\n~~~\ncode\n~~~\nb\n", {2, 3, 4}),
        # Up to three leading spaces opens and closes.
        ("a\n   ```\ncode\n   ```\nb\n", {2, 3, 4}),
        # Four does not: that is an indented code block, which §3.3 leaves alone.
        ("a\n    ```\ncode\n    ```\nb\n", set()),
        # A tab is not one of the three spaces: CommonMark expands it against a
        # column model this rule deliberately does not have.
        ("a\n\t```\ncode\n\t```\nb\n", set()),
        # A longer opener contains a shorter run.
        ("````\n```\ninner\n```\n````\n", {1, 2, 3, 4, 5}),
        # A shorter run cannot close a longer one, so this never closes.
        ("````\ncode\n```\nstill code\n", {1, 2, 3, 4, 5}),
        # Different character cannot close.
        ("```\ncode\n~~~\nstill code\n", {1, 2, 3, 4, 5}),
        # An unclosed fence runs to the end of the document.
        ("a\n```\ncode\n", {2, 3, 4}),
        # A closing fence takes space or tab and nothing else after the run.
        ("```\ncode\n``` \nafter\n", {1, 2, 3}),
        ("```\ncode\n```x\nstill code\n", {1, 2, 3, 4, 5}),
        # A backtick fence's info string may not contain a backtick.
        ("a\n```md `x`\nnot a fence\n", set()),
        # A tilde fence's info string may.
        ("a\n~~~md `x`\ncode\n~~~\n", {2, 3, 4}),
    ],
)
def test_code_lines_recognises_the_fences_the_line_rule_can_see(md, expected):
    assert M.code_lines(md) == expected


def test_crlf_and_lf_twins_give_the_same_mask():
    lf = "a\n```\ncode\n```\nb\n"
    assert M.code_lines(lf.replace("\n", "\r\n")) == M.code_lines(lf)


def test_a_fence_carrying_a_blockquote_marker_is_not_covered():
    # §3.3 names this as the rule's real limit rather than hiding it: seeing this
    # fence means knowing the container, which is the parser §5.2 exists to avoid.
    assert M.code_lines("> ```\n> code\n> ```\n") == set()


# --- reading (§3.3 reader rules, §8) --------------------------------------


def test_a_marker_in_a_fence_identifies_no_block():
    md = "Intro.\n\n```md\nThe paragraph.\n<!-- stay:demo hash=sha256:7a9c -->\n```\n"
    blocks = M.parse_document(md)
    assert [b.markers for b in blocks] == [[], []]


def test_a_marker_in_a_fence_is_hashed_with_the_body():
    fence = "```md\nThe paragraph.\n<!-- stay:demo hash=sha256:7a9c -->\n```"
    blocks = M.parse_document(fence + "\n")
    assert "stay:demo" in blocks[0].content
    assert blocks[0].content == M.normalize_body(fence)


def test_two_fences_sharing_an_example_id_are_not_a_duplicate():
    # Symptom 3: SPEC.md reported a DUPLICATE_ID under its own linter that no
    # restamp could clear, because restamp resolves an id to the first block
    # carrying it and hands the second that block's digest.
    md = (
        "```md\n<!-- stay:8f24 hash=sha256:7a9c -->\n```\n\n"
        "```mdx\n{/* stay:8f24 hash=sha256:7a9c */}\n```\n"
    )
    _, findings = M.lint_document(md)
    assert findings == []


def test_a_marker_in_an_opening_fence_info_string_is_not_a_marker():
    # The fence lines are part of the block deliberately: before §3.3 this bound
    # to whatever block preceded it.
    md = "Intro.\n\n~~~md <!-- stay:demo -->\ncode\n~~~\n"
    blocks = M.parse_document(md)
    assert [b.markers for b in blocks] == [[], []]


def test_an_inline_code_span_still_carries_a_marker():
    # §3.3 declines inline spans on purpose: that is the shape pandoc's native
    # markdown writer produces when it mangles a trailing marker, and a mangled
    # marker a tool can still see beats one that has silently stopped existing.
    md = "A line showing `<!-- stay:demo -->` inline.\n"
    (block,) = M.parse_document(md)
    assert [mk.id for mk in block.markers] == ["demo"]


# --- writing (§3.3 writer rules) ------------------------------------------


def test_restamp_leaves_an_illustrative_hash_alone():
    # Symptom 1, and the one that shipped: a restamp of SPEC.md rewrote §3.1's
    # and §3.2's example `hash=` values to the digest of the fence around them.
    md = "```md\nThe paragraph being identified.\n<!-- stay:8f24 hash=sha256:7a9c -->\n```\n"
    result = M.restamp(md)
    assert result.text == md
    assert result.refreshed == []


def test_restamp_leaves_an_example_alone_even_when_the_id_is_live():
    # The mask is load-bearing rather than belt-and-braces: with a real block
    # carrying the same id, the example is reachable through `content_by_id`.
    body = "Live content."
    md = (
        f"{body}\n<!-- stay:demo hash=sha256:{M.body_hash(body, 4)} -->\n\n"
        "```md\n<!-- stay:demo hash=sha256:7a9c -->\n```\n"
    )
    assert M.restamp(md).text == md


def test_a_fence_showing_a_marker_now_gets_a_stay_of_its_own():
    # Symptom 2: the example counted as the fence's stay, so the block a tutorial
    # most wants addressable was the one block that was not.
    md = "```md\nThe paragraph.\n<!-- stay:demo hash=sha256:7a9c -->\n```\n"
    result = M.stamp(md, mode="commonmark", new_id=ids())
    fence, marker = result.text.rstrip("\n").rsplit("\n", 1)
    assert fence == md.rstrip("\n")
    assert marker == f"<!-- stay:n0 hash=sha256:{M.body_hash(fence, 12)} -->"
    assert result.minted == [{"id": "n0", "line": 4}]


def test_the_stamper_refuses_to_write_into_a_listing():
    # Symptom 4, and the worst one: under the baseline segmenter a fence with an
    # internal blank line splits into ordinary blocks, and a stamping run put a
    # real marker inside this specification's own §4 ABNF grammar.
    md = "```text\nfirst = a\n\nsecond = b\n```\n"
    result = M.stamp(md, new_id=ids())
    assert result.text == md
    assert result.minted == []


def test_a_complete_fence_still_takes_its_stay_after_the_closing_fence():
    # The writer rule refuses the insertion *point*, not the block: under §5.2 a
    # fence is one block and is stamped in the ordinary way.
    md = "```text\nfirst = a\n\nsecond = b\n```\n"
    result = M.stamp(md, mode="commonmark", new_id=ids())
    assert result.text.rstrip("\n").split("\n")[-1].startswith(
        "<!-- stay:n0 hash=sha256:"
    )


def test_repair_does_not_rename_an_example_id():
    body = "Live content."
    md = (
        f"{body}\n<!-- stay:demo hash=sha256:{M.body_hash(body, 4)} -->\n\n"
        "```md\n<!-- stay:demo -->\n```\n"
    )
    result = M.repair_duplicates(md, new_id=ids())
    assert result.text == md
    assert result.renamed == []


# --- migration (§3.3, the two shapes) -------------------------------------


def test_migration_one_the_block_drifts_once_and_restamps_clean():
    fence = "```md\nExample.\n<!-- stay:demo -->\n```"
    stale = M.body_hash("```md\nExample.\n\n```", 12)  # what v1.4 hashed
    md = f"{fence}\n<!-- stay:live hash=sha256:{stale} -->\n"
    _, findings = M.lint_document(md)
    assert [f.code for f in findings] == ["HASH_DRIFT"]
    assert M.lint_document(M.restamp(md).text)[1] == []


def test_migration_two_the_block_is_silently_unstamped_and_needs_minting():
    # A restamp does not fix this one: the block needs a stay minted, so it gets a
    # new id rather than a corrected hash, and no linter finding fires on the way
    # because an unstamped block is not an error.
    md = "```md\nExample.\n<!-- stay:demo hash=sha256:7a9c -->\n```\n"
    assert M.lint_document(md)[1] == []
    assert M.restamp(md).text == md
    assert M.stamp(md, mode="commonmark", new_id=ids()).minted == [{"id": "n0", "line": 4}]


# --- the line rule's sharpest limit ---------------------------------------


def test_a_fence_opening_on_a_list_marker_line_swallows_the_rest_of_the_document():
    # §3.3's limit, in the shape that costs the most. The opening fence shares a
    # line with the list marker, so the line scan never sees it open; the closing
    # `  ``` ` is the first fence-shaped line it does see and reads as an
    # *opener*, and with no later fence-shaped line it runs to EOF. Every marker
    # after it goes silent: no drift is reported because nothing is compared, and
    # the block simply looks unstamped. It fails closed rather than corrupting
    # anything, and it is pinned here so a later reader meets it as a decision
    # rather than as a surprise. The variant where a later fence-shaped line
    # *does* close the phantom is the test below.
    md = (
        "- ```txt\n"
        "  code\n"
        "  ```\n"
        "\n"
        "A later paragraph.\n"
        "<!-- stay:live hash=sha256:dead -->\n"
    )
    assert M.code_lines(md) == {3, 4, 5, 6, 7}
    assert all(b.markers == [] for b in M.parse_document(md))
    # Nothing is reported about the swallowed markers. The one finding is §13's
    # subset advisory, since a fence inside a list item is §5.4 case 2.
    assert [f.code for f in M.lint_document(md)[1]] == ["OUTSIDE_SUBSET"]
    assert M.stamp(md, new_id=ids()).minted == []


def test_a_fence_on_its_own_line_inside_a_list_item_is_seen_correctly():
    # The common shape is fine, which is why the case above is a limit rather
    # than a defect in the rule: a balanced pair at one to three spaces of indent
    # opens and closes exactly where CommonMark puts it.
    md = "- item:\n\n  ```py\n  code\n  ```\n\nAfter.\n<!-- stay:live -->\n"
    assert M.code_lines(md) == {3, 4, 5}
    blocks = M.parse_document(md)
    assert [mk.id for b in blocks for mk in b.markers] == ["live"]


# --- child blocks (§5.5) hash by the same §8 rule --------------------------


def test_a_child_body_keeps_a_fenced_marker_the_way_its_container_does():
    # One document must not give two §8 answers. The container's content keeps a
    # fenced example, so the child block holding the same fence has to as well;
    # `child_body` cutting markers code-blind is how they came apart.
    pytest.importorskip("markdown_it")
    md = "- Item:\n\n  ```text\n  <!-- stay:example -->\n  ```\n"
    (block,) = M.parse_document(md, mode="commonmark", child_blocks=True)
    assert "stay:example" in block.content
    assert block.children[0].content == "Item:\n\n```text\n<!-- stay:example -->\n```"


def test_child_stamping_hashes_the_fenced_marker_into_the_subhash():
    pytest.importorskip("markdown_it")
    md = "- Item:\n\n  ```text\n  <!-- stay:example -->\n  ```\n"
    result = M.stamp(md, mode="commonmark", child_blocks=True, new_id=ids())
    body = "Item:\n\n```text\n<!-- stay:example -->\n```"
    assert f"subhash=sha256:{M.body_hash(body, 12)}" in result.text



def test_a_phantom_fence_that_later_closes_still_refuses_the_block_it_straddles():
    # The phantom opener from the case above is closed by the next fence-shaped
    # line, so the mask ends and later lines are ordinary content again. What does
    # not come back is the block that *straddles* the close: it begins inside the
    # phantom fence and ends outside it, and `stamp` refuses it even though the
    # marker would land on an unmasked line.
    #
    # That refusal is the rule working rather than over-reaching, and the case
    # below shows why: the same shape occurs with a perfectly real fence, where
    # the straddling block's body is half a listing and §5.2 already says such a
    # half cannot reliably carry a stay. Refusing on "a fence was open before this
    # block started" is the fail-closed side of a question the line scan cannot
    # answer, not a proxy that happens to be wrong here.
    md = "- ```txt\n  code\n  ```\n\nA paragraph.\n```\nAfter.\n"
    assert M.code_lines(md) == {3, 4, 5, 6}
    assert M.stamp(md, new_id=ids()).minted == []


def test_a_real_blank_line_fence_refuses_the_half_that_straddles_its_close():
    # No phantom anywhere: a genuine fence with an internal blank line, which the
    # baseline segmenter splits. The second half runs past the closing fence into
    # the prose after it, so its body would be `b`, the closing fence, and a line
    # of prose. §3.3: "under the baseline segmenter the halves of such a fence are
    # simply not stampable, which is where §5.2 already arrives."
    md = "```text\na\n\nb\n```\nprose\n"
    assert [b.content for b in M.parse_document(md)] == ["```text\na", "b\n```\nprose"]
    assert M.stamp(md, new_id=ids()).minted == []
    # The same document under §5.2 is a fence and a paragraph, and the fence takes
    # its stay after the closing line in the ordinary way, which is the whole
    # reason that segmenter exists.
    assert M.stamp(md, mode="commonmark", new_id=ids()).minted == [
        {"id": "n0", "line": 5},
        {"id": "n1", "line": 6},
    ]
