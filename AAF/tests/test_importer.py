import argparse
import copy
import math
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import wave

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "aaf_core.py").is_file():
    ROOT = ROOT / "outputs" / "reaper-aaf-importer"
sys.path.insert(0, str(ROOT))
from aaf_core import *
import aaf2
from reaper_apply import apply_plan
from pcm_audio import PCMReader, assemble


def wav(path, samples, channels=1, width=2, rate=48000):
    with wave.open(str(path), "wb") as w:
        w.setparams((channels, width, rate, len(samples) // channels, "NONE", "not compressed"))
        w.writeframes(b"".join(int(n).to_bytes(width, "little", signed=True) for n in samples))


def make_fixture(folder, mode="physical", name="stereo.aaf", frames=4800):
    folder = Path(folder)
    path = folder / name
    source = folder / "stereo.wav"
    left = [round(15000 * math.sin(2 * math.pi * 440 * i / 48000)) for i in range(frames)]
    right = [round(10000 * math.sin(2 * math.pi * 880 * i / 48000)) for i in range(frames)]
    wav(source, [v for pair in zip(left, right) for v in pair], channels=2)
    with aaf2.open(str(path), "w") as f:
        composition = f.create.CompositionMob("Pro Tools style • stereo diagnostic")
        f.content.mobs.append(composition)
        filemob = f.create.SourceMob("Stereo PCM")
        f.content.mobs.append(filemob)
        fs1 = filemob.import_audio_essence(str(source))
        fs1.slot_id = 11  # deliberately NOT channel 1
        fs2 = filemob.create_timeline_slot(48000, 42)  # deliberately NOT channel 2
        fs2.segment = f.create.SourceClip(length=frames, media_kind="sound")
        if mode != "ambiguous":
            fs1["PhysicalTrackNumber"].value = 1
            fs2["PhysicalTrackNumber"].value = 1 if mode == "duplicate" else 2
        master = f.create.MasterMob("Stereo recording")
        f.content.mobs.append(master)
        for i, (file_slot, master_id) in enumerate(((11, 101), (42, 205))):
            ms = master.create_timeline_slot(48000, master_id)
            ms.segment = f.create.SourceClip(0, frames, filemob.mob_id, file_slot, "sound")
            cs = composition.create_timeline_slot(48000, 7 + i * 10)
            cs.name = "A1" if i == 0 else "A2"
            sequence = f.create.Sequence("sound")
            sequence.components.append(f.create.Filler("sound", 48000))
            sequence.components.append(f.create.SourceClip(0, frames // 2, master.mob_id, master_id, "sound"))
            sequence.components.append(f.create.Filler("sound", frames // 2))
            sequence.components.append(f.create.SourceClip(frames // 2, frames // 2, master.mob_id, master_id, "sound"))
            sequence.length = 48000 + frames + frames // 2
            cs.segment = sequence
        tc = composition.create_timeline_slot(25, 99)
        tc.name = "TC"
        tc.segment = f.create.Timecode(25, False, 25 * 3601)
        tc.segment.start = 25 * 3600
    return path, source, left, right


class FakeReaper:
    def __init__(self, fail=False):
        self.tracks = [{"name": "existing", "clips": []}]
        self.calls = []
        self.fail = fail

    def __getattr__(self, key):
        if not key.startswith("RPR_"):
            raise AttributeError(key)
        name = key.removeprefix("RPR_")
        def call(*args):
            self.calls.append((name, args))
            if name == "EnumProjects": return ("project", -1, "", 4096)
            if name == "CountTracks": return len(self.tracks)
            if name == "InsertTrackAtIndex":
                self.tracks.insert(args[0], {"name": "", "clips": []}); return
            if name == "GetTrack": return self.tracks[args[1]]
            if name == "GetSetMediaTrackInfo_String": args[0]["name"] = args[2]; return (True,)
            if name == "PCM_Source_CreateFromFile": return "source"
            if name == "GetMediaItemTake_Source": return args[0]["source"]
            if name == "GetMediaSourceLength":
                assert any(take.get("source") == args[0] for track in self.tracks
                           for item in track["clips"] for take in item.get("takes", []))
                return (100.0, "source", False)
            if name == "GetMediaSourceSampleRate": return 48000
            if name == "GetMediaSourceNumChannels": return 2
            if name == "AddMediaItemToTrack":
                item = {"settings": {}, "takes": []}
                args[0]["clips"].append(item); return item
            if name == "AddTakeToMediaItem":
                take = {"settings": {}}
                args[0]["takes"].append(take); return take
            if name == "SetMediaItemTake_Source":
                if self.fail: return False
                args[0]["source"] = args[1]
                return True
            if name in ("SetMediaItemInfo_Value", "SetMediaItemTakeInfo_Value"):
                args[0]["settings"][args[1]] = args[2]; return True
            if name == "DeleteTrack": self.tracks.remove(args[0]); return
            return True
        return call


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)

    def tearDown(self): self.temp.cleanup()

    def fixture(self, mode="physical"):
        path, source, left, right = make_fixture(self.folder, mode)
        return read_sessions(path)[0], source, left, right

    def plan(self, session):
        return materialize(session, {"7", "17"}, [("7", "17")], self.folder / "media")

    def test_pro_tools_combiner_assembles_stereo_without_manual_pair(self):
        aaf = self.folder / "combiner.aaf"
        samples = ([1000, 2000, 3000, 4000], [-1, -2, -3, -4])
        with aaf2.open(str(aaf), "w") as f:
            op = f.create.OperationDef(PRO_TOOLS_CHANNEL_COMBINER, "Audio Channel Combiner")
            op.media_kind = "sound"
            op.number_inputs = 2
            f.dictionary.register_def(op)
            group = f.create.OperationGroup(op, 3, "sound")
            for index, data in enumerate(samples):
                audio = self.folder / (str(index) + ".wav")
                wav(audio, data)
                mob = f.create.SourceMob("Audio")
                f.content.mobs.append(mob)
                slot = mob.import_audio_essence(str(audio))
                group.segments.append(mob.create_source_clip(slot.slot_id, start=1, length=3))
            composition = f.create.CompositionMob("Stereo")
            f.content.mobs.append(composition)
            slot = composition.create_timeline_slot(48000)
            slot.segment = group
        session = read_sessions(aaf)[0]
        self.assertEqual(session.tracks[0].errors, [])
        plan = materialize(session, {str(slot.slot_id)}, [], self.folder / "media")
        clip, = plan["tracks"][0]["clips"]
        self.assertTrue(clip["stereo"])
        self.assertEqual(clip["offset"], 1 / 48000)
        self.assertEqual(clip["duration"], 3 / 48000)
        with PCMReader(clip["path"]) as pcm:
            self.assertEqual(pcm.channels, 2)
            self.assertEqual(pcm.channel(0, 4, 0, 2), struct.pack("<4h", *samples[0]))
            self.assertEqual(pcm.channel(0, 4, 1, 2), struct.pack("<4h", *samples[1]))

    def test_nonsequential_slot_ids_map_to_distinct_pcm_channels(self):
        s, _, _, _ = self.fixture()
        self.assertEqual([t.clips[0].channel for t in s.tracks], [0, 1])
        self.assertEqual([t.clips[0].route[-1]["slot_id"] for t in s.tracks], [11, 42])

    def test_stereo_is_sample_exact_not_duplicated_left(self):
        s, _, left, right = self.fixture()
        plan = self.plan(s)
        self.assertEqual(len(plan["tracks"]), 1)
        clip = plan["tracks"][0]["clips"][0]
        with PCMReader(clip["path"]) as pcm:
            actual_l = pcm.channel(0, len(left), 0, 2)
            actual_r = pcm.channel(0, len(right), 1, 2)
        self.assertEqual(actual_l, b"".join(struct.pack("<h", n) for n in left))
        self.assertEqual(actual_r, b"".join(struct.pack("<h", n) for n in right))
        self.assertNotEqual(actual_l, actual_r)

    def test_explicit_pair_resolves_ambiguous_interleaved_source(self):
        s, _, left, right = self.fixture("ambiguous")
        self.assertIsNone(s.tracks[0].clips[0].channel)
        plan = self.plan(s)
        with PCMReader(plan["tracks"][0]["clips"][0]["path"]) as pcm:
            self.assertEqual(pcm.channel(0, len(left), 0, 2), b"".join(struct.pack("<h", n) for n in left))
            self.assertEqual(pcm.channel(0, len(right), 1, 2), b"".join(struct.pack("<h", n) for n in right))

    def test_same_channel_duplicate_is_blocked_even_with_different_slot_ids(self):
        s, _, _, _ = self.fixture("duplicate")
        with self.assertRaisesRegex(ImportErrorAAF, "SAME source channel"):
            self.plan(s)

    def test_explicit_override_can_recover_known_channel_two(self):
        s, _, _, right = self.fixture("duplicate")
        for c in s.tracks[1].clips: c.channel = 1
        plan = self.plan(s)
        with PCMReader(plan["tracks"][0]["clips"][0]["path"]) as pcm:
            self.assertEqual(pcm.channel(0, len(right), 1, 2), b"".join(struct.pack("<h", n) for n in right))

    def test_session_positions_gaps_and_source_trims_preserved(self):
        s, _, _, _ = self.fixture()
        clips = self.plan(s)["tracks"][0]["clips"]
        self.assertEqual([(c["position"], c["duration"], c["offset"]) for c in clips], [(1, .05, 0), (1.1, .05, .05)])
        self.assertEqual(s.timecode_zero, 3600)

    def test_selection_does_not_import_other_track(self):
        s, _, left, _ = self.fixture()
        p = materialize(s, {"7"}, [], self.folder / "one")
        self.assertEqual(len(p["tracks"]), 1)
        with PCMReader(p["tracks"][0]["clips"][0]["path"]) as pcm:
            self.assertEqual(pcm.channels, 1)
            self.assertEqual(pcm.channel(0, len(left), 0, 2), b"".join(struct.pack("<h", n) for n in left))

    def test_stereo_different_offsets_preserves_handles_and_alignment(self):
        lp, rp = self.folder / "l.wav", self.folder / "r.wav"
        wav(lp, [10, 20, 30, 40], rate=4)
        wav(rp, [100, 200, 300, 400], rate=4)
        path, offset = assemble(lp, 0, .25, self.folder, rp, 0, .5)
        self.assertEqual(offset, .5)
        with PCMReader(path) as pcm:
            self.assertEqual(list(struct.unpack("<5h", pcm.channel(0, 5, 0, 2))), [0, 10, 20, 30, 40])
            self.assertEqual(list(struct.unpack("<5h", pcm.channel(0, 5, 1, 2))), [100, 200, 300, 400, 0])
            self.assertEqual(struct.unpack("<h", pcm.channel(2, 1, 0, 2))[0], 20)
            self.assertEqual(struct.unpack("<h", pcm.channel(2, 1, 1, 2))[0], 300)

    def test_mismatched_edits_are_blocked(self):
        s, _, _, _ = self.fixture()
        s.tracks[1].clips[1].position += .001
        with self.assertRaisesRegex(ImportErrorAAF, "not aligned"):
            self.plan(s)

    def test_samplerate_mismatch_is_blocked(self):
        lp, rp = self.folder / "l.wav", self.folder / "r.wav"
        wav(lp, [1, 2, 3], rate=48000)
        wav(rp, [4, 5, 6], rate=44100)
        with self.assertRaisesRegex(AudioError, "sample rates differ"):
            assemble(lp, 0, 0, self.folder, rp, 0, 0)

    def test_bit_depth_is_extended_without_losing_sample_values(self):
        lp, rp = self.folder / "l.wav", self.folder / "r.wav"
        wav(lp, [-32768, 32767], width=2)
        wav(rp, [-8388608, 8388607], width=3)
        path, _ = assemble(lp, 0, 0, self.folder, rp, 0, 0)
        with PCMReader(path) as pcm:
            self.assertEqual(pcm.width, 3)
            self.assertEqual(pcm.channel(0, 2, 0, 3), bytes.fromhex("00008000ff7f"))

    def test_api_creates_one_stereo_track_and_preserves_existing_track(self):
        s, _, _, _ = self.fixture()
        api = FakeReaper()
        self.assertEqual(apply_plan(api, self.plan(s), "project"), (1, 2))
        self.assertEqual(api.tracks[0]["name"], "existing")
        self.assertEqual(api.tracks[1]["clips"][1]["takes"][0]["settings"]["D_STARTOFFS"], .05)
        self.assertEqual([c for c in api.calls if c[0] == "PreventUIRefresh"][-1][1], (-1,))

    def test_api_failure_removes_only_new_tracks(self):
        s, _, _, _ = self.fixture()
        api = FakeReaper(fail=True)
        with self.assertRaisesRegex(RuntimeError, "attach"):
            apply_plan(api, self.plan(s), "project")
        self.assertEqual(len(api.tracks), 1)
        self.assertEqual(api.tracks[0]["name"], "existing")
        self.assertTrue(any(c[0] == "PCM_Source_Destroy" for c in api.calls))

    def test_wrong_project_is_blocked_before_mutation(self):
        s, _, _, _ = self.fixture()
        api = FakeReaper()
        with self.assertRaisesRegex(ValueError, "project changed"):
            apply_plan(api, self.plan(s), "other_project")
        self.assertFalse(any(c[0] == "Undo_BeginBlock2" for c in api.calls))

    def test_cross_rate_source_start_and_origins_are_converted_separately(self):
        source = self.folder / "two_seconds.wav"
        wav(source, [99] * 96000)
        path = self.folder / "rates.aaf"
        with aaf2.open(str(path), "w") as f:
            sm = f.create.SourceMob("Audio")
            f.content.mobs.append(sm)
            fs = sm.import_audio_essence(str(source))
            fs.origin = 48
            master = f.create.MasterMob("Master")
            f.content.mobs.append(master)
            ms = master.create_timeline_slot(48000, 8)
            ms.segment = f.create.SourceClip(480, 95520, sm.mob_id, fs.slot_id, "sound")
            comp = f.create.CompositionMob("Cross-rate")
            f.content.mobs.append(comp)
            cs = comp.create_timeline_slot(25, 2)
            cs.origin = 5
            cs.segment = f.create.SourceClip(25, 10, master.mob_id, 8, "sound")
        c = read_sessions(path)[0].tracks[0].clips[0]
        self.assertAlmostEqual(c.offset, 1.011)
        self.assertAlmostEqual(c.position, -.2)
        self.assertAlmostEqual(c.duration, .4)

    def test_aiff_big_endian_stereo_assembles_without_swapping_channels(self):
        # 80-bit extended representation of 48000 Hz.
        rate80 = bytes.fromhex("400ebb80000000000000")
        samples = [10, -100, 20, -200]
        comm = struct.pack(">HIH", 2, 2, 16) + rate80
        pcm = struct.pack(">4h", *samples)
        ssnd = struct.pack(">II", 0, 0) + pcm
        body = b"AIFF" + b"COMM" + struct.pack(">I", len(comm)) + comm + b"SSND" + struct.pack(">I", len(ssnd)) + ssnd
        source = self.folder / "source.aif"
        source.write_bytes(b"FORM" + struct.pack(">I", len(body)) + body)
        output, _ = assemble(source, 0, 0, self.folder, source, 1, 0)
        with PCMReader(output) as pcm:
            self.assertEqual(pcm.rate, 48000)
            self.assertEqual(pcm.channel(0, 2, 0, 2), struct.pack("<2h", 10, 20))
            self.assertEqual(pcm.channel(0, 2, 1, 2), struct.pack("<2h", -100, -200))

    def test_two_distinct_mono_file_mobs_remain_distinct(self):
        lpath, rpath = self.folder / "L.wav", self.folder / "R.wav"
        wav(lpath, [100] * 100)
        wav(rpath, [-200] * 100)
        path = self.folder / "mono.aaf"
        with aaf2.open(str(path), "w") as f:
            comp = f.create.CompositionMob("Split mono")
            f.content.mobs.append(comp)
            for i, audio in enumerate((lpath, rpath), 1):
                sm = f.create.SourceMob("same name")
                f.content.mobs.append(sm)
                source_slot = sm.import_audio_essence(str(audio))
                slot = comp.create_timeline_slot(48000, i)
                slot.name = "A%d" % i
                slot.segment = f.create.SourceClip(0, 100, sm.mob_id, source_slot.slot_id, "sound")
        s = read_sessions(path)[0]
        plan = materialize(s, {"1", "2"}, [("1", "2")], self.folder / "media")
        with PCMReader(plan["tracks"][0]["clips"][0]["path"]) as pcm:
            self.assertEqual(pcm.channel(0, 100, 0, 2), struct.pack("<100h", *([100] * 100)))
            self.assertEqual(pcm.channel(0, 100, 1, 2), struct.pack("<100h", *([-200] * 100)))

    def test_nested_master_sequence_preserves_cuts(self):
        source = self.folder / "source.wav"
        wav(source, [123] * 48000)
        path = self.folder / "nested.aaf"
        with aaf2.open(str(path), "w") as f:
            sm = f.create.SourceMob("Audio")
            f.content.mobs.append(sm)
            fs = sm.import_audio_essence(str(source))
            master = f.create.MasterMob("Nested edits")
            f.content.mobs.append(master)
            ms = master.create_timeline_slot(48000, 4)
            seq = f.create.Sequence("sound")
            seq.components.append(f.create.SourceClip(0, 24000, sm.mob_id, fs.slot_id, "sound"))
            seq.components.append(f.create.SourceClip(12000, 24000, sm.mob_id, fs.slot_id, "sound"))
            seq.length = 48000
            ms.segment = seq
            comp = f.create.CompositionMob("Timeline")
            f.content.mobs.append(comp)
            cs = comp.create_timeline_slot(48000, 1)
            cs.segment = f.create.SourceClip(12000, 24000, master.mob_id, 4, "sound")
        clips = read_sessions(path)[0].tracks[0].clips
        self.assertEqual([(c.position, c.duration, c.offset) for c in clips], [(0, .25, .25), (.25, .25, .25)])

    def test_channel_ids_on_master_reference_choose_right_channel(self):
        path, _, _, right = make_fixture(self.folder, "ambiguous")
        with aaf2.open(str(path), "rw") as f:
            master = next(f.content.mastermobs())
            master.slots[0].segment["ChannelIDs"].value = [1]
            master.slots[1].segment["ChannelIDs"].value = [2]
        s = read_sessions(path)[0]
        self.assertEqual([t.clips[0].channel for t in s.tracks], [0, 1])
        p = self.plan(s)
        with PCMReader(p["tracks"][0]["clips"][0]["path"]) as pcm:
            self.assertEqual(pcm.channel(0, len(right), 1, 2), b"".join(struct.pack("<h", n) for n in right))

    def test_channel_ids_propagate_through_intermediate_stereo_master(self):
        path, _, _, _ = make_fixture(self.folder, "ambiguous")
        with aaf2.open(str(path), "rw") as f:
            master = next(f.content.mastermobs())
            for slot in master.slots:
                slot.segment["ChannelIDs"].value = [1, 2]
            comp = next(f.content.compositionmobs())
            for i, slot in enumerate(s for s in comp.slots if s.media_kind == "Sound"):
                for child in slot.segment.components:
                    if type(child).__name__ == "SourceClip": child["ChannelIDs"].value = [i + 1]
        s = read_sessions(path)[0]
        self.assertEqual([t.clips[0].channel for t in s.tracks], [0, 1])

    def test_linear_source_clip_fade_enum_is_preserved(self):
        path, _, _, _ = make_fixture(self.folder)
        with aaf2.open(str(path), "rw") as f:
            comp = next(f.content.compositionmobs())
            for slot in comp.slots:
                if slot.media_kind == "Sound":
                    clip = slot.segment.components[1]
                    clip["FadeInLength"].value = 480
                    clip["FadeInType"].value = "FadeLinearAmp"
        s = read_sessions(path)[0]
        self.assertFalse(s.tracks[0].errors)
        self.assertFalse(s.tracks[0].warnings)
        self.assertEqual(s.tracks[0].clips[0].fade_in, .01)


if __name__ == "__main__":
    unittest.main(verbosity=2)
