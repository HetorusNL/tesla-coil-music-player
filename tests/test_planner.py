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


def write_song(path: Path, notes: list[tuple[int, int, int, int]]) -> Song:
    """A song of (channel, note, start beat, end beat) notes, in a single track."""
    messages: list[tuple[int, mido.Message]] = []
    for channel, note, start, end in notes:
        messages.append((start * TICKS_PER_BEAT, mido.Message("note_on", channel=channel, note=note, velocity=100)))
        messages.append((end * TICKS_PER_BEAT, mido.Message("note_off", channel=channel, note=note, velocity=0)))
    messages.sort(key=lambda message: (message[0], message[1].type == "note_on"))
    track = mido.MidiTrack()
    tick = 0
    for time, message in messages:
        track.append(message.copy(time=time - tick))
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
