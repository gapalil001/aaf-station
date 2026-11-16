# @noindex
"""Plan and write editable audio AAFs from an immutable REAPER snapshot."""
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from fractions import Fraction
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import wave

from aaf_core import PRO_TOOLS_CHANNEL_COMBINER
from pcm_audio import AudioError, PCMReader


@dataclass
class ExportClip:
    name: str
    path: str
    start: int
    length: int
    source_start: int
    channels: tuple
    gain: float
    fade_in: int
    fade_out: int
    rate: int
    error: str = ""
    render_fades: tuple = ()

    @property
    def position(self):
        return self.start / self.rate

    @property
    def duration(self):
        return self.length / self.rate

    @property
    def offset(self):
        return self.source_start / self.rate


@dataclass
class ExportTrack:
    id: str
    name: str
    clips: list = field(default_factory=list)


@dataclass
class ExportPlan:
    name: str
    rate: int
    length: int
    tc_rate: Fraction
    tc_start: int
    drop: bool
    tracks: list
    warnings: list
    errors: list

    @property
    def timecode_zero(self):
        return float(self.tc_start / self.tc_rate)


def output_channels(count, mode):
    if mode == 3:
        return (0,) if count else ()
    if mode == 4:
        return (1,) if count >= 2 else ()
    if mode in (0, 1) and count in (1, 2):
        return tuple(range(count))[::-1] if mode == 1 else tuple(range(count))
    return ()


def frame_rate(value):
    if not math.isfinite(value) or value <= 0:
        return Fraction(30)
    for nominal in (24, 30, 60, 120):
        ntsc = Fraction(nominal * 1000, 1001)
        if abs(float(ntsc) - value) < 0.01:
            return ntsc
    return Fraction(value).limit_denominator(1001)


def clipped_fade(fade, cut, length, rendered):
    """An intact edge retains its fade. An interrupted ramp needs rendering."""
    if fade == 0 or cut >= fade:
        return 0, False
    partial = cut > 0 or fade > length
    if partial and not rendered:
        return 0, True
    return min(fade - cut, length), partial


def plan_export(snapshot, selected, bounds, rate, keep_positions=False, split_stereo=False, render_fades=True):
    """Build the same clip lists consumed by the canvas and the AAF writer."""
    begin, end = (round(value * rate) for value in bounds)
    if begin < 0 or end <= begin:
        raise ValueError("Choose a non-empty range on or after the start of the project.")
    origin = 0 if keep_positions else begin
    tc_rate = frame_rate(snapshot["fps"])
    tc_start = round((snapshot["timecode"] + origin / rate) * tc_rate)
    tracks, warnings, errors = [], [], []
    used_names = set()

    def track_name(label):
        candidate = label
        count = 2
        while candidate.casefold() in used_names:
            candidate = f"{label} ({count})"
            count += 1
        used_names.add(candidate.casefold())
        return candidate

    for track in snapshot["tracks"]:
        if track["id"] not in selected:
            continue
        lanes = []
        for item in sorted(track["clips"], key=lambda clip: clip["position"]):
            item_start = round(item["position"] * rate)
            item_end = round((item["position"] + item["duration"]) * rate)
            first, last = max(begin, item_start), min(end, item_end)
            if last <= first:
                continue
            cut_in, cut_out = first - item_start, item_end - last
            length = last - first
            source_start = round(item["offset"] * rate) + cut_in
            channels = output_channels(item["channels"], item["channel_mode"])
            description = track["name"] + " / " + item["name"]
            error = item["error"]
            if item["gain"] < 0:
                error = "Glue this item to commit take polarity inversion."
            if not channels and not error:
                error = "Glue to a mono or stereo file to commit this channel mode."
            if not error and source_start < 0:
                error = "Glue this item to commit a negative source offset."
            elif not error and source_start + length > round(item["source_length"] * rate) + 1:
                error = ("Glue this item to commit its source loop." if item["loop"] else
                         "The item extends beyond the source file.")
            if error:
                errors.append(description + " — " + error)
            warnings.extend(description + " — " + note for note in item["notes"])
            full_length = item_end - item_start
            full_in = min(full_length, round(item["fade_in"] * rate))
            full_out = min(full_length, round(item["fade_out"] * rate))
            fade_in, partial_in = clipped_fade(full_in, cut_in, length, render_fades)
            fade_out, partial_out = clipped_fade(full_out, cut_out, length, render_fades)
            if not render_fades and (partial_in or partial_out):
                warnings.append(description + " — A fade intersected by the range is omitted.")
            render = ((source_start - cut_in, full_length, full_in, full_out)
                      if render_fades and (full_in or full_out) else ())
            clip = ExportClip(item["name"], item["path"], first - origin, length, source_start,
                              channels, item["gain"], fade_in, fade_out, rate, error, render)
            for lane in lanes:
                previous = lane[-1]
                if len(previous.channels) == len(channels) and previous.start + previous.length <= clip.start:
                    lane.append(clip)
                    break
            else:
                lanes.append([clip])
        if not lanes:
            continue
        warnings.extend(track["name"] + " — " + note for note in track["warnings"])
        if len(lanes) > 1:
            warnings.append(track["name"] + " — Overlaps or mixed mono/stereo clips span separate output tracks.")
        for i, lane in enumerate(lanes, 1):
            label = track["name"] if i == 1 else f"{track['name']} ({i})"
            if split_stereo and len(lane[0].channels) == 2:
                for side, channel in enumerate(("L", "R")):
                    tracks.append(ExportTrack(str(len(tracks) + 1), track_name(f"{label}.{channel}"),
                                              [replace(c, channels=(c.channels[side],)) for c in lane]))
            else:
                tracks.append(ExportTrack(str(len(tracks) + 1), track_name(label), lane))
    nominal = round(float(tc_rate))
    drop = snapshot["drop"] and tc_rate in (Fraction(30000, 1001), Fraction(60000, 1001))
    if nominal not in range(1, 121):
        raise ValueError("The project frame rate is outside the AAF timecode range.")
    return ExportPlan(snapshot["name"], rate, end - origin, tc_rate, tc_start, drop,
                      tracks, list(dict.fromkeys(warnings)), list(dict.fromkeys(errors)))


