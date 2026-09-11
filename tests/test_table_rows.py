import markstay as M


def ids(*values):
    proposals = iter(values)
    return lambda: next(proposals)


def child_marker(marker_id, content):
    return f"<!-- stay:{marker_id} " f"subhash=sha256:{M.body_hash(content, 12)} -->"


def test_table_rows_are_children_with_canonical_bodies_in_both_modes():
    row_body = r"a\\\|b||tail\\"
    digest = M.body_hash(row_body, 12)
    md = (
        "| one | two | three |\n"
        "|---|---|---|\n"
        rf"| a\|b | | tail\<!-- stay:r1 subhash=sha256:{digest} --> |"
        "\n<!-- stay:table -->\n"
    )
    for mode in ("blank-line", "commonmark"):
        blocks, findings = M.lint_document(md, mode=mode, child_blocks=True)
        rows = [child for child in blocks[0].children if child.kind == "row"]
        assert [(row.ordinal, row.content) for row in rows] == [(1, row_body)]
        assert [marker.id for marker in rows[0].markers] == ["r1"]
        assert findings == []


def test_table_scan_is_fail_closed_and_keeps_delimiter_provenance():
    md = (
        "| h |\n"
        "|---<!-- stay:delimiter subhash=bogus -->|\n"
        "| body |\n"
        "<!-- stay:table -->\n"
    )
    blocks, findings = M.lint_document(md, child_blocks=True)
    assert not [child for child in blocks[0].children if child.kind == "row"]
    finding = next(item for item in findings if item.id == "delimiter")
    assert finding.code == "CHILD_UNADDRESSED"
    assert "table row" in finding.message
    assert "complete §5.6 scan" in finding.message

    opaque = (
        "| h |\n|---|\n"
        '| a\\<!-- stay:r subhash=bogus x-note="|" -->|\n'
        "<!-- stay:t -->\n"
    )
    opaque_row = M.parse_document(opaque, child_blocks=True)[0].children[0]
    assert opaque_row.content == "a\\\\"

    refused = [
        (
            "| h |\n|---|\n"
            '| one<!-- stay:r subhash=bogus x-note="two\n'
            'three" --> |\n'
        ),
        (
            "| h |\n|---|\n"
            "| one<!-- stay:outer subhash=bogus x=<!--stay:inner --> |\n"
        ),
    ]
    for unsafe in refused:
        assert not [
            child
            for block in M.parse_document(unsafe, child_blocks=True)
            for child in block.children
            if child.kind == "row"
        ]


def test_list_and_row_children_have_separate_ordinals_and_ownership():
    md = (
        "- item <!-- stay:list subhash=bogus -->\n"
        "  | h |\n"
        "  |---|\n"
        "  | value<!-- stay:row subhash=bogus --> |\n"
        "<!-- stay:parent -->\n"
    )
    block = M.parse_document(md, mode="commonmark", child_blocks=True)[0]
    assert [(child.kind, child.ordinal) for child in block.children] == [
        ("list", 1),
        ("row", 1),
    ]
    assert [marker.id for marker in block.children[0].markers] == ["list"]
    assert [marker.id for marker in block.children[1].markers] == ["row"]


def test_writer_refuses_list_carrier_when_it_collides_with_a_row_carrier():
    md = "- item\n  | h |\n  |---|\n  | value |\n"
    result = M.stamp(
        md,
        mode="commonmark",
        child_blocks=True,
        new_id=ids("row", "parent", "unused"),
    )
    assert [item["id"] for item in result.minted] == ["row", "parent"]
    block = M.parse_document(result.text, mode="commonmark", child_blocks=True)[0]
    row = next(child for child in block.children if child.kind == "row")
    item = next(child for child in block.children if child.kind == "list")
    assert [marker.id for marker in row.markers] == ["row"]
    assert item.markers == []
    assert result.text.splitlines()[3].rstrip().endswith("|")


def test_row_stamp_is_flush_in_content_and_empty_last_cells_and_stays_hash_stable():
    md = "| h1 | h2 |\n|---|---|\n| a | b |\n| c |   |\n"
    original_body = M.parse_document(md)[0].content
    result = M.stamp(
        md,
        child_blocks=True,
        new_id=ids("row1", "row2", "table"),
    )
    assert "| a | b<!-- stay:row1 subhash=sha256:" in result.text
    assert "| c |<!-- stay:row2 subhash=sha256:" in result.text
    assert "-->   |" in result.text
    assert result.text.splitlines()[-1].startswith("<!-- stay:table hash=sha256:")

    block = M.parse_document(result.text, child_blocks=True)[0]
    assert block.content == original_body
    assert [child.kind for child in block.children] == ["row", "row"]
    parent = next(marker for marker in block.markers if marker.id == "table")
    assert parent.hash == M.body_hash(block.content, len(parent.hash))


def test_empty_row_body_still_checks_subhash_drift():
    md = (
        "| h |\n|---|\n"
        "|<!-- stay:empty subhash=sha256:dead --> |\n"
        "<!-- stay:table -->\n"
    )
    block, findings = M.lint_document(md, child_blocks=True)
    assert (
        next(child for child in block[0].children if child.kind == "row").content == ""
    )
    assert [(finding.code, finding.id) for finding in findings] == [
        ("HASH_DRIFT", "empty")
    ]


def test_legacy_suffix_migrates_verbatim_before_row_stamp():
    md = "| h |\n|---|\n" "| value |  <!-- stay:table x-note=legacy -->\n"
    for mode in ("blank-line", "commonmark"):
        result = M.stamp(md, mode=mode, child_blocks=True, new_id=lambda: "row")
        lines = result.text.splitlines()
        assert lines[2].startswith("| value<!-- stay:row subhash=sha256:")
        assert lines[2].endswith(" |  ")
        assert lines[3] == "<!-- stay:table x-note=legacy -->"
        assert [item["id"] for item in result.minted] == ["row"]


def test_illegal_migration_and_failed_full_probe_roll_back_byte_exactly():
    illegal = "| h |\r\n|---|\r\n" "| value |<!-- stay:child subhash=bogus -->\r\n"
    refused = M.stamp(illegal, child_blocks=True, new_id=lambda: "unused")
    assert refused.text == illegal
    assert refused.minted == []

    two_candidates = (
        "| h |\n|---|\n| one |<!-- stay:table -->\n" "| h |\n|---|\n| two |\n"
    )
    rolled_back = M.stamp(two_candidates, child_blocks=True, new_id=lambda: "unused")
    assert rolled_back.text == two_candidates
    assert rolled_back.minted == []


