"""Create/advance of a release-channel worktree, and how the fleet publishes it.

Two classes of defect are guarded here, both of which produce a tree that looks
healthy:

* **Resolving stale.** ``create`` / ``advance`` must fetch BEFORE resolving. The
  background refresher keeps the fleet ROWS current, but it runs on its own
  schedule — a mutation that resolved first would pin whatever tags happened to be
  local at that moment and report the result as the channel tip.
* **Adopting a tree that is not ours.** ``release-channel-stable`` is a reserved
  name, and a user's own branch checkout under that name must never have its HEAD
  moved. Adoption requires the SHAPE (detached), never the name.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web

from kiro_crew.apps.builtins.dev_fleet import (
    fleet_state,
    http_api,
    release_channel_pin,
    repository,
    runtime,
    worktree_ops,
)


class _Git:
    """Records every argv and answers the reads these ops make."""

    def __init__(
        self,
        *,
        tags=("v0.5.0",),
        head="old-oid",
        head_tag="v0.4.9",
        status="",
        fail=None,
        unmerged="0",
        behind="3",
        changed_paths="",
    ):
        self.calls: list[list[str]] = []
        self._tags = list(tags)
        self._head = head
        # What `git tag --points-at HEAD` answers: the release the worktree is
        # ACTUALLY on. Deliberately a different release from the lane tip in
        # `tags`, because a fake that answered with the tip could not tell a row
        # showing its own version from one showing the tip's.
        self._head_tag = head_tag
        self._status = status
        self._fail = fail or {}
        # `rev-list --count <tip>..HEAD`: commits this worktree holds that the lane
        # tip does not. "0" is the ordinary case -- a lane pin nobody committed on.
        self._unmerged = unmerged
        # `rev-list --count <head>..<tip>`: how far behind the lane tip the row is.
        self._behind = behind
        # `git diff --name-only <old>..<new>`: which tracked paths the advance
        # crosses. Empty is the ordinary case -- an advance that changes no build
        # input must keep the provisioned tree, which is the feature's whole point.
        self._changed_paths = changed_paths
        self.modes: list[str] = []

    async def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        self.modes.append(kw.get("mode", "standard"))
        for needle, result in self._fail.items():
            if needle in cmd:
                return result
        if "fetch" in cmd:
            return 0, "", ""
        if "tag" in cmd and "--list" in cmd:
            return 0, "\n".join(self._tags) + "\n", ""
        if "tag" in cmd and "--points-at" in cmd:
            return (0, self._head_tag + "\n", "") if self._head_tag else (0, "", "")
        if "symbolic-ref" in cmd:
            return 1, "", "not a symbolic ref"  # detached
        if "status" in cmd:
            return 0, self._status, ""
        if "rev-parse" in cmd:
            if cmd[-1] == "HEAD":
                return 0, self._head + "\n", ""
            return 0, "tip-oid\n", ""
        if "worktree" in cmd and "add" in cmd:
            return 0, "", ""
        if "worktree" in cmd and "move" in cmd:
            return 0, "", ""
        if "diff" in cmd and "--name-only" in cmd:
            return 0, self._changed_paths, ""
        if "checkout" in cmd:
            return 0, "", ""
        if "rev-list" in cmd:
            # Two callers ask opposite questions of the same command, and the
            # RANGE DIRECTION is what tells them apart: `<tip>..HEAD` is "what
            # would be stranded" (the advance guard), `<head>..<tip>` is "how far
            # behind the lane tip" (the fleet row). Answering both with one number
            # is what made this fake agree with a guard that was not being tested.
            if cmd[-1].endswith("..HEAD"):
                return 0, self._unmerged + "\n", ""
            return 0, self._behind + "\n", ""
        return 1, "", f"unexpected argv: {cmd}"

    def argv_with(self, needle: str) -> list[str] | None:
        for c in self.calls:
            if needle in c:
                return c
        return None

    def order(self, *needles: str) -> list[int]:
        """Index of the first call containing each needle."""
        out = []
        for needle in needles:
            out.append(next(i for i, c in enumerate(self.calls) if needle in c))
        return out


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A primary checkout path whose sibling lane dirs genuinely do not exist."""
    # A DIRECTORY name, not prose: it mirrors the real primary checkout's own
    # folder, and `worktree_path` derives every lane dir as that folder's
    # sibling — so respelling it would stop the fixture matching the layout
    # under test.
    checkout = tmp_path / "KiroCrew"  # brand-ok: directory name, not prose
    checkout.mkdir()
    monkeypatch.setattr(repository, "_repo", lambda: str(checkout))
    monkeypatch.setattr(repository, "_UPSTREAM_REMOTE", "origin")
    # Each test owns its own lock, so a refusal in one cannot leak into the next.
    monkeypatch.setattr(worktree_ops, "_WT_LOCKS", {})
    return str(checkout)


