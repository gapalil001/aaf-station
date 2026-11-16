# @noindex
"""AAF audio edit reader. Source references are resolved by MobID AND SlotID.

No use of SourceClip.walk(), slot-list indexes, or clip names to select audio.
All AAF time arithmetic uses Fraction until the final REAPER import plan.
"""
from dataclasses import dataclass, field, fields
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path, PureWindowsPath
import shutil
import sys
from typing import Optional
from urllib.parse import unquote, urlsplit
import wave

sys.path.insert(0, str(Path(__file__).parent / "vendor" / "pyaaf2-1.7.1-py2.py3-none-any.whl"))
from pcm_audio import PCMReader, AudioError, assemble


class ImportErrorAAF(ValueError):
    pass


PRO_TOOLS_CHANNEL_COMBINER = "6b46dd7a-132d-4856-ab21-8b751d8462ec"


def prop(obj, key, default=None):
    if key in obj:
        value = obj[key].value
        if value is not None:
            return value
    return default


def rate_of(slot):
    rate = Fraction(str(slot.edit_rate))
    if rate <= 0:
        raise ImportErrorAAF("Invalid AAF edit rate")
    return rate


@dataclass
class Asset:
    key: str
    mob_id: str
    slot_id: int
    name: str
    descriptor: str
    embedded: bool
    locators: list
    channels: int
    physical: object
    path: str = ""
    problem: str = ""
    sample_rate: float = 0
    bits: int = 0


@dataclass
class Clip:
    name: str
    position: float
    duration: float
    offset: float
    asset_key: str
    channel: object  # zero based PCM channel, or None = whole interleaved file
    gain: float = 1.0
    fade_in: float = 0.0
    fade_out: float = 0.0
    route: list = field(default_factory=list)
    stereo_right: Optional["Clip"] = None


@dataclass
class Track:
    id: str
    name: str
    slot_id: int
    edit_rate: str
    clips: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)


@dataclass
class Session:
    path: str
    id: str
    name: str
    tracks: list
    assets: dict
    timecode_zero: object = None
    preferred: bool = False


class LocatorResolver:
    def __init__(self, aaf_path, roots=()):
        self.base = Path(aaf_path).resolve().parent
        self.roots = [Path(p).expanduser().resolve() for p in roots]
        self.index = None

    @staticmethod
    def decode(locator):
        value = str(locator)
        if value.lower().startswith("file:"):
            parsed = urlsplit(value)
            value = unquote(parsed.path)
            if parsed.netloc and parsed.netloc.lower() != "localhost":
                value = "//" + parsed.netloc + value
            if os.name == "nt" and len(value) > 2 and value[0] == "/" and value[2] == ":":
                value = value[1:]
        elif "://" in value:
            return None
        return value.replace("\\", "/")

    def resolve(self, locators):
        names = set()
        for locator in locators:
            decoded = self.decode(locator)
            if not decoded:
                continue
            path = Path(decoded)
            names.add(PureWindowsPath(decoded).name.casefold())
            # Absolute locators have priority over basename relinking.
            candidates = [path] if path.is_absolute() else [self.base / path]
            candidates += [self.base / path.name, self.base / path.parent.name / path.name]
            candidates += [root / path.name for root in self.roots]
            for candidate in candidates:
                if candidate.is_file():
                    return str(candidate.resolve()), ""
        if self.roots:
            if self.index is None:
                self.index = {}
                for root in self.roots:
                    for directory, dirs, files in os.walk(root, followlinks=False):
                        dirs[:] = [d for d in dirs if not d.startswith(".")]
                        for name in files:
                            self.index.setdefault(name.casefold(), set()).add(str((Path(directory) / name).resolve()))
            matches = set()
            for name in names:
                matches.update(self.index.get(name, ()))
            if len(matches) == 1:
                return matches.pop(), ""
            if len(matches) > 1:
                return "", "Ambiguous relink: several files have this basename"
        return "", "Missing external media: " + ("; ".join(map(str, locators)) or "no locator")


