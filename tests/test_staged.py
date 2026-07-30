"""Commit-time checking (``markstay check-staged``), the verb a pre-commit hook calls.

The case these exist for: a rewrite big enough to drop stays is a rewrite git will
not call a rename, so it lands as delete + create and a path-keyed baseline finds
nothing to compare against. Similarity is anti-correlated with the failure mode.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from markstay.cli import build_parser, main  # noqa: E402
from markstay.staged import check_staged, is_markdown  # noqa: E402


def git(repo, *args):
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e",
        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e",
        GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null",
    )
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, env=env)


def doc(n: int, prefix: str = "s") -> str:
    """A stamped document with n sections, ids <prefix>0..<prefix>n-1."""
    return "# Doc\n\n" + "\n".join(
        f"## Section {i}\n\nBody text for section {i}, long enough to hash.\n"
        f"<!-- stay:{prefix}{i} -->\n" for i in range(n))


@pytest.fixture()
def repo():
    path = Path(tempfile.mkdtemp(prefix="markstay-staged-"))
    git(path, "init", "-q")
    git(path, "checkout", "-q", "-b", "main")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def write(repo, name, text):
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(text)


def codes(result):
    return [f.code for _, fs in result.reports for f in fs]


def test_catches_a_drop_a_rename_hid(repo):
    write(repo, "STATUS.md", doc(9))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")

    git(repo, "mv", "STATUS.md", "PHASE1.md")
    write(repo, "PHASE1.md",
          "# Doc\n\n## Phase 1 (complete)\n\nAll nine done.\n<!-- stay:s0 -->\n")
    git(repo, "add", "-A")
    # git itself sees no rename here; that is the premise
    assert git(repo, "diff", "--cached", "--name-status").stdout.startswith("A")

    result = check_staged(repo=str(repo))
    assert result.has_errors
    assert codes(result).count("DROPPED_ID") == 8
    assert "baseline STATUS.md" in result.reports[0][0]


def test_pure_rename_keeps_every_stay(repo):
    write(repo, "STATUS.md", doc(5))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    git(repo, "mv", "STATUS.md", "RENAMED.md")
    git(repo, "add", "-A")
    result = check_staged(repo=str(repo))
    assert not result.has_errors
    assert "DROPPED_ID" not in codes(result)


def test_cross_document_move_is_a_note_not_a_block(repo):
    moved = ("## Section 2\n\nBody text for section 2, long enough to hash.\n"
             "<!-- stay:s2 -->\n")
    write(repo, "a.md", doc(3))
    write(repo, "b.md", doc(2, "t"))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")

    write(repo, "a.md", doc(3).replace(moved, ""))
    write(repo, "b.md", doc(2, "t") + "\n" + moved)
    git(repo, "add", "-A")
    result = check_staged(repo=str(repo))
    assert not result.has_errors
    assert "DROPPED_ID" not in codes(result)
    assert any("s2: moved out of a.md into b.md" in n for n in result.notes)


def test_deletion_names_the_lost_stays_without_blocking(repo):
    write(repo, "doomed.md", doc(4, "d"))
    write(repo, "keep.md", doc(1, "k"))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    git(repo, "rm", "-q", "doomed.md")
    result = check_staged(repo=str(repo))
    assert not result.has_errors
    assert any("deleted with 4 stay(s)" in n for n in result.notes)


def test_scope_narrows_the_report_not_the_baseline_search(repo):
    write(repo, "STATUS.md", doc(9))
    write(repo, "other.md", doc(2, "o"))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")

    git(repo, "mv", "STATUS.md", "PHASE1.md")
    write(repo, "PHASE1.md",
          "# Doc\n\n## Phase 1 (complete)\n\nAll nine done.\n<!-- stay:s0 -->\n")
    write(repo, "other.md", doc(2, "o").replace("Body text for section 0", "Reworded"))
    git(repo, "add", "-A")

    scoped = check_staged(["PHASE1.md"], repo=str(repo))
    assert scoped.has_errors
    assert all("other.md" not in label for label, _ in scoped.reports)


def test_empty_staging_area_is_a_noop(repo):
    write(repo, "a.md", doc(1))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    result = check_staged(repo=str(repo))
    assert result.reports == [] and result.notes == []


def test_vendored_markstay_tree_is_ignored():
    # PRESERVE.md shows the marker form twice, so linting the tooling's own output
    # would report a duplicate id that is not the repo's problem.
    assert not is_markdown(".markstay/PRESERVE.md")
    assert is_markdown("docs/notes.md")
    assert is_markdown("notes.markdown")


def test_worktree_check_sees_a_loss_before_it_is_staged(repo):
    """The latency fix. A real loss sat in a working tree for 12 days before landing
    in a commit nobody reviewed line by line; the commit hook could only ever have
    fired at the end of that."""
    from markstay.staged import check_worktree

    write(repo, "a.md", doc(4))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")

    # an agent rewrites the file wholesale, nothing staged
    write(repo, "a.md", "# Doc\n\n## All of it\n\nCollapsed.\n<!-- stay:s0 -->\n")
    assert git(repo, "diff", "--cached", "--name-only").stdout.strip() == ""
    assert check_staged(repo=str(repo)).reports == []      # nothing staged to see

    result = check_worktree(repo=str(repo))
    assert result.has_errors
    assert codes(result).count("DROPPED_ID") == 3


def test_worktree_check_pairs_an_untracked_rename(repo):
    """An agent that rewrites a document under a new name leaves a deleted path and an
    untracked one, with nothing staged at all."""
    from markstay.staged import check_worktree

    write(repo, "STATUS.md", doc(9))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")

    (repo / "STATUS.md").unlink()
    write(repo, "PHASE1.md",
          "# Doc\n\n## Phase 1 (complete)\n\nAll nine done.\n<!-- stay:s0 -->\n")
    result = check_worktree(repo=str(repo))
    assert result.has_errors
    assert codes(result).count("DROPPED_ID") == 8
    assert "baseline STATUS.md" in result.reports[0][0]


def test_pre_commit_hook_definition_matches_the_cli():
    """The hook definition is the adopter-facing contract, and pre-commit only finds
    out it is wrong at hook run time. Parsed with a regex on purpose: this package is
    zero-dependency and must not grow a YAML parser for a test."""
    import re

    text = (Path(__file__).resolve().parents[1] / ".pre-commit-hooks.yaml").read_text()
    ids = re.findall(r"^- id: (\S+)", text, re.M)
    entries = re.findall(r"^  entry: (.+)$", text, re.M)
    assert ids == ["markstay", "markstay-collections"], ids
    assert len(entries) == len(ids)

    for entry in entries:
        argv = entry.split()
        assert argv[0] == "markstay", entry
        # every verb and flag after the console script must really parse
        args = build_parser().parse_args(argv[1:])
        assert getattr(args, "func", None) is not None, entry

    # a hook that passed filenames would be invoked in batches, each re-reading the
    # whole commit; this check needs one invocation, so the flag is load-bearing.
    # Match the YAML keys, not the prose that explains them.
    assert len(re.findall(r"^  pass_filenames: false$", text, re.M)) == len(ids)
    assert len(re.findall(r"^  types: \[markdown\]$", text, re.M)) == len(ids)


def test_cli_exit_codes_and_quiet_channel(repo, capsys, monkeypatch):
    write(repo, "a.md", doc(2))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    monkeypatch.chdir(repo)

    # a stay-preserving in-place edit: non-actionable, so the hook says nothing
    write(repo, "a.md", doc(2).replace("Body text for section 0", "Reworded"))
    git(repo, "add", "-A")
    assert main(["check-staged"]) == 0
    assert capsys.readouterr().err.strip() == ""

    # dropping one blocks, and names the id
    write(repo, "a.md", "# Doc\n\n## Section 0\n\nOnly this remains.\n<!-- stay:s0 -->\n")
    git(repo, "add", "-A")
    assert main(["check-staged"]) == 1
    assert "DROPPED_ID" in capsys.readouterr().err
