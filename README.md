# markstay , Python reference implementation (v1 core)

[![PyPI](https://img.shields.io/pypi/v/markstay)](https://pypi.org/project/markstay/)
[![Python versions](https://img.shields.io/pypi/pyversions/markstay)](https://pypi.org/project/markstay/)
[![tests](https://img.shields.io/github/actions/workflow/status/markstaymd/markstay-py/test.yml?label=tests)](https://github.com/markstaymd/markstay-py/actions/workflows/test.yml)
[![spec](https://img.shields.io/badge/spec-v1.1-blue)](https://markstay.org)
![License](https://img.shields.io/pypi/l/markstay)

The Python reference implementation of the [markstay spec](https://markstay.org)
(v1.1). markstay is a source-level identity primitive for Markdown blocks: an id
token that **stays** bound to its block across edits (marker `stay:`), so a
reference to a block survives the document being rewritten, including by an LLM.

This is the **parser-free core**: everything string-level and parser-independent
(§8 hashing, §3/§4 marker grammar, §5 blank-line segmentation, §6 id minting, the
§3/§4/§7/§8 write path, §7/§11 lint, §9 quote recovery, §9.1 resolution ladder).
It mirrors the JavaScript reference
([`markstay` on npm](https://www.npmjs.com/package/markstay)); both are gated by a
shared language-neutral conformance corpus, which turns "two implementations
agree" from an assertion into a tested fact.

## Install

```sh
pip install markstay
```

Zero runtime dependencies (Python standard library only). CommonMark-tree
segmentation (§5.2) is an optional extra:

```sh
pip install "markstay[commonmark]"   # pulls in markdown-it-py
```

Requires Python >= 3.9.

## Library

```python
import markstay as M

md = "The ingest stage retries three times.\n<!-- stay:a1b2 -->\n"

# parse into content blocks with attached markers (§5)
blocks = M.parse_document(md)

# well-formedness + intra-doc invariants (§7): duplicate/orphan/malformed/drift
_, findings = M.lint_document(md)

# regeneration diff (§11): what an edit did to the ids (dropped/duplicated/moved)
findings = M.lint_diff(before_md, after_md)

# §8 content hash (ASCII-normalized SHA-256)
M.body_hash("some block body")

# §9.1 resolution ladder: re-attach ids after an edit, or report DETACHED
anchors = M.build_anchors(before_md)
resolutions = M.resolve(anchors, after_md)   # id -> marker | hash | quote | detached

# write path: mint ids for unmarked blocks (§6), append the §3.1 trailing marker
res = M.stamp("First paragraph.\n\nSecond paragraph.\n")
res.text     # each block now carries <!-- stay:ID hash=sha256:... -->
res.minted   # [{"id": ..., "line": ...}, ...]

# refresh a hash you edited on purpose (§8); repair duplicate ids (§7, copy mints new)
M.restamp(edited_md)            # -> RestampResult(text, refreshed)
M.repair_duplicates(copied_md)  # -> RepairResult(text, renamed)
```

Public API (mirrors the JS `index.js` surface): `normalize_body`, `body_hash`,
`Marker`, `find_markers`, `strip_markers`, `rewrite_markers`,
`segment_blank_line`, `segment_commonmark`, `Block`, `parse_document`, `Finding`,
`lint_document`, `lint_diff`, `sort_findings`, `has_errors`, `mint_id`,
`ID_CHARSET`, `format_marker`, `format_attr_value`, `stamp`, `restamp`,
`repair_duplicates`, `DEFAULT_HASH_LENGTH`, `Selector`, `normalize`,
`body_score`, `context_bonus`, `best_match`, `CONTEXT_CHARS`, `Anchor`,
`Resolution`, `build_anchors`, `resolve`, `DEFAULT_THRESHOLD`, `DEFAULT_MARGIN`.

## CLI

```sh
markstay lint    FILE [FILE ...]      # well-formedness + intra-doc checks
markstay lint    --before OLD.md NEW  # regeneration diff (dropped/duplicated/relocated ids)
markstay lint    --json ...           # machine-readable findings
markstay lint    --commonmark ...     # §5.2 CommonMark-tree segmentation (needs the extra)
markstay stamp   FILE... [-w]         # mint ids for unmarked blocks (§6)
markstay restamp FILE... [-w]         # refresh hashes that drifted (§8)
markstay repair  FILE... [-w]         # mint fresh ids for duplicate ids (§7)
```

`lint` exits non-zero when any error-level finding is reported, so it gates a
commit hook or an agent's post-edit step. The write verbs print the result to
stdout by default; `-w`/`--write` edits files in place.

## The conformance corpus (the actual deliverable)

The corpus under [`conformance/`](conformance) is shared with the JavaScript
reference. **290 vectors** across two tiers:

- **`spec/`** , hand-authored from the spec prose, asserting what the *words*
  require. These are authority; a `spec/` vector the reference fails is a
  reference bug, not a corpus error.
- **`gen/`** , emitted from the reference for breadth/regression.

The JS reference runs the same JSON, so the two runners are a cross-impl
regression sentinel: any later change to either implementation that breaks
agreement fails one of them.

## Running the tests

```sh
pip install -e ".[commonmark]"
pytest
```

## License

MIT
