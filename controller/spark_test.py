#!/usr/bin/env python
#
# Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
#
# This file is part of the Tesla Coil Music Player.

"""Checks the sparks of the controller, measured by the controller itself.

Needs the test firmware (pio run -e nucleo_spark_test -t upload) and a wire from the coil pin D10 to A0, disconnect
the coil and close the GUI. The firmware logs the sparks it schedules and captures the pulses on A0, both in ticks of
TIM2, this script plays notes and compares the two.

    uv run controller/spark_test.py /dev/ttyACM0
"""

import argparse
import struct
import sys
from dataclasses import dataclass
from time import sleep

import serial

STX = 0x02
ETX = 0x03
ACK = 0x06
NACK = 0x15
TYPE_MIDI_MSG = 0x02
TYPE_CONFIG = 0x04
TYPE_PING = 0x05
TYPE_TEST = 0x06
TYPE_RESET = 0xFF
TEST_START = 0x01
TEST_DUMP = 0x02
SETTING_ON_TIME = 0x01
SETTING_MIN_ON_TIME = 0x02
SETTING_MAX_ON_TIME = 0x03
SETTING_VOLUME = 0x04
SETTING_VELOCITY_SENSITIVITY = 0x05
SETTING_MAX_DUTY = 0x06
NUM_ON_TIMES = 20
FRACTION_SCALE = 10000
DUMP_MAGIC = 0x4B525053
# the minimum off-time of the firmware (us)
MIN_OFF_TIME = 10
# the edges of a pulse may differ a tick from the configured on-time, the timers synchronize to each other
TOLERANCE_TICKS = 2


def ticks_diff(a: int, b: int) -> int:
    """a - b of two TIM2 ticks, which wrap around at 32 bits."""
    return (a - b + 2**31) % 2**32 - 2**31


@dataclass(frozen=True)
class Spark:
    tick: int
    on_ticks: int
    timer_id: int


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
    now: int
    sparks: list[Spark]
    pulses: list[Pulse]
    missed_pulses: int
    dropped: int

    def us(self, ticks: float) -> float:
        return ticks * 1e6 / self.clock_hz

    def ticks(self, us: float) -> float:
        return us * self.clock_hz / 1e6


class Controller:
    def __init__(self, port: str):
        self.serial = serial.Serial(port, 921600, timeout=5)
        self.seq = 0
        sleep(0.1)
        self.serial.reset_input_buffer()

    def request(self, msg_type: int, data: list[int] | None = None) -> None:
        data = data or []
        for _ in range(2):
            self.serial.write(bytes([STX, self.seq, msg_type, len(data), *data, ETX]))
            reply = self.serial.read(4)
            if len(reply) == 4 and reply[0] == STX and reply[1] == ACK and reply[2] == self.seq and reply[3] == ETX:
                self.seq = (self.seq + 1) % 256
                return
            if len(reply) == 4 and reply[1] == NACK and reply[2] != self.seq:
                # out of sync (e.g. the GUI used the controller before), continue with the sequence number it expects
                self.seq = reply[2]
                sleep(0.05)
                self.serial.reset_input_buffer()
                continue
            raise RuntimeError(f"unexpected reply to message type {msg_type:02x}: {reply.hex(' ')}")
        raise RuntimeError(f"the controller kept rejecting message type {msg_type:02x}")

    def reset(self):
        self.request(TYPE_RESET)

    def configure(
        self, on_time: int, min_on_time: int = 10, max_on_time: int = 100, volume: float = 1.0, max_duty: float = 0.15
    ):
        for index in range(NUM_ON_TIMES):
            self._setting(SETTING_ON_TIME, index, on_time)
        self._setting(SETTING_MIN_ON_TIME, 0, min_on_time)
        self._setting(SETTING_MAX_ON_TIME, 0, max_on_time)
        self._setting(SETTING_VOLUME, 0, round(volume * FRACTION_SCALE))
        self._setting(SETTING_VELOCITY_SENSITIVITY, 0, 0)
        self._setting(SETTING_MAX_DUTY, 0, round(max_duty * FRACTION_SCALE))

    def _setting(self, setting: int, index: int, value: int):
        self.request(TYPE_CONFIG, [setting, index, value >> 8, value & 0xFF])

    def note_on(self, note: int, velocity: int = 127):
        self.request(TYPE_MIDI_MSG, [0x90, note, velocity, 0, 0])

    def note_off(self, note: int):
        self.request(TYPE_MIDI_MSG, [0x80, note, 0, 0, 0])

    def start_log(self):
        self.request(TYPE_TEST, [TEST_START])

    def dump_log(self) -> Log:
        # the dump comes before the ACK
        self.serial.write(bytes([STX, self.seq, TYPE_TEST, 1, TEST_DUMP, ETX]))
        header = self._read_exactly(28)
        magic, clock_hz, now, num_sparks, num_pulses, missed, dropped = struct.unpack("<7I", header)
        if magic != DUMP_MAGIC:
            raise RuntimeError(f"no spark log received ({header.hex(' ')}), is the test firmware on the controller?")
        sparks = [Spark(*s) for s in struct.iter_unpack("<IHBx", self._read_exactly(num_sparks * 8))]
        pulses = [Pulse(*p) for p in struct.iter_unpack("<II", self._read_exactly(num_pulses * 8))]
        reply = self._read_exactly(4)
        if reply != bytes([STX, ACK, self.seq, ETX]):
            raise RuntimeError(f"unexpected reply to the dump: {reply.hex(' ')}")
        self.seq = (self.seq + 1) % 256
        return Log(clock_hz, now, sparks, pulses, missed, dropped)

    def _read_exactly(self, size: int) -> bytes:
        data = self.serial.read(size)
        if len(data) != size:
            raise RuntimeError(f"expected {size} bytes from the controller, received {len(data)}")
        return data

    def play(self, notes: list[int], seconds: float) -> Log:
        """Plays the notes together and returns the log, from the first note on until well after the last note off."""
        self.start_log()
        for note in notes:
            self.note_on(note)
        sleep(seconds)
        for note in notes:
            self.note_off(note)
        sleep(0.05)
        return self.dump_log()


