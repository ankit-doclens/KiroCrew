"""Release-channel worktrees: one detached checkout per published lane.

Dev Fleet manages git checkouts, and a git checkout has no release channel —
``platform/update_capability.py`` says so outright: *"Only the wheel command
carries a channel. A git checkout follows its remote."* That is fine for the
install the user runs, and useless for the question this module answers: **what
did stable actually ship, and can I click through it right now?**

WHY A WORKTREE PER LANE, AND NOT A PIN ON THE PRIMARY CHECKOUT. Sync
fast-forwards the primary checkout (``git merge --ff-only``) and refuses to run
unless HEAD is literally :data:`repository.BASE_BRANCH`. A stable tag is
normally BEHIND main, so pinning that checkout to a lane could only work by
detaching its HEAD (which the sync guard rejects, and every ``origin/main``
comparison on the fleet row is then measuring against a ref the user did not
choose) or by resetting ``main`` backwards, which destroys work. So the lane
gets its own detached worktree instead: additive, non-destructive, and it lands
in the fleet as an ordinary row that pods and Make Live already know how to
drive.

WHAT THIS MODULE IS NOT. It never reads or writes ``$KIROCREW_HOME/channel``.
That file says which lane the user's real install FOLLOWS for updates; a pin
here says which git ref a worktree SITS ON. Coupling them would mean
materializing a stable worktree silently changed what the user's live install
downloads next — a blast radius nobody asked for. The only thing borrowed from
the update stack is vocabulary and validation.

Resolution is deliberately split from mutation: everything here is a read, so
the fleet snapshot can resolve every lane on its refresh path without any risk
of moving a worktree. The create/advance mutations live in ``worktree_ops``
beside the other worktree writers, because they take the same ``.git`` admin
lock those do.
"""

from __future__ import annotations

import re
from pathlib import Path

from kiro_crew.apps.builtins.dev_fleet import repository, runtime
from kiro_crew.apps.version import parse_version

#: Basename prefix of a release-channel worktree. The basename becomes the fleet
#: row label (``fleet_state`` uses ``Path(path).name`` verbatim) AND the pod
#: identity (``kirocrew-pod@<name>.service``), so it is spelled in full rather
#: than abbreviated: ``release-channel-stable`` reads as what it is next to a
#: ``kirocrew-wt-<slug>`` feature worktree, and the missing ``kirocrew-wt-``
#: prefix is what visually separates the two groups with no extra chrome.
#:
#: Deliberately NOT ``channel-`` — bare "channel" already means four unrelated
#: things in this codebase (agent channels in ``kiro_crew/channel.py``, messaging
#: channels, notification channels, upload document channels).
WORKTREE_PREFIX = "release-channel-"

#: The lanes Dev Fleet can materialize: the ones whose tip a git tag actually
#: names, and whose order among those tags is a fact rather than a rule this
#: feature would have to invent. Spelled out rather than filtered out of
#: :data:`RELEASE_CHANNELS`, because each exclusion below has its own reason and a
#: filter expression states none of them; ``test_lanes_are_real_release_channels``
#: pins that every name here is still a channel that stack knows.
#:
#: ``nightly`` is out because it is not resolvable from git at all. ``nightly.yml``
#: builds from ``main`` HEAD on a schedule and tags NOTHING, so the newest ref a
#: nightly lane could name is ``<remote>/main`` — which is main *now*, not the
#: commit the last nightly published, and is where the primary checkout already
#: sits after Sync. A row promising "what nightly shipped" that shows neither is
#: worse than no row: it duplicates ``main`` and misleads about which build it is.
#:
#: ``insider`` is out by product decision, not by a missing fact — its ``-insider.N``
#: tags do resolve. Ranking them is what has no answer: ordering ``-insider.N``
#: against ``-rc.N`` needs a precedence between two prerelease spellings that
#: nothing in this repo states, so a prerelease lane's "tip" would rest on a rule
#: this feature invented. One lane whose order is a fact beats two where one is a
#: guess, and the shape here takes another lane back the moment that rule exists.
LANES = ("stable",)

