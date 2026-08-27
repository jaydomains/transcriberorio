"""Is the audio itself intact?

This is the check nobody had. A recording cut off by a dying battery uploads perfectly,
matches its Graph hash byte for byte, transcribes as an eight-second fragment and is filed
as a success — invisible forever. Nothing upstream of this module can see it: the bytes
that arrived are exactly the bytes that were written, and they are wrong anyway.

``ffprobe`` is used when it is on the PATH, but it is treated as a bonus and never as a
dependency: it is not installed here and may not be installed in production, so the
pure-Python container walk below is the primary path and has to be genuinely correct.

Three containers are walked properly — MP4/M4A (his phone records ``.m4a``), MP3 and WAV.
Anything else returns ``truncated=False`` with the reason "container not understood", said
plainly, because falsely quarantining an unfamiliar format is its own kind of damage. What
this module must never do is return ``truncated=False`` for a file it *did* understand and
found broken, and it must never let ffprobe talk it back down from a truncation the walk
proved.

What the walk cannot see, stated plainly rather than left for someone to discover: when an
``mdat`` is declared "to end of file" and the index sits in front of it, the container
carries no media length to check against, so only a gross shortfall is caught (the
implied-bitrate floor below). That file is why ``plausibility.py`` exists as a second net —
a short transcript against a long declared duration catches what the bytes will not admit.

The fixture builders at the bottom exist so the detector can be proved to fire: a test can
construct a deliberately truncated MP4 in memory, with no binary checked into the tree and
no ffmpeg anywhere near the test run.
"""

from __future__ import annotations

import json
import mmap
import os
import shutil
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from .models import AudioInfo

__all__ = [
    "AudioInfo",
    "probe",
    "probe_bytes",
    "sniff_container",
    "duration_is_known",
    "ffprobe_available",
    "FFPROBE_TIMEOUT_S",
    "build_mp4_bytes",
    "truncated_mp4_bytes",
    "mdat_overrun_mp4_bytes",
    "build_wav_bytes",
    "truncated_wav_bytes",
    "build_mp3_bytes",
    "truncated_mp3_bytes",
]

#: ffprobe is asked once per file and never allowed to hang the worker: a probe that blocks
#: forever is indistinguishable from a service that has stopped, which is the failure class
#: this whole service exists to remove.
FFPROBE_TIMEOUT_S = 30.0

#: If ffprobe decodes dramatically less audio than the container's own index declares, the
#: media data is short even though the index is intact. Both limbs must hold — a ratio on
#: its own would fire on ordinary rounding, and a flat number on its own would fire on every
#: short clip.
FFPROBE_SHORTFALL_RATIO = 0.5
FFPROBE_SHORTFALL_MIN_S = 10.0

#: Strings ffmpeg prints when the *file* is broken rather than merely unfamiliar. Anything
#: else from ffprobe is reported but not treated as proof of truncation.
_FFPROBE_CORRUPTION_MARKERS = (
    "moov atom not found",
    "invalid data found when processing input",
    "could not find codec parameters",
    "truncat",
    "partial file",
)

#: Media bytes per second of declared duration, below which the audio cannot actually be
#: there. The lowest-bitrate speech codecs in real use sit around 5 kbit/s; 1 kbit/s is far
#: under anything a recorder emits, so this fires only on a file whose index promises far
#: more audio than the file could possibly hold — the one truncation an ``mdat`` declared
#: "to end of file" can otherwise hide.
MIN_IMPLIED_BITRATE_BPS = 1000

_MAX_BOX_DEPTH = 6          # moov > trak > mdia > minf > stbl is the deepest we ever go
_MP3_RESYNC_WINDOW = 8192   # bytes of garbage a real-world MP3 may carry between frames
_MP3_XING_TOLERANCE = 0.98  # a Xing frame count is authoritative to within a frame or two

_READ_CHUNK = 1 << 20


# --------------------------------------------------------------------------- buffers


@contextmanager
def _mapped(path: str) -> Iterator[Any]:
    """Read-only view of a file that does not pull 90 MB of audio into memory."""
    with open(path, "rb") as handle:
        try:
            view = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
        except (ValueError, OSError):
            yield handle.read()      # empty file, or a filesystem that will not map
            return
        try:
            yield view
        finally:
            view.close()


def _u32be(data: Any, off: int) -> int:
    return int.from_bytes(data[off:off + 4], "big")


def _u64be(data: Any, off: int) -> int:
    return int.from_bytes(data[off:off + 8], "big")


def _u16le(data: Any, off: int) -> int:
    return int.from_bytes(data[off:off + 2], "little")


def _u32le(data: Any, off: int) -> int:
    return int.from_bytes(data[off:off + 4], "little")


def _u64le(data: Any, off: int) -> int:
    return int.from_bytes(data[off:off + 8], "little")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


# --------------------------------------------------------------------------- container sniffing


_MP4_BRANDS = {
    b"M4A ": "m4a",
    b"M4B ": "m4a",
    b"M4P ": "m4a",
    b"3gp4": "3gp",
    b"3gp5": "3gp",
    b"3g2a": "3gp",
    b"qt  ": "mov",
}


