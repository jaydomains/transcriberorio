"""Truncated audio is caught — the check that stops a battery-death fragment being a success.

The failure this replaces has no other symptom. A recording cut off when the handset dies
uploads perfectly, matches its Graph hash byte for byte, transcribes as a fragment and is
filed as done. Nothing upstream can see it: the bytes that arrived are exactly the bytes
that were written, and they are wrong anyway. Only the container says so.

The fixtures are built here, in this file, byte by byte — deliberately not taken from
``audio.py``'s own builders, so the detector is being shown bytes the test owns rather than
bytes the module and the test agree about. ``ffprobe`` is switched off in every case: the
pure-Python walk is the primary path and has to be correct on its own.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from transcriber import audio


# --------------------------------------------------------------------------- fixtures
#
# A minimal but structurally real ISO base media file: ftyp, then mdat holding the audio
# bytes, then the moov index last — which is the order his phone writes, and the reason an
# interrupted recording loses its index rather than its audio.


def _box(box_type: bytes, payload: bytes) -> bytes:
    return (len(payload) + 8).to_bytes(4, "big") + box_type + payload


def _mvhd(duration_s: float, timescale: int = 600) -> bytes:
    duration = int(round(duration_s * timescale))
    head = (
        b"\x00\x00\x00\x00"                       # version 0, no flags
        + (0).to_bytes(4, "big")                  # creation time
        + (0).to_bytes(4, "big")                  # modification time
        + timescale.to_bytes(4, "big")
        + duration.to_bytes(4, "big")
    )
    matrix = b"".join(
        v.to_bytes(4, "big") for v in (0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)
    )
    tail = (
        (0x00010000).to_bytes(4, "big")           # rate 1.0
        + (0x0100).to_bytes(2, "big")             # volume 1.0
        + b"\x00" * 2 + b"\x00" * 8               # reserved
        + matrix + b"\x00" * 24
        + (2).to_bytes(4, "big")                  # next track id
    )
    return _box(b"mvhd", head + tail)


#: 32 kbit/s, which is what a phone's AAC voice recording actually costs per second.
BYTES_PER_SECOND = 4000


def whole_m4a(duration_s: float = 754.0, *, index_first: bool = False) -> bytes:
    """A structurally complete recording: ftyp + mdat + moov, all lengths agreeing."""
    ftyp = _box(b"ftyp", b"M4A " + (0).to_bytes(4, "big") + b"M4A mp42isom")
    media = bytes(int(duration_s * BYTES_PER_SECOND))
    mdat = _box(b"mdat", media)
    moov = _box(b"moov", _mvhd(duration_s))
    return ftyp + moov + mdat if index_first else ftyp + mdat + moov


def battery_death_m4a(duration_s: float = 754.0, keep_fraction: float = 0.6) -> bytes:
    """The dying-battery file: the write stops partway, so the moov never lands."""
    whole = whole_m4a(duration_s)
    return whole[: int(len(whole) * keep_fraction)]


def mdat_overruns_the_file(duration_s: float = 754.0, missing: int = 4096) -> bytes:
    """Index intact, media short: the mdat declares more bytes than the file holds."""
    whole = whole_m4a(duration_s, index_first=True)
    return whole[:-missing]


def _write(data: bytes, directory: str, name: str = "recording.m4a") -> str:
    path = os.path.join(directory, name)
    with open(path, "wb") as handle:
        handle.write(data)
    return path


class TruncatedMp4IsCaught(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def probe(self, data: bytes, name: str = "recording.m4a"):
        # ffprobe off everywhere: it is not installed here, it may not be installed in
        # production, and the walk is what has to be right.
        return audio.probe(_write(data, self.dir.name, name), use_ffprobe=False)

    # -- the file that must fail ---------------------------------------------------

    def test_a_recording_that_lost_its_index_is_truncated(self) -> None:
        info = self.probe(battery_death_m4a())

        self.assertTrue(info.truncated, "the battery-death fragment was accepted as whole")
        self.assertIn("moov", info.reason)
        self.assertEqual(info.probed_by, "walk")
        self.assertEqual(info.detail.get("moov"), "absent")

    def test_the_reason_says_what_a_person_needs_to_know(self) -> None:
        """Quarantine is read by a human at 06:00. 'truncated=True' on its own is not a reason."""
        info = self.probe(battery_death_m4a())
        self.assertIn("interrupted mid-capture", info.reason)

    def test_an_mdat_that_overruns_the_file_is_truncated(self) -> None:
        """A separate failure: the index parses perfectly and most of the audio is gone."""
        info = self.probe(mdat_overruns_the_file())

        self.assertTrue(info.truncated)
        self.assertIn("mdat", info.reason)
        self.assertIn("bytes short", info.reason)
        # The duration is still readable — which is exactly why this one is dangerous.
        self.assertAlmostEqual(info.duration_s, 754.0, places=1)

    def test_an_empty_file_is_truncated_not_silence(self) -> None:
        info = self.probe(b"")
        self.assertTrue(info.truncated)

    def test_a_file_cut_off_inside_its_index_is_truncated(self) -> None:
        whole = whole_m4a(120.0, index_first=True)
        info = self.probe(whole[: len(whole) - 8])
        self.assertTrue(info.truncated)

    def test_a_file_with_no_audio_data_at_all_is_truncated(self) -> None:
        ftyp = _box(b"ftyp", b"M4A " + (0).to_bytes(4, "big") + b"M4A mp42isom")
        info = self.probe(ftyp + _box(b"moov", _mvhd(60.0)))
        self.assertTrue(info.truncated)
        self.assertIn("no mdat", info.reason)

    def test_a_missing_file_is_reported_rather_than_assumed_fine(self) -> None:
        info = audio.probe(os.path.join(self.dir.name, "never-written.m4a"), use_ffprobe=False)
        self.assertTrue(info.truncated)
        self.assertIn("does not exist", info.reason)

    # -- the file that must pass ---------------------------------------------------

    def test_a_well_formed_recording_is_not_truncated(self) -> None:
        info = self.probe(whole_m4a(754.0))

        self.assertFalse(info.truncated, f"a complete file was rejected: {info.reason}")
        self.assertAlmostEqual(info.duration_s, 754.0, places=1)
        self.assertEqual(info.container, "m4a")
        self.assertTrue(info.detail.get("duration_known"))

    def test_a_well_formed_recording_with_the_index_first_is_not_truncated(self) -> None:
        """A desktop encoder writes faststart. It must not read as a fault."""
        info = self.probe(whole_m4a(300.0, index_first=True))
        self.assertFalse(info.truncated, info.reason)
        self.assertAlmostEqual(info.duration_s, 300.0, places=1)

    def test_a_twelve_second_note_is_not_truncated_for_being_short(self) -> None:
        """"ja, approved, go ahead on Beach Court" is a real recording, not a fragment."""
        info = self.probe(whole_m4a(12.0))
        self.assertFalse(info.truncated, info.reason)
        self.assertAlmostEqual(info.duration_s, 12.0, places=1)

    # -- the other containers ------------------------------------------------------

    def test_a_truncated_wav_is_caught_and_a_whole_one_is_not(self) -> None:
        whole = audio.build_wav_bytes(12.0)
        self.assertFalse(self.probe(whole, "note.wav").truncated)

        cut = audio.truncated_wav_bytes(12.0, keep_fraction=0.4)
        info = self.probe(cut, "cut.wav")
        self.assertTrue(info.truncated, info.reason)

    def test_a_truncated_mp3_is_caught_and_a_whole_one_is_not(self) -> None:
        self.assertFalse(self.probe(audio.build_mp3_bytes(400), "note.mp3").truncated)

        info = self.probe(audio.truncated_mp3_bytes(400), "cut.mp3")
        self.assertTrue(info.truncated, info.reason)

    def test_an_unfamiliar_container_is_not_quarantined_on_a_guess(self) -> None:
        """Falsely quarantining a format we do not understand is its own kind of damage."""
        info = self.probe(b"OggS" + bytes(4096), "note.ogg")
        self.assertFalse(info.truncated)
        self.assertIn("not understood", info.reason)
        self.assertFalse(info.detail.get("duration_known", False))

    # -- ffprobe is a bonus, never a dependency ------------------------------------

    def test_the_walk_stands_alone_when_ffprobe_is_absent(self) -> None:
        """Named as its own case because 'it works on my machine with ffmpeg' is the trap."""
        missing = os.path.join(self.dir.name, "no-such-ffprobe")
        info = audio.probe(_write(battery_death_m4a(), self.dir.name), use_ffprobe=True, ffprobe_path=missing)

        self.assertTrue(info.truncated)
        self.assertEqual(info.probed_by, "walk", "the walk must own the verdict on its own")
        # And it says so, rather than quietly claiming ffprobe took part.
        self.assertIn("not an executable file", info.detail.get("ffprobe_error", ""))

    def test_a_configured_ffprobe_that_is_not_there_is_not_reported_as_used(self) -> None:
        """A provenance field that lies is worse than one that is missing."""
        self.assertIsNone(audio.ffprobe_available(os.path.join(self.dir.name, "nope")))


class TruncationSurvivesTheFixtureBuilders(unittest.TestCase):
    """The builders shipped in ``audio.py`` are used by ``selftest``; they must agree."""

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def probe(self, data: bytes, name: str = "recording.m4a"):
        return audio.probe(_write(data, self.dir.name, name), use_ffprobe=False)

    def test_the_shipped_truncated_fixture_is_detected(self) -> None:
        self.assertTrue(self.probe(audio.truncated_mp4_bytes()).truncated)

    def test_the_shipped_whole_fixture_is_not(self) -> None:
        info = self.probe(audio.build_mp4_bytes())
        self.assertFalse(info.truncated, info.reason)

    def test_the_shipped_overrun_fixture_is_detected(self) -> None:
        self.assertTrue(self.probe(audio.mdat_overrun_mp4_bytes()).truncated)


if __name__ == "__main__":
    unittest.main()