def descriptor_for(mob, slot):
    descriptor = mob.descriptor
    if type(descriptor).__name__ == "MultipleDescriptor":
        items = list(prop(descriptor, "FileDescriptors", []))
        matches = [d for d in items if prop(d, "LinkedSlotID") == slot.slot_id]
        if len(matches) != 1:
            raise ImportErrorAAF("MultipleDescriptor has no unique LinkedSlotID for slot %s" % slot.slot_id)
        descriptor = matches[0]
    return descriptor


class Reader:
    def __init__(self, f, path, roots):
        self.f, self.path = f, str(Path(path).resolve())
        self.resolver = LocatorResolver(path, roots)
        self.assets = {}

    def asset(self, mob, slot, hint, selected_channel=None):
        descriptor = descriptor_for(mob, slot)
        kind = type(descriptor).__name__
        if kind not in ("PCMDescriptor", "WAVEDescriptor", "AIFCDescriptor", "SoundDescriptor"):
            raise ImportErrorAAF("Unsupported audio descriptor: " + kind)
        physical = prop(slot, "PhysicalTrackNumber", hint)
        channels = int(prop(descriptor, "Channels", 0))
        locators = [prop(loc, "URLString", "") for loc in prop(descriptor, "Locator", [])]
        embedded = self.f.content.essencedata.get(mob.mob_id, None) is not None
        if embedded and type(mob.descriptor).__name__ == "MultipleDescriptor":
            raise ImportErrorAAF("Embedded MultipleDescriptor audio requires rendering to separate WAV sources")
        key = "%s:%s" % (mob.mob_id, slot.slot_id)
        if key not in self.assets:
            asset = Asset(key, str(mob.mob_id), slot.slot_id, mob.name or "Audio",
                          kind, embedded, locators, channels, physical,
                          sample_rate=float(prop(descriptor, "AudioSamplingRate", prop(descriptor, "SampleRate", 0))),
                          bits=int(prop(descriptor, "QuantizationBits", 0)))
            if not embedded:
                asset.path, asset.problem = self.resolver.resolve(locators)
                if asset.path:
                    try:
                        with PCMReader(asset.path) as pcm:
                            asset.channels, asset.sample_rate, asset.bits = pcm.channels, pcm.rate, pcm.width * 8
                    except AudioError:
                        # Other linked formats may still be opened by REAPER; stereo assembly is stricter.
                        pass
            elif kind in ("WAVEDescriptor", "AIFCDescriptor"):
                # Descriptors often store channel count only inside the Summary header.
                summary = bytes(prop(descriptor, "Summary", b""))
                if kind == "WAVEDescriptor":
                    index = summary.find(b"fmt ")
                    if index >= 0 and len(summary) >= index + 12:
                        channels = int.from_bytes(summary[index + 10:index + 12], "little")
                else:
                    index = summary.find(b"COMM")
                    if index >= 0 and len(summary) >= index + 10:
                        channels = int.from_bytes(summary[index + 8:index + 10], "big")
                asset.channels = channels
            self.assets[key] = asset
        asset = self.assets[key]
        if selected_channel is not None:
            if asset.channels and not 0 <= selected_channel < asset.channels:
                raise ImportErrorAAF("ChannelIDs selects a channel absent from the source")
            channel = selected_channel
        elif asset.channels == 1:
            channel = 0
        elif physical is not None and asset.channels > 1:
            if not 1 <= int(physical) <= asset.channels:
                raise ImportErrorAAF("PhysicalTrackNumber exceeds source channel count")
            channel = int(physical) - 1
        else:
            channel = None
        return asset, channel

    def walk(self, segment, rate, begin, length, dest, track, route=(), stack=(),
             gain=1.0, name="", hint=None, depth=0, selected_channel=None):
        if depth > 64:
            raise ImportErrorAAF("AAF nesting exceeds 64 levels")
        if length <= 0:
            return []
        cls = type(segment).__name__
        child_args = dict(track=track, route=route, stack=stack, gain=gain,
                          name=name, hint=hint, depth=depth + 1, selected_channel=selected_channel)
        if cls == "Filler":
            return []
        if cls == "Sequence":
            clips = []
            for _, pos, child in segment.positions():
                start = Fraction(pos, 1) / rate
                duration = Fraction(child.length, 1) / rate
                if type(child).__name__ == "Transition":
                    if start < begin + length and start + duration > begin:
                        raise ImportErrorAAF("AAF Transition: render crossfades in Pro Tools before export (not approximated)")
                    continue
                lo, hi = max(begin, start), min(begin + length, start + duration)
                if lo < hi:
                    clips += self.walk(child, rate, lo - start, hi - lo,
                                       dest + lo - begin, **child_args)
            return clips
        if cls == "SourceClip":
            mob = segment.mob
            channel_ids = list(prop(segment, "ChannelIDs", []))
            mono_ids = list(prop(segment, "MonoSourceSlotIDs", []))
            if mono_ids:
                index = selected_channel if selected_channel is not None else 0
                if selected_channel is None and len(mono_ids) != 1:
                    raise ImportErrorAAF("Multiple MonoSourceSlotIDs require split-mono track export")
                if index >= len(mono_ids):
                    raise ImportErrorAAF("Channel selection exceeds MonoSourceSlotIDs")
                slot = mob.slot_at(mono_ids[index]) if mob is not None else None
                selected_channel = None
            else:
                slot = segment.slot
            if channel_ids:
                if any(int(n) < 1 for n in channel_ids):
                    raise ImportErrorAAF("Invalid ChannelIDs: channel numbering starts at 1")
                if selected_channel is not None:
                    if selected_channel >= len(channel_ids):
                        raise ImportErrorAAF("Channel selection exceeds ChannelIDs")
                    selected_channel = int(channel_ids[selected_channel]) - 1
                elif len(channel_ids) == 1:
                    selected_channel = int(channel_ids[0]) - 1
                elif channel_ids != [1, 2]:
                    raise ImportErrorAAF("Reordered/multichannel ChannelIDs require split-mono export")
            if mob is None or slot is None:
                raise ImportErrorAAF("Unresolved SourceClip MobID / SourceMobSlotID")
            marker = (str(mob.mob_id), slot.slot_id)
            if marker in stack:
                raise ImportErrorAAF("Cyclic AAF source references")
            target_rate = rate_of(slot)
            # StartTime is in the referring component's edit units; target Origin is in target edit units.
            source_start = Fraction(segment.start, 1) / rate + Fraction(slot.origin, 1) / target_rate + begin
            physical = prop(slot, "PhysicalTrackNumber", hint)
            step = {"mob": mob.name, "mob_id": str(mob.mob_id), "slot_id": slot.slot_id,
                    "physical_track": physical, "start_units": segment.start,
                    "context_rate": str(rate), "target_rate": str(target_rate), "target_origin": slot.origin,
                    "channel_ids": channel_ids, "mono_source_slot_ids": mono_ids,
                    "selected_channel_1based": None if selected_channel is None else selected_channel + 1}
            next_route = list(route) + [step]
            label = name or mob.name or "Audio"
            if type(mob).__name__ == "SourceMob" and hasattr(mob, "descriptor"):
                desc = mob.descriptor
                if type(desc).__name__ not in ("TapeDescriptor", "FilmDescriptor", "PhysicalDescriptor", "ImportDescriptor", "AuxiliaryDescriptor"):
                    asset, channel = self.asset(mob, slot, physical, selected_channel)
                    if source_start < 0:
                        raise ImportErrorAAF("Negative source offset: " + label)
                    clip = Clip(label, float(dest), float(length), float(source_start),
                                asset.key, channel, gain=gain, route=next_route)
                    self.fades(segment, rate, begin, length, clip, track)
                    return [clip]
            clips = self.walk(slot.segment, target_rate, source_start, length, dest,
                              track, next_route, stack + (marker,), gain, label, physical, depth + 1, selected_channel)
            self.outer_fades(segment, rate, begin, length, clips, track)
            return clips
        if cls == "OperationGroup":
            inputs = list(segment.segments)
            operation = segment.operation.name or str(segment.operation.auid)
            if str(segment.operation.auid) == PRO_TOOLS_CHANNEL_COMBINER:
                if len(inputs) != 2:
                    raise ImportErrorAAF("Audio Channel Combiner requires exactly two inputs for stereo")
                if any(part.length != segment.length for part in inputs):
                    raise ImportErrorAAF("Audio Channel Combiner inputs have different durations")
                if selected_channel is not None:
                    if selected_channel not in (0, 1):
                        raise ImportErrorAAF("Audio Channel Combiner has only two channels")
                    child_args["selected_channel"] = None
                    return self.walk(inputs[selected_channel], rate, begin, length, dest, **child_args)
                left, right = [self.walk(part, rate, begin, length, dest, **child_args)
                               for part in inputs]
                if len(left) != len(right):
                    raise ImportErrorAAF("Audio Channel Combiner inputs have different edits")
                for l, r in zip(left, right):
                    validate_stereo_edit(l, r)
                    l.stereo_right = r
                return left
            if len(inputs) != 1:
                raise ImportErrorAAF("Multi-input operation requires rendering: " + operation)
            if Fraction(inputs[0].length, 1) / rate != Fraction(segment.length, 1) / rate:
                raise ImportErrorAAF("Time-changing operation requires rendering: " + operation)
            if "audiogain" in operation.lower():
                params = list(segment.parameters)
                levels = [p for p in params if str(p.auid) in (
                    "e4962320-2267-11d3-8a4c-0050040ef7d2",  # Level
                    "e4962321-2267-11d3-8a4c-0050040ef7d2",  # Amplitude
                )]
                if len(levels) == 1 and type(levels[0]).__name__ == "ConstantValue":
                    level = float(levels[0].value)
                    if not math.isfinite(level) or level < 0:
                        raise ImportErrorAAF("Invalid audio gain")
                    child_args["gain"] *= level
                else:
                    track.warnings.append("Gain automation omitted: " + operation)
            else:
                if any(word in operation.lower() for word in ("speed", "warp", "stretch", "pulldown")):
                    raise ImportErrorAAF("Time-changing operation requires rendering: " + operation)
                track.warnings.append("Effect/pan omitted; dry audio only: " + operation)
            return self.walk(inputs[0], rate, begin, length, dest, **child_args)
        if cls == "Selector":
            selected = prop(segment, "Selected")
            if selected is None:
                raise ImportErrorAAF("Selector has no selected segment")
            return self.walk(selected, rate, begin, length, dest, **child_args)
        if cls == "EssenceGroup":
            choices = list(prop(segment, "Choices", []))
            if len(choices) != 1:
                raise ImportErrorAAF("EssenceGroup has multiple alternatives; render/select one in Pro Tools")
            return self.walk(choices[0], rate, begin, length, dest, **child_args)
        raise ImportErrorAAF("Unsupported AAF component: " + cls)

    def fades(self, segment, rate, begin, length, clip, track):
        original = Fraction(segment.length, 1) / rate
        for suffix, attr in (("In", "fade_in"), ("Out", "fade_out")):
            units = prop(segment, "Fade%sLength" % suffix, 0)
            shape = prop(segment, "Fade%sType" % suffix, 1)
            if units:
                if shape not in (1, "FadeLinearAmp"):
                    track.warnings.append("Nonlinear %s fade omitted" % suffix.lower())
                    continue
                fade = Fraction(units, 1) / rate
                # A partially trimmed ramp requires gain automation, not a fresh zero-to-one fade.
                boundary = begin if suffix == "In" else original - begin - length
                if boundary > 0 and boundary < fade:
                    raise ImportErrorAAF("Trimmed fade ramp requires rendering in Pro Tools")
                value = float(fade if boundary == 0 else 0)
                if value and getattr(clip, attr):
                    raise ImportErrorAAF("Nested audio fades require rendering in Pro Tools")
                setattr(clip, attr, min(float(length), max(getattr(clip, attr), value)))

    def outer_fades(self, segment, rate, begin, length, clips, track):
        if not clips:
            return
        if len(clips) > 1 and (prop(segment, "FadeInLength", 0) or prop(segment, "FadeOutLength", 0)):
            raise ImportErrorAAF("Fade spanning multiple nested clips requires rendering")
        if len(clips) == 1:
            clip = clips[0]
            self.fades(segment, rate, begin, length, clip, track)
            if clip.stereo_right is not None:
                self.fades(segment, rate, begin, length, clip.stereo_right, track)