#: A tag naming a stable release: ``v1.2.3`` and nothing after it.
_STABLE_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")


def worktree_name(lane: str) -> str:
    """The worktree basename for *lane*."""
    return f"{WORKTREE_PREFIX}{lane}"


def tag_lane(tag: str) -> str | None:
    """``"stable"`` if *tag* is a release tag, ``None`` if it is not.

    The shape check IS the classification. ``_STABLE_TAG_RE`` admits only a bare
    ``vX.Y.Z``, and every such string is stable by definition -- there is no
    prerelease suffix left for ``release_channel.channel`` to read, so deferring to
    it could only ever return the answer this already knows. A second lane would
    need its own tag pattern here anyway, so the deferral bought no extensibility
    either; it just spent a call to agree with the regex.
    """
    return "stable" if _STABLE_TAG_RE.match(tag) else None


def worktree_path(repo: str, lane: str) -> str:
    """Where *lane*'s worktree lives: a sibling of the primary checkout.

    Matches where the existing fleet already is — every ``kirocrew-wt-<slug>``
    worktree is a sibling of the primary checkout — so the new trees land in the
    directory the operator already associates with this repo instead of a second
    root they have to learn.
    """
    return str(Path(repo).parent / worktree_name(lane))


async def fetch_refs(repo: str, *, timeout: int = 120) -> str | None:
    """Refresh the remote-tracking ref AND the tags every lane resolves against.

    Returns ``None`` on success, else the error to report. Called before every
    resolve that must be current, so a mutation acts on the lane's real tip rather
    than on whatever tags happened to be local. The background refresher also
    fetches ``--tags``, which is what keeps the fleet ROWS honest between
    mutations; this call is what makes a Create or an Advance honest at the moment
    it runs.

    ADDITIVE, and deliberately so: a tag deleted upstream (a retracted release)
    is NOT removed locally, so it stays resolvable as a channel tip until someone
    deletes it by hand. Pruning it is not available at this cost — measured on git
    2.54, the only fetch forms that drop a remotely-deleted tag
    (``--prune --prune-tags`` with no ``--tags``, or an explicit
    ``+refs/tags/*:refs/tags/*`` under ``--prune``) delete every local-only tag
    with it, including ones the operator authored, and a pruned tag ref is not in
    any reflog. Trading an operator's own tags for retraction coverage is the
    worse bargain. Buying it properly means fetching release tags into a private
    namespace and resolving lanes there, which is a larger change than this.
    """
    remote = await repository._upstream_remote()
    rc, _out, err = await runtime._run_cmd(
        [
            "git",
            "-C",
            repo,
            "fetch",
            "--tags",
            remote,
            repository.BASE_BRANCH,
        ],
        timeout=timeout,
    )
    if rc != 0:
        return runtime._redact((err or "").strip())[:200] or f"git fetch {remote} --tags failed"
    return None


async def resolve(lane: str, *, repo: str | None = None) -> dict:
    """Resolve *lane* to the ref its channel most recently published.

    Read-only: never fetches (call :func:`fetch_refs` first when freshness
    matters) and never touches a worktree.

    Returns ``{"ok": True, lane, ref, tag, oid, version}`` or
    ``{"ok": False, "error": ...}``.

    Every lane resolves the same way — to a TAG — because :data:`LANES` is the
    tagged subset. An untagged channel has no ref that names a specific published
    build, so it is excluded there rather than special-cased here.

    Ordering is per-lane; see :func:`_lane_candidates` for which order each lane
    gets and why they differ.
    """
    if lane not in LANES:
        return {
            "ok": False,
            "error": f"unknown release channel {lane!r} (expected one of {LANES})",
        }
    if repo is None:
        repo = repository._repo()

    listed = await list_release_tags(repo)
    if listed is None:
        return {"ok": False, "error": "cannot list tags (git tag failed)"}
    return await _resolve_tagged(lane, repo, listed)


