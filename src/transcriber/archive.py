"""Archive by age — his pick, and the most conservative of the three options put to him.

Once a month, source recordings older than ``ARCHIVE_AGE_DAYS`` (60 by default) are moved
out of the working folder into the archive folder. That is the whole job, and almost all of
this module is the checking around it, because moving somebody's only copy of what he said
on site is the one operation here that cannot be undone by re-running anything.

Five rules, and none of them bends:

**Nothing is ever deleted.** There is no delete call in this file and there is not meant to
be one. An archived recording is in a different folder; it is not gone.

**A failure is never moved.** Only rows the ledger has as DONE with all three outputs named
are even considered, and each one is re-checked here rather than taken on trust from the
query that produced it.

**Nothing recent is touched.** The age test is applied twice — once in the ledger's query,
once again here against the item's own creation date — so a bug in either place fails
closed rather than sweeping up last week's site walk.

**Outputs are confirmed present before each move, one file at a time.** The ledger saying a
transcript exists is this service's own belief about its own work, and the incumbent has at
least one recording with a summary and no transcript. So before an original moves, each of
its three outputs is fetched from OneDrive and must come back as a real file with bytes in
it. Then that one recording is moved, and only then is the next one considered: an
interruption halfway through leaves every recording either fully moved and recorded, or
untouched.

**A recording is only ever moved into its own route's archive folder.** The pass runs one
route at a time and asks the ledger for that route's finished recordings, and then checks
the row's route again immediately before the move. Moving a site-meeting recording into the
phone-calls archive would not lose it, but it would file it under a kind it never belonged
to, and the record is only worth anything if where a thing sits is true. A route with no
archive folder is **skipped, not failed**: an empty archive folder means "this kind of
recording stays where it is", which is a decision, not a misconfiguration.

The one ordering hazard is a move that succeeds and a ledger write that then fails. That
leaves the file in the archive folder with no ``archived_at``. The next run notices,
because it re-reads the item and finds it is already where it belongs, and records it
without moving anything. Self-correcting, and visible in the meantime.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .ledger import Ledger, LedgerError
from .models import DEFAULT_ROUTE, Row, State, utc_now_iso
from .sweep import local_now, parse_stamp, route_display, select_routes

log = logging.getLogger("transcriber.archive")

__all__ = [
    "ArchiveOutcome",
    "ArchiveReport",
    "ArchiveRun",
    "archive",
    "archive_route",
    "should_run",
    "mark_run",
    "ARCHIVE_MONTH_MARK",
    "ARCHIVE_ATTEMPT_MARK",
    "RETRY_AFTER_S",
]

#: Bookkeeping mark: the local ``YYYY-MM`` the archive pass last completed for.
ARCHIVE_MONTH_MARK = "archive:last_completed_month"

#: When the pass last started, successful or not — so a month of failed moves is retried
#: periodically rather than on every poll of the worker loop.
ARCHIVE_ATTEMPT_MARK = "archive:last_attempt_at"

#: How long a failed archive pass waits before going round again.
RETRY_AFTER_S = 6 * 3600.0

#: Stop after this many consecutive failures **on one route**. If OneDrive is refusing moves
#: into that folder, the eleventh attempt tells nobody anything the first three did not.
#: Counted per route on purpose: one route's folder being unwritable says nothing about
#: another's, and stopping the whole pass on it would quietly stop archiving everything.
MAX_CONSECUTIVE_FAILURES = 5

MOVED = "moved"
ALREADY_THERE = "already-in-archive"
HELD_BACK = "held-back"
FAILED = "failed"


@dataclass(frozen=True)
class ArchiveOutcome:
    """What happened to one recording, and why. ``held-back`` is a normal, good answer."""

    item_id: str
    name: str
    result: str
    detail: str
    route: str = ""

    @property
    def needs_a_person(self) -> bool:
        return self.result == FAILED

    def line(self) -> str:
        mark = "!" if self.needs_a_person else "-"
        return f"  {mark} {self.result}: {self.name or self.item_id} — {self.detail}"


@dataclass
class ArchiveReport:
    """One route's archive pass."""

    started_at: str
    age_days: int
    finished_at: str = ""
    considered: int = 0
    moved: int = 0
    already_there: int = 0
    held_back: int = 0
    failed: int = 0
    dry_run: bool = False
    stopped_early: str = ""
    outcomes: list[ArchiveOutcome] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: Appended rather than inserted: every caller builds this by keyword, and a new field at
    #: the end cannot change what an existing positional construction means.
    route: str = DEFAULT_ROUTE
    route_label: str = ""
    #: Why this route was passed over without being an error — almost always "it has no
    #: archive folder, so its recordings stay where they are".
    skipped: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors and not self.failed and not self.stopped_early

    @property
    def display(self) -> str:
        label = (self.route_label or "").strip()
        return f"{label} ({self.route})" if label and label != self.route else self.route

    def add(self, item_id: str, name: str, result: str, detail: str) -> None:
        self.outcomes.append(ArchiveOutcome(item_id, name, result, detail, self.route))
        if result == MOVED:
            self.moved += 1
        elif result == ALREADY_THERE:
            self.already_there += 1
        elif result == HELD_BACK:
            self.held_back += 1
        elif result == FAILED:
            self.failed += 1

    def headline(self) -> str:
        if self.skipped:
            return f"{self.display}: {self.skipped}"
        if self.stopped_early:
            return f"{self.display} STOPPED: {self.stopped_early}"
        if self.errors:
            return f"{self.display} FAILED: {self.errors[0]}"
        if not self.considered:
            return f"{self.display}: nothing is older than {self.age_days} days and finished"
        parts = [f"{self.moved} moved"]
        if self.already_there:
            parts.append(f"{self.already_there} already in the archive")
        if self.held_back:
            parts.append(f"{self.held_back} held back")
        if self.failed:
            parts.append(f"{self.failed} FAILED")
        return f"{self.display}: " + ", ".join(parts)

    def render(self) -> str:
        lines = [self.headline()]
        if self.dry_run:
            lines.append("  (dry run — nothing was moved)")
        for outcome in self.outcomes:
            lines.append(outcome.line())
        for error in self.errors:
            lines.append(f"  ! error: {error}")
        return "\n".join(lines)


