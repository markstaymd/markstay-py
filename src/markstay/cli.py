"""``markstay`` command-line interface.

Subcommand grammar, matching the npm `markstay` CLI so the write verbs read
naturally and the two ecosystems converge:

    markstay lint    FILE...              well-formedness + intra-doc checks
    markstay lint    --before OLD.md NEW  regeneration diff (SPEC.md §11)
    markstay stamp   FILE... [-w]         mint ids for unmarked blocks (§6)
    markstay restamp FILE... [-w]         refresh drifted hashes (§8)
    markstay repair  FILE... [-w]         mint fresh ids for duplicate ids (§7)

``lint`` exits non-zero when any error-level finding is reported, so it gates a
commit hook or an agent's post-edit step. The write verbs print the result to
stdout by default; ``-w``/``--write`` edits files in place.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import lint as L
from .lint import Finding
from .stamp import DEFAULT_HASH_LENGTH, repair_duplicates, restamp, stamp


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


def _cmd_lint(args, ap) -> int:
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


def _run_write(verb: str, args, ap, op) -> int:
    """Shared driver for the write verbs: run ``op(text) -> (text, note)`` per
    file, then either emit to stdout or edit in place."""
    if len(args.files) > 1 and not args.write:
        ap.error(f"{verb} on multiple files requires -w/--write")
    for f in args.files:
        text, note = op(Path(f).read_text())
        if args.write:
            Path(f).write_text(text)
            sys.stderr.write(f"{f}: {note}\n")
        else:
            sys.stdout.write(text)
            sys.stderr.write(f"{f}: {note}\n")
    return 0


def _cmd_stamp(args, ap) -> int:
    def op(md: str):
        res = stamp(
            md,
            syntax="mdx" if args.mdx else "html",
            hash=not args.no_hash,
            hash_length=args.hash_length if args.hash_length is not None else DEFAULT_HASH_LENGTH,
        )
        return res.text, f"{len(res.minted)} id(s) minted"
    return _run_write("stamp", args, ap, op)


def _cmd_restamp(args, ap) -> int:
    def op(md: str):
        res = restamp(md, hash_length=args.hash_length, add_missing=args.add_missing)
        return res.text, f"{len(res.refreshed)} hash(es) refreshed"
    return _run_write("restamp", args, ap, op)


def _cmd_repair(args, ap) -> int:
    def op(md: str):
        res = repair_duplicates(md)
        return res.text, f"{len(res.renamed)} duplicate id(s) re-minted"
    return _run_write("repair", args, ap, op)


def _positive_int(s: str) -> int:
    n = int(s)
    if n < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return n


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="markstay", description="markstay reference CLI")
    sub = ap.add_subparsers(dest="command", required=True, metavar="<command>")

    p_lint = sub.add_parser("lint", help="well-formedness + intra-doc checks")
    p_lint.add_argument("files", nargs="+", metavar="FILE", help="Markdown file(s) to lint")
    p_lint.add_argument("--before", metavar="OLD.md",
                        help="baseline version; runs a regeneration diff against the "
                             "single FILE given (dropped/duplicated/relocated ids)")
    p_lint.add_argument("--json", action="store_true", help="emit findings as JSON")
    p_lint.add_argument("--commonmark", action="store_true",
                        help="segment over the CommonMark tree (SPEC.md §5.2): loose "
                             "lists and blank-line fences attach as one block. Needs "
                             "the 'commonmark' extra (markdown-it-py)")
    p_lint.set_defaults(func=_cmd_lint)

    p_stamp = sub.add_parser("stamp", help="mint ids for unmarked blocks (§6)")
    p_stamp.add_argument("files", nargs="+", metavar="FILE")
    p_stamp.add_argument("-w", "--write", action="store_true",
                         help="edit files in place (required for >1 file)")
    p_stamp.add_argument("--mdx", action="store_true", help="emit the MDX comment form {/* ... */}")
    p_stamp.add_argument("--no-hash", action="store_true", dest="no_hash",
                         help="do not write a hash attribute")
    p_stamp.add_argument("--hash-length", type=_positive_int, default=None, dest="hash_length",
                         help="hex-prefix length for written hashes (default 12)")
    p_stamp.set_defaults(func=_cmd_stamp)

    p_restamp = sub.add_parser("restamp", help="refresh hashes that drifted (§8)")
    p_restamp.add_argument("files", nargs="+", metavar="FILE")
    p_restamp.add_argument("-w", "--write", action="store_true",
                           help="edit files in place (required for >1 file)")
    p_restamp.add_argument("--add-missing", action="store_true", dest="add_missing",
                           help="add a hash to markers that lack one")
    p_restamp.add_argument("--hash-length", type=_positive_int, default=None, dest="hash_length",
                           help="override the written hash precision (default: preserve each marker's)")
    p_restamp.set_defaults(func=_cmd_restamp)

    p_repair = sub.add_parser("repair", help="mint fresh ids for duplicate ids (§7)")
    p_repair.add_argument("files", nargs="+", metavar="FILE")
    p_repair.add_argument("-w", "--write", action="store_true",
                          help="edit files in place (required for >1 file)")
    p_repair.set_defaults(func=_cmd_repair)

    return ap


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Match the JS CLI: bare `help`/`-h`/`--help` prints usage and exits 0; no
    # command prints usage and exits 2.
    if not argv or argv[0] in ("help", "-h", "--help"):
        build_parser().print_help()
        return 0 if argv else 2
    ap = build_parser()
    args = ap.parse_args(argv)
    return args.func(args, ap)


if __name__ == "__main__":
    sys.exit(main())
