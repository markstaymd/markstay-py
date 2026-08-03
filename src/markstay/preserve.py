"""The §11 preservation instruction, and the prompt wrapper that carries it.

SPEC.md §11 is the AI editing contract. `eval/FINDINGS.md` measured what honouring
it is worth: a naive "clean this up" rewrite keeps ~5% of markers, the same rewrite
carrying this instruction keeps ~96-100%, across five models and three vendors.
That is a ~20x effect and it outweighs model tier, which makes the instruction the
lever and the post-edit check (`lint --before`, `check-staged`) the backstop.

`INSTRUCTION` is the normative phrasing of §11's six obligations (preserve /
keep-attached / mint-new / never-reuse / report-dropped / report-duplicate),
worded for a model rather than for a tool author, and folding in the exact wording
the eval measured rather than a paraphrase of it.

The text is held byte-identical across the Python, JavaScript, and Rust
implementations by the shared conformance corpus (`conformance/spec/preserve.json`,
category ``preserve``): each implementation carries its own copy so an installed
package needs no corpus on disk, and each implementation's own test suite fails if
its copy drifts from the corpus.
"""

from __future__ import annotations

# The ASCII whitespace set SPEC.md pins for §8 hashing and §9 matching. Trimming
# the same set here (rather than each language's native `strip`/`trim`) is what
# lets three implementations compose byte-identical prompts: Python's bare
# `str.strip()` also eats U+001C-U+001F and NBSP, JavaScript's `trim()` eats NBSP
# and U+FEFF, and Rust's eats every Unicode White_Space. Non-ASCII whitespace in a
# document is content and survives the wrap.
_ASCII_WS = " \t\n\r\f\v"

INSTRUCTION = """\
This Markdown document uses markstay markers: HTML comments of the form
`<!-- stay:ID ... -->` (or, in MDX, `{/* stay:ID ... */}`) placed on or just after
the block they identify. Each marker is a stable address that other tools rely on,
so it must survive your edit.

When you edit this document you MUST:

- preserve every existing `stay:` marker exactly as written, including its id and
  any `hash=` / `quote=` attributes; do not remove, reword, renumber, or relocate it;
- keep each marker attached to the same logical block it was on before, even when
  you move, reword, or reformat that block;
- mint a fresh id (any new short token) only for content that is genuinely new;
- never reuse an existing id for different content;
- if you must drop a marker, report it explicitly in your reply, never drop one
  silently;
- never introduce a duplicate id (the same id on two blocks).

Return the edited Markdown with every original marker still present and in place."""

RETURN_ONLY = (
    "Return only the resulting Markdown, with no commentary and no code fence around it."
)


def preserve_wrap(doc: str, task: str | None = None) -> str:
    """Compose a ready-to-send editing prompt: optional task, the preservation
    instruction, the return-format line, then the document behind a `---` rule.

    This is the prompt shape `eval/run_eval.py` measured, so a caller reproduces
    the measured survival rate rather than an approximation of it. A task that is
    empty or only ASCII whitespace is treated as absent.
    """
    parts: list[str] = []
    if task is not None:
        trimmed = task.strip(_ASCII_WS)
        if trimmed:
            parts.append(trimmed)
    parts.append(INSTRUCTION)
    parts.append(RETURN_ONLY)
    parts.append("---\n\n" + doc.strip(_ASCII_WS) + "\n")
    return "\n\n".join(parts)
