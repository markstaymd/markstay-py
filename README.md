# markstay , Python reference implementation (v1 core)

[![PyPI](https://img.shields.io/pypi/v/markstay)](https://pypi.org/project/markstay/)
[![Python versions](https://img.shields.io/pypi/pyversions/markstay)](https://pypi.org/project/markstay/)
[![tests](https://img.shields.io/github/actions/workflow/status/markstaymd/markstay-py/test.yml?label=tests)](https://github.com/markstaymd/markstay-py/actions/workflows/test.yml)
[![spec](https://img.shields.io/badge/spec-v1.8-blue)](https://markstay.org)
![License](https://img.shields.io/pypi/l/markstay)

The Python reference implementation of the [markstay spec](https://markstay.org)
(v1.8). markstay is a source-level identity primitive for Markdown blocks: an id
token that **stays** bound to its block across edits (marker `stay:`), so a
reference to a block survives the document being rewritten, including by an LLM.

This is the **parser-free core**: everything string-level and parser-independent
(§8 hashing, §3/§4 marker grammar, §5 blank-line segmentation, §6 id minting, the
§3/§4/§7/§8 write path, §7/§11 lint, §9 quote recovery, §9.1 resolution ladder).
It mirrors the JavaScript reference
([`markstay` on npm](https://www.npmjs.com/package/markstay)); both are gated by a
shared language-neutral conformance corpus, which turns "two implementations
agree" from an assertion into a tested fact.

**Child-block identity (§5.5 and §5.6) is implemented here**, behind
`--child-blocks` on the CLI and `child_blocks=True` in the API: a direct list item
or accepted GFM table body row may carry its own stay under the reserved `subhash`
key, resolved through the §9.2 ladder. It is opt-in because §16 makes segmenting and
resolving child blocks optional. The read/write safety rules §16 makes mandatory are
unconditional here, as they are in every implementation.

**Write safety (§3.4, v1.8).** Child markers are inserted only at carriers that
pass the plain-text predicate. It scans the container's original source prefix
outside existing plain markers, and skips a child when the prefix contains `<`,
a backslash, or (in MDX) `{`. At a list-item carrier the content of a **closed**
inline code span is masked out of that scan first, so `` `<repo>` `` in an earlier
item does not refuse a later one; the scan reads one line at a time and stops at
the first line whose backtick runs do not pair evenly, and an unclosed run masks
nothing. A flush row carrier keeps the unmasked rule, because GFM splits cells
before inline parsing, and also refuses a trailing `*`, `_` or `~`. Each skipped
position is reported in `StampResult.refused_carriers` and by the CLI; other
writable children can still receive stays. Table marker
relocations must also pass the removal and insertion checks. Inline recovery
evidence belongs in a side index, because a new child marker carries only an id
and a digest.

These checks preserve the specification's byte-level contract; they do not
promise identical rendering for arbitrary Markdown. With the CommonMark extra
installed, the linter emits the informational `OUTSIDE_SUBSET` advisory when
§5.1 and §5.2 would select different blocks. Without the extra it emits no such
advisory. Use `mode="commonmark"` or `--commonmark` to opt into tree segmentation.

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

## Keeping stays alive through an agent's edit (start here)

Almost every stay that goes missing goes missing the same way: a model rewrote the
document and did not know the markers were load-bearing. The eval measured both
halves of the fix, and they are not close , a naive "clean this up" rewrite keeps
about **5%** of markers, the same rewrite carrying the SPEC.md §11 instruction keeps
**~96-100%**, across five models and three vendors. That outweighs model tier.

```sh
markstay preserve                        # the §11 instruction, ready to paste into
                                         #   AGENTS.md / CLAUDE.md / a system prompt
markstay preserve --wrap DOC.md          # the instruction wrapped around a document,
                                         #   as a complete editing prompt
markstay preserve --wrap DOC.md --task "Rewrite this to be clearer."
```

```python
import markstay as M

M.PRESERVE_INSTRUCTION            # the §11 contract, worded for an editing agent
M.preserve_wrap(doc, "Tighten it.")   # the prompt shape the eval measured
```

The instruction is byte-identical in the npm and crates.io packages, held there by
the shared conformance corpus rather than by convention.

Everything below is the **backstop**: it catches loss after the fact, it does not
prevent it. Ship the instruction first.

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

detached = resolutions["a1b2"]
detached.reason       # ambiguous | unmatched, or a child-path reason
detached.candidates   # diagnostic contenders for ambiguity, never a committed target

# write path: mint ids for unmarked blocks (§6), append the §3.1 trailing marker
res = M.stamp("First paragraph.\n\nSecond paragraph.\n")
res.text     # each block now carries <!-- stay:ID hash=sha256:... -->
res.minted   # [{"id": ..., "line": ...}, ...]
res.drifted  # container ids whose pre-existing drift made a row write abort
res.refused_carriers  # child positions skipped by §3.4, with returned-text line numbers
res.refused  # why the whole write path declined, or None; a refusal returns the source
             # byte for byte with nothing minted, which is what a document with
             # nothing to do returns too

# refresh a hash you edited on purpose (§8); repair duplicate ids (§7, copy mints new)
M.restamp(edited_md)            # -> RestampResult(text, refreshed)
M.repair_duplicates(copied_md)  # -> RepairResult(text, renamed)
```

### Child-block identity

The Python package implements the optional list-item and GFM table-row readers and
writers from §5.5 and §5.6. Enable child segmentation explicitly:

```python
seeded = M.stamp(md, mode="commonmark", child_blocks=True).text
_, findings = M.lint_document(seeded, mode="commonmark", child_blocks=True)
findings = M.lint_diff(before, after, mode="commonmark", child_blocks=True)

anchors = M.build_child_anchors(seeded, mode="commonmark")
resolved = M.resolve_children(anchors, after, mode="commonmark")
```

Child markers use `subhash=sha256:...`; list markers sit at the end of the item's
last paragraph, while row markers sit flush inside the last cell. The same stamping
pass puts the selected container's stay on a marker-only line. A legacy bare table
stay after the last row's closing pipe is migrated only after a full-document probe
accepts the proposed table. Pre-existing container hash drift aborts that write and
is exposed through `StampResult.drifted` and a nonzero CLI result.

`restamp(..., child_blocks=True)` refreshes `subhash` and never injects a parent
`hash` into a child. If an older
`restamp --add-missing` already added that parent hash,
`repair_duplicates(..., child_blocks=True)` removes the detectable residue.

CommonMark mode handles direct list-item source spans. The dependency-free mode
fails closed outside flat, tight, single-paragraph lists. Measured attachment
safety is 0% false attachment from two independent directions (0/324 deterministic,
95% upper bound 0.92%; 0/256 on real gpt4o rewrites, bound 1.16%) at 95.0% and
92.2% recovery. Table rows use the parser-free §5.6 line scan in both segmenter
modes and keep list and row ordinals and sibling recovery scopes separate.
That scan accepts only outer-pipe rows, a same-width delimiter row whose cells are
`:`/`-` grammar, and an uninterrupted all-row body. Marker spans are opaque while
pipe boundaries are fixed, multiline or overlapping spans refuse affected row
candidates, and more than one accepted candidate in a selected container fails
closed. Regardless of whether child segmentation is enabled, §16 prevents an exact
parsed `subhash` key from identifying its containing block or receiving `hash`.

Row writes are transactional. The package builds a complete in-memory document,
moves only a legal legacy bare container suffix, places the container stay on a
marker-only line, writes row stays flush in their final cells, and reruns the selected
segmenter plus the complete row scan. A failed probe or revalidation returns the
source byte-for-byte unchanged. Existing container hash drift is reported before the
write through `StampResult.drifted`; the CLI exits nonzero and leaves the file alone.

**Every refusal names itself.** `StampResult.refused` carries a short reason
(`container-drift`, `unsafe-relocation`, `no-row-carrier`, `child-not-addressable`,
`parent-not-addressable`, `proposal-drifts`) or `None` when the write path did not
decline, and the CLI prints it and exits nonzero. Without it a refusal is
byte-identical to a document that had nothing to stamp, so a pre-commit hook reading
the exit code would pass on work the write path had declined.

Public API (the spec'd portion mirrors the JS `index.js` surface; child names are
experimental Python-only): `normalize_body`, `body_hash`,
`Marker`, `find_markers`, `strip_markers`, `strip_markers_outside_code`,
`rewrite_markers`, `code_lines`, `fence_state`,
`segment_blank_line`, `segment_commonmark`, `segment_child_items`, `Block`,
`ChildBlock`, `child_body`, `parse_document`, `Finding`,
`lint_document`, `lint_diff`, `sort_findings`, `has_errors`, `mint_id`,
`ID_CHARSET`, `format_marker`, `format_attr_value`, `stamp`, `restamp`,
`repair_duplicates`, `DEFAULT_HASH_LENGTH`, `Selector`, `normalize`,
`body_score`, `context_bonus`, `rank_candidates`, `best_match`, `CONTEXT_CHARS`,
`Evidence`, `Candidate`, `Anchor`,
`Resolution`, `build_anchors`, `resolve`, `ChildAnchor`, `ChildResolution`,
`build_child_anchors`, `resolve_children`, `DEFAULT_THRESHOLD`, `DEFAULT_MARGIN`,
`PRESERVE_INSTRUCTION`, `PRESERVE_RETURN_ONLY`, `preserve_wrap`, `CommitEntry`,
`StagedCheck`, `check_entries`.

## CLI

```sh
markstay preserve                     # the §11 instruction for an editing agent
markstay preserve --wrap DOC.md       # that instruction + the doc, as a prompt
markstay lint    FILE [FILE ...]      # well-formedness + intra-doc checks
markstay lint    --before OLD.md NEW  # regeneration diff (dropped/duplicated/relocated ids)
markstay lint    --json ...           # machine-readable findings
markstay lint    --commonmark ...     # §5.2 CommonMark-tree segmentation (needs the extra)
markstay lint    --child-blocks --commonmark ...  # list-item and table-row identity
markstay resolve --before OLD.md NEW.md  # explain each attachment or detachment
markstay resolve --before OLD.md NEW.md --show-candidates  # show ambiguous contenders
markstay resolve --before OLD.md NEW.md --json  # versioned structured diagnostics
markstay check-staged [FILE...]       # the same diff against the staged commit
markstay check-worktree [FILE...]     # check files on disk before the next commit
markstay stamp   FILE... [-w]         # mint ids for unmarked blocks (§6)
markstay restamp FILE... [-w]         # refresh hashes that drifted (§8)
markstay repair  FILE... [-w]         # mint fresh ids for duplicate ids (§7)
```

`--child-blocks` is also accepted by `check-staged`, `check-worktree`, `stamp`,
`restamp`, and `repair`. It remains off unless requested.

`resolve` always requires the marked baseline and the edited document. Its normal
text output reports every state but keeps candidate details behind
`--show-candidates`. JSON uses `markstay.resolve/v1`; detached entries carry
`committed: false` and a `markstay.resolve-diagnostics/v1` object containing the
failed threshold and margin context, structured evidence codes, optional human
labels, and candidate provenance. The candidate set is diagnostic and is not an
attachment recommendation. `unmatched` omits sub-threshold candidates by design.

Block detachments use `ambiguous` or `unmatched`. The child path can also report
`unscored` when no match was attempted, `unaddressed` for a surviving child marker
that owns no direct child, and `contested` when multiple stays propose the same
tier-start candidate. A contest carries the proposed target and the ids of the
other stays that proposed it, while keeping `candidates` empty. Snapshot context
evidence is labelled as adjacency within that filtered candidate snapshot. A child
contest still continues to weaker tiers as §9.2 requires; only a stay that remains
detached reports its strongest contest. A child blocked by an unresolved parent
carries that parent resolution in `blocked_by`; a surviving child marker still
resolves independently.

`lint` exits non-zero when any error-level finding is reported, so it gates a
commit hook or an agent's post-edit step. The write verbs print the result to
stdout by default; `-w`/`--write` edits files in place.

## Gating commits (pre-commit framework)

`lint` needs two files. `check-staged` needs only a repo: it reads the staged commit
and finds each document's baseline itself, which is what a hook actually wants.

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/markstaymd/markstay-py
    rev: v0.12.0
    hooks:
      - id: markstay                  # or markstay-collections, to include table
                                      # rows and list bullets
```

It stays quiet unless there is something to act on: a commit that only edits stamped
blocks in place or mints new ids prints nothing, so the channel keeps meaning
something. `--show-drift` opts back in, `--json` for machine output.

**The baseline is resolved by stay id, not by filename.** git's rename detection is
content-similarity based, and similarity is anti-correlated with this failure mode:
the more a rewrite destroys, the more stays it can drop *and* the less git sees a
rename. A measured real case scored 2% similarity, so git recorded delete + create
and a path-keyed baseline found nothing to compare against. A surviving stay id is
the stronger signal. An id that moved to another document in the same commit is
reported as a move rather than a loss, so reorganising documents does not block.

## Segmentation notes

- **Leading YAML frontmatter is metadata, not a block (§5.3):** it is skipped before
  segmentation, so it is never a block, never stamped, and never hashed , a
  metadata-only edit (`status: draft` -> `status: done`) must not read as a content
  edit. Recognition is conservative, because `---` is also a thematic break and a
  setext underline: a span counts only when line 1 is exactly `---`, a later line is
  exactly `---` or `...`, the payload between them is non-empty with no blank line,
  and at least one payload line is unambiguously YAML (a `key:` or a `- item`). A
  YAML *comment* does not count, since `# x` is also an ATX heading. "Unambiguously
  YAML" is judged with ASCII whitespace, as everywhere else in the spec (§8/§9): the
  runtimes' own Unicode whitespace sets disagree with each other, and a rule that
  DELETES a span must not vary by implementation. The conditions confine the
  ambiguity rather than removing it, and the rule does not pretend otherwise: a
  blank-free payload that reads as YAML is *also* ordinary Markdown, whether it is a
  sequence (`---` / `- Keep this` / `---`, a list between two thematic breaks) or a
  mapping (`---` / `title: v` / `---`, a setext heading under one). Both are accepted
  and their content is excluded. **Frontmatter wins**, the same call every mainstream
  Markdown site generator makes on the same bytes. A document that fails any of the
  four conditions (no opening `---`, no closing fence, a blank line in the payload,
  no payload line that reads as YAML) falls through to ordinary Markdown, where the
  worst case is a spurious block and a stray hash-drift warning. A marker an older version stamped onto
  frontmatter usually raises `ORPHAN_MARKER`, and deleting that one marker is the
  whole migration. **Lint before deleting**: with no blank line between the marker
  and the content below it, blank-line segmentation reads one run and binds the
  marker to that content, so it is live rather than orphaned and deleting it drops
  a working id.

## The conformance corpus (the actual deliverable)

The corpus under [`conformance/`](conformance) is shared with the JavaScript
reference. **420 core vectors** across two tiers, plus a 32-vector optional
profile this package advertises, so its own runner reports **452**. The `check`
category supplies 14 commit-shaped cases with paths, statuses, before/after text,
expected baseline pairings, findings, move/deletion/tracking-departure notes, and
scope behavior.

- **`spec/`** , hand-authored from the spec prose, asserting what the *words*
  require. These are authority; a `spec/` vector the reference fails is a
  reference bug, not a corpus error.
- **`gen/`** , emitted from the reference for breadth/regression.
- **`rows/`** , the optional `rows` profile (SPEC.md §5.6 table-row identity).
  §16 keeps child segmentation optional, so a conforming runner MAY decline this
  profile; the JavaScript and Rust references do, and run the 420 core vectors
  alone. This package implements §5.6, so it advertises `rows` and runs all 452.
  A runner that meets a profile it has never heard of fails rather than skipping
  it, which is what stops a new category going missing quietly.

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
