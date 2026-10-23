#!/usr/bin/env python
#
# Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
#
# This file is part of the Tesla Coil Music Player.

import argparse
import contextlib
import io
import logging
import sys
import tempfile
import warnings
from pathlib import Path

import mido

TICKS_PER_BEAT = 480
TEMPO_BPM = 120
# the tesla coil controller has 9 timers, so it can't play more notes at once
COIL_MAX_NOTES = 9
# MIDI channel 10 (index 9) is reserved for percussion, keep it free so the pitched parts don't play as drums
DRUM_CHANNEL = 9
DRUM_STEM = "drums"
# list the parts that most likely carry the melody first
STEM_ORDER = ["vocals", "guitar", "piano", "other", "bass", "drums"]
PROGRESS_WIDTH = 40

# a transcribed note: start and end in seconds, MIDI note number, amplitude between 0 and 1
type Note = tuple[float, float, int, float]


def separate(path: Path, model_name: str, out_dir: Path) -> dict[str, Path]:
    # import here so --help and --no-separate work without loading PyTorch
    import soundfile
    from demucs.api import Separator

    # the progress bar of demucs shows float rounding errors, draw our own from its progress callbacks
    def show_progress(info: dict):
        if info["state"] != "end":
            return
        length = info["audio_length"]
        done = min(info["segment_offset"], length)
        fraction = (info["model_idx_in_bag"] + done / length) / info["models"]
        bar = "█" * round(fraction * PROGRESS_WIDTH)
        model = f", model {info['model_idx_in_bag'] + 1}/{info['models']}" if info["models"] > 1 else ""
        seconds = f"{done / separator.samplerate:.0f}/{length / separator.samplerate:.0f} s"
        print(f"\r  {bar:<{PROGRESS_WIDTH}} {fraction:4.0%} ({seconds}{model})", end="", flush=True)

    separator = Separator(model_name, callback=show_progress if sys.stdout.isatty() else None)
    _, stems = separator.separate_audio_file(path)
    if sys.stdout.isatty():
        print(f"\r  {'█' * PROGRESS_WIDTH} 100%" + " " * 30)
    stem_paths: dict[str, Path] = {}
    for name, wav in stems.items():
        stem_paths[name] = out_dir / f"{name}.wav"
        soundfile.write(stem_paths[name], wav.numpy().T, separator.samplerate)
    return stem_paths


def transcribe(path: Path, args: argparse.Namespace) -> list[Note]:
    # import here so --help works without loading the model and its dependencies
    from basic_pitch import FilenameSuffix, build_icassp_2022_model_path
    from basic_pitch.inference import Model, predict

    model = Model(build_icassp_2022_model_path(FilenameSuffix.onnx))
    # basic-pitch prints the (temporary) path of every file it transcribes
    with contextlib.redirect_stdout(io.StringIO()):
        _, _, note_events = predict(
            path,
            model,
            onset_threshold=args.onset_threshold,
            frame_threshold=args.frame_threshold,
            minimum_note_length=args.min_note_ms,
            minimum_frequency=args.min_freq,
            maximum_frequency=args.max_freq,
        )
    return [(float(start), float(end), int(note), float(amplitude)) for start, end, note, amplitude, _ in note_events]


def limit_polyphony(notes: list[Note], max_notes: int) -> list[Note]:
    # keep the loudest notes first, dropping every note that would exceed the limit somewhere during its span
    kept: list[Note] = []
    for note in sorted(notes, key=lambda n: n[3], reverse=True):
        start, end = note[0], note[1]
        overlapping = [k for k in kept if k[0] < end and start < k[1]]
        # the maximum overlap within the span occurs at the start of one of the overlapping notes (or this one)
        points = [start] + [k[0] for k in overlapping if k[0] > start]
        if all(sum(k[0] <= p < k[1] for k in overlapping) < max_notes for p in points):
            kept.append(note)
    return kept


def build_track(notes: list[Note], name: str, channel: int, tempo: int) -> mido.MidiTrack:
    def ticks(seconds: float) -> int:
        return round(mido.second2tick(seconds, TICKS_PER_BEAT, tempo))

    # (tick, is_note_on, note, velocity), note offs sort before note ons at the same tick
    events: list[tuple[int, bool, int, int]] = []
    for start, end, note, amplitude in notes:
        velocity = max(1, min(127, round(amplitude * 127)))
        events.append((ticks(start), True, note, velocity))
        events.append((max(ticks(end), ticks(start) + 1), False, note, 0))
    events.sort(key=lambda e: (e[0], e[1]))

    track = mido.MidiTrack()
    track.append(mido.MetaMessage("track_name", name=name, time=0))
    track.append(mido.MetaMessage("set_tempo", tempo=tempo, time=0))
    now = 0
    for tick, is_note_on, note, velocity in events:
        message_type = "note_on" if is_note_on else "note_off"
        track.append(mido.Message(message_type, channel=channel, note=note, velocity=velocity, time=tick - now))
        now = tick
    track.append(mido.MetaMessage("end_of_track", time=0))
    return track


