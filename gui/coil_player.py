#!/usr/bin/env python
#
# Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
#
# This file is part of the Tesla Coil Music Player.

import threading
from collections import deque
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from time import monotonic, sleep

from gui.coil_model import COIL_VOICES, CoilModel, MeterReading, note_frequency, spark_on_time
from gui.config import ABSOLUTE_MAX_ON_TIME, SparkConfig, load_config, save_config
from gui.planner import Step, plan_song
from gui.protocol import (
    ConnectionLost,
    Controller,
    Event,
    Kind,
    Pending,
    RequestFailed,
    Status,
    Type,
    voice_on,
    voice_set,
)
from gui.song import PartKey, Song

# time between the attempts to reconnect after the connection with the controller was lost
RECONNECT_INTERVAL = 0.5
# the controller is pinged this often while connected, also while playing, so its watchdog keeps the coil on
PING_INTERVAL = 0.25
# the events of a song are sent this far (us) ahead of the clock of the controller
LOOKAHEAD = 500_000
# a change of the muted parts or the settings applies this far (us) ahead, after the queued events are flushed
REPLAN_DELAY = 50_000
# how often the playback thread fills the queue and follows the clock (s)
PLAYBACK_INTERVAL = 0.02
# stable names of the USB serial devices, the device itself can get another name when it reconnects
SERIAL_BY_ID = Path("/dev/serial/by-id")

# the number of log lines kept
MAX_LOG_LINES = 5000


@dataclass(frozen=True)
class LogLine:
    number: int
    debug: bool
    text: str


def stable_port_path(port_name: str) -> Path | None:
    """Returns the /dev/serial/by-id path of the serial port, if it has one."""
    try:
        return next(path for path in SERIAL_BY_ID.iterdir() if path.resolve() == Path(port_name).resolve())
    except OSError, StopIteration:
        return None


class Delivery(Enum):
    """What the controller replied to a request."""

    ACKNOWLEDGED = "acknowledged"
    REJECTED = "rejected"
    NO_REPLY = "no reply"


class PlayerState(Enum):
    DISCONNECTED = "disconnected"
    IDLE = "idle"
    PLAYING = "playing"
    PAUSED = "paused"
    RECONNECTING = "reconnecting"