def sniff_container(data: Any, size: int, name: str = "") -> str:
    """Name the container from its magic bytes, falling back to the extension.

    Magic first, extension second: a file called ``.m4a`` that a recorder actually wrote as
    3GP or WAV is common enough, and trusting the name is how a walker ends up "proving"
    a perfectly good file is broken.
    """
    if size == 0:
        return "empty"
    head = bytes(data[: min(size, 16)])
    if size >= 12 and head[4:8] == b"ftyp":
        return _MP4_BRANDS.get(bytes(data[8:12]), "mp4")
    if head[:4] == b"RIFF" and size >= 12 and bytes(data[8:12]) == b"WAVE":
        return "wav"
    if head[:4] == b"RF64":
        return "wav"
    if head[:3] == b"ID3":
        return "mp3"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        # Both MPEG audio and AAC/ADTS start with a sync run of ones. ADTS always carries
        # layer bits of 00; MPEG audio never does.
        return "aac" if (head[1] & 0x06) == 0 else "mp3"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"fLaC":
        return "flac"
    if head[:5] == b"#!AMR":
        return "amr"
    if head[:4] == b"caff":
        return "caf"
    if head[:4] == b"\x30\x26\xb2\x75":
        return "asf"
    extension = os.path.splitext(name)[1].lower().lstrip(".")
    return f"unknown/{extension}" if extension else "unknown"


def duration_is_known(info: AudioInfo) -> bool:
    """``duration_s == 0`` means two different things; this is which.

    A recording of genuinely zero length and a container that would not say how long it is
    are not the same fact, and the plausibility gate must not treat them the same way.
    """
    return bool(info.detail.get("duration_known"))


# --------------------------------------------------------------------------- MP4 / M4A


@dataclass(frozen=True)
class _Box:
    type: str
    start: int
    header: int
    size: int           # total declared size, header included
    to_eof: bool        # declared as 0, i.e. "runs to the end of the file"

    @property
    def end(self) -> int:
        return self.start + self.size

    @property
    def payload_start(self) -> int:
        return self.start + self.header

    @property
    def payload_size(self) -> int:
        return self.size - self.header


def _box_type_ok(raw: bytes) -> bool:
    # Printable ASCII, plus 0xA9 for the ©-prefixed metadata atoms inside udta/ilst.
    return len(raw) == 4 and all(0x20 <= b <= 0x7E or b == 0xA9 for b in raw)


def _scan_boxes(data: Any, start: int, end: int) -> tuple[list[_Box], str]:
    """Walk one level of the atom tree. Returns the boxes and an error, "" when clean.

    A box that overruns ``end`` is returned rather than rejected — the caller says something
    specific about it, because "the mdat claims 4 MB more than the file holds" is a useful
    sentence for a person and "unparseable" is not.
    """
    boxes: list[_Box] = []
    offset = start
    while offset < end:
        if offset + 8 > end:
            return boxes, (
                f"{_plural(end - offset, 'byte')} at offset {offset} are too few to hold a "
                f"box header"
            )
        declared = _u32be(data, offset)
        raw_type = bytes(data[offset + 4:offset + 8])
        if not _box_type_ok(raw_type):
            return boxes, f"the four bytes at offset {offset} are not a box type ({raw_type!r})"
        box_type = raw_type.decode("latin-1")
        header, to_eof, size = 8, False, declared
        if declared == 1:
            if offset + 16 > end:
                return boxes, f"the 64-bit size of the {box_type} box at offset {offset} is cut off"
            size = _u64be(data, offset + 8)
            header = 16
            if size < 16:
                return boxes, (
                    f"the {box_type} box at offset {offset} declares a 64-bit size of {size} bytes, "
                    f"fewer than its own header"
                )
        elif declared == 0:
            size, to_eof = end - offset, True
        elif declared < 8:
            return boxes, (
                f"the {box_type} box at offset {offset} declares {declared} bytes, fewer than a "
                f"box header"
            )
        box = _Box(box_type, offset, header, size, to_eof)
        boxes.append(box)
        if box.end > end:
            return boxes, ""     # the overrun itself is the caller's story to tell
        if box.end <= offset:
            return boxes, f"the {box_type} box at offset {offset} does not advance"
        offset = box.end
    return boxes, ""


_MP4_CONTAINER_BOXES = frozenset({"moov", "trak", "mdia", "minf", "stbl", "edts", "udta"})


def _collect_boxes(data: Any, box: _Box, depth: int, out: dict[str, list[_Box]]) -> str:
    """Recurse into the container boxes we need, indexing children by type."""
    if depth > _MAX_BOX_DEPTH:
        return ""
    children, error = _scan_boxes(data, box.payload_start, box.end)
    if error:
        return f"inside the {box.type} box, {error}"
    for child in children:
        out.setdefault(child.type, []).append(child)
        if child.type in _MP4_CONTAINER_BOXES:
            nested = _collect_boxes(data, child, depth + 1, out)
            if nested:
                return nested
    return ""


def _mvhd_duration(data: Any, box: _Box) -> tuple[float | None, dict[str, Any]]:
    """Duration from the movie header, both box layouts.

    Version 0 stores 32-bit times, version 1 stores 64-bit ones, and the timescale sits at a
    different offset in each. Reading version 1 with version 0's offsets yields a duration
    that is wrong by orders of magnitude rather than obviously broken, which is why both
    layouts are spelled out here.
    """
    if box.payload_size < 4:
        return None, {"mvhd": "shorter than its own version field"}
    version = data[box.payload_start]
    base = box.payload_start + 4
    if version == 1:
        if box.payload_size < 4 + 28:
            return None, {"mvhd": "version 1 header is cut off"}
        timescale = _u32be(data, base + 16)
        duration = _u64be(data, base + 20)
        unknown = duration == 0xFFFFFFFFFFFFFFFF
    else:
        if box.payload_size < 4 + 16:
            return None, {"mvhd": "version 0 header is cut off"}
        timescale = _u32be(data, base + 8)
        duration = _u32be(data, base + 12)
        unknown = duration == 0xFFFFFFFF
    detail = {"mvhd_version": int(version), "mvhd_timescale": timescale, "mvhd_duration": duration}
    if timescale <= 0 or unknown or duration <= 0:
        return None, detail
    return duration / timescale, detail


