#!/usr/bin/env python
#
# Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
#
# This file is part of the Tesla Coil Music Player.

"""Turns the notes of a song into the voice events of the controller."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from heapq import merge

from gui.coil_model import MeterReading, note_frequency, spark_on_time
from gui.config import SparkConfig
from gui.protocol import Event, Kind, voice_off, voice_on, voice_set
from gui.song import BendEvent, NoteEvent, PartKey, Song

# a bend that changes less than this (semitones, 1 cent) isn't sent, nor one sooner than BEND_INTERVAL (s) after the
# previous one of the channel, unless it's the last of its run: so a dense bend curve doesn't flood the link
MIN_BEND_CHANGE = 0.01
BEND_INTERVAL = 0.005


def thin_bends(bends: list[BendEvent]) -> list[BendEvent]:
    """The bends that are worth sending, see MIN_BEND_CHANGE and BEND_INTERVAL."""
    # the time of the next bend of the same channel, of every bend
    upcoming: list[float | None] = [None] * len(bends)
    following: dict[int, float] = {}
    for idx in range(len(bends) - 1, -1, -1):
        upcoming[idx] = following.get(bends[idx].channel)
        following[bends[idx].channel] = bends[idx].time
    kept: list[BendEvent] = []
    last: dict[int, BendEvent] = {}
    for idx, bend in enumerate(bends):
        previous = last.get(bend.channel)
        if previous is not None and abs(bend.semitones - previous.semitones) < MIN_BEND_CHANGE:
            continue
        next_time = upcoming[idx]
        last_of_run = next_time is None or next_time - bend.time >= BEND_INTERVAL
        if previous is not None and bend.time - previous.time < BEND_INTERVAL and not last_of_run:
            continue
        kept.append(bend)
        last[bend.channel] = bend
    return kept


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

    # the pitch bend of every channel (semitones)
    bends: dict[int, float] = {}

    def reading() -> MeterReading:
        return MeterReading(tuple((note, velocity) for _, note, velocity in filter(None, voices)), len(dropped))

    def frequency(channel: int, note: int) -> float:
        return note_frequency(note) * 2 ** (bends.get(channel, 0.0) / 12)

    def on_time(note: int, velocity: int) -> float:
        # the on-time of the note itself, so its loudness doesn't change while it bends
        return spark_on_time(note_frequency(note), velocity, config)

    def voice_events(time: int, slot: tuple[int, int, int], voice: int) -> Event:
        channel, note, velocity = slot
        return voice_set(time, voice, frequency(channel, note), on_time(note, velocity))

    def first_step() -> Step:
        events = [voice_events(start, slot, idx) if slot else voice_off(start, idx) for idx, slot in enumerate(voices)]
        return Step(start, events, reading())

    # the bends before the notes at the same time, so a note that starts with a bend starts bent
    timeline: Iterator[BendEvent | NoteEvent] = merge(
        thin_bends(song.bends), song.events, key=lambda event: (event.time, isinstance(event, NoteEvent))
    )
    started = False
    for event in timeline:
        time = round(event.time * 1e6)
        if not started and time >= start:
            yield first_step()
            started = True
        if isinstance(event, BendEvent):
            bends[event.channel] = event.semitones
            # the voices of the channel follow it, VOICE_SET keeps their phase
            bent = [
                voice_events(time, slot, idx) for idx, slot in enumerate(voices) if slot and slot[0] == event.channel
            ]
            if started and bent:
                yield Step(time, bent, reading())
            continue
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
                planned = voice_on(
                    time, free, frequency(event.channel, event.note), on_time(event.note, event.velocity)
                )
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
