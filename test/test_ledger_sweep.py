"""Ledger cleanup sweep — the refuse-safe rules, one test per rule.

Pins what docs/system-specs/modules/session-work-ledger.md §2 "Cleanup" states:
a dry run lists and deletes nothing; a purge removes only what the report named;
an in-flight session ledger and a conductor holding an open item are never
candidates at any age; the threshold is a boundary rather than a hint; an
unreadable record is listed but survives a plain purge; the orphan check is
opt-in and still pays the threshold; and the window has one owner, so the CLI
default cannot drift from the module's.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from kiro_crew import ledger_sweep as sweep
from kiro_crew import session_ledger as sl
from kiro_crew import work_ledger as wl

CONDUCTOR = "chat-9-conductor"
WORKER = "chat-9-worker"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test writes into its own data home, never the live one."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


# ── fixtures on disk ──────────────────────────────────────────────────────


def _backdate(path: Path, days: float) -> None:
    stamp = time.time() - days * 86400.0
    os.utime(path, (stamp, stamp))


def _iso_days_ago(days: float) -> str:
    return (datetime.now().astimezone() - timedelta(days=days)).isoformat(timespec="seconds")


def _session_ledger(key: str, *, phase: str, age_days: float) -> Path:
    """A session ledger in *phase*, whose age the sweep will measure as *age_days*."""
    sl.record(key, goal="ship it", phase=phase, event="moved", event_kind="phase")
    directory = sl.ledger_dir(key)
    state = json.loads((directory / "state.json").read_text(encoding="utf-8"))
    if phase in sl.TERMINAL_PHASES:
        state["finished_at"] = _iso_days_ago(age_days)
    (directory / "state.json").write_text(json.dumps(state), encoding="utf-8")
    _backdate(directory / "state.json", age_days)
    return directory


def _work_item(conductor: str = CONDUCTOR) -> str:
    wl.ensure_conductor(conductor, goal="drive the fleet")
    result = wl.apply_conductor_action(
        conductor, "create", title="port the gate", acceptance={"kind": "human_approval"}
    )
    return str(result["item"].item_id)


def _work_ledger(
    conductor: str = CONDUCTOR, *, closed: bool = True, age_days: float = 90.0
) -> Path:
    """A conductor ledger whose single item is closed (or left open)."""
    item_id = _work_item(conductor)
    if closed:
        wl.apply_conductor_action(conductor, "close", item_id=item_id, state="accepted")
        path = wl.item_path(conductor, item_id)
        record = json.loads(path.read_text(encoding="utf-8"))
        record["closed_at"] = _iso_days_ago(age_days)
        path.write_text(json.dumps(record), encoding="utf-8")
    directory = wl.conductor_dir(conductor)
    _backdate(directory / "conductor.json", age_days)
    return directory


def _stores(report: sweep.SweepReport) -> set[str]:
    return {candidate.store for candidate in report.candidates}


# ── dry run ───────────────────────────────────────────────────────────────


def test_dry_run_lists_both_kinds_and_deletes_nothing():
    session = _session_ledger("chat-1-old", phase="done", age_days=90)
    work = _work_ledger()

    report = sweep.scan(older_than_days=30)

    assert _stores(report) == {session.name, work.name}
    assert {c.kind for c in report.candidates} == {sweep.KIND_SESSION, sweep.KIND_WORK}
    assert session.is_dir() and work.is_dir(), "a scan must not remove anything"
    # Every line names the record and why it qualified — the report is what makes
    # the irreversible second command reviewable.
    rendered = "\n".join(sweep.render(report))
    assert session.name in rendered and work.name in rendered
    assert "phase=done" in rendered and "items=0 open/1 closed" in rendered
    assert "2 candidate(s)" in rendered


def test_purge_removes_only_the_candidates():
    stale = _session_ledger("chat-1-old", phase="done", age_days=90)
    live = _session_ledger("chat-2-live", phase="implementing", age_days=90)
    young = _session_ledger("chat-3-young", phase="done", age_days=1)
    work = _work_ledger()
    open_work = _work_ledger("chat-8-busy", closed=False)

    report = sweep.scan(older_than_days=30)
    result = sweep.purge(report)

    assert {c.store for c in result.removed} == {stale.name, work.name}
    assert not result.failed and not result.skipped_unreadable
    assert not stale.exists() and not work.exists()
    assert live.is_dir() and young.is_dir() and open_work.is_dir()


# ── never a candidate, whatever the age ───────────────────────────────────


@pytest.mark.parametrize("phase", ["implementing", "awaiting-ci", "blocked"])
def test_an_in_flight_session_ledger_is_never_a_candidate(phase):
    """The ledger IS what a resumed loop reads to recover its next step, so age
    alone can never make it collectable."""
    directory = _session_ledger("chat-4-busy", phase=phase, age_days=3650)

    report = sweep.scan(older_than_days=1)

    assert directory.name not in _stores(report)
    assert any("in flight" in why for _, store, why in report.kept if store == directory.name)


def test_a_conductor_with_an_open_item_is_never_a_candidate():
    directory = _work_ledger(closed=False, age_days=3650)

    report = sweep.scan(older_than_days=1)

    assert directory.name not in _stores(report)
    assert any("1 open item(s)" in why for _, store, why in report.kept if store == directory.name)


def test_a_bound_workers_open_item_keeps_the_conductor():
    """A binding is a worker's only report channel, and the item census is what
    protects it: the item a live binding names is open, and an open item keeps the
    whole ledger. That is why the sweep needs no separate binding scan."""
    item_id = _work_item()
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=item_id, worker_session_key=WORKER)
    assert wl.read_binding(WORKER) == (CONDUCTOR, item_id)
    directory = wl.conductor_dir(CONDUCTOR)
    _backdate(directory / "conductor.json", 90)

    report = sweep.scan(older_than_days=30)

    assert directory.name not in _stores(report)
    assert any("1 open item(s)" in why for _, store, why in report.kept if store == directory.name)


# ── threshold ─────────────────────────────────────────────────────────────


def test_threshold_is_a_boundary_not_a_hint():
    directory = _session_ledger("chat-5-edge", phase="done", age_days=30.5)

    assert directory.name in _stores(sweep.scan(older_than_days=30))
    assert directory.name not in _stores(sweep.scan(older_than_days=31))


def test_age_falls_back_to_the_state_file_when_the_stamp_is_empty():
    """A terminal record whose ``finished_at`` never landed is still measurable —
    otherwise it would be permanently uncollectable."""
    directory = _session_ledger("chat-6-nostamp", phase="done", age_days=90)
    state_path = directory / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["finished_at"] = ""
    state_path.write_text(json.dumps(state), encoding="utf-8")
    _backdate(state_path, 90)

    candidates = [c for c in sweep.scan(older_than_days=30).candidates if c.store == directory.name]

    assert candidates and candidates[0].age_days >= 89


# ── unreadable ────────────────────────────────────────────────────────────


def test_unreadable_session_state_is_listed_but_survives_a_plain_purge():
    directory = _session_ledger("chat-7-torn", phase="done", age_days=90)
    (directory / "state.json").write_text("{not json", encoding="utf-8")
    _backdate(directory / "state.json", 90)
    _backdate(directory, 90)

    report = sweep.scan(older_than_days=30)
    listed = [c for c in report.candidates if c.store == directory.name]
    assert listed and listed[0].unreadable
    assert "unreadable" in "\n".join(sweep.render(report))
    assert not report.removable, "an unreadable record is not a plain-purge candidate"

    kept = sweep.purge(report)
    assert directory.is_dir()
    assert {c.store for c in kept.skipped_unreadable} == {directory.name}

    gone = sweep.purge(sweep.scan(older_than_days=30), include_unreadable=True)
    assert {c.store for c in gone.removed} == {directory.name}
    assert not directory.exists()


def test_a_torn_item_record_makes_the_whole_conductor_unreadable():
    """``list_work_items`` SKIPS an unreadable item, so "no open items" must not
    be provable by damaging one."""
    item_id = _work_item()
    wl.item_path(CONDUCTOR, item_id).write_text("{tor", encoding="utf-8")
    directory = wl.conductor_dir(CONDUCTOR)
    _backdate(directory / "conductor.json", 90)

    report = sweep.scan(older_than_days=30)
    listed = [c for c in report.candidates if c.store == directory.name]

    assert listed and listed[0].unreadable
    assert not report.removable
    sweep.purge(report)
    assert directory.is_dir()


def test_a_store_without_a_breadcrumb_is_reported_and_never_purged():
    """Neither delete primitive can be aimed at a store whose key is unknown, so
    the sweep names it for a human instead of guessing."""
    directory = _session_ledger("chat-11-anon", phase="done", age_days=90)
    (directory / "slot_key").unlink()
    (directory / "state.json").write_text("{", encoding="utf-8")
    _backdate(directory, 90)

    report = sweep.scan(older_than_days=30)
    listed = [c for c in report.candidates if c.store == directory.name]
    assert listed and not listed[0].purgeable
    assert "no slot_key breadcrumb" in listed[0].reason

    result = sweep.purge(report, include_unreadable=True)
    assert {c.store for c in result.skipped_unaddressable} == {directory.name}
    assert directory.is_dir()


# ── orphans ───────────────────────────────────────────────────────────────


def test_orphans_are_opt_in_and_still_pay_the_threshold(tmp_path):
    """A ledger whose key names no session is only listed with the flag, and only
    once it is old: a live session's first transcript write lags its first ledger
    write, so a young orphan is that race rather than garbage."""
    old = _session_ledger("chat-12-gone", phase="implementing", age_days=90)
    young = _session_ledger("chat-13-fresh", phase="implementing", age_days=1)

    assert old.name not in _stores(sweep.scan(older_than_days=30))
    with_flag = _stores(sweep.scan(older_than_days=30, include_orphans=True))
    assert old.name in with_flag
    assert young.name not in with_flag


def test_a_key_with_a_transcript_is_not_an_orphan():
    """The check uses the real key-to-file mapping: the ledger key is stored
    stripped of its ``dashboard_`` prefix, and the transcript carries it."""
    from kiro_crew.config.paths import config_dir
    from kiro_crew.history import SESSIONS_DIR_NAME

    directory = _session_ledger("chat-14-alive", phase="implementing", age_days=90)
    sessions = config_dir() / SESSIONS_DIR_NAME
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "dashboard_chat-14-alive.jsonl").write_text("{}\n", encoding="utf-8")

    assert directory.name not in _stores(sweep.scan(older_than_days=30, include_orphans=True))


def test_a_key_in_the_session_map_is_not_an_orphan():
    from kiro_crew.config.paths import config_dir

    directory = _session_ledger("chat-15-mapped", phase="implementing", age_days=90)
    config_dir().mkdir(parents=True, exist_ok=True)
    (config_dir() / "session_map.json").write_text(
        json.dumps({"dashboard:chat-15-mapped": {"sid": "abc"}}), encoding="utf-8"
    )

    assert directory.name not in _stores(sweep.scan(older_than_days=30, include_orphans=True))


# ── stores that are absent or damaged must not crash a report ─────────────


def test_scan_is_silent_on_a_machine_with_no_ledgers():
    report = sweep.scan(older_than_days=30)
    assert report.candidates == () and report.kept == ()
    assert "no ledger is older than the threshold" in "\n".join(sweep.render(report))


def test_the_bindings_directory_is_not_mistaken_for_a_conductor():
    _work_ledger()
    wl.bindings_dir().mkdir(parents=True, exist_ok=True)

    report = sweep.scan(older_than_days=30)

    assert "bindings" not in _stores(report)


# ── one owner for the window ──────────────────────────────────────────────


def test_the_cli_default_window_comes_from_the_module(monkeypatch, capsys):
    """``--older-than-days`` defaults to ``None`` and the module resolves it, so
    the CLI holds no second literal that could drift from the module's."""
    from kiro_crew import cli_doctor

    assert sweep.DEFAULT_OLDER_THAN_DAYS == 30
    seen: dict[str, float] = {}

    def _fake_scan(*, older_than_days, include_orphans):
        seen["window"] = older_than_days
        return sweep.SweepReport((), (), older_than_days, include_orphans)

    monkeypatch.setattr(sweep, "scan", _fake_scan)
    cli_doctor._ledger_sweep(
        purge=False, older_than_days=None, include_orphans=False, purge_unreadable=False
    )
    capsys.readouterr()

    assert seen["window"] == sweep.DEFAULT_OLDER_THAN_DAYS