def safe_name(name):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")[:50] or "Audio"


def pcm_source(path, rate, folder, cache):
    if path in cache:
        return cache[path]
    if not Path(path).is_file():
        raise ValueError("Source file is offline: " + path)
    try:
        with PCMReader(path) as source:
            if source.rate == rate:
                cache[path] = path
                return path
    except AudioError:
        pass
    if not Path("/usr/bin/afconvert").is_file():
        raise ValueError("Convert this source to integer PCM WAV at the export sample rate: " + path)
    target = folder / f"convert-{len(cache):05d}.wav"
    outcome = subprocess.run(["/usr/bin/afconvert", "-f", "WAVE", "-d", f"LEI24@{rate}", "-r", "127",
                              path, str(target)], capture_output=True, text=True)
    if outcome.returncode:
        raise ValueError("Could not decode or resample " + path + "\n" + outcome.stderr.strip())
    cache[path] = target
    return target


def render_edge_fades(data, source_start, spec):
    item_start, item_length, fade_in, fade_out = spec
    item_end = item_start + item_length
    block_end = source_start + len(data) // 3
    # Avoid a per-sample loop over the untouched middle of long clips.
    spans = [(item_start, item_start + fade_in), (item_end - fade_out, item_end)]
    if spans[0][1] > spans[1][0]:
        spans = [(item_start, item_end)]
    for start, end in spans:
        for frame in range(max(start, source_start), min(end, block_end)):
            factor = 1.0
            if fade_in:
                factor *= min(1.0, (frame - item_start) / fade_in)
            if fade_out:
                factor *= min(1.0, (item_end - frame) / fade_out)
            index = (frame - source_start) * 3
            sample = int.from_bytes(data[index:index + 3], "little", signed=True)
            data[index:index + 3] = round(sample * factor).to_bytes(3, "little", signed=True)