# --------------------------------------------------------------------------
# create
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_create_rejects_an_unknown_lane(repo, monkeypatch):
    got = await worktree_ops._release_channel_create("beta")
    assert got == {"ok": False, "error": "unknown release channel 'beta'"}


@pytest.mark.asyncio
async def test_create_refuses_when_the_worktree_already_exists(repo, monkeypatch):
    monkeypatch.setattr(
        repository, "_find_worktree", _found({"path": "/somewhere/release-channel-stable"})
    )
    got = await worktree_ops._release_channel_create("stable")
    assert got["ok"] is False
    assert "Advance" in got["error"]


@pytest.mark.asyncio
async def test_create_refuses_an_occupied_path_by_name(repo, monkeypatch):
    """Refuse and NAME the path rather than letting git talk about it.

    ``git worktree add`` fails on a non-empty path anyway, but its message is
    about a directory the operator may not know is involved.
    """
    Path(release_channel_pin.worktree_path(repo, "stable")).mkdir()
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    got = await worktree_ops._release_channel_create("stable")
    assert got["ok"] is False
    assert "already exists on disk" in got["error"]
    assert "release-channel-stable" in got["error"]


@pytest.mark.asyncio
async def test_create_stages_the_worktree_then_adopts_the_lane_path(repo, monkeypatch):
    """Built at a staging path, then moved into place.

    The add must NOT target the lane path directly. Doing so is what made the
    failure cleanup unsafe: it inferred ownership from an earlier existence check,
    which only holds for a single writer, and the locks here are per-event-loop.
    """
    git = _Git()
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_create("stable")
    assert got["ok"] is True
    assert got["name"] == "release-channel-stable"
    assert got["version"] == "0.5.0"
    assert got["ref"] == "refs/tags/v0.5.0"
    final = release_channel_pin.worktree_path(repo, "stable")

    add = git.argv_with("add")
    assert add is not None
    assert "--detach" in add
    assert add[-1] == "tip-oid"
    staged = add[-2]
    assert staged != final
    assert staged.startswith(final + ".staging.")

    move = git.argv_with("move")
    assert move is not None
    assert move[-2:] == [staged, final]


@pytest.mark.asyncio
async def test_a_failed_create_never_force_removes_the_lane_path(repo, monkeypatch):
    """The destructive cleanup may only ever name a path this call staged.

    A second process sharing the repo can create the lane path between our
    existence guard and our add. If cleanup targeted the lane path, that process's
    populated worktree would be deleted, and untracked files are in no reflog.
    """
    git = _Git(fail={"add": (1, "", "fatal: boom")})
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_create("stable")
    assert got["ok"] is False
    final = release_channel_pin.worktree_path(repo, "stable")
    removes = [c for c in git.calls if "worktree" in c and "remove" in c]
    assert removes, "a failed create must still clean up its own residue"
    for argv in removes:
        assert final not in argv
        assert any(a.startswith(final + ".staging.") for a in argv)


@pytest.mark.asyncio
async def test_create_fetches_before_it_resolves(repo, monkeypatch):
    """Order is the guard against pinning a stale tag as the channel tip."""
    git = _Git()
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", git)
    await worktree_ops._release_channel_create("stable")
    fetch_at, tag_at = git.order("fetch", "--list")
    assert fetch_at < tag_at


@pytest.mark.asyncio
async def test_create_reports_a_failed_fetch_instead_of_resolving_locally(repo, monkeypatch):
    git = _Git(fail={"fetch": (1, "", "fatal: unable to access remote")})
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_create("stable")
    assert got["ok"] is False
    assert "cannot refresh release refs" in got["error"]
    assert git.argv_with("add") is None


@pytest.mark.asyncio
async def test_create_reports_a_failed_worktree_add(repo, monkeypatch):
    git = _Git(fail={"add": (128, "", "fatal: invalid reference")})
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_create("stable")
    assert got["ok"] is False
    assert "git worktree add failed" in got["error"]


@pytest.mark.asyncio
async def test_a_failed_create_does_not_leave_the_lane_uncreatable(repo, monkeypatch):
    """``worktree add`` can fail AFTER registering the worktree.

    The leftover directory then trips create's own path-exists refusal, so every
    retry is rejected and the operator has a lane that can neither be created nor
    advanced. Cleaning up is safe precisely because that refusal already proved
    the path did not exist before this attempt, so anything there is ours.
    """
    git = _Git(fail={"add": (1, "", "fatal: could not create work tree dir")})
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_create("stable")
    assert got["ok"] is False
    removed = git.argv_with("remove")
    assert removed is not None and "--force" in removed
    assert git.argv_with("prune") is not None
    # The cleanup is best-effort and must never replace the real cause: git's own
    # message is what the operator needs, not "cleanup failed".
    assert "could not create work tree dir" in got["error"]