class Report:
    def __init__(self):
        self.failures = 0

    def check(self, name: str, ok: bool, detail: str = ""):
        print(f"  {'PASS' if ok else 'FAIL'}  {name}{f': {detail}' if detail else ''}")
        self.failures += not ok

    def info(self, text: str):
        print(f"        {text}")


def check_pulses(report: Report, log: Log):
    """The checks of every test: the pulses on the pin are the scheduled sparks, at least the minimum off-time apart."""
    report.check(
        "log complete",
        log.missed_pulses == 0 and log.dropped == 0,
        f"{log.missed_pulses} missed pulses, {log.dropped} dropped entries",
    )
    if log.sparks and not log.pulses:
        report.check("pulses on A0", False, f"{len(log.sparks)} sparks scheduled but no pulses, check the D10-A0 wire")
        return
    report.check(
        "every spark made a pulse",
        len(log.pulses) == len(log.sparks),
        f"{len(log.sparks)} sparks, {len(log.pulses)} pulses",
    )
    pairs = list(zip(log.sparks, log.pulses))
    if not pairs:
        return
    # the hardware trigger starts the pulse a fixed number of ticks after the compare, whatever the interrupts do
    latencies = [ticks_diff(p.rise, s.tick) for s, p in pairs]
    report.check(
        "pulse start exact",
        max(latencies) - min(latencies) <= 1,
        f"{min(latencies)}..{max(latencies)} ticks after the compare "
        f"({log.us(min(latencies)) * 1000:.0f}..{log.us(max(latencies)) * 1000:.0f} ns)",
    )
    errors = [p.width - s.on_ticks for s, p in pairs]
    report.check(
        "pulse widths exact",
        max(map(abs, errors)) <= TOLERANCE_TICKS,
        f"width - on-time: {min(errors)}..{max(errors)} ticks",
    )
    gaps = [ticks_diff(b.rise, a.fall) for a, b in zip(log.pulses, log.pulses[1:])]
    if gaps:
        report.check(
            "minimum off-time",
            min(gaps) >= log.ticks(MIN_OFF_TIME) - TOLERANCE_TICKS,
            f"shortest gap {log.us(min(gaps)):.3f} us",
        )


def check_note(report: Report, log: Log, timer_id: int, note: int):
    """The sparks of the timer are on the grid of the frequency of the note: delayed sometimes, but never drifting."""
    frequency = 440 * 2 ** ((note - 69) / 12)
    period = log.clock_hz / frequency
    ticks = [s.tick for s in log.sparks if s.timer_id == timer_id]
    if len(ticks) < 2:
        report.check(f"note {note} sparks", False, f"{len(ticks)} sparks")
        return
    offsets = [ticks_diff(t, ticks[0]) for t in ticks]
    # the position of every spark relative to the grid through the first spark
    residuals = [offset - round(offset / period) * period for offset in offsets]
    delays = [r - min(residuals) for r in residuals]
    skipped = sum(round((b - a) / period) - 1 for a, b in zip(offsets, offsets[1:]))
    # the frequency from the first to the last spark that wasn't delayed
    on_time = [offset for offset, delay in zip(offsets, delays) if delay <= 1]
    periods = round((on_time[-1] - on_time[0]) / period)
    measured = f", {log.clock_hz * periods / (on_time[-1] - on_time[0]):.3f} Hz" if periods else ""
    report.check(
        f"note {note} ({frequency:.3f} Hz) on its grid",
        max(delays) < period / 2,
        f"{len(ticks)} sparks, {skipped} skipped, delayed up to {log.us(max(delays)):.1f} us{measured}",
    )


