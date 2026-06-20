"""External transform-safety fixture: does a real Markdown formatter preserve
markstay markers across a reformat?

mdformat (built on markdown-it-py) is the canonical deterministic formatter; a
reformat is exactly the §11 transform-safety case the spec cares about, minus the
LLM. This pins that an `mdformat` pass over a stay-marked document drops, relocates,
or duplicates *nothing*: every id survives and stays bound to its block. The one
allowed side effect is an honest §8 HASH_DRIFT where the formatter genuinely rewrites
a block's source (bullet style), which is the correct "same block, content changed"
signal, not a failure.

mdformat is a test-only tool the package never imports, so this skips when it is not
installed (e.g. the publish CI that runs the corpus without dev extras). Install it
with `pip install -e ".[test]"`.
"""

import pytest

mdformat = pytest.importorskip("mdformat")

from markstay.lint import find_markers, lint_diff

# One marker per block kind: paragraph, list, fenced code, blockquote, table.
# Markers sit on the line right after their block (no blank line); mdformat will
# insert a blank line between block and marker, which is the interesting case.
DOC = """\
# Title

First paragraph about the thing.
<!-- stay:p1 -->

* item one
* item two
* item three
<!-- stay:l1 -->

```python
def f():
    return 1
```
<!-- stay:c1 -->

> a quoted line
> second quoted line
<!-- stay:q1 -->

| a | b |
|---|---|
| 1 | 2 |
<!-- stay:t1 -->
"""

ALL_IDS = {"p1", "l1", "c1", "q1", "t1"}


def test_mdformat_preserves_every_marker():
    after = mdformat.text(DOC)

    # 1. every marker survives the reformat (byte-for-byte at the id level).
    assert {m.id for m in find_markers(DOC)} == ALL_IDS
    assert {m.id for m in find_markers(after)} == ALL_IDS

    # 2. regeneration diff: nothing dropped, duplicated, or relocated. These are
    #    the error-level §11 failures; the formatter must produce none.
    findings = lint_diff(DOC, after)
    errors = [(f.code, f.message) for f in findings if f.level == "error"]
    assert errors == [], errors

    # 3. the only effect is an in-place drift on the list, whose bullets mdformat
    #    rewrites (`*` -> `-`). §8 does not normalize bullet style, so this is real
    #    content drift on the same block, correctly reported, and a `restamp` clears
    #    it. No other block drifts (blank-line insertion alone is not drift).
    drifted = {f.id for f in findings if f.code == "HASH_DRIFT"}
    assert drifted == {"l1"}, drifted


def test_mdformat_inserts_a_gap_but_attachment_holds():
    # Documents the mechanism: mdformat puts a blank line between a block and its
    # trailing marker, turning it into a marker-only chunk. Attachment survives
    # because that chunk binds to the preceding block (already proven by the
    # zero-error diff above); this just pins the observed gap so a future mdformat
    # spacing change is noticed here rather than silently.
    after = mdformat.text(DOC)
    assert "thing.\n\n<!-- stay:p1 -->" in after
