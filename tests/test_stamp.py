"""Write-path tests (SPEC.md §3 / §4 / §6 / §7 / §8): id minting, marker
serialization, stamping an unmarked corpus, hash refresh, and duplicate repair.

Mirrors the JavaScript reference's ``test/stamp.test.js``. The strong invariants
checked here are: stamping never changes block bodies, the result lints clean, and
every write op is idempotent.
"""

from __future__ import annotations

import pytest

import markstay as M


def counter(prefix: str = "id"):
    """Deterministic id factory for reproducible assertions. Collision-avoidance
    in the write helpers wraps this, so plain sequential ids are fine."""
    n = 0

    def nxt() -> str:
        nonlocal n
        s = f"{prefix}{n:02d}"
        n += 1
        return s

    return nxt


def bodies(md: str) -> list[str]:
    return [b.content for b in M.parse_document(md) if b.index >= 0]


def error_codes(md: str) -> list[str]:
    _, findings = M.lint_document(md)
    return [f.code for f in findings if f.level == "error"]


# --- mint_id (§6) ---------------------------------------------------------


def test_mint_id_default_charset_and_length():
    for _ in range(200):
        i = M.mint_id()
        assert len(i) == 8
        assert M.ID_CHARSET.match(i), f"{i} not in charset"


def test_mint_id_injectable_byte_source_is_deterministic():
    zeros = lambda k: bytes(k)  # every byte 0 -> alphabet[0] = 'A'
    assert M.mint_id(random=zeros) == "AAAAAAAA"
    assert M.mint_id(length=3, random=zeros) == "AAA"


def test_mint_id_rejects_degenerate_params():
    with pytest.raises(ValueError):
        M.mint_id(length=0)
    with pytest.raises(ValueError):
        M.mint_id(alphabet="x")


# --- format_attr_value / format_marker (§3 / §4) --------------------------


def test_format_attr_value_bare_vs_quoted_with_escaping():
    assert M.format_attr_value("sha256:7a9c") == "sha256:7a9c"
    assert M.format_attr_value("two words") == '"two words"'
    assert M.format_attr_value('a"b\\c') == '"a\\"b\\\\c"'


def test_format_marker_html_and_mdx_round_trip():
    html = M.format_marker("8f24", hash="7a9c")
    assert html == "<!-- stay:8f24 hash=sha256:7a9c -->"
    mdx = M.format_marker("8f24", hash="7a9c", syntax="mdx")
    assert mdx == "{/* stay:8f24 hash=sha256:7a9c */}"
    for raw in (html, mdx):
        mk = M.find_markers(raw)[0]
        assert mk.id == "8f24"
        assert mk.hash == "7a9c"
        assert mk.malformed is False


def test_format_marker_extension_attrs_and_uppercase_hash_folds_lower():
    m = M.format_marker("x1", hash="ABCD", attrs={"x-acme-note": "hi there"})
    assert m == '<!-- stay:x1 hash=sha256:abcd x-acme-note="hi there" -->'


def test_format_marker_rejects_bad_id_nonhex_hash_and_terminators():
    with pytest.raises(ValueError):
        M.format_marker("bad id")
    with pytest.raises(ValueError):
        M.format_marker("ok", hash="zz")
    with pytest.raises(ValueError):
        M.format_marker("ok", attrs={"x-k": "a-->b"})
    with pytest.raises(ValueError):
        M.format_marker("ok", attrs={"x-k": "a*/}b"}, syntax="mdx")


def test_format_attr_value_rejects_chars_outside_qchar_set():
    # §4 qchar is printable ASCII only; newline/tab/control/non-ASCII have no form
    with pytest.raises(ValueError):
        M.format_marker("x", attrs={"x-v": "line\nbreak"})
    with pytest.raises(ValueError):
        M.format_attr_value("tab\there")
    with pytest.raises(ValueError):
        M.format_attr_value("café")  # non-ASCII


# --- stamp (§5 / §6 / §8) -------------------------------------------------

DOC = "# Title\n\nFirst paragraph.\n\nSecond paragraph.\n\n- a\n- b\n"


def test_stamp_marks_every_unmarked_block_bodies_unchanged_lints_clean():
    before = bodies(DOC)
    res = M.stamp(DOC, new_id=counter())
    assert len(res.minted) == len(before)  # one id per content block
    assert bodies(res.text) == before  # bodies untouched
    assert error_codes(res.text) == []  # clean
    for b in M.parse_document(res.text):
        if b.index < 0:
            continue
        ids = [mk for mk in b.markers if mk.id and not mk.malformed]
        assert len(ids) == 1


def test_stamp_canonical_trailing_shape_with_fresh_hash():
    res = M.stamp("Hello world.", new_id=lambda: "abc12345")
    assert res.text == (
        f"Hello world.\n<!-- stay:abc12345 hash=sha256:{M.body_hash('Hello world.', 12)} -->"
    )


def test_stamp_idempotent_leaves_already_marked_alone():
    once = M.stamp(DOC, new_id=counter("a")).text
    twice = M.stamp(once, new_id=counter("b"))
    assert len(twice.minted) == 0
    assert twice.text == once


def test_stamp_marker_only_chunk_after_block_identifies_it():
    md = "Para body.\n\n<!-- stay:keep hash=sha256:0000 -->\n\nOther."
    res = M.stamp(md, new_id=lambda: "new0")
    assert len(res.minted) == 1  # only "Other." is unmarked
    assert res.minted[0]["id"] == "new0"
    assert "stay:keep" in res.text