def copy_pcm_channels(source, first, count, paths, channels, pad=0, fades=()):
    if (count + pad) * 3 > 0xFFFFFFFF - 128:
        raise ValueError("A consolidated mono WAV would exceed the RIFF file size limit.")
    with ExitStack() as stack:
        outputs = [stack.enter_context(wave.open(str(path), "wb")) for path in paths]
        for out in outputs:
            out.setparams((1, 3, source.rate, count + pad, "NONE", "not compressed"))
        source.file.seek(source.data_start + first * source.frame_size)
        for start in range(0, count, 65536):
            n = min(65536, count - start)
            raw = source.file.read(n * source.frame_size)
            if len(raw) != n * source.frame_size:
                raise ValueError("The audio file changed while reading: " + source.path)
            for out, channel in zip(outputs, channels):
                if source.channels == 1 and source.width == 3 and not source.big_endian:
                    data = bytearray(raw) if fades else raw
                else:
                    data = bytearray(n * 3)
                    for byte in range(max(0, 3 - source.width), 3):
                        offset = byte + source.width - 3
                        if source.big_endian:
                            offset = source.width - 1 - offset
                        base = channel * source.width + offset
                        data[byte::3] = raw[base::source.frame_size]
                if fades:
                    render_edge_fades(data, first + start, fades)
                out.writeframesraw(data)
        if pad:
            for out in outputs:
                out.writeframesraw(b"\0" * (pad * 3))
    # RIFF chunks are word aligned. wave writes exactly the supplied data bytes.
    if (count + pad) % 2:
        for path in paths:
            with path.open("r+b") as stream:
                stream.seek(0, os.SEEK_END)
                stream.write(b"\0")
                riff_size = stream.tell() - 8
                stream.seek(4)
                stream.write(riff_size.to_bytes(4, "little"))


def master_reference(file, path, media_dir, embedded, name, frames, rate, offset, length):
    master = file.create.MasterMob(name)
    file.content.mobs.append(master)
    source = file.create.SourceMob(path.stem)
    file.content.mobs.append(source)
    if embedded:
        source_slot = source.import_audio_essence(str(path))
        source.descriptor["ContainerFormat"].value = file.dictionary.lookup_containerdef("AAF")
    else:
        descriptor = file.create.WAVEDescriptor()
        descriptor["SampleRate"].value = rate
        descriptor["Length"].value = frames
        # Include the data chunk header so readers know the source sample count
        # from the summary. These mono WAVs have a fixed 44-byte header.
        with path.open("rb") as wave_file:
            descriptor["Summary"].value = wave_file.read(44)
        descriptor["ContainerFormat"].value = file.dictionary.lookup_containerdef("RIFFWAVE")
        locator = file.create.NetworkLocator()
        locator["URLString"].value = (media_dir / path.name).as_uri()
        descriptor["Locator"].append(locator)
        source.descriptor = descriptor
        source_slot = source.create_empty_slot(rate, "sound")
        source_slot.segment.length = frames
    source_slot["PhysicalTrackNumber"].value = 1
    master_slot = master.create_empty_sequence_slot(rate, media_kind="sound")
    master_slot["PhysicalTrackNumber"].value = 1
    master_slot.segment.components.append(source.create_source_clip(source_slot.slot_id, length=frames))
    master_slot.segment.length = frames
    return master.create_source_clip(master_slot.slot_id, start=offset, length=length)