def timecode_start(slot):
    rate = rate_of(slot)
    segment = slot.segment
    if type(segment).__name__ == "Timecode":
        return float(Fraction(segment.start + slot.origin, 1) / rate)
    if type(segment).__name__ == "Sequence":
        for _, pos, child in segment.positions():
            if type(child).__name__ == "Timecode":
                return float(Fraction(child.start - pos + slot.origin, 1) / rate)
    return None


def read_sessions(path, roots=()):
    import aaf2

    sessions = []
    with aaf2.open(str(path), "r") as f:
        for mob in f.content.compositionmobs():
            reader = Reader(f, path, roots)
            tracks, tc = [], None
            for slot in mob.slots:
                kind = (slot.media_kind or "").lower()
                if kind == "timecode":
                    value = timecode_start(slot)
                    if value is not None:
                        tc = value
                    continue
                if kind != "sound":
                    continue
                track = Track(str(slot.slot_id), slot.name or "Audio %s" % slot.slot_id,
                              slot.slot_id, str(slot.edit_rate))
                try:
                    rate = rate_of(slot)
                    track.clips = reader.walk(slot.segment, rate, Fraction(0),
                                              Fraction(slot.segment.length, 1) / rate,
                                              -Fraction(slot.origin, 1) / rate, track)
                    track.clips.sort(key=lambda c: (c.position, c.duration))
                    track.warnings = list(dict.fromkeys(track.warnings))
                except Exception as exc:
                    track.errors.append(str(exc))
                tracks.append(track)
            if tracks:
                sessions.append(Session(reader.path, str(mob.mob_id), mob.name or "AAF session",
                                        tracks, reader.assets, tc,
                                        "toplevel" in str(prop(mob, "UsageCode", "")).lower()))
    if not sessions:
        raise ImportErrorAAF("No CompositionMob with audio tracks found")
    return sorted(sessions, key=lambda s: (not s.preferred, -len(s.tracks)))


