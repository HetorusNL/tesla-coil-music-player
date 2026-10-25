#
# Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
#
# This file is part of the Tesla Coil Music Player.

"""Tests of turning the notes of a song into voice events."""

from pathlib import Path

import mido
import pytest

from gui.coil_model import note_frequency, spark_on_time
from gui.config import SparkConfig
from gui.planner import plan_song
from gui.protocol import Kind
from gui.song import PartKey, Song

TICKS_PER_BEAT = 480
# 120 bpm: a beat is 0.5 s
BEAT_US = 500_000


def write_song(
    path: Path,
    notes: list[tuple[int, int, int, int]],
    bends: list[tuple[int, float, int]] | None = None,
    bend_ranges: dict[int, int] | None = None,
) -> Song:
    """A song of (channel, note, start beat, end beat) notes in a single track, with (channel, beat, pitch) pitch
    bends (pitch -8192..8191) and the pitch bend ranges (semitones) of channels, set with RPN 0 at the start."""
    # (tick, order, type, fields), at the same tick: the settings, the note offs, the bends and then the note ons
    messages: list[tuple[int, int, str, dict[str, int]]] = []
    for channel, semitones in (bend_ranges or {}).items():
        for control, value in [(101, 0), (100, 0), (6, semitones), (38, 0)]:
            messages.append((0, 0, "control_change", {"channel": channel, "control": control, "value": value}))
    for channel, note, start, end in notes:
        messages.append((end * TICKS_PER_BEAT, 1, "note_off", {"channel": channel, "note": note, "velocity": 0}))
        messages.append((start * TICKS_PER_BEAT, 3, "note_on", {"channel": channel, "note": note, "velocity": 100}))
    for channel, beat, pitch in bends or []:
        messages.append((round(beat * TICKS_PER_BEAT), 2, "pitchwheel", {"channel": channel, "pitch": pitch}))
    messages.sort(key=lambda message: message[:2])
    track = mido.MidiTrack()
    tick = 0
    for time, _, type, fields in messages:
        track.append(mido.Message(type, time=time - tick, **fields))
        tick = time
    file = mido.MidiFile(ticks_per_beat=TICKS_PER_BEAT)
    file.tracks.append(track)
    file.save(path)
    return Song(path)


def all_parts(_: PartKey) -> bool:
    return True


@pytest.fixture
def chord(tmp_path: Path) -> Song:
    # 3 notes together from beat 0 to 2, and a note from beat 1 to 3
    return write_song(tmp_path / "chord.mid", [(0, 60, 0, 2), (0, 64, 0, 2), (0, 67, 0, 2), (0, 72, 1, 3)])


def test_plan_from_the_start(chord: Song):
    config = SparkConfig()
    steps = list(plan_song(chord, all_parts, config, 16, 0))
    # the first step sets all voices, the voices are free at the start
    assert steps[0].time == 0 and [e.kind for e in steps[0].events] == [Kind.VOICE_OFF] * 16
    events = [event for step in steps[1:] for event in step.events]
    ons = [e for e in events if e.kind == Kind.VOICE_ON]
    assert [(e.time, e.voice) for e in ons] == [(0, 0), (0, 1), (0, 2), (BEAT_US, 3)]
    assert ons[0].frequency == round(note_frequency(60) * 1000)
    assert ons[0].on_time == round(spark_on_time(note_frequency(60), 100, config) * 1000)
    offs = [(e.time, e.voice) for e in events if e.kind == Kind.VOICE_OFF]
    assert sorted(offs) == [(2 * BEAT_US, 0), (2 * BEAT_US, 1), (2 * BEAT_US, 2), (3 * BEAT_US, 3)]
    # the song ends with all voices off
    assert steps[-1].events[0].kind == Kind.ALL_OFF and steps[-1].time == 3 * BEAT_US


def test_plan_from_halfway_sets_the_sounding_voices(chord: Song):
    start = BEAT_US + BEAT_US // 2
    steps = list(plan_song(chord, all_parts, SparkConfig(), 16, start))
    first = steps[0]
    assert first.time == start
    sets = {e.voice for e in first.events if e.kind == Kind.VOICE_SET}
    assert sets == {0, 1, 2, 3}
    assert all(e.kind == Kind.VOICE_OFF for e in first.events if e.voice not in sets)
    assert len(first.reading.notes) == 4
    # nothing before the start
    assert all(step.time >= start for step in steps)


