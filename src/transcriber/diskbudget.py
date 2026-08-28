"""How much scratch the work directory may hold, and whether there is room for one more.

The pipeline downloads every recording to ``WORK_DIR/items/<item>/`` and splits the long
ones into pieces beside the original, so one hour-long call is its own 58 MB plus the
pieces cut from it. Eight of those at once — the shape of eight members of staff recording
into eight folders — is gigabytes of a small VM's disk, and a disk that fills mid-download
does not fail politely: the download dies, the transcript never happens, and the ledger row
looks exactly like a row that is merely busy.

So the work directory gets a budget, and the budget is checked **before** a recording is
claimed rather than discovered while writing to it.

Three properties are deliberate.

**It never drops anything.** Being over budget is not an error, not a quarantine and not a
failed attempt. It is a reason to claim nothing this cycle: the rows stay claimable, the
recordings stay in OneDrive, and the queue drains as the work in progress finishes and its
directories are removed. The only thing that changes under pressure is the rate.

**Measuring is cheap.** Walking a work directory holding eighty recordings on every claim
would cost more than the claim. Each item directory's size is remembered for
:data:`DEFAULT_TTL_S` seconds and re-walked only when that has passed, and a directory that
has gone is dropped from the cache by the ``items/`` listing that finds it missing —
:meth:`DiskBudget.forget` makes that immediate when the worker knows a recording has
finished.

**A refusal says what to do about it.** A recording whose own working set is larger than
the entire budget can never start, however much finishes first. That is a configuration
fault rather than a busy afternoon, and it reads as one: the reason names the recording,
the space it needs, the limit, and the variable to raise.

**Being over budget always ends.** Those three properties, on their own, once produced a
state nothing could leave: a quarantined recording keeps its downloaded audio on purpose,
a quarantined row is finished so nothing ever removes that directory, and enough of them
put the work directory permanently over its limit with nothing running. The drain then
claimed nothing, forever, while discovery kept writing rows — a silent, permanent stop
wearing the clothes of a busy afternoon, which is the exact failure this service exists to
remove. Two things stop it. :meth:`DiskBudget.reclaim` clears scratch belonging to
recordings that are finished — done, quarantined or written off as silence — once it is
older than the keep window, so "kept for a retry" can never mean "kept until the disk is
full". And :func:`admit` takes a ``force_one`` flag: when the caller can see that nothing
at all is running and nothing has been for some time, exactly one recording is started
anyway, because a drain that only a person can restart is a drain that has stopped.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

__all__ = [
    "DiskBudget",
    "Decision",
    "Usage",
    "Admission",
    "Reclaimed",
    "admit",
    "parse_bytes",
    "format_bytes",
    "KIB",
    "MIB",
    "GIB",
    "DEFAULT_WORK_DIR_MAX_BYTES",
    "MINIMUM_WORK_DIR_MAX_BYTES",
    "DEFAULT_TTL_S",
    "DEFAULT_KEEP_FINISHED_S",
    "WORKING_COPY_FACTOR",
    "WORKING_COPY_SLACK_BYTES",
    "OK",
    "OVER_BUDGET",
    "NO_ROOM",
    "TOO_LARGE",
]

KIB = 1024
MIB = 1024 * KIB
GIB = 1024 * MIB

#: Right for one person on one machine, and comfortably more than one recording needs, so
#: nothing changes for the current deployment until somebody turns the concurrency up.
DEFAULT_WORK_DIR_MAX_BYTES = 4 * GIB

#: Below this the budget refuses recordings it should be processing. An hour-long call at
#: this recorder's measured rate is ~58 MB, and transcribing it writes the download plus the
#: pieces it is split into, so a quarter of a gigabyte is about the smallest limit under
#: which a single ordinary recording still fits.
MINIMUM_WORK_DIR_MAX_BYTES = 256 * MIB

#: How long a measured directory size is trusted. Short enough that a download in flight is
#: noticed within seconds, long enough that a cycle's worth of claims walks the tree once.
DEFAULT_TTL_S = 5.0

#: How long the scratch of a *finished* recording — done, quarantined, or written off as
#: silence — is kept before it is cleared. A failed recording keeps its audio on purpose, so
#: that a retry does not download it again and so that a person can listen to what went
#: wrong; two days is long enough for both and short enough that a bad week cannot fill the
#: work directory permanently. Scratch for a recording that is still unfinished is never
#: touched by this, however old it is.
DEFAULT_KEEP_FINISHED_S = 48 * 3600.0

#: What one recording occupies while it is being worked on, as a multiple of its own size:
#: the downloaded audio, plus the pieces a long file is split into, which together come to
#: about the original again once the overlap between them is counted.
WORKING_COPY_FACTOR = 2.2

#: Added on top, for the transcript JSON and the rounding on a small file. A 200 KB voice
#: note does not need 16 MiB, and reserving it costs nothing on any budget worth having.
WORKING_COPY_SLACK_BYTES = 16 * MIB

#: What a :class:`Decision` says happened, as a value a test can assert on rather than a
#: sentence it has to match.
OK = "ok"
OVER_BUDGET = "over-budget"
NO_ROOM = "no-room"
TOO_LARGE = "too-large"

_SIZE = re.compile(
    r"^\s*(?P<number>\d+(?:[._]\d+)*(?:\.\d+)?)\s*(?P<unit>[a-zA-Z]*)\s*$"
)

#: Both conventions, because both are written on the side of the tin: ``MB`` is a million
#: bytes and ``MiB`` is 1048576, and a bare ``G`` is read as ``GiB`` the way ``dd`` and
#: ``truncate`` read it.
_UNITS: dict[str, int] = {
    "": 1, "b": 1, "byte": 1, "bytes": 1,
    "k": KIB, "kib": KIB, "kb": 1000,
    "m": MIB, "mib": MIB, "mb": 1000 ** 2,
    "g": GIB, "gib": GIB, "gb": 1000 ** 3,
    "t": 1024 * GIB, "tib": 1024 * GIB, "tb": 1000 ** 4,
}


def parse_bytes(text: object) -> int:
    """``4GiB``, ``500MB``, ``2.5g``, ``1_000_000`` or a plain integer, as bytes.

    Raises :class:`ValueError` with what was wrong in words, because this is read by
    ``Config.from_env`` which reports every problem in the environment at once and each one
    has to stand on its own.
    """
    if isinstance(text, bool):  # bool is an int, and a boolean size is a mistake
        raise ValueError("expected a size in bytes, such as 4GiB, 500MB or 4294967296")
    if isinstance(text, int):
        value = int(text)
        if value < 0:
            raise ValueError("a size cannot be negative")
        return value
    if isinstance(text, float):
        if text < 0:
            raise ValueError("a size cannot be negative")
        return int(text)

    raw = str(text or "").strip()
    if not raw:
        return 0
    match = _SIZE.match(raw.replace(",", ""))
    if not match:
        raise ValueError(
            "expected a size in bytes, such as 4GiB, 500MB or 4294967296 "
            "(KB/MB/GB count in thousands, KiB/MiB/GiB in 1024s)"
        )
    unit = match.group("unit").lower()
    if unit not in _UNITS:
        raise ValueError(
            f"{match.group('unit')!r} is not a unit this understands — use one of: "
            "B, KB, MB, GB, TB, KiB, MiB, GiB, TiB"
        )
    number = float(match.group("number").replace("_", ""))
    if number < 0:
        raise ValueError("a size cannot be negative")
    return int(number * _UNITS[unit])


def format_bytes(value: float) -> str:
    """A size as somebody would say it: ``4.0 GiB``, ``58.2 MB`` becomes ``55.5 MiB``.

    One decimal place, binary units, because the budget is set in them and a report that
    counts in different units from the setting it is about invites arithmetic nobody
    should have to do.
    """
    number = float(value or 0)
    if number < 0:
        return "-" + format_bytes(-number)
    if number < KIB:
        return f"{int(number)} bytes" if number != 1 else "1 byte"
    for unit, scale in (("KiB", KIB), ("MiB", MIB), ("GiB", GIB)):
        if number < scale * 1024 or unit == "GiB":
            return f"{number / scale:.1f} {unit}"
    return f"{number / GIB:.1f} GiB"  # pragma: no cover - unreachable, kept honest


@dataclass(frozen=True)
class Usage:
    """What the work directory holds right now, or as near as the cache knows."""

    used_bytes: int = 0
    items: int = 0
    measured_at: float = 0.0
    #: Directories the measurement could not read. Never fatal — an unreadable directory is
    #: reported and treated as empty, because refusing to process anything on the strength
    #: of a permissions error would be a worse failure than the one it guards against.
    unreadable: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.unreadable


@dataclass(frozen=True)
class Decision:
    """Whether there is room, and — when there is not — what to say about it.

    ``kind`` is the machine-readable half (:data:`OK`, :data:`OVER_BUDGET`,
    :data:`NO_ROOM`, :data:`TOO_LARGE`) and ``reason`` is the half a person reads.
    """

    ok: bool
    kind: str = OK
    reason: str = ""
    used_bytes: int = 0
    max_bytes: int = 0
    needed_bytes: int = 0

    @property
    def free_bytes(self) -> int:
        return max(0, self.max_bytes - self.used_bytes)

    @property
    def permanent(self) -> bool:
        """True when finishing the work in progress cannot help — a person has to act."""
        return self.kind == TOO_LARGE


@dataclass
class Reclaimed:
    """What :meth:`DiskBudget.reclaim` cleared away, for a log line and for a test.

    Data rather than a printed sentence, for the same reason :class:`Admission` is: the
    worker owns what a cycle says, and "the disk freed itself up" has to be assertable.
    """

    removed: list[str] = field(default_factory=list)
    freed_bytes: int = 0
    #: Directories left alone because they are still inside the keep window.
    kept_recent: int = 0
    errors: list[str] = field(default_factory=list)
    unreadable: tuple[str, ...] = ()

    @property
    def anything(self) -> bool:
        return bool(self.removed)

    def line(self) -> str:
        if not self.removed:
            return ""
        return (
            f"cleared {format_bytes(self.freed_bytes)} of scratch from "
            f"{len(self.removed)} finished recording(s) — the audio kept for recordings "
            f"that are done, quarantined or written off as silence, which nothing is "
            f"coming back for. The recordings themselves are untouched in OneDrive."
        )


@dataclass
class _Cached:
    size: int
    at: float


class DiskBudget:
    """The work directory's size limit, and the cheap measurement behind it.

    Safe to share between threads: the pipeline runs several recordings at once and each of
    them finishes on its own thread, so the cache is guarded.
    """

    def __init__(
        self,
        work_dir: str,
        max_bytes: int = DEFAULT_WORK_DIR_MAX_BYTES,
        *,
        ttl_s: float = DEFAULT_TTL_S,
        clock: Callable[[], float] = time.monotonic,
        factor: float = WORKING_COPY_FACTOR,
        slack_bytes: int = WORKING_COPY_SLACK_BYTES,
    ) -> None:
        self.work_dir = str(work_dir or "")
        self.max_bytes = max(0, int(max_bytes or 0))
        self.ttl_s = max(0.0, float(ttl_s))
        self.clock = clock
        self.factor = max(1.0, float(factor))
        self.slack_bytes = max(0, int(slack_bytes))
        self._lock = threading.Lock()
        self._items: dict[str, _Cached] = {}
        self._loose: _Cached | None = None

    # -- state ---------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """False when ``WORK_DIR_MAX_BYTES`` is 0, which means "do not limit this"."""
        return self.max_bytes > 0 and bool(self.work_dir)

    @property
    def items_root(self) -> str:
        return os.path.join(self.work_dir, "items")

    def invalidate(self) -> None:
        """Forget every measurement. The next check walks the tree."""
        with self._lock:
            self._items.clear()
            self._loose = None

    def forget(self, item_id: str = "") -> None:
        """One recording has finished and its directory may be gone; re-measure it.

        Called by the worker as each recording ends rather than by the pipeline that
        removes the directory, so nothing in the pipeline has to know a budget exists. The
        directory name is derived the same way the pipeline derives it; if that ever drifts,
        this falls back to invalidating everything, which is slower and still correct.
        """
        name = _item_dir_name(item_id)
        with self._lock:
            if name and name in self._items:
                del self._items[name]
                return
            self._items.clear()
            self._loose = None

    # -- measuring -----------------------------------------------------------------

    def usage(self, *, refresh: bool = False) -> Usage:
        """What the work directory holds, from the cache when it is fresh enough."""
        if not self.work_dir:
            return Usage(measured_at=self.clock())
        now = self.clock()
        unreadable: list[str] = []
        total = 0
        with self._lock:
            fresh: dict[str, _Cached] = {}
            for name in _child_dirs(self.items_root, unreadable):
                cached = self._items.get(name)
                if cached is None or refresh or (now - cached.at) >= self.ttl_s:
                    size = _tree_size(os.path.join(self.items_root, name), unreadable)
                    cached = _Cached(size, now)
                fresh[name] = cached
                total += cached.size
            # Rebuilt rather than updated: a directory that has been removed is gone from
            # the listing, and so is gone from the cache, which is what stops a finished
            # recording holding budget it no longer occupies.
            self._items = fresh
            items = len(fresh)

            loose = self._loose
            if loose is None or refresh or (now - loose.at) >= self.ttl_s:
                loose = _Cached(
                    _tree_size(self.work_dir, unreadable, skip={"items"}), now
                )
            self._loose = loose
            total += loose.size

        return Usage(
            used_bytes=total,
            items=items,
            measured_at=now,
            unreadable=tuple(dict.fromkeys(unreadable)),
        )

    # -- reclaiming ----------------------------------------------------------------

    def reclaim(
        self,
        *,
        keep: Iterable[str] = (),
        older_than_s: float = DEFAULT_KEEP_FINISHED_S,
        now: float | None = None,
    ) -> "Reclaimed":
        """Clear the scratch of recordings that are finished with, and say what went.

        ``keep`` is every recording that is **not** finished — the caller's unfinished
        ledger rows plus whatever is running this second. Their directories are never
        touched, however old, because that audio is what makes the next attempt cheap and
        the ledger row is still going somewhere. Everything else under ``items/`` belongs to
        a recording that is done, quarantined, written off as silence, or has no row at all,
        and none of those will ever come back for it on their own.

        ``older_than_s`` is the grace period, measured on the wall clock from the newest
        file in the directory. It is what keeps the two purposes of kept audio intact: a
        recording that failed an hour ago still has its download for the retry, and a person
        looking into this morning's quarantine still has the file to listen to. What it
        removes is the *permanent* case — the audio of a recording nothing is coming back
        for, which without this sits in the budget until somebody deletes it by hand, and
        which is how a work directory goes over its limit and never comes back under it.
        """
        result = Reclaimed()
        if not self.work_dir:
            return result
        clock = time.time() if now is None else float(now)
        grace = max(0.0, float(older_than_s))
        protected = {name for name in (_item_dir_name(i) for i in keep) if name}
        unreadable: list[str] = []

        for name in _child_dirs(self.items_root, unreadable):
            if name in protected:
                continue
            path = os.path.join(self.items_root, name)
            size, newest = _tree_stats(path, unreadable)
            if grace and (clock - newest) < grace:
                result.kept_recent += 1
                continue
            try:
                shutil.rmtree(path)
            except OSError as exc:
                # Never fatal, and never retried into a loop: a directory that cannot be
                # removed is reported, counted as still occupying its space, and left alone.
                result.errors.append(f"{path}: {exc}")
                continue
            result.removed.append(name)
            result.freed_bytes += size

        result.unreadable = tuple(dict.fromkeys(unreadable))
        if result.removed:
            self.invalidate()
        return result

    def estimate_for(self, size_bytes: int | float | None) -> int:
        """Scratch one recording of this size needs while it is being transcribed."""
        size = max(0, int(size_bytes or 0))
        return int(size * self.factor) + self.slack_bytes

    # -- the question everything else asks -----------------------------------------

    def check(
        self,
        *,
        needed_bytes: int = 0,
        promised_bytes: int = 0,
        what: str = "",
        usage: Usage | None = None,
    ) -> Decision:
        """Is there room — for another cycle of work, or for this one recording?

        ``needed_bytes`` is what the candidate will occupy (:meth:`estimate_for` turns a
        recording's size into it); ``promised_bytes`` is what has already been admitted this
        cycle and is not on disk yet, so a cycle cannot admit eight recordings that each fit
        the free space only while the other seven do not exist. ``what`` names the recording
        in the reason.
        """
        if not self.enabled:
            return Decision(ok=True, kind=OK, needed_bytes=int(needed_bytes or 0))

        need = max(0, int(needed_bytes or 0))
        promised = max(0, int(promised_bytes or 0))
        limit = self.max_bytes
        subject = f"{what} " if what else ""

        if need > limit:
            return Decision(
                ok=False,
                kind=TOO_LARGE,
                needed_bytes=need,
                used_bytes=0,
                max_bytes=limit,
                reason=(
                    f"{subject}needs about {format_bytes(need)} of scratch space to download "
                    f"and split, which is more than the whole {format_bytes(limit)} the work "
                    f"directory is allowed to hold (WORK_DIR_MAX_BYTES). Waiting will not "
                    f"help — nothing finishing frees up more than the whole budget. The "
                    f"recording is untouched in OneDrive and stays queued: raise "
                    f"WORK_DIR_MAX_BYTES past {format_bytes(need)} and it is picked up on "
                    f"the next cycle."
                ),
            )

        measured = self.usage() if usage is None else usage
        used = measured.used_bytes + promised
        free = max(0, limit - used)

        # Genuinely over, measured on the disk — not merely over once this cycle's own
        # reservations are counted, which is a different sentence and lands below.
        if measured.used_bytes >= limit:
            return Decision(
                ok=False,
                kind=OVER_BUDGET,
                needed_bytes=need,
                used_bytes=used,
                max_bytes=limit,
                reason=(
                    f"the work directory holds {format_bytes(used)}, which is at or over the "
                    f"{format_bytes(limit)} it is allowed (WORK_DIR_MAX_BYTES), so no new "
                    f"recording is being started. Nothing has been dropped: the queue is "
                    f"where it was and it starts moving again as the recordings in progress "
                    f"finish and their files are cleared away. If it stays full with nothing "
                    f"running, what is in there is the audio kept for recordings that failed "
                    f"or were quarantined — kept on purpose, so that a retry does not "
                    f"download them again."
                ),
            )

        if need and need > free:
            return Decision(
                ok=False,
                kind=NO_ROOM,
                needed_bytes=need,
                used_bytes=used,
                max_bytes=limit,
                reason=(
                    f"{subject}needs about {format_bytes(need)} of scratch space and only "
                    f"{format_bytes(free)} of the {format_bytes(limit)} work-directory budget "
                    f"is free, so it waits for the recordings already in progress to finish. "
                    f"It is still queued and nothing has been dropped."
                ),
            )

        return Decision(
            ok=True, kind=OK, needed_bytes=need, used_bytes=used, max_bytes=limit,
        )

    def note(self, usage: Usage | None = None) -> str:
        """One plain line about the work directory, for ``status`` and the digest."""
        if not self.enabled:
            return (
                "The work directory has no size limit (WORK_DIR_MAX_BYTES is 0), so nothing "
                "stops a burst of long recordings filling the disk."
            )
        measured = self.usage() if usage is None else usage
        head = (
            f"The work directory holds {format_bytes(measured.used_bytes)} of its "
            f"{format_bytes(self.max_bytes)} budget"
        )
        if measured.items:
            head += f", holding files for {measured.items} recording(s)"
        if measured.used_bytes >= self.max_bytes:
            head += " — no new recording is being started until some of that clears"
        if measured.unreadable:
            head += (
                f" (part of it could not be read: {measured.unreadable[0]}, so the real "
                f"figure may be higher)"
            )
        return head + "."


# --------------------------------------------------------------------------- walking


_UNSAFE_PATH = re.compile(r"[^A-Za-z0-9._-]+")


def _item_dir_name(item_id: str) -> str:
    """The directory ``pipeline._item_dir`` gives this recording, name only.

    Duplicated deliberately rather than imported: this module is read by ``config``, and a
    budget that could not be built without the pipeline would drag the whole pipeline into
    startup. A drift between the two costs a cache miss, never a wrong answer — see
    :meth:`DiskBudget.forget`.
    """
    cleaned = _UNSAFE_PATH.sub("_", os.path.basename(str(item_id or "")).strip())[:180]
    return cleaned or ""


def _child_dirs(path: str, unreadable: list[str]) -> list[str]:
    """Immediate subdirectory names, sorted, or nothing at all if there is no such tree."""
    try:
        with os.scandir(path) as entries:
            return sorted(
                entry.name for entry in entries if entry.is_dir(follow_symlinks=False)
            )
    except FileNotFoundError:
        return []
    except OSError:
        unreadable.append(path)
        return []


def _tree_size(path: str, unreadable: list[str], skip: Iterable[str] = ()) -> int:
    """Bytes under ``path``, symlinks counted but never followed.

    An explicit stack rather than ``os.walk`` so one unreadable directory costs its own
    subtree and nothing else, and so a symlink loop cannot turn a measurement into a hang.
    """
    skipped = set(skip)
    total = 0
    stack = [(path, True)]
    while stack:
        current, top = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if top and entry.name in skipped:
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append((entry.path, False))
                            continue
                        total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        unreadable.append(entry.path)
        except FileNotFoundError:
            continue
        except OSError:
            unreadable.append(current)
    return total


def _tree_stats(path: str, unreadable: list[str]) -> tuple[int, float]:
    """``(bytes, newest modification time)`` under ``path``, on the same walk.

    The age is the newest file in the tree rather than the directory's own timestamp: a
    download writes into a directory that was created hours earlier, and reading the
    directory's own mtime would call a recording that is being worked on right now old.
    """
    total = 0
    newest = 0.0
    try:
        newest = os.stat(path, follow_symlinks=False).st_mtime
    except OSError:
        unreadable.append(path)
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                            stat = entry.stat(follow_symlinks=False)
                            newest = max(newest, stat.st_mtime)
                            continue
                        stat = entry.stat(follow_symlinks=False)
                        total += stat.st_size
                        newest = max(newest, stat.st_mtime)
                    except OSError:
                        unreadable.append(entry.path)
        except FileNotFoundError:
            continue
        except OSError:
            unreadable.append(current)
    return total, newest


@dataclass
class Admission:
    """The outcome of offering a cycle's candidates to the budget, in order.

    Kept as data rather than printed here: the worker owns what a cycle reports, and this
    has to be assertable in a test without reading a log line.
    """

    admitted: list[str] = field(default_factory=list)
    held: list[str] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)
    promised_bytes: int = 0
    #: The whole-directory decision this admission started from, and the measurement behind
    #: it, so a caller can report the state of the work directory without measuring twice.
    decision: Decision | None = None
    usage: Usage | None = None
    #: Why the scan stopped, when it stopped for want of space rather than for want of
    #: candidates. Empty when everything offered was admitted.
    held_reason: str = ""
    #: True when the budget said no and one recording was started regardless, because
    #: nothing was running and nothing had been for some time. Always reported: it is the
    #: service telling somebody that the work directory needs looking at.
    forced: bool = False


def admit(
    budget: DiskBudget,
    candidates: Sequence[tuple[str, int, str]],
    *,
    force_one: bool = False,
) -> Admission:
    """Take candidates ``(key, size_bytes, what)`` in order while the budget allows it.

    The order given is the order kept — the caller has already decided what is fair. A
    candidate too large for the budget on its own is refused and the scan continues, because
    one oversized recording must not stop the seven ordinary ones behind it; a candidate
    that merely does not fit right now stops the scan, and everything from there on is held
    for the next cycle rather than started and starved of disk.

    ``force_one`` is the caller saying "nothing is running, nothing has been for some time,
    and waiting is no longer a plan". Being over budget is normally a reason to wait,
    because the work in progress finishes and frees its space; when there is no work in
    progress there is nothing to wait *for*, and claiming nothing forever is a stopped
    service. So exactly one candidate is admitted — one, not the queue — and the admission
    says it was forced, so the caller can report it as something to look at rather than as
    an ordinary busy afternoon. A candidate too large for the whole budget is never the one:
    that one genuinely cannot run at any time.
    """
    result = Admission()
    if not budget.enabled:
        result.admitted = [key for key, _size, _what in candidates]
        return result

    usage = budget.usage()
    result.usage = usage
    result.decision = budget.check(usage=usage)
    if not result.decision.ok:
        result.held = [key for key, _size, _what in candidates]
        result.held_reason = result.decision.reason
        if force_one:
            _force_one(budget, candidates, result)
        return result

    index = 0
    for index, (key, size, what) in enumerate(candidates):
        need = budget.estimate_for(size)
        decision = budget.check(
            needed_bytes=need, promised_bytes=result.promised_bytes, what=what, usage=usage
        )
        if decision.ok:
            result.admitted.append(key)
            result.promised_bytes += need
            continue
        if decision.kind == TOO_LARGE:
            result.refused.append((key, decision.reason))
            continue
        result.held = [k for k, _s, _w in candidates[index:]]
        result.held_reason = decision.reason
        if force_one and not result.admitted:
            # Room for none of them and nothing running to make room: the same standstill
            # as above, reached by arithmetic rather than by the whole-directory check.
            _force_one(budget, candidates[index:], result)
        return result
    return result


def _force_one(
    budget: DiskBudget, candidates: Sequence[tuple[str, int, str]], result: Admission
) -> None:
    """Start exactly one of these anyway, and say which and why. Nothing else changes."""
    refused = {key for key, _reason in result.refused}
    for key, size, _what in candidates:
        if key in refused:
            continue
        need = budget.estimate_for(size)
        if need > budget.max_bytes:
            continue  # it cannot run at any time; forcing it would only fill the disk
        result.admitted.append(key)
        result.promised_bytes += need
        result.held = [k for k in result.held if k != key]
        result.forced = True
        result.held_reason = (
            f"the work directory is over its {format_bytes(budget.max_bytes)} budget "
            f"(WORK_DIR_MAX_BYTES) and nothing is running, so nothing is going to free it "
            f"up on its own — one recording has been started anyway rather than leaving the "
            f"queue stopped. What is filling it is scratch left by recordings that failed "
            f"or were quarantined; it clears itself after two days, and `transcriber status` "
            f"names what is in there. Nothing has been dropped."
        )
        return