def validate_stereo_edit(left, right):
    if left.stereo_right is not None or right.stereo_right is not None:
        raise ImportErrorAAF("The inputs must be individual channels. This clip already contains stereo.")
    if abs(left.position - right.position) > 1e-9 or abs(left.duration - right.duration) > 1e-9:
        raise ImportErrorAAF("L/R edits are not aligned")
    if any(abs(getattr(left, key) - getattr(right, key)) > 1e-9 for key in ("gain", "fade_in", "fade_out")):
        raise ImportErrorAAF("L/R gain/fades differ; render these in Pro Tools first")


def matching_pair(left, right):
    if left.errors or right.errors:
        return "One of the tracks has an unsupported AAF structure"
    if not left.clips or len(left.clips) != len(right.clips):
        return "L/R clip counts differ or tracks are empty"
    for i, (l, r) in enumerate(zip(left.clips, right.clips), 1):
        try:
            validate_stereo_edit(l, r)
        except ImportErrorAAF as exc:
            return "%s (clip %d)" % (exc, i)
    return ""


def extract_asset(f, asset, folder):
    if not asset.embedded:
        if not asset.path or not Path(asset.path).is_file():
            raise ImportErrorAAF(asset.problem or "Missing media: " + asset.name)
        return asset.path
    from aaf2.mobid import MobID

    mob = f.content.mobs.get(MobID(asset.mob_id))
    slot = mob.slot_at(asset.slot_id)
    descriptor = descriptor_for(mob, slot)
    extension = ".aif" if asset.descriptor == "AIFCDescriptor" else ".wav"
    aaf_path = getattr(f.f, "name", "")
    stat = Path(aaf_path).stat() if aaf_path else None
    identity = (str(aaf_path), stat.st_size if stat else 0,
                stat.st_mtime_ns if stat else 0, asset.mob_id, asset.descriptor)
    digest = hashlib.sha256(repr(identity).encode()).hexdigest()[:24]
    output = Path(folder) / ("essence_" + digest + extension)
    if output.exists():
        return str(output)
    temporary = output.with_suffix(extension + ".partial")
    essence = f.content.essencedata.get(mob.mob_id)
    stream = essence.open("r")
    try:
        if asset.descriptor == "PCMDescriptor":
            channels = int(prop(descriptor, "Channels", 0))
            width = int(prop(descriptor, "QuantizationBits", 0)) // 8
            sample_rate = round(float(prop(descriptor, "AudioSamplingRate", prop(descriptor, "SampleRate", 0))))
            if channels < 1 or width not in (2, 3, 4) or sample_rate < 1:
                raise ImportErrorAAF("Unsupported embedded PCM format")
            alignment = channels * width
            block_align = prop(descriptor, "BlockAlign", alignment)
            if block_align != alignment:
                raise ImportErrorAAF("Unsupported embedded PCM padding")
            with wave.open(str(temporary), "wb") as target:
                target.setnchannels(channels)
                target.setsampwidth(width)
                target.setframerate(sample_rate)
                while True:
                    block = stream.read(alignment * 32768)
                    if not block:
                        break
                    if len(block) % alignment:
                        raise ImportErrorAAF("Embedded PCM stream is not frame aligned")
                    if target.tell() * alignment + len(block) > 0xFFFFFFFF - 128:
                        raise ImportErrorAAF("Embedded PCM exceeds WAV 4 GB limit")
                    target.writeframesraw(block)
        elif asset.descriptor in ("WAVEDescriptor", "AIFCDescriptor"):
            with open(temporary, "wb") as target:
                while True:
                    block = stream.read(1024 * 1024)
                    if not block:
                        break
                    target.write(block)
        else:
            raise ImportErrorAAF("Embedded format unsupported: " + asset.descriptor)
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()
    return str(output)