def _mdhd_duration(data: Any, box: _Box) -> float | None:
    """A track's own duration, used when the movie header declines to say (fragmented MP4)."""
    if box.payload_size < 4:
        return None
    version = data[box.payload_start]
    base = box.payload_start + 4
    if version == 1:
        if box.payload_size < 4 + 28:
            return None
        timescale, duration = _u32be(data, base + 16), _u64be(data, base + 20)
        unknown = duration == 0xFFFFFFFFFFFFFFFF
    else:
        if box.payload_size < 4 + 16:
            return None
        timescale, duration = _u32be(data, base + 8), _u32be(data, base + 12)
        unknown = duration == 0xFFFFFFFF
    if timescale <= 0 or unknown or duration <= 0:
        return None
    return duration / timescale


def _walk_mp4(data: Any, size: int, container: str) -> AudioInfo:
    detail: dict[str, Any] = {"container_family": "iso-bmff"}
    top, error = _scan_boxes(data, 0, size)
    detail["top_level_boxes"] = [b.type for b in top]

    if not top:
        return _broken(container, size, f"no MP4 boxes could be read at all: {error or 'the file is empty'}", detail)

    overruns = [b for b in top if b.end > size and not b.to_eof]
    by_type: dict[str, list[_Box]] = {}
    for box in top:
        by_type.setdefault(box.type, []).append(box)

    faults: list[str] = []

    # 1. mdat: does the declared media length fit inside the file?
    mdats = by_type.get("mdat", [])
    detail["mdat_count"] = len(mdats)
    for box in mdats:
        if box.to_eof:
            detail["mdat_to_eof"] = True
            detail["mdat_bytes"] = box.size - box.header
            continue
        detail["mdat_bytes"] = box.size - box.header
        if box.end > size:
            faults.append(
                f"the mdat box at offset {box.start} declares {box.size} bytes but the file ends "
                f"{box.end - size} bytes short of that — the audio data is incomplete"
            )

    # 2. any other box that runs off the end of the file
    for box in overruns:
        if box.type == "mdat":
            continue
        faults.append(
            f"the {box.type} box at offset {box.start} declares {box.size} bytes but only "
            f"{size - box.start} are present — the file was cut off mid-box"
        )

    # 3. moov: present, whole, and parseable
    moovs = by_type.get("moov", [])
    duration: float | None = None
    if not moovs:
        # This is the dying-battery signature. His phone writes the index last, so a
        # recording that ended when the handset died has every byte of audio it managed and
        # no index at all — and it is exactly the file that transcribes as a fragment.
        detail["moov"] = "absent"
        faults.append(
            "there is no moov index in the file — the recorder never finished writing it, "
            "which is what a recording interrupted mid-capture looks like"
        )
    else:
        moov = moovs[-1]
        detail["moov_offset"] = moov.start
        detail["moov_bytes"] = moov.size
        detail["faststart"] = bool(mdats) and moov.start < mdats[0].start
        if moov.end > size:
            faults.append(
                f"the moov index declares {moov.size} bytes but only {size - moov.start} are "
                f"present — the file was cut off while the index was being written"
            )
        else:
            children: dict[str, list[_Box]] = {}
            nested_error = _collect_boxes(data, moov, 1, children)
            if nested_error:
                faults.append(f"the moov index is present but cannot be parsed: {nested_error}")
            else:
                detail["moov_children"] = sorted(children)
                detail["track_count"] = len(children.get("trak", []))
                if "mvhd" not in children:
                    faults.append("the moov index has no mvhd movie header, so it is incomplete")
                else:
                    duration, mvhd_detail = _mvhd_duration(data, children["mvhd"][0])
                    detail.update(mvhd_detail)
                if duration is None:
                    track_durations = [
                        d for d in (_mdhd_duration(data, b) for b in children.get("mdhd", [])) if d
                    ]
                    if track_durations:
                        duration = max(track_durations)
                        detail["duration_from"] = "mdhd"
                    elif children.get("mvex") or by_type.get("moof"):
                        detail["fragmented"] = True
                else:
                    detail["duration_from"] = "mvhd"

    if not by_type.get("mdat") and not by_type.get("moof"):
        faults.append("there is no mdat box, so the file contains no audio data")

    # Last resort, and the only check that catches an intact index sitting over an mdat
    # declared "to end of file": arithmetic. A declared duration the media data cannot
    # possibly hold means the media data is not all there.
    media_bytes = detail.get("mdat_bytes")
    if not faults and duration and media_bytes:
        implied = media_bytes * 8 / duration
        detail["implied_bitrate"] = int(implied)
        if implied < MIN_IMPLIED_BITRATE_BPS:
            faults.append(
                f"the index declares {duration:.1f}s of audio but the mdat holds only "
                f"{media_bytes} bytes — {implied:.0f} bits per second, far less than any "
                f"recording of that length could occupy"
            )

    detail["duration_known"] = duration is not None
    if faults:
        return AudioInfo(
            duration_s=duration or 0.0,
            container=container,
            truncated=True,
            reason=f"{container}: " + "; ".join(faults),
            size_bytes=size,
            probed_by="walk",
            detail=detail,
        )
    if duration is None:
        # Structurally whole but it will not say how long it is. Not truncated — but the
        # plausibility gate must know it cannot measure this one.
        return AudioInfo(
            duration_s=0.0,
            container=container,
            truncated=False,
            reason=(
                f"{container}: the container is complete (moov index present, mdat within the "
                f"file) but declares no duration"
                + (", because it is fragmented" if detail.get("fragmented") else "")
            ),
            size_bytes=size,
            probed_by="walk",
            detail=detail,
        )
    return AudioInfo(
        duration_s=duration,
        container=container,
        truncated=False,
        reason=(
            f"{container}: complete container — moov index present and parseable, mdat within "
            f"the file, {duration:.1f}s declared"
        ),
        size_bytes=size,
        probed_by="walk",
        detail=detail,
    )


