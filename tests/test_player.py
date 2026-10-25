#
# Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
#
# This file is part of the Tesla Coil Music Player.

"""Tests of the player of the GUI on the controller running the test firmware, skipped without --port."""

from collections.abc import Iterator
from pathlib import Path
from time import monotonic, sleep

import pytest

from gui.coil_player import CoilPlayer, PlayerState
from gui.config import SparkConfig
from gui.song import Song
from tests.sparks import Log, LoggingController, assert_pulses, frequency_of, ticks_diff
from tests.test_planner import write_song


@pytest.fixture(scope="module")
def player(port: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[CoilPlayer]:
    directory = tmp_path_factory.mktemp("player")
    player = CoilPlayer(directory, directory / "config.json")
    player.set_spark_config(SparkConfig(volume=1.0, min_on_time=5, max_on_time=100, max_duty=0.15))
    player.connect(port)
    yield player
    player.disconnect()


@pytest.fixture
def spark_log(player: CoilPlayer) -> LoggingController:
    assert player.controller is not None
    return LoggingController.sharing(player.controller)


@pytest.fixture
def song(tmp_path: Path) -> Song:
    # a chord C4 E4 G4 on channel 0 for beats 0-2, A4 on channel 1 for beats 0-4 (120 bpm, 2 s)
    return write_song(tmp_path / "song.mid", [(0, 60, 0, 2), (0, 64, 0, 2), (0, 67, 0, 2), (1, 69, 0, 4)])


def wait_until_done(player: CoilPlayer):
    end = monotonic() + 10
    while player.state == PlayerState.PLAYING and monotonic() < end:
        sleep(0.05)


def voices_by_frequency(log: Log) -> dict[int, list[int]]:
    """The spark ticks of every voice that sparked, by its frequency (Hz, rounded)."""
    voices = {voice: log.sparks_of(voice) for voice in range(16)}
    return {round(frequency_of(log, ticks)): ticks for ticks in voices.values() if len(ticks) > 2}


def test_plays_a_song(player: CoilPlayer, spark_log: LoggingController, song: Song):
    spark_log.start_log()
    player.play(song)
    wait_until_done(player)
    log = spark_log.read_log()
    assert_pulses(log)
    assert player.position == song.length
    voices = voices_by_frequency(log)
    assert sorted(voices) == [262, 330, 392, 440]
    # the chord for 1 s, the melody for 2 s, the first spark exactly at the start of the stream
    assert log.us(ticks_diff(voices[440][-1], voices[440][0])) / 1e6 == pytest.approx(2.0, abs=0.01)
    for frequency in [262, 330, 392]:
        assert log.us(ticks_diff(voices[frequency][-1], voices[frequency][0])) / 1e6 == pytest.approx(1.0, abs=0.01)
    assert min(ticks_diff(ticks[0], log.stream_tick(0)) for ticks in voices.values()) == 0
    assert not any("late" in line.text for line in player.log_since(0))


def test_pause_and_resume(player: CoilPlayer, spark_log: LoggingController, song: Song):
    player.play(song)
    sleep(0.5)
    player.pause()
    paused_at = player.position
    assert player.state == PlayerState.PAUSED and 0.45 < paused_at < 0.56
    # silent while paused
    spark_log.start_log()
    sleep(0.3)
    assert not spark_log.read_log().sparks
    # the chord sounds until 1 s, the melody until 2 s of the song
    spark_log.start_log()
    player.play(song, paused_at)
    wait_until_done(player)
    log = spark_log.read_log()
    assert_pulses(log)
    resumed = log.stream_tick(round(paused_at * 1e6))
    for frequency, ticks in voices_by_frequency(log).items():
        end = 2.0 if frequency == 440 else 1.0
        assert log.us(ticks_diff(ticks[-1], resumed)) / 1e6 == pytest.approx(end - paused_at, abs=0.01)


def test_mute_a_part_halfway(player: CoilPlayer, spark_log: LoggingController, song: Song):
    melody = next(part for part in song.parts if part[1] == 1)
    spark_log.start_log()
    player.play(song)
    sleep(0.6)
    muted_at = player.position
    player.set_muted(song, [melody], True)
    wait_until_done(player)
    player.set_muted(song, [melody], False)
    log = spark_log.read_log()
    assert_pulses(log)
    voices = voices_by_frequency(log)
    # the melody stops within the replan delay and a playback interval, the chord plays on without a gap
    melody_end = log.us(ticks_diff(voices[440][-1], log.stream_tick(0))) / 1e6
    assert muted_at < melody_end < muted_at + 0.1
    for frequency in [262, 330, 392]:
        ticks = voices[frequency]
        assert max(ticks_diff(b, a) for a, b in zip(ticks, ticks[1:])) < log.ticks(5000)


def test_live_notes(player: CoilPlayer, spark_log: LoggingController):
    spark_log.start_log()
    player.live_note_on(69)
    sleep(0.2)
    player.live_note_on(72)
    sleep(0.2)
    assert sorted(note for note, _ in player.coil_model.reading().notes) == [69, 72]
    player.live_release_all()
    sleep(0.05)
    log = spark_log.read_log()
    assert_pulses(log)
    assert sorted(voices_by_frequency(log)) == [440, 523]