def write_aaf(plan, destination, *, embedded=False, handles_ms=1000):
    import aaf2

    if plan.errors:
        raise ValueError(plan.errors[0])
    if not plan.tracks:
        raise ValueError("No audio clips in the selected range.")
    destination = Path(destination).expanduser().absolute()
    rate = plan.rate
    handles = round(rate * handles_ms / 1000)
    media_dir = None
    if not embedded:
        media_dir = destination.with_name(safe_name(destination.stem) + " Audio Files")
        sequence = 2
        while media_dir.exists():
            media_dir = destination.with_name(f"{safe_name(destination.stem)} Audio Files {sequence}")
            sequence += 1
    with tempfile.TemporaryDirectory(prefix=".aaf-station-", dir=destination.parent) as temporary:
        stage = Path(temporary)
        staged_aaf = stage / "session.aaf"
        staged_media = stage / "audio"
        staged_media.mkdir()
        cache = {}
        with aaf2.open(str(staged_aaf), "w", extensions=False) as file:
            identity = file.header["IdentificationList"][0]
            identity["ProductName"].value = "AAF Station"
            identity["CompanyName"].value = "AAF Station"
            identity["ProductVersionString"].value = "0.2.1"
            version = dict(major=0, minor=2, tertiary=1, patchLevel=0, type="VersionBeta")
            identity["ProductVersion"].value = version
            file.content["EssenceData"].value = []
            composition = file.create.CompositionMob(plan.name)
            file.content.mobs.append(composition)
            composition.usage = "Usage_TopLevel"
            tc = composition.create_timeline_slot(str(plan.tc_rate))
            tc.segment = file.create.Timecode(round(float(plan.tc_rate)), plan.drop,
                                              math.ceil(Fraction(plan.length, rate) * plan.tc_rate))
            tc.name = "Timecode"
            tc.segment.start = plan.tc_start
            combiner = None
            gain_def, level_def = None, None
            serial = 0
            for i, track in enumerate(plan.tracks, 1):
                slot = composition.create_empty_sequence_slot(rate, media_kind="sound")
                slot.name = track.name
                slot["PhysicalTrackNumber"].value = i
                end = 0
                for clip in track.clips:
                    if clip.start > end:
                        slot.segment.components.append(file.create.Filler("sound", clip.start - end))
                    media_path = pcm_source(clip.path, rate, stage, cache)
                    with PCMReader(media_path) as pcm:
                        if max(clip.channels) >= pcm.channels:
                            raise ValueError("The source channel layout changed: " + clip.path)
                        first = max(0, clip.source_start - handles)
                        requested_end = clip.source_start + clip.length
                        if requested_end > pcm.frames + 1 or clip.source_start >= pcm.frames:
                            raise ValueError("The clip extends beyond the decoded media: " + clip.name)
                        last = min(pcm.frames, requested_end + handles)
                        count = last - first
                        serial += 1
                        paths = [staged_media / f"{serial:05d}_{safe_name(clip.name)}{suffix}.wav"
                                 for suffix in (("",) if len(clip.channels) == 1 else (".L", ".R"))]
                        # Resampling may round the source tail down by one sample.
                        pad = max(0, requested_end - last)
                        copy_pcm_channels(pcm, first, count, paths, clip.channels, pad, clip.render_fades)
                        count += pad
                    segments = []
                    for j, path in enumerate(paths):
                        name = clip.name + ((".L", ".R")[j] if len(paths) == 2 else "")
                        segment = master_reference(file, path, media_dir, embedded, name, count, rate,
                                                   clip.source_start - first, clip.length)
                        for kind, frames in (("In", clip.fade_in), ("Out", clip.fade_out)):
                            if frames and not clip.render_fades:
                                segment["Fade" + kind + "Length"].value = frames
                                segment["Fade" + kind + "Type"].value = "FadeLinearAmp"
                        segments.append(segment)
                        if embedded:
                            path.unlink()
                    if len(segments) == 2:
                        if combiner is None:
                            combiner = file.create.OperationDef(PRO_TOOLS_CHANNEL_COMBINER, "Audio Channel Combiner")
                            combiner.media_kind = "sound"
                            combiner.number_inputs = 2
                            file.dictionary.register_def(combiner)
                        operation = file.create.OperationGroup(combiner, clip.length, "sound")
                        operation.segments.extend(segments)
                        segment = operation
                    else:
                        segment = segments[0]
                    if abs(clip.gain - 1) > 1e-9:
                        if gain_def is None:
                            gain_def = file.create.OperationDef("9d2ea894-0968-11d3-8a38-0050040ef7d2",
                                                                "MonoAudioGain")
                            gain_def.media_kind = "sound"
                            gain_def.number_inputs = 1
                            level_def = file.create.ParameterDef("e4962321-2267-11d3-8a4c-0050040ef7d2",
                                                                 "Amplitude", typedef="Rational")
                            file.dictionary.register_def(level_def)
                            file.dictionary.register_def(gain_def)
                            gain_def.parameters.append(level_def)
                        gain = file.create.OperationGroup(gain_def, clip.length, "sound")
                        gain.segments.append(segment)
                        level = Fraction(clip.gain).limit_denominator(100000)
                        gain.parameters.append(file.create.ConstantValue(level_def, level))
                        segment = gain
                    slot.segment.components.append(segment)
                    end = clip.start + clip.length
                if end < plan.length:
                    slot.segment.components.append(file.create.Filler("sound", plan.length - end))
                slot.segment.length = plan.length
        if media_dir is not None:
            staged_media.rename(media_dir)
        try:
            os.replace(staged_aaf, destination)
        except Exception:
            if media_dir is not None:
                shutil.rmtree(media_dir)
            raise
    return destination, media_dir