# --------------------------------------------------------------------------- MP3


_MP3_VERSIONS = {0b00: "2.5", 0b01: None, 0b10: "2", 0b11: "1"}
_MP3_LAYERS = {0b00: None, 0b01: 3, 0b10: 2, 0b11: 1}

# kbit/s by bitrate index. Index 0 is "free format" and index 15 is invalid; both are
# stored as 0 here and rejected at the point of use.
_MP3_BITRATES: dict[tuple[str, int], tuple[int, ...]] = {
    ("1", 1): (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448, 0),
    ("1", 2): (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, 0),
    ("1", 3): (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0),
    ("2", 1): (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256, 0),
    ("2", 2): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0),
    ("2", 3): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0),
}
_MP3_SAMPLE_RATES = {
    "1": (44100, 48000, 32000),
    "2": (22050, 24000, 16000),
    "2.5": (11025, 12000, 8000),
}
_MP3_TRAILERS = (b"TAG", b"APETAGEX", b"LYRICSBEGIN", b"ID3")


@dataclass(frozen=True)
class _Mp3Frame:
    version: str
    layer: int
    bitrate: int          # bits per second
    sample_rate: int
    length: int           # bytes, padding included
    samples: int
    channels: int
    protected: bool

    @property
    def seconds(self) -> float:
        return self.samples / self.sample_rate