@pytest.mark.asyncio
async def test_create_checks_out_without_credential_helpers(repo, monkeypatch):
    """Create's checkout runs in the same strict tier as Advance's.

    ``worktree add`` MATERIALIZES repo-controlled content, and a checkout runs
    whatever content filter the checked-out tree configures — a vector the git
    env neutralizers do not cover. Running it in the standard tier put the
    gateway's trusted credential helpers within reach of a filter defined by the
    very release tag being checked out, while the sibling operation doing the
    identical thing was already strict.
    """
    git = _Git()
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_create("stable")
    assert got["ok"] is True
    idx = next(i for i, c in enumerate(git.calls) if "add" in c)
    assert git.modes[idx] == "strict"


@pytest.mark.asyncio
async def test_the_two_lane_mutations_are_drained_across_cancellation(repo, monkeypatch):
    """Both destructive git calls hold their frame until git has finished.

    A plain ``await`` unwinds on cancellation while the child is mid-write and
    releases ``_GIT_MUTATION_LOCK`` with the tree half-written: for ``add`` that
    strands the residue the failure cleanup never runs for, and for ``checkout``
    it leaves the lane — and any pod on it — holding files from two commits.

    Observed by correlation rather than by trusting a call count: whatever argv
    ``_run_cmd`` issues while a drain is in flight is what that drain protected,
    so the assertion names the two commands instead of counting wrappers.
    """
    git = _Git(head="old-oid")
    real = runtime._run_uninterruptible
    drained: list[list[str]] = []

    async def recording(coro):
        before = len(git.calls)
        result = await real(coro)
        drained.extend(git.calls[before:])
        return result

    monkeypatch.setattr(runtime, "_run_uninterruptible", recording)
    monkeypatch.setattr(runtime, "_run_cmd", git)

    monkeypatch.setattr(repository, "_find_worktree", _missing())
    assert (await worktree_ops._release_channel_create("stable"))["ok"] is True
    assert any("add" in c for c in drained), drained

    drained.clear()
    monkeypatch.setattr(repository, "_find_worktree", _found({"path": "/wt/rcs"}))
    assert (await worktree_ops._release_channel_advance("stable"))["ok"] is True
    assert any("checkout" in c for c in drained), drained
    # The reads (fetch, rev-parse, status, rev-list) are NOT drained: they write
    # nothing, so holding the lock through a cancellation for them would delay
    # shutdown for no protection.
    assert not any("rev-list" in c for c in drained), drained


# --------------------------------------------------------------------------
# advance
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_advance_refuses_a_worktree_that_is_on_a_branch(repo, monkeypatch):
    """The name guard: adoption requires the shape, never the name.

    Moving the HEAD of somebody's own ``release-channel-stable`` BRANCH checkout
    would silently abandon their work.
    """
    git = _Git()

    async def attached(cmd, **kw):
        if "symbolic-ref" in cmd:
            return 0, "refs/heads/release-channel-stable\n", ""
        return await git(cmd, **kw)

    monkeypatch.setattr(repository, "_find_worktree", _found({"path": "/wt/rcs"}))
    monkeypatch.setattr(runtime, "_run_cmd", attached)
    got = await worktree_ops._release_channel_advance("stable")
    assert got["ok"] is False
    assert "is on a branch, not detached" in got["error"]
    assert git.argv_with("checkout") is None


@pytest.mark.asyncio
async def test_advance_refuses_a_dirty_worktree(repo, monkeypatch):
    git = _Git(status=" M src/kiro_crew/__init__.py\n")
    monkeypatch.setattr(repository, "_find_worktree", _found({"path": "/wt/rcs"}))
    monkeypatch.setattr(runtime, "_run_cmd", git)
    monkeypatch.setattr(
        repository, "_dirt_report", _async_return(({"dirty_tracked": True}, " (1 modified)"))
    )
    got = await worktree_ops._release_channel_advance("stable")
    assert got["ok"] is False
    assert "uncommitted changes" in got["error"]
    assert git.argv_with("checkout") is None


@pytest.mark.asyncio
async def test_advance_at_tip_is_a_success_not_an_error(repo, monkeypatch):
    """Nothing is wrong when the tree is already where it was asked to be.

    Refusing here would render a failure for the state the action was trying to
    reach.
    """
    git = _Git(head="tip-oid")
    monkeypatch.setattr(repository, "_find_worktree", _found({"path": "/wt/rcs"}))
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_advance("stable")
    assert got["ok"] is True
    assert got["moved"] is False
    assert git.argv_with("checkout") is None