def test_notes_beyond_the_voices_are_dropped(chord: Song):
    steps = list(plan_song(chord, all_parts, SparkConfig(), 2, 0))
    ons = [e for step in steps for e in step.events if e.kind == Kind.VOICE_ON]
    assert [e.voice for e in ons] == [0, 1]
    # both voices are busy until beat 2, so the third and fourth note are dropped
    assert max(step.reading.dropped for step in steps) == 2


def test_muted_parts_are_left_out(tmp_path: Path):
    song = write_song(tmp_path / "parts.mid", [(0, 60, 0, 1), (1, 64, 0, 1)])
    muted: PartKey = (0, 1)
    steps = list(plan_song(song, lambda part: part != muted, SparkConfig(), 16, 0))
    ons = [e for step in steps for e in step.events if e.kind == Kind.VOICE_ON]
    assert [e.frequency for e in ons] == [round(note_frequency(60) * 1000)]


def bent(note: int, semitones: float) -> int:
    """The frequency (mHz) of the note, bent by the semitones."""
    return round(note_frequency(note) * 2 ** (semitones / 12) * 1000)


def test_reads_the_bends_and_their_range(tmp_path: Path):
    # the default range of 2 semitones on channel 0, 12 semitones on channel 1
    song = write_song(
        tmp_path / "bends.mid",
        [(0, 60, 0, 2), (1, 64, 0, 2)],
        bends=[(0, 1, 4096), (1, 1, -8192)],
        bend_ranges={1: 12},
    )
    assert [(b.channel, b.time, b.semitones) for b in song.bends] == [(0, 0.5, 1.0), (1, 0.5, -12.0)]


def test_a_bend_bends_the_sounding_voices_of_its_channel(tmp_path: Path):
    song = write_song(tmp_path / "bend.mid", [(0, 60, 0, 2), (1, 64, 0, 2)], bends=[(0, 1, 8191)])
    config = SparkConfig()
    steps = list(plan_song(song, all_parts, config, 16, 0))
    sets = [e for step in steps[1:] for e in step.events if e.kind == Kind.VOICE_SET]
    # only the note on channel 0, up to almost 2 semitones, with the on-time of the note itself
    assert [(e.time, e.voice) for e in sets] == [(BEAT_US, 0)]
    assert sets[0].frequency == bent(60, 8191 / 8192 * 2)
    assert sets[0].on_time == round(spark_on_time(note_frequency(60), 100, config) * 1000)


def test_a_note_starts_bent(tmp_path: Path):
    # the bend at beat 1 comes before the note that starts then
    song = write_song(tmp_path / "start.mid", [(0, 60, 1, 2)], bends=[(0, 1, -4096)])
    ons = [
        e for step in plan_song(song, all_parts, SparkConfig(), 16, 0) for e in step.events if e.kind == Kind.VOICE_ON
    ]
    assert [e.frequency for e in ons] == [bent(60, -1)]


def test_a_plan_from_halfway_starts_bent(tmp_path: Path):
    song = write_song(tmp_path / "halfway.mid", [(0, 60, 0, 4)], bends=[(0, 1, 4096)])
    first = next(iter(plan_song(song, all_parts, SparkConfig(), 16, 3 * BEAT_US)))
    assert [(e.kind, e.frequency) for e in first.events if e.voice == 0] == [(Kind.VOICE_SET, bent(60, 1))]


def test_dense_bends_are_thinned(tmp_path: Path):
    # a sweep of 2 semitones over a beat in steps of a 480th beat (1 ms), and bends that change nothing
    sweep = [(0, step / 480, round(8191 * step / 480)) for step in range(481)]
    steady = [(0, 1 + step / 10, 8191) for step in range(1, 10)]
    song = write_song(tmp_path / "dense.mid", [(0, 60, 0, 2)], bends=sweep + steady)
    sets = [
        e for step in plan_song(song, all_parts, SparkConfig(), 16, 0) for e in step.events if e.kind == Kind.VOICE_SET
    ]
    times = [e.time for e in sets]
    # at most one every 5 ms, but the end of the sweep is there, and nothing for the steady bends
    assert all(b - a >= 5000 for a, b in zip(times, times[1:-1]))
    assert sets[-1].frequency == bent(60, 8191 / 8192 * 2)
    assert times[-1] <= BEAT_US


def test_the_preview_bends(tmp_path: Path):
    song = write_song(tmp_path / "preview.mid", [(0, 60, 0, 2)], bends=[(0, 1, 8191)])
    assert song.preview_notes(set(song.parts)) == [(0.0, 1.0, 60, 100, [(0.0, 0), (0.5, 200)])]
