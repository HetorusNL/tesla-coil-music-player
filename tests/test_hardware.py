#
# Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
#
# This file is part of the Tesla Coil Music Player.

"""Tests of the controller running the test firmware, skipped without --port.

    pio run -e nucleo_spark_test -t upload
    uv run pytest --port /dev/ttyACM0

Wire the coil pin D10 to A0 and disconnect the coil, a speaker with a series resistor (or a piezo) may stay on D10.
"""

from time import sleep

import pytest

from gui.protocol import (
    PingStatus,
    RequestFailed,
    Status,
    StreamState,
    Type,
    frame,
    voice_off,
    voice_on,
    voice_set,
)
from tests.sparks import (
    TOLERANCE_TICKS,
    LoggingController,
    assert_on_grid,
    assert_pulses,
    frequency_of,
    ticks_diff,
)

# C major over 3 octaves, and 16 notes of a C major scale (Hz)
CHORD_9 = [261.626, 329.628, 391.995, 523.251, 659.255, 783.991, 1046.502, 1318.510, 1567.982]
CHORD_16 = [130.813 * 2 ** (step / 12) for step in [0, 2, 4, 5, 7, 9, 11, 12, 14, 16, 17, 19, 21, 23, 24, 26]]


def test_hello(controller: LoggingController):
    assert controller.info is not None
    assert controller.info.num_voices == 16
    assert controller.info.clock_hz == 108_000_000


def test_single_voice(coil: LoggingController):
    log = coil.play([(440, 50)], 0.5)
    assert_pulses(log)
    assert all(abs(s.on_ticks - log.ticks(50)) <= 1 for s in log.sparks)
    # the period has a fraction of a tick, so the intervals differ at most a tick from it
    period = log.clock_hz / 440
    intervals = [ticks_diff(b.rise, a.rise) for a, b in zip(log.pulses, log.pulses[1:])]
    assert max(abs(i - period) for i in intervals) <= 1
    assert frequency_of(log, [p.rise for p in log.pulses]) == pytest.approx(440, abs=0.001)


@pytest.mark.parametrize("on_time", [25.5, 1.25, 0.333])
def test_sub_microsecond_on_time(coil: LoggingController, on_time: float):
    coil.set_limits(min_on_time=0)
    log = coil.play([(440, on_time)], 0.1)
    assert_pulses(log)
    assert log.pulses
    assert all(abs(p.width - log.ticks(on_time)) <= TOLERANCE_TICKS for p in log.pulses)


def test_max_on_time(coil: LoggingController):
    coil.set_limits(max_on_time=100)
    log = coil.play([(220, 150)], 0.2)
    assert_pulses(log)
    assert log.pulses
    assert all(abs(p.width - log.ticks(100)) <= TOLERANCE_TICKS for p in log.pulses)


def test_chord_of_16_voices(coil: LoggingController):
    log = coil.play([(frequency, 20) for frequency in CHORD_16], 0.5)
    assert_pulses(log)
    for voice, frequency in enumerate(CHORD_16):
        assert_on_grid(log, voice, frequency)


def test_duty_limit(coil: LoggingController):
    coil.set_limits(min_on_time=10, max_duty=0.05)
    log = coil.play([(frequency, 50) for frequency in CHORD_9], 0.5)
    assert_pulses(log)
    # the budget starts full, which adds its 10 ms of on-time to the average
    span = ticks_diff(log.pulses[-1].fall, log.pulses[0].rise)
    average = sum(p.width for p in log.pulses) / span
    assert average <= 0.05 + (0.05 * 10_000 + 50) / log.us(span)
    # the sparks get shorter, but not below the min on-time
    assert min(p.width for p in log.pulses) >= log.ticks(10) - TOLERANCE_TICKS
    assert min(p.width for p in log.pulses) < log.ticks(50) - TOLERANCE_TICKS


