"""Hub-side index of every connected crew's live sessions, and the RESOLUTION of
that index against this machine's own slots into ONE list.

Why this exists
---------------
The Sessions list has two sources: the slots this machine owns, and the sessions
each connected crew owns. They used to be merged in the BROWSER --
``[...localSlots, ...instanceSessions.rows]`` -- from two channels with independent
freshness: local slots arrive on the authoritative push, peer rows on a per-crew
poll. ``read_peer_slots`` did dedupe, but only within the peer channel, so it could
never resolve against what the browser already held from the other one. Adopt a
session and the peer row sat beside the new local row until the next poll; the row
the user clicked was replaced by a different element rather than becoming the
session they asked for.

Resolution is not a rendering problem, so it does not belong in the renderer. This
module owns it: the hub keeps the index, resolves it against its own slots, and
serves ONE already-merged list. Dedupe happens before visibility by construction --
there is no client-side instant at which an unresolved pair can render.

Identity
--------
A resolved row keeps the PEER identity, ``<instance_id>:<peer_key>``, for the whole
life of the session -- including after it has been adopted and is being driven from
here. That is deliberate, and the reason is debuggability: the row's identity, its
``data-session-row`` selector and everything logged about it stay continuous across
the adopt instead of splitting into a before and an after.

The consequence is the invariant this module and its consumers rest on: for a
remote-bound session, **identity is not the local slot key**. Every resolved row
therefore carries both, and they answer different questions:

``identity``
    Stable, peer-shaped. The React key, the ``layoutId``, ``data-session-row``, the
    hover-hold seat arithmetic, the adopt pending/error maps.
``slot_key``
    The LOCAL slot, or ``None`` for a peer session nobody here has adopted.
    ``switchSlot``, the transcript pane, and every read of local slot state.

Never recover the local key by parsing the identity. Read ``slot_key``.

Both creation paths land here identically, which is what lets one rule cover them:
``create_peer_slot`` MINTS a session on the crew and knows the key it minted, and
adopt is HANDED an existing key. Either way the hub ends up holding an
``(instance_id, peer_key)`` pair, so a remote-bound session is identified by its
peer coordinates no matter who started it. The mint path stops being a special case.

Serving never awaits a tunnel
-----------------------------
The index is refreshed out of band and served from memory. A crew that is slow,
unreachable or mid-handshake must not delay -- or fail -- a list that is mostly this
machine's own sessions, which is exactly what the per-crew browser query used to
buy and what a naive server-side merge would give away. Per-crew failure is
isolated and reported as a FIELD on the payload rather than as an exception.

Cold start is allowed to be slow, by explicit decision. What it may not be is
indistinguishable from an empty crew: a crew still being indexed reports
``INDEXING``, never zero rows. A crew that genuinely has no open sessions reports
``OK`` with an empty list. Collapsing those two is the one answer that misleads,
because "no sessions over there" and "we have not looked yet" call for different
things from the user.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable, Optional

from kiro_crew.metrics.provider import get_recorder

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

#: Metric names. Namespaced under ``kirocrew.`` like every other instrument here.
#: These four are the baseline the maintainer asked for before any tuning: the
#: cold-start cost that was accepted, the refresh cost that pays for it, the
#: staleness actually SHIPPED (the reading that would have caught the two-channel
#: bug), and the adopt breakdown (recorded by the adopt path, named here so the
#: four live in one place).
METRIC_INDEX_COLD_START = "kirocrew.peer_index.cold_start"
METRIC_INDEX_FETCH = "kirocrew.peer_index.fetch"
METRIC_INDEX_AGE_AT_SERVE = "kirocrew.peer_index.age_at_serve"
METRIC_ADOPT_PHASE = "kirocrew.adopt.phase"

#: Per-crew index states. ``INDEXING`` is load-bearing: see the module docstring.
INDEXING = "indexing"
OK = "ok"
FAILED = "failed"


@dataclass
class PeerIndexEntry:
    """One crew's slice of the index."""

    instance_id: str
    status: str = INDEXING
    rows: list[dict[str, Any]] = field(default_factory=list)
    #: Monotonic clock, so the age reported at serve time cannot be distorted by a
    #: wall-clock adjustment mid-session.
    fetched_at: float = 0.0
    #: Present only while ``status == FAILED``. A short reason, already safe to
    #: serve: it is the code from ``PeerSlotsUnavailable``, never a peer body.
    failure: str = ""
    #: Set once the first refresh of this crew SETTLES, success or failure. The
    #: cold-start metric is emitted exactly once per crew per process.
    cold_start_recorded: bool = False

    def age_ms(self, now: float) -> float:
        if not self.fetched_at:
            return 0.0
        return max(0.0, (now - self.fetched_at) * 1000.0)