@pytest.mark.asyncio
async def test_advance_refuses_to_strand_commits_the_tip_does_not_contain(repo, monkeypatch):
    """A CLEAN tree is not the same as nothing to lose.

    Committing is exactly what makes a worktree clean again, and a commit made on
    a detached HEAD belongs to no branch — so once HEAD moves it is reachable only
    from the reflog, and only until gc. `status --porcelain` cannot see that,
    which is why the dirty check is not the guard here.
    """
    git = _Git(head="old-oid", unmerged="2")
    monkeypatch.setattr(repository, "_find_worktree", _found({"path": "/wt/rcs"}))
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_advance("stable")
    assert got["ok"] is False
    assert got["unmerged_commits"] == 2
    assert "reflog" in got["error"]
    # The whole point: HEAD must not have moved.
    assert git.argv_with("checkout") is None
    # And the range asked is "what does the tip NOT contain", not the reverse.
    ranges = [c[-1] for c in git.calls if "rev-list" in c]
    assert ranges == ["tip-oid..HEAD"]


@pytest.mark.asyncio
async def test_advance_refuses_when_the_unmerged_probe_fails(repo, monkeypatch):
    """An unreadable answer is not permission to proceed.

    If `rev-list` cannot run, whether anything would be stranded is unknown, and
    checking out over an unknown is how the loss happens silently.
    """
    git = _Git(head="old-oid", fail={"rev-list": (128, "", "fatal: bad revision")})
    monkeypatch.setattr(repository, "_find_worktree", _found({"path": "/wt/rcs"}))
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_advance("stable")
    assert got["ok"] is False
    assert "cannot verify" in got["error"]
    assert git.argv_with("checkout") is None


@pytest.mark.asyncio
async def test_advance_detaches_onto_the_tip(repo, monkeypatch):
    git = _Git(head="old-oid")
    monkeypatch.setattr(repository, "_find_worktree", _found({"path": "/wt/rcs"}))
    monkeypatch.setattr(runtime, "_run_cmd", git)
    got = await worktree_ops._release_channel_advance("stable")
    assert got["ok"] is True
    assert got["moved"] is True
    argv = git.argv_with("checkout")
    assert argv is not None
    assert "--detach" in argv and argv[-1] == "tip-oid"


@pytest.mark.asyncio
async def test_neither_result_carries_a_field_no_caller_reads(repo, monkeypatch):
    """The result keys are exactly what the HTTP caller types.

    A field kept "for diagnostics" that no surface shows is a claim about the
    payload's contract that no consumer keeps: `path` was a redacted string
    nothing rendered, and `from_oid` a sha the toast never named. Pinned as a set
    so re-adding one has to come with the reader that justifies it.
    """
    git = _Git(head="old-oid")
    monkeypatch.setattr(runtime, "_run_cmd", git)

    monkeypatch.setattr(repository, "_find_worktree", _missing())
    created = await worktree_ops._release_channel_create("stable")
    assert set(created) == {"ok", "lane", "name", "ref", "version"}

    monkeypatch.setattr(repository, "_find_worktree", _found({"path": "/wt/rcs"}))
    advanced = await worktree_ops._release_channel_advance("stable")
    assert set(advanced) == {"ok", "lane", "name", "moved", "ref", "version", "needs_provision"}


@pytest.mark.asyncio
async def test_advance_refuses_rather_than_overwrite_an_ignored_file(repo, monkeypatch):
    """The checkout must not silently replace an ignored local file.

    The dirty gate reads ``git status --porcelain``, which omits IGNORED paths, so
    an ignored file passes it unseen. Git's default ``--overwrite-ignore`` then
    replaces that file as soon as the target release tracks its path, and nothing
    recovers it: the file was never in the object store, so no reflog or stash
    holds a copy. ``--no-overwrite-ignore`` makes git refuse the checkout instead.
    """
    git = _Git(head="old-oid")
    monkeypatch.setattr(repository, "_find_worktree", _found({"path": "/wt/rcs"}))
    monkeypatch.setattr(runtime, "_run_cmd", git)
    await worktree_ops._release_channel_advance("stable")
    argv = git.argv_with("checkout")
    assert argv is not None
    assert "--no-overwrite-ignore" in argv