@dataclass
class ArchiveRun:
    """The whole month's pass, across every route.

    The counters are sums over the routes, so the total and the per-route lines can never
    disagree. A route that was skipped for having no archive folder is still listed: "it did
    nothing" and "it was never asked" look identical in a report that omits it, and only one
    of those is what he configured.
    """

    started_at: str
    age_days: int
    finished_at: str = ""
    dry_run: bool = False
    reports: list[ArchiveReport] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    #: Things worth saying that are not faults — a route with no archive folder, finished
    #: recordings sitting on a route nobody watches any more.
    notes: list[str] = field(default_factory=list)

    def _sum(self, attribute: str) -> int:
        return sum(int(getattr(r, attribute, 0) or 0) for r in self.reports)

    considered = property(lambda self: self._sum("considered"))
    moved = property(lambda self: self._sum("moved"))
    already_there = property(lambda self: self._sum("already_there"))
    held_back = property(lambda self: self._sum("held_back"))
    failed = property(lambda self: self._sum("failed"))

    @property
    def outcomes(self) -> list[ArchiveOutcome]:
        out: list[ArchiveOutcome] = []
        for report in self.reports:
            out.extend(report.outcomes)
        return out

    @property
    def errors(self) -> list[str]:
        out = list(self.problems)
        for report in self.reports:
            out.extend(f"{report.display}: {error}" for error in report.errors)
        return out

    @property
    def stopped_early(self) -> str:
        stopped = [f"{r.display}: {r.stopped_early}" for r in self.reports if r.stopped_early]
        return "; ".join(stopped)

    @property
    def ok(self) -> bool:
        return not self.problems and all(r.ok for r in self.reports)

    def report_for(self, route: str) -> ArchiveReport | None:
        for report in self.reports:
            if report.route == route:
                return report
        return None

    def headline(self) -> str:
        if self.problems and not self.reports:
            return f"archive pass did not run: {self.problems[0]}"
        failed = [r for r in self.reports if not r.ok]
        if failed:
            names = ", ".join(r.display for r in failed)
            return f"archive ({self.age_days} days): PROBLEMS on {names}"
        if not self.considered:
            return f"archive: nothing is older than {self.age_days} days and finished"
        parts = [f"{self.moved} moved"]
        if self.already_there:
            parts.append(f"{self.already_there} already in the archive")
        if self.held_back:
            parts.append(f"{self.held_back} held back")
        if self.failed:
            parts.append(f"{self.failed} FAILED")
        return f"archive ({self.age_days} days): " + ", ".join(parts)

    def render(self) -> str:
        lines = [self.headline()]
        if self.dry_run:
            lines.append("  (dry run — nothing was moved)")
        lines.append("  nothing was deleted; nothing can be deleted by this pass")
        for problem in self.problems:
            lines.append(f"  ! {problem}")
        for note in self.notes:
            lines.append(f"  - {note}")
        for report in self.reports:
            lines.append("")
            for line in report.render().split("\n"):
                lines.append(f"  {line}" if line else "")
        return "\n".join(lines)