def test_existing_container_stay_moves_and_preexisting_drift_is_reported():
    bare = "| h |\n|---|\n| value<!-- stay:table --> |\n"
    body = M.parse_document(bare)[0].content
    valid = bare.replace(
        "stay:table", f"stay:table hash=sha256:{M.body_hash(body, 12)}"
    )
    moved = M.stamp(valid, child_blocks=True, new_id=lambda: "row")
    assert moved.drifted == []
    assert moved.text.splitlines()[2].startswith("| value<!-- stay:row subhash=sha256:")
    assert moved.text.splitlines()[3] == (
        f"<!-- stay:table hash=sha256:{M.body_hash(body, 12)} -->"
    )

    drifted = bare.replace("stay:table", "stay:table hash=sha256:dead")
    refused = M.stamp(drifted, child_blocks=True, new_id=lambda: "unused")
    assert refused.text == drifted
    assert refused.minted == []
    assert refused.drifted == ["table"]

    drifted_crlf = (
        "| h |\r\n|---|\r\n" "| value |<!-- stay:table hash=sha256:dead -->\r\n"
    )
    refused_crlf = M.stamp(drifted_crlf, child_blocks=True, new_id=lambda: "unused")
    assert refused_crlf.text == drifted_crlf
    assert refused_crlf.drifted == ["table"]


def test_restamp_refreshes_row_and_container_hashes_after_an_edit():
    stamped = M.stamp(
        "| h |\n|---|\n| old |\n",
        child_blocks=True,
        new_id=ids("row", "table"),
    ).text
    edited = stamped.replace("| old", "| new")
    result = M.restamp(edited, child_blocks=True)
    assert set(result.refreshed) == {"row", "table"}
    assert M.lint_document(result.text, child_blocks=True)[1] == []


def test_same_kind_scoping_for_child_recovery_tiers():
    md = (
        "- outer <!-- stay:list subhash=bogus -->\n"
        "  | h |\n"
        "  |---|\n"
        "  | value<!-- stay:row subhash=bogus --> |\n"
        "<!-- stay:parent -->\n"
    )
    block = M.parse_document(md, mode="commonmark", child_blocks=True)[0]
    list_child = next(child for child in block.children if child.kind == "list")
    row_child = next(child for child in block.children if child.kind == "row")
    parent = M.Anchor("parent", M.body_hash(block.content), M.Selector(block.content))

    ordinal_anchors = M.build_child_anchors(md, mode="commonmark")
    markerless = md.replace(" <!-- stay:list subhash=bogus -->", "").replace(
        "<!-- stay:row subhash=bogus -->", ""
    )
    ordinal = M.resolve_children(ordinal_anchors, markerless, mode="commonmark")
    reparsed = {
        child.index: child
        for parsed in M.parse_document(markerless, mode="commonmark", child_blocks=True)
        for child in parsed.children
    }
    assert reparsed[ordinal["list"].target].kind == "list"
    assert reparsed[ordinal["row"].target].kind == "row"

    row_only = M.ChildAnchor(
        "synthetic",
        M.body_hash(list_child.content),
        M.Selector(row_child.content),
        1,
        parent,
        "not-the-parent-hash",
        sibling_hash_count=1,
        document_hash_count=2,
        kind="row",
    )
    recovered = M.resolve_children([row_only], md, mode="commonmark")["synthetic"]
    assert recovered.method == "quote"
    assert recovered.target == row_child.index

    quote_only = M.ChildAnchor(
        "quote-only",
        "not-a-child-hash",
        M.Selector(list_child.content),
        1,
        parent,
        "not-the-parent-hash",
        sibling_hash_count=2,
        document_hash_count=2,
        kind="row",
    )
    quoted = M.resolve_children([quote_only], md, mode="commonmark")["quote-only"]
    assert quoted.target != list_child.index


def test_row_anchors_use_row_ordinals_and_row_sibling_context_only():
    md = (
        "- list one <!-- stay:l1 subhash=bogus -->\n"
        "- list two <!-- stay:l2 subhash=bogus -->\n"
        "  | h |\n"
        "  |---|\n"
        "  | row one<!-- stay:r1 subhash=bogus --> |\n"
        "  | row two<!-- stay:r2 subhash=bogus --> |\n"
        "<!-- stay:p -->\n"
    )
    anchors = {
        anchor.id: anchor for anchor in M.build_child_anchors(md, mode="commonmark")
    }
    assert [(anchors[mid].kind, anchors[mid].ordinal) for mid in ("l1", "l2")] == [
        ("list", 1),
        ("list", 2),
    ]
    assert [(anchors[mid].kind, anchors[mid].ordinal) for mid in ("r1", "r2")] == [
        ("row", 1),
        ("row", 2),
    ]
    assert anchors["r1"].selector.prefix == ""
    assert anchors["r1"].selector.suffix == "row two"
    assert anchors["r2"].selector.prefix == "row one"
    assert anchors["r2"].selector.suffix == ""


def test_markerless_row_recovers_by_parent_hash_and_document_hash():
    row_marker = child_marker("row", "Move row")
    table = (
        "| h |\n|---|\n"
        f"| Move row{row_marker} |\n"
        "| Keep row<!-- stay:keep subhash=bogus --> |\n"
    )
    parent_hash = M.body_hash(M.parse_document(table, child_blocks=True)[0].content, 12)
    before = table + f"<!-- stay:parent hash=sha256:{parent_hash} -->\n"
    after = before.replace(row_marker, "")
    anchor = next(
        anchor for anchor in M.build_child_anchors(before) if anchor.id == "row"
    )
    assert M.resolve_children([anchor], after)["row"].method == "parent-hash"

    first = "| h |\n|---|\n" f"| Move row{row_marker} |\n" "<!-- stay:p1 -->\n"
    second = (
        "| h |\n|---|\n"
        "| Stable<!-- stay:stable subhash=bogus --> |\n"
        "<!-- stay:p2 -->\n"
    )
    before_move = first + "\n" + second
    moved_line = f"| Move row{row_marker} |"
    after_move = before_move.replace(moved_line + "\n", "", 1).replace(
        "| Stable<!-- stay:stable subhash=bogus --> |\n",
        "| Stable<!-- stay:stable subhash=bogus --> |\n| Move row |\n",
        1,
    )
    anchor = next(
        anchor for anchor in M.build_child_anchors(before_move) if anchor.id == "row"
    )
    assert M.resolve_children([anchor], after_move)["row"].method == "document-hash"


def test_row_quote_candidates_exclude_overlapping_list_children(monkeypatch):
    import importlib

    resolver = importlib.import_module("markstay.resolve")
    marker = child_marker("row", "target original")
    before = (
        "- outer\n"
        "  | h |\n"
        "  |---|\n"
        f"  | target original{marker} |\n"
        "<!-- stay:parent -->\n"
    )
    after = before.replace(f"target original{marker}", "target revised")
    anchor = next(
        anchor
        for anchor in M.build_child_anchors(before, mode="commonmark")
        if anchor.id == "row"
    )
    calls = []
    original = resolver.rank_candidates

    def record_candidates(selector, bodies, **kwargs):
        calls.append(list(bodies))
        return original(selector, bodies, **kwargs)

    monkeypatch.setattr(resolver, "rank_candidates", record_candidates)
    result = M.resolve_children([anchor], after, mode="commonmark")["row"]
    assert result.method == "quote"
    assert calls == [["target revised"]]