def test_single_note(controller: Controller, report: Report):
    print("single note A4, 50 us")
    controller.configure(on_time=50)
    log = controller.play([69], 0.5)
    check_pulses(report, log)
    report.check("on-time 50 us", all(abs(s.on_ticks - log.ticks(50)) <= 1 for s in log.sparks))
    intervals = [ticks_diff(b.rise, a.rise) for a, b in zip(log.pulses, log.pulses[1:])]
    period = log.clock_hz / 440
    if intervals:
        deviation = max(abs(i - period) for i in intervals)
        frequency = log.clock_hz * len(intervals) / ticks_diff(log.pulses[-1].rise, log.pulses[0].rise)
        report.check(
            "440 Hz without jitter", deviation <= 1, f"{frequency:.4f} Hz, intervals within {deviation:.2f} ticks"
        )


def test_fractional_on_time(controller: Controller, report: Report):
    print("sub-microsecond on-time: 50 us at volume 0.51")
    controller.configure(on_time=50, volume=0.51)
    log = controller.play([69], 0.2)
    check_pulses(report, log)
    widths = sorted({p.width for p in log.pulses})
    report.check(
        "on-time 25.5 us",
        bool(widths) and all(abs(w - log.ticks(25.5)) <= TOLERANCE_TICKS for w in widths),
        ", ".join(f"{log.us(w):.3f} us" for w in widths[:5]),
    )


def test_max_on_time(controller: Controller, report: Report):
    print("maximum on-time: 150 us limited to 100 us")
    controller.configure(on_time=150, max_on_time=100)
    log = controller.play([57], 0.2)
    check_pulses(report, log)
    report.check(
        "limited to 100 us",
        bool(log.pulses) and all(abs(p.width - log.ticks(100)) <= TOLERANCE_TICKS for p in log.pulses),
    )


def test_chord(controller: Controller, report: Report):
    notes = [60, 64, 67, 72, 76, 79, 84, 88, 91]
    print(f"chord of {len(notes)} notes, 50 us")
    controller.configure(on_time=50)
    log = controller.play(notes, 0.5)
    check_pulses(report, log)
    # the timers are handed out in order after a reset, so timer i plays the i-th note
    for timer_id, note in enumerate(notes):
        check_note(report, log, timer_id, note)


def test_duty_limit(controller: Controller, report: Report):
    notes = [60, 64, 67, 72, 76, 79, 84, 88, 91]
    print(f"duty cycle limit: chord of {len(notes)} notes, 50 us, at most 5%")
    controller.configure(on_time=50, min_on_time=10, max_duty=0.05)
    log = controller.play(notes, 0.5)
    check_pulses(report, log)
    if not log.pulses:
        return
    # the budget holds 10 ms of on-time at the limit, so any window can have that much more than the limit
    window = log.ticks(10_000)
    budget = 0.05 * window
    worst = 0.0
    start = 0
    for end, pulse in enumerate(log.pulses):
        while ticks_diff(pulse.rise, log.pulses[start].rise) > window:
            start += 1
        worst = max(worst, sum(p.width for p in log.pulses[start : end + 1]))
    # the budget starts full, which adds its 10 ms of on-time to the average
    span = ticks_diff(log.pulses[-1].fall, log.pulses[0].rise)
    average = sum(p.width for p in log.pulses) / span
    report.check(
        "at most 5% on average",
        average <= 0.05 + (0.05 * 10_000 + 50) / log.us(span),
        f"{average:.2%} over {log.us(span) / 1000:.0f} ms",
    )
    report.check(
        "at most the budget per 10 ms",
        worst <= 2 * budget + log.ticks(50),
        f"{log.us(worst):.0f} us in the busiest 10 ms (limit {log.us(2 * budget):.0f} us)",
    )
    shortened = [p for p in log.pulses if p.width < log.ticks(50) - TOLERANCE_TICKS]
    report.check(
        "shortened sparks not below the min on-time",
        all(p.width >= log.ticks(10) - TOLERANCE_TICKS for p in log.pulses),
        f"{len(shortened)} of {len(log.pulses)} sparks shortened, shortest {log.us(min(p.width for p in log.pulses)):.2f} us",
    )


def test_note_off(controller: Controller, report: Report):
    print("note off stops the sparks")
    controller.configure(on_time=50)
    controller.start_log()
    controller.note_on(69)
    sleep(0.2)
    controller.note_off(69)
    sleep(0.3)
    log = controller.dump_log()
    check_pulses(report, log)
    if log.pulses:
        silence = log.us(ticks_diff(log.now, log.pulses[-1].fall)) / 1e6
        report.check("silent after the note off", silence > 0.25, f"last pulse {silence:.3f} s before the dump")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("port", help="serial port of the controller")
    args = parser.parse_args()

    controller = Controller(args.port)
    controller.request(TYPE_PING)
    report = Report()
    tests = [test_single_note, test_fractional_on_time, test_max_on_time, test_chord, test_duty_limit, test_note_off]
    for test in tests:
        controller.reset()
        test(controller, report)
    controller.reset()
    print(f"\n{'all checks passed' if report.failures == 0 else f'{report.failures} checks failed'}")
    print("the controller has the test settings now, restart the GUI to send it the settings again")
    sys.exit(report.failures != 0)


if __name__ == "__main__":
    main()