def archive(
    config: Any,
    ledger: Ledger,
    graph: Any,
    *,
    route: str | None = None,
    now: float | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    age_days: int | None = None,
) -> ArchiveRun:
    """Move aged, finished, output-confirmed recordings into **their own route's** archive.

    ``route`` narrows the pass to one route; omitted, every enabled route is passed over in
    turn. ``limit`` is a budget for the whole run, so an operator asking for ten moves gets
    ten moves and not ten per route.
    """
    clock = time.time() if now is None else now
    days = int(age_days if age_days is not None else getattr(config, "archive_age_days", 60) or 60)
    run = ArchiveRun(started_at=utc_now_iso(clock), age_days=days, dry_run=dry_run)

    routes, problems = select_routes(config, route)
    run.problems.extend(problems)
    for problem in problems:
        log.error("archive pass: %s", problem)
    if not routes:
        run.finished_at = utc_now_iso()
        return run

    if not dry_run:
        ledger.cursor_set(ARCHIVE_ATTEMPT_MARK, utc_now_iso(clock))

    budget = None if limit is None else max(0, int(limit))
    for one in routes:
        try:
            report = archive_route(
                config, ledger, graph, one,
                now=clock, dry_run=dry_run, limit=budget, age_days=days,
            )
        except Exception as exc:  # noqa: BLE001 - one route's bad month is not the others'
            report = ArchiveReport(
                started_at=utc_now_iso(clock),
                finished_at=utc_now_iso(),
                age_days=days,
                dry_run=dry_run,
                route=str(getattr(one, "name", "") or DEFAULT_ROUTE),
                route_label=str(getattr(one, "label", "") or ""),
            )
            report.errors.append(f"{type(exc).__name__}: {exc}")
            log.exception("the archive pass for %s failed outright", route_display(one))
        run.reports.append(report)
        if report.skipped:
            run.notes.append(report.headline())
        if budget is not None:
            budget = max(0, budget - report.moved)
        log.info("%s", report.headline())

    if route is None:
        _note_unwatched_routes(ledger, run, days=days, clock=clock, watched={r.route for r in run.reports})

    if run.ok and not dry_run:
        mark_run(config, ledger, now=clock)

    run.finished_at = utc_now_iso()
    log.info("%s", run.headline())
    return run


def archive_route(
    config: Any,
    ledger: Ledger,
    graph: Any,
    route: Any,
    *,
    now: float | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    age_days: int | None = None,
) -> ArchiveReport:
    """One route's pass. Never raises; a bad route is reported, not thrown."""
    clock = time.time() if now is None else now
    days = int(age_days if age_days is not None else getattr(config, "archive_age_days", 60) or 60)
    name = str(getattr(route, "name", "") or DEFAULT_ROUTE)
    report = ArchiveReport(
        started_at=utc_now_iso(clock),
        age_days=days,
        dry_run=dry_run,
        route=name,
        route_label=str(getattr(route, "label", "") or ""),
    )

    archive_folder_id = str(getattr(route, "archive_folder_id", "") or "").strip()
    if not archive_folder_id:
        # A deliberate value, not a missing one: this kind of recording stays where it is.
        report.skipped = (
            "no archive folder is configured for it, so its recordings stay where they are; "
            "nothing was touched"
        )
        report.finished_at = utc_now_iso()
        return report

    try:
        candidates = ledger.due_for_archive(days, now=clock, route=name)
    except LedgerError as exc:
        report.errors.append(f"the ledger could not list what is due for archive: {exc}")
        report.finished_at = utc_now_iso()
        return report

    if limit is not None:
        candidates = candidates[: max(0, int(limit))]

    consecutive_failures = 0
    for row in candidates:
        report.considered += 1
        verdict, detail = _may_move(row, days, clock, route_name=name)
        if not verdict:
            report.add(row.item_id, row.name, HELD_BACK, detail)
            continue

        try:
            result, detail = _archive_one(
                ledger, graph, row, route,
                archive_folder_id=archive_folder_id,
                dry_run=dry_run,
                clock=clock,
            )
        except Exception as exc:  # noqa: BLE001 - one bad recording must not end the pass
            result, detail = FAILED, f"{type(exc).__name__}: {exc}"
            log.exception("archiving %s failed", row.item_id)

        report.add(row.item_id, row.name, result, detail)

        if result == FAILED:
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                report.stopped_early = (
                    f"{consecutive_failures} moves failed in a row, so this route was left alone "
                    "rather than keep hammering OneDrive; everything not yet moved is exactly "
                    "where it was, and the other routes were still done"
                )
                log.error("%s: %s", report.display, report.stopped_early)
                break
        else:
            consecutive_failures = 0

    report.finished_at = utc_now_iso()
    return report


