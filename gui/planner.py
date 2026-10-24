#!/usr/bin/env python
#
# Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
#
# This file is part of the Tesla Coil Music Player.

"""Turns the notes of a song into the voice events of the controller."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass

from gui.coil_model import MeterReading, note_frequency, spark_on_time
from gui.config import SparkConfig
from gui.protocol import Event, Kind, voice_off, voice_on, voice_set
from gui.song import PartKey, Song


@dataclass(frozen=True)
class Step:
    """The events at a time of the song, and what the coil plays from then on."""

    time: int  # us
    events: list[Event]
    reading: MeterReading


def plan_song(
    song: Song, playing: Callable[[PartKey], bool], config: SparkConfig, num_voices: int, start: int
) -> Iterator[Step]:
    """The steps of the song from the start (us), in order of time.

    The notes get the first free voice, a note that finds all voices in use is dropped. The voices are assigned from
    the beginning of the song, so a plan from any start assigns the same voices. The first step sets every voice to
    what it plays at the start: VOICE_SET for the voices that sound (which starts a silent voice and keeps the phase of
    a sounding one), VOICE_OFF for the others. So the controller ends up in the same state, whatever it played
    before. The last step turns all voices off at the end of the song.
    """
    voices: list[tuple[int, int, int] | None] = [None] * num_voices  # (channel, note, velocity)
    dropped: set[tuple[int, int]] = set()

    def reading() -> MeterReading:
        return MeterReading(tuple((note, velocity) for _, note, velocity in filter(None, voices)), len(dropped))

    def on_time(note: int, velocity: int) -> float:
        return spark_on_time(note_frequency(note), velocity, config)

    def first_step() -> Step:
        events = [
            voice_set(start, idx, note_frequency(slot[1]), on_time(slot[1], slot[2])) if slot else voice_off(start, idx)
            for idx, slot in enumerate(voices)
        ]
        return Step(start, events, reading())

    started = False
    for event in song.events:
        time = round(event.time * 1e6)
        if not started and time >= start:
            yield first_step()
            started = True
        key = (event.channel, event.note)
        sounding = next((idx for idx, slot in enumerate(voices) if slot and slot[:2] == key), None)
        planned: Event | None = None
        if event.is_note_on:
            if not playing(event.part) or sounding is not None or key in dropped:
                continue
            free = next((idx for idx, slot in enumerate(voices) if slot is None), None)
            if free is None:
                dropped.add(key)
            else:
                voices[free] = (event.channel, event.note, event.velocity)
                planned = voice_on(time, free, note_frequency(event.note), on_time(event.note, event.velocity))
        elif key in dropped:
            dropped.discard(key)
        elif sounding is not None:
            voices[sounding] = None
            planned = voice_off(time, sounding)
        else:
            continue
        if started:
            yield Step(time, [planned] if planned else [], reading())
    if not started:
        yield first_step()
    end = max(round(song.length * 1e6), start)
    yield Step(end, [Event(end, Kind.ALL_OFF)], MeterReading((), 0))
