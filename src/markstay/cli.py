"""``markstay`` command-line linter.

    markstay FILE [FILE ...]            # well-formedness + intra-doc checks
    markstay --before OLD.md NEW.md     # regeneration diff
    markstay --json ...                 # machine-readable findings

Exit status is non-zero when any error-level finding is reported, so it can gate
a commit hook or an agent's post-edit step.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import lint as L
from .lint import Finding


def render_text(label: str, findings: list[Finding]) -> str:
    if not findings:
        return f"{label}: clean (no findings)"
    out = [f"{label}:"]
    for f in L.sort_findings(findings):
        where = f"L{f.line}" if f.line else "-"
        out.append(f"  [{f.level:5}] {f.code:16} {where:>5}  {f.message}")
    n_err = sum(1 for f in findings if f.level == "error")
    n_warn = sum(1 for f in findings if f.level == "warn")
    n_info = sum(1 for f in findings if f.level == "info")
    out.append(f"  -> {n_err} error, {n_warn} warn, {n_info} info")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="markstay", description="markstay reference linter")
    ap.add_argument("files", nargs="+", help="Markdown file(s) to lint")
    ap.add_argument("--before", metavar="OLD.md",
                    help="baseline version; runs a regeneration diff against the "
                         "single FILE given (dropped/duplicated/relocated ids)")
    ap.add_argument("--json", action="store_true", help="emit findings as JSON")
    ap.add_argument("--commonmark", action="store_true",
                    help="segment blocks over the CommonMark tree (SPEC.md §5.2, "
                         "v1.1): loose lists and blank-line fences attach as one "
                         "block. Needs the 'commonmark' extra (markdown-it-py); "
                         "the default is the dependency-free blank-line model")
    args = ap.parse_args(argv)
    mode = "commonmark" if args.commonmark else "blank-line"

    results = []  # (label, findings)
    if args.before:
        if len(args.files) != 1:
            ap.error("--before takes exactly one NEW file")
        before_md = Path(args.before).read_text()
        after_md = Path(args.files[0]).read_text()
        results.append((f"{args.before} -> {args.files[0]}",
                        L.lint_diff(before_md, after_md, mode=mode)))
    else:
        for f in args.files:
            _, findings = L.lint_document(Path(f).read_text(), mode=mode)
            results.append((f, findings))

    if args.json:
        payload = {label: [x.to_dict() for x in L.sort_findings(fs)] for label, fs in results}
        print(json.dumps(payload, indent=2))
    else:
        print("\n".join(render_text(label, fs) for label, fs in results))

    return 1 if any(L.has_errors(fs) for _, fs in results) else 0


if __name__ == "__main__":
    sys.exit(main())