async def list_release_tags(repo: str) -> list[str] | None:
    """Candidate release tags, newest published first. ``None`` if git failed.

    Split out so a caller resolving EVERY lane pays for one ``git tag`` instead
    of one per lane — the fleet snapshot resolves every lane on its refresh path.

    Creation order is what git is asked for because it is the order git can give
    cheaply and correctly for both tag spellings. Re-ordering per lane is
    :func:`_lane_candidates`' job, not this one's.
    """
    listed = await repository._git(repo, "tag", "--list", "v*", "--sort=-creatordate", timeout=20)
    if listed is None:
        return None
    return [ln.strip() for ln in listed.splitlines() if ln.strip()]


def _lane_candidates(lane: str, listed: list[str]) -> list[str]:
    """*lane*'s tags out of *listed*, best-first — the order that lane's tip means.

    Ordered by SEMVER, descending, and creation date is NOT the same order. A
    backport cut after a newer line (``v0.4.1`` tagged after ``v0.5.0``, which is
    ordinary release practice) is the NEWEST tag by date and an OLDER release by
    version, and a re-pushed or re-created tag carries today's date for last
    year's release. Either would pin the row to a release stable users are not on.

    One order for every lane, because every lane here is tagged ``vX.Y.Z`` and
    that order is total. A prerelease feed would need a different one — ranking
    ``-insider.N`` against ``-rc.N`` needs a precedence between two prerelease
    spellings that nothing in this repo states — and no lane here is one.

    Stable under ties, so a repeat call resolves the same tag.
    """
    tags = [t for t in (tag.strip() for tag in listed) if t and tag_lane(t) == lane]
    # `parse_version` is the repo's one version parser; a second hand-rolled key
    # here would be a second place for `v0.10.0` vs `v0.9.0` to disagree. Every
    # tag reaching this line matched ``_STABLE_TAG_RE`` in `tag_lane`, so it is a
    # bare ``vX.Y.Z`` and the parse cannot raise.
    return sorted(tags, key=lambda t: parse_version(t[1:]), reverse=True)


async def _resolve_tagged(lane: str, repo: str, listed: list[str]) -> dict:
    """Pick *lane*'s tip out of an already-listed tag set."""
    for tag in _lane_candidates(lane, listed):
        oid = await repository._git(repo, "rev-parse", f"refs/tags/{tag}^{{commit}}")
        if not oid:
            # A listed tag that will not resolve is a broken local ref, not an
            # empty lane. Keep scanning rather than reporting the lane as
            # unpublished, which would hide a real release behind one bad ref.
            continue
        # No second classification of `version` here. `tag_lane(tag)` already
        # decided the lane from the tag's shape, and `version` is that same
        # `tag[1:]` — so re-asking would be a tautology that can only ever agree.
        # One decision, made once, at the point that picks the tag.
        return {
            "ok": True,
            "lane": lane,
            "ref": f"refs/tags/{tag}",
            "tag": tag,
            "oid": oid,
            "version": tag[1:],
        }
    return {"ok": False, "error": f"no {lane} release tag found in this checkout"}


async def resolve_all(*, repo: str | None = None) -> dict[str, dict]:
    """Resolve every lane, listing tags once. Read-only, never fetches.

    A lane that fails to resolve is present in the mapping with its own
    ``{"ok": False, "error": ...}`` rather than omitted. An absent key and a
    failed key look identical to a caller iterating :data:`LANES`, and the
    difference matters: "this repo has never cut a stable release" and "git could
    not be read" want different words on screen.
    """
    if repo is None:
        repo = repository._repo()
    out: dict[str, dict] = {}
    listed = await list_release_tags(repo)
    for lane in LANES:
        if listed is None:
            out[lane] = {"ok": False, "error": "cannot list tags (git tag failed)"}
            continue
        out[lane] = await _resolve_tagged(lane, repo, listed)
    return out


