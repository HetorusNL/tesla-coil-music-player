#!/usr/bin/env python
#
# Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
#
# This file is part of the Tesla Coil Music Player.

import threading
from collections.abc import Iterable
from dataclasses import dataclass

# the number of notes the controller plays at once (TimerManager::NUM_TIMERS of the firmware)
COIL_TIMERS = 9

# spark on-time table and constants of CoilTimer in the controller firmware
ON_TIMES = [80, 75, 70, 66, 62, 59, 57, 56, 55, 54, 53, 52, 51, 50, 50, 50, 50, 50, 50, 50]
VOLUME_MULTIPLIER = 0.25
VOLUME_FACTOR = 1.5
MIN_ON_TIME = 10
MAX_ON_TIME = 100
TIMER_VELOCITY = 0  # the firmware never sets the velocity of its timers


def note_frequency(note: int) -> float:
    return 440 * 2 ** ((note - 69) / 12)


def spark_on_time(frequency: float) -> int:
    """Mirrors CoilTimer::get_on_time and CoilTimer::create_spark: the on-time (us) of a single spark."""
    freq = int(frequency)
    index = freq // 100
    # beyond the end of the table, the last entry is used
    on_time = ON_TIMES[-1]
    if index < len(ON_TIMES) - 1:
        base = ON_TIMES[index]
        interval = base - ON_TIMES[index + 1]
        on_time = base - interval * (freq % 100 + 1) // 100
    duration = int(on_time * VOLUME_MULTIPLIER / VOLUME_FACTOR)
    duration = (duration * (127 + TIMER_VELOCITY // 2)) >> 8
    return min(max(duration, MIN_ON_TIME), MAX_ON_TIME)


def duty_cycle(notes: Iterable[int]) -> float:
    """The fraction of time the coil is on, when the notes are playing."""
    return sum(note_frequency(note) * spark_on_time(note_frequency(note)) for note in notes) / 1e6


@dataclass(frozen=True)
class MeterReading:
    notes: tuple[int, ...]  # the notes playing on the timers of the coil
    dropped: int  # the number of notes that didn't get a timer

    @property
    def duty_cycle(self) -> float:
        return duty_cycle(self.notes)


class CoilModel:
    """Mirrors the notes the controller plays, it drops the notes that don't get one of its timers."""

    def __init__(self):
        self._lock = threading.Lock()
        # (channel, note) of the notes on the timers, and of the notes that were dropped
        self._playing: list[tuple[int, int]] = []
        self._dropped: set[tuple[int, int]] = set()

    def handle_midi(self, midi_bytes: list[int]):
        status, note, velocity = midi_bytes[0] & 0xF0, midi_bytes[1], midi_bytes[2]
        key = (midi_bytes[0] & 0x0F, note)
        with self._lock:
            if status == 0x90 and velocity > 0:
                if key in self._playing or key in self._dropped:
                    return
                if len(self._playing) < COIL_TIMERS:
                    self._playing.append(key)
                else:
                    self._dropped.add(key)
            elif status in [0x80, 0x90]:
                if key in self._playing:
                    self._playing.remove(key)
                self._dropped.discard(key)

    def clear(self):
        with self._lock:
            self._playing.clear()
            self._dropped.clear()

    def reading(self) -> MeterReading:
        with self._lock:
            return MeterReading(tuple(note for _, note in self._playing), len(self._dropped))