@dataclass
class ResolvedRow:
    """One row of the merged list, already resolved.

    ``identity`` is what the renderer keys on; ``slot_key`` is what addresses local
    state. For a purely local session the two coincide, and that is the only case
    in which they may be used interchangeably.
    """

    identity: str
    slot_key: Optional[str]
    peer_id: Optional[str]
    #: The row body the client renders. For a local (or adopted) session this is the
    #: local slot's own serialization; for an unadopted peer session it is the
    #: allowlist-reshaped, re-redacted peer row.
    row: dict[str, Any]


def peer_identity(instance_id: str, peer_key: str) -> str:
    """The stable identity of a session that lives on *instance_id*.

    One function so the shape cannot drift between the index, the resolver and the
    renderer. It matches the frontend's ``sessionRowIdentity`` for a peer row by
    construction -- that equality is what lets an adopted row keep the identity the
    peer row had, so the element transforms in place instead of being replaced.
    """
    return f"{instance_id}:{peer_key}"


class PeerSessionIndex:
    """The hub's index of peer sessions, and the resolver over it.

    Deliberately NOT a cache in the "fetch on miss" sense: a read never triggers a
    fetch and never awaits one. Refresh is a separate, scheduled concern, so the
    read path stays synchronous and cannot be made slow by a peer.
    """

    def __init__(self) -> None:
        self._entries: dict[str, PeerIndexEntry] = {}
        #: One in-flight refresh per crew. A second request for a crew already being
        #: refreshed joins the first rather than opening a second tunnel call.
        self._inflight: dict[str, asyncio.Task[None]] = {}
        self._started_at = time.monotonic()

    # ---- reads (synchronous, never await a peer) -------------------------------

    def entry(self, instance_id: str) -> PeerIndexEntry:
        """This crew's slice, creating an ``INDEXING`` placeholder if unseen.

        A crew appears in the index the moment it is known to be connected, not the
        moment its first fetch lands -- otherwise "connected but not yet indexed"
        would be indistinguishable from "not connected", and the list would silently
        omit a crew instead of saying it is still being read.
        """
        got = self._entries.get(instance_id)
        if got is None:
            got = PeerIndexEntry(instance_id=instance_id)
            self._entries[instance_id] = got
        return got

    def status_payload(self, instance_ids: Iterable[str]) -> list[dict[str, Any]]:
        """Per-crew index status, for the client to render honestly.

        Emitted for every connected crew, including ones with no rows, because the
        distinction this carries -- indexing vs. genuinely empty vs. failed -- is
        exactly what a bare row list cannot express.
        """
        now = time.monotonic()
        out: list[dict[str, Any]] = []
        for instance_id in instance_ids:
            got = self.entry(instance_id)
            item: dict[str, Any] = {
                "instance_id": instance_id,
                "status": got.status,
                "rows": len(got.rows),
                "age_ms": round(got.age_ms(now)),
            }
            if got.status == FAILED and got.failure:
                item["failure"] = got.failure
            out.append(item)
        return out

    def record_age_at_serve(self, instance_ids: Iterable[str]) -> None:
        """Observe the staleness actually handed to a client.

        The reading that matters most of the four: every other metric describes what
        the hub DID, this one describes what the user SAW. A rising age here is the
        shape the old client-side merge failed at, so it is worth a number even
        though nothing consumes it yet.
        """
        now = time.monotonic()
        for instance_id in instance_ids:
            got = self._entries.get(instance_id)
            if got is None or got.status != OK:
                continue
            _observe(
                METRIC_INDEX_AGE_AT_SERVE,
                got.age_ms(now),
                {"instance": instance_id},
                "Age of the peer-session index slice at the moment it was served",
            )

    def resolve(self, state: "DashboardState", instance_ids: Iterable[str]) -> list[ResolvedRow]:
        """The merged list: this machine's slots and every indexed peer session.

        Order is local-first, matching what the browser produced before, so this
        change moves WHERE the merge happens without also changing what it yields.
        Sorting stays the renderer's job -- it owns pinning, recency buckets and the
        filter chips, none of which belong here.

        A remote-bound local slot -- minted through ``create_peer_slot`` or adopted --
        is emitted ONCE, under its peer identity. That single rule is what makes an
        adopt transform the clicked row instead of replacing it: the identity the
        peer row had is the identity the local row now has, so the renderer sees the
        same key and the same ``layoutId`` before and after.

        A peer row already driven from here is not emitted a second time, because
        ``read_peer_slots`` has already dropped it from the slice. If that binding
        goes away -- the local slot is closed -- the row returns on the next refresh
        rather than instantly; the index is allowed to lag on the way BACK, since the
        failure mode there is a missing row that reappears, not a duplicate.
        """
        driven: dict[tuple[str, str], Any] = {}
        for slot in state._slots.values():
            if slot.is_remote and slot.instance_id and slot.remote_slot:
                driven[(slot.instance_id, slot.remote_slot)] = slot

        out: list[ResolvedRow] = []
        for slot in state._slots.values():
            bound = slot.is_remote and slot.instance_id and slot.remote_slot
            identity = peer_identity(slot.instance_id, slot.remote_slot) if bound else slot.key
            out.append(
                ResolvedRow(
                    identity=identity,
                    slot_key=slot.key,
                    peer_id=slot.instance_id if bound else None,
                    row=state.serialize_slot(slot),
                )
            )

        for instance_id in instance_ids:
            got = self._entries.get(instance_id)
            if got is None or got.status != OK:
                continue
            for row in got.rows:
                key = row.get("key")
                if not isinstance(key, str) or not key:
                    continue
                if (instance_id, key) in driven:
                    # Belt and braces: the slice should not contain it, and if a
                    # refresh raced the binding it must still not double-render.
                    continue
                out.append(
                    ResolvedRow(
                        identity=peer_identity(instance_id, key),
                        slot_key=None,
                        peer_id=instance_id,
                        row=row,
                    )
                )
        return out

    # ---- refresh (out of band; the only place a tunnel is awaited) --------------

    async def refresh(self, state: "DashboardState", instance_id: str) -> None:
        """Re-read one crew's sessions into the index.

        Never raises: a crew that cannot be read becomes a ``FAILED`` slice with a
        short reason, which the payload carries as a field. Raising here would let
        one unreachable crew fail a list that is mostly local sessions -- the exact
        property the per-crew browser query used to provide for free.

        Concurrent callers for the same crew join the in-flight refresh instead of
        opening a second tunnel call, so a burst of pushes costs one read.
        """
        running = self._inflight.get(instance_id)
        if running is not None and not running.done():
            await asyncio.shield(running)
            return
        task = asyncio.current_task()
        if task is not None:
            self._inflight[instance_id] = task  # type: ignore[assignment]
        try:
            await self._refresh_once(state, instance_id)
        finally:
            if self._inflight.get(instance_id) is task:
                self._inflight.pop(instance_id, None)

    async def _refresh_once(self, state: "DashboardState", instance_id: str) -> None:
        from kiro_crew.dashboard.handlers_instances import (
            PeerSlotsUnavailable,
            _clean_peer_slot,
            read_peer_slots,
        )

        got = self.entry(instance_id)
        started = time.monotonic()
        outcome = "ok"
        try:
            peer = await read_peer_slots(state, instance_id)
            # SHAPED before it is stored, never after. ``read_peer_slots`` returns
            # the peer's OWN rows verbatim -- its two readers need different fields
            # off them -- so the allowlist reshape and the peer-text redaction live
            # in the reader that serves a browser. This index IS such a reader: its
            # rows go out to the client, so storing them raw would make the index a
            # redaction hole that no later step closes. Shaping here also means the
            # cached bytes are already safe, so a serve never has to remember to.
            got.rows = [
                cleaned
                for cleaned in (_clean_peer_slot(row) for row in peer.rows)
                if cleaned is not None
            ]
            got.status = OK
            got.failure = ""
        except PeerSlotsUnavailable as exc:
            outcome = "failed"
            got.status = FAILED
            # The CODE, not the peer's body: this string is served to the client.
            got.failure = getattr(exc, "code", "") or "peer_slots_unavailable"
            got.rows = []
        except Exception:  # noqa: BLE001 - an indexing bug must not break the list
            outcome = "error"
            logger.warning("peer index refresh for %s raised", instance_id, exc_info=True)
            got.status = FAILED
            got.failure = "index_error"
            got.rows = []
        finally:
            got.fetched_at = time.monotonic()
            elapsed_ms = (got.fetched_at - started) * 1000.0
            _observe(
                METRIC_INDEX_FETCH,
                elapsed_ms,
                {"instance": instance_id, "outcome": outcome},
                "Duration of one peer-session index refresh",
            )
            if not got.cold_start_recorded:
                got.cold_start_recorded = True
                # Measured from process start, not from the fetch: the number the
                # maintainer accepted is how long a user waits for a crew's sessions
                # to APPEAR after launch, which includes everything before the first
                # read was even scheduled.
                _observe(
                    METRIC_INDEX_COLD_START,
                    (got.fetched_at - self._started_at) * 1000.0,
                    {"instance": instance_id, "outcome": outcome},
                    "Time from process start to a crew's first indexed session list",
                )


def _observe(name: str, value_ms: float, attrs: dict[str, Any], description: str) -> None:
    """Record one histogram observation. Never raises.

    Telemetry may not break a list read, which is the same contract
    ``db_metrics.record_query`` keeps for a store call.
    """
    try:
        get_recorder().histogram(
            name,
            value_ms,
            unit="ms",
            attrs={str(k): v for k, v in attrs.items()},
            description=description,
        )
    except Exception:  # noqa: BLE001 - telemetry must never break the caller
        logger.debug("peer index metric %r failed", name, exc_info=True)
