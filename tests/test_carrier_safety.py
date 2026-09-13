"""SPEC.md §3.4: a writer refuses a carrier the text beside it could capture.

The two positions this specification makes a marker share a line with content are
§5.5's child carrier and §5.6's row carrier. The rule refuses on the **presence**
of `<`, a backslash or `{` in the container's raw-source prefix, permits only an
id-and-digest marker there, and refuses a flush carrier landing against `*`, `_`
or `~`. It never asks what a character means.

v1.8 adds one scoped exception at the §5.5 child carrier: a `<` that a code span
opened AND closed inside the carrier text is masked before the scan. Rows keep
the v1.7 presence rule exactly.

The row half is pinned by the shared corpus (`conformance/rows`). This file is
where the list half lives, because §16 makes list-item segmentation optional and
only this reference implements it, so there is no profile for it to sit in.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import markstay as M  # noqa: E402
from markstay.lint import code_lines  # noqa: E402
from markstay.stamp import (  # noqa: E402
    _carrier_prefix,
    _code_spans,
    _inert_code_spans,
    _outside_markers,
    plain_marker,
    plain_text_state,
)


def ids(*values):
    it = iter(values)
    return lambda: next(it)


def test_container_marker_follows_trailing_unicode_whitespace_content():
    """§5 blank lines are ASCII-only; Unicode whitespace stays in the body."""
    for mode in ("blank-line", "commonmark"):
        for whitespace in ("\u00a0", "\u2003", "\u202f"):
            doc = f"A.\n{whitespace}\n\nB.\n"
            result = M.stamp(doc, mode=mode, new_id=ids("first", "second"))
            assert f"A.\n{whitespace}\n<!-- stay:first " in result.text
            before = [b.content for b in M.parse_document(doc, mode=mode) if b.index >= 0]
            after = [b.content for b in M.parse_document(result.text, mode=mode) if b.index >= 0]
            assert after == before
            assert not any(
                f.code == "HASH_DRIFT"
                for f in M.lint_document(result.text, mode=mode)[1]
            )


def test_a_capturing_character_refuses_the_child_and_not_the_container():
    doc = "- one <!-- todo\n- two\n"
    result = M.stamp(doc, child_blocks=True, new_id=ids("c1", "c2", "ln"))

    assert result.text == "- one <!-- todo\n- two\n<!-- stay:c1 hash=sha256:4194d089e47b -->\n"
    assert [entry["id"] for entry in result.minted] == ["c1"]
    assert result.refused is None
    assert result.refused_carriers == [
        {"kind": "list", "line": 1},
        {"kind": "list", "line": 2},
    ]


def test_the_scope_reaches_the_whole_container_and_not_the_item():
    """An unclosed construct in one item is still open in the next one.

    The second item's own source is clean, so a child-scoped rule reports safe on
    a document a renderer changes. This is the correction review round 6 forced.
    """
    doc = "- <textarea>a\n- b\n- </textarea>\n"
    result = M.stamp(doc, child_blocks=True, new_id=ids("c1", "c2", "c3", "ln"))

    assert [entry["kind"] for entry in result.refused_carriers] == ["list"] * 3
    assert [entry["id"] for entry in result.minted] == ["c1"]  # the container only


def test_a_stamped_sibling_does_not_refuse_the_next_child():
    """The characters are looked for OUTSIDE the carrier text's own markers.

    Without that, a container refuses every child added to it after the first
    pass: measured over 2417 documents of npm package documentation, 70.9% of
    stamped children would refuse re-stamping, every one of them because of a
    marker markstay itself wrote. Child identity would be single-shot per
    container, and no gate that starts from unmarked text can see it.
    """
    stamped = M.stamp("- one\n- two\n", child_blocks=True, new_id=ids("c1", "c2", "ln"))
    grown = stamped.text.replace("<!-- stay:ln", "- three\n<!-- stay:ln")
    refreshed = M.restamp(grown, child_blocks=True)

    result = M.stamp(refreshed.text, child_blocks=True, new_id=ids("c3"))

    assert result.refused_carriers == []
    assert [entry["id"] for entry in result.minted] == ["c3"]
    assert "- three <!-- stay:c3 subhash=" in result.text


def test_only_a_closed_host_comment_is_masked():
    """`--!>` closes a comment for an HTML parser and not for CommonMark.

    §4 admits a record that ends there, so the bytes reach a reader and the `<`
    in front of them still refuses. Masking every §4 record instead would permit
    a carrier the renderer changes.
    """
    assert _outside_markers("a <!-- stay:x -->") == "a " + " " * 15
    assert _outside_markers("a <!-- stay:hash=1 --!>") == "a <!-- stay:hash=1 --!>"
    assert plain_text_state("a <!-- stay:x -->") is True
    assert plain_text_state("a <!-- stay:hash=1 --!>") is False
    # An ordinary comment is not a marker and is not masked: deciding that one
    # closes itself is the predicate this rule replaced.
    assert plain_text_state("a <!-- a note -->") is False


def test_the_mdx_profile_refuses_a_brace_and_masks_its_own_form():
    assert plain_text_state("a {value", syntax="mdx") is False
    assert plain_text_state("a {value") is True  # HTML profile: `{` is text
    assert plain_text_state("a {/* stay:x */}", syntax="mdx") is True


def test_the_flush_clause_only_binds_a_flush_carrier():
    assert plain_text_state("| a | *Hello!**", flush=True) is False
    assert plain_text_state("- *Hello!** ", flush=False) is True
    for delimiter in ("*", "_", "~"):
        assert plain_text_state(f"| a | x{delimiter}", flush=True) is False


def test_a_marker_may_carry_only_an_id_and_a_digest():
    assert plain_marker("") is True
    assert plain_marker("<!-- stay:a -->") is True
    assert plain_marker("<!-- stay:a hash=sha256:dead -->") is True
    assert plain_marker("<!-- stay:a subhash=sha256:dead -->") is True
    assert plain_marker("<!-- stay:a\tsubhash=sha256:dead -->") is True
    assert plain_marker('<!-- stay:a quote="x" -->') is False
    assert plain_marker("<!-- stay:a x-key=1 -->") is False
    # §4 admits neither LF nor U+00A0 between attributes, so neither does this.
    assert plain_marker("<!-- stay:a\nhash=sha256:dead -->") is False
    assert plain_marker("<!-- stay:a hash=sha256:dead -->") is False
    # The delimiters are paired, not alternated.
    assert plain_marker("<!-- stay:a */}") is False
    assert plain_marker("{/* stay:a */}") is True
    # A marker carrying evidence is refused at a carrier even on clean text.
    assert plain_text_state("- one ", '<!-- stay:a quote="x" -->') is False


def test_the_prefix_ends_where_the_marker_goes():
    lines = ["| a | b |", "|---|---|", "| x | y |"]
    assert _carrier_prefix(lines, 0, 2, "row") == "| a | b |\n|---|---|\n| x | y"
    assert _carrier_prefix(["- one  "], 0, 0, "list") == "- one"
    assert _carrier_prefix(["not a row"], 0, 0, "row") is None
    assert _carrier_prefix(["- one"], 0, 5, "list") is None


def test_a_refused_pass_returns_the_document_it_was_given():
    """Nothing minted means nothing to justify a §5.6 preparation.

    The relocation a row pass makes is provisional, so a document whose only row
    is refused comes back as it arrived rather than with its container stay moved
    for a stamp that never happened.
    """
    doc = (
        "intro <!-- stay:p hash=sha256:6a533207fe88 -->\n"
        "| a | b |\n|---|---|\n| x<y | z |\n"
    )
    result = M.stamp(doc, child_blocks=True, new_id=ids("r1"))

    assert result.text == doc
    assert result.minted == []
    assert result.refused is None
    assert result.refused_carriers == [{"kind": "row", "line": 4}]


def test_refreshing_a_marker_already_inline_is_not_an_insertion():
    """§3.4 binds insertion. `restamp` and duplicate repair rewrite in place.

    Both operate on markers that already share a line with content, in a document
    whose carrier text this rule would refuse, and neither is affected.
    """
    doc = "- one<x <!-- stay:c1 subhash=sha256:0000deadbeef -->\n<!-- stay:ln hash=sha256:dead -->\n"
    refreshed = M.restamp(doc, child_blocks=True, hash_length=12)
    assert "stay:c1 subhash=sha256:" in refreshed.text
    assert refreshed.refreshed == ["c1", "ln"]

    duplicated = doc.replace("stay:ln", "stay:c1")
    repaired = M.repair_duplicates(duplicated, child_blocks=True, new_id=ids("c9"))
    assert [entry["to"] for entry in repaired.renamed] == ["c9"]


def test_a_marker_carrying_evidence_is_not_masked():
    """Round 10's first false accept: the mask is PLAIN, not merely complete.

    An earlier backtick pairs with the one inside the quoted value, so the code
    span ends inside the marker and the `<textarea>` after it is live HTML. The
    same shape reaches GFM's cell splitting through a `|` in such a value. Both
    documents lint clean and both digests are correct, which is what makes them
    the counterexample rather than a malformed-input case.
    """
    item = '- `<!-- stay:x subhash=sha256:8d33 quote="`<textarea>" -->\n- next\n'
    result = M.stamp(item, child_blocks=True, new_id=ids("c1", "c2"))
    assert result.refused_carriers == [{"kind": "list", "line": 2}]
    assert [entry["id"] for entry in result.minted] == ["c1"]  # the container only

    row = (
        "| a | b | c |\n|---|---|---|\n"
        '| a | <!-- stay:x subhash=sha256:2df70e4b1769 quote="| <textarea>" --> | c |\n'
        "| d | e | f |\n"
    )
    assert M.stamp(row, child_blocks=True, new_id=ids("r1", "tb")).refused_carriers == [
        {"kind": "row", "line": 4}
    ]

    assert plain_text_state('a <!-- stay:x quote="`<textarea>" -->') is False
    assert plain_text_state("a <!-- stay:x subhash=sha256:dead -->") is True


def test_either_host_form_is_masked():
    """A §5.6 preparation can relocate the other form, so refusing it would make
    the answer depend on which profile a pass happens to be writing."""
    assert plain_text_state("a {/* stay:x */}") is True
    assert plain_text_state("a <!-- stay:x -->", syntax="mdx") is True
    assert plain_text_state("a {/* stay:x */}", syntax="mdx") is True


def test_a_marker_in_a_fence_is_content_and_not_a_mask():
    """§3.3 decides what a marker is before §3.4 does, in both linters and here."""
    carrier = "- a\n  ```\n  <!-- stay:fake hash=sha256:dead -->\n  ```\n- b"
    assert _outside_markers(carrier) == carrier
    assert plain_text_state(carrier) is False


def test_a_declined_relocation_takes_its_rows_with_it():
    """Round 10's second false accept: a §5.6 preparation is an edit of its own.

    The row is refused by the flush clause, so relocating the container stay out
    of the cell buys nothing and changes what the cell renders as. The rollback
    this replaced was conditioned on nothing being minted ANYWHERE, so stamping
    the unrelated paragraph below was enough to keep the move.
    """
    doc = "| h |\n|---|\n| *Hello!**<!-- stay:p --> |\n\nhello\n"
    result = M.stamp(doc, child_blocks=True, new_id=ids("n1", "n2"))

    assert result.text == (
        "| h |\n|---|\n| *Hello!**<!-- stay:p --> |\n"
        "\nhello\n<!-- stay:n1 hash=sha256:2cf24dba5fb0 -->\n"
    )
    assert [entry["id"] for entry in result.minted] == ["n1"]
    assert result.refused_carriers == [{"kind": "row", "line": 3}]
    assert result.refused is None

    # And with nothing else in the document, so the old rollback would have fired:
    alone = "| h |\n|---|\n| *Hello!**<!-- stay:p --> |\n"
    assert M.stamp(alone, child_blocks=True, new_id=ids("n1")).text == alone


def test_a_refusal_reports_the_line_the_reader_will_see():
    """Coordinates come from the document the caller gets back.

    They pointed into the prepared text while the return value was the original,
    so a refused row on line 4 was reported as line 3, the delimiter row.
    """
    doc = "<!-- stay:p -->\n| h |\n|---|\n| x<y |\n"
    result = M.stamp(doc, child_blocks=True, new_id=ids("n1", "n2"))
    assert result.text == doc
    assert result.refused_carriers == [{"kind": "row", "line": 4}]


def test_a_relocation_is_refused_by_the_position_it_empties():
    """§3.4's question from the other side, found in review round 12.

    A row the writer can stamp does not make removing a marker from another cell
    safe: the header here renders literally because the delimiter run is followed
    by `<`, and moving the stay out makes the emphasis appear. The second document
    is the same defect through the marker's own bytes, where a code span opens
    before it and closes inside its quoted value.
    """
    header = "| *Hello!**<!-- stay:p --> |\n|---|\n| next |\n"
    result = M.stamp(header, child_blocks=True, new_id=ids("n1"))
    assert result.text == header
    assert result.minted == []
    assert result.refused_carriers == [{"kind": "row", "line": 3}]

    span = '| a |\n|---|\n| `<!-- stay:p quote="`x" --> |\n'
    assert M.stamp(span, child_blocks=True, new_id=ids("n1")).text == span

    pipe = '| a | b |\n|---|---|\n| x | <!-- stay:p quote="x|y" --> |\n'
    assert M.stamp(pipe, child_blocks=True, new_id=ids("n1")).text == pipe


def test_a_duplicate_id_does_not_spread_one_table_refusal_to_another():
    """§7 duplicates are an error `stamp` does not refuse, so they must not steer it.

    Keyed on marker ids, the declined set reached the second table, and the
    candidate lookup could approve a relocation for rows it never saw. Both
    tables carry `stay:p` here; only the first is refused, in either order.
    """
    first = (
        "| a |\n|---|\n| *Hello!**<!-- stay:p --> |\n\n"
        "| a |\n|---|\n| x |\n<!-- stay:p -->\n"
    )
    result = M.stamp(first, child_blocks=True, new_id=ids("n1", "n2"))
    assert [entry["id"] for entry in result.minted] == ["n1"]
    assert result.refused_carriers == [{"kind": "row", "line": 3}]
    assert "| x<!-- stay:n1 subhash=" in result.text

    swapped = (
        "| a |\n|---|\n| x |\n<!-- stay:p -->\n\n"
        "| a |\n|---|\n| *Hello!**<!-- stay:p --> |\n"
    )
    other = M.stamp(swapped, child_blocks=True, new_id=ids("n1", "n2"))
    assert [entry["id"] for entry in other.minted] == ["n1"]
    assert other.refused_carriers == [{"kind": "row", "line": 8}]
    assert "| *Hello!**<!-- stay:p --> |" in other.text


def test_identical_rows_do_not_identify_a_relocation_candidate():
    good = "| h |\n|---|\n| x |\n<!-- stay:p -->\n"
    bad = "| <!-- stay:p --><textarea>h |\n|---|\n| x |\n"
    for mode in ("blank-line", "commonmark"):
        for doc in (good + "\n" + bad, bad + "\n" + good):
            result = M.stamp(doc, mode=mode, child_blocks=True, new_id=ids("row"))
            assert bad in result.text
            assert len(result.minted) == 1
            assert len(result.refused_carriers) == 1


def test_identical_content_does_not_share_a_container_refusal():
    pairs = (
        ("| *Hello!**<!-- stay:p --> |\n|---|\n| x |\n",
         "| <!-- stay:q -->*Hello!** |\n|---|\n| x |\n"),
        ("| h <!-- stay:p x-note=legacy --> |\n|---|\n| x |\n",
         "| h  |\n|---|\n| x |\n<!-- stay:q -->\n"),
    )
    for bad, good in pairs:
        for mode in ("blank-line", "commonmark"):
            for tables in (bad + "\n" + good, good + "\n" + bad):
                result = M.stamp(tables + "\nUnrelated.\n", mode=mode,
                                 child_blocks=True, new_id=ids("row", "para"))
                assert bad in result.text
                assert len(result.minted) == 2
                assert "| x<!-- stay:row subhash=" in result.text
                assert len(result.refused_carriers) == 1
                refused_line = result.refused_carriers[0]["line"]
                assert result.text.splitlines()[refused_line - 1] == "| x |"


def test_relocation_cannot_erase_nonplain_snapshot_evidence():
    blocked = "| h <!-- stay:p x-note=legacy --> |\n|---|\n| x |\n"
    partial = "| h |\n|---|\n| x |\n| y<!-- stay:p x-note=legacy --> |\n| z |\n"
    suffix = "| h |\n|---|\n| x |<!-- stay:p x-note=legacy -->\n"
    for mode in ("blank-line", "commonmark"):
        result = M.stamp(blocked, mode=mode, child_blocks=True, new_id=ids("row"))
        assert result.text == blocked
        assert result.minted == []
        assert result.refused_carriers == [{"kind": "row", "line": 3}]
        # A row before the evidence stays writable, while later rows read the
        # original prefix even after the container's evidence has moved away.
        result = M.stamp(partial, mode=mode, child_blocks=True, new_id=ids("row"))
        assert "| x<!-- stay:row subhash=" in result.text
        assert result.refused_carriers == [
            {"kind": "row", "line": 4}, {"kind": "row", "line": 5}
        ]
        result = M.stamp(suffix, mode=mode, child_blocks=True, new_id=ids("row"))
        assert len(result.minted) == 1
        assert result.refused_carriers == []
        assert result.text.endswith("\n<!-- stay:p x-note=legacy -->\n")


def test_declined_legacy_suffix_reports_rows_visible_only_in_the_probe():
    doc = "| h |\n|---|\n| *Hello!** |<!-- stay:p -->\n"
    for mode in ("blank-line", "commonmark"):
        result = M.stamp(doc, mode=mode, child_blocks=True, new_id=ids("row"))
        assert result.text == doc
        assert result.minted == []
        assert result.refused_carriers == [{"kind": "row", "line": 3}]


def test_refusal_lines_follow_insertions_before_the_declined_table():
    table = "| h <!-- stay:p x-note=legacy --> |\n|---|\n| x |\n"
    for mode in ("blank-line", "commonmark"):
        result = M.stamp("Hello.\n\n" + table, mode=mode, child_blocks=True,
                         new_id=ids("intro"))
        assert result.refused_carriers == [{"kind": "row", "line": 6}]
        assert result.text.splitlines()[5] == "| x |"


# §3.4 v1.8: the code-span clause at a §5.5 child carrier.


def test_a_closed_code_span_neutralises_the_capturing_character():
    """The clause's whole purpose, and the consumer's case verbatim.

    `<repo>` in a tracker's action line is CLI metasyntax inside a code span. The
    span binds before raw inline HTML, so the `<` opens nothing.
    """
    assert plain_text_state("- run `git -C <repo> status` first") is True
    assert plain_text_state("- see `<style scoped>` and `<div`") is True


def test_an_unclosed_code_span_neutralises_nothing():
    """A run with no equal-length partner opens no span that closes here, so the
    `<` after it is still live and the presence rule still refuses."""
    assert plain_text_state("- run `git -C <repo> status") is False
    assert plain_text_state("- ``a ` b <c") is False


def test_a_backslash_is_refused_before_the_clause_runs():
    """The ordering the clause depends on: a carrier holding a backslash never
    reaches the backtick scan, which is what keeps that scan unambiguous. An
    escaped backtick therefore cannot change which runs pair."""
    assert plain_text_state("- a `b` c \\") is False
    assert plain_text_state("- a \\` b `") is False


def test_a_row_carrier_keeps_the_presence_rule():
    """Rows are excluded on purpose: GFM splits cells before inline parsing, so a
    lexical backtick scan pairs across a `|` where a renderer does not."""
    assert plain_text_state("| a | `<div>` ", flush=True) is False
    assert plain_text_state("| a | `<div>` ", flush=False) is True


def test_the_flush_guard_survives_the_clause():
    """A flush carrier landing against a delimiter run is still refused, and the
    clause must not mask the byte the guard reads.

    This is the shape `eval/write_safety/carrier_cost.py` lost by calling
    `plain_text_state` without `flush`.
    """
    assert plain_text_state("| star | \u2605 | *", flush=True) is False


def test_an_unbalanced_line_ends_the_scan():
    """The round-8 false accept, found by probing a review arm's direction.

    CommonMark pairs backtick runs sequentially across a whole paragraph, so one
    leftover run on an earlier line takes the next line's first run as its
    closer and shifts every pairing after it. Per-line pairing would instead
    pair that next line's two runs with each other and mask what lies between,
    which CommonMark leaves literal. Appending a marker to the second carrier
    below changed its rendering before this fix.
    """
    assert plain_text_state("- a `\n  b ` <div ` c") is False
    assert plain_text_state("- a `\n  b ` <!-- ` c") is False
    assert plain_text_state("- x `\n  y ` <textarea ` z") is False


def test_spans_before_an_unbalanced_line_still_stand():
    """Only lines from the ambiguity onward are poisoned. A balanced earlier line
    agrees with CommonMark's sequential scan exactly, because nothing is left
    over to carry into it."""
    assert plain_text_state("- a `x<y` z\n- b plain") is True
    assert plain_text_state("- a `x<y` z\n- b ` <div") is False


def test_pairing_never_crosses_a_line():
    """The safety property. A carrier text is the container's prefix, so for a
    list it spans earlier items, which are separate blocks. Backticks in two
    different blocks pair for a lexical scan and not for a renderer, so masking
    across them would hide a live `<`. Refusing instead is the safe direction."""
    assert plain_text_state("- a `open\n- b <c close` d") is False
    assert plain_text_state("- a `open\n- b <c") is False
    assert plain_text_state("- a `x<y` z\n- b plain") is True


def test_masking_preserves_every_offset():
    """Masked rather than deleted, so a trailing backslash is still trailing and
    the flush clause still reads the real last byte."""
    text = "- a `x<y` z"
    masked = _inert_code_spans(text)
    assert len(masked) == len(text)
    assert masked == "- a `   ` z"


def test_a_span_inside_a_fence_masks_nothing():
    """§3.3 decides what a fence is before §3.4 reads it, so a `<` shown inside
    one stays visible to the capture scan even when the line it sits on holds a
    perfectly good pair of backticks around it.

    A BACKTICK fence needs no help here: its delimiter is a lone run on its own
    line, so the balance rule ends the scan before the fence body is reached.
    A TILDE fence contributes no backtick runs at all, so its body looks like
    ordinary balanced text, and §3.3's line set is the only thing standing
    between a span shown inside it and the mask. That is the case this pins.
    """
    tilde = "- a\n  ~~~\n  `<div>` shown\n  ~~~\n- b"
    assert _code_spans(tilde) != []              # unaware of §3.3, it masks
    assert _code_spans(tilde, code_lines(tilde)) == []
    assert plain_text_state(tilde) is False

    backtick = "- a\n  ```\n  `<div>` shown\n  ```\n- b"
    assert _code_spans(backtick) == []           # the balance rule already stops it
    assert plain_text_state(backtick) is False