@pytest.mark.asyncio
async def test_create_needs_no_overwrite_guard_because_it_makes_the_directory(repo, monkeypatch):
    """Create's materialization cannot overwrite a local file, so it carries no flag.

    The sibling of Advance's checkout is ``worktree add``, and the reason the same
    guard is absent there is not an oversight: ``worktree add`` creates the
    directory, and an occupied path is refused before git runs. There is no
    pre-existing ignored file in a directory that did not exist, so the failure
    mode the flag prevents has no branch here to occur on.
    """
    git = _Git(head="old-oid")
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    monkeypatch.setattr(runtime, "_run_cmd", git)
    await worktree_ops._release_channel_create("stable")
    argv = git.argv_with("worktree")
    assert argv is not None
    assert "add" in argv
    assert "--no-overwrite-ignore" not in argv


@pytest.mark.asyncio
async def test_advance_keeps_the_provisioned_tree_when_no_build_input_changed(repo, monkeypatch):
    """The whole point of Advance is not re-provisioning, so keep the tree.

    An advance across a backend-only release changes nothing `dist`, the venv or
    `node_modules` was built from, so throwing them away would cost exactly what
    Remove + Create costs and buy nothing.
    """
    git = _Git(head="old-oid", changed_paths="src/kiro_crew/apps/builtins/dev_fleet/x.py\n")
    monkeypatch.setattr(repository, "_find_worktree", _found({"path": "/wt/rcs"}))
    monkeypatch.setattr(runtime, "_run_cmd", git)
    removed: list[str] = []
    monkeypatch.setattr(worktree_ops, "_discard_dir", lambda d: removed.append(str(d)))
    got = await worktree_ops._release_channel_advance("stable")
    assert got["ok"] is True
    assert got["needs_provision"] == []
    assert removed == []


@pytest.mark.asyncio
async def test_advance_invalidates_only_the_artifacts_whose_inputs_changed(repo, monkeypatch):
    """An artifact built from the previous release is a claim about absent code.

    Provisioning reports itself by PRESENCE -- `has_dist` is `dist_dir().is_dir()`
    and `build_dist` returns early on it -- so a preserved `dist` makes an advanced
    lane read as provisioned and lets a pod boot on the old build. Dropping the
    stale ones is what makes provisioning rebuild them; the venv survives here
    because no Python metadata moved.
    """
    git = _Git(head="old-oid", changed_paths="website/src/App.tsx\n")
    monkeypatch.setattr(repository, "_find_worktree", _found({"path": "/wt/rcs"}))
    monkeypatch.setattr(runtime, "_run_cmd", git)
    removed: list[str] = []
    monkeypatch.setattr(worktree_ops, "_discard_dir", lambda d: removed.append(str(d)))
    got = await worktree_ops._release_channel_advance("stable")
    assert got["ok"] is True
    assert got["needs_provision"] == ["src/kiro_crew/static/dist"]
    # `needs_provision` names the artifact the way the repo does, with `/`. The
    # removal takes a real filesystem path, so it carries the platform's own
    # separator -- hence the expectation is built with `Path`, not spelled out.
    assert removed == [str(Path("/wt/rcs") / "src/kiro_crew/static/dist")]


@pytest.mark.asyncio
async def test_advance_checks_out_without_credential_helpers(repo, monkeypatch):
    """The checkout runs repo-controlled content, so it uses the strict tier.

    Same boundary rebase uses: a checkout of worktree content must not run with
    the gateway's git credential helpers in its environment.
    """
    git = _Git(head="old-oid")
    monkeypatch.setattr(repository, "_find_worktree", _found({"path": "/wt/rcs"}))
    monkeypatch.setattr(runtime, "_run_cmd", git)
    await worktree_ops._release_channel_advance("stable")
    idx = next(i for i, c in enumerate(git.calls) if "checkout" in c)
    assert git.modes[idx] == "strict"


@pytest.mark.asyncio
async def test_advance_reports_a_missing_worktree(repo, monkeypatch):
    monkeypatch.setattr(repository, "_find_worktree", _missing())
    got = await worktree_ops._release_channel_advance("stable")
    assert got["ok"] is False
    assert "not found" in got["error"]


# --------------------------------------------------------------------------
# fleet payload
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fleet_publishes_a_lane_with_no_worktree_as_a_placeholder(repo, monkeypatch):
    """A lane the operator has not materialized is still listed.

    Without the placeholder there is nowhere on the page the feature is
    discoverable — there is no header control.
    """
    monkeypatch.setattr(runtime, "_run_cmd", _Git())
    rows = await fleet_state._release_channels([])
    by_lane = {r["lane"]: r for r in rows}
    assert set(by_lane) == set(release_channel_pin.LANES)
    assert by_lane["stable"]["worktree"] is None
    assert by_lane["stable"]["version"] == "0.5.0"
    assert by_lane["stable"]["ref"] == "refs/tags/v0.5.0"
    assert by_lane["stable"]["name_taken_by_branch"] is False


