# markstay , Python reference implementation (v1 core)

[![PyPI](https://img.shields.io/pypi/v/markstay)](https://pypi.org/project/markstay/)
[![Python versions](https://img.shields.io/pypi/pyversions/markstay)](https://pypi.org/project/markstay/)
[![tests](https://img.shields.io/github/actions/workflow/status/markstaymd/markstay-py/test.yml?label=tests)](https://github.com/markstaymd/markstay-py/actions/workflows/test.yml)
[![spec](https://img.shields.io/badge/spec-v1.2-blue)](https://markstay.org)
![License](https://img.shields.io/pypi/l/markstay)

The Python reference implementation of the [markstay spec](https://markstay.org)
(v1.2). markstay is a source-level identity primitive for Markdown blocks: an id
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

# write path: mint ids for unmarked blocks (§6), append the §3.1 trailing marker
res = M.stamp("First paragraph.\n\nSecond paragraph.\n")
res.text     # each block now carries <!-- stay:ID hash=sha256:... -->
res.minted   # [{"id": ..., "line": ...}, ...]

# refresh a hash you edited on purpose (§8); repair duplicate ids (§7, copy mints new)
M.restamp(edited_md)            # -> RestampResult(text, refreshed)
M.repair_duplicates(copied_md)  # -> RepairResult(text, renamed)
```

### Experimental list-item identity

The canonical Python reference includes an opt-in list-item prototype. It is not
part of the spec and has no cross-language parity, so it is not a portable format
promise. Enable it explicitly:

```python
seeded = M.stamp(md, mode="commonmark", child_blocks=True).text
_, findings = M.lint_document(seeded, mode="commonmark", child_blocks=True)
findings = M.lint_diff(before, after, mode="commonmark", child_blocks=True)

anchors = M.build_child_anchors(seeded, mode="commonmark")
resolved = M.resolve_children(anchors, after, mode="commonmark")
```

Child markers are inline and use `subhash=sha256:...`; the same stamping pass mints
the containing list's parent stay. `restamp(..., child_blocks=True)` refreshes
`subhash` and never injects a parent `hash` into a child. If an older
`restamp --add-missing` already added that parent hash,
`repair_duplicates(..., child_blocks=True)` removes the detectable residue.

CommonMark mode handles direct list-item source spans. The dependency-free mode
fails closed outside flat, tight, single-paragraph lists. Measured attachment
safety is 0% false attachment from two independent directions (0/324 deterministic,
95% upper bound 0.92%; 0/256 on real gpt4o rewrites, bound 1.16%) at 95.0% and
92.2% recovery. The feature stays experimental because its benefit is unmeasured,
not because its safety is in doubt.

Public API (the spec'd portion mirrors the JS `index.js` surface; child names are
experimental Python-only): `normalize_body`, `body_hash`,
`Marker`, `find_markers`, `strip_markers`, `rewrite_markers`,
`segment_blank_line`, `segment_commonmark`, `segment_child_items`, `Block`,
`ChildBlock`, `child_body`, `parse_document`, `Finding`,
`lint_document`, `lint_diff`, `sort_findings`, `has_errors`, `mint_id`,
`ID_CHARSET`, `format_marker`, `format_attr_value`, `stamp`, `restamp`,
`repair_duplicates`, `DEFAULT_HASH_LENGTH`, `Selector`, `normalize`,
`body_score`, `context_bonus`, `best_match`, `CONTEXT_CHARS`, `Anchor`,
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
markstay lint    --child-blocks --commonmark ...  # experimental list-item identity
markstay check-staged [FILE...]       # the same diff against the staged commit
markstay check-worktree [FILE...]     # check files on disk before the next commit
markstay stamp   FILE... [-w]         # mint ids for unmarked blocks (§6)
markstay restamp FILE... [-w]         # refresh hashes that drifted (§8)
markstay repair  FILE... [-w]         # mint fresh ids for duplicate ids (§7)
```

`--child-blocks` is also accepted by `check-staged`, `check-worktree`, `stamp`,
`restamp`, and `repair`. It remains off unless requested.

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
    rev: v0.6.0
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
reference. **332 vectors** across two tiers. The `check` category supplies 13
commit-shaped cases with paths, statuses, before/after text, expected baseline
pairings, findings, move/deletion/tracking-departure notes, and scope behavior.

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
