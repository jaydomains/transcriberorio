"""Forgetting a recording, on purpose, at a named person's request.

Everywhere else in this service, nothing is ever deleted. :mod:`transcriber.archive` says
so in its own first rule and means it literally — there is no delete call in that file. The
monthly pass moves originals into an archive folder and that is all it will ever do.

This module is the deliberate exception, and it is a separate file precisely so that rule
stays true where it is written. Nothing here runs on a schedule, nothing here is reached by
the worker, and nothing here happens without a person's name attached.

**Why it exists.** A client or a member of staff can ask what is held about them and ask for
it to be removed. Until now the honest answer was "everything, forever, and there is no way
to take it out", which is a fine answer for an archive and not one you can give somebody who
asks. The rule is now: nothing is ever removed automatically; a person can remove something
on request.

**What survives, and why that is not a loophole.** The ledger row stays as a TOMBSTONE — the
item id, the route, when it arrived, when it finished, and the fact that it was erased, by
whom, and on what request. Everything describing the recording is gone: its name, the names
of its three output files, its hashes, its metadata. Two reasons. A record with a silent
hole in it is worse than one that says a thing was here and was removed on this date. And a
deleted row is a row the next enumeration rediscovers as new.

**What this CANNOT reach, said out loud.** Three things, and a person carrying out an
erasure needs all three:

  * **The recycle bin.** Deleting a file in OneDrive moves it to the recycle bin, where it
    sits for up to 93 days on a business account and can be restored by an administrator
    the whole time. Until that bin is emptied the file is not gone, and this module says so
    in its report rather than letting "deleted" imply more than it did.
  * **The site record.** The documents this service publishes are ingested by another
    repository which this service may only read. Removing the transcript here does not
    remove what the record derived from it. The report names those files so somebody can
    go and deal with them.
  * **Anything already sent to a person.** An emailed morning digest, a transcript somebody
    downloaded. Out of reach by definition, and worth saying.

**It cannot delete what it did not write.** Every candidate comes from the ledger, and the
only files touched are the source recording and the three outputs named on that row. There
is no path here that takes a folder id or a file path from a person and deletes it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .models import Row, State, utc_now_iso

log = logging.getLogger(__name__)

__all__ = [
    "EraseCandidate",
    "ErasePlan",
    "EraseResult",
    "columns_not_covered",
    "erase",
    "plan",
]


@dataclass
class EraseCandidate:
    """One recording that would be forgotten, and everything of it that can be reached."""

    item_id: str
    name: str = ""
    route: str = ""
    recorded_at: str = ""
    state: str = ""
    #: The original audio, if the ledger still believes it is where it was.
    source_present: bool = True
    #: The three published files, by the names this service gave them.
    output_names: tuple[str, ...] = ()
    output_ids: tuple[str, ...] = ()
    held_passages: int = 0

    @property
    def reach(self) -> int:
        return (1 if self.source_present else 0) + len(self.output_ids)


@dataclass
class ErasePlan:
    """What would go. Building one touches nothing at all."""

    candidates: tuple[EraseCandidate, ...] = ()
    #: What was asked for, echoed back so a report says what it was answering.
    asked: str = ""

    @property
    def recordings(self) -> int:
        return len(self.candidates)

    @property
    def files(self) -> int:
        return sum(c.reach for c in self.candidates)

    @property
    def held(self) -> int:
        return sum(c.held_passages for c in self.candidates)

    @property
    def unreachable_outputs(self) -> tuple[str, ...]:
        """Output files named on a row but with no id to delete them by.

        A published file whose id was never recorded cannot be removed by this service. It
        is named so a person can remove it by hand, and NOT quietly counted as erased.
        """
        out: list[str] = []
        for c in self.candidates:
            if len(c.output_ids) < len(c.output_names):
                have = set(c.output_ids)
                for name in c.output_names:
                    if name and name not in have:
                        out.append(name)
        return tuple(out)


@dataclass
class EraseResult:
    """What actually happened. Every number here is a thing that was done, not intended."""

    recordings: int = 0
    files_deleted: int = 0
    files_already_gone: int = 0
    files_refused: tuple[str, ...] = ()
    held_forgotten: int = 0
    rows_tombstoned: int = 0
    by: str = ""
    because: str = ""
    at: str = ""
    #: Named rather than counted: these are what somebody still has to deal with by hand.
    still_in_the_record: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.files_refused


def plan(ledger: Any, *, rows: Sequence[Row], asked: str = "") -> ErasePlan:
    """What erasing these rows would reach. Reads only; writes nothing; deletes nothing."""
    candidates: list[EraseCandidate] = []
    for row in rows:
        if getattr(row, "state", "") == State.ERASED:
            continue                      # already forgotten; not a candidate again
        names = tuple(
            n for n in (getattr(row, "transcript_name", None),
                        getattr(row, "summary_name", None),
                        getattr(row, "actions_name", None)) if n
        )
        ids = _output_ids(row)
        candidates.append(EraseCandidate(
            item_id=row.item_id,
            name=str(getattr(row, "name", "") or ""),
            route=str(getattr(row, "route", "") or ""),
            recorded_at=str(getattr(row, "created_at", "") or getattr(row, "discovered_at", "") or ""),
            state=str(getattr(row, "state", "") or ""),
            # The original is still out there unless we already recorded it gone.
            source_present=not getattr(row, "source_deleted_at", None),
            output_names=names,
            output_ids=ids,
            held_passages=0,
        ))
    return ErasePlan(candidates=tuple(candidates), asked=asked)


def _output_ids(row: Any) -> tuple[str, ...]:
    """The Graph ids of the three published files, as the ledger recorded them."""
    raw = getattr(row, "output_item_ids", None)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except ValueError:
            return ()
    if not isinstance(raw, Mapping):
        return ()
    return tuple(str(v) for v in raw.values() if v)


def erase(
    ledger: Any,
    plan_: ErasePlan,
    *,
    by: str,
    because: str,
    client: Any = None,
    held_store: Any = None,
) -> EraseResult:
    """Carry out a plan. Files first, then the row.

    THE ORDER IS THE POINT. The files go before the row is emptied, because the row is the
    only thing that knows which files to delete. Emptying it first and then failing to reach
    the drive would leave the audio and the transcripts sitting in OneDrive with nothing left
    that knows they are supposed to be gone — an erasure that removed the evidence of what it
    was meant to remove.

    So a file that cannot be deleted is REPORTED AND THE ROW IS LEFT ALONE. Re-running picks
    it up again. A half-done erasure that says so is recoverable; one that quietly tombstoned
    the row is not.
    """
    who = (by or "").strip()
    why = (because or "").strip()
    if not who or not why:
        raise ValueError("an erasure needs the name of the person who decided it and the reason")

    result = EraseResult(by=who, because=why, at=utc_now_iso())
    for candidate in plan_.candidates:
        refused: list[str] = []
        deleted = gone = 0
        for item_id in (candidate.item_id, *candidate.output_ids):
            if client is None:
                break
            try:
                if client.delete(item_id):
                    deleted += 1
                else:
                    gone += 1
            except Exception as exc:  # noqa: BLE001 - reported, never silently skipped
                refused.append(f"{candidate.item_id}: {type(exc).__name__}")
                log.warning("could not delete %s during an erasure", item_id, exc_info=True)

        if refused:
            # The row is NOT emptied. See the docstring: it is the only thing that knows
            # what is still out there.
            result.files_refused = result.files_refused + tuple(refused)
            result.files_deleted += deleted
            result.files_already_gone += gone
            continue

        if held_store is not None:
            try:
                result.held_forgotten += int(held_store.forget(candidate.item_id) or 0)
            except Exception:  # noqa: BLE001
                log.warning("could not clear held passages for %s", candidate.item_id,
                            exc_info=True)

        ledger.erase(candidate.item_id, by=who, because=why)
        result.recordings += 1
        result.rows_tombstoned += 1
        result.files_deleted += deleted
        result.files_already_gone += gone
        result.still_in_the_record = result.still_in_the_record + candidate.output_names

    return result


def columns_not_covered(ledger: Any) -> tuple[str, ...]:
    """Columns on ``items`` that hold content and are not cleared by an erasure.

    Exists so that adding a column later cannot quietly create a place a recording's details
    survive being forgotten. A test walks the real table against
    ``Ledger.CONTENT_COLUMNS`` and against this list of columns that are deliberately kept,
    and fails on anything that is in neither — which is the only way a new column gets
    thought about at the moment it is added rather than at the moment somebody asks to be
    forgotten.
    """
    kept = {
        # Identity and shape, which the tombstone is made of.
        "item_id", "state", "route", "size", "duration_s", "word_count", "truncated",
        # When things happened. Dates are not content: "a recording arrived on the 3rd and
        # was erased on the 9th" is the record, and is what was asked to be kept.
        "created_at", "modified_at", "discovered_at", "updated_at", "done_at",
        "archived_at", "source_deleted_at", "erased_at",
        # Who and why, which is the whole point.
        "erased_by", "erased_because",
        # Working state, meaningless on a terminal row and cleared by erase() anyway.
        "claimed_by", "lease_until", "attempts", "seen_count", "quarantined_at",
        # The engine that was used is a fact about this service, not about the person.
        "engine",
        # The parent folder is a folder, not a recording.
        "parent_id",
    }
    covered = set(getattr(ledger, "CONTENT_COLUMNS", ()))
    conn = ledger._conn()
    columns = [r["name"] for r in conn.execute("PRAGMA table_info(items)").fetchall()]
    return tuple(c for c in columns if c not in covered and c not in kept)