def test_a_negative_window_refuses_instead_of_collecting_everything(capsys):
    """Clamping a negative window to zero would make every ledger a candidate,
    which is the one input a purge must not accept."""
    from kiro_crew import cli_doctor

    with pytest.raises(SystemExit) as exit_info:
        cli_doctor._ledger_sweep(
            purge=True, older_than_days=-1, include_orphans=False, purge_unreadable=False
        )

    assert exit_info.value.code == 2
    assert "must not be negative" in capsys.readouterr().out


def test_the_doctor_health_pass_does_not_run_in_sweep_mode(monkeypatch, capsys):
    """The sweep is a MODE, like ``--bundle``: it reports on stored state rather
    than on the health of the install, so it must not print the health report."""
    from kiro_crew import cli_doctor

    called: list[bool] = []
    monkeypatch.setattr(
        cli_doctor,
        "_ledger_sweep",
        lambda **kwargs: called.append(True),
    )
    cli_doctor._doctor(ledger_sweep=True)

    assert called == [True]
    assert "Kiro Crew Doctor" not in capsys.readouterr().out


# ── damage is classified last ──────────────────────────────────────────────


def test_a_torn_item_beside_an_open_one_keeps_the_conductor():
    """One open item outranks every other reading, damage included.

    Classifying damage first would let ONE torn file carry a live open item —
    and the work its worker is still reporting against — into the deletion
    ``--purge-unreadable`` authorises.
    """
    open_item = _work_item()
    torn = wl.apply_conductor_action(
        CONDUCTOR, "create", title="second", acceptance={"kind": "human_approval"}
    )["item"].item_id
    wl.item_path(CONDUCTOR, torn).write_text("{tor", encoding="utf-8")
    directory = wl.conductor_dir(CONDUCTOR)
    _backdate(directory / "conductor.json", 90)

    report = sweep.scan(older_than_days=30)

    assert directory.name not in _stores(report)
    assert wl.read_work_item(CONDUCTOR, open_item) is not None
    result = sweep.purge(report, include_unreadable=True)
    assert not result.removed
    assert directory.is_dir()