def test_cli_reports_row_container_drift(tmp_path, capsys):
    from markstay.cli import main

    path = tmp_path / "table.md"
    text = "| h |\n|---|\n| value<!-- stay:table hash=sha256:dead --> |\n"
    path.write_text(text)
    assert main(["stamp", "--child-blocks", "--write", str(path)]) == 1
    assert path.read_text() == text
    stderr = capsys.readouterr().err
    assert "pre-existing container hash drift" in stderr
    assert "table" in stderr


def test_cli_refused_crlf_row_migration_never_opens_the_file_for_write(
    tmp_path, capsys, monkeypatch
):
    from pathlib import Path

    from markstay.cli import main

    path = tmp_path / "refused.md"
    raw = b"| h |\r\n" b"|---|\r\n" b"| value |<!-- stay:child subhash=bogus -->\r\n"
    path.write_bytes(raw)

    real_open = Path.open
    modes = []

    def tracked_open(self, mode="r", *args, **kwargs):
        modes.append(mode)
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracked_open)
    # Exit 1 and a named reason: the refusal is real work declined, not an empty
    # document. This assertion used to be `== 0` with `0 id(s) minted`, which is
    # what a document with nothing to do also prints, so a hook could not tell the
    # two apart. What the test is actually for is the write mode below.
    assert main(["stamp", "--child-blocks", "--write", str(path)]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "refused (unsafe-relocation)" in captured.err
    assert modes == ["r"]
    assert path.read_bytes() == raw


def test_cli_successful_crlf_row_stamp_still_writes_lf(tmp_path, capsys):
    from markstay.cli import main

    path = tmp_path / "successful.md"
    path.write_bytes(b"| h |\r\n|---|\r\n| value |\r\n")

    assert main(["stamp", "--child-blocks", "--write", str(path)]) == 0

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "2 id(s) minted" in captured.err
    written = path.read_bytes()
    assert b"\r" not in written
    assert b"subhash=sha256:" in written
    assert written.endswith(b"\n")


def test_relocating_a_marker_only_line_does_not_split_the_container():
    """SPEC.md §5.6: the migration probe relocates marker spans 'leaving every
    non-marker byte unchanged', and a marker-only line has none.

    A container can carry more than one stay, and the probe moves them together
    so they stay adjacent. When one of them occupied a whole line, removing its
    markers must remove the LINE. Left behind as an empty string it is a §5 block
    boundary, which splits the very container being prepared: the relocated stay
    then binds to a shorter body, and the next pass reports the tool's own
    provisional edit as pre-existing container drift.

    Here the relocation legitimately fails, because promoting the second table to
    an accepted candidate leaves two in one container and §5.6 addresses neither.
    The correct outcome is a whole-document rollback: byte-identical source,
    nothing minted, and nothing reported as drifted, since the container was clean
    before the tool touched it.
    """
    import markstay as M
    from markstay import lint as L

    body = "| a | b |\n|---|---|\n| x | y |\n\n| c | d |\n|---|---|\n| p | q |"
    digest = L.body_hash(body, 12)
    doc = (
        "| a | b |\n|---|---|\n| x | y |\n<!-- stay:sep -->\n"
        f"| c | d |\n|---|---|\n| p | q |<!-- stay:tb1 hash=sha256:{digest} -->\n"
    )

    result = M.stamp(doc, child_blocks=True, new_id=lambda: "never")

    assert result.text == doc
    assert result.minted == []
    assert list(result.drifted) == [], (
        "the container matched its stored hash before the write, so naming it "
        "drifted blames the source for the probe's own provisional edit"
    )


def test_relocating_a_shared_marker_line_preserves_a_subhash_marker():
    """SPEC.md §16 rule 1: a write-path tool leaves a `subhash` marker alone.

    The migration probe removes a whole source line when relocating a container
    stay that occupied one, because an empty line left behind is a §5 block
    boundary that splits the container being prepared. Whether the line is empty
    has to be decided on what SURVIVES the removal, not on whether the source
    line held only markers: a movable parent marker can share its line with a
    `subhash` marker, which this probe does not move and MUST NOT drop.

    Deciding it on the original line answers "marker-only" here and deletes a
    line with a live marker still on it, silently, while reporting a clean mint
    and no drift. Nothing in the corpus or the mutation battery sees that,
    because the loss is invisible to every gate that only reads the result.
    """
    import markstay as M

    child = f'<!-- stay:r9 subhash=sha256:{M.body_hash("zz", 12)} -->'
    doc = (
        f"<!-- stay:tb1 -->{child}\n"
        "| one | two |\n"
        "|---|---|\n"
        "| a | b |\n"
    )

    result = M.stamp(doc, child_blocks=True, new_id=lambda: "minted01")

    assert child in result.text, (
        "the probe deleted a line that still carried a subhash marker; §16 rule "
        "1 requires the marker to survive the write path lexically"
    )
    # Substring survival alone is satisfied by a stamp that did NOTHING, so the
    # mint and the exact output are asserted too: a refusal that returns the input
    # byte-for-byte would otherwise read as a pass for the behavior under test.
    assert result.minted == [{"id": "minted01", "line": 4}]
    assert result.text == (
        f"{child}\n"
        "| one | two |\n"
        "|---|---|\n"
        "| a | b<!-- stay:minted01 subhash=sha256:0eab8a0a3380 --> |\n"
        "<!-- stay:tb1 -->\n"
    )
    assert list(result.drifted) == []


def test_relocating_identical_markers_preserves_their_multiplicity():
    """Two byte-identical markers on one line are one relocation, not one marker.

    `_marker_record_on_line` matches a marker object to a source record by id and
    raw text. Those are equal for both occurrences of a repeated marker, so a
    first-match lookup hands both marker objects the SAME record: the removal
    takes one span, the relocation re-inserts two, and the document gains an
    occurrence nobody wrote. The multi-pass loop then compounds it.

    The whole-line delete this probe used to do hid the defect, because it removed
    both occurrences whatever the lookup returned. Preserving a line that still
    carries a `subhash` marker is what made it reachable, so this asserts exact
    counts rather than mere survival: a relocation must be occurrence-preserving.
    """
    import markstay as M

    child = f'<!-- stay:r9 subhash=sha256:{M.body_hash("zz", 12)} -->'
    doc = (
        f"<!-- stay:p --><!-- stay:p -->{child}\n"
        "| one | two |\n"
        "|---|---|\n"
        "| a | b |\n"
    )

    result = M.stamp(doc, child_blocks=True, new_id=lambda: "minted01")

    assert result.text.count("<!-- stay:p -->") == 2, (
        "the relocation changed how many times a repeated marker appears"
    )
    assert result.text.count(child) == 1
    assert result.text == (
        f"{child}\n"
        "| one | two |\n"
        "|---|---|\n"
        "| a | b<!-- stay:minted01 subhash=sha256:0eab8a0a3380 --> |\n"
        "<!-- stay:p --><!-- stay:p -->\n"
    )
    assert result.minted == [{"id": "minted01", "line": 4}]
    assert list(result.drifted) == []


def test_the_write_path_preserves_the_multiset_of_existing_markers():
    """A relocation may ADD a minted marker. It may never lose or duplicate one.

    Both write-path defects found in review were violations of this single
    invariant: one dropped a `subhash` marker with the line it shared, the other
    relocated two byte-identical markers into four. Neither was caught by the
    conformance corpus or the mutation battery, because both are invisible to any
    check that reads the result's semantics rather than counting its bytes. A
    vector asserts what one document becomes; this asserts what every document
    keeps.

    The oracle is `find_markers`, the production scanner, NOT a regex written for
    the test. An independent parser here is a silent-success path: a regex like
    `<!-- stay:[^>]*-->` cannot see `<!-- stay:p x-note=">" -->`, which §4 permits
    and production recognizes, so deleting that marker would compare empty against
    empty and pass. Counting with a Counter rather than a sorted list is the other
    half: it catches a marker emitted twice, including a MINTED one, which an
    id-based exclusion would otherwise excuse.

    The cross-product is small and total rather than random, so a failure names a
    reproducible document instead of a seed.
    """
    import itertools
    from collections import Counter

    import markstay as M

    sub = f'subhash=sha256:{M.body_hash("zz", 12)}'

    parents = [
        "<!-- stay:p -->",
        "<!-- stay:p --><!-- stay:p -->",          # byte-identical siblings
        "<!-- stay:p --><!-- stay:q -->",
        "<!-- stay:p hash=sha256:1122 -->",
        '<!-- stay:p x-note=">" -->',              # §4 allows > inside a quoted value
        # One valid marker wrapped in fragments. Removing it splices `<!-- stay:`
        # to `x -->` and CREATES `<!-- stay:x -->`, an identity nobody wrote. A
        # cross-product of well-formed, separable markers never produces this
        # shape, which is why the invariant needs an adversarial fixture and not
        # just more combinations of reasonable ones.
        "<!-- stay:<!-- stay:p -->x -->",
    ]
    children = ["", f"<!-- stay:r9 {sub} -->", f"<!-- stay:r9 {sub} --><!-- stay:r8 {sub} -->"]
    tables = [
        "| one | two |\n|---|---|\n| a | b |",
        "| one | two |\n|---|---|\n| a | b |\n| c | d |",
        "| one |\n|---|\n| a |",
    ]

    def placements(line, table):
        yield f"{line}\n{table}\n"
        yield f"{table}\n{line}\n"
        yield f"{table}\n\n{line}\n"

    def counted(text):
        return Counter(
            marker.raw for marker in M.find_markers(text) if not marker.malformed
        )

    for parent, child, table in itertools.product(parents, children, tables):
        for doc in placements(parent + child, table):
            ids = iter(f"m{i}" for i in range(20))
            result = M.stamp(doc, child_blocks=True, new_id=lambda: next(ids))

            before, after = counted(doc), counted(result.text)

            # Subtract exactly the markers this run says it minted: one per entry,
            # matched by id, so a minted marker emitted TWICE leaves a residue
            # instead of being excused wholesale.
            minted_ids = [entry["id"] for entry in result.minted]
            for mid in minted_ids:
                raw = next(
                    (m.raw for m in M.find_markers(result.text)
                     if not m.malformed and m.id == mid),
                    None,
                )
                assert raw is not None, f"minted id {mid!r} is not in the output, for {doc!r}"
                after[raw] -= 1
                if after[raw] == 0:
                    del after[raw]

            assert after == before, (
                f"the write path changed which markers exist, for {doc!r}\n"
                f"  expected {dict(before)}\n  got      {dict(after)}"
            )


def _stamp_module():
    """The write-path module, not the ``stamp`` function the package re-exports."""
    import importlib

    return importlib.import_module("markstay.stamp")


def test_the_write_path_refuses_a_net_zero_marker_substitution():
    """A relocation may not destroy one occurrence and create its byte twin.

    Three well-formed markers go in. `a` is a `subhash` marker whose quoted value
    spans lines 1-2, which §4 permits and §16 rule 1 requires the write path to
    leave alone. Moving `p` destroys the `a` built from lines 1-2, and splicing
    `q` out of the second block builds a byte-identical `a` from lines 3-4.

    Every raw-byte count is therefore unchanged, which is why the guard this
    replaced passed the document, committed the edit, and reported the row mint
    with `drifted=[]`. What changed is WHICH occurrence exists: a `subhash`
    marker was destroyed and a different one created. Only a check that resolves
    each record to a document span and asks where it lands can see that, so this
    fixture is the one that pins the difference between the two invariants.
    """
    import markstay as M

    doc = (
        '<!--stay:a subhash=bad note="\n'
        'v" x={/*stay:p y=z-->*/}\n'
        '<!--stay:a subhash=bad note="\n'
        'v<!--stay:q-->" x={/*stay:p y=z-->\n'
        "| h |\n"
        "|---|\n"
        "| a |\n"
    )

    result = M.stamp(doc, child_blocks=True, new_id=ids("m0", "m1", "m2"))

    assert result.text == doc, "the write path mutated a document it must refuse"
    assert result.minted == []
    assert list(result.drifted) == []


def test_relocation_moves_records_and_leaves_the_rest_where_they_land():
    """The positive control: a legal plan applies and reports the exact result."""
    stamp_module = _stamp_module()

    text = "<!-- stay:p -->\nbody\n"
    (record,) = stamp_module._identity_records(text)
    offsets = stamp_module._line_offsets(text)

    plan = stamp_module._Relocation(
        excisions=tuple(stamp_module._dropped_line_spans(text, [0])),
        moves=(record,),
        insert_at=offsets[1] + len("body"),
        prefix="\n",
    )

    assert stamp_module._relocate(text, plan) == "body\n<!-- stay:p -->\n"


def test_relocation_refuses_to_cut_into_a_record_it_does_not_move():
    """Rule 4. `a`'s quoted value runs past `p`'s opener, so the two records
    overlap: excising `p` removes bytes `a` is made of. A plan that would edit
    inside a record it never named cannot be checked, so it is refused rather
    than applied and inspected afterwards."""
    stamp_module = _stamp_module()

    text = '<!--stay:a note="\nv" x={/*stay:p y=z-->*/}\nbody\n'
    records = stamp_module._identity_records(text)
    assert [record.marker.id for record in records] == ["a", "p"]
    overlapping = records[1]
    offsets = stamp_module._line_offsets(text)

    plan = stamp_module._Relocation(
        excisions=((overlapping.start, overlapping.end),),
        moves=(overlapping,),
        insert_at=offsets[2] + len("body"),
        prefix="\n",
    )

    assert stamp_module._relocate(text, plan) is None


def test_relocation_refuses_an_excision_that_swallows_an_unnamed_record():
    """Rule 1. The plan deletes the whole line, so it destroys `q` as well as
    `p`, but promises only `p` back. Deleting the line is legal when the line
    holds nothing else; it is the mismatch between what an excision destroys and
    what the plan re-emits that is not.

    The landing check cannot see this on its own: with `q` gone from the source
    inventory too, every remaining record still lands where predicted and the
    document quietly loses an identity."""
    stamp_module = _stamp_module()

    text = "<!-- stay:p --><!-- stay:q -->\nbody\n"
    records = stamp_module._identity_records(text)
    offsets = stamp_module._line_offsets(text)

    plan = stamp_module._Relocation(
        excisions=tuple(stamp_module._dropped_line_spans(text, [0])),
        moves=(records[0],),
        insert_at=offsets[1] + len("body"),
        prefix="\n",
    )

    assert stamp_module._relocate(text, plan) is None


def test_relocation_refuses_a_move_that_invents_a_record():
    """Rules 2 and 3. One valid marker wrapped in fragments: removing it splices
    `<!-- stay:` to `x -->` and CREATES `<!-- stay:x -->`, an identity nobody
    wrote. Nothing was lost and nothing was duplicated, so the arithmetic of the
    document is untouched; what fails is that the result holds a record the plan
    never wrote."""
    stamp_module = _stamp_module()

    text = "<!-- stay:<!-- stay:p -->x -->\nbody\n"
    (record,) = stamp_module._identity_records(text)
    offsets = stamp_module._line_offsets(text)

    plan = stamp_module._Relocation(
        excisions=((record.start, record.end),),
        moves=(record,),
        insert_at=offsets[1] + len("body"),
        prefix="\n",
    )

    assert stamp_module._relocate(text, plan) is None


def test_relocation_refuses_a_move_that_is_not_the_record_at_its_span():
    """Rule 1, at the other end: a move must name the record that is really
    there, not merely a span the document happens to have.

    `q` occupies the span the plan names, and the plan carries a same-length `p`
    from another document. Comparing spans alone accepts it: the excision removes
    `q`, the plan writes `p`, and every later check agrees, because they all read
    the bytes the PLAN declares rather than the bytes the document holds. That is
    the substitution this whole function exists to refuse, arriving through its
    own front door.

    No production builder can construct this today, since both probes source
    their moves from the current inventory. It is checked because the contract is
    the thing being relied on: an invariant a caller can violate is not one.
    """
    stamp_module = _stamp_module()

    text = "<!-- stay:q -->\nbody\n"
    (real,) = stamp_module._identity_records(text)
    (foreign,) = stamp_module._identity_records("<!-- stay:p -->\n")
    assert (foreign.start, foreign.end) == (real.start, real.end)
    assert foreign.marker.raw != real.marker.raw

    plan = stamp_module._Relocation(
        excisions=tuple(stamp_module._dropped_line_spans(text, [0])),
        moves=(foreign,),
        insert_at=stamp_module._line_offsets(text)[1] + len("body"),
        prefix="\n",
    )

    assert stamp_module._relocate(text, plan) is None


def test_relocation_refuses_to_name_one_record_twice():
    """Rule 1, on multiplicity. Naming a record twice removes one occurrence and
    writes two, which is how a pair of byte-identical siblings once became four.

    Every other rule here compares SETS of spans, and a set cannot count: the
    excision destroys one record, the plan promises one record back, and both
    copies land exactly where a plan naming two would put them. Only refusing the
    repeat sees it."""
    stamp_module = _stamp_module()

    text = "<!-- stay:p -->\nbody\n"
    (record,) = stamp_module._identity_records(text)
    offsets = stamp_module._line_offsets(text)

    plan = stamp_module._Relocation(
        excisions=tuple(stamp_module._dropped_line_spans(text, [0])),
        moves=(record, record),
        insert_at=offsets[1] + len("body"),
        prefix="\n",
    )

    assert stamp_module._relocate(text, plan) is None


def test_relocation_refuses_an_excision_inside_a_record_it_moves():
    """Rule 4, on the record the plan does name. Cutting bytes out of the middle
    of a marker and re-emitting the whole raw elsewhere leaves the marker's
    remains behind as stray prose.

    The landing check is blind to it, because the only record it can see did move
    and did land where promised: what the edit wrecked is the text around it. An
    excision either takes a whole record or none of it, whether or not the record
    is one the plan is moving."""
    stamp_module = _stamp_module()

    text = "<!-- stay:p -->\nbody\n"
    (record,) = stamp_module._identity_records(text)
    offsets = stamp_module._line_offsets(text)

    plan = stamp_module._Relocation(
        excisions=((record.start + 5, record.start + 10),),
        moves=(record,),
        insert_at=offsets[1] + len("body"),
        prefix="\n",
    )

    assert stamp_module._relocate(text, plan) is None


def test_relocation_refuses_to_write_inside_a_record():
    """Rule 4 on the insertion side, in both the shapes it has.

    An insertion point is a seam between records, never a position inside one:
    writing into `q` rewrites bytes the plan promised to preserve, and writing
    into `p` puts the insertion inside the excision that removes it, which leaves
    no single result to predict.

    Unlike the other rules here, this one is a precondition rather than the last
    line of defence: an insertion always changes the bytes of the record it lands
    in, so the landing comparison would refuse either plan too. What the
    precondition buys is that an ill-formed plan is turned away before it is
    applied, instead of being applied and then found wanting.
    """
    stamp_module = _stamp_module()

    text = "<!-- stay:p --><!-- stay:q -->\nbody\n"
    moved, survivor = stamp_module._identity_records(text)

    for inside in (survivor.start + 5, moved.start + 5):
        plan = stamp_module._Relocation(
            excisions=((moved.start, moved.end),),
            moves=(moved,),
            insert_at=inside,
            prefix="\n",
        )
        assert stamp_module._relocate(text, plan) is None


def test_a_fenced_marker_shaped_string_is_not_an_identity_but_is_read_as_markup():
    """The two masks answer different questions, and only one of them is safe to
    get wrong.

    `z` opens inside a fence and its quoted value runs to the end of the document,
    so its span swallows the container marker `p`. §3.3 says a marker-shaped string
    in a fenced code block is content, so `z` is NOT in the write path's identity
    inventory: that filter is the spec's own line-based fence rule, and it stays.

    Comment structure is read WITHOUT that mask. Whether a `<!--` or `{/*` opens a
    comment is the renderer's call, and this project does not model the renderer's
    fence recognition: inside a raw HTML block a line of backticks opens nothing,
    so blanking it hides a live opener and lets an edit re-pair a real comment and
    silently swallow text. Refusing a document that merely SHOWS an unclosed
    example is the cheaper mistake, so `p` reads as nested and the stamp refuses.

    The trade is deliberate and this pins both halves of it: `z` is absent from the
    identity inventory, and the document is still refused.
    """
    import markstay as M

    stamp_module = _stamp_module()
    doc = (
        "```\n"
        '{/*stay:z x="\n'
        "```\n"
        "<!-- stay:p -->\n"
        "| h |\n"
        "|---|\n"
        "| a |\n"
        "\n"
        '" y=1*/}\n'
    )

    assert [record.marker.id for record in stamp_module._identity_records(doc)] == ["p"]
    assert [marker.id for marker in M.find_markers(doc) if not marker.malformed] == [
        "z",
        "p",
    ]

    result = M.stamp(doc, child_blocks=True, new_id=ids("m0", "m1"))
    assert result.text == doc
    assert result.minted == []


def test_a_document_with_no_trailing_newline_still_relocates_adjacent_markers():
    """Two marker-only lines at the end of a document that does not end in a
    newline are one deletion, not two.

    Deleting them separately double-claims a byte: the second-to-last line owns
    the newline that follows it, the last line owns the newline that precedes it,
    and at the end of the document those are the same newline. The overlapping
    pair reads as an ill-formed plan, so the relocation refuses and `stamp()`
    returns the document unchanged with `minted=[]` and `drifted=[]`, which a
    caller cannot tell from having nothing to do.

    The same document with a trailing newline has always worked, which is what
    makes this shape easy to miss.
    """
    import markstay as M

    body = (
        "| h |\n"
        "|---|\n"
        "| a<!-- stay:p --> |\n"
        "<!-- stay:q -->\n"
        "<!-- stay:r -->"
    )
    expected = (
        "| h |\n"
        "|---|\n"
        "| a<!-- stay:m0 subhash=sha256:ca978112ca1b --> |\n"
        "<!-- stay:p --><!-- stay:q --><!-- stay:r -->"
    )

    for mode in ("blank-line", "commonmark"):
        result = M.stamp(body, child_blocks=True, mode=mode, new_id=ids("m0"))
        assert result.text == expected, mode
        assert result.minted == [{"id": "m0", "line": 3}]
        assert list(result.drifted) == []
        assert M.lint_document(result.text, mode=mode, child_blocks=True)[1] == []
        again = M.stamp(result.text, child_blocks=True, mode=mode, new_id=ids("m1"))
        assert again.text == result.text


def test_the_write_path_cannot_fuse_fragments_into_a_diagnostic():
    """A relocation must not leave behind a marker-shaped string §4 reads as
    malformed.

    The container marker `p` sits inside the fragments of a longer comment.
    Removing it splices `<!-- stay:` to `hash=x -->`, and the result reads as a
    key-first body, which §4 calls a diagnostic rather than a marker. Nothing was
    lost, nothing was duplicated, and no IDENTITY was invented, so a guard that
    inventories only well-formed markers commits the edit and reports a
    successful mint. What it hands back is a document that lints clean going in
    and reports MALFORMED_MARKER coming out.

    That is why the conservation inventory is every ACTIVE record and not only
    the identities, while the records a plan may move stay restricted to the
    identities.
    """
    import markstay as M

    doc = "| h |\n|---|\n| a<!-- stay:<!-- stay:p -->hash=x --> |\n"
    assert M.lint_document(doc, child_blocks=True)[1] == []

    for mode in ("blank-line", "commonmark"):
        result = M.stamp(doc, child_blocks=True, mode=mode, new_id=ids("m0", "m1"))
        assert result.text == doc, mode
        assert result.minted == []
        assert list(result.drifted) == []
        assert M.lint_document(result.text, mode=mode, child_blocks=True)[1] == []


def test_relocation_refuses_to_move_a_malformed_record():
    """A diagnostic is not an identity, and the write path does not relocate one.

    §4 reads `<!-- stay:hash=x -->` as a malformed marker: it is a report about
    the document, anchored where its author wrote it. Moving it would carry the
    report to a line that has nothing wrong with it. The conservation inventory
    counts it, which is what makes the fused-fragment case above visible, so the
    restriction has to be stated separately rather than falling out of the
    inventory.
    """
    stamp_module = _stamp_module()

    text = "<!-- stay:hash=x -->\nbody\n"
    (record,) = stamp_module._active_records(text)
    assert record.marker.malformed
    assert stamp_module._identity_records(text) == []

    plan = stamp_module._Relocation(
        excisions=tuple(stamp_module._dropped_line_spans(text, [0])),
        moves=(record,),
        insert_at=stamp_module._line_offsets(text)[1] + len("body"),
        prefix="\n",
    )

    assert stamp_module._relocate(text, plan) is None


def test_the_write_path_cannot_hide_text_by_re_pairing_a_comment():
    """A relocation must not change how much of the document a reader can see.

    `p` sits inside another host comment, and it is `p`'s own `-->` that closes
    that comment. Remove `p` and the outer opener finds the NEXT closer instead,
    so `secret -->`, which was visible text beside a comment, becomes the inside
    of one. Every byte survives, no marker is lost, duplicated, invented or
    substituted, and no diagnostic appears, which is why no other rule here can
    see it: what changed is the pairing, not the bytes.

    A marker may therefore only leave a position where it is a host comment in
    its own right, and may only land in one.
    """
    import markstay as M

    for doc in (
        "| h |\n|---|\n| a<!-- before <!-- stay:p -->secret --> |\n",
        "| h |\n|---|\n| a{/* before {/*stay:p*/}secret */} |\n",
    ):
        assert M.lint_document(doc, child_blocks=True)[1] == []
        for mode in ("blank-line", "commonmark"):
            result = M.stamp(doc, child_blocks=True, mode=mode, new_id=ids("m0", "m1"))
            assert result.text == doc, (mode, doc)
            assert result.minted == []
            assert list(result.drifted) == []


def test_a_comment_beside_a_marker_does_not_block_a_row_stamp():
    """The independence rule is about NESTING, not about neighbours.

    An ordinary comment next to a container marker closes itself, so the marker is
    still a host comment in its own right and relocating it re-pairs nothing. Nor
    does ordinary text after the marker on the same line: the seam it leaves joins
    prose to prose, which writes no comment.
    """
    import markstay as M

    row = "| a<!-- stay:m0 subhash=sha256:ca978112ca1b --> |"
    doc = "<!-- stay:p --> tail\n| h |\n|---|\n| a |\n"
    result = M.stamp(doc, child_blocks=True, new_id=ids("m0", "m1"))
    assert result.text == f" tail\n| h |\n|---|\n{row}\n<!-- stay:p -->\n"
    assert [entry["id"] for entry in result.minted] == ["m0"]
    assert M.lint_document(result.text, child_blocks=True)[1] == []


def test_an_ordinary_comment_in_the_container_refuses_the_row_carrier():
    """SPEC.md §3.4 refuses on presence, and an ordinary comment is presence.

    Deciding that this particular comment closes itself is the predicate that was
    written and withdrawn in review round 4, and `--!>` is why: HTML closes a
    comment at a spelling CommonMark does not recognise, so "closed" is not one
    question. A marker is different and is masked, because §4 defines its bytes.

    The relocation is then **declined rather than made and rolled back**: moving
    a stay is itself an edit, so a preparation that buys no row stay does not
    happen. The container's rows go with it, because §5.6 wants the container's
    own stay on a marker-only line after the body and a row stamped without the
    move would leave it inside a cell.
    """
    import markstay as M

    doc = "<!-- a note --><!-- stay:p -->\n| h |\n|---|\n| a |\n"
    result = M.stamp(doc, child_blocks=True, new_id=ids("m0", "m1"))
    assert result.text == doc
    assert result.minted == []
    assert result.refused is None
    assert result.refused_carriers == [{"kind": "row", "line": 4}]

    prepared, drifted, declined = _stamp_module()._prepare_row_containers(
        doc, "blank-line", "html"
    )
    assert drifted == []
    # Keyed on the container's content rather than on its stay's id: §7 duplicates
    # are an error `stamp` does not refuse, and a shared id would spread this
    # refusal to a different table.
    assert declined == {"<!-- a note -->\n| h |\n|---|\n| a |"}
    assert prepared == doc


def test_relocation_refuses_to_land_a_record_inside_a_comment():
    """Rule 5's destination half. Moving a marker into an open comment hides it
    from every reader while leaving it perfectly visible to the §4 scanner, which
    considers each opener independently and so still reports it as a record. The
    landing comparison is satisfied; the document is not.
    """
    stamp_module = _stamp_module()

    text = "<!-- stay:p -->\nbody <!-- open\nmore -->\n"
    (record,) = stamp_module._identity_records(text)
    offsets = stamp_module._line_offsets(text)

    plan = stamp_module._Relocation(
        excisions=tuple(stamp_module._dropped_line_spans(text, [0])),
        moves=(record,),
        insert_at=offsets[1] + len("body <!-- open"),
        prefix="\n",
    )

    assert stamp_module._relocate(text, plan) is None


def test_the_write_path_cannot_write_a_comment_nobody_opened():
    """The seam an excision leaves must not become markup.

    `p` here is a host comment in its own right, at both ends of the edit, so
    checking only the records that move says the relocation is safe. It is not:
    removing `p` joins `a<` to `!--secret -->` and writes `<!--secret -->`, a
    comment nobody opened, so the cell renders as `a` instead of
    `a<!--secret -->`. No record changed and no record moved anywhere it should
    not have; what appeared is a comment, which is why the rule has to conserve
    comment STRUCTURE rather than inspect the moved records.
    """
    import markstay as M

    doc = "| h |\n|---|\n| a<<!-- stay:p -->!--secret --> |\n"
    for mode in ("blank-line", "commonmark"):
        result = M.stamp(doc, child_blocks=True, mode=mode, new_id=ids("m0", "m1"))
        assert result.text == doc, mode
        assert result.minted == []
        assert list(result.drifted) == []


def test_a_fence_inside_a_raw_html_block_does_not_license_a_relocation():
    """Fenced code is not masked when reading comment structure, and this is why.

    Inside a raw HTML block a line of backticks opens no code fence, so
    `<!-- before` here is a live comment opener whatever the §3.3 line rule says
    about it. Masking those lines would hide the opener, make `p` look
    independent, and let the relocation hand the opener a later closer: `secret`
    stops being comment content and appears in the rendered cell.

    §3.3's mask is the spec's own rule about which markers are identities and it
    still applies to the identity inventory. It is not a claim about where a
    renderer sees comments, and it is not used as one.
    """
    import markstay as M

    doc = (
        "<div>\n"
        "```\n"
        "<!-- before\n"
        "```\n"
        "| h |\n"
        "|---|\n"
        "| a<!-- stay:p -->secret --> |\n"
    )
    for mode in ("blank-line", "commonmark"):
        result = M.stamp(doc, child_blocks=True, mode=mode, new_id=ids("m0", "m1"))
        assert result.text == doc, mode
        assert result.minted == []
        assert list(result.drifted) == []


def test_relocation_refuses_a_record_no_comment_reading_reaches():
    """Rule 5's first half, which the conservation check does not imply.

    An `<!--` with no closer anywhere ends the comment scan: everything after it
    is inside that comment as far as a reader is concerned, and there is nothing
    further to pair. `p` therefore has no comment span of its own, and moving it
    within that region conserves comment structure perfectly, because there is
    none left to conserve.

    Conserving nothing is not the same as knowing the edit is safe. Requiring a
    moved record to BE a comment span is what turns "no reading reached here" into
    a refusal rather than a pass.
    """
    stamp_module = _stamp_module()

    text = "<!-- unclosed\n{/*stay:p*/}\nbody\n"
    assert stamp_module._comment_spans(stamp_module._mask_metadata(text)) == set()
    (record,) = stamp_module._identity_records(text)

    plan = stamp_module._Relocation(
        excisions=tuple(stamp_module._dropped_line_spans(text, [1])),
        moves=(record,),
        insert_at=stamp_module._line_offsets(text)[2] + len("body"),
        prefix="\n",
    )

    assert stamp_module._relocate(text, plan) is None


def test_a_refusal_is_distinguishable_from_a_no_op():
    """A refusal and a document with nothing to do return the same text and the
    same empty `minted`, so without a reason a caller cannot tell them apart.

    The transactional row write refuses on purpose and often, and every rule the
    relocation guard carries is another way to reach that return. Round 7's first
    finding was a legal stamp silently refused, and the only symptom was silence:
    it took an external review round to notice. `refused` is what a caller reads
    instead.
    """
    import markstay as M

    refused = M.stamp(
        "| h |\n|---|\n| a<!-- before <!-- stay:p -->secret --> |\n",
        child_blocks=True,
        new_id=ids("m0"),
    )
    assert refused.refused == "unsafe-relocation"
    assert refused.minted == []

    drifted = M.stamp(
        "<!-- stay:p hash=sha256:000000000000 -->\n| h |\n|---|\n| a |\n",
        child_blocks=True,
        new_id=ids("m0"),
    )
    assert drifted.refused == "container-drift"
    assert list(drifted.drifted) == ["p"]

    # A genuine no-op: already stamped, nothing to do, and no refusal.
    stamped = M.stamp(
        "| h |\n|---|\n| a |\n",
        child_blocks=True,
        new_id=ids("m0", "m1"),
    )
    again = M.stamp(stamped.text, child_blocks=True, new_id=ids("m2"))
    assert again.text == stamped.text
    assert again.minted == []
    assert again.refused is None


def test_the_cli_exits_non_zero_on_a_refusal(tmp_path):
    """The API distinction is only useful if the CLI carries it. A pre-commit hook
    reading the exit code is the caller that most needs to know the difference,
    and it used to be told `0 id(s) minted` and a clean exit for work the write
    path had declined.
    """
    import subprocess
    import sys

    refused = tmp_path / "refused.md"
    refused.write_text("| h |\n|---|\n| a<!-- before <!-- stay:p -->secret --> |\n")
    run = subprocess.run(
        [sys.executable, "-m", "markstay.cli", "stamp", "--child-blocks", str(refused)],
        capture_output=True,
        text=True,
    )
    assert run.returncode == 1
    assert "refused (unsafe-relocation)" in run.stderr
    assert "id(s) minted" not in run.stderr

    noop = tmp_path / "noop.md"
    noop.write_text("para\n<!-- stay:a hash=sha256:8b8d1b48c0cd -->\n")
    clean = subprocess.run(
        [sys.executable, "-m", "markstay.cli", "stamp", "--child-blocks", str(noop)],
        capture_output=True,
        text=True,
    )
    assert clean.returncode == 0
    assert "0 id(s) minted" in clean.stderr


def test_a_document_with_no_row_work_is_a_no_op_not_a_refusal(tmp_path):
    """A speculative §5.6 migration that produces no table is not work declined.

    `| ordinary text |<!-- stay:p -->` is an already-stamped paragraph that
    happens to contain pipes. The legacy-suffix probe tries to migrate it, the
    relocation itself checks out, and the result is still not a table, so there
    was never a migration here to do. Reporting that as a refusal tells a caller
    the write path withheld work on a document it has nothing to do with, and the
    CLI turns that into a non-zero exit on a clean file.

    The distinction only became visible once refusals were reportable at all,
    which is the point of making them reportable.
    """
    import subprocess
    import sys

    import markstay as M

    doc = "| ordinary text |<!-- stay:p -->\n"
    assert M.lint_document(doc, child_blocks=True)[1] == []

    for mode in ("blank-line", "commonmark"):
        result = M.stamp(doc, child_blocks=True, mode=mode, new_id=ids("m0"))
        assert result.refused is None, mode
        assert result.text == doc
        assert result.minted == []

    path = tmp_path / "pipes.md"
    path.write_text(doc)
    run = subprocess.run(
        [sys.executable, "-m", "markstay.cli", "stamp", "--child-blocks", str(path)],
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0
    assert "0 id(s) minted" in run.stderr


def test_a_relocation_that_would_refit_an_existing_rows_cells_is_refused():
    """SPEC.md §5.6: a container-stay relocation is provisional until the whole
    proposed document has been re-read, and what has to survive it is every row's
    hash body, not only the children the pass mints.

    Removing a marker from inside a cell normally leaves that cell's body alone,
    because a row body is computed with markers already cut. It does not when the
    bytes left behind change meaning: `a\\<!-- stay:p -->|` becomes `a\\|`, the
    backslash now escapes the delimiter, and the two cells either side fuse into
    one. Row 1's body changes, so the `subhash` already stored on that row covers a
    body that no longer exists.

    Nothing downstream would report it. §11's diff sees the child marker still
    present under the same id, the container refresh re-checks only children this
    pass minted, and a block-level lint of the result is clean, so the document
    would go out carrying row evidence that silently no longer matches. The write
    is therefore abandoned whole, which for a probe means byte-identical source and
    nothing reported as drifted: the document was consistent before the tool
    touched it.
    """
    import markstay as M

    row_body = "a\\\\|b"
    doc = (
        "| h | h |\n"
        "|---|---|\n"
        f"| a\\<!-- stay:p hash=sha256:d911b9ba7908 -->| b{child_marker('r', row_body)} |\n"
        "| c | d |\n"
    )
    # The fixture only tests what it says if the document starts consistent: the
    # container hash matches its body and row 1's subhash matches row 1's body.
    container = M.parse_document(doc)[0]
    stored = next(m for m in container.markers if m.hash)
    assert stored.hash == M.body_hash(container.content, len(stored.hash))
    before = [c.content for c in M.parse_document(doc, child_blocks=True)[0].children]
    assert before[0] == row_body

    result = M.stamp(doc, child_blocks=True, new_id=ids("n1", "n2", "n3"))
    assert result.refused == "unsafe-relocation"
    assert result.text == doc
    assert result.minted == []
    assert result.drifted == []


def test_a_duplicate_container_id_cannot_steer_the_guard_at_the_wrong_table():
    """The candidate a relocation validates is found by matching the parent marker's
    id and raw text, so a duplicate id (§7, an error, but not one ``stamp`` refuses)
    makes that match land on the first table rather than the one being written.

    Here the first table's rows are untouched and the second table's relocation is
    the corrupting one. A guard that compared the candidate's rows would compare the
    first table against itself, pass, and let the write through: `r2` kept its stored
    digest while its body became `a\\\\\\| b`, adding a HASH_DRIFT the document did not
    have. The invariant is therefore every row body in the document, which no
    legitimate relocation changes.
    """
    import markstay as M

    doc = (
        "| h | h |\n|---|---|\n"
        "| a\\<!-- stay:r0 subhash=sha256:0c12c17a7c55 -->| b |\n"
        "| c | d<!-- stay:r1 subhash=sha256:455b714a6669 --> |\n"
        "<!-- stay:p -->\n"
        "\n"
        "| h | h |\n|---|---|\n"
        "| a\\<!-- stay:p -->| b<!-- stay:r2 subhash=sha256:0c12c17a7c55 --> |\n"
        "| c | d |\n"
    )

    def row_digests(text):
        return {
            marker.id: M.body_hash(child.content, 12)
            for block in M.parse_document(text, child_blocks=True)
            for child in block.children
            if child.kind == "row"
            for marker in child.markers
            if marker.subhash
        }

    before = row_digests(doc)
    # Every stored row digest matches its body, so the only drift a reader could
    # report afterwards would be drift this write introduced.
    for block in M.parse_document(doc, child_blocks=True):
        for child in block.children:
            for marker in child.markers:
                if marker.subhash:
                    assert marker.subhash.endswith(before[marker.id])

    result = M.stamp(doc, child_blocks=True, new_id=ids("n0", "n1", "n2"))
    assert result.refused == "unsafe-relocation"
    assert result.text == doc
    assert result.minted == []
    assert row_digests(result.text) == before
