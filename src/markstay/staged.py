"""Commit-time checking: the regeneration diff against what git actually holds.

Every other entry point in this package works on files. This one works on a commit,
because catching a dropped stay means diffing a document against *the same document
before the edit*, and only git knows what that was.

The baseline is resolved by stay id, not by filename. A stay's whole point is that
identity is not positional, and neither is its baseline:

    M  ->  HEAD:<path>
    R  ->  HEAD:<old path>          (git recorded a rename)
    A  ->  the document deleted in the same commit sharing the most stay ids

That last case is the one path-keyed pairing misses, and it is not an edge case.
git's rename detection is content-similarity based, and similarity is
anti-correlated with this failure mode: the more a rewrite destroys, the more stays
it can drop *and* the less git sees a rename. A measured real case scored 2%
similarity, so git recorded delete + create, no baseline was found, and ten dropped
stays were reported as nothing at all. A surviving stay id is the stronger signal.

An id that moved to another document in the same commit is reported as a move
rather than a loss: the stay is still in the commit, so blocking a deliberate
reorganisation would be wrong. Only ids that leave the commit entirely block.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import lint as L
from .lint import Finding

MD_SUFFIXES = (".md", ".markdown")


@dataclass
class StagedCheck:
    """The outcome of checking one staged commit."""

    reports: list[tuple[str, list[Finding]]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(L.has_errors(f) for _, f in self.reports)


def _git(args: list[str], repo: str | None = None, allow_fail: bool = False):
    res = subprocess.run(
        ["git", *(["-C", repo] if repo else []), *args],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        if allow_fail:
            return None
        raise RuntimeError(f"git {' '.join(args)} failed: {res.stderr.strip()}")
    return res.stdout


def is_markdown(path: str) -> bool:
    """Markdown, excluding a vendored ``.markstay/`` tree.

    ``.markstay/PRESERVE.md`` demonstrates the marker form twice, so linting the
    tooling's own output would report a duplicate id that is not the repo's problem.
    """
    if path == ".markstay" or path.startswith(".markstay/"):
        return False
    return path.endswith(MD_SUFFIXES)


def staged_entries(repo: str | None = None) -> list[tuple[str, str, str]]:
    """Staged changes as ``(status, src, dst)``.

    ``--name-status -z`` emits NUL-separated fields; a rename or copy carries two
    paths, everything else one. ``--find-renames`` is explicit so the listing does
    not change shape under a repo that sets ``diff.renames=false``.
    """
    raw = _git(["diff", "--cached", "--name-status", "-z", "--find-renames"], repo)
    fields = (raw or "").split("\0")
    out: list[tuple[str, str, str]] = []
    i = 0
    while i < len(fields) and fields[i]:
        status = fields[i][0]
        if status in ("R", "C") and i + 2 < len(fields):
            out.append((status, fields[i + 1], fields[i + 2]))
            i += 3
        elif i + 1 < len(fields):
            out.append((status, fields[i + 1], fields[i + 1]))
            i += 2
        else:
            break
    return out


def worktree_entries(repo: str | None = None) -> list[tuple[str, str, str]]:
    """Changes between HEAD and the working tree, staged or not, plus untracked files.

    The commit-time check runs once per commit, which on a real repo can be a week
    after an agent dropped a stay, in a diff too large to review. This is the same
    question asked of the working tree, so the answer arrives in minutes. Untracked
    files count: an agent that rewrites a document under a new name leaves a deleted
    path and an untracked one, which is exactly the rename case.
    """
    raw = _git(["diff", "HEAD", "--name-status", "-z", "--find-renames"], repo,
               allow_fail=True)
    fields = (raw or "").split("\0")
    out: list[tuple[str, str, str]] = []
    i = 0
    while i < len(fields) and fields[i]:
        status = fields[i][0]
        if status in ("R", "C") and i + 2 < len(fields):
            out.append((status, fields[i + 1], fields[i + 2]))
            i += 3
        elif i + 1 < len(fields):
            out.append((status, fields[i + 1], fields[i + 1]))
            i += 2
        else:
            break
    untracked = _git(["ls-files", "--others", "--exclude-standard", "-z"], repo) or ""
    for path in untracked.split("\0"):
        if path:
            out.append(("A", path, path))
    return out


def check_staged(
    scope: list[str] | None = None,
    *,
    mode: str = "blank-line",
    check_collections: bool = False,
    repo: str | None = None,
) -> StagedCheck:
    """Lint the staged commit against its per-document baseline.

    ``scope`` (repo-relative paths, e.g. what pre-commit or lint-staged appends)
    narrows the *report* only. The commit is always read whole, because a renamed
    document's baseline lives at a deleted path and neither tool passes one.
    """
    return _check(
        staged_entries(repo),
        lambda p: _git(["show", f":{p}"], repo, allow_fail=True) or "",
        scope, mode=mode, check_collections=check_collections, repo=repo,
    )


def check_worktree(
    scope: list[str] | None = None,
    *,
    mode: str = "blank-line",
    check_collections: bool = False,
    repo: str | None = None,
) -> StagedCheck:
    """The same check against the working tree, staged or not.

    Detection latency, not blindness, is the commit hook's real weakness: it fires
    once per commit, and a loss can sit in the working tree for days before landing
    in a diff nobody reads line by line. Run this after an editing pass instead.
    """
    root = Path((_git(["rev-parse", "--show-toplevel"], repo) or ".").strip())

    def on_disk(path: str) -> str:
        try:
            return (root / path).read_text()
        except (OSError, UnicodeDecodeError):
            return ""

    return _check(
        worktree_entries(repo), on_disk, scope,
        mode=mode, check_collections=check_collections, repo=repo,
    )


def _check(
    entries: list[tuple[str, str, str]],
    after_text,
    scope: list[str] | None,
    *,
    mode: str,
    check_collections: bool,
    repo: str | None,
) -> StagedCheck:
    result = StagedCheck()
    changed = [(st, src, dst) for st, src, dst in entries
               if st != "D" and is_markdown(dst)]
    deleted = [src for st, src, _ in entries if st == "D" and is_markdown(src)]
    if not changed and not deleted:
        return result

    def head_text(path: str) -> str | None:
        return _git(["show", f"HEAD:{path}"], repo, allow_fail=True)

    def ids_of(text: str | None) -> set[str]:
        if text is None:
            return set()
        return set(L._id_index(L.parse_document(text, mode=mode)))

    staged_text = {dst: after_text(dst) for _, _, dst in changed}
    staged_ids = {p: ids_of(t) for p, t in staged_text.items()}
    # An id present anywhere in the staged tree has not been lost, wherever it
    # ended up.
    committed_ids: set[str] = set()
    for s in staged_ids.values():
        committed_ids |= s

    deleted_ids = {p: ids_of(head_text(p)) for p in deleted}
    claimed: set[str] = set()

    def baseline_for(status: str, src: str, dst: str) -> tuple[str | None, str | None]:
        if status == "R":
            text = head_text(src)
            if text is not None:
                return text, src
        elif status != "C":
            text = head_text(dst)
            if text is not None:
                return text, dst
        mine = staged_ids.get(dst, set())
        best, best_n = None, 0
        for cand, cand_ids in deleted_ids.items():
            if cand in claimed:
                continue
            n = len(mine & cand_ids)
            if n > best_n:
                best, best_n = cand, n
        if best is not None:
            claimed.add(best)
            return head_text(best), best
        return None, None

    want = set(scope or ())
    for status, src, dst in changed:
        staged = staged_text[dst]
        baseline, origin = baseline_for(status, src, dst)
        _, findings = L.lint_document(staged, mode=mode)
        findings = list(findings)
        if baseline is not None:
            findings += L.lint_diff(
                baseline, staged, mode=mode, check_collections=check_collections
            )

        kept = []
        for fd in findings:
            if fd.code == "DROPPED_ID" and fd.id in committed_ids:
                elsewhere = sorted(p for p, s in staged_ids.items()
                                   if p != dst and fd.id in s)
                result.notes.append(
                    f"{fd.id}: moved out of {origin or dst} into "
                    f"{', '.join(elsewhere)} (still in this commit, not blocking)")
                continue
            kept.append(fd)

        if want and dst not in want:
            continue
        if kept:
            label = dst if origin in (None, dst) else f"{dst} (baseline {origin})"
            result.reports.append((label, kept))

    for path in deleted:
        if path in claimed:
            continue
        gone = sorted(deleted_ids.get(path, set()) - committed_ids)
        if gone:
            shown = ", ".join(gone[:6]) + (", ..." if len(gone) > 6 else "")
            result.notes.append(
                f"{path}: deleted with {len(gone)} stay(s) that no staged file "
                f"carries ({shown})")

    return result
