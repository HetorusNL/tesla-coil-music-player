#!/usr/bin/env python
#
# Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
#
# This file is part of the Tesla Coil Music Player.

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from time import perf_counter, sleep

from gui.coil_model import CoilModel
from gui.serial_handler import SerialHandler
from gui.song import PartKey, Song

# message types understood by the tesla coil controller
TYPE_MIDI_START = 0x01
TYPE_MIDI_MSG = 0x02
TYPE_MIDI_END = 0x03
TYPE_RESET = 0xFF
TYPE_NAMES = {TYPE_MIDI_START: "midi start", TYPE_MIDI_MSG: "midi", TYPE_MIDI_END: "midi end", TYPE_RESET: "reset"}
# control values of the replies of the tesla coil controller
STX = 0x02
ETX = 0x03
ACK = 0x06
NACK = 0x15
DEBUG = 0xFF

# MIDI channel used for notes played live on the keyboard
LIVE_CHANNEL = 0


# the number of log lines kept, the debug messages add two lines per note
MAX_LOG_LINES = 5000


@dataclass(frozen=True)
class LogLine:
    number: int
    debug: bool
    text: str


def hex_bytes(data: list[int]) -> str:
    return " ".join(f"{byte:02x}" for byte in data)


class PlayerState(Enum):
    DISCONNECTED = "disconnected"
    IDLE = "idle"
    PLAYING = "playing"