def test_a_freshly_written_ledger_that_reads_as_damaged_is_left_alone():
    """A file being replaced right now can itself read as unreadable — a Windows
    read of a file another handle holds open raises — so the age gate runs before
    damage is classified, measured from the directory ``atomic_write`` renames
    into."""
    directory = _session_ledger("chat-16-mid-write", phase="done", age_days=90)
    (directory / "state.json").write_text("{half", encoding="utf-8")
    _backdate(directory / "state.json", 90)
    # Directory mtime stays NOW: this ledger was just written to.

    report = sweep.scan(older_than_days=30)

    assert directory.name not in _stores(report)
    sweep.purge(report, include_unreadable=True)
    assert directory.is_dir()


# ── the report is re-derived before the delete ─────────────────────────────


def test_a_ledger_reopened_after_the_scan_is_not_purged():
    """A report is a snapshot and the gateway keeps running while it is read, so
    the verdict is re-derived immediately before the delete."""
    stale = _session_ledger("chat-17-reopened", phase="done", age_days=90)
    other = _session_ledger("chat-18-still-done", phase="done", age_days=90)

    report = sweep.scan(older_than_days=30)
    assert {stale.name, other.name} <= _stores(report)

    # The session came back to life between the report and the purge.
    sl.record("chat-17-reopened", phase="implementing", event="resumed", event_kind="phase")

    result = sweep.purge(report)

    assert {c.store for c in result.stale} == {stale.name}
    assert {c.store for c in result.removed} == {other.name}
    assert stale.is_dir(), "a reopened ledger must survive a stale report"
    assert not other.exists()
    assert "changed since the scan" in "\n".join(sweep.render(report, purged=result))


def test_a_conductor_that_opened_an_item_after_the_scan_is_not_purged():
    directory = _work_ledger()

    report = sweep.scan(older_than_days=30)
    assert directory.name in _stores(report)

    wl.apply_conductor_action(
        CONDUCTOR, "create", title="new round", acceptance={"kind": "human_approval"}
    )

    result = sweep.purge(report)

    assert {c.store for c in result.stale} == {directory.name}
    assert not result.removed
    assert directory.is_dir()
