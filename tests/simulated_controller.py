#
# Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
#
# This file is part of the Tesla Coil Music Player.

"""A controller in Python, to test the host without hardware.

It's a serial port with the protocol of the firmware behind it (controller/src/protocol.cpp and sequencer.cpp): the
sequence numbers and the replayed replies, the stream clock on the time of the host, the event queue and the
watchdog. Instead of sparks, it records the events it applies and the voices they leave sounding.
"""

import struct
import threading
from dataclasses import dataclass
from time import monotonic

from gui.protocol import Kind, Status, StreamState, Type, cobs_decode, crc16, frame

NUM_VOICES = 16
QUEUE_SIZE = 4095
CLOCK_HZ = 108_000_000
ABSOLUTE_MAX_ON_TIME = 200_000
ABSOLUTE_MAX_DUTY = 1500
MIN_OFF_TIME = 10_000
# like the firmware: events are applied this long (us) ahead, running starts this long after STREAM_RUN
EVENT_LEAD = 100
RUN_DELAY = 2000
WATCHDOG_TIME = 1.0


@dataclass(frozen=True)
class Applied:
    """An event the controller applied."""

    time: int  # us of the stream clock it was meant for, the clock for an immediate event
    kind: Kind
    voice: int
    frequency: int  # mHz
    on_time: int  # ns
    arrived: int  # the clock when it arrived (us)
    late: bool