def write_midi(parts: dict[str, list[Note]], path: Path):
    # every part gets its own track and channel, so they can be toggled separately in the web interface
    tempo = mido.bpm2tempo(TEMPO_BPM)
    channels = [channel for channel in range(16) if channel != DRUM_CHANNEL]
    file = mido.MidiFile(ticks_per_beat=TICKS_PER_BEAT)
    for channel, (name, notes) in zip(channels, parts.items()):
        file.tracks.append(build_track(notes, name, channel, tempo))
    file.save(path)


def convert(path: Path, output: Path, args: argparse.Namespace):
    with tempfile.TemporaryDirectory() as tmp:
        if args.no_separate:
            stems = {path.stem: path}
        else:
            print(f"separating the instruments of {path} with {args.model}...")
            stems = separate(path, args.model, Path(tmp))
            if not args.drums:
                stems.pop(DRUM_STEM, None)

        parts: dict[str, list[Note]] = {}
        order = {name: idx for idx, name in enumerate(STEM_ORDER)}
        for name, stem_path in sorted(stems.items(), key=lambda stem: order.get(stem[0], len(order))):
            notes = transcribe(stem_path, args)
            total = len(notes)
            if args.max_notes > 0:
                notes = limit_polyphony(notes, args.max_notes)
            dropped = f" ({total - len(notes)} dropped by --max-notes)" if len(notes) < total else ""
            print(f"  {name}: {len(notes)} notes{dropped}")
            # leave out the instruments that aren't in the song
            if notes:
                parts[name.capitalize()] = notes

    write_midi(parts, output)
    print(f"wrote {len(parts)} part{'' if len(parts) == 1 else 's'} to {output}")


def main():
    parser = argparse.ArgumentParser(
        description="Converts audio files (mp3, wav, flac, ogg, ...) to MIDI songs. The instruments are separated "
        "with demucs into their own parts, and transcribed to notes with Spotify's basic-pitch model"
    )
    parser.add_argument("inputs", type=Path, nargs="+", help="audio files to convert")
    parser.add_argument(
        "-o", "--output", type=Path, help="output file, or folder for multiple inputs (default: next to the input)"
    )
    parser.add_argument(
        "--max-notes",
        type=int,
        default=0,
        help=f"maximum number of notes playing at once per part, keeping the loudest ones "
        f"(default: unlimited, the coil plays at most {COIL_MAX_NOTES} of all selected parts together)",
    )
    parser.add_argument(
        "--model",
        default="htdemucs_6s",
        help="demucs model: htdemucs_6s splits vocals, bass, guitar, piano, other and drums, htdemucs or htdemucs_ft "
        "only vocals, bass, other and drums but more cleanly (default: htdemucs_6s)",
    )
    parser.add_argument("--drums", action="store_true", help="also transcribe the drums, as pitched notes")
    parser.add_argument(
        "--no-separate", action="store_true", help="don't separate the instruments, transcribe all into one part"
    )
    parser.add_argument(
        "--onset-threshold", type=float, default=0.5, help="higher means fewer new notes are detected (default: 0.5)"
    )
    parser.add_argument(
        "--frame-threshold", type=float, default=0.3, help="higher means notes are cut off sooner (default: 0.3)"
    )
    parser.add_argument(
        "--min-note-ms", type=float, default=127.7, help="drop notes shorter than this (default: 127.7 ms)"
    )
    parser.add_argument("--min-freq", type=float, help="drop notes below this frequency in Hz")
    parser.add_argument("--max-freq", type=float, help="drop notes above this frequency in Hz")
    args = parser.parse_args()

    multiple = len(args.inputs) > 1
    if args.output and multiple and not args.output.is_dir():
        parser.error("--output must be an existing folder when converting multiple files")
    for path in args.inputs:
        if not path.is_file():
            parser.error(f"{path} is not a file")

    # basic-pitch warns about every model backend that isn't installed, only the ONNX one is needed
    warnings.filterwarnings("ignore")
    logging.getLogger("root").setLevel(logging.ERROR)

    for path in args.inputs:
        if args.output and not multiple and not args.output.is_dir():
            output = args.output
        else:
            output = (args.output or path.parent) / path.with_suffix(".mid").name
        convert(path, output, args)


if __name__ == "__main__":
    main()