@pytest.mark.asyncio
async def test_fleet_adopts_a_detached_worktree_and_counts_behind_the_lane_tip(repo, monkeypatch):
    monkeypatch.setattr(runtime, "_run_cmd", _Git(head="old-oid"))
    rows = await fleet_state._release_channels(
        [{"path": "/wt/release-channel-stable", "is_main": False}]
    )
    stable = next(r for r in rows if r["lane"] == "stable")
    assert stable["worktree"] == "release-channel-stable"
    assert stable["behind"] == 3
    assert stable["at_tip"] is False
    # The row names the release the TREE holds, and the tip separately. Feeding
    # the resolved version into `version` made the badge flip to each new release
    # as it shipped while the checkout stayed on the old one.
    assert stable["version"] == "0.4.9"
    assert stable["tip_version"] == "0.5.0"


@pytest.mark.asyncio
async def test_an_adopted_row_on_no_release_tag_reports_no_version(repo, monkeypatch):
    """``None`` rather than borrowing the tip's version to look complete.

    A lane worktree is adopted for being DETACHED, not for being at a release, so
    an operator who checked out an arbitrary commit in it is on no release — and
    the row has to say so instead of naming a build the tree does not contain.
    """
    monkeypatch.setattr(runtime, "_run_cmd", _Git(head="old-oid", head_tag=""))
    rows = await fleet_state._release_channels(
        [{"path": "/wt/release-channel-stable", "is_main": False}]
    )
    stable = next(r for r in rows if r["lane"] == "stable")
    assert stable["worktree"] == "release-channel-stable"
    assert stable["version"] is None
    assert stable["tip_version"] == "0.5.0"


@pytest.mark.asyncio
async def test_fleet_does_not_adopt_a_branch_checkout_that_shares_the_name(repo, monkeypatch):
    """The reserved name must not confer lane controls on somebody's branch."""

    async def attached(cmd, **kw):
        if "symbolic-ref" in cmd:
            return 0, "refs/heads/release-channel-stable\n", ""
        return await _Git()(cmd, **kw)

    monkeypatch.setattr(runtime, "_run_cmd", attached)
    rows = await fleet_state._release_channels(
        [{"path": "/wt/release-channel-stable", "is_main": False}]
    )
    stable = next(r for r in rows if r["lane"] == "stable")
    assert stable["worktree"] is None
    assert stable["name_taken_by_branch"] is True


@pytest.mark.asyncio
async def test_fleet_does_not_claim_a_branch_when_the_probe_could_not_read_head(repo, monkeypatch):
    """An unreadable HEAD is a THIRD state, not "on a branch".

    Collapsing it into ``name_taken_by_branch`` asserts a git fact about a tree
    nobody read, and because that flag also suppresses the lane's placeholder row,
    the lane would vanish from the page behind a fabricated explanation.
    """

    async def unreadable(cmd, **kw):
        if "symbolic-ref" in cmd:
            return 1, "", "fatal: not a git repository"
        if "rev-parse" in cmd and cmd[-1] == "HEAD":
            return 1, "", "fatal: bad revision"
        return await _Git()(cmd, **kw)

    monkeypatch.setattr(runtime, "_run_cmd", unreadable)
    monkeypatch.setattr(
        release_channel_pin,
        "worktree_state",
        _async_return({"at_tip": False, "behind": None, "detached": None, "version": None}),
    )
    rows = await fleet_state._release_channels(
        [{"path": "/wt/release-channel-stable", "is_main": False}]
    )
    stable = next(r for r in rows if r["lane"] == "stable")
    assert stable["worktree"] is None
    assert stable["name_taken_by_branch"] is False
    assert stable["error"] and "could not be read" in stable["error"]


@pytest.mark.asyncio
async def test_fleet_publishes_the_worktree_basename_for_every_lane(repo, monkeypatch):
    """The frontend labels the not-yet-created row from this field.

    Re-deriving the name in the frontend would put the prefix rule on both sides
    of the boundary, where a change to WORKTREE_PREFIX desyncs the label from the
    directory that actually gets created.
    """
    monkeypatch.setattr(runtime, "_run_cmd", _Git())
    rows = await fleet_state._release_channels([])
    for row in rows:
        assert row["name"] == release_channel_pin.worktree_name(row["lane"])
    assert {r["name"] for r in rows} == {"release-channel-stable"}


