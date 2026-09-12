"""The hub-side peer-session index, and the RESOLUTION of it against local slots.

What these pin, in one line each: a resolved row keeps the peer identity for the
whole life of the session, ``INDEXING`` never masquerades as an empty crew, one
unreachable crew does not take the list down with it, and a peer row is shaped and
redacted BEFORE it enters the index rather than on the way out.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from kiro_crew.dashboard import peer_session_index as psi


class _Slot:
    """The three fields resolution reads off a slot, plus a key."""

    def __init__(
        self,
        key: str,
        *,
        instance_id: str = "",
        remote_slot: str = "",
        is_remote: bool = False,
    ) -> None:
        self.key = key
        self.instance_id = instance_id
        self.remote_slot = remote_slot
        self.is_remote = is_remote


class _State:
    def __init__(self, slots: list[_Slot]) -> None:
        self._slots = {s.key: s for s in slots}

    def serialize_slot(self, slot: _Slot) -> dict[str, Any]:
        return {"key": slot.key, "title": f"local {slot.key}"}


def _peer_row(key: str = "chat-9", title: str = "REMOTE row") -> dict[str, Any]:
    return {"key": key, "title": title}


class TestIdentity:
    def test_the_identity_is_the_peer_coordinate_pair(self):
        """One helper, so the shape cannot drift from the frontend's own.

        The frontend builds a peer row's identity as `${peer_id}:${key}`. This
        equality is not cosmetic -- it is what lets an ADOPTED row keep the identity
        the peer row had, so the renderer sees one element changing rather than one
        element replaced by another.
        """
        assert psi.peer_identity("astro", "chat-9") == "astro:chat-9"


@pytest.mark.asyncio
class TestResolution:
    async def test_a_local_session_resolves_under_its_own_key(self):
        index = psi.PeerSessionIndex()
        state = _State([_Slot("s1")])

        rows = index.resolve(state, [])

        assert [(r.identity, r.slot_key, r.peer_id) for r in rows] == [("s1", "s1", None)]

    async def test_an_adopted_session_keeps_the_PEER_identity(self):
        """The decision this module exists to implement.

        The local slot has its own key, and that key is NOT the row identity: the
        identity stays the peer's for the life of the session, so a trace, a DOM
        selector and a log line all remain continuous across the adopt. ``slot_key``
        is what carries the local key for `switchSlot` and the transcript.
        """
        index = psi.PeerSessionIndex()
        state = _State(
            [_Slot("local-42", instance_id="astro", remote_slot="chat-9", is_remote=True)]
        )

        rows = index.resolve(state, [])

        assert len(rows) == 1
        assert rows[0].identity == "astro:chat-9"
        assert rows[0].slot_key == "local-42"
        assert rows[0].peer_id == "astro"

    async def test_a_minted_remote_session_resolves_the_same_way_as_an_adopted_one(self):
        """Both creation paths land in one rule.

        ``create_peer_slot`` MINTS a session on the crew; adopt is HANDED one. Either
        way the hub holds an ``(instance_id, peer_key)`` pair, so the mint path is not
        a special case -- which is the simplification that made this reshape worth
        doing rather than merely correct.
        """
        index = psi.PeerSessionIndex()
        minted = _Slot("local-7", instance_id="astro", remote_slot="minted-1", is_remote=True)
        state = _State([minted])

        rows = index.resolve(state, [])

        assert rows[0].identity == "astro:minted-1"
        assert rows[0].slot_key == "local-7"

    async def test_an_unadopted_peer_row_has_no_local_slot_key(self):
        index = psi.PeerSessionIndex()
        state = _State([])
        entry = index.entry("astro")
        entry.status = psi.OK
        entry.rows = [_peer_row()]

        rows = index.resolve(state, ["astro"])

        assert [(r.identity, r.slot_key) for r in rows] == [("astro:chat-9", None)]

    async def test_a_driven_peer_row_is_never_emitted_twice(self):
        """Belt and braces over ``read_peer_slots``' own filter.

        The slice should not contain a row this hub drives, but a refresh that raced
        the binding could still hold one -- and rendering both is the duplicate this
        whole change exists to remove, so resolution refuses it independently.
        """
        index = psi.PeerSessionIndex()
        state = _State(
            [_Slot("local-42", instance_id="astro", remote_slot="chat-9", is_remote=True)]
        )
        entry = index.entry("astro")
        entry.status = psi.OK
        entry.rows = [_peer_row()]  # stale: still lists the adopted session

        rows = index.resolve(state, ["astro"])

        assert [r.identity for r in rows] == ["astro:chat-9"]
        assert rows[0].slot_key == "local-42", "the LOCAL row must be the survivor"

    async def test_a_crew_that_is_not_OK_contributes_no_rows(self):
        """An indexing or failed crew must not render half a list as if it were whole."""
        index = psi.PeerSessionIndex()
        state = _State([])
        entry = index.entry("astro")
        entry.rows = [_peer_row()]
        entry.status = psi.INDEXING

        assert index.resolve(state, ["astro"]) == []


class TestStatusPayload:
    def test_an_unseen_crew_reports_INDEXING_not_an_empty_list(self):
        """The one distinction a bare row list cannot express.

        "We have not looked yet" and "there is nothing over there" ask different
        things of the user, so collapsing them into zero rows is the single wrong
        answer available here.
        """
        index = psi.PeerSessionIndex()

        payload = index.status_payload(["astro"])

        assert payload == [{"instance_id": "astro", "status": psi.INDEXING, "rows": 0, "age_ms": 0}]

    def test_an_empty_crew_reports_OK_with_zero_rows(self):
        index = psi.PeerSessionIndex()
        entry = index.entry("astro")
        entry.status = psi.OK
        entry.fetched_at = 1.0

        payload = index.status_payload(["astro"])

        assert payload[0]["status"] == psi.OK
        assert payload[0]["rows"] == 0

    def test_a_failed_crew_carries_its_code_and_not_a_peer_body(self):
        index = psi.PeerSessionIndex()
        entry = index.entry("astro")
        entry.status = psi.FAILED
        entry.failure = "peer_slots_refused"

        payload = index.status_payload(["astro"])

        assert payload[0]["failure"] == "peer_slots_refused"


@pytest.mark.asyncio
class TestRefresh:
    async def test_rows_are_shaped_and_redacted_before_they_enter_the_index(self, monkeypatch):
        """The index is a reader that serves a browser, so it owes the redaction.

        ``read_peer_slots`` hands back the peer's rows VERBATIM -- its two callers
        need different fields off them -- so the allowlist reshape and the peer-text
        scrub live in whichever reader serves the client. Storing raw rows here would
        put unredacted peer text in the cache and leave nothing downstream to catch
        it, which is why this is pinned rather than left to review.
        """
        seen: dict[str, Any] = {}

        def _fake_clean(row: object) -> dict[str, object]:
            seen["called"] = True
            return {"key": "chat-9", "title": "SHAPED"}

        async def _fake_read(_state: Any, _instance_id: str) -> Any:
            class _Peer:
                rows = [{"key": "chat-9", "title": "raw ghp_deadbeef", "extra": "dropped"}]

            return _Peer()

        monkeypatch.setattr("kiro_crew.dashboard.handlers_instances._clean_peer_slot", _fake_clean)
        monkeypatch.setattr("kiro_crew.dashboard.handlers_instances.read_peer_slots", _fake_read)

        index = psi.PeerSessionIndex()
        await index.refresh(_State([]), "astro")

        assert seen.get("called") is True
        assert index.entry("astro").rows == [{"key": "chat-9", "title": "SHAPED"}]

    async def test_an_unreachable_crew_becomes_a_FAILED_slice_and_does_not_raise(self, monkeypatch):
        """One dead crew must not take down a list that is mostly local sessions.

        That isolation is what the per-crew browser query provided for free, and it
        is the property a naive server-side merge would give away. So the failure is
        a field on the slice, never an exception out of the refresh.
        """
        from kiro_crew.dashboard.handlers_instances import PeerSlotsUnavailable

        async def _fake_read(_state: Any, _instance_id: str) -> Any:
            raise PeerSlotsUnavailable(
                "peer_slots_refused", "the crew refused", 502, "peer HTTP 500"
            )

        monkeypatch.setattr("kiro_crew.dashboard.handlers_instances.read_peer_slots", _fake_read)

        index = psi.PeerSessionIndex()
        await index.refresh(_State([]), "astro")  # must not raise

        entry = index.entry("astro")
        assert entry.status == psi.FAILED
        assert entry.failure == "peer_slots_refused"
        assert entry.rows == []

    async def test_an_indexing_bug_is_contained_the_same_way(self, monkeypatch):
        async def _boom(_state: Any, _instance_id: str) -> Any:
            raise RuntimeError("bug in the index, not in the peer")

        monkeypatch.setattr("kiro_crew.dashboard.handlers_instances.read_peer_slots", _boom)

        index = psi.PeerSessionIndex()
        await index.refresh(_State([]), "astro")

        assert index.entry("astro").status == psi.FAILED
        assert index.entry("astro").failure == "index_error"

    async def test_concurrent_refreshes_of_one_crew_read_the_peer_once(self, monkeypatch):
        """A burst of pushes must cost one tunnel call, not one per push."""
        calls = 0
        release = asyncio.Event()

        async def _slow_read(_state: Any, _instance_id: str) -> Any:
            nonlocal calls
            calls += 1
            await release.wait()

            class _Peer:
                rows: list[dict[str, Any]] = []

            return _Peer()

        monkeypatch.setattr("kiro_crew.dashboard.handlers_instances.read_peer_slots", _slow_read)

        index = psi.PeerSessionIndex()
        state = _State([])
        first = asyncio.create_task(index.refresh(state, "astro"))
        await asyncio.sleep(0)
        second = asyncio.create_task(index.refresh(state, "astro"))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)

        assert calls == 1
