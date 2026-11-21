"""SPEC.md §3.5 (v1.12): a closed inline code span is content, not markup.

The case that opened it: a review quoted one example marker twice on a line, and
every tool reading the file reported a duplicate id that no restamp could clear.
The rule is a line scan and is allowed to read MORE markers than a renderer does,
never fewer, so most cases below pin where it stops: each one is a document where
CommonMark could read the marker as a live comment.
"""

from __future__ import annotations

from itertools import count

import pytest

import markstay as M
from markstay import lint as L


def ids(prefix: str = "n"):
    seq = count()
    return lambda: f"{prefix}{next(seq)}"


def block_ids(md: str, **kwargs) -> list[list[str]]:
    return [[mk.id for mk in b.markers] for b in M.parse_document(md, **kwargs)]


# --- what the span mask hides ----------------------------------------------


@pytest.mark.parametrize(
    "md",
    [
        "Shows `<!-- stay:demo -->` inline.\n",
        "Shows `{/* stay:demo */}` inline.\n",
        "Shows ``a ` <!-- stay:demo --> b`` with a longer run.\n",
        "Shows `<!-- stay:demo -->` and `<!-- stay:demo -->` twice.\n",
        "- an item quoting `<!-- stay:demo -->` here\n",
        "> a quote showing `<!-- stay:demo -->` here\n",
        "Shows `<!-- stay:demo -->` inline.\r\n",
    ],
)
def test_a_marker_in_a_closed_span_is_content(md):
    assert block_ids(md) == [[]]


def test_two_identical_quotes_are_not_a_duplicate():
    md = "Quote `<!-- stay:Nested01 -->` and `<!-- stay:Nested01 -->` here.\n"
    assert M.lint_document(md)[1] == []


def test_a_quoted_marker_is_hashed_with_the_body():
    body = "Shows `<!-- stay:demo -->` inline."
    good = f"{body}\n<!-- stay:live hash=sha256:{M.body_hash(body, 12)} -->\n"
    assert M.lint_document(good)[1] == []
    stripped = "Shows `` inline."
    old = f"{body}\n<!-- stay:live hash=sha256:{M.body_hash(stripped, 12)} -->\n"
    assert [f.code for f in M.lint_document(old)[1]] == ["HASH_DRIFT"]


def test_a_real_marker_beside_a_span_still_counts():
    assert block_ids("Shows `code` beside it. <!-- stay:live -->\n") == [["live"]]
    md = "Shows `<!-- stay:demo -->` then a real one. <!-- stay:live -->\n"
    assert block_ids(md) == [["live"]]


def test_the_mask_is_by_column_not_by_line():
    md = "Shows `<!-- stay:demo -->` then <!-- stay:live -->\n"
    code = M.code_lines(md)
    assert set(code) == set() and bool(code)
    markers = M.find_markers(md)
    assert [L.marker_outside_code(mk, code) for mk in markers] == [False, True]


# --- where the scan stops, so the marker stays live -------------------------


@pytest.mark.parametrize(
    "md, expected",
    [
        # pandoc's native writer: raw-inline HTML, not code.
        ("Shows `<!-- stay:demo -->`{=html} raw.\n", [["demo"]]),
        # GFM splits a table row's cells before any inline scan.
        ("Shows `a | <!-- stay:demo -->` with a pipe.\n", [["demo"]]),
        # An escaped backtick opens nothing for CommonMark.
        ("Shows \\`<!-- stay:demo -->` escaped.\n", [["demo"]]),
        # A run left open on one line may close on the next and re-pair it.
        ("A lone ` here\nthen `<!-- stay:demo -->` there.\n", [["demo"]]),
        # An HTML tag or autolink opening first outranks a code span.
        ('Text <a title="`"> `<!-- stay:demo -->`\n', [["demo"]]),
        ("Text <http://x.y/`> `<!-- stay:demo -->`\n", [["demo"]]),
        # A backtick inside a marker-shaped string stops the scan, as an opener or
        # as a closer: markdown-it-py does not always close a span inside a comment.
        ('Shows `<!-- stay:demo quote="`" --> after.\n', [["demo"]]),
        # A backtick inside a marker-shaped string stops the scan.
        ('A <!-- stay:live quote="`" --> then `<!-- stay:demo -->`\n', [["live", "demo"]]),
        # A line starting with a comment is an HTML block: no inline content.
        ("Para.\n<!-- stay:live --> `<!-- stay:demo -->`\n", [["live", "demo"]]),
    ],
)
def test_the_scan_leaves_a_possibly_live_marker_alone(md, expected):
    assert block_ids(md) == expected


def test_a_blank_line_restarts_the_scan():
    md = "A lone ` here\n\nthen `<!-- stay:demo -->` there.\n"
    assert block_ids(md) == [[], []]


def test_spans_before_a_stop_on_the_same_line_stand():
    md = "Shows `<!-- stay:demo -->` then <b> `<!-- stay:demo -->`\n"
    assert block_ids(md) == [["demo"]]


def test_a_run_on_html_block_runs_across_blank_lines():
    md = "Intro.\n\n<pre>\n\n`<!-- stay:demo -->`\n\n</pre>\n\nAfter `<!-- stay:x -->`.\n"
    assert block_ids(md) == [[], [], ["demo"], [], []]


def test_fence_lines_neither_pair_nor_get_scanned():
    md = "Intro `x\n```\ncode\n```\nthen `<!-- stay:demo -->`.\n"
    assert block_ids(md) == [[]]


# --- writing ----------------------------------------------------------------


def test_stamp_gives_a_paragraph_quoting_a_marker_a_stay_of_its_own():
    md = "Shows `<!-- stay:demo hash=sha256:7a9c -->` inline.\n"
    out = M.stamp(md, new_id=ids())
    body = "Shows `<!-- stay:demo hash=sha256:7a9c -->` inline."
    assert out.text == f"{md}<!-- stay:n0 hash=sha256:{M.body_hash(body, 12)} -->\n"


def test_restamp_leaves_a_quoted_example_alone():
    md = (
        "Shows `<!-- stay:demo hash=sha256:7a9c -->` inline.\n"
        "<!-- stay:live hash=sha256:0000 -->\n"
    )
    out = M.restamp(md)
    assert "`<!-- stay:demo hash=sha256:7a9c -->`" in out.text
    assert out.text.count("hash=sha256:7a9c") == 1
    assert M.lint_document(out.text)[1] == []


def test_repair_leaves_two_identical_quotes_alone():
    md = "Quote `<!-- stay:demo -->` and `<!-- stay:demo -->` here.\n"
    assert M.repair_duplicates(md, new_id=ids()).text == md


def test_a_quoted_marker_in_a_row_cell_is_hashed_with_the_row():
    # §3.5 holds for a §5.6 row too: the quote stays an opaque token for finding
    # cell boundaries, and stays in the cell text the row hashes over.
    md = (
        "| H |\n| --- |\n"
        "| a `<!-- stay:QUOTE -->` b<!-- stay:row subhash=sha256:5e33ab145a37 --> |\n"
        "<!-- stay:table -->\n"
    )
    bodies = [
        [c.content for b in M.parse_document(md.replace("QUOTE", q), child_blocks=True)
         for c in b.children]
        for q in ("quoted", "changed")
    ]
    assert bodies == [["a `<!-- stay:quoted -->` b"], ["a `<!-- stay:changed -->` b"]]


def test_code_mask_inequality_against_a_non_set():
    code = M.code_lines("```\nx\n```\n")
    assert code != None and code != [] and code != {1}  # noqa: E711
    assert not (code != {1, 2, 3}) and code == {1, 2, 3}
