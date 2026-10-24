#
# Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
#
# This file is part of the Tesla Coil Music Player.

"""Reads the spark log of the test firmware (the nucleo_spark_test build) and checks it.

The firmware logs the sparks it schedules and captures the pulses on the coil pin (wired from D10 to A0), both in ticks
of TIM2.
"""

import struct
from dataclasses import dataclass
from time import sleep

from gui.protocol import Controller, Event, Kind, Type, voice_on

TEST_START = 0x01
TEST_STOP = 0x02
TEST_READ = 0x03
READ_ENTRIES = 29
# the minimum off-time of the firmware (us)
MIN_OFF_TIME = 10
# the edges of a pulse may differ a tick from the on-time, the timers synchronize to each other
TOLERANCE_TICKS = 2


def ticks_diff(a: int, b: int) -> int:
    """a - b of two TIM2 ticks, which wrap around at 32 bits."""
    return (a - b + 2**31) % 2**32 - 2**31


@dataclass(frozen=True)
class Spark:
    tick: int
    on_ticks: int
    voice: int


@dataclass(frozen=True)
class Pulse:
    rise: int
    fall: int

    @property
    def width(self) -> int:
        return ticks_diff(self.fall, self.rise)


@dataclass(frozen=True)
class Log:
    clock_hz: int
    tick_at_zero: int  # the tick at which the stream clock was 0
    now: int
    sparks: list[Spark]
    pulses: list[Pulse]
    missed_pulses: int
    dropped: int

    def us(self, ticks: float) -> float:
        return ticks * 1e6 / self.clock_hz

    def ticks(self, us: float) -> float:
        return us * self.clock_hz / 1e6

    def stream_tick(self, time: int) -> int:
        """The tick of the time (us) of the stream clock."""
        return (self.tick_at_zero + time * self.clock_hz // 1_000_000) % 2**32

    def sparks_of(self, voice: int) -> list[int]:
        return [s.tick for s in self.sparks if s.voice == voice]


class LoggingController(Controller):
    @classmethod
    def sharing(cls, controller: Controller) -> "LoggingController":
        """Reads the spark log over the connection of the controller."""
        logging = cls.__new__(cls)
        logging.link = controller.link
        logging.info = controller.info
        return logging

    def set_limits(self, max_on_time: float = 100, min_on_time: float = 10, max_duty: float = 0.15):
        super().set_limits(max_on_time, min_on_time, max_duty)

    def start_log(self):
        self.link.request(Type.TEST, bytes([TEST_START]))

    def read_log(self) -> Log:
        header = self.link.request(Type.TEST, bytes([TEST_STOP])).body
        clock_hz, tick_at_zero, now, num_sparks, num_pulses, missed, dropped = struct.unpack("<7I", header)
        sparks = [Spark(*s) for s in struct.iter_unpack("<IHBx", self._read_entries(0, num_sparks))]
        pulses = [Pulse(*p) for p in struct.iter_unpack("<II", self._read_entries(1, num_pulses))]
        return Log(clock_hz, tick_at_zero, now, sparks, pulses, missed, dropped)

    def _read_entries(self, which: int, count: int) -> bytes:
        pendings = [
            self.link.post(Type.TEST, struct.pack("<BBH", TEST_READ, which, first))
            for first in range(0, count, READ_ENTRIES)
        ]
        return b"".join(self.link.wait(pending).body for pending in pendings)

    def play(self, voices: list[tuple[float, float]], seconds: float) -> Log:
        """Plays the (frequency Hz, on-time us) voices together right away, returns the log until after they stop."""
        self.start_log()
        self.immediate(voice_on(0, voice, frequency, on_time) for voice, (frequency, on_time) in enumerate(voices))
        sleep(seconds)
        self.immediate([Event(0, Kind.ALL_OFF)])
        sleep(0.05)
        return self.read_log()


def assert_pulses(log: Log):
    """The checks of every test: the pulses on the pin are the scheduled sparks, at least the minimum off-time apart."""
    assert log.missed_pulses == 0 and log.dropped == 0, f"{log.missed_pulses} missed pulses, {log.dropped} dropped"
    assert log.pulses or not log.sparks, f"{len(log.sparks)} sparks but no pulses on A0, check the D10-A0 wire"
    assert len(log.pulses) == len(log.sparks), f"{len(log.sparks)} sparks, {len(log.pulses)} pulses"
    pairs = list(zip(log.sparks, log.pulses))
    # the hardware trigger starts the pulse a fixed number of ticks after the compare, whatever the interrupts do
    latencies = [ticks_diff(p.rise, s.tick) for s, p in pairs]
    assert not latencies or max(latencies) - min(latencies) <= 1, f"pulse starts {min(latencies)}..{max(latencies)}"
    errors = [p.width - s.on_ticks for s, p in pairs]
    assert not errors or max(map(abs, errors)) <= TOLERANCE_TICKS, f"width - on-time {min(errors)}..{max(errors)}"
    gaps = [ticks_diff(b.rise, a.fall) for a, b in zip(log.pulses, log.pulses[1:])]
    shortest = log.us(min(gaps)) if gaps else MIN_OFF_TIME
    assert shortest >= MIN_OFF_TIME - log.us(TOLERANCE_TICKS), f"shortest off-time {shortest:.3f} us"


def assert_on_grid(log: Log, voice: int, frequency: float):
    """The sparks of the voice are on the grid of its frequency: delayed sometimes, but never drifting or skipped."""
    period = log.clock_hz / frequency
    ticks = log.sparks_of(voice)
    assert len(ticks) >= 2, f"voice {voice}: {len(ticks)} sparks"
    offsets = [ticks_diff(t, ticks[0]) for t in ticks]
    # the position of every spark relative to the grid through the first spark
    residuals = [offset - round(offset / period) * period for offset in offsets]
    delays = [r - min(residuals) for r in residuals]
    skipped = sum(round((b - a) / period) - 1 for a, b in zip(offsets, offsets[1:]))
    assert skipped == 0, f"voice {voice}: {skipped} sparks skipped"
    assert max(delays) < period / 2, f"voice {voice}: delayed up to {log.us(max(delays)):.1f} us"


def frequency_of(log: Log, ticks: list[int]) -> float:
    """The average frequency (Hz) of the ticks."""
    intervals = [ticks_diff(b, a) for a, b in zip(ticks, ticks[1:])]
    return log.clock_hz * len(intervals) / sum(intervals)