def _note_unwatched_routes(
    ledger: Ledger, run: ArchiveRun, *, days: int, clock: float, watched: set[str]
) -> None:
    """Finished recordings on a route that is no longer configured.

    Nothing is moved for them — there is no folder to move them to — but they are said out
    loud, because "the archive pass ran and did not mention them" would otherwise be the
    only record of a whole kind of recording ageing quietly in the working folder.
    """
    try:
        seen = ledger.routes_seen()
    except LedgerError as exc:  # noqa: BLE001 - a reporting extra never fails the pass
        log.warning("could not list the routes in the ledger: %s", exc)
        return
    for name in seen:
        if name in watched:
            continue
        try:
            waiting = ledger.due_for_archive(days, now=clock, route=name)
        except LedgerError:
            continue
        if not waiting:
            continue
        run.notes.append(
            f"{len(waiting)} finished recording(s) on the route {name!r} are older than "
            f"{days} days, but that route is not in the configuration any more, so there is no "
            "archive folder to move them to. They are untouched and their ledger history is intact."
        )


def _may_move(row: Row, days: int, clock: float, *, route_name: str = "") -> tuple[bool, str]:
    """The second, independent age, state and route check. Fails closed on anything unclear."""
    if route_name and (row.route or DEFAULT_ROUTE) != route_name:
        # Belt and braces over the ledger's own filter. A recording belongs to the route it
        # arrived on, and that route's archive folder is the only folder it may be moved to.
        return False, (
            f"it arrived on the route {row.route!r}, not {route_name!r} — a recording is only "
            "ever moved into its own route's archive folder"
        )
    if row.state != State.DONE:
        return False, f"it is {row.state}, not finished — a recording that did not succeed is never moved"
    if row.archived_at:
        return False, "already recorded as archived"
    if row.quarantine_reason:
        return False, "it carries a quarantine reason; a failure is never moved"
    if row.source_deleted_at:
        # Established on an earlier pass. Still reported every month, because an original
        # that left OneDrive without us is a fact somebody should eventually explain — but
        # not re-queried, because the answer will not have changed.
        return False, (
            f"the original left OneDrive before it could be archived (noticed {row.source_deleted_at[:10]}); "
            "there is nothing here to move"
        )
    if not row.outputs_present:
        return False, "the ledger does not name all three outputs for it"
    stamp = parse_stamp(row.created_at) or parse_stamp(row.discovered_at)
    if stamp is None:
        return False, "it has no usable date, so its age cannot be established and it stays put"
    age_days = (clock - stamp) / 86400.0
    if age_days < days:
        return False, f"it is {age_days:.1f} days old, inside the {days}-day window"
    return True, ""


def _archive_one(
    ledger: Ledger,
    graph: Any,
    row: Row,
    route: Any,
    *,
    archive_folder_id: str,
    dry_run: bool,
    clock: float,
) -> tuple[str, str]:
    """Confirm the outputs, confirm the source, move it, record it. In that order."""
    confirmed, detail = _outputs_confirmed(graph, row, route)
    if not confirmed:
        return HELD_BACK, detail

    try:
        source = graph.get_item(row.item_id)
    except Exception as exc:  # noqa: BLE001
        message = f"{type(exc).__name__}: {exc}"
        if _looks_missing(exc):
            # Not our doing and not recoverable, but it is a fact about the record and it
            # is written down rather than passed over.
            if not dry_run:
                ledger.set_fields(row.item_id, source_deleted_at=utc_now_iso(clock))
            return HELD_BACK, (
                "the original is no longer in OneDrive — somebody moved or deleted it outside this "
                f"service. Recorded, not archived ({message})"
            )
        return FAILED, f"could not re-read the original before moving it ({message})"

    if str(getattr(source, "parent_id", "") or "") == archive_folder_id:
        if not dry_run:
            ledger.set_fields(row.item_id, archived_at=utc_now_iso(clock), parent_id=archive_folder_id)
        return ALREADY_THERE, "it was already in the archive folder; the ledger has been brought into line"

    if dry_run:
        return MOVED, f"would be moved to this route's archive folder ({detail})"

    graph.move(row.item_id, archive_folder_id)
    # Only after the move has actually returned. If this write fails, the next pass finds
    # the item already in the archive folder and records it then.
    ledger.set_fields(row.item_id, archived_at=utc_now_iso(clock), parent_id=archive_folder_id)
    return MOVED, detail