class CoilPlayer:
    """Plays songs and live notes on the tesla coil controller, shared by all GUI clients."""

    def __init__(self, song_dir: Path, config_path: Path):
        # the folder new browser tabs start browsing in
        self.song_dir = song_dir
        # the settings of the sparks, the limits are sent to the controller after connecting
        self.config_path = config_path
        self.spark_config = load_config(config_path)
        self.config_version = 0
        # loaded songs per path, with the modification time they were loaded at (None when not a valid MIDI file)
        self._song_cache: dict[Path, tuple[float, Song | None]] = {}
        self.songs_version = 0
        self.log_lines: deque[LogLine] = deque(maxlen=MAX_LOG_LINES)
        self._log_number = 0
        self._log_lock = threading.Lock()

        # muted parts per song, shared so every client (and the running playback) sees the same
        self.muted: dict[Path, set[PartKey]] = {}
        self.muted_version = 0

        self.port_name: str | None = None
        self.current_song: Song | None = None
        self.position = 0.0
        # a paused song resumes from its position, the controller is idle meanwhile
        self.paused = False
        self._pausing = False
        self.notes_on = 0
        self.notes_off = 0

        self._controller: Controller | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # reconnecting after a lost connection, by the playback thread (to resume the song) or in the background
        self._stable_port: Path | None = None
        self._reconnecting = False
        self._cancel_reconnect = threading.Event()
        self._reconnect_thread: threading.Thread | None = None
        # the live notes and the voices they play on
        self._live_notes: dict[int, tuple[int, int]] = {}  # note: (voice, velocity)
        self._live_lock = threading.Lock()
        # what the controller is playing, for the meter
        self.coil_model = CoilModel()
        threading.Thread(target=self._watch_connection, daemon=True).start()

    @property
    def state(self) -> PlayerState:
        # a song keeps playing while the playback thread reconnects to resume it
        if self._thread is not None and self._thread.is_alive():
            return PlayerState.PLAYING
        if self._reconnecting:
            return PlayerState.RECONNECTING
        if self._controller is None:
            return PlayerState.DISCONNECTED
        if self.paused:
            return PlayerState.PAUSED
        return PlayerState.IDLE

    @property
    def reconnecting(self) -> bool:
        return self._reconnecting

    @property
    def num_voices(self) -> int:
        info = self._controller.info if self._controller is not None else None
        return info.num_voices if info is not None else COIL_VOICES

    def log(self, line: str, debug: bool = False):
        with self._log_lock:
            self._log_number += 1
            self.log_lines.append(LogLine(self._log_number, debug, f"{datetime.now():%H:%M:%S.%f}"[:-3] + f" {line}"))

    def log_since(self, number: int) -> list[LogLine]:
        """Returns the log lines after the given log line number."""
        with self._log_lock:
            return [line for line in self.log_lines if line.number > number]

    def list_folder(self, folder: Path) -> tuple[list[Path], list[Song]]:
        """Returns the (non-hidden) subfolders and the songs in the folder."""
        folders: list[Path] = []
        songs: list[Song] = []
        try:
            paths = sorted(folder.iterdir(), key=lambda p: p.name.lower())
        except OSError as e:
            self.log(f"can't open {folder}: {e}")
            return folders, songs
        for path in paths:
            if path.name.startswith("."):
                continue
            if path.is_dir():
                folders.append(path)
            elif path.suffix.lower() in [".mid", ".midi"] and path.is_file():
                song = self.load_song(path)
                if song is not None:
                    songs.append(song)
        return folders, songs

    def load_song(self, path: Path) -> Song | None:
        mtime = path.stat().st_mtime
        cached = self._song_cache.get(path)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        song: Song | None = None
        try:
            song = Song(path)
        except Exception as e:
            self.log(f'skipping "{path.name}": {e}')
        self._song_cache[path] = (mtime, song)
        return song

    def songs_changed(self):
        # let every browser tab reload its folder
        self.songs_version += 1

    def is_muted(self, song: Song, part: PartKey) -> bool:
        return part in self.muted.get(song.path, set())

    def set_muted(self, song: Song, parts: list[PartKey], muted: bool):
        song_muted = self.muted.setdefault(song.path, set())
        if muted:
            song_muted.update(parts)
        else:
            song_muted.difference_update(parts)
        # a playing song follows it
        self.muted_version += 1

    def connect(self, port_name: str):
        self.disconnect()
        self._cancel_reconnect.clear()
        self._stable_port = stable_port_path(port_name)
        self._open(port_name)

    def disconnect(self):
        # also cancels reconnecting, the playback thread checks it as well
        self._cancel_reconnect.set()
        self.stop()
        if self._reconnect_thread is not None:
            self._reconnect_thread.join()
            self._reconnect_thread = None
        if self.port_name is None:
            return
        if self._controller is not None:
            with suppress(ConnectionLost, RequestFailed):
                self._controller.stop()
        self._close()
        self.log(f"disconnected from {self.port_name}")
        self.port_name = None

    def reset(self):
        self.stop()
        try:
            self.log("resetting tesla coil controller...")
            self._require().stop()
        except ConnectionLost as e:
            self._connection_lost(e)
        self._clear_live_notes()

    def set_spark_config(self, config: SparkConfig) -> Delivery | None:
        """Saves the settings and sends them to the controller, returns its reply (None when not connected).

        Raises ValueError for invalid settings. A playing song and the live notes follow the new settings."""
        if errors := config.errors():
            raise ValueError(", ".join(errors))
        save_config(self.config_path, config)
        self.spark_config = config
        self.config_version += 1
        if self._controller is None:
            return None
        try:
            self._send_limits()
            self._update_live_notes()
            return Delivery.ACKNOWLEDGED
        except RequestFailed as e:
            self.log(f"the controller rejected the settings: {e}")
            return Delivery.REJECTED
        except ConnectionLost as e:
            # the reconnect sends the settings as well
            self._connection_lost(e)
            return None

    def play(self, song: Song, start: float = 0.0):
        if self._controller is None:
            reconnecting = "still reconnecting to" if self._reconnecting else "not connected to"
            raise RuntimeError(f"{reconnecting} the tesla coil controller")
        self.stop()
        self.live_release_all()
        self.current_song = song
        self.position = start
        self.notes_on = 0
        self.notes_off = 0
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._play_song, args=(song, start), daemon=True)
        self._thread.start()

    def stop(self):
        self.paused = False
        self._stop_thread()

    def pause(self):
        if self.state != PlayerState.PLAYING or self.current_song is None:
            return
        # the playback thread keeps the position where the controller stopped
        self._pausing = True
        self._stop_thread()
        self._pausing = False
        self.paused = True

    def pause_at(self, song: Song, position: float):
        """Holds the song paused at the position, playing resumes it from there."""
        if self._controller is None:
            raise RuntimeError("not connected to the tesla coil controller")
        self.stop()
        self.live_release_all()
        self.current_song = song
        self.position = min(max(position, 0.0), song.length)
        self.notes_on = 0
        self.notes_off = 0
        self.paused = True

    def seek_paused(self, position: float):
        """Moves the position a paused song resumes from."""
        if self.paused and self.current_song is not None:
            self.position = min(max(position, 0.0), self.current_song.length)

    def _stop_thread(self):
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join()
        self._thread = None

    # --- live notes ---

    def live_note_on(self, note: int, velocity: int = 100):
        if self.state not in [PlayerState.IDLE, PlayerState.PAUSED]:
            return
        with self._live_lock:
            used = {voice for voice, _ in self._live_notes.values()}
            voice = next((v for v in range(self.num_voices) if v not in used), None)
            if note in self._live_notes or voice is None:
                return
            self._live_notes[note] = (voice, velocity)
        frequency = note_frequency(note)
        try:
            self._require().immediate(
                [voice_on(0, voice, frequency, spark_on_time(frequency, velocity, self.spark_config))]
            )
        except ConnectionLost as e:
            self._connection_lost(e)
        self._show_live_notes()

    def live_note_off(self, note: int):
        with self._live_lock:
            if self._controller is None or note not in self._live_notes:
                return
            voice, _ = self._live_notes.pop(note)
        try:
            self._require().immediate([Event(0, Kind.VOICE_OFF, voice)])
        except ConnectionLost as e:
            self._connection_lost(e)
        self._show_live_notes()

    def live_release_all(self):
        for note in list(self._live_notes):
            self.live_note_off(note)

    def _update_live_notes(self):
        """The live notes follow the new settings."""
        with self._live_lock:
            notes = list(self._live_notes.items())
        events = []
        for note, (voice, velocity) in notes:
            frequency = note_frequency(note)
            events.append(voice_set(0, voice, frequency, spark_on_time(frequency, velocity, self.spark_config)))
        if events:
            self._require().immediate(events)

    def _show_live_notes(self):
        with self._live_lock:
            notes = tuple((note, velocity) for note, (_, velocity) in self._live_notes.items())
        self.coil_model.set_reading(MeterReading(notes, 0))

    def _clear_live_notes(self):
        with self._live_lock:
            self._live_notes.clear()
        self.coil_model.clear()

    # --- connection ---

    def _require(self) -> Controller:
        controller = self._controller
        if controller is None:
            raise ConnectionLost("not connected")
        return controller

    def _open(self, port_name: str):
        controller = Controller(port_name, self._log_frame)
        try:
            info = controller.hello()
            self._controller = controller
            self._send_limits()
        except (ConnectionLost, RequestFailed, OSError) as e:
            controller.close()
            self._controller = None
            raise OSError(f"the controller on {port_name} doesn't answer the protocol: {e}") from e
        self.port_name = port_name
        self._clear_live_notes()
        self.log(f"connected to {port_name}: protocol {info.version}, {info.num_voices} voices")

    def _close(self):
        controller, self._controller = self._controller, None
        if controller is not None:
            # closing a port that's gone fails as well
            with suppress(OSError):
                controller.close()
        self._clear_live_notes()

    def _connection_lost(self, error: Exception):
        # the playback thread runs into it as well, and reconnects to resume the song
        if self.state == PlayerState.PLAYING:
            return
        if self._controller is not None:
            self.log(f"lost the connection with {self.port_name}: {error}")
        self._close()
        self._reconnect_in_background()

    def _log_frame(self, type: int, text: str):
        # the pings come 4 times a second, they would flood the debug log
        if type != Type.PING:
            self.log(text, debug=True)

    def _send_limits(self):
        config = self.spark_config
        self._require().set_limits(min(config.max_on_time, ABSOLUTE_MAX_ON_TIME), config.min_on_time, config.max_duty)

    def _reconnect(self, abort: threading.Event | None = None) -> bool:
        """Reconnects until it succeeds (returns True), disconnect() cancels it or the abort event is set."""
        self._reconnecting = True
        self.log(f"reconnecting to {self.port_name}...")
        try:
            while not self._cancel_reconnect.is_set() and not (abort is not None and abort.is_set()):
                # follow the stable name, as the device can come back under another name
                port = str(self._stable_port.resolve()) if self._stable_port is not None else self.port_name
                if port is not None and Path(port).exists():
                    try:
                        self._open(port)
                        return True
                    except OSError as e:
                        self.log(f"could not reconnect yet: {e}", debug=True)
                self._cancel_reconnect.wait(RECONNECT_INTERVAL)
            return False
        finally:
            self._reconnecting = False

    def _reconnect_in_background(self):
        if self.port_name is None or self._cancel_reconnect.is_set():
            return
        if self._reconnect_thread is not None and self._reconnect_thread.is_alive():
            return
        # already reconnecting before the thread starts, so the state doesn't show disconnected meanwhile
        self._reconnecting = True
        self._reconnect_thread = threading.Thread(target=self._reconnect, daemon=True)
        self._reconnect_thread.start()

    def _watch_connection(self):
        # pings the controller while connected: a lost connection shows up without playing a song, and the watchdog of
        # the controller keeps the coil on during the silences of a song
        while True:
            sleep(PING_INTERVAL)
            controller = self._controller
            if controller is None or self._reconnecting:
                continue
            try:
                status = controller.ping()
                if status.late_events:
                    self.log(f"{status.late_events} events reached the controller too late")
                if status.rejected_frames:
                    self.log(f"the controller rejected {status.rejected_frames} broken frames", debug=True)
            except ConnectionLost as e:
                if self._controller is controller:
                    self._connection_lost(e)
            except RequestFailed:
                pass

    # --- playback ---

    def _play_song(self, song: Song, start: float):
        self.log(f'playing "{song.name}"' + (f" from {start:.1f}s" if start else ""))
        while True:
            try:
                terminated = self._stream_song(song, start)
                break
            except (ConnectionLost, OSError) as e:
                self.log(f"lost the connection with {self.port_name}: {e}")
                self._close()
                # resume the song once reconnected, from where the controller was
                if not self._reconnect(abort=self._stop_event):
                    # stopped while reconnecting, keep reconnecting unless it was disconnect() that stopped it
                    self._reconnect_in_background()
                    terminated = True
                    break
                start = self.position
                self.log(f'resuming "{song.name}" from {start:.1f}s')
            except RequestFailed as e:
                self.log(f'stopped playing "{song.name}": {e}')
                terminated = True
                break
        self.coil_model.clear()

        # log some statistics
        song_end = "paused" if self._pausing else "stopped playing" if terminated else "finished playing"
        self.log(f'{song_end} "{song.name}" (notes_on: {self.notes_on}, notes_off: {self.notes_off})')

    def _plan(self, song: Song, start: int) -> Iterator[Step]:
        return plan_song(song, lambda part: not self.is_muted(song, part), self.spark_config, self.num_voices, start)

    def _stream_song(self, song: Song, start: float) -> bool:
        """Plays the song from the start (s), returns whether it was stopped before its end."""
        controller = self._require()
        link = controller.link
        end = round(song.length * 1e6)
        plan_version = (self.muted_version, self.config_version)
        steps = self._plan(song, round(start * 1e6))
        # the steps sent to the controller, for the meter as the clock passes them
        timeline: deque[Step] = deque()
        pendings: deque[Pending] = deque()
        next_step: Step | None = next(steps, None)

        def send_until(time: int):
            # the events of the steps up to the time, without waiting for the replies
            nonlocal next_step
            events: list[Event] = []
            while next_step is not None and next_step.time <= time:
                events.extend(next_step.events)
                timeline.append(next_step)
                self.notes_on += sum(event.kind == Kind.VOICE_ON for event in next_step.events)
                self.notes_off += sum(event.kind == Kind.VOICE_OFF for event in next_step.events)
                next_step = next(steps, None)
            pendings.extend(controller.events(events))

        def check_replies():
            while pendings and pendings[0].done.is_set():
                reply = link.wait(pendings.popleft(), check=False)
                if reply.status != Status.OK:
                    raise RequestFailed(reply)

        controller.stream_start(round(start * 1e6))
        send_until(round(start * 1e6) + LOOKAHEAD)
        controller.stream_run()
        running = True
        while True:
            if self._stop_event.wait(PLAYBACK_INTERVAL):
                reply = controller.stop()
                self.position = min(reply.clock / 1e6, song.length)
                return True
            check_replies()
            # the clock of the controller, from its latest reply
            clock = link.clock + round((monotonic() - link.clock_time) * 1e6) if running else link.clock
            self.position = min(clock / 1e6, song.length)
            while timeline and timeline[0].time <= clock:
                self.coil_model.set_reading(timeline.popleft().reading)

            # muting a part or changing a setting replaces the queued events, from a moment ahead
            if (self.muted_version, self.config_version) != plan_version:
                plan_version = (self.muted_version, self.config_version)
                for pending in pendings:
                    link.wait(pending, check=False)
                pendings.clear()
                controller.flush()
                replan = max(link.clock + REPLAN_DELAY, clock + REPLAN_DELAY)
                while timeline and timeline[-1].time >= replan:
                    timeline.pop()
                steps = self._plan(song, replan)
                next_step = next(steps, None)

            send_until(clock + LOOKAHEAD)
            if next_step is None and clock >= end:
                controller.stop()
                self.position = song.length
                return False
