#!/usr/bin/env python3
"""Original retro 8-bit alert sounds for Miner Hearth, synthesized in pure Python.

No numpy, no bundled audio files: every sound is generated from short, original
note sequences (square / triangle / noise channels, like a classic 8-bit sound
chip) and written as 16-bit mono WAV in memory. These melodies were composed for
this program and are not transcriptions of any game's audio, so they are free to
use and redistribute with the program.
"""

from __future__ import annotations

import io
import math
import random
import struct
import threading
import wave
from typing import Optional

SAMPLE_RATE = 22050

# Note frequencies (equal temperament, A4 = 440 Hz)
_NOTE_INDEX = {"C": -9, "C#": -8, "D": -7, "D#": -6, "E": -5, "F": -4, "F#": -3, "G": -2, "G#": -1, "A": 0, "A#": 1, "B": 2}


def note_hz(name: str) -> float:
    """'A4' -> 440.0, 'C#6' -> 1108.7. 'R' (rest) -> 0."""
    if not name or name.upper() == "R":
        return 0.0
    letter = name[:-1]
    octave = int(name[-1])
    semis = _NOTE_INDEX[letter] + (octave - 4) * 12
    return 440.0 * (2.0 ** (semis / 12.0))


def _square(phase: float, duty: float) -> float:
    return 1.0 if (phase % 1.0) < duty else -1.0


def _triangle(phase: float) -> float:
    p = phase % 1.0
    return 4.0 * p - 1.0 if p < 0.5 else 3.0 - 4.0 * p


def _render_notes(
    notes: list[tuple[str, float]],
    wave_kind: str = "square",
    duty: float = 0.25,
    volume: float = 0.5,
    decay: float = 0.0,
    gap: float = 0.012,
    vibrato_hz: float = 0.0,
    vibrato_depth: float = 0.0,
) -> list[float]:
    out: list[float] = []
    for name, dur in notes:
        hz = note_hz(name)
        n = int(SAMPLE_RATE * dur)
        gap_n = min(n, int(SAMPLE_RATE * gap))
        phase = 0.0
        for i in range(n):
            t = i / SAMPLE_RATE
            if hz <= 0 or i >= n - gap_n:
                out.append(0.0)
                continue
            f = hz * (1.0 + vibrato_depth * math.sin(2 * math.pi * vibrato_hz * t)) if vibrato_hz else hz
            phase += f / SAMPLE_RATE
            env = math.exp(-decay * t) if decay else 1.0
            # tiny attack to avoid clicks
            if i < 40:
                env *= i / 40.0
            v = _square(phase, duty) if wave_kind == "square" else _triangle(phase)
            out.append(v * env * volume)
    return out


def _sweep(start_hz: float, end_hz: float, dur: float, steps: int, duty: float = 0.5, volume: float = 0.45) -> list[float]:
    """Stepped pitch sweep (classic chip-tune 'power up' style)."""
    out: list[float] = []
    step_n = max(1, int(SAMPLE_RATE * dur / steps))
    phase = 0.0
    for s in range(steps):
        hz = start_hz * ((end_hz / start_hz) ** (s / max(1, steps - 1)))
        for i in range(step_n):
            phase += hz / SAMPLE_RATE
            env = 1.0 - 0.5 * (s / steps)
            out.append(_square(phase, duty) * volume * env)
    return out


def _noise(dur: float, volume: float = 0.25, decay: float = 18.0, seed: int = 7) -> list[float]:
    rng = random.Random(seed)
    n = int(SAMPLE_RATE * dur)
    out = []
    val = 0.0
    for i in range(n):
        if i % 6 == 0:  # chip-style low-rate noise
            val = rng.uniform(-1.0, 1.0)
        out.append(val * volume * math.exp(-decay * i / SAMPLE_RATE))
    return out


def _mix(*tracks: list[float]) -> list[float]:
    length = max(len(t) for t in tracks)
    out = [0.0] * length
    for t in tracks:
        for i, v in enumerate(t):
            out[i] += v
    return out


def _concat(*parts: list[float]) -> list[float]:
    out: list[float] = []
    for p in parts:
        out.extend(p)
    return out


def _to_wav(samples: list[float], crush_bits: int = 6) -> bytes:
    """Quantize to `crush_bits` (8-bit flavour) then store as 16-bit PCM WAV."""
    peak = max((abs(s) for s in samples), default=1.0) or 1.0
    scale = 0.85 / peak if peak > 0.85 else 1.0
    levels = 2 ** crush_bits
    frames = bytearray()
    for s in samples:
        v = max(-1.0, min(1.0, s * scale))
        v = round(v * (levels / 2)) / (levels / 2)
        frames += struct.pack("<h", int(v * 32000))
    # short silent tail so players do not clip the end
    frames += b"\x00\x00" * int(SAMPLE_RATE * 0.05)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(bytes(frames))
    return buf.getvalue()


# --- Original compositions ---------------------------------------------------

def _coin_chime() -> list[float]:
    lead = _render_notes([("G5", 0.06), ("D6", 0.06), ("G6", 0.32)], duty=0.25, volume=0.5, decay=6.0, gap=0.004)
    echo = _render_notes([("R", 0.12), ("B6", 0.22)], duty=0.125, volume=0.18, decay=9.0)
    return _mix(lead, echo)