@pytest.mark.asyncio
async def test_fleet_reports_an_unresolvable_lane_without_dropping_it(repo, monkeypatch):
    """An absent key and a failed key are indistinguishable to the UI.

    "this repo has never cut a stable release" and "git could not be read" want
    different words on screen, so the lane stays in the list carrying its error.
    """
    monkeypatch.setattr(runtime, "_run_cmd", _Git(tags=["v0.6.0-insider.6"]))
    rows = await fleet_state._release_channels([])
    stable = next(r for r in rows if r["lane"] == "stable")
    assert stable["error"] and "no stable release tag" in stable["error"]
    assert stable["version"] is None
    assert stable["ref"] is None


@pytest.mark.asyncio
async def test_fleet_release_channels_never_raise(repo, monkeypatch):
    """This rides on the cached fleet snapshot; one bad lane must not blank it."""

    async def boom(*a, **kw):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(release_channel_pin, "resolve_all", boom)
    assert await fleet_state._release_channels([]) == []


@pytest.mark.asyncio
async def test_fleet_release_channels_empty_without_a_checkout(monkeypatch):
    monkeypatch.setattr(repository, "_repo", _raise(repository.RepoNotConfigured("no checkout")))
    assert await fleet_state._release_channels([]) == []


# --------------------------------------------------------------------------
# route validation
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_route_rejects_an_unknown_lane_with_a_machine_readable_code():
    """The lane becomes a git ref and a directory name, so it is rejected, not
    sanitized — the same contract ``update_layout.set_release_channel`` keeps."""

    async def never_called(lane):  # pragma: no cover - must not run
        raise AssertionError("action ran on an invalid lane")

    resp = await http_api._lane_action(_FakeRequest({"lane": "../etc"}), never_called)
    assert resp.status == 400
    assert json.loads(resp.text)["code"] == "invalid_release_channel"


@pytest.mark.asyncio
async def test_route_rejects_a_missing_lane():
    async def never_called(lane):  # pragma: no cover - must not run
        raise AssertionError("action ran with no lane")

    resp = await http_api._lane_action(_FakeRequest({}), never_called)
    assert resp.status == 400


@pytest.mark.asyncio
async def test_route_forwards_a_valid_lane():
    async def action(lane):
        return {"ok": True, "lane": lane}

    resp = await http_api._lane_action(_FakeRequest({"lane": "stable"}), action)
    assert resp.status == 200
    assert json.loads(resp.text)["lane"] == "stable"


@pytest.mark.asyncio
async def test_a_lane_route_audits_the_lane_and_nothing_else(monkeypatch):
    """The audit target is per ROUTE, so a body cannot name a target for it.

    A first-match scan over the union of every route's target field (`name` ||
    `names` || `path` || `lane`) let a `/release-channel/*` body carry a stray
    `name` and have the tamper-evident trail record a mutation against THAT —
    while the handler acted on the lane. A record naming a target nothing touched
    is worse than a record with an empty one.

    The body below carries both, with `name` first in the old precedence, so it
    fails against the union scan and passes only when the route reads its own key.
    """
    seen: list[dict] = []

    class _Sel:
        def log_tool_invocation(self, **kw):
            seen.append(kw)

    monkeypatch.setattr(runtime, "_sel", lambda: _Sel())
    monkeypatch.setattr(runtime, "_redact", lambda s: s)

    @http_api._audited("dev_fleet_release_channel_create", target_keys=("lane",))
    async def handler(_request):
        return web.json_response({"ok": True})

    await handler(_FakeRequest({"name": "kirocrew-wt-something-else", "lane": "stable"}))
    assert [r["resources"] for r in seen] == ["stable"]


def test_a_worktree_route_cannot_be_audited_against_a_lane():
    """The default target keys do NOT include `lane`, for the mirror reason.

    A worktree mutation reads `name`/`names`/`path`; admitting `lane` into its
    fallback chain would let a body name a lane as the target of a mutation that
    never touched one.
    """
    import inspect

    default = inspect.signature(http_api._audited).parameters["target_keys"].default
    assert "lane" not in default
    assert default == ("name", "names", "path")


# --------------------------------------------------------------------------
# prune
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_prune_never_offers_a_detached_tree_however_it_was_made(tmp_path, monkeypatch):
    """``empty`` is withheld from a BRANCHLESS tree, not from a matching name.

    The trap is that every ORDINARY prune signal says "delete me": detached (so
    no branch and no PR), clean, and holding no commits of its own because a
    release tag is an ancestor of the base branch. That is the ``empty`` verdict,
    and past 48h ``empty`` is a candidate the preview PRESELECTS.

    Both rows below are that state and neither may be offered. The second is the
    one a basename guard could not reach: a tree the operator detached BY HAND at
    a release tag -- the workflow this feature automates -- carries no lane name,
    so a prefix match left it preselected while claiming pins were safe.
    """
    monkeypatch.setattr(repository, "_own_commits_count", _async_return(0))
    monkeypatch.setattr(repository, "_real_dirty", _async_return(False))
    monkeypatch.setattr(repository, "_git", _async_return("a" * 40))
    monkeypatch.setattr(repository, "_dirty_split", _async_return((None, [])))
    # Age the trees past the 48h preselection threshold: without this the verdict
    # is `fresh` for a second reason and the test would pass without the guard.
    monkeypatch.setattr(worktree_ops, "time", SimpleNamespace(time=lambda: time.time() + 400_000))

    for name in (release_channel_pin.worktree_name("stable"), "kirocrew-wt-hand-detached"):
        path = tmp_path / name
        path.mkdir()
        got = await worktree_ops._prunable(str(path), None)
        assert got["ok"] is False, name
        assert got["code"] == "fresh", name