def test_queued_events_are_exact(coil: LoggingController):
    # 4 voices on at 100 ms, voice 0 from 440 to 880 Hz at 200 ms, all off at 300 ms
    coil.start_log()
    coil.stream_start(0)
    frequencies = [440, 554.365, 659.255, 880]
    events = [voice_on(100_000, voice, frequency, 20) for voice, frequency in enumerate(frequencies)]
    events += [voice_set(200_000, 0, 880, 20)]
    events += [voice_off(300_000, voice) for voice in range(len(frequencies))]
    for pending in coil.events(events):
        coil.link.wait(pending)
    coil.stream_run()
    sleep(0.5)
    status = coil.ping()
    log = coil.read_log()
    assert_pulses(log)
    assert status.late_events == 0

    # the first spark of the chord at exactly 100 ms, the others as soon as the coil is free
    on_tick = log.stream_tick(100_000)
    assert min(ticks_diff(s.tick, on_tick) for s in log.sparks) == 0
    # no sparks from 300 ms
    assert max(ticks_diff(s.tick, log.stream_tick(300_000)) for s in log.sparks) < 0
    # voice 0 changes its frequency at 200 ms
    change_tick = log.stream_tick(200_000)
    voice_0 = log.sparks_of(0)
    before = [t for t in voice_0 if ticks_diff(t, change_tick) < 0]
    after = [t for t in voice_0 if ticks_diff(t, change_tick) >= 0]
    assert frequency_of(log, before) == pytest.approx(440, abs=1)
    assert frequency_of(log, after) == pytest.approx(880, abs=2)


def test_watchdog(coil: LoggingController):
    # a voice without frames from the host stops after 1 s
    coil.start_log()
    coil.immediate([voice_on(0, 0, 440, 20)])
    sleep(1.5)
    log = coil.read_log()
    assert log.pulses
    span = log.us(ticks_diff(log.pulses[-1].rise, log.pulses[0].rise)) / 1e6
    assert 0.95 < span < 1.1


def test_broken_frame_is_counted(coil: LoggingController):
    coil.ping()
    coil.link.write_raw(frame(Type.PING, 99, b"")[:-3] + b"\x01\x00")
    sleep(0.05)
    assert coil.ping().rejected_frames == 1


def test_recovers_after_a_gap_in_the_sequence(coil: LoggingController):
    # a request after a gap is refused, the next request in sequence works
    coil.link.write_raw(frame(Type.PING, (coil.link.next_seq + 1) % 256, b""))
    sleep(0.05)
    assert coil.ping().state == StreamState.STOPPED


def test_events_need_a_started_stream(coil: LoggingController):
    with pytest.raises(RequestFailed) as error:
        coil.link.request(Type.EVENTS, bytes([1]) + voice_off(0, 0).pack())
    assert error.value.reply.status == Status.WRONG_STATE


def test_queue_full(coil: LoggingController):
    coil.stream_start(0)
    assert coil.info is not None
    events = [voice_off(1_000_000, 0)] * coil.info.queue_size
    replies = [coil.link.wait(pending) for pending in coil.events(events)]
    assert replies[-1].queue_free == 0
    full = coil.link.request(Type.EVENTS, bytes([1]) + voice_off(1_000_000, 0).pack(), check=False)
    assert full.status == Status.QUEUE_FULL


def test_many_events_at_once(coil: LoggingController):
    # all 16 voices change at the same time, like the first step of a song or a seek: working out that many voices at
    # once takes longer than arming a compare ahead
    coil.start_log()
    coil.stream_start(0)
    events = [voice_off(50_000, voice) for voice in range(16)]
    events += [voice_set(50_000, voice, frequency, 10) for voice, frequency in enumerate(CHORD_16)]
    events += [voice_off(250_000, voice) for voice in range(16)]
    for pending in coil.events(events):
        coil.link.wait(pending)
    coil.stream_run()
    sleep(0.4)
    log = coil.read_log()
    assert_pulses(log)
    for voice, frequency in enumerate(CHORD_16):
        assert_on_grid(log, voice, frequency)
    assert max(ticks_diff(s.tick, log.stream_tick(250_000)) for s in log.sparks) < 0


def test_dense_events_lose_no_frames(coil: LoggingController):
    # all 16 voices change every 20 ms for 2 s, 1600 events a second in full frames, which kept the controller busy
    # long enough to lose received bytes while its timer interrupt disabled all interrupts
    coil.stream_start(0)
    coil.ping()
    # the counters of a ping count since the previous one
    statuses: list[PingStatus] = []
    frame = 20_000
    for start in range(0, 2_000_000, 250_000):
        events = [
            voice_set(time, voice, frequency * (1 + 0.01 * (time // frame % 5)), 10)
            for time in range(start, start + 250_000, frame)
            for voice, frequency in enumerate(CHORD_16)
        ]
        for pending in coil.events(events):
            coil.link.wait(pending)
        if start == 0:
            coil.stream_run()
        # stay about 250 ms ahead of the clock
        while coil.link.clock + 250_000 < start:
            sleep(0.01)
            statuses.append(coil.ping())
    sleep(2.3)
    statuses.append(coil.ping())
    assert sum(status.rejected_frames for status in statuses) == 0
    assert sum(status.late_events for status in statuses) == 0
