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
    pairings: list[tuple[str, str | None]] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(L.has_errors(f) for _, f in self.reports)


@dataclass(frozen=True)
class CommitEntry:
    """One commit-shaped input to :func:`check_entries`.

    ``before`` is the HEAD text at ``src`` for rename/copy/delete entries and at
    ``dst`` otherwise. ``after`` is the index or worktree text at ``dst`` for
    non-deletions. Keeping this shape free of git makes baseline pairing part of
    the shared cross-language conformance surface rather than an integration-test
    detail in three unrelated CLIs.
    """

    status: str
    src: str
    dst: str
    before: str | None
    after: str | None


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
    raw = _git(
        ["diff", "HEAD", "--name-status", "-z", "--find-renames"], repo, allow_fail=True
    )
    if raw is None:
        # An unborn repository has no HEAD. Every tracked path is necessarily an
        # index addition, and the caller still reads its current worktree text.
        raw = _git(
            ["diff", "--cached", "--name-status", "-z", "--find-renames"],
            repo,
            allow_fail=True,
        )
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
    child_blocks: bool = False,
    repo: str | None = None,
) -> StagedCheck:
    """Lint the staged commit against its per-document baseline.

    ``scope`` (repo-relative paths, e.g. what pre-commit or lint-staged appends)
    narrows the *report* only. The commit is always read whole, because a renamed
    document's baseline lives at a deleted path and neither tool passes one.
    """
    return check_entries(
        _materialize_entries(
            staged_entries(repo),
            lambda p: _git(["show", f":{p}"], repo, allow_fail=True) or "",
            repo,
        ),
        scope,
        mode=mode,
        check_collections=check_collections,
        child_blocks=child_blocks,
    )


def check_worktree(
    scope: list[str] | None = None,
    *,
    mode: str = "blank-line",
    check_collections: bool = False,
    child_blocks: bool = False,
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

    return check_entries(
        _materialize_entries(worktree_entries(repo), on_disk, repo),
        scope,
        mode=mode,
        check_collections=check_collections,
        child_blocks=child_blocks,
    )


def _materialize_entries(
    entries: list[tuple[str, str, str]],
    after_text,
    repo: str | None,
) -> list[CommitEntry]:
    """Read git-backed entries once, then hand the pure checker owned text."""

    out: list[CommitEntry] = []
    for status, src, dst in entries:
        tracked = (
            is_markdown(src)
            if status == "D"
            else (
                (is_markdown(src) or is_markdown(dst))
                if status == "R"
                else is_markdown(dst)
            )
        )
        if not tracked:
            continue
        before_path = src if status in ("R", "C", "D") else dst
        before = _git(["show", f"HEAD:{before_path}"], repo, allow_fail=True)
        after = None if status == "D" else after_text(dst)
        out.append(CommitEntry(status, src, dst, before, after))
    return out


def check_entries(
    entries: list[CommitEntry],
    scope: list[str] | None = None,
    *,
    mode: str = "blank-line",
    check_collections: bool = False,
    child_blocks: bool = False,
) -> StagedCheck:
    """Check commit-shaped entries without reading git or the filesystem.

    This is the language-neutral core used by both git-aware CLI verbs and by the
    ``check`` conformance category. Pairings are exposed even when a document is
    otherwise clean, so a runner can prove which baseline was selected instead of
    inferring it only from a later finding.
    """

    result = StagedCheck()
    changed = [e for e in entries if e.status != "D" and is_markdown(e.dst)]
    deleted = [
        e
        for e in entries
        if (
            (e.status == "D" and is_markdown(e.src))
            or (e.status == "R" and is_markdown(e.src) and not is_markdown(e.dst))
        )
    ]
    if not changed and not deleted:
        return result

    def ids_of(text: str | None) -> set[str]:
        if text is None:
            return set()
        blocks = L.parse_document(text, mode=mode, child_blocks=child_blocks)
        ids = set(L._id_index(blocks))
        if child_blocks:
            ids.update(L._child_id_index(blocks))
        return ids

    staged_text = {e.dst: e.after or "" for e in changed}
    staged_ids = {p: ids_of(t) for p, t in staged_text.items()}
    # An id present anywhere in the staged tree has not been lost, wherever it
    # ended up.
    committed_ids: set[str] = set()
    for s in staged_ids.values():
        committed_ids |= s

    deleted_ids = {e.src: ids_of(e.before) for e in deleted}
    deleted_text = {e.src: e.before for e in deleted}
    claimed: set[str] = set()

    def baseline_for(entry: CommitEntry) -> tuple[str | None, str | None]:
        if entry.status == "R" and is_markdown(entry.src) and entry.before is not None:
            return entry.before, entry.src
        if entry.status not in ("C", "R") and entry.before is not None:
            return entry.before, entry.dst
        mine = staged_ids.get(entry.dst, set())
        best, best_n = None, 0
        for cand, cand_ids in deleted_ids.items():
            if cand in claimed:
                continue
            n = len(mine & cand_ids)
            if n > best_n:
                best, best_n = cand, n
        if best is not None:
            claimed.add(best)
            return deleted_text[best], best
        return None, None

    want = set(scope or ())
    for entry in changed:
        staged = staged_text[entry.dst]
        baseline, origin = baseline_for(entry)
        result.pairings.append((entry.dst, origin))
        _, findings = L.lint_document(staged, mode=mode, child_blocks=child_blocks)
        findings = list(findings)
        if baseline is not None:
            findings += L.lint_diff(
                baseline,
                staged,
                mode=mode,
                check_collections=check_collections,
                child_blocks=child_blocks,
            )

        kept = []
        for fd in findings:
            if fd.code in ("DROPPED_ID", "CHILD_DROPPED") and fd.id in committed_ids:
                elsewhere = sorted(
                    p for p, s in staged_ids.items() if p != entry.dst and fd.id in s
                )
                result.notes.append(
                    f"{fd.id}: moved out of {origin or entry.dst} into "
                    f"{', '.join(elsewhere)} (still in this commit, not blocking)"
                )
                continue
            kept.append(fd)

        if want and entry.dst not in want:
            continue
        if kept:
            label = (
                entry.dst
                if origin in (None, entry.dst)
                else f"{entry.dst} (baseline {origin})"
            )
            result.reports.append((label, kept))

    for entry in deleted:
        path = entry.src
        if path in claimed:
            continue
        gone = sorted(deleted_ids.get(path, set()) - committed_ids)
        if gone:
            shown = ", ".join(gone[:6]) + (", ..." if len(gone) > 6 else "")
            if entry.status == "R":
                result.notes.append(
                    f"{path}: renamed to {entry.dst}, leaving Markdown tracking "
                    f"with {len(gone)} stay(s) not carried by another Markdown "
                    f"file ({shown})"
                )
            else:
                result.notes.append(
                    f"{path}: deleted with {len(gone)} stay(s) that no staged file "
                    f"carries ({shown})"
                )

    return result