@pytest.mark.asyncio
async def test_prune_still_offers_a_branch_checkout_holding_the_reserved_name(
    tmp_path, monkeypatch
):
    """The reserved name confers nothing on a tree that is ON A BRANCH.

    A basename guard hid this row from Prune merged entirely: an ordinary feature
    worktree, its PR merged, that happened to be called `release-channel-stable`
    became permanently unprunable. Keying on the shape lets it fall through to the
    ordinary merged logic, which is where it belongs.
    """
    path = tmp_path / release_channel_pin.worktree_name("stable")
    path.mkdir()
    monkeypatch.setattr(repository, "_own_commits_count", _async_return(0))
    monkeypatch.setattr(repository, "_real_dirty", _async_return(False))
    monkeypatch.setattr(repository, "_git", _async_return("a" * 40))
    monkeypatch.setattr(repository, "_dirty_split", _async_return((None, [])))
    monkeypatch.setattr(fleet_state, "_pr_status_cached", _async_return({"state": "MERGED"}))
    monkeypatch.setattr(fleet_state, "_fetch_pr_head_oid", _async_return("a" * 40))
    monkeypatch.setattr(fleet_state, "_head_contained_in_pr", _async_return(True))

    got = await worktree_ops._prunable(str(path), "feat/x")
    assert got["ok"] is True
    assert got["code"] == "merged"


@pytest.mark.asyncio
async def test_prune_candidates_keeps_a_lane_worktree_out_of_the_selection(tmp_path, monkeypatch):
    """End to end: the lane row lands in ``kept``, never in ``candidates``.

    Asserted through ``_prune_candidates`` and not just the verdict because the
    preselection the operator confirms is built from ``candidates`` — a verdict
    that was right but reached the wrong list would still delete the pin.
    """
    lane_dir = tmp_path / release_channel_pin.worktree_name("stable")
    lane_dir.mkdir()
    feature = {"path": "/repos/kirocrew-wt-feature", "branch": "feat-x", "is_main": False}
    lane = {"path": str(lane_dir), "is_main": False}
    monkeypatch.setattr(repository, "_discover_worktrees", _async_return([feature, lane]))
    monkeypatch.setattr(repository, "_own_commits_count", _async_return(0))
    monkeypatch.setattr(repository, "_real_dirty", _async_return(False))
    monkeypatch.setattr(repository, "_git", _async_return("a" * 40))
    monkeypatch.setattr(repository, "_dirty_split", _async_return((None, [])))
    monkeypatch.setattr(worktree_ops, "time", SimpleNamespace(time=lambda: time.time() + 400_000))
    monkeypatch.setattr(fleet_state, "_pr_status_cached", _async_return({"state": "MERGED"}))
    monkeypatch.setattr(fleet_state, "_fetch_pr_head_oid", _async_return("a" * 40))
    monkeypatch.setattr(fleet_state, "_head_contained_in_pr", _async_return(True))

    got = await worktree_ops._prune_candidates()
    names = [row["name"] for row in got["candidates"]]
    assert names == ["kirocrew-wt-feature"]
    kept = {row["name"]: row["code"] for row in got["kept"]}
    assert kept[release_channel_pin.worktree_name("stable")] == "fresh"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
class _FakeRequest:
    def __init__(self, body: dict):
        self._body = body
        self.content_length = 1
        # `_audited` reads the raw stream (cached, so a handler can re-parse it);
        # `_lane_action` uses `.json()`. Both paths are exercised by these tests.
        self.can_read_body = True

    async def read(self):
        return json.dumps(self._body).encode()

    async def json(self):
        return self._body


def _found(entry: dict):
    async def _f(name):
        return entry, None

    return _f


def _missing():
    async def _f(name):
        return None, f"worktree not found: {name}"

    return _f


def _async_return(value):
    async def _f(*a, **kw):
        return value

    return _f


def _raise(exc):
    def _f(*a, **kw):
        raise exc

    return _f
