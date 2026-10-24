#!/usr/bin/env python
#
# Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
#
# This file is part of the Tesla Coil Music Player.

import threading
from collections.abc import Iterable
from dataclasses import dataclass

from gui.config import ABSOLUTE_MAX_ON_TIME, SparkConfig

# the number of notes the controller plays at once (SparkGenerator::NUM_VOICES of the firmware)
COIL_VOICES = 16


def note_frequency(note: int) -> float:
    return 440 * 2 ** ((note - 69) / 12)


def spark_on_time(frequency: float, velocity: int, config: SparkConfig) -> float:
    """The on-time (us) of the sparks of a note: the on-time table times the volume and the velocity factor, within the
    min and max on-time."""
    on_times = config.on_times
    freq = int(frequency)
    index = freq // 100
    # beyond the end of the table, the last entry is used
    on_time: float = on_times[-1]
    if index < len(on_times) - 1:
        on_time = on_times[index] + (on_times[index + 1] - on_times[index]) * (freq % 100 + 1) / 100
    velocity_factor = 1 - config.velocity_sensitivity * (1 - velocity / 127)
    on_time *= config.volume * velocity_factor
    max_on_time = min(config.max_on_time, ABSOLUTE_MAX_ON_TIME)
    return min(max(on_time, config.min_on_time), max_on_time)


def duty_cycle(notes: Iterable[tuple[int, int]], config: SparkConfig) -> float:
    """The fraction of time the coil would be on without the duty cycle limit, when the (note, velocity) notes play."""
    return (
        sum(note_frequency(note) * spark_on_time(note_frequency(note), velocity, config) for note, velocity in notes)
        / 1e6
    )


def limited_duty_cycle(notes: Iterable[tuple[int, int]], config: SparkConfig) -> float:
    """Mirrors the SparkGenerator of the firmware: the fraction of time the coil is on within the max duty cycle.
    Beyond it all sparks get shorter by the same factor, but not below the min on-time, and the budget of on-time
    limits the rest.
    """
    sparks = [(note_frequency(note), spark_on_time(note_frequency(note), velocity, config)) for note, velocity in notes]
    demand = sum(frequency * on_time for frequency, on_time in sparks) / 1e6
    if demand <= config.max_duty:
        return demand
    scale = config.max_duty / demand
    scaled = [(frequency, max(on_time * scale, min(on_time, config.min_on_time))) for frequency, on_time in sparks]
    return min(sum(frequency * on_time for frequency, on_time in scaled) / 1e6, config.max_duty)


@dataclass(frozen=True)
class MeterReading:
    notes: tuple[tuple[int, int], ...]  # the (note, velocity) notes playing on the timers of the coil
    dropped: int  # the number of notes that didn't get a timer

    def duty_cycle(self, config: SparkConfig) -> float:
        return duty_cycle(self.notes, config)

    def limited_duty_cycle(self, config: SparkConfig) -> float:
        return limited_duty_cycle(self.notes, config)


class CoilModel:
    """What the coil plays, for the meter: the player keeps it up to date while a song or live notes play."""

    def __init__(self):
        self._lock = threading.Lock()
        self._reading = MeterReading((), 0)

    def set_reading(self, reading: MeterReading):
        with self._lock:
            self._reading = reading

    def clear(self):
        self.set_reading(MeterReading((), 0))

    def reading(self) -> MeterReading:
        with self._lock:
            return self._reading