def test_stamp_minted_ids_never_collide_with_existing():
    md = "A.\n<!-- stay:id00 -->\n\nB."
    proposals = iter(["id00", "id00", "id01"])  # factory re-proposes id00, must skip
    res = M.stamp(md, new_id=lambda: next(proposals))
    assert len(res.minted) == 1
    assert res.minted[0]["id"] == "id01"


def test_stamp_mdx_syntax_and_no_hash():
    res = M.stamp("Body.", new_id=lambda: "m1", syntax="mdx", hash=False)
    assert res.text == "Body.\n{/* stay:m1 */}"


def test_stamp_hash_length_controls_precision():
    res = M.stamp("Body.", new_id=lambda: "h1", hash_length=4)
    mk = M.find_markers(res.text)[0]
    assert len(mk.hash) == 4
    assert mk.hash == M.body_hash("Body.", 4)


def test_stamp_commonmark_fence_with_blank_line_gets_one_trailing_marker():
    pytest.importorskip("markdown_it")
    md = "```txt\nalpha\n\nbeta\n```\n"
    res = M.stamp(md, new_id=lambda: "fence01", hash=False, mode="commonmark")
    assert res.text == "```txt\nalpha\n\nbeta\n```\n<!-- stay:fence01 -->\n"
    blocks = [b for b in M.parse_document(res.text, mode="commonmark") if b.index >= 0]
    assert len(blocks) == 1
    assert [m.id for m in blocks[0].markers] == ["fence01"]


def test_restamp_commonmark_hashes_whole_fence_with_blank_line():
    pytest.importorskip("markdown_it")
    md = "```txt\nalpha\n\nbeta\n```\n<!-- stay:fence01 hash=sha256:0000 -->\n"
    res = M.restamp(md, hash_length=4, mode="commonmark")
    block = [b for b in M.parse_document(md, mode="commonmark") if b.index >= 0][0]
    assert res.refreshed == ["fence01"]
    assert f"hash=sha256:{M.body_hash(block.content, 4)}" in res.text


# --- restamp (§8) ---------------------------------------------------------


def test_restamp_refreshes_drifted_hash_then_lints_clean():
    stamped = M.stamp("Original body.", new_id=lambda: "r1").text
    edited = stamped.replace("Original body.", "Edited body now.")
    _, findings = M.lint_document(edited)
    assert [f.code for f in findings] == ["HASH_DRIFT"]
    res = M.restamp(edited)
    assert res.refreshed == ["r1"]
    _, after = M.lint_document(res.text)
    assert after == []


def test_restamp_no_op_when_nothing_drifted():
    stamped = M.stamp(DOC, new_id=counter()).text
    res = M.restamp(stamped)
    assert res.refreshed == []
    assert res.text == stamped


def test_restamp_preserves_stored_hash_precision():
    md = "New text here.\n<!-- stay:p1 hash=sha256:0000 -->"
    res = M.restamp(md)
    mk = M.find_markers(res.text)[0]
    assert len(mk.hash) == 4
    assert mk.hash == M.body_hash("New text here.", 4)


def test_restamp_add_missing_gives_hashless_marker_a_hash():
    md = "Body text.\n<!-- stay:n1 -->"
    res = M.restamp(md, add_missing=True)
    assert res.refreshed == ["n1"]
    mk = M.find_markers(res.text)[0]
    assert mk.hash == M.body_hash("Body text.", 12)


# --- repair_duplicates (§7) -----------------------------------------------


def test_repair_duplicates_first_kept_later_reminted_lints_clean():
    md = (
        "Para one.\n<!-- stay:dup hash=sha256:0000 -->\n\n"
        "Para two.\n<!-- stay:dup hash=sha256:1111 -->"
    )
    assert "DUPLICATE_ID" in error_codes(md)
    res = M.repair_duplicates(md, new_id=lambda: "fresh1")
    assert res.renamed == [{"from": "dup", "to": "fresh1"}]
    assert "stay:dup" in res.text  # first kept
    assert "stay:fresh1" in res.text  # second re-minted
    assert error_codes(res.text) == []


def test_repair_duplicates_same_block_markers():
    # two markers sharing an id on ONE block: lint flags it, repair must fix it
    md = "A.\n<!-- stay:dup -->\n<!-- stay:dup -->"
    assert "DUPLICATE_ID" in error_codes(md)
    res = M.repair_duplicates(md, new_id=lambda: "fresh1")
    assert res.renamed == [{"from": "dup", "to": "fresh1"}]
    assert error_codes(res.text) == []


def test_repair_duplicates_no_op_without_duplicates():
    md = M.stamp(DOC, new_id=counter()).text
    res = M.repair_duplicates(md)
    assert res.renamed == []
    assert res.text == md


def test_repair_duplicates_reminted_id_never_collides():
    md = "One.\n<!-- stay:dup -->\n\nTwo.\n<!-- stay:dup -->\n\nThree.\n<!-- stay:taken -->"
    proposals = iter(["taken", "ok1"])  # first proposal clashes, must be skipped
    res = M.repair_duplicates(md, new_id=lambda: next(proposals))
    assert res.renamed == [{"from": "dup", "to": "ok1"}]
