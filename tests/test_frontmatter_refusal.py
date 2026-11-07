"""SPEC.md §5.3 (v1.11): a writer does not write into a frontmatter-shaped span.

A leading `---` span that recognition rejects only because its payload holds a
blank line, while a payload line still reads as YAML, is content to every reader
and YAML to a frontmatter reader. `stamp` inserts no marker on lines 1 to n and
mints no stay for a block that starts there. The text-and-minted half is pinned by
the shared corpus (`conformance/spec/stamp.json`); this file holds the reporting
surface, the two segmenters, child blocks and the row-preparation backstop, which
the corpus cannot reach because JS and Rust have no §5.2 or §5.5.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import markstay as M  # noqa: E402
from markstay.cli import main  # noqa: E402
from markstay.lint import (  # noqa: E402
    _blank_frontmatter,
    frontmatter_write_refusal,
    parse_document,
)

BLANK_PAYLOAD_SPAN = "---\nname: demo\n\ndescription: two\n---\n\nBody text.\n"


def ids(*values):
    it = iter(values)
    return lambda: next(it)


def test_helper_names_the_closer_only_for_the_rejected_yaml_shape():
    assert frontmatter_write_refusal(BLANK_PAYLOAD_SPAN) == 5
    assert frontmatter_write_refusal(BLANK_PAYLOAD_SPAN.replace("\n---\n\nBody", "\n...\n\nBody")) == 5
    # Recognised frontmatter is blanked by the reader, so nothing is refused.
    assert frontmatter_write_refusal("---\nname: demo\n---\n\nBody.\n") is None
    # No YAML-ish payload line: two thematic breaks around a paragraph.
    assert frontmatter_write_refusal("---\n\nIntro.\n\n---\n\nBody.\n") is None
    # No closer, no opener, an empty payload.
    assert frontmatter_write_refusal("---\nname: demo\n\nBody.\n") is None
    assert frontmatter_write_refusal("Intro.\n---\nname: a\n\nb: c\n---\n") is None
    assert frontmatter_write_refusal("---\n---\n\nBody.\n") is None
    # A blank line made only of ASCII whitespace counts; a non-ASCII one does not.
    assert frontmatter_write_refusal("---\nname: a\n \t\nb: c\n---\n") == 5
    assert frontmatter_write_refusal("---\nname: a\n \nb: c\n---\n") is None


def test_recognition_and_parse_do_not_move():
    # The reader still segments the span as content, block for block.
    assert _blank_frontmatter(BLANK_PAYLOAD_SPAN) == BLANK_PAYLOAD_SPAN
    blocks = [b for b in parse_document(BLANK_PAYLOAD_SPAN) if b.index >= 0]
    assert [b.line for b in blocks] == [1, 4, 7]


def test_stamp_reports_the_span_and_stamps_the_rest():
    r = M.stamp(BLANK_PAYLOAD_SPAN, new_id=ids("n0"))
    assert r.refused_frontmatter == {"start": 1, "end": 5}
    assert r.refused is None and r.refused_carriers == []
    assert r.minted == [{"id": "n0", "line": 7}]
    assert r.text.splitlines()[:5] == BLANK_PAYLOAD_SPAN.splitlines()[:5]


def test_no_report_when_the_span_withheld_nothing():
    # Every block that starts in the span already carries a stay.
    doc = (
        "---\nname: demo\n<!-- stay:aa -->\n\ndescription: two\n---\n"
        "<!-- stay:bb -->\n\nBody.\n"
    )
    r = M.stamp(doc, new_id=ids("n0"))
    assert r.refused_frontmatter is None
    assert r.minted == [{"id": "n0", "line": 9}]
    # And no report at all on a document without the shape.
    assert M.stamp("Body.\n", new_id=ids("n1")).refused_frontmatter is None


def test_restamp_refreshes_a_marker_in_the_span():
    doc = "---\nname: demo\n<!-- stay:aa hash=sha256:0000 -->\n\nb: c\n---\n"
    r = M.restamp(doc)
    assert r.refreshed == ["aa"]
    assert "hash=sha256:0000" not in r.text


def test_tree_segmentation_stamps_a_block_that_runs_on_from_the_closer():
    """The stated segmenter difference: §5.1 refuses the joined chunk whole,
    §5.2 sees the line after the closer as a block of its own."""
    doc = "---\nname: demo\n\ndescription: two\n---\nBody right after.\n"
    blank = M.stamp(doc, new_id=ids("n0"))
    assert blank.minted == [] and blank.text == doc
    assert blank.refused_frontmatter == {"start": 1, "end": 5}
    tree = M.stamp(doc, new_id=ids("n0"), mode="commonmark")
    assert tree.minted == [{"id": "n0", "line": 6}]
    assert tree.text.splitlines()[:5] == doc.splitlines()[:5]
    assert tree.refused_frontmatter == {"start": 1, "end": 5}


def test_child_blocks_in_the_span_are_withheld():
    doc = "---\n- a\n- b\n\nmore: v\n---\n\n- c\n- d\n"
    for mode in ("blank-line", "commonmark"):
        r = M.stamp(doc, new_id=ids("n0", "n1", "n2", "n3"), mode=mode, child_blocks=True)
        assert r.text.splitlines()[:6] == doc.splitlines()[:6], mode
        assert all(entry["line"] > 6 for entry in r.minted), (mode, r.minted)
        assert r.refused_frontmatter == {"start": 1, "end": 6}
        # The list after the closer gets its container stay and both child stays.
        assert len(r.minted) == 3, (mode, r.minted)


def test_a_row_preparation_that_would_move_a_marker_in_the_span_refuses_the_write():
    """§5.6 preparation moves a legacy row-suffix container marker to its own
    line. Inside the span that is an insertion, so the whole write is refused
    and the source comes back byte for byte."""
    doc = (
        "---\n| key: a | b |\n| --- | --- |\n| x | y | <!-- stay:tbl -->\n\n"
        "more: v\n---\n\nBody.\n"
    )
    r = M.stamp(doc, new_id=ids("n0", "n1", "n2"), child_blocks=True)
    assert r.refused == "frontmatter-span"
    assert r.text == doc and r.minted == []
    assert r.refused_frontmatter == {"start": 1, "end": 7}


def test_cli_reports_the_span_and_exits_zero(tmp_path, capsys):
    path = tmp_path / "doc.md"
    path.write_text(BLANK_PAYLOAD_SPAN)
    assert main(["stamp", str(path)]) == 0
    err = capsys.readouterr().err
    assert f"{path}: frontmatter-shaped span L1-L5 refused under §5.3, left unstamped" in err
    assert f"{path}: 1 id(s) minted" in err
    plain = tmp_path / "plain.md"
    plain.write_text("Body.\n")
    assert main(["stamp", str(plain)]) == 0
    assert "frontmatter-shaped" not in capsys.readouterr().err