class SimulatedController:
    def __init__(self):
        self._lock = threading.RLock()
        self._received = bytearray()
        self._output = bytearray()
        self._connected = False
        self._expected = 0
        self._history: dict[int, bytes] = {}
        self._last_frame = monotonic()
        self.state = StreamState.STOPPED
        self._origin_clock = 0
        self._origin_time = 0.0
        self.queue: list[tuple[int, Kind, int, int, int, int]] = []  # (time, kind, voice, frequency, on, arrived)
        self.voices: dict[int, tuple[int, int]] = {}  # voice: (frequency, on-time)
        self.applied: list[Applied] = []
        self.requests: list[Type] = []
        self.late_events = 0
        self.watchdog_trips = 0
        self.limits = (100_000, 10_000, 1000)

    # --- the serial port ---

    def write(self, data: bytes) -> int:
        with self._lock:
            for byte in data:
                if byte != 0:
                    self._received.append(byte)
                    continue
                self._handle(bytes(self._received))
                self._received.clear()
        return len(data)

    def read(self, size: int) -> bytes:
        with self._lock:
            self._advance()
            data = bytes(self._output[:size])
            del self._output[:size]
        if not data:
            threading.Event().wait(0.002)
        return data

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._output)

    def reset_input_buffer(self):
        pass

    def close(self):
        pass

    # --- the stream ---

    def clock(self) -> int:
        if self.state != StreamState.RUNNING:
            return self._origin_clock
        elapsed = round((monotonic() - self._origin_time) * 1e6)
        return self._origin_clock + max(elapsed, 0)

    def _advance(self):
        """Applies the queued events that are due, and stops everything when the host went quiet."""
        if self._connected and monotonic() - self._last_frame > WATCHDOG_TIME and (self.voices or self.queue):
            self.watchdog_trips += 1
            self._stop()
        if self.state != StreamState.RUNNING:
            return
        clock = self.clock()
        while self.queue and self.queue[0][0] <= clock + EVENT_LEAD:
            time, kind, voice, frequency, on_time, arrived = self.queue.pop(0)
            self._apply(time, kind, voice, frequency, on_time, arrived, late=time < arrived)

    def _apply(self, time: int, kind: Kind, voice: int, frequency: int, on_time: int, arrived: int, late: bool):
        if late:
            self.late_events += 1
        self.applied.append(Applied(time, kind, voice, frequency, on_time, arrived, late))
        if kind in [Kind.VOICE_ON, Kind.VOICE_SET]:
            self.voices[voice] = (frequency, on_time)
        elif kind == Kind.VOICE_OFF:
            self.voices.pop(voice, None)
        else:
            self.voices.clear()

    def _stop(self):
        self._origin_clock = self.clock()
        self.state = StreamState.STOPPED
        self.queue.clear()
        self.voices.clear()

    # --- the requests ---

    def _handle(self, encoded: bytes):
        decoded = cobs_decode(encoded)
        if decoded is None or len(decoded) < 4 or crc16(decoded[:-2]) != struct.unpack("<H", decoded[-2:])[0]:
            return
        self._advance()
        self._last_frame = monotonic()
        type, seq, body = decoded[0], decoded[1], decoded[2:-2]
        if type == Type.HELLO:
            self._connected = True
            self._expected = seq
            self._history.clear()
        elif not self._connected:
            self._reply(type, seq, Status.WRONG_STATE, b"")
            return
        if seq != self._expected:
            if seq in self._history and 0 < (self._expected - seq) % 256 <= 8:
                self._output += self._history[seq]
            else:
                self._reply(type, seq, Status.OUT_OF_SEQUENCE, bytes([self._expected]))
            return
        self.requests.append(Type(type))
        status, reply = self._request(type, body)
        self._history[seq] = self._reply(type, seq, status, reply)
        self._expected = (seq + 1) % 256

    def _reply(self, type: int, seq: int, status: Status, body: bytes) -> bytes:
        free = QUEUE_SIZE - len(self.queue)
        encoded = frame(type, seq, struct.pack("<BIH", status, self.clock(), free) + body)
        self._output += encoded
        return encoded

    def _request(self, type: int, body: bytes) -> tuple[Status, bytes]:
        if type == Type.HELLO:
            self._stop()
            info = struct.pack(
                "<BBHIIHI", 2, NUM_VOICES, QUEUE_SIZE, CLOCK_HZ, ABSOLUTE_MAX_ON_TIME, ABSOLUTE_MAX_DUTY, MIN_OFF_TIME
            )
            return (Status.OK if body == bytes([2]) else Status.OUT_OF_RANGE), info
        if type == Type.PING:
            active = sum(1 << voice for voice in self.voices)
            late, self.late_events = self.late_events, 0
            return Status.OK, struct.pack("<BIBHH", self.state, active, 0, late, 0)
        if type == Type.SET_LIMITS:
            max_on, min_on, duty = struct.unpack("<IIH", body)
            if max_on > ABSOLUTE_MAX_ON_TIME or min_on > max_on or duty > ABSOLUTE_MAX_DUTY:
                return Status.OUT_OF_RANGE, b""
            self.limits = (max_on, min_on, duty)
            return Status.OK, b""
        if type in [Type.EVENTS, Type.IMMEDIATE]:
            return self._events(type, body), b""
        if type == Type.STREAM_START:
            self._stop()
            (self._origin_clock,) = struct.unpack("<I", body)
            self.state = StreamState.READY
            return Status.OK, b""
        if type == Type.STREAM_RUN:
            if self.state != StreamState.READY:
                return Status.WRONG_STATE, b""
            self._origin_time = monotonic() + RUN_DELAY / 1e6
            self.state = StreamState.RUNNING
            return Status.OK, b""
        if type == Type.FLUSH:
            self.queue.clear()
            return Status.OK, b""
        if type == Type.STOP:
            self._stop()
            return Status.OK, b""
        return Status.UNKNOWN_TYPE, b""

    def _events(self, type: int, body: bytes) -> Status:
        if not body or len(body) != 1 + body[0] * 14:
            return Status.BAD_LENGTH
        events = [struct.unpack_from("<IBBII", body, 1 + i * 14) for i in range(body[0])]
        for _, kind, voice, frequency, on_time in events:
            if kind not in list(Kind) or (kind != Kind.ALL_OFF and voice >= NUM_VOICES):
                return Status.OUT_OF_RANGE
            if kind in [Kind.VOICE_ON, Kind.VOICE_SET] and (
                not 1000 <= frequency <= 20_000_000 or on_time > ABSOLUTE_MAX_ON_TIME
            ):
                return Status.OUT_OF_RANGE
        clock = self.clock()
        if type == Type.IMMEDIATE:
            for _, kind, voice, frequency, on_time in events:
                self._apply(clock, Kind(kind), voice, frequency, on_time, clock, late=False)
            return Status.OK
        if self.state == StreamState.STOPPED:
            return Status.WRONG_STATE
        if len(self.queue) + len(events) > QUEUE_SIZE:
            return Status.QUEUE_FULL
        arrived = clock if self.state == StreamState.RUNNING else -1
        for time, kind, voice, frequency, on_time in events:
            self.queue.append((time, Kind(kind), voice, frequency, on_time, arrived))
        return Status.OK
