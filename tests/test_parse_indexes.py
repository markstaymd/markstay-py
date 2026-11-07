"""Semantic and work-bound regressions for child ownership and row refusals."""
import random
from dataclasses import replace
from unittest.mock import patch

import importlib

L = importlib.import_module("markstay.lint")


class CountedList(list):
    def __init__(self, values):
        super().__init__(values)
        self.reads = 0

    def __getitem__(self, index):
        self.reads += 1
        return super().__getitem__(index)


def test_child_ownership_checks_only_possible_spans():
    count = 200
    text = "".join(
        f"- item {i} <!--stay:c{i} subhash=sha256:aa -->\n"
        for i in range(count)
    )
    with patch.object(L, "_record_belongs_to_child", wraps=L._record_belongs_to_child) as check:
        blocks = L.parse_document(text, child_blocks=True)
    children = blocks[0].children
    assert len(children) == count
    assert [child.content for child in children] == [f"item {i}" for i in range(count)]
    assert [[m.id for m in child.markers] for child in children] == [
        [f"c{i}"] for i in range(count)
    ]
    assert blocks[0].markers == []
    assert check.call_count <= 2 * count


def test_strip_index_clips_crossing_ranges_and_visits_only_nearby_ranges():
    count = 200
    text = "".join(f"- item {i} <!--stay:c{i} -->\n" for i in range(count))
    records = L._scan_marker_records(text)
    index = L._MarkerRangeIndex(records)
    index.ranges = CountedList(index.ranges)
    offset = 0
    for line in text.splitlines(keepends=True):
        assert index.strip(line, source_start=offset) == L._strip_record_ranges(
            line, records, source_start=offset
        )
        offset += len(line)
    assert index.ranges.reads <= 2 * count
    # Includes empty slices, overlapping host records, adjacent markers, and
    # slices beginning/ending inside a marker instead of at line boundaries.
    rng = random.Random(201102)
    text = "a<!--stay:x a=<!--stay:y -->{/*stay:z */}tail"
    records = L._scan_marker_records(text)
    index = L._MarkerRangeIndex(records)
    for _ in range(200):
        start, end = sorted((rng.randrange(len(text) + 1), rng.randrange(len(text) + 1)))
        assert index.strip(text[start:end], source_start=start) == L._strip_record_ranges(
            text[start:end], records, source_start=start
        )


def test_row_refusals_match_pair_oracle_with_linear_record_reads():
    text = "| " + "<!--stay:x a=" * 200 + "tail --> |"
    records = L._scan_marker_records(text)
    enumerated = 0

    def counted_range(*args):
        nonlocal enumerated
        values = range(*args)
        enumerated += len(values)
        return values

    # Count both candidate-pair iteration and emitted line ranges. The old
    # pair loop visits O(N**2) values even though its result is just one line.
    with patch.object(L, "_scan_marker_records", return_value=records), patch.object(
        L, "range", counted_range, create=True
    ):
        assert L._row_refused_marker_lines(text, set()) == {1}
    assert enumerated <= 2 * len(records)

    rng = random.Random(211102)
    text = "line\n" * 20
    template = records[0]
    for _ in range(100):
        synthetic = []
        for i in range(rng.randrange(1, 20)):
            start = rng.randrange(len(text))
            end = rng.randrange(start + 1, len(text) + 1)
            marker = replace(template.marker, line=text.count("\n", 0, start) + 1)
            synthetic.append(replace(template, start=start, end=end, marker=marker))
        synthetic.sort(key=lambda r: (r.start, r.end))
        expected = set()
        for i, record in enumerate(synthetic):
            overlaps = any(
                other.start < record.end and record.start < other.end
                for j, other in enumerate(synthetic) if i != j
            )
            first = text.count("\n", 0, record.start) + 1
            last = text.count("\n", 0, record.end - 1) + 1
            if first != last or overlaps:
                expected.update(range(first, last + 1))
        with patch.object(L, "_scan_marker_records", return_value=synthetic):
            assert L._row_refused_marker_lines(text, set()) == expected


def test_row_refusals_emit_each_multiline_refused_line_once():
    count = 400
    text = "line\n" * count
    template = L._scan_marker_records("<!--stay:x -->")[0]
    records = [
        replace(template, start=i, end=len(text) if i % 2 == 0 else i + 6, marker=replace(
            template.marker, line=text.count("\n", 0, i) + 1
        ))
        for i in range(count)
    ]
    enumerated = 0

    def counted_range(*args):
        nonlocal enumerated
        values = range(*args)
        enumerated += len(values)
        return values

    with patch.object(L, "_scan_marker_records", return_value=records), patch.object(
        L, "range", counted_range, create=True
    ):
        assert L._row_refused_marker_lines(text, set()) == set(range(1, count + 1))
    assert enumerated <= 2 * count


def test_mixed_child_ownership_falls_back_from_row_to_list():
    text = (
        "- parent\n"
        "  | h | k |\n"
        "  | - | - |\n"
        "  | a | b<!--stay:r subhash=sha256:bb --> |\n"
        "  <!--stay:l subhash=sha256:aa -->\n"
    )
    for mode in ("blank-line", "commonmark"):
        blocks = L.parse_document(text, mode=mode, child_blocks=True)
        assert len(blocks) == 1
        children = {child.kind: child for child in blocks[0].children}
        assert set(children) == {"list", "row"}
        assert [marker.id for marker in children["row"].markers] == ["r"]
        assert [marker.id for marker in children["list"].markers] == ["l"]
        assert blocks[0].markers == []
