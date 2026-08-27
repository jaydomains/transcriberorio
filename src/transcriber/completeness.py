"""The upload-completeness gate, and byte-for-byte verification after download.

Why this module exists twice over:

  1. A file that is still uploading looks like a perfectly ordinary file. Transcribing one
     yields a fragment that transcribes cleanly and is filed as a success. The gate is what
     stops that.
  2. The check has to re-``GET`` the item. ``@microsoft.graph.downloadUrl`` and the
     ``file.hashes`` facet are NOT returned by ``/delta`` on business accounts
     (ARCHITECTURE.md, verified against the live tenant), so a gate written against the
     delta payload never sees a hash and therefore never fires — silently, forever.

Every answer here is ``(ready, reason)``. The reason is a plain sentence meant to be
written to the ledger and shown in the morning digest: a file that is not being processed
must always be able to say why.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

log = logging.getLogger("transcriber.completeness")

#: How long the two reads must be apart before "the size did not change" means anything.
DEFAULT_SETTLE_SECONDS = 30.0

#: Strongest first. Business tenants generally offer only quickXorHash, which is why it is
#: implemented below rather than treated as a reason to skip verification.
HASH_PREFERENCE: tuple[str, ...] = ("sha256Hash", "sha1Hash", "quickXorHash")

_HASH_READ_CHUNK = 1024 * 1024


class _ItemSource(Protocol):
    """Just the one method this module needs from ``graph.GraphClient``.

    Structural, not an import: the gate is unit-testable against a stub that returns
    canned items, with no network and no credential.
    """

    def get_item(self, item_id: str) -> Any: ...


# --------------------------------------------------------------------------- observing


@dataclass(frozen=True)
class Observation:
    """One re-``GET`` of an item, reduced to what the gate reasons about."""

    item_id: str
    name: str
    size: int
    at: float
    is_folder: bool
    is_deleted: bool
    ctag: str
    etag: str
    hash_algorithm: str
    hash_value: str
    pending: tuple[str, ...]

    @property
    def has_hash(self) -> bool:
        return bool(self.hash_value)


def hashes_of(item: Any) -> dict[str, str]:
    """The ``file.hashes`` facet from a DriveItem, a raw Graph payload, or a plain dict."""
    if item is None:
        return {}
    hashes = getattr(item, "hashes", None)
    if isinstance(hashes, Mapping):
        return {str(k): str(v) for k, v in hashes.items() if v}
    if isinstance(item, Mapping):
        if isinstance(item.get("hashes"), Mapping):
            return {str(k): str(v) for k, v in item["hashes"].items() if v}
        facet = item.get("file")
        if isinstance(facet, Mapping) and isinstance(facet.get("hashes"), Mapping):
            return {str(k): str(v) for k, v in facet["hashes"].items() if v}
        # A bare facet, e.g. {"quickXorHash": "..."} handed straight to verify_download.
        if item and all(str(k).lower().endswith("hash") for k in item):
            return {str(k): str(v) for k, v in item.items() if v}
    return {}


def preferred_hash(hashes: Mapping[str, str]) -> tuple[str, str]:
    """Pick the strongest hash Graph actually supplied. ``("", "")`` when there is none."""
    lowered = {str(k).lower(): str(v) for k, v in hashes.items() if v}
    for name in HASH_PREFERENCE:
        value = lowered.get(name.lower())
        if value:
            return name, value
    for key, value in lowered.items():  # an algorithm we have not met before
        if value:
            return key, value
    return "", ""


def _pending_of(item: Any) -> tuple[str, ...]:
    pending = getattr(item, "pending_operations", None)
    if pending:
        return tuple(str(p) for p in pending)
    if isinstance(item, Mapping):
        out: list[str] = []
        facet = item.get("pendingOperations")
        if isinstance(facet, Mapping):
            out.extend(str(k) for k in facet)
        elif facet:
            out.append("pendingOperations")
        if item.get("pendingContentUpdate"):
            out.append("pendingContentUpdate")
        return tuple(dict.fromkeys(out))
    return ()


def observe(
    client: _ItemSource, item_id: str, *, clock: Callable[[], float] = time.monotonic
) -> Observation:
    """Re-``GET`` the item. Never call this with a delta payload — that is the whole point."""
    item = client.get_item(item_id)
    algorithm, value = preferred_hash(hashes_of(item))
    get = (lambda k, d: item.get(k, d)) if isinstance(item, Mapping) else (lambda k, d: getattr(item, k, d))
    return Observation(
        item_id=item_id,
        name=str(get("name", "")),
        size=int(get("size", 0) or 0),
        at=clock(),
        is_folder=bool(get("is_folder", False)),
        is_deleted=bool(get("is_deleted", False)),
        ctag=str(get("ctag", "") or ""),
        etag=str(get("etag", "") or ""),
        hash_algorithm=algorithm,
        hash_value=value,
        pending=_pending_of(item),
    )


# --------------------------------------------------------------------------- the gate


def evaluate(
    previous: Observation | None,
    current: Observation,
    settle_seconds: float = DEFAULT_SETTLE_SECONDS,
) -> tuple[bool, str]:
    """Judge two reads of the same item. All three conditions, or not ready.

    Complete means: size unchanged across two reads a settle interval apart, no pending
    operation, and a hash present. Any one of them missing is a reason, never a shrug.
    """
    if current.is_deleted:
        return False, f"item {current.item_id} no longer exists in the drive"
    if current.is_folder:
        return False, f"{current.name or current.item_id} is a folder, not a file"
    if current.pending:
        return False, (
            f"{current.name}: Graph still reports {', '.join(current.pending)} — "
            f"the upload is not finished"
        )
    if current.size <= 0:
        return False, f"{current.name}: size is {current.size} bytes"
    if not current.has_hash:
        return False, (
            f"{current.name}: no file.hashes value yet — OneDrive has not finished "
            f"assembling the content"
        )
    if previous is None:
        return False, (
            f"{current.name}: first read at {current.size} bytes; needs a second read "
            f"{settle_seconds:.0f}s later to prove the size is stable"
        )
    if previous.item_id != current.item_id:
        return False, (
            f"cannot compare reads of different items ({previous.item_id} vs {current.item_id})"
        )
    elapsed = current.at - previous.at
    if previous.size != current.size:
        return False, (
            f"{current.name}: size changed {previous.size} -> {current.size} bytes over "
            f"{elapsed:.1f}s — still uploading"
        )
    if previous.ctag and current.ctag and previous.ctag != current.ctag:
        return False, (
            f"{current.name}: content tag changed between reads at the same size — "
            f"the content was rewritten"
        )
    if elapsed < settle_seconds:
        return False, (
            f"{current.name}: stable at {current.size} bytes but the reads were only "
            f"{elapsed:.1f}s apart; the settle interval is {settle_seconds:.0f}s"
        )
    return True, (
        f"{current.name}: stable at {current.size} bytes across {elapsed:.1f}s, "
        f"{current.hash_algorithm} present, no pending operations"
    )


def is_upload_complete(
    client: _ItemSource,
    item_id: str,
    *,
    settle_seconds: float = DEFAULT_SETTLE_SECONDS,
    previous: Observation | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[bool, str]:
    """Has this upload finished? Two re-``GET``s, a settle interval apart.

    Pass ``previous`` (from an earlier ``observe``) to make this a single read: a poller
    that already looked at the item a minute ago does not need to hold a thread open for
    the settle interval to look again.
    """
    ready, reason = _decide(
        client, item_id, settle_seconds=settle_seconds, previous=previous, sleep=sleep, clock=clock
    )
    log.debug("completeness gate: %s is %s (%s)", item_id, "ready" if ready else "NOT ready", reason)
    return ready, reason


def _decide(
    client: _ItemSource,
    item_id: str,
    *,
    settle_seconds: float,
    previous: Observation | None,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> tuple[bool, str]:
    if previous is not None and (clock() - previous.at) >= settle_seconds:
        return evaluate(previous, observe(client, item_id, clock=clock), settle_seconds)
    first = previous or observe(client, item_id, clock=clock)
    ready, reason = evaluate(None, first, settle_seconds)
    if not ready and not _worth_a_second_look(first):
        return False, reason
    wait = max(0.0, settle_seconds - (clock() - first.at))
    if wait:
        sleep(wait)
    second = observe(client, item_id, clock=clock)
    return evaluate(first, second, settle_seconds)


def _worth_a_second_look(first: Observation) -> bool:
    """A folder or a deleted item will not become a finished upload by waiting."""
    return not first.is_folder and not first.is_deleted


# --------------------------------------------------------------------------- hashing


class QuickXorHash:
    """Microsoft's quickXorHash — the only hash OneDrive for Business offers on a file.

    Ported from the algorithm Microsoft publishes for OneDrive. It is implemented here
    rather than skipped because "no hash available" would mean downloading 90 MB of audio
    and never checking that it arrived intact, which is precisely the class of silent
    failure this service exists to remove.

    Streams: ``update`` may be called with any chunk sizes and gives the same digest.
    """

    _WIDTH_IN_BITS = 160
    _SHIFT = 11
    _BITS_IN_LAST_CELL = 32
    _MASK64 = (1 << 64) - 1

    name = "quickXorHash"
    digest_size = 20

    def __init__(self, data: bytes | None = None) -> None:
        self._data = [0, 0, 0]  # 160 bits, as 64 + 64 + 32 used bits
        self._length = 0
        self._shift = 0
        if data:
            self.update(data)

    def update(self, block: bytes) -> None:
        if not block:
            return
        cells = self._data
        last_index = len(cells) - 1
        width = self._WIDTH_IN_BITS
        size = len(block)
        vector_index = self._shift // 64
        vector_offset = self._shift % 64
        for i in range(min(size, width)):
            is_last_cell = vector_index == last_index
            bits_in_cell = self._BITS_IN_LAST_CELL if is_last_cell else 64
            # XOR of every byte at this stride, folded before shifting: XOR distributes
            # over the shift, so this is the reference algorithm one byte-column at a time.
            xored = 0
            for j in range(i, size, width):
                xored ^= block[j]
            if vector_offset <= bits_in_cell - 8:
                cells[vector_index] ^= (xored << vector_offset) & self._MASK64
            else:
                low = bits_in_cell - vector_offset
                cells[vector_index] ^= (xored << vector_offset) & self._MASK64
                cells[0 if is_last_cell else vector_index + 1] ^= xored >> low
            vector_offset += self._SHIFT
            while vector_offset >= bits_in_cell:
                vector_index = 0 if is_last_cell else vector_index + 1
                vector_offset -= bits_in_cell
        self._shift = (self._shift + self._SHIFT * (size % width)) % width
        self._length += size

    def digest(self) -> bytes:
        rgb = bytearray(self.digest_size)
        rgb[0:8] = (self._data[0] & self._MASK64).to_bytes(8, "little")
        rgb[8:16] = (self._data[1] & self._MASK64).to_bytes(8, "little")
        rgb[16:20] = (self._data[2] & 0xFFFFFFFF).to_bytes(4, "little")
        # The file length is XORed into the least significant bits, little endian.
        length_bytes = (self._length & self._MASK64).to_bytes(8, "little")
        for i, byte in enumerate(length_bytes):
            rgb[self.digest_size - 8 + i] ^= byte
        return bytes(rgb)

    def hexdigest(self) -> str:
        return self.digest().hex()

    def b64digest(self) -> str:
        """The canonical form: base64, exactly as Graph reports it in ``file.hashes``."""
        return base64.b64encode(self.digest()).decode("ascii")


def quickxorhash_file(path: str, *, chunk_size: int = _HASH_READ_CHUNK) -> str:
    hasher = QuickXorHash()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk_size), b""):
            hasher.update(block)
    return hasher.b64digest()


def hash_file(path: str, algorithm: str, *, chunk_size: int = _HASH_READ_CHUNK) -> str:
    """Hash a file in the form Graph reports it: base64 for quickXor, hex for the rest."""
    name = algorithm.lower().replace("hash", "")
    if name in ("quickxor", "quick_xor"):
        return quickxorhash_file(path, chunk_size=chunk_size)
    try:
        digest = hashlib.new(name)
    except ValueError as exc:
        raise ValueError(f"unsupported hash algorithm {algorithm!r}") from exc
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()


def _normalise(algorithm: str, value: str) -> str:
    """Compare like with like: hex is case-insensitive, base64 is not."""
    value = value.strip()
    if algorithm.lower().startswith("quickxor"):
        try:
            return base64.b64encode(base64.b64decode(value, validate=True)).decode("ascii")
        except (binascii.Error, ValueError):
            return value
    return value.lower()


def _expected_pair(expected_hash: Any, algorithm: str | None) -> tuple[str, str]:
    """Accept a hashes facet, a DriveItem, or a bare digest string."""
    if isinstance(expected_hash, str):
        value = expected_hash.strip()
        if algorithm:
            return algorithm, value
        if ":" in value:
            name, _, rest = value.partition(":")
            return name.strip(), rest.strip()
        stripped = value.rstrip("=")
        if len(value) == 64 and all(c in "0123456789abcdefABCDEF" for c in value):
            return "sha256Hash", value
        if len(value) == 40 and all(c in "0123456789abcdefABCDEF" for c in value):
            return "sha1Hash", value
        if len(stripped) == 27:
            return "quickXorHash", value
        return "", value
    hashes = hashes_of(expected_hash)
    if algorithm and hashes:
        lowered = {k.lower(): v for k, v in hashes.items()}
        value = lowered.get(algorithm.lower(), "")
        return (algorithm, value) if value else ("", "")
    return preferred_hash(hashes)


def verify_download(
    path: str, expected_hash: Any, *, algorithm: str | None = None, expected_size: int | None = None
) -> tuple[bool, str]:
    """Byte-for-byte check of a downloaded file against what Graph said it should be.

    ``expected_hash`` may be the ``file.hashes`` facet, a ``DriveItem``, or a single digest
    string. A missing hash is a failure, not a pass: a download nobody verified is a
    download nobody can vouch for.
    """
    if not os.path.exists(path):
        return False, f"{path} does not exist"
    actual_size = os.path.getsize(path)
    if expected_size is not None and actual_size != expected_size:
        return False, (
            f"{os.path.basename(path)}: {actual_size} bytes on disk but Graph reported "
            f"{expected_size}"
        )
    name, expected = _expected_pair(expected_hash, algorithm)
    if not expected:
        return False, (
            f"{os.path.basename(path)}: no hash was supplied, so the download cannot be "
            f"verified — refusing to call it good"
        )
    if not name:
        return False, (
            f"{os.path.basename(path)}: a hash was supplied but its algorithm could not be "
            f"identified, so the download cannot be verified"
        )
    try:
        actual = hash_file(path, name)
    except (ValueError, OSError) as exc:
        return False, f"{os.path.basename(path)}: could not compute {name}: {exc}"
    if _normalise(name, actual) != _normalise(name, expected):
        return False, (
            f"{os.path.basename(path)}: {name} mismatch over {actual_size} bytes — "
            f"expected {expected}, got {actual}"
        )
    return True, f"{os.path.basename(path)}: {name} matches over {actual_size} bytes"