async def worktree_state(path: str, resolved: dict) -> dict:
    """Where the worktree at *path* sits relative to its resolved lane tip.

    ``at_tip`` / ``behind`` describe distance from the CHANNEL TIP, not from
    ``BASE_BRANCH`` — a release worktree is not trying to track main, so the
    fleet's usual behind-main count would be a large number that means nothing
    on this row.

    ``version`` is the release the tree is ACTUALLY on, which is not the lane's
    resolved version: the moment a newer release ships, the resolved tip moves
    and the tree does not. A row that showed the resolved version would rename
    the operator's checkout to a build it does not contain.
    """
    # Four keys, all read by `fleet_state._release_channels`. The worktree's own
    # HEAD *oid* is still not among them — no surface shows a bare sha — but the
    # release that oid corresponds to is exactly what the row's badge claims to
    # display, so it is resolved here rather than inferred from the lane tip.
    out: dict = {"at_tip": False, "behind": None, "detached": None, "version": None}
    head = await repository._git(path, "rev-parse", "HEAD")
    # THREE states, and the primitive underneath has to carry all three or the
    # caller's three-state handling is decoration. `symbolic-ref --quiet HEAD`
    # exits non-zero for a detached HEAD, which `_git` reports as None -- but it
    # ALSO exits non-zero when the read fails outright (the directory was removed
    # while still registered in `git worktree list`, the repo is unreadable). So
    # `is None` alone published an unreadable checkout as a confirmed detached
    # lane, which is the one thing the caller must never be told: it would adopt
    # a tree of unknown shape and offer Advance on it. `rev-parse HEAD` is the
    # discriminator -- a tree whose HEAD cannot be read is not known to be
    # anything, so `detached` stays None and the caller says so.
    symref = await repository._git(path, "symbolic-ref", "--quiet", "HEAD")
    if symref is not None:
        out["detached"] = False
    elif head:
        out["detached"] = True
    tip = resolved.get("oid")
    if not head or not tip:
        return out
    if head == tip:
        out["at_tip"] = True
        out["behind"] = 0
        # Same commit as the tip, so the same release by definition: no second
        # git call to learn what this already tells us.
        out["version"] = resolved.get("version")
        return out
    count = await repository._git(path, "rev-list", "--count", f"{head}..{tip}", timeout=12)
    if count and count.isdigit():
        out["behind"] = int(count)
    out["version"] = await _release_at_head(path, resolved.get("lane"))
    return out


async def _release_at_head(path: str, lane: str | None) -> str | None:
    """The version of the release tag *path*'s HEAD sits on, if it is on one.

    Filtered to *lane*'s own tags: a commit can carry several tags, and picking
    the first would let an unrelated tag that happens to share the commit rename
    the row. ``None`` is a real answer — a lane worktree is adopted for being
    DETACHED, not for being at a release, so an operator who checked out an
    arbitrary commit in it is on no release and the row must not invent one.
    """
    listed = await repository._git(path, "tag", "--points-at", "HEAD", timeout=12)
    if not listed:
        return None
    for tag in (t.strip() for t in listed.splitlines()):
        if tag and tag_lane(tag) == lane:
            return tag[1:]
    return None


#: What OTHER Dev Fleet components read through the ``server`` facade, and
#: nothing else. Each name below has a caller outside this module; a name with
#: none is reachable as an attribute anyway (tests import the module directly),
#: so exporting it would only widen the facade's surface without widening its
#: use. ``tag_lane`` and ``list_release_tags`` are internal for that reason.
__all__ = [
    "LANES",
    "fetch_refs",
    "resolve",
    "resolve_all",
    "worktree_name",
    "worktree_path",
    "worktree_state",
]
