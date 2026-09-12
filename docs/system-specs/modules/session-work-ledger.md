# Session Work Ledger

Owners: `kiro_crew.session_ledger`, `kiro_crew.mcp_tools.ledger`, and the dashboard ledger routes. Cleanup for both on-disk ledgers — this one and the conductor work ledger in `kiro_crew.work_ledger` — is owned by `kiro_crew.ledger_sweep`; see [Cleanup](#cleanup).

## 1. Purpose

`kiro_crew.session_ledger` provides durable, per-session work state for long-running work. The state record holds a goal, phase, resumable next step, rejected approaches, artifact pointers, and a bounded event history. This separates resumable state from transcript context; `mcp_tools.ledger.schemas()` describes the record as authoritative over prior-cycle memory.

## 2. Storage, identity, and lifecycle

Each recorded ledger lives below `<data_home>/ledger/` in a directory containing:

```
slot_key   # original ledger key breadcrumb
state.json # complete state record
.lock      # dedicated cross-process lock inode
```

`session_ledger.record()` creates the directory and writes `state.json` atomically. `session_ledger._locked()` keeps the lock file separate from the replaced state file, so replacing state cannot let concurrent writers lock different inodes.

`session_ledger.ledger_key()` removes only dashboard namespace and prefix spellings before storage. `session_ledger._store_name()` combines a readable fold with a digest of that exact key; `test_distinct_channel_keys_never_share_a_ledger` guards against the lossy-fold collision that would otherwise let one channel session overwrite another. `session_ledger.ledger_dir()` rejects hostile raw keys and requires the resolved directory to remain below the ledger root, preventing traversal through a folded name.

Closing a dashboard tab preserves the ledger. Permanent history deletion also
preserves it. A transcript can be created or restored by another process after
any in-process owner check, and no request-local fence can make ledger deletion
atomic with that claim. A stale ledger is reversible; deleting a successor's
resumable state is not. Standalone `purge()` / `purge_matching()` remain explicit
synchronous maintenance primitives, not part of the history-delete request path.
Their one caller is the operator sweep in [Cleanup](#cleanup).

The same history-delete funnel separately releases cron ownership. The single
delete performs a strict cron-owner scan before unlink and another after it; bulk
clear batches both scans. `_delete_history_session()` binds the exact owner keys
from that scan and a readable `linked_session_key` into the immutable
`_HistoryDeleteClaim` while holding the canonical-plus-legacy transcript lock
set. `_remove_slot_for_history_key()` first revalidates the captured slot,
transcript, task and session generation, then cancels the old turn and conditionally
destroys only that manager generation. After those awaits it rechecks both live
slots and SessionManager's live/reserved keys, and hands only the proven retired
owner keys to `CronService.release_jobs_owned_by()`. Pins, ledgers and autocompact overrides are
preserved; cron release is allowed because its exact ownership was established
before unlink rather than inferred from a lossy filename fold. Known cron-store
failures (`cron_store_unreadable`, `cron_store_busy`, and for an unreadable
transcript `cron_ownership_unknown`) leave the row intact with a 409; bulk clear
reports per-row unreadable claims in `undeletable`. Other post-unlink errors log
the by-id recovery command (`kirocrew cron adopt <id> --release`). See
[learn-cron-dashboard](learn-cron-dashboard.md).

### Cleanup

Nothing in the product reclaims either ledger, so the only cleanup is an explicit operator command: `kirocrew doctor --ledger-sweep`, owned by `kiro_crew.ledger_sweep` and printed by `cli_doctor._ledger_sweep()`. It is shaped as a flag rather than a `doctor` subcommand because `doctor` has no subparsers — `--bundle` is the same kind of short-circuit mode — and it takes `--purge`, `--older-than-days N` (default `ledger_sweep.DEFAULT_OLDER_THAN_DAYS`, 30), `--include-orphans` and `--purge-unreadable`.

**It stays off every automatic path.** It is not a gateway-start hook, not part of the history-delete funnel described above, and deliberately not an MCP tool: the deletion is irreversible, so no model-reachable surface reaches it. `ledger_sweep.scan()` changes nothing at all, and a purge is a second, separate invocation over the report the first one printed.

`scan()` answers "leave it alone" unless the record itself proves otherwise:

| Record | Candidate when | Never a candidate |
|---|---|---|
| session ledger | `phase` in `session_ledger.TERMINAL_PHASES` and age ≥ threshold | any in-flight phase, at any age |
| session ledger | key names no session and `--include-orphans`, and age ≥ threshold | a key with a transcript or a session-map entry |
| work ledger | every item in `work_ledger.TERMINAL_ITEM_STATES` and age ≥ threshold | one open item, at any age |
| work ledger | key names no session and `--include-orphans`, and age ≥ threshold | one item record the sweep cannot parse |
| either | listed `unreadable` when the record cannot be parsed AND age ≥ threshold | a record younger than the threshold, or a conductor with an open item — damage is classified last |

Age is measured from `finished_at` for a session ledger and from the newest item `closed_at` for a work ledger, falling back to `state.json` / `conductor.json` mtime when the stamp is empty — without that fallback a record whose stamp never landed would be permanently uncollectable. An orphan pays the threshold too: a live session's first transcript write lags its first ledger write, so a zero-age orphan is that race rather than garbage.

`_item_census()` reads the item files directly instead of through `work_ledger.list_work_items()`, which skips an unreadable item — the right answer for a listing and the wrong one here, because "no open items" would otherwise be provable by damaging one. A conductor with any unparseable item is reported `unreadable` and kept.

**Damage is classified LAST**, after the open-item and age gates, in both scanners. An unreadable record is deletable with `--purge-unreadable`, so classifying damage first would let one torn item file carry a live open item — and the work its worker is still reporting against — into that deletion. The age gate runs ahead of it for the same reason in a different form: a file being replaced right now can itself read as damaged (a Windows read of a file another handle holds open raises), and for a damaged record age falls back to the DIRECTORY mtime, which `atomic_write` refreshes on every write. So a live ledger reads as young and is left alone rather than reported as damage. `test_a_torn_item_beside_an_open_one_keeps_the_conductor` and `test_a_freshly_written_ledger_that_reads_as_damaged_is_left_alone` pin both halves. The census also answers the worker-binding question, which is why there is no separate binding scan: a binding names one conductor and one of its items, so a binding into a ledger whose every item is terminal is a binding onto a terminal item — the stale half-state `work_ledger._refuse_if_worker_holds_open_item()` documents as replaceable by the next bind. The sweep therefore leaves `bindings/` alone rather than deleting a worker's only report channel on a guess.

An unreadable record is listed and NOT purged unless `--purge-unreadable` is given, and a store whose `slot_key` breadcrumb is gone is never purged at all: the directory name is a fold plus a digest and is not decodable, so neither delete primitive can be aimed at it, and the sweep names the path for a human instead.

`ledger_sweep.purge()` **re-derives the report before deleting anything**, and drops as `stale` any candidate the fresh scan no longer names the same way. A report is a snapshot and the gateway keeps running while an operator reads it, so a ledger can be reopened, an item created, or a damaged file repaired in between — each of which makes the ledger live again. The re-derivation calls `scan()` with the report's own window and orphan setting rather than a second copy of the rules, and matches on kind, store directory and the unreadable flag, so a record that merely changed class is dropped too. It narrows the window to one scan; it does not close it, because no lock spans both stores. `test_a_ledger_reopened_after_the_scan_is_not_purged` and `test_a_conductor_that_opened_an_item_after_the_scan_is_not_purged` pin it.

Deletion goes through the two primitives that already existed for it. Session ledgers use `session_ledger.purge_matching()` with an identity fold and an empty folded set, so the only thing that can match is a breadcrumb holding exactly one of the keys the report named. Work ledgers use `work_ledger.purge_conductor()`, added for this: it takes `conductor_lock` — first in the store's lock order — removes the directory contents under that hold, and unlinks the lock file only after releasing it, because Windows refuses to unlink a file an open handle still holds and doing it inside the critical section would leave the directory behind on exactly the platform the lock covers.

One residual is stated rather than implied, in `purge_conductor`'s own docstring: removing the lock file removes the inode the lock is taken on, so a writer blocked on the old inode is not serialised against a later writer that creates a new one. Ordering cannot fix it — unlinking inside the hold has the same effect, and any path-based advisory lock has this the moment its store is deleted; `session_ledger.purge()` removes the same kind of lock file with no lock held at all. What bounds it is what is left to interleave over: `conductor.json` goes first, so a writer arriving afterwards mints a fresh header instead of editing the purged one, and the worst outcome is two fresh headers racing on a ledger the operator just deleted. The re-derivation above is what keeps a ledger with a live writer from reaching the function at all.

The orphan check lives in `ledger_sweep`, not in either store: `session_ledger` never imports dashboard state and `work_ledger` reaches the product through one routes module, so a store resolving session existence for itself would break both. It is disk-only and conservative — `history.transcript_stems()` over the ledger key and its `dashboard_` / `dashboard:` spellings, plus the keys in `session_map.json` read by path rather than through `SessionMap()` (which migrates and prunes as it loads). It cannot see a live in-memory slot that has persisted nothing yet, which is why it is opt-in.

`test_ledger_sweep.py` pins the rules: `test_dry_run_lists_both_kinds_and_deletes_nothing`, `test_purge_removes_only_the_candidates`, `test_an_in_flight_session_ledger_is_never_a_candidate`, `test_a_conductor_with_an_open_item_is_never_a_candidate`, `test_threshold_is_a_boundary_not_a_hint`, `test_unreadable_session_state_is_listed_but_survives_a_plain_purge`, `test_a_torn_item_record_makes_the_whole_conductor_unreadable`, `test_a_store_without_a_breadcrumb_is_reported_and_never_purged`, `test_orphans_are_opt_in_and_still_pay_the_threshold`, and `test_a_negative_window_refuses_instead_of_collecting_everything` (a clamped negative window would make every ledger a candidate). `test_work_ledger.py::test_purge_conductor_removes_the_ledger_under_the_conductor_lock` pins the lock, asserting what was gone while the hold was still open rather than the return value, which a purge running entirely outside the lock would also produce.

## 3. State record and bounded writes

`session_ledger._empty_state()` defines the state fields:

| Field | Meaning |
|---|---|
| `schema` | stored schema marker |
| `goal` | binding objective |
| `phase` | current free-form work phase |
| `next` | concrete resumable intent |
| `tried` | rejected approaches with their reason and timestamp |
| `artifacts` | string pointers to work artifacts |
| `events` | recent classified progress entries |
| timestamps | creation, latest progress, and terminal-completion time |

`session_ledger._coerce_state()` supplies defaults for malformed known fields and preserves unknown fields for forward compatibility. `session_ledger._read_state_unlocked()` treats unreadable, malformed, undecodable, or oversized state as empty rather than failing a turn. `test_read_state_malformed_oversized_or_undecodable_reads_empty` pins that behavior.

`session_ledger.record()` applies partial updates. Omitted fields retain their stored value; artifact updates merge with the stored map; a supplied rejected approach appends to `tried`; and a nonblank event appends to `events`. `test_record_roundtrip_and_partial_update`, `test_artifacts_merge_and_clamp`, `test_tried_appends_and_caps`, and `test_events_tail_bounded` pin the retention and aging rules. The bounds keep a resumed session from accumulating unbounded durable context.

Every accepted record advances `last_progress_at`. Changing `phase` requires a nonblank event and a recognized event kind. `session_ledger.record()` writes the phase and its event in one atomic state document, so a crash cannot expose a phase move without its classified reason; `test_phase_and_event_land_in_one_document` enforces this load-bearing audit trail. A phase in `session_ledger.TERMINAL_PHASES` stamps `finished_at`; a later non-terminal phase clears it, as guarded by `test_terminal_phase_sets_finished_at_and_reopening_clears_it`.

`session_ledger._serialize_bounded()` evicts oldest history before an accepted state file can exceed the reader ceiling. `test_writer_guarantees_the_read_ceiling_for_legitimate_records` ensures a valid record remains readable, and `test_oversized_unknown_fields_are_dropped_not_self_corrupting` ensures oversized forward-compatible fields do not destroy known state.

Each eviction discards data that never reaches disk, so it cannot be recovered from the stored file the way a read-side clamp can. `_serialize_bounded()` therefore logs one warning per over-budget serialization naming the ledger, the evicted `events`/`tried` counts, and any unknown fields dropped - including on the refusal path, which raises with the in-memory record already stripped. The line describes the document the call built rather than a durable write, because `atomic_write` runs afterwards and may still fail. A document that fits logs nothing. `TestBoundedWriteIsLoudAboutLoss` pins the counts, the named ledger, the refusal report, the failed-write case leaving the stored file intact, and the silence.

`record()` returns the same dict `_serialize_bounded()` evicts from, so a caller's post-write view is the document on disk rather than the pre-eviction one. `test_record_returns_exactly_what_landed_on_disk` pins that equality across an eviction; serializing a copy instead would report entries the write dropped.

Writes use the bounded exclusive lock in `session_ledger._locked()`. Contention raises `OSError` rather than allowing an unserialized write or indefinitely blocking a worker; `test_record_fails_closed_on_held_lock` enforces this. Reads are lock-free because `atomic_write` exposes either the previous or complete replacement document, so the state and event tail come from one transaction.

## 4. MCP surface and authorization

`mcp_tools.__init__.DOMAIN_MODULES` registers `mcp_tools.ledger`. `session_ledger_read` has no arguments and returns the calling session's state and recent event tail. `session_ledger_record` accepts only optional state fields; `validation.SESSION_LEDGER_RECORD_SCHEMA` validates their types and lengths, while `session_ledger.record()` enforces the conditional phase/event rule.

`mcp_tools.ledger._strict_session_key()` obtains a gateway-authored session identity before either tool calls the loopback routes. This is load-bearing because the lenient resolver can walk a subagent process tree to its parent; rejecting an unverified identity prevents a subagent from reading or overwriting the parent's ledger. `test_mcp_tools_refuse_without_strict_identity` and `test_mcp_tools_pass_the_verified_key_to_transport` enforce that boundary.

`dashboard.handlers.session_ledger._resolve_ledger_key()` derives storage identity from the recognized `X-Session-Key`, never the request body. The routes reject missing or unrecognized identities and restricted session modes, so a request can read or write only its own durable ledger. `api_session_ledger_record()` sends bounded-lock failures back as retryable service errors and validates that artifact maps contain only strings. `test_route_refuses_unrecognized_session`, `test_route_refuses_restricted_session`, and `test_route_rejects_non_string_artifacts` cover those boundaries.

## 5. Auto-nudge injection

`dashboard.handlers.autonudge.compose_nudge_body()` renders the normal nudge body, then reads `render_snapshot()` in a worker thread. A nonempty, non-terminal ledger snapshot prefixes the nudge; an absent ledger, terminal phase, or snapshot exception leaves the nudge body unchanged. `test_compose_nudge_body_prefixes_snapshot`, `test_compose_nudge_body_unchanged_without_ledger`, and `test_compose_nudge_body_survives_snapshot_failure` enforce those outcomes.

`session_ledger.render_snapshot()` includes current goal, phase, next step, recent rejected approaches, and artifact pointers, and applies its own rendering bounds. It omits terminal records because completed work provides no next-cycle steering; `test_snapshot_empty_without_ledger_or_when_terminal` and `test_snapshot_contains_state_and_is_capped` guard this behavior.

Every `_fire_*_nudge` adapter in `slack.gateway` calls `compose_nudge_body()`, including the messaging and dashboard transports. `test_gateway_fire_callbacks_use_the_composer` enumerates adapters rather than pinning their count, so a new transport cannot silently bypass the ledger snapshot.

## 6. Failure behavior and scope

A read failure yields an empty ledger. A write lock failure is retryable. Both
closing a tab and permanently deleting its history preserve matching ledger
directories; the explicit sweep in [Cleanup](#cleanup) is what removes stale
ledger state later, and it reports rather than deletes any record it cannot
parse. Snapshot failures are best-effort and never prevent the nudge from
firing.

The ledger does not journal individual tool operations, arbitrate execution ownership with leases, or add a dashboard UI. It records state between wakes; the MCP tools and nudge composer are its public surfaces.
