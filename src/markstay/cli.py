"""``markstay`` command-line interface.

Subcommand grammar, matching the npm `markstay` CLI so the write verbs read
naturally and the two ecosystems converge:

    markstay preserve                     print the §11 instruction for an editing
                                          agent (the lever; see below)
    markstay preserve --wrap DOC.md       that instruction wrapped around a document,
                                          as a ready editing prompt
    markstay lint    FILE...              well-formedness + intra-doc checks
    markstay lint    --before OLD.md NEW  regeneration diff (SPEC.md §11)
    markstay resolve --before OLD.md NEW  explain attachment or detachment (§9)
    markstay check-staged [FILE...]       the same diff against the staged commit,
                                          for a pre-commit hook (§11)
    markstay stamp   FILE... [-w]         mint ids for unmarked blocks (§6)
    markstay restamp FILE... [-w]         refresh drifted hashes (§8)
    markstay repair  FILE... [-w]         mint fresh ids for duplicate ids (§7)

``preserve`` comes first because measurement puts it first: an instructed rewrite
keeps ~96-100% of markers against ~5% for a naive one (`eval/FINDINGS.md`), so the
instruction prevents loss and the checks below only catch it.

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
from .resolve import (
    DEFAULT_MARGIN,
    DEFAULT_THRESHOLD,
    build_anchors,
    build_child_anchors,
    resolve,
    resolve_children,
)
from .stamp import DEFAULT_HASH_LENGTH, repair_duplicates, restamp, stamp


RESOLVE_SCHEMA = "markstay.resolve/v1"
RESOLVE_DIAGNOSTICS_SCHEMA = "markstay.resolve-diagnostics/v1"


def render_text(label: str, findings: list[Finding], show_drift: bool = False) -> str:
    """Human render. HASH_DRIFT is the dominant, non-actionable line in normal use
    (it never blocks; it only ever says "you edited things"), so it is hidden by
    default and collapsed to one discoverable line. `show_drift=True` lists it.
    `--json` and the return tuples always carry drift, so the structured channel is
    unaffected. The error/warn/info summary counts the real totals either way."""
    if not findings:
        return f"{label}: clean (no findings)"
    out = [f"{label}:"]
    shown = findings if show_drift else [f for f in findings if f.code != "HASH_DRIFT"]
    n_drift_hidden = len(findings) - len(shown)
    for f in L.sort_findings(shown):
        where = f"L{f.line}" if f.line else "-"
        out.append(f"  [{f.level:5}] {f.code:16} {where:>5}  {f.message}")
    if n_drift_hidden:
        noun = "finding" if n_drift_hidden == 1 else "findings"
        out.append(
            f"  -> {n_drift_hidden} hash-drift {noun} hidden (--show-drift to list)"
        )
    n_err = sum(1 for f in findings if f.level == "error")
    n_warn = sum(1 for f in findings if f.level == "warn")
    n_info = sum(1 for f in findings if f.level == "info")
    out.append(f"  -> {n_err} error, {n_warn} warn, {n_info} info")
    return "\n".join(out)


def _cmd_check_staged(args, ap) -> int:
    from .staged import check_staged, check_worktree

    # check-staged reads the index (a commit hook); check-worktree reads the files on
    # disk (an agent's post-edit step, minutes after the edit rather than at the next
    # commit). Same baseline resolution either way.
    check = check_worktree if getattr(args, "worktree", False) else check_staged
    mode = "commonmark" if args.commonmark else "blank-line"
    try:
        result = check(
            [_repo_relative(f) for f in args.files],
            mode=mode,
            check_collections=args.check_collections,
            child_blocks=args.child_blocks,
        )
    except RuntimeError as exc:
        sys.stderr.write(f"markstay: {exc}\n")
        return 2

    if args.json:
        payload = {
            label: [f.__dict__ for f in L.sort_findings(fs)]
            for label, fs in result.reports
        }
        print(json.dumps({"findings": payload, "notes": result.notes}, indent=2))
    else:
        # A hook speaks only when there is something to act on. HASH_DRIFT says
        # "you edited a stamped block" and NEW_ID says "you added a stay"; neither
        # blocks and neither asks anything of the committer, so a commit carrying
        # only those prints nothing. --show-drift opts back in.
        def actionable(f):
            return f.level == "error" or (f.level == "warn" and f.code != "HASH_DRIFT")

        shown = [
            render_text(label, fs, args.show_drift)
            for label, fs in result.reports
            if (fs if args.show_drift else any(actionable(f) for f in fs))
        ]
        if shown:
            sys.stderr.write("\n".join(shown) + "\n")
        if result.notes:
            sys.stderr.write(
                "markstay: stays that changed document (not blocking):\n"
                + "\n".join(f"  {n}" for n in result.notes)
                + "\n"
            )

    if result.has_errors:
        sys.stderr.write(
            "\nmarkstay: this commit breaks a stay (dropped / duplicated / "
            "relocated / malformed). Fix it, or bypass once with "
            "`git commit --no-verify`.\n"
        )
        return 1
    return 0


def _repo_relative(path: str) -> str:
    """pre-commit and lint-staged both pass paths that may be absolute."""
    import os
    import subprocess

    p = Path(path)
    if not p.is_absolute():
        return str(p).replace(os.sep, "/")
    top = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True
    )
    if top.returncode != 0:
        return str(p).replace(os.sep, "/")
    try:
        return str(p.resolve().relative_to(Path(top.stdout.strip()).resolve())).replace(
            os.sep, "/"
        )
    except ValueError:
        return str(p).replace(os.sep, "/")


def _cmd_lint(args, ap) -> int:
    mode = "commonmark" if args.commonmark else "blank-line"
    results = []  # (label, findings)
    if args.before:
        if len(args.files) != 1:
            ap.error("--before takes exactly one NEW file")
        before_md = Path(args.before).read_text()
        after_md = Path(args.files[0]).read_text()
        results.append(
            (
                f"{args.before} -> {args.files[0]}",
                L.lint_diff(
                    before_md,
                    after_md,
                    mode=mode,
                    check_collections=args.check_collections,
                    child_blocks=args.child_blocks,
                ),
            )
        )
    else:
        for f in args.files:
            _, findings = L.lint_document(
                Path(f).read_text(), mode=mode, child_blocks=args.child_blocks
            )
            results.append((f, findings))

    if args.json:
        payload = {
            label: [x.to_dict() for x in L.sort_findings(fs)] for label, fs in results
        }
        print(json.dumps(payload, indent=2))
    else:
        print(
            "\n".join(
                render_text(label, fs, show_drift=args.show_drift)
                for label, fs in results
            )
        )

    return 1 if any(L.has_errors(fs) for _, fs in results) else 0


def _target_detail(target: int, targets: dict[int, object]) -> dict:
    block = targets[target]
    content = block.content.replace("\n", " ").strip()
    return {
        "index": target,
        "line": block.line,
        "preview": content[:120],
    }


def _candidate_dict(candidate, targets: dict[int, object]) -> dict:
    return {
        "target": _target_detail(candidate.target, targets),
        "score": candidate.score,
        "provenance": candidate.provenance,
        "evidence": [
            {
                "code": evidence.code,
                "label": evidence.label,
                "contribution": evidence.contribution,
            }
            for evidence in candidate.evidence
        ],
    }


def _resolution_dict(result, targets: dict[int, object], threshold: float, margin: float):
    observed_margin = result.score - result.runner_up_score
    payload = {
        "id": result.id,
        "method": result.method,
        "reason": result.reason,
        "committed": result.target is not None,
        "target": (
            _target_detail(result.target, targets) if result.target is not None else None
        ),
        "score": result.score,
        "runner_up_score": result.runner_up_score,
        "threshold": threshold,
        "required_margin": margin,
        "observed_margin": observed_margin,
    }
    if result.method == "detached":
        diagnostics = {
            "schema": RESOLVE_DIAGNOSTICS_SCHEMA,
            "diagnostic": True,
            "candidate_set": "diagnostic, not an attachment recommendation",
            "candidates": [
                _candidate_dict(candidate, targets) for candidate in result.candidates
            ],
        }
        proposed_target = getattr(result, "proposed_target", None)
        if proposed_target is not None:
            diagnostics["contest"] = {
                "proposed_target": _target_detail(proposed_target, targets),
                "contested_with": result.contested_with,
                "provenance": result.proposal_provenance,
                "outcome": "same target proposed by multiple stays; assigned to none",
            }
        payload["diagnostics"] = diagnostics
    blocked_by = getattr(result, "blocked_by", None)
    if blocked_by is not None:
        payload["blocked_by"] = {
            "id": blocked_by.id,
            "method": blocked_by.method,
            "reason": blocked_by.reason,
            "committed": blocked_by.target is not None,
            "score": blocked_by.score,
            "proposed_target_index": blocked_by.proposed_target,
            "contested_with": blocked_by.contested_with,
            "proposal_provenance": blocked_by.proposal_provenance,
        }
    return payload


def _render_resolution(result, targets: dict[int, object], threshold: float, margin: float):
    if result.target is not None:
        target = _target_detail(result.target, targets)
        return [
            f"{result.id}: {result.method} -> block[{target['index']}] L{target['line']} "
            f"(score {result.score:.3f}, attachment committed)"
        ]
    observed = result.score - result.runner_up_score
    lines = [
        f"{result.id}: detached ({result.reason}; score {result.score:.3f}, "
        f"runner-up {result.runner_up_score:.3f}, threshold {threshold:.3f}, "
        f"required margin {margin:.3f}, observed margin {observed:.3f}; "
        "no attachment committed)"
    ]
    blocked_by = getattr(result, "blocked_by", None)
    if blocked_by is not None:
        lines.append(
            f"  blocked by parent {blocked_by.id}: {blocked_by.reason or blocked_by.method}"
        )
        if blocked_by.contested_with:
            lines.append(
                "  parent proposal contested with "
                + ", ".join(blocked_by.contested_with)
                + "; assigned to no stay"
            )
    proposed_target = getattr(result, "proposed_target", None)
    if proposed_target is not None:
        target = _target_detail(proposed_target, targets)
        lines.append(
            f"  contested proposal: block[{target['index']}] L{target['line']} also "
            f"proposed by {', '.join(result.contested_with)}; assigned to no stay"
        )
    return lines


def _cmd_resolve(args, ap) -> int:
    mode = "commonmark" if args.commonmark else "blank-line"
    before_md = Path(args.before).read_text(encoding="utf-8")
    after_md = Path(args.file).read_text(encoding="utf-8")
    if args.child_blocks:
        anchors = build_child_anchors(before_md, mode=mode)
        resolved = resolve_children(
            anchors,
            after_md,
            threshold=args.threshold,
            margin=args.margin,
            mode=mode,
        )
        blocks = [
            block
            for block in L.parse_document(after_md, mode=mode, child_blocks=True)
            if block.index >= 0
        ]
        targets = {
            child.index: child for block in blocks for child in block.children
        }
        granularity = "child"
    else:
        anchors = build_anchors(before_md, mode=mode)
        resolved = resolve(
            anchors,
            after_md,
            threshold=args.threshold,
            margin=args.margin,
            mode=mode,
        )
        blocks = [
            block for block in L.parse_document(after_md, mode=mode) if block.index >= 0
        ]
        targets = {index: block for index, block in enumerate(blocks)}
        granularity = "block"

    ordered = [resolved[anchor.id] for anchor in anchors]
    if args.json:
        print(
            json.dumps(
                {
                    "schema": RESOLVE_SCHEMA,
                    "before": args.before,
                    "after": args.file,
                    "segmenter": mode,
                    "granularity": granularity,
                    "threshold": args.threshold,
                    "required_margin": args.margin,
                    "resolutions": [
                        _resolution_dict(result, targets, args.threshold, args.margin)
                        for result in ordered
                    ],
                },
                indent=2,
            )
        )
        return 0

    lines = []
    for result in ordered:
        lines.extend(_render_resolution(result, targets, args.threshold, args.margin))
        if args.show_candidates:
            for number, candidate in enumerate(result.candidates, 1):
                target = _target_detail(candidate.target, targets)
                labels = ", ".join(evidence.label for evidence in candidate.evidence)
                lines.append(
                    f"  diagnostic candidate {number}: block[{target['index']}] "
                    f"L{target['line']} score {candidate.score:.3f} ({labels})"
                )
    print("\n".join(lines))
    return 0


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
    mode = "commonmark" if args.commonmark else "blank-line"
    if args.child_blocks and args.no_hash:
        ap.error("--child-blocks requires child subhash evidence; remove --no-hash")

    def op(md: str):
        res = stamp(
            md,
            syntax="mdx" if args.mdx else "html",
            hash=not args.no_hash,
            hash_length=(
                args.hash_length
                if args.hash_length is not None
                else DEFAULT_HASH_LENGTH
            ),
            mode=mode,
            child_blocks=args.child_blocks,
        )
        return res.text, f"{len(res.minted)} id(s) minted"

    return _run_write("stamp", args, ap, op)


def _cmd_restamp(args, ap) -> int:
    mode = "commonmark" if args.commonmark else "blank-line"

    def op(md: str):
        res = restamp(
            md,
            hash_length=args.hash_length,
            add_missing=args.add_missing,
            mode=mode,
            child_blocks=args.child_blocks,
        )
        return res.text, f"{len(res.refreshed)} hash(es) refreshed"

    return _run_write("restamp", args, ap, op)


def _cmd_repair(args, ap) -> int:
    mode = "commonmark" if args.commonmark else "blank-line"

    def op(md: str):
        res = repair_duplicates(md, mode=mode, child_blocks=args.child_blocks)
        return res.text, (
            f"{len(res.renamed)} duplicate id(s) re-minted, "
            f"{len(res.cleaned)} injected child hash(es) removed"
        )

    return _run_write("repair", args, ap, op)


def _cmd_preserve(args, ap) -> int:
    from .preserve import INSTRUCTION, preserve_wrap

    if args.task is not None and args.wrap is None:
        ap.error("--task only applies with --wrap")
    if args.wrap is None:
        sys.stdout.write(INSTRUCTION + "\n")
        return 0
    # Read bytes and decode strictly. Invalid input is rejected rather than
    # guessed at: Python's text streams surrogate-escape and Node substitutes
    # U+FFFD, so a lenient read is precisely how three implementations stop
    # emitting the same bytes for the same document.
    where = "stdin" if args.wrap == "-" else args.wrap
    try:
        raw = (
            sys.stdin.buffer.read()
            if args.wrap == "-"
            else Path(args.wrap).read_bytes()
        )
    except OSError as exc:
        sys.stderr.write(f"markstay: {exc}\n")
        return 2
    try:
        doc = raw.decode("utf-8")
    except UnicodeDecodeError:
        sys.stderr.write(f"markstay: {where}: not valid UTF-8\n")
        return 2
    sys.stdout.write(preserve_wrap(doc, task=args.task) + "\n")
    return 0


def _positive_int(s: str) -> int:
    n = int(s)
    if n < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return n


def _unit_interval(s: str) -> float:
    value = float(s)
    if not 0.0 <= value <= 1.0:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return value


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="markstay", description="markstay reference CLI")
    sub = ap.add_subparsers(dest="command", required=True, metavar="<command>")
    commonmark_help = (
        "segment over the CommonMark tree (SPEC.md §5.2): loose lists and "
        "blank-line fences attach as one block. Needs the 'commonmark' extra "
        "(markdown-it-py)"
    )
    child_help = (
        "enable experimental direct list-item identity; CommonMark mode handles "
        "general list items, while the dependency-free mode accepts only flat "
        "tight single-paragraph lists and otherwise emits no child blocks"
    )

    # First, because measurement puts it first: the instruction prevents loss,
    # everything below only catches it.
    p_preserve = sub.add_parser(
        "preserve",
        help="print the §11 instruction that keeps markers alive through an "
        "agent's edit (--wrap DOC.md to build the whole prompt)",
    )
    p_preserve.add_argument(
        "--wrap",
        metavar="DOC.md",
        help="emit the instruction wrapped around this document as a ready "
        "editing prompt ('-' reads stdin)",
    )
    p_preserve.add_argument(
        "--task",
        metavar="TEXT",
        help="edit task to prepend to a --wrap prompt "
        '(e.g. "Rewrite this to be clearer.")',
    )
    p_preserve.set_defaults(func=_cmd_preserve)

    p_lint = sub.add_parser("lint", help="well-formedness + intra-doc checks")
    p_lint.add_argument(
        "files", nargs="+", metavar="FILE", help="Markdown file(s) to lint"
    )
    p_lint.add_argument(
        "--before",
        metavar="OLD.md",
        help="baseline version; runs a regeneration diff against the "
        "single FILE given (dropped/duplicated/relocated ids)",
    )
    p_lint.add_argument("--json", action="store_true", help="emit findings as JSON")
    p_lint.add_argument(
        "--show-drift",
        action="store_true",
        dest="show_drift",
        help="list HASH_DRIFT findings in the text output (hidden by default; "
        "--json always carries them)",
    )
    p_lint.add_argument(
        "--check-collections",
        action="store_true",
        dest="check_collections",
        help="with --before, also block when a kept stay's table or list lost "
        "rows/bullets (COLLECTION_SHRANK); off by default",
    )
    p_lint.add_argument("--commonmark", action="store_true", help=commonmark_help)
    p_lint.add_argument("--child-blocks", action="store_true", help=child_help)
    p_lint.set_defaults(func=_cmd_lint)

    p_resolve = sub.add_parser(
        "resolve",
        help="resolve baseline stays against an edited document and explain detachments",
    )
    p_resolve.add_argument(
        "file", metavar="NEW.md", help="edited Markdown document to resolve against"
    )
    p_resolve.add_argument(
        "--before",
        required=True,
        metavar="OLD.md",
        help="marked baseline that supplies ids and recovery evidence",
    )
    p_resolve.add_argument(
        "--json", action="store_true", help="emit the versioned structured result"
    )
    p_resolve.add_argument(
        "--show-candidates",
        action="store_true",
        dest="show_candidates",
        help="show diagnostic candidates for detached stays in text output; "
        "this does not change --json",
    )
    p_resolve.add_argument(
        "--threshold",
        type=_unit_interval,
        default=DEFAULT_THRESHOLD,
        help=f"quote commit threshold (default {DEFAULT_THRESHOLD})",
    )
    p_resolve.add_argument(
        "--margin",
        type=_unit_interval,
        default=DEFAULT_MARGIN,
        help=f"required lead over the runner-up (default {DEFAULT_MARGIN})",
    )
    p_resolve.add_argument("--commonmark", action="store_true", help=commonmark_help)
    p_resolve.add_argument("--child-blocks", action="store_true", help=child_help)
    p_resolve.set_defaults(func=_cmd_resolve)

    p_staged = sub.add_parser(
        "check-staged",
        help="lint the staged commit against its baseline (for a pre-commit hook)",
    )
    p_staged.add_argument(
        "files",
        nargs="*",
        metavar="FILE",
        help="narrow the report to these paths; the commit is still read whole, "
        "because a renamed document's baseline lives at a deleted path",
    )
    p_staged.add_argument("--json", action="store_true", help="machine-readable output")
    p_staged.add_argument(
        "--show-drift",
        action="store_true",
        dest="show_drift",
        help="list the non-blocking findings a hook hides by default",
    )
    p_staged.add_argument(
        "--check-collections",
        action="store_true",
        dest="check_collections",
        help="also block when a kept stay's table or list lost rows/bullets "
        "(COLLECTION_SHRANK); off by default",
    )
    p_staged.add_argument("--commonmark", action="store_true", help=commonmark_help)
    p_staged.add_argument("--child-blocks", action="store_true", help=child_help)
    p_staged.set_defaults(func=_cmd_check_staged, worktree=False)

    p_wt = sub.add_parser(
        "check-worktree",
        help="the same check against the files on disk, staged or not "
        "(for an agent's post-edit step, not a commit hook)",
    )
    p_wt.add_argument(
        "files", nargs="*", metavar="FILE", help="narrow the report to these paths"
    )
    p_wt.add_argument("--json", action="store_true", help="machine-readable output")
    p_wt.add_argument(
        "--show-drift",
        action="store_true",
        dest="show_drift",
        help="list the non-blocking findings this hides by default",
    )
    p_wt.add_argument(
        "--check-collections",
        action="store_true",
        dest="check_collections",
        help="also report when a kept stay's table or list lost rows/bullets",
    )
    p_wt.add_argument("--commonmark", action="store_true", help=commonmark_help)
    p_wt.add_argument("--child-blocks", action="store_true", help=child_help)
    p_wt.set_defaults(func=_cmd_check_staged, worktree=True)

    p_stamp = sub.add_parser("stamp", help="mint ids for unmarked blocks (§6)")
    p_stamp.add_argument("files", nargs="+", metavar="FILE")
    p_stamp.add_argument(
        "-w",
        "--write",
        action="store_true",
        help="edit files in place (required for >1 file)",
    )
    p_stamp.add_argument("--commonmark", action="store_true", help=commonmark_help)
    p_stamp.add_argument("--child-blocks", action="store_true", help=child_help)
    p_stamp.add_argument(
        "--mdx", action="store_true", help="emit the MDX comment form {/* ... */}"
    )
    p_stamp.add_argument(
        "--no-hash",
        action="store_true",
        dest="no_hash",
        help="do not write a hash attribute",
    )
    p_stamp.add_argument(
        "--hash-length",
        type=_positive_int,
        default=None,
        dest="hash_length",
        help="hex-prefix length for written hashes (default 12)",
    )
    p_stamp.set_defaults(func=_cmd_stamp)

    p_restamp = sub.add_parser("restamp", help="refresh hashes that drifted (§8)")
    p_restamp.add_argument("files", nargs="+", metavar="FILE")
    p_restamp.add_argument(
        "-w",
        "--write",
        action="store_true",
        help="edit files in place (required for >1 file)",
    )
    p_restamp.add_argument("--commonmark", action="store_true", help=commonmark_help)
    p_restamp.add_argument("--child-blocks", action="store_true", help=child_help)
    p_restamp.add_argument(
        "--add-missing",
        action="store_true",
        dest="add_missing",
        help="add a hash to markers that lack one",
    )
    p_restamp.add_argument(
        "--hash-length",
        type=_positive_int,
        default=None,
        dest="hash_length",
        help="override the written hash precision (default: preserve each marker's)",
    )
    p_restamp.set_defaults(func=_cmd_restamp)

    p_repair = sub.add_parser("repair", help="mint fresh ids for duplicate ids (§7)")
    p_repair.add_argument("files", nargs="+", metavar="FILE")
    p_repair.add_argument(
        "-w",
        "--write",
        action="store_true",
        help="edit files in place (required for >1 file)",
    )
    p_repair.add_argument("--commonmark", action="store_true", help=commonmark_help)
    p_repair.add_argument("--child-blocks", action="store_true", help=child_help)
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
    try:
        return args.func(args, ap)
    except ImportError as exc:
        # CommonMark mode (§5.2) is the one optional extra; a missing parser is a
        # setup answer, not a stack trace.
        if getattr(exc, "name", "") == "markdown_it":
            print(
                "error: --commonmark needs the optional CommonMark parser.\n"
                "       install it with:  pip install 'markstay[commonmark]'",
                file=sys.stderr,
            )
            return 2
        raise


if __name__ == "__main__":
    sys.exit(main())