def _outputs_confirmed(graph: Any, row: Row, route: Any) -> tuple[bool, str]:
    """All three outputs must be real files in OneDrive right now, or nothing moves.

    "Cannot confirm" and "confirmed absent" are treated identically, because the safe act is
    the same for both: leave the original alone and say so.
    """
    wanted = {
        "transcript": row.transcript_name,
        "summary": row.summary_name,
        "actions": row.actions_name,
    }
    ids = dict(row.output_item_ids or {})
    missing: list[str] = []
    checked = 0

    for label, name in wanted.items():
        if not name:
            missing.append(f"no {label} is named in the ledger")
            continue
        item_id = _output_id_for(ids, label)
        if item_id:
            ok, why = _item_is_real(graph, item_id)
            if ok:
                checked += 1
            else:
                missing.append(f"{label} ({name}): {why}")
            continue
        # No id was recorded for it. Fall back to looking for it in **this route's** output
        # folder by the name we wrote. This listing is only ever used to *confirm* a file is
        # there; a listing that came back short means "not confirmed", which holds the move
        # back.
        ok, why = _named_output_present(graph, route, name)
        if ok:
            checked += 1
        else:
            missing.append(f"{label} ({name}): {why}")

    if missing:
        return False, "outputs not confirmed — " + "; ".join(missing)
    return True, f"all {checked} outputs confirmed present in OneDrive"


def _output_id_for(ids: dict[str, str], label: str) -> str:
    """Find the recorded driveItem id for one output, however outputs.py keyed the map."""
    for key, value in ids.items():
        if not value:
            continue
        if str(key).strip().lower() == label:
            return str(value)
    return ""


def _item_is_real(graph: Any, item_id: str) -> tuple[bool, str]:
    try:
        item = graph.get_item(item_id)
    except Exception as exc:  # noqa: BLE001
        return False, f"could not be read back from OneDrive ({type(exc).__name__}: {exc})"
    if getattr(item, "is_deleted", False):
        return False, "OneDrive reports it as deleted"
    if getattr(item, "is_folder", False):
        return False, "the recorded id is a folder, not the output file"
    if int(getattr(item, "size", 0) or 0) <= 0:
        return False, "it is present but empty"
    return True, ""


def _named_output_present(graph: Any, route: Any, name: str) -> tuple[bool, str]:
    output_folder_id = str(getattr(route, "output_folder_id", "") or "")
    if not output_folder_id:
        return False, "no id was recorded for it and this route has no output folder to look in"
    try:
        children = graph.list_children(output_folder_id)
    except Exception as exc:  # noqa: BLE001
        return False, f"the output folder could not be listed ({type(exc).__name__}: {exc})"
    for child in children:
        if str(getattr(child, "name", "")) == name and int(getattr(child, "size", 0) or 0) > 0:
            return True, ""
    return False, "no id was recorded for it and it was not found in this route's output folder"


def _looks_missing(exc: Exception) -> bool:
    if getattr(exc, "is_not_found", False):
        return True
    return int(getattr(exc, "status", 0) or 0) == 404


# --------------------------------------------------------------------------- schedule


def should_run(config: Any, ledger: Ledger, *, now: float | None = None) -> bool:
    """True once in a local month, on or after the configured day of the month.

    "On or after" rather than "on", so a service that was down on the 1st still archives on
    the 2nd instead of waiting a further month.
    """
    clock = time.time() if now is None else now
    moment = local_now(config, clock)
    if moment.day < int(getattr(config, "archive_day_of_month", 1) or 1):
        return False
    if ledger.cursor_get(ARCHIVE_MONTH_MARK) == moment.strftime("%Y-%m"):
        return False
    last_attempt = parse_stamp(ledger.cursor_get(ARCHIVE_ATTEMPT_MARK))
    if last_attempt is not None and (clock - last_attempt) < RETRY_AFTER_S:
        return False
    return True


def mark_run(config: Any, ledger: Ledger, *, now: float | None = None) -> None:
    ledger.cursor_set(ARCHIVE_MONTH_MARK, local_now(config, now).strftime("%Y-%m"))
