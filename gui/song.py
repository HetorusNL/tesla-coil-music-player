#!/usr/bin/env python
#
# Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
#
# This file is part of the Tesla Coil Music Player.

from dataclasses import dataclass, field
from pathlib import Path

import mido

# a part is a single (track index, channel) combination within a song
type PartKey = tuple[int, int]

# MIDI channel 10 (index 9) is reserved for percussion by the General MIDI standard
DRUM_CHANNEL = 9
DEFAULT_TEMPO = 500000


@dataclass(frozen=True)
class NoteEvent:
    time: float  # seconds since the start of the song
    part: PartKey
    is_note_on: bool
    channel: int
    note: int
    velocity: int

    @property
    def midi_bytes(self) -> list[int]:
        return [(0x90 if self.is_note_on else 0x80) | self.channel, self.note, self.velocity]


@dataclass(frozen=True)
class NoteSpan:
    start: float
    end: float
    note: int
    velocity: int
    part: PartKey


@dataclass
class TrackInfo:
    index: int
    name: str
    channels: list[int] = field(default_factory=lambda: [])


class Song:
    def __init__(self, path: Path):
        self.path = path
        file = mido.MidiFile(path)
        self.length: float = file.length
        self.events = self._load_note_events(file)

        # count the notes of every part and group the parts per track
        self.parts: dict[PartKey, int] = {}
        for event in self.events:
            if event.is_note_on:
                self.parts[event.part] = self.parts.get(event.part, 0) + 1
        self.tracks: list[TrackInfo] = []
        for idx, track in enumerate(file.tracks):
            channels = sorted(channel for track_idx, channel in self.parts if track_idx == idx)
            if channels:
                self.tracks.append(TrackInfo(idx, track.name.strip() or f"Track {idx + 1}", channels))
        # the parts in the order they're listed, which also decides their color
        self.part_order: list[PartKey] = [(track.index, channel) for track in self.tracks for channel in track.channels]
        self.spans = self._pair_note_spans(self.events)

    @property
    def name(self) -> str:
        return self.path.stem

    def preview_notes(self, parts: set[PartKey]) -> list[tuple[float, float, int, int]]:
        """Returns (start, duration, note, velocity) tuples of the notes in the given parts."""
        return [
            (round(span.start, 4), round(span.end - span.start, 4), span.note, span.velocity)
            for span in self.spans
            if span.part in parts
        ]

    def load(self, parts: set[PartKey], bins: int) -> list[int]:
        """Returns the maximum number of simultaneous notes of the given parts, for equal slices of the song."""
        changes: list[tuple[float, int]] = []
        for span in self.spans:
            if span.part in parts:
                changes += [(span.start, 1), (span.end, -1)]
        # handle the note ends before the note starts at the same time
        changes.sort(key=lambda change: (change[0], change[1]))
        load = [0] * bins
        bin_length = self.length / bins if self.length else 1.0
        active = 0
        for idx, (time, change) in enumerate(changes):
            active += change
            if active <= 0:
                continue
            # the number of active notes holds until the next change
            end = changes[idx + 1][0] if idx + 1 < len(changes) else time
            for bin_idx in range(int(time / bin_length), min(int(end / bin_length), bins - 1) + 1):
                load[bin_idx] = max(load[bin_idx], active)
        return load

    @staticmethod
    def _pair_note_spans(events: list[NoteEvent]) -> list[NoteSpan]:
        spans: list[NoteSpan] = []
        started: dict[tuple[PartKey, int], list[tuple[float, int]]] = {}
        for event in events:
            key = (event.part, event.note)
            if event.is_note_on:
                started.setdefault(key, []).append((event.time, event.velocity))
            elif started.get(key):
                start, velocity = started[key].pop(0)
                spans.append(NoteSpan(start, event.time, event.note, velocity, event.part))
        # the notes are collected when they end, but they're played in the order they start
        return sorted(spans, key=lambda span: span.start)

    @staticmethod
    def _load_note_events(file: mido.MidiFile) -> list[NoteEvent]:
        # merge the tracks ourselves (like mido does when iterating a MidiFile), but keep the track index
        tick_events: list[tuple[int, int, mido.Message]] = []
        for idx, track in enumerate(file.tracks):
            tick = 0
            for msg in track:
                tick += msg.time
                if msg.dict()["type"] in ["note_on", "note_off", "set_tempo"]:
                    tick_events.append((tick, idx, msg))
        tick_events.sort(key=lambda event: event[0])

        # convert the ticks to seconds, following the tempo changes
        events: list[NoteEvent] = []
        tempo = DEFAULT_TEMPO
        last_tick = 0
        time = 0.0
        for tick, idx, msg in tick_events:
            time += mido.tick2second(tick - last_tick, file.ticks_per_beat, tempo)
            last_tick = tick
            fields = msg.dict()
            if fields["type"] == "set_tempo":
                tempo = int(fields["tempo"])
                continue
            channel, note, velocity = int(fields["channel"]), int(fields["note"]), int(fields["velocity"])
            # a note_on with velocity zero is a note_off by convention
            is_note_on = fields["type"] == "note_on" and velocity > 0
            events.append(NoteEvent(time, (idx, channel), is_note_on, channel, note, velocity))
        return events
