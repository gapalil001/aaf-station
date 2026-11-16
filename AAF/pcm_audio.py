# @noindex
"""Small, lossless PCM WAV/AIFF reader and channel assembler (Python 3.9+)."""
import hashlib
import math
import os
from pathlib import Path
import struct
import wave


class AudioError(ValueError):
    pass


class PCMReader:
    """Read integer PCM, exposing little-endian signed 16/24/32-bit samples."""
    def __init__(self, path):
        self.path = str(path)
        self.file = open(path, "rb")
        self.big_endian = False
        try:
            self._header()
        except Exception:
            self.file.close()
            raise

    def _header(self):
        f = self.file
        header = f.read(12)
        if len(header) != 12:
            raise AudioError("Truncated audio: " + self.path)
        riff = header[:4] == b"RIFF" and header[8:] == b"WAVE"
        aiff = header[:4] == b"FORM" and header[8:] in (b"AIFF", b"AIFC")
        if not (riff or aiff):
            raise AudioError("Stereo assembly requires PCM WAV or AIFF: " + self.path)
        endian = "<" if riff else ">"
        fmt = data = None
        f.seek(0, 2)
        file_size = f.tell()
        f.seek(12)
        while f.tell() + 8 <= file_size:
            tag = f.read(4)
            size = struct.unpack(endian + "I", f.read(4))[0]
            start = f.tell()
            if start + size > file_size:
                raise AudioError("Truncated audio chunk: " + self.path)
            if tag in (b"fmt ", b"COMM"):
                fmt = f.read(min(size, 128))
            elif tag == b"data":
                data = (start, size)
            elif tag == b"SSND":
                if size < 8:
                    raise AudioError("Invalid AIFF sound chunk")
                offset, _ = struct.unpack(">II", f.read(8))
                if offset > size - 8:
                    raise AudioError("Invalid AIFF sample offset")
                data = (start + 8 + offset, size - 8 - offset)
            f.seek(start + size + (size & 1))
        if fmt is None or data is None:
            raise AudioError("Missing format/audio chunk: " + self.path)
        if riff:
            if len(fmt) < 16:
                raise AudioError("Invalid WAVE format")
            code, self.channels, self.rate, _, align, bits = struct.unpack("<HHIIHH", fmt[:16])
            if code == 0xFFFE and len(fmt) >= 40:
                # WAVEFORMATEXTENSIBLE: only integer PCM with full valid bits.
                valid = struct.unpack("<H", fmt[18:20])[0]
                if fmt[24:40] != bytes.fromhex("0100000000001000800000aa00389b71") or valid not in (0, bits):
                    raise AudioError("Unsupported WAVE extensible subtype/valid bits")
                code = 1
            if code != 1:
                raise AudioError("Stereo assembly requires integer PCM (float/compressed WAV unsupported)")
        else:
            if len(fmt) < 18:
                raise AudioError("Invalid AIFF format")
            self.channels, declared_frames, bits = struct.unpack(">HIH", fmt[:8])
            exponent, mantissa = struct.unpack(">HQ", fmt[8:18])
            rate = math.ldexp(mantissa, (exponent & 0x7FFF) - 16383 - 63)
            self.rate = round(rate)
            if exponent & 0x8000 or abs(rate - self.rate) > 1e-5:
                raise AudioError("Unsupported AIFF sample rate")
            compression = fmt[18:22] if header[8:] == b"AIFC" else b"NONE"
            if compression not in (b"NONE", b"twos", b"sowt"):
                raise AudioError("Compressed/float AIFF is unsupported for stereo assembly")
            self.big_endian = compression != b"sowt"
            align = self.channels * ((bits + 7) // 8)
        self.width = bits // 8
        if bits not in (16, 24, 32) or self.channels < 1 or self.rate < 1:
            raise AudioError("Only 16/24/32-bit integer PCM is supported for channel assembly")
        self.frame_size = self.width * self.channels
        if align != self.frame_size or data[1] % self.frame_size:
            raise AudioError("Invalid PCM block alignment")
        self.data_start, self.data_size = data
        self.frames = self.data_size // self.frame_size
        if aiff and declared_frames != self.frames:
            raise AudioError("AIFF frame count disagrees with sample data")

    def channel(self, start, count, channel, out_width):
        if not 0 <= channel < self.channels:
            raise AudioError("Requested channel does not exist: " + self.path)
        if out_width < self.width:
            raise AudioError("Refusing to reduce PCM bit depth")
        result = bytearray(count * out_width)
        first, last = max(0, start), min(self.frames, start + count)
        if first >= last:
            return result
        self.file.seek(self.data_start + first * self.frame_size)
        raw = self.file.read((last - first) * self.frame_size)
        if len(raw) != (last - first) * self.frame_size:
            raise AudioError("Audio changed or was truncated while reading")
        pad = b"\x00" * (out_width - self.width)
        for i in range(last - first):
            source = i * self.frame_size + channel * self.width
            sample = raw[source:source + self.width]
            if self.big_endian:
                sample = sample[::-1]
            dest = (first - start + i) * out_width
            result[dest:dest + out_width] = pad + sample
        return result

    def close(self):
        self.file.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def assemble(left_path, left_channel, left_offset, folder,
             right_path=None, right_channel=None, right_offset=0.0):
    """Create mono or stereo PCM, preserving available handles on both sources.

    Returns (filename, common source offset). No resampling, panning or summing.
    A channel is an explicit zero-based PCM index; never an AAF slot number.
    """
    readers = []
    try:
        readers.append(PCMReader(left_path))
        if right_path is not None:
            readers.append(PCMReader(right_path))
        rate = readers[0].rate
        if any(r.rate != rate for r in readers):
            raise AudioError("L/R sample rates differ; export both channels at the same rate")
        channels = [left_channel] + ([right_channel] if len(readers) == 2 else [])
        offsets = [left_offset] + ([right_offset] if len(readers) == 2 else [])
        frames = [round(x * rate) for x in offsets]
        if any(abs(x * rate - n) > 0.01 for x, n in zip(offsets, frames)):
            raise AudioError("Source offset is not sample aligned; refusing to move a channel")
        for reader, channel, frame in zip(readers, channels, frames):
            if channel is None:
                raise AudioError("Ambiguous channel mapping; select L/R explicitly")
            if not 0 <= channel < reader.channels or frame < 0 or frame > reader.frames:
                raise AudioError("Invalid source channel or source offset")
        common = max(frames)
        shifts = [common - n for n in frames]
        length = max(r.frames + shift for r, shift in zip(readers, shifts))
        width = max(r.width for r in readers)
        if length * width * len(readers) > 0xFFFFFFFF - 128:
            raise AudioError("Assembled WAV would exceed 4 GB; split the source before import")
        identity = [(str(Path(r.path).resolve()), os.stat(r.path).st_size,
                     os.stat(r.path).st_mtime_ns, c, s)
                    for r, c, s in zip(readers, channels, shifts)]
        digest = hashlib.sha256(repr(identity).encode()).hexdigest()[:24]
        output = Path(folder) / ("stereo_" if len(readers) == 2 else "mono_")
        output = output.with_name(output.name + digest + ".wav")
        if output.exists():
            with PCMReader(output) as cached:
                if (cached.frames, cached.channels, cached.width, cached.rate) != (length, len(readers), width, rate):
                    raise AudioError("Cached PCM file has unexpected properties: " + str(output))
            return str(output), common / rate
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(".wav.partial")
        try:
            with wave.open(str(temporary), "wb") as w:
                w.setparams((len(readers), width, rate, length, "NONE", "not compressed"))
                for start in range(0, length, 32768):
                    count = min(32768, length - start)
                    blocks = [r.channel(start - shift, count, c, width)
                              for r, shift, c in zip(readers, shifts, channels)]
                    if len(blocks) == 1:
                        w.writeframesraw(blocks[0])
                    else:
                        interleaved = bytearray(count * width * 2)
                        for i in range(count):
                            a, b = i * width, i * width * 2
                            interleaved[b:b + width] = blocks[0][a:a + width]
                            interleaved[b + width:b + 2 * width] = blocks[1][a:a + width]
                        w.writeframesraw(interleaved)
            os.replace(temporary, output)
        finally:
            if temporary.exists():
                temporary.unlink()
        return str(output), common / rate
    finally:
        for reader in readers:
            reader.close()
