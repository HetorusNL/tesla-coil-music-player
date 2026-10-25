#
# Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
#
# This file is part of the Tesla Coil Music Player.

"""Tests of the player of the GUI against the simulated controller, in real time."""

from collections.abc import Callable, Iterator
from pathlib import Path
from time import monotonic, sleep

import pytest

import gui.coil_player
from gui.coil_player import LOOKAHEAD, CoilPlayer, PlayerState
from gui.config import SparkConfig
from gui.protocol import Controller, Kind, Type
from gui.song import Song
from tests.simulated_controller import SimulatedController
from tests.test_planner import write_song


@pytest.fixture
def controller() -> SimulatedController:
    return SimulatedController()


@pytest.fixture
def player(controller: SimulatedController, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[CoilPlayer]:
    # the player opens the simulated controller instead of a serial port
    def open_simulated(port: str, on_log: Callable[[int, str], None] | None) -> Controller:
        return Controller(controller, on_log)

    monkeypatch.setattr(gui.coil_player, "Controller", open_simulated)
    player = CoilPlayer(tmp_path, tmp_path / "config.json")
    player.set_spark_config(SparkConfig(volume=0.5, min_on_time=5))
    player.connect("simulated")
    yield player
    player.disconnect()


@pytest.fixture
def song(tmp_path: Path) -> Song:
    # a chord C4 E4 G4 on channel 0 for beats 0-2, A4 on channel 1 for beats 0-4 (120 bpm, 2 s)
    return write_song(tmp_path / "song.mid", [(0, 60, 0, 2), (0, 64, 0, 2), (0, 67, 0, 2), (1, 69, 0, 4)])


def wait_until_done(player: CoilPlayer, timeout: float = 10):
    end = monotonic() + timeout
    while player.state == PlayerState.PLAYING and monotonic() < end:
        sleep(0.02)


def assert_in_time(controller: SimulatedController):
    """Every queued event arrived before its time, and nothing sounds at the end."""
    assert controller.late_events == 0 and not any(applied.late for applied in controller.applied)
    assert controller.watchdog_trips == 0
    assert not controller.voices


def test_plays_a_song(player: CoilPlayer, controller: SimulatedController, song: Song):
    player.play(song)
    wait_until_done(player)
    assert player.position == song.length
    assert_in_time(controller)
    ons = [a for a in controller.applied if a.kind == Kind.VOICE_ON]
    # the chord and the melody start at 0 (the first step starts them with VOICE_SET), the chord stops at 1 s
    starts = [a for a in controller.applied if a.time == 0 and a.kind == Kind.VOICE_SET]
    assert len(starts) + len(ons) == 4
    offs = [a for a in controller.applied if a.kind == Kind.VOICE_OFF and a.time == 1_000_000]
    assert len(offs) == 3
    # the events arrive about the lookahead ahead of their time
    queued = [a for a in controller.applied if a.arrived >= 0 and a.time > 0]
    assert all(a.time - a.arrived > LOOKAHEAD * 0.8 for a in queued if a.time < 1_900_000)


def test_pause_and_resume(player: CoilPlayer, controller: SimulatedController, song: Song):
    player.play(song)
    sleep(0.5)
    player.pause()
    assert player.state == PlayerState.PAUSED
    paused_at = player.position
    assert 0.45 < paused_at < 0.56
    assert not controller.voices and controller.state == 0
    player.play(song, paused_at)
    sleep(0.1)
    # the 4 notes sound again from where the song paused
    assert len(controller.voices) == 4
    wait_until_done(player)
    assert_in_time(controller)
    assert controller.requests.count(Type.STREAM_START) == 2


def test_mute_a_part_halfway(player: CoilPlayer, controller: SimulatedController, song: Song):
    melody = next(part for part in song.parts if part[1] == 1)
    player.play(song)
    sleep(0.6)
    player.set_muted(song, [melody], True)
    sleep(0.15)
    # the melody stopped, the chord plays on
    assert len(controller.voices) == 3
    assert Type.FLUSH in controller.requests
    wait_until_done(player)
    assert_in_time(controller)


def test_silence_in_a_song_keeps_the_connection(player: CoilPlayer, controller: SimulatedController, tmp_path: Path):
    # a note, 1.5 s of silence, a note: the pings keep the watchdog quiet
    song = write_song(tmp_path / "silence.mid", [(0, 60, 0, 1), (0, 64, 4, 5)])
    player.play(song)
    wait_until_done(player)
    assert_in_time(controller)
    assert [a.time for a in controller.applied if a.kind == Kind.VOICE_ON] == [0, 2_000_000]


def test_pitch_bend(player: CoilPlayer, controller: SimulatedController, tmp_path: Path):
    # A4 for 2 beats, bent up a whole tone in steps over the first beat
    sweep = [(0, step / 20, round(8191 * step / 20)) for step in range(21)]
    song = write_song(tmp_path / "bend.mid", [(0, 69, 0, 2)], bends=sweep)
    player.play(song)
    wait_until_done(player)
    assert_in_time(controller)
    sets = [a for a in controller.applied if a.kind == Kind.VOICE_SET and a.voice == 0 and a.time > 0]
    # the voice follows the bends without restarting
    assert len(sets) == 20
    assert not [a for a in controller.applied if a.kind == Kind.VOICE_ON and a.time > 0]
    assert sets[-1].frequency == round(440 * 2 ** (8191 / 8192 * 2 / 12) * 1000)
