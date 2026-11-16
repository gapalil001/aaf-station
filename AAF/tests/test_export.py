from pathlib import Path
import shutil
import struct
import sys
import tempfile
import unittest
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aaf_core import materialize, read_sessions
from aaf_export import plan_export, write_aaf
from pcm_audio import PCMReader


def item(path, name, start, length, offset=0, fade_in=0, fade_out=0, mode=0):
    return dict(name=name, position=start, duration=length, path=str(path), offset=offset,
                fade_in=fade_in, fade_out=fade_out, channels=2, source_length=3,
                channel_mode=mode, gain=1, loop=False, notes=[], error="")


def project(*clips):
    return dict(name="Fade test", fps=25, timecode=3600, drop=False, tracks=[
        dict(id="chosen", name="Stereo", clips=list(clips), warnings=[]),
        dict(id="excluded", name="Excluded", clips=list(clips), warnings=[]),
    ])


def stereo_samples(path, offset, times):
    with PCMReader(path) as audio:
        return [tuple(int.from_bytes(audio.channel(round((offset + i) * audio.rate), 1, ch, 3),
                                     "little", signed=True) for ch in (0, 1)) for i in times]


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.waves = self.folder / "asymmetric.wav"
        with wave.open(str(self.waves), "wb") as dest:
            dest.setparams((2, 2, 48000, 144000, "NONE", "not compressed"))
            dest.writeframes(struct.pack("<hh", 8192, 16384) * 144000)

    def roundtrip(self, plan, embedded=False, move=False):
        file, folder = write_aaf(plan, self.folder / "session.aaf", embedded=embedded, handles_ms=250)
        if move:
            moved = self.folder / "moved"
            moved.mkdir()
            file = Path(shutil.move(file, moved))
            shutil.move(folder, moved)
        session = read_sessions(file)[0]
        self.assertFalse(any(t.errors for t in session.tracks))
        self.assertTrue(all(not a.problem for a in session.assets.values()))
        return materialize(session, {t.id for t in session.tracks}, [], self.folder / "roundtrip")

    def test_range_inside_fade_preserves_envelope_and_stereo(self):
        raw = item(self.waves, "Fade edges", 1, 2, .5, .5, .5)
        plan = plan_export(project(raw), {"chosen"}, (1.25, 2.75), 48000)
        result = self.roundtrip(plan, move=True)
        self.assertEqual(result["timecode_zero"], 3601.24)
        self.assertEqual([t["name"] for t in result["tracks"]], ["Stereo"])
        clip, = result["tracks"][0]["clips"]
        self.assertEqual((clip["position"], clip["duration"], clip["fade_in"], clip["fade_out"]), (0, 1.5, 0, 0))
        self.assertEqual(stereo_samples(clip["path"], clip["offset"], (0, .25, 1.375)),
                         [(1048576, 2097152), (2097152, 4194304), (1572864, 3145728)])

    def test_overlaps_keep_complementary_fades_and_timeline(self):
        left = item(self.waves, "Outgoing", 0, 1, fade_out=.25)
        right = item(self.waves, "Incoming", .75, 1, fade_in=.25)
        plan = plan_export(project(left, right), {"chosen"}, (0, 2), 48000)
        tracks = self.roundtrip(plan, embedded=True)["tracks"]
        self.assertEqual(len(tracks), 2)
        outgoing, incoming = (t["clips"][0] for t in tracks)
        self.assertEqual(incoming["position"], .75)
        a, = stereo_samples(outgoing["path"], outgoing["offset"], (.875,))
        b, = stereo_samples(incoming["path"], incoming["offset"], (.125,))
        self.assertEqual(tuple(x + y for x, y in zip(a, b)), (2097152, 4194304))

    def test_editable_fades_reopen_with_lengths_and_unchanged_samples(self):
        raw = item(self.waves, "Editable", 1, 2, .5, .25, .5)
        plan = plan_export(project(raw), {"chosen"}, (0, 3), 48000, render_fades=False)
        clip, = self.roundtrip(plan, embedded=True)["tracks"][0]["clips"]
        self.assertEqual((clip["fade_in"], clip["fade_out"]), (.25, .5))
        self.assertEqual(stereo_samples(clip["path"], clip["offset"], (0,)), [(2097152, 4194304)])

    def test_split_preview_reopens_as_ordered_mono_tracks_at_project_position(self):
        raw = item(self.waves, "Split", 1, 2, .5, mode=1)
        plan = plan_export(project(raw), {"chosen"}, (1.5, 2.5), 48000,
                           keep_positions=True, split_stereo=True)
        tracks = self.roundtrip(plan)["tracks"]
        self.assertEqual([t["name"] for t in tracks], ["Stereo.L", "Stereo.R"])
        for track, expected in zip(tracks, (4194304, 2097152)):
            clip, = track["clips"]
            self.assertEqual((clip["position"], clip["duration"]), (1.5, 1))
            with PCMReader(clip["path"]) as audio:
                self.assertEqual(audio.channels, 1)
                sample = audio.channel(round(clip["offset"] * audio.rate), 1, 0, 3)
                self.assertEqual(int.from_bytes(sample, "little", signed=True), expected)