def _mp3_frame_at(data: Any, offset: int, size: int) -> _Mp3Frame | None:
    """Decode one frame header, or None if these four bytes are not one."""
    if offset + 4 > size:
        return None
    b0, b1, b2, b3 = data[offset], data[offset + 1], data[offset + 2], data[offset + 3]
    if b0 != 0xFF or (b1 & 0xE0) != 0xE0:
        return None
    version = _MP3_VERSIONS[(b1 >> 3) & 0b11]
    layer = _MP3_LAYERS[(b1 >> 1) & 0b11]
    if version is None or layer is None:
        return None
    group = "1" if version == "1" else "2"
    bitrate_index, rate_index = (b2 >> 4) & 0x0F, (b2 >> 2) & 0b11
    if rate_index == 0b11:
        return None
    bitrate_kbps = _MP3_BITRATES[(group, layer)][bitrate_index]
    if bitrate_kbps == 0:
        return None      # free format or the invalid index: no length can be computed
    sample_rate = _MP3_SAMPLE_RATES[version][rate_index]
    padding = (b2 >> 1) & 1
    bitrate = bitrate_kbps * 1000
    if layer == 1:
        samples = 384
        length = (12 * bitrate // sample_rate + padding) * 4
    elif layer == 2:
        samples = 1152
        length = 144 * bitrate // sample_rate + padding
    else:
        samples = 1152 if version == "1" else 576
        length = (144 if version == "1" else 72) * bitrate // sample_rate + padding
    if length <= 4:
        return None
    channels = 1 if ((b3 >> 6) & 0b11) == 0b11 else 2
    return _Mp3Frame(version, layer, bitrate, sample_rate, length, samples, channels, not (b1 & 1))


def _id3v2_length(data: Any, size: int) -> int:
    """Bytes of ID3v2 tag at the front, 0 if there is none."""
    if size < 10 or bytes(data[:3]) != b"ID3":
        return 0
    flags = data[5]
    body = 0
    for i in range(6, 10):
        byte = data[i]
        if byte & 0x80:      # not syncsafe; the tag is malformed, do not trust its length
            return 0
        body = (body << 7) | byte
    return 10 + body + (10 if flags & 0x10 else 0)


def _mp3_declared_frames(data: Any, offset: int, frame: _Mp3Frame, size: int) -> int:
    """Frame count from a Xing/Info or VBRI header, 0 when absent.

    Worth the trouble: a Xing count written when the file was created, compared against the
    frames actually present, catches a truncation that nothing else in an MP3 reveals.
    """
    if frame.version == "1":
        side_info = 32 if frame.channels == 2 else 17
    else:
        side_info = 17 if frame.channels == 2 else 9
    for candidate, flag_driven in ((offset + 4 + side_info, True), (offset + 4 + 32, False)):
        if candidate + 12 > size:
            continue
        tag = bytes(data[candidate:candidate + 4])
        if flag_driven and tag in (b"Xing", b"Info"):
            flags = _u32be(data, candidate + 4)
            if flags & 0x0001 and candidate + 12 <= size:
                return _u32be(data, candidate + 8)
            return 0
        if not flag_driven and tag == b"VBRI" and candidate + 20 <= size:
            return _u32be(data, candidate + 14)
    return 0


def _mp3_first_frame(data: Any, start: int, size: int) -> int:
    """Offset of the first frame whose successor is also a frame.

    One valid-looking header proves nothing — 0xFF bytes occur in album art and in noise.
    Two in a row, exactly one frame length apart, is a stream.
    """
    offset = start
    limit = min(size, start + _MP3_RESYNC_WINDOW * 8)
    while offset + 4 <= limit:
        frame = _mp3_frame_at(data, offset, size)
        if frame is not None:
            following = offset + frame.length
            if following + 4 > size or _mp3_frame_at(data, following, size) is not None:
                return offset
        offset += 1
    return -1


def _walk_mp3(data: Any, size: int, container: str) -> AudioInfo:
    detail: dict[str, Any] = {}
    tag_bytes = _id3v2_length(data, size)
    detail["id3v2_bytes"] = tag_bytes
    if tag_bytes >= size:
        return _broken(
            container, size,
            f"the ID3 tag declares {tag_bytes} bytes but the file is only {size} — there is no "
            f"audio after the tag",
            detail,
        )

    first = _mp3_first_frame(data, tag_bytes, size)
    if first < 0:
        return _broken(
            container, size,
            "no MP3 frame header could be found — the file is not a readable MP3 stream",
            detail,
        )
    detail["first_frame_offset"] = first

    frame = _mp3_frame_at(data, first, size)
    if frame is None:      # unreachable via _mp3_first_frame, and not asserted: an assert
        return _broken(   # is removed under -O and this must never become an AttributeError
            container, size, f"the frame header at offset {first} could not be re-read", detail
        )
    declared_frames = _mp3_declared_frames(data, first, frame, size)
    detail.update(
        sample_rate=frame.sample_rate,
        channels=frame.channels,
        mpeg_version=frame.version,
        layer=frame.layer,
    )

    offset, frames, seconds, skipped = first, 0, 0.0, 0
    bitrates: set[int] = set()
    cut_short = ""
    while offset + 4 <= size:
        current = _mp3_frame_at(data, offset, size)
        if current is None:
            resync = _mp3_first_frame(data, offset, min(size, offset + _MP3_RESYNC_WINDOW))
            if resync < 0:
                break
            skipped += resync - offset
            offset = resync
            continue
        if offset + current.length > size:
            cut_short = (
                f"the final frame at offset {offset} needs {current.length} bytes and only "
                f"{size - offset} are present — the file stops in the middle of a frame"
            )
            break
        frames += 1
        seconds += current.seconds
        bitrates.add(current.bitrate)
        offset += current.length

    detail.update(frames=frames, bytes_skipped=skipped, variable_bitrate=len(bitrates) > 1)
    if bitrates:
        detail["bitrate"] = max(bitrates) if len(bitrates) == 1 else int(sum(bitrates) / len(bitrates))
    if declared_frames:
        detail["declared_frames"] = declared_frames

    faults: list[str] = []
    if cut_short:
        faults.append(cut_short)
    if frames == 0:
        faults.append("no complete MP3 frame is present")
    if declared_frames and frames < declared_frames * _MP3_XING_TOLERANCE:
        missing = declared_frames - frames
        faults.append(
            f"the file's own Xing header says it holds {declared_frames} frames and only "
            f"{frames} are present — {missing} frames "
            f"({missing * frame.samples / frame.sample_rate:.1f}s) are missing from the end"
        )

    trailing = size - offset
    if trailing > 0 and not cut_short:
        tail = bytes(data[offset:offset + 16])
        if any(tail.startswith(marker) for marker in _MP3_TRAILERS):
            detail["trailer"] = tail[:8].decode("latin-1", "replace")
        elif tail[:1] == b"\xff":
            faults.append(
                f"{_plural(trailing, 'byte')} at the end begin a frame header that is not "
                f"followed by a frame — the stream is cut off"
            )
        else:
            detail["trailing_bytes"] = trailing

    detail["duration_known"] = frames > 0
    reason_head = (
        f"{container}: {frames} frames, {frame.sample_rate} Hz, "
        f"{'VBR' if len(bitrates) > 1 else f'{frame.bitrate // 1000} kbps'}"
    )
    if faults:
        return AudioInfo(
            duration_s=seconds,
            container=container,
            truncated=True,
            reason=f"{reason_head}; " + "; ".join(faults),
            size_bytes=size,
            probed_by="walk",
            bitrate=detail.get("bitrate"),
            detail=detail,
        )
    return AudioInfo(
        duration_s=seconds,
        container=container,
        truncated=False,
        reason=f"{reason_head}, {seconds:.1f}s, every frame complete",
        size_bytes=size,
        probed_by="walk",
        bitrate=detail.get("bitrate"),
        detail=detail,
    )


# --------------------------------------------------------------------------- WAV


def _walk_wav(data: Any, size: int, container: str) -> AudioInfo:
    detail: dict[str, Any] = {}
    if size < 12:
        return _broken(container, size, f"only {size} bytes — too short to be a RIFF header", detail)

    magic = bytes(data[:4])
    detail["riff_form"] = magic.decode("latin-1")
    declared_riff = _u32le(data, 4)
    detail["riff_declared_bytes"] = declared_riff

    faults: list[str] = []
    # RF64 stores 0xFFFFFFFF here and the real sizes in a ds64 chunk; reading it as a plain
    # RIFF size would "prove" every RF64 file is truncated.
    is_rf64 = magic == b"RF64"
    if not is_rf64 and declared_riff != 0xFFFFFFFF and declared_riff + 8 > size:
        faults.append(
            f"the RIFF header declares {declared_riff + 8} bytes and the file holds {size} — "
            f"{declared_riff + 8 - size} bytes are missing from the end"
        )

    offset = 12
    fmt: dict[str, int] = {}
    data_offset = data_size = -1
    ds64_data_size = -1
    while offset + 8 <= size:
        chunk_id = bytes(data[offset:offset + 4])
        chunk_size = _u32le(data, offset + 4)
        body = offset + 8
        if chunk_id == b"ds64" and body + 24 <= size:
            ds64_data_size = _u64le(data, body + 8)
            detail["ds64_data_bytes"] = ds64_data_size
        elif chunk_id == b"fmt " and body + 16 <= size:
            fmt = {
                "format": _u16le(data, body),
                "channels": _u16le(data, body + 2),
                "sample_rate": _u32le(data, body + 4),
                "byte_rate": _u32le(data, body + 8),
                "block_align": _u16le(data, body + 12),
                "bits": _u16le(data, body + 14),
            }
        elif chunk_id == b"data":
            data_offset, data_size = body, chunk_size
            if is_rf64 and (chunk_size == 0xFFFFFFFF) and ds64_data_size >= 0:
                data_size = ds64_data_size
            if data_size in (0, 0xFFFFFFFF):
                # Some recorders never go back to patch the length. The bytes that are here
                # are the audio; say so rather than calling a normal file broken.
                data_size = size - body
                detail["data_length_patched"] = True
            break
        # Always advances by at least the 8-byte header, so a zero-length chunk (an empty
        # LIST, say) is stepped over rather than ending the scan before the data chunk.
        offset = body + chunk_size + (chunk_size & 1)   # chunks are word aligned

    detail["fmt"] = fmt
    if data_offset < 0:
        return _broken(container, size, "there is no data chunk, so the file holds no audio", detail)

    present = max(0, size - data_offset)
    detail["data_declared_bytes"] = data_size
    detail["data_present_bytes"] = present
    if data_size > present:
        faults.append(
            f"the data chunk declares {data_size} bytes of audio and only {present} are present "
            f"— {data_size - present} bytes are missing from the end"
        )

    byte_rate = fmt.get("byte_rate") or (
        fmt.get("sample_rate", 0) * fmt.get("channels", 0) * (fmt.get("bits", 0) // 8)
    )
    duration = (min(data_size, present) / byte_rate) if byte_rate > 0 else None
    detail["duration_known"] = duration is not None
    if not fmt:
        faults.append("there is no fmt chunk, so the sample format is unknown")

    if faults:
        return AudioInfo(
            duration_s=duration or 0.0,
            container=container,
            truncated=True,
            reason=f"{container}: " + "; ".join(faults),
            size_bytes=size,
            probed_by="walk",
            bitrate=byte_rate * 8 if byte_rate else None,
            detail=detail,
        )
    return AudioInfo(
        duration_s=duration or 0.0,
        container=container,
        truncated=False,
        reason=(
            f"{container}: RIFF and data lengths agree with the file"
            + (f", {duration:.1f}s at {fmt.get('sample_rate', 0)} Hz" if duration is not None
               else ", but the sample format does not give a duration")
        ),
        size_bytes=size,
        probed_by="walk",
        bitrate=byte_rate * 8 if byte_rate else None,
        detail=detail,
    )


# --------------------------------------------------------------------------- the walk


def _broken(container: str, size: int, reason: str, detail: dict[str, Any]) -> AudioInfo:
    detail.setdefault("duration_known", False)
    return AudioInfo(
        duration_s=0.0,
        container=container,
        truncated=True,
        reason=f"{container}: {reason}",
        size_bytes=size,
        probed_by="walk",
        detail=detail,
    )


def _not_understood(container: str, size: int) -> AudioInfo:
    """An unfamiliar format is reported, never quarantined.

    Saying "container not understood" out loud costs a line in the digest. Calling it
    truncated would stop a perfectly good recording, which is a worse failure than the one
    this module is here to catch.
    """
    return AudioInfo(
        duration_s=0.0,
        container=container,
        truncated=False,
        reason=f"{container}: container not understood — no integrity check was performed",
        size_bytes=size,
        probed_by="walk",
        detail={"duration_known": False, "understood": False},
    )


def probe_bytes(data: bytes, name: str = "") -> AudioInfo:
    """Probe audio already in memory. The whole walk is offline and side-effect free."""
    return _walk(data, len(data), name)


def _walk(data: Any, size: int, name: str) -> AudioInfo:
    if size == 0:
        return _broken("empty", 0, "the file is empty — zero bytes arrived", {})
    container = sniff_container(data, size, name)
    if container in ("mp4", "m4a", "3gp", "mov"):
        return _walk_mp4(data, size, container)
    if container == "mp3":
        return _walk_mp3(data, size, container)
    if container == "wav":
        return _walk_wav(data, size, container)
    if container.startswith("unknown") and os.path.splitext(name)[1].lower() == ".mp3":
        # Junk in front of the first frame is common enough in the wild to be worth one try.
        attempt = _walk_mp3(data, size, "mp3")
        if not attempt.truncated:
            return attempt
    return _not_understood(container, size)


# --------------------------------------------------------------------------- ffprobe


def ffprobe_available(ffprobe_path: str | None = None) -> str | None:
    """The ffprobe this run may actually use, or None. A named path is checked, not trusted.

    Handing back a path that cannot be executed made every probe report ``probed_by`` of
    "walk+ffprobe" when ffprobe had never run once — a provenance field saying the opposite
    of what happened, on the one check whose entire value is that its answer is believed.
    """
    if ffprobe_path:
        return ffprobe_path if (os.path.isfile(ffprobe_path) and os.access(ffprobe_path, os.X_OK)) else None
    return shutil.which("ffprobe")


def _run_ffprobe(binary: str, path: str, timeout_s: float) -> tuple[dict[str, Any] | None, str]:
    try:
        completed = subprocess.run(
            [binary, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"ffprobe could not be run: {exc}"
    stderr = completed.stderr.decode("utf-8", "replace").strip()
    if completed.returncode != 0:
        return None, stderr or f"ffprobe exited {completed.returncode}"
    try:
        parsed = json.loads(completed.stdout.decode("utf-8", "replace") or "{}")
    except ValueError as exc:
        return None, f"ffprobe returned output that is not JSON: {exc}"
    return parsed, stderr


def _ffprobe_duration(parsed: dict[str, Any]) -> float | None:
    container = parsed.get("format") or {}
    for source in (container, *(parsed.get("streams") or [])):
        try:
            value = float(source.get("duration"))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _merge_ffprobe(walked: AudioInfo, parsed: dict[str, Any] | None, message: str) -> AudioInfo:
    """Fold ffprobe's answer into the walk's, never downgrading a proven truncation.

    The asymmetry is deliberate and is the point of the whole module: ffprobe may add a
    duration the walk could not derive, and may reveal a shortfall the container's index
    hides, but it may not talk the walk out of a truncation it proved from the bytes. A
    check that can be argued down is not a check.
    """
    detail = dict(walked.detail)
    duration = walked.duration_s
    truncated = walked.truncated
    reason = walked.reason
    known = bool(detail.get("duration_known"))

    if parsed is None:
        detail["ffprobe_error"] = message
        lowered = message.lower()
        if any(marker in lowered for marker in _FFPROBE_CORRUPTION_MARKERS):
            truncated = True
            reason = f"{reason}; ffprobe cannot read the file either: {message}"
        else:
            reason = f"{reason} (ffprobe: {message})"
        return _replace(walked, duration_s=duration, truncated=truncated, reason=reason,
                        probed_by="walk+ffprobe", detail=detail)

    probed = _ffprobe_duration(parsed)
    container_facet = parsed.get("format") or {}
    detail["ffprobe_format"] = container_facet.get("format_name")
    if probed is not None:
        detail["ffprobe_duration_s"] = probed
    try:
        bitrate = int(container_facet.get("bit_rate"))
    except (TypeError, ValueError):
        bitrate = walked.bitrate or 0

    if probed is not None and known and duration > 0:
        shortfall = duration - probed
        if probed < duration * FFPROBE_SHORTFALL_RATIO and shortfall >= FFPROBE_SHORTFALL_MIN_S:
            truncated = True
            reason = (
                f"{reason}; the index declares {duration:.1f}s but ffprobe finds only "
                f"{probed:.1f}s of decodable audio — {shortfall:.1f}s of media data is missing"
            )
        duration = probed
    elif probed is not None:
        duration = probed
        detail["duration_known"] = True
        detail["duration_from"] = "ffprobe"
        reason = f"{reason}; ffprobe reports {probed:.1f}s"

    return _replace(walked, duration_s=duration, truncated=truncated, reason=reason,
                    probed_by="walk+ffprobe", bitrate=bitrate or None, detail=detail)


def _replace(info: AudioInfo, **changes: Any) -> AudioInfo:
    fields = {
        "duration_s": info.duration_s,
        "container": info.container,
        "truncated": info.truncated,
        "reason": info.reason,
        "size_bytes": info.size_bytes,
        "probed_by": info.probed_by,
        "bitrate": info.bitrate,
        "detail": info.detail,
    }
    fields.update(changes)
    return AudioInfo(**fields)


# --------------------------------------------------------------------------- entry point


def probe(
    path: str,
    *,
    use_ffprobe: bool = True,
    ffprobe_path: str | None = None,
    ffprobe_timeout_s: float = FFPROBE_TIMEOUT_S,
) -> AudioInfo:
    """Is this file a whole recording? Returns an :class:`AudioInfo`, never raises for content.

    The pure-Python walk always runs and owns the ``truncated`` verdict. ffprobe, when it is
    installed, refines the duration and can add a truncation the container's index conceals
    — it can never remove one.

    ``truncated=True`` means quarantine, loudly. It never means transcribe-and-mark-done.
    """
    if not os.path.exists(path):
        return _broken("missing", 0, f"{path} does not exist, so nothing could be probed", {})
    try:
        size = os.path.getsize(path)
        with _mapped(path) as data:
            walked = _walk(data, size, os.path.basename(path))
    except OSError as exc:
        return _broken("unreadable", 0, f"{os.path.basename(path)} could not be read: {exc}", {})

    binary = ffprobe_available(ffprobe_path) if use_ffprobe else None
    if not binary:
        if use_ffprobe and ffprobe_path:
            # A configured path that is not there is an operator mistake, and it is written
            # onto the result rather than passed over: the walk's verdict stands either way,
            # but nobody should have to guess why the refinement never happened.
            walked.detail["ffprobe_error"] = (
                f"{ffprobe_path} is not an executable file, so ffprobe was not used; the "
                "container walk's verdict stands on its own"
            )
        return walked
    parsed, message = _run_ffprobe(binary, path, ffprobe_timeout_s)
    return _merge_ffprobe(walked, parsed, message)


# --------------------------------------------------------------------------- fixtures
#
# Builders, not test data on disk. A fixture that is a checked-in binary is a fixture nobody
# can read a diff of, and one nobody can vary. These construct the bytes in memory so a test
# can prove each branch of the detector fires — above all the dying-battery MP4, whose whole
# danger is that it looks perfect to every other check in the pipeline.


def _box(box_type: bytes, payload: bytes) -> bytes:
    return (len(payload) + 8).to_bytes(4, "big") + box_type + payload


def _mvhd(duration_s: float, timescale: int, version: int) -> bytes:
    duration = int(round(duration_s * timescale))
    if version == 1:
        head = (
            b"\x01\x00\x00\x00"
            + (0).to_bytes(8, "big") + (0).to_bytes(8, "big")
            + timescale.to_bytes(4, "big") + duration.to_bytes(8, "big")
        )
    else:
        head = (
            b"\x00\x00\x00\x00"
            + (0).to_bytes(4, "big") + (0).to_bytes(4, "big")
            + timescale.to_bytes(4, "big") + duration.to_bytes(4, "big")
        )
    rate = (0x00010000).to_bytes(4, "big")            # 1.0
    volume = (0x0100).to_bytes(2, "big")              # 1.0
    matrix = b"".join(
        v.to_bytes(4, "big")
        for v in (0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)
    )
    tail = rate + volume + b"\x00" * 2 + b"\x00" * 8 + matrix + b"\x00" * 24 + (2).to_bytes(4, "big")
    return _box(b"mvhd", head + tail)


#: 32 kbit/s — what a phone's AAC voice recording actually costs per second.
_FIXTURE_BYTES_PER_SECOND = 4000


def build_mp4_bytes(
    duration_s: float = 754.0,
    *,
    timescale: int = 600,
    mdat_bytes: int | None = None,
    mvhd_version: int = 0,
    faststart: bool = False,
    mdat_to_eof: bool = False,
) -> bytes:
    """A structurally valid MP4/M4A: ftyp, mdat, moov(mvhd).

    Enough of a file to exercise the walk — it is not a playable recording and does not
    pretend to be one. ``faststart`` puts the index first, as a desktop encoder would; the
    default puts it last, as his phone does, which is why an interrupted recording loses it.
    An mdat declared "to end of file" consumes everything after it, so that form is only
    ever emitted with the index in front of it, which is the only arrangement a real writer
    produces.
    """
    if mdat_bytes is None:
        mdat_bytes = max(1024, int(duration_s * _FIXTURE_BYTES_PER_SECOND))
    ftyp = _box(b"ftyp", b"M4A " + (0).to_bytes(4, "big") + b"M4A " + b"mp42" + b"isom")
    payload = (bytes(range(251)) * (mdat_bytes // 251 + 1))[:mdat_bytes]
    mdat = (b"\x00\x00\x00\x00" + b"mdat" + payload) if mdat_to_eof else _box(b"mdat", payload)
    moov = _box(b"moov", _mvhd(duration_s, timescale, mvhd_version))
    return ftyp + moov + mdat if (faststart or mdat_to_eof) else ftyp + mdat + moov


def truncated_mp4_bytes(
    duration_s: float = 754.0,
    *,
    keep_fraction: float = 0.55,
    mdat_bytes: int | None = None,
    mdat_to_eof: bool = False,
) -> bytes:
    """The dying-battery file: a real recording that stops mid-write, so the moov never lands.

    This is the exact shape of the failure this service exists to catch. It has a plausible
    header, real audio bytes, and it will hash and upload perfectly.

    With ``mdat_to_eof`` the index is written first and the media has no declared length, so
    the walk can only catch a severe cut — pass a small ``keep_fraction`` to exercise that
    branch, and see the module docstring for why the check stops there rather than guessing.
    """
    whole = build_mp4_bytes(duration_s, mdat_bytes=mdat_bytes, mdat_to_eof=mdat_to_eof)
    cut = max(16, int(len(whole) * keep_fraction))
    return whole[:cut]


def mdat_overrun_mp4_bytes(duration_s: float = 754.0, *, missing_bytes: int = 2048) -> bytes:
    """Index intact, media short: the mdat declares more bytes than the file holds.

    A separate branch from the missing-moov case, and worth its own fixture — a file can
    carry a perfectly parseable duration and still be missing most of its audio.
    """
    whole = build_mp4_bytes(duration_s, faststart=True)
    if missing_bytes >= len(whole):
        raise ValueError("cannot remove more bytes than the fixture contains")
    return whole[:-missing_bytes]


def build_wav_bytes(
    duration_s: float = 12.0, *, sample_rate: int = 16000, channels: int = 1, bits: int = 16
) -> bytes:
    """A complete PCM WAV whose RIFF and data lengths agree with the file."""
    byte_rate = sample_rate * channels * (bits // 8)
    block_align = channels * (bits // 8)
    frames = int(round(duration_s * sample_rate))
    audio = bytes(frames * block_align)
    fmt = (
        (1).to_bytes(2, "little") + channels.to_bytes(2, "little")
        + sample_rate.to_bytes(4, "little") + byte_rate.to_bytes(4, "little")
        + block_align.to_bytes(2, "little") + bits.to_bytes(2, "little")
    )
    body = (
        b"WAVE"
        + b"fmt " + len(fmt).to_bytes(4, "little") + fmt
        + b"data" + len(audio).to_bytes(4, "little") + audio
    )
    return b"RIFF" + len(body).to_bytes(4, "little") + body


def truncated_wav_bytes(duration_s: float = 12.0, *, keep_fraction: float = 0.4) -> bytes:
    """A WAV whose header still promises the full recording after the tail was lost."""
    whole = build_wav_bytes(duration_s)
    return whole[: max(44, int(len(whole) * keep_fraction))]


def build_mp3_bytes(frames: int = 400, *, with_id3: bool = True) -> bytes:
    """MPEG-1 Layer III, 128 kbps, 44.1 kHz — ``frames`` complete frames, 26.1 ms each."""
    header = bytes((0xFF, 0xFB, 0x90, 0x00))
    length = 144 * 128000 // 44100      # 417 bytes, no padding
    frame = header + bytes(length - 4)
    tag = b""
    if with_id3:
        body = bytes(64)
        tag = b"ID3" + bytes((3, 0, 0)) + bytes((0, 0, 0, len(body))) + body
    return tag + frame * frames


def truncated_mp3_bytes(frames: int = 400, *, keep_bytes_of_last: int = 100) -> bytes:
    """An MP3 that stops in the middle of its final frame."""
    whole = build_mp3_bytes(frames)
    length = 144 * 128000 // 44100
    return whole[: len(whole) - (length - keep_bytes_of_last)]