def same_file(left, right):
    return os.path.samefile(left, right)


def materialize(session, selected, pairs, folder, allow_dry=False, copy_linked=True):
    """Prepare only selected media and return an import plan."""
    import aaf2

    folder = Path(folder).expanduser().resolve()
    folder.mkdir(parents=True, exist_ok=True)
    chosen = [t for t in session.tracks if t.id in selected]
    if not chosen:
        raise ImportErrorAAF("No tracks selected")
    paired = {}
    for left_id, right_id in pairs:
        if left_id not in selected or right_id not in selected:
            raise ImportErrorAAF("Both channels of a stereo pair must be selected")
        if left_id == right_id or left_id in paired or right_id in paired:
            raise ImportErrorAAF("A track belongs to more than one stereo pair")
        paired[left_id] = (left_id, right_id)
        paired[right_id] = (left_id, right_id)
    for track in chosen:
        if track.errors:
            raise ImportErrorAAF(track.name + ": " + "; ".join(track.errors))
        if track.warnings and not allow_dry:
            raise ImportErrorAAF(track.name + ": review warnings and enable dry import, or render in Pro Tools")
    by_id = {t.id: t for t in chosen}
    plan = {"version": 1, "source": session.path, "session": session.name,
            "timecode_zero": session.timecode_zero, "tracks": [], "warnings": []}
    for t in chosen:
        plan["warnings"] += [t.name + ": " + w for w in t.warnings]
    paths = {}
    with aaf2.open(session.path, "r") as f:
        def effective_channel(clip, pcm):
            if clip.channel is not None:
                return clip.channel
            physical = clip.route[-1].get("physical_track") if clip.route else None
            if physical is not None and pcm.channels > 1:
                if not 1 <= int(physical) <= pcm.channels:
                    raise ImportErrorAAF("PhysicalTrackNumber exceeds actual source channel count")
                return int(physical) - 1
            return 0 if pcm.channels == 1 else None

        def media(clip):
            asset = session.assets[clip.asset_key]
            if asset.key not in paths:
                path = extract_asset(f, asset, folder)
                if copy_linked and not asset.embedded:
                    status = Path(path).stat()
                    digest = hashlib.sha256(repr((str(Path(path).resolve()), status.st_size, status.st_mtime_ns)).encode()).hexdigest()[:24]
                    copied = folder / ("linked_" + digest + Path(path).suffix.lower())
                    if not copied.exists():
                        temporary = copied.with_suffix(copied.suffix + ".partial")
                        try:
                            shutil.copy2(path, temporary)
                            os.replace(temporary, copied)
                        finally:
                            if temporary.exists():
                                temporary.unlink()
                    path = str(copied)
                paths[asset.key] = path
            return paths[asset.key]

        def finish(clip, path, offset):
            item = {entry.name: getattr(clip, entry.name) for entry in fields(clip)
                    if entry.name not in ("route", "stereo_right")}
            item.update(path=path, offset=offset)
            # Validate available source range for PCM before touching the project.
            try:
                with PCMReader(path) as pcm:
                    if offset < 0 or offset + clip.duration > pcm.frames / pcm.rate + 1.01 / pcm.rate:
                        raise ImportErrorAAF("Clip extends beyond source samples: " + clip.name)
            except AudioError:
                if not Path(path).is_file():
                    raise
            return item

        def stereo_item(left, right):
            lp, rp = media(left), media(right)
            la, ra = session.assets[left.asset_key], session.assets[right.asset_key]
            shared = same_file(lp, rp) or (la.embedded and ra.embedded and la.mob_id == ra.mob_id)
            with PCMReader(lp) as lpcm, PCMReader(rp) as rpcm:
                lc, rc = effective_channel(left, lpcm), effective_channel(right, rpcm)
                if shared and lpcm.channels == 2 and lc is None and rc is None:
                    lc, rc = 0, 1
                if lc is None or rc is None:
                    raise ImportErrorAAF("Ambiguous interleaved channel: cannot pair " + left.name + " / " + right.name)
                if shared and lc == rc:
                    raise ImportErrorAAF("Both A1/A2 resolve to the SAME source channel. Stereo import stopped.")
                for clip, pcm in ((left, lpcm), (right, rpcm)):
                    if clip.offset < 0 or clip.offset + clip.duration > pcm.frames / pcm.rate + 1.01 / pcm.rate:
                        raise ImportErrorAAF("Stereo clip extends beyond source samples: " + clip.name)
            path, offset = assemble(lp, lc, left.offset, folder, rp, rc, right.offset)
            item = finish(left, path, offset)
            item["stereo"] = True
            return item

        emitted = set()
        for track in chosen:
            if track.id in emitted:
                continue
            out = {"name": track.name, "clips": [], "stereo": False}
            if track.id in paired:
                left_id, right_id = paired[track.id]
                left, right = by_id[left_id], by_id[right_id]
                problem = matching_pair(left, right)
                if problem:
                    raise ImportErrorAAF(problem)
                out["name"] = left.name + " + " + right.name + " [stereo]"
                out["stereo"] = True
                for l, r in zip(left.clips, right.clips):
                    out["clips"].append(stereo_item(l, r))
                emitted.update((left_id, right_id))
            else:
                for clip in track.clips:
                    if clip.stereo_right is not None:
                        out["clips"].append(stereo_item(clip, clip.stereo_right))
                        continue
                    path = media(clip)
                    with PCMReader(path) as pcm:
                        channel = effective_channel(clip, pcm)
                        if channel is not None and not 0 <= channel < pcm.channels:
                            raise ImportErrorAAF("Requested source channel does not exist")
                        if channel is not None and pcm.channels > 1:
                            path, offset = assemble(path, channel, clip.offset, folder)
                        else:
                            offset = clip.offset
                    out["clips"].append(finish(clip, path, offset))
                emitted.add(track.id)
            plan["tracks"].append(out)
    return plan


def write_json(path, data):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".partial")
    with open(temporary, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
    os.replace(temporary, path)