class CoilPlayer:
    """Plays songs and live notes on the tesla coil controller, shared by all GUI clients."""

    def __init__(self, song_dir: Path):
        # the folder new browser tabs start browsing in
        self.song_dir = song_dir
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
        self.notes_on = 0
        self.notes_off = 0

        self._sh: SerialHandler | None = None
        self._serial_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._live_notes: set[int] = set()
        # what the controller is playing, for the meter
        self.coil_model = CoilModel()

    @property
    def state(self) -> PlayerState:
        if self._sh is None:
            return PlayerState.DISCONNECTED
        if self._thread is not None and self._thread.is_alive():
            return PlayerState.PLAYING
        return PlayerState.IDLE

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
        self.muted_version += 1

    def connect(self, port_name: str):
        self.disconnect()
        # setup the serial connection with the tesla coil controller
        sh = SerialHandler(port_name)
        sh.open()
        self._sh = sh
        self.port_name = port_name
        self.log(f"connected to {port_name}")
        sleep(0.05)

        # force a reset of the tesla coil controller, the first one syncs the sequence number
        with self._serial_lock:
            for _ in range(2):
                self._reset()

    def disconnect(self):
        if self._sh is None:
            return
        self.stop()
        self.live_release_all()
        with self._serial_lock:
            self._reset()
            self._sh.close()
        self._sh = None
        self.coil_model.clear()
        self.log(f"disconnected from {self.port_name}")
        self.port_name = None

    def reset(self):
        self.stop()
        with self._serial_lock:
            self.log("resetting tesla coil controller...")
            self._reset()
        self._live_notes.clear()

    def play(self, song: Song, start: float = 0.0):
        if self._sh is None:
            raise RuntimeError("not connected to the tesla coil controller")
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
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join()
        self._thread = None

    def live_note_on(self, note: int, velocity: int = 100):
        if self.state != PlayerState.IDLE or note in self._live_notes:
            return
        self._live_notes.add(note)
        with self._serial_lock:
            self._send_midi([0x90 | LIVE_CHANNEL, note, velocity], 0)

    def live_note_off(self, note: int):
        if self._sh is None or note not in self._live_notes:
            return
        self._live_notes.discard(note)
        with self._serial_lock:
            self._send_midi([0x80 | LIVE_CHANNEL, note, 0], 0)

    def live_release_all(self):
        for note in list(self._live_notes):
            self.live_note_off(note)

    def _reset(self):
        self.coil_model.clear()
        self._write([TYPE_RESET, 0x00])
        sleep(0.01)
        self._read()

    def _write(self, msg: list[int]):
        assert self._sh is not None
        name = TYPE_NAMES.get(msg[0], f"type {msg[0]:02x}")
        data = f": {hex_bytes(msg[2:])}" if msg[2:] else ""
        self.log(f"→ {name} seq {self._sh.sequence_number}{data}", debug=True)
        self._sh.write_msg(msg)

    def _read(self):
        assert self._sh is not None
        _, msg = self._sh.read_msg()
        if not msg:
            return
        # the read can contain multiple replies, the regular ACKs are only interesting when debugging
        replies: list[str] = []
        while len(msg) >= 4 and msg[0] == STX and msg[1] == ACK and msg[3] == ETX:
            replies.append(f"ACK seq {msg[2]}")
            msg = msg[4:]
        debug = True
        if len(msg) >= 3 and msg[0] == STX and msg[1] == NACK:
            replies.append(f"NACK, expected seq {msg[2]}, received {hex_bytes(msg[4:])}")
            debug = False
        elif len(msg) >= 3 and msg[0] == STX and msg[1] == DEBUG:
            replies.append(f'controller debug: "{bytes(msg[4:]).decode("utf-8", "ignore")}"')
        elif msg:
            replies.append(hex_bytes(msg))
        self.log(f"← {', '.join(replies)}", debug=debug)

    def _send_midi(self, midi_bytes: list[int], delay: float):
        # the delay (in ms) since the previous message is sent along with the MIDI message
        time = min(int(delay * 1000), 0xFFFF)
        midi_msg = [*midi_bytes, time // 256, time % 256]
        self._write([TYPE_MIDI_MSG, len(midi_msg), *midi_msg])
        self._read()
        self.coil_model.handle_midi(midi_bytes)

    def _play_song(self, song: Song, start: float):
        self.log(f'playing "{song.name}"' + (f" from {start:.1f}s" if start else ""))

        # enter midi mode
        with self._serial_lock:
            self._write([TYPE_MIDI_START, 0x00])
            sleep(0.05)
            self._read()

        # play the whole song
        terminated = self._play_note_events(song, start)

        # exit midi mode
        with self._serial_lock:
            self._write([TYPE_MIDI_END, 0x00])
            sleep(0.01)
            self._read()

        # log some statistics
        song_end = "stopped" if terminated else "finished"
        self.log(f'{song_end} playing "{song.name}" (notes_on: {self.notes_on}, notes_off: {self.notes_off})')

    def _play_note_events(self, song: Song, start: float) -> bool:
        # notes that are currently on, so their note_off is sent even when the part is muted halfway
        sounding: set[tuple[int, int]] = set()
        try:
            return self._play_sounding_note_events(song, start, sounding)
        finally:
            # the controller doesn't release the notes at the end of the stream, so turn them off here
            with self._serial_lock:
                for channel, note in sounding:
                    self._send_midi([0x80 | channel, note, 0], 0)

    def _wait_until(self, time: float) -> bool:
        """Waits until the perf_counter time, returns whether playback was stopped meanwhile."""
        delay = time - perf_counter()
        if delay > 0:
            return self._stop_event.wait(delay)
        return self._stop_event.is_set()

    def _play_sounding_note_events(self, song: Song, start: float, sounding: set[tuple[int, int]]) -> bool:
        # schedule the events against the start time, so the serial overhead doesn't accumulate
        start_time = perf_counter() - start
        previous_time = start
        for event in song.events:
            # when starting halfway, the notes before the start (and ones still held at the start) are skipped
            if event.time < start:
                continue
            # wait for muted notes too, so the song keeps playing when all parts are muted
            if self._wait_until(start_time + event.time):
                return True
            self.position = event.time
            note = (event.channel, event.note)
            if event.is_note_on:
                if self.is_muted(song, event.part) or note in sounding:
                    continue
                sounding.add(note)
                self.notes_on += 1
            else:
                if note not in sounding:
                    continue
                sounding.discard(note)
                self.notes_off += 1
            with self._serial_lock:
                self._send_midi(event.midi_bytes, event.time - previous_time)
            previous_time = event.time
        # play the silence until the end of the song as well
        if self._wait_until(start_time + song.length):
            return True
        self.position = song.length
        return False