def _one_up() -> list[float]:
    return _render_notes(
        [("D5", 0.075), ("F#5", 0.075), ("A5", 0.075), ("D6", 0.075), ("A5", 0.075), ("F#6", 0.16)],
        duty=0.5, volume=0.45, gap=0.01,
    )


def _level_clear() -> list[float]:
    lead = _render_notes(
        [("C5", 0.09), ("G5", 0.09), ("E5", 0.09), ("C6", 0.09), ("R", 0.05), ("A5", 0.09), ("B5", 0.09), ("C6", 0.42)],
        duty=0.25, volume=0.45, decay=1.2,
    )
    bass = _render_notes(
        [("C3", 0.36), ("R", 0.05), ("F3", 0.18), ("G3", 0.42)],
        wave_kind="triangle", volume=0.55,
    )
    return _mix(lead, bass)


def _power_up() -> list[float]:
    return _concat(_sweep(260.0, 1250.0, 0.42, 14, duty=0.5), _render_notes([("E6", 0.12)], duty=0.25, volume=0.35, decay=10.0))


def _treasure_chest() -> list[float]:
    trill = _render_notes([("E5", 0.05), ("G#5", 0.05)] * 3, duty=0.25, volume=0.35, gap=0.004)
    rise = _render_notes([("B5", 0.08), ("E6", 0.08), ("G#6", 0.36)], duty=0.25, volume=0.45, decay=3.0)
    sparkle = _render_notes([("R", 0.08), ("B6", 0.05), ("E7", 0.05), ("B6", 0.05), ("E7", 0.12)], duty=0.125, volume=0.15, decay=6.0)
    return _concat(trill, _mix(rise, sparkle))


def _victory_fanfare() -> list[float]:
    lead = _render_notes(
        [("E5", 0.1), ("R", 0.04), ("E5", 0.1), ("A5", 0.16), ("R", 0.04), ("C#6", 0.12), ("E6", 0.5)],
        duty=0.5, volume=0.42, decay=0.6, vibrato_hz=6.0, vibrato_depth=0.004,
    )
    harmony = _render_notes(
        [("C#5", 0.1), ("R", 0.04), ("C#5", 0.1), ("E5", 0.16), ("R", 0.04), ("A5", 0.12), ("C#6", 0.5)],
        duty=0.25, volume=0.22, decay=0.6,
    )
    bass = _render_notes([("A3", 0.24), ("E3", 0.2), ("A3", 0.62)], wave_kind="triangle", volume=0.55)
    drums = _concat(_noise(0.06, 0.22), [0.0] * int(SAMPLE_RATE * 0.18), _noise(0.06, 0.22), [0.0] * int(SAMPLE_RATE * 0.14), _noise(0.12, 0.25, decay=10))
    return _mix(lead, harmony, bass, drums)


def _alarm() -> list[float]:
    return _render_notes([("A4", 0.14), ("E4", 0.14)] * 3, duty=0.5, volume=0.45, gap=0.02)


BUILTIN_SOUNDS: dict[str, tuple[str, object]] = {
    "coin_chime": ("Coin chime", _coin_chime),
    "one_up": ("1-up", _one_up),
    "level_clear": ("Level clear", _level_clear),
    "power_up": ("Power-up", _power_up),
    "treasure_chest": ("Treasure chest", _treasure_chest),
    "victory_fanfare": ("Victory fanfare", _victory_fanfare),
    "alarm": ("Two-tone alarm", _alarm),
}

_CACHE: dict[str, bytes] = {}
_LOCK = threading.Lock()


def builtin_sound_ids() -> list[str]:
    return list(BUILTIN_SOUNDS.keys())


def builtin_sound_list() -> list[dict[str, str]]:
    return [{"id": k, "label": v[0]} for k, v in BUILTIN_SOUNDS.items()]


def get_builtin_wav(sound_id: str) -> Optional[bytes]:
    if sound_id not in BUILTIN_SOUNDS:
        return None
    with _LOCK:
        cached = _CACHE.get(sound_id)
        if cached is None:
            gen = BUILTIN_SOUNDS[sound_id][1]
            cached = _to_wav(gen())  # type: ignore[operator]
            _CACHE[sound_id] = cached
        return cached


def looks_like_audio(data: bytes, ext: str) -> bool:
    """Light validation of an uploaded .wav / .mp3 file."""
    ext = ext.lower().lstrip(".")
    if ext == "wav":
        return len(data) > 44 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    if ext == "mp3":
        if data[:3] == b"ID3":
            return len(data) > 128
        return len(data) > 128 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0
    return False


if __name__ == "__main__":  # write all sounds next to this file for listening
    from pathlib import Path

    out = Path(__file__).resolve().parent / "sounds-preview"
    out.mkdir(exist_ok=True)
    for sid in builtin_sound_ids():
        (out / f"{sid}.wav").write_bytes(get_builtin_wav(sid) or b"")
        print("wrote", out / f"{sid}.wav")
