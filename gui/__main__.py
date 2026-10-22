#!/usr/bin/env python
#
# Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
#
# This file is part of the Tesla Coil Music Player.

import mido
from pathlib import Path
from time import sleep

from gui.keyboard_handler import KeyboardHandler
from gui.serial_handler import SerialHandler


class TempMain:
    def __init__(self):
        # initialize the song list that can be played
        self.song_list: list[Path] = []
        song_path = Path(__file__).parent.parent / "songs"
        print(song_path)
        for file_path in song_path.rglob("*.mid"):
            print(file_path)
            if file_path.is_file():
                self.song_list.append(file_path)

        # setup the keyboard handler that monitors keyboard presses async
        self.kb_handler = KeyboardHandler()

        # setup the serial connection with the tesla coil interrupter
        self.sh: SerialHandler = SerialHandler("/dev/ttyACM0")
        print(self.sh.open())
        sleep(0.05)

        # force a reset of the tesla coil interrupter
        for _ in range(2):
            self.sh.write_msg([0xFF, 0x00])
            sleep(0.01)
            print(self.sh.read_msg())
        sleep(0.01)

    def start(self):
        while True:
            choice = self.show_menu().lower()
            # handle non-song options first
            if choice == "q":
                return
            if choice == "r":
                print("resetting tesla coil controller...")
                self.sh.write_msg([0xFF, 0x00])
                sleep(0.01)
                print(self.sh.read_msg())
                continue

            # otherwise we assume that a song is selected
            song_name = ""
            try:
                idx = int(choice)
                if idx not in range(len(self.song_list)):
                    print("invalid song number specified!")
                song_name = self.song_list[idx]
            except:
                print("invalid menu option specified!")
                continue
            self.play_song(song_name)

    def show_menu(self) -> str:
        # print menu
        print("+" + "-" * 79)
        print("| MAIN MENU")
        print("+" + "=" * 79)
        for idx, song in enumerate(self.song_list):
            print(f"| {idx:>2}: {song.name}")
        print("+" + "-" * 79)
        print(f"| {'r':>2}: reset the tesla coil interrupter")
        print(f"| {'q':>2}: quit the application")
        print("+" + "-" * 79)

        print("\nmenu choice:")
        return input(">>> ")

    def play_song(self, song_name: Path):
        # load the midi file
        file = mido.MidiFile(song_name)

        # enter midi mode
        self.sh.write_msg([0x01, 0x00])
        sleep(0.05)
        print(self.sh.read_msg())

        # start the keyboard handler
        self.kb_handler.start()

        # play the whole midi file
        notes_on, notes_off, terminated = self._play_midi_file_messages(file)

        # stop the keyboard handler
        self.kb_handler.stop()

        # exit midi mode
        self.sh.write_msg([0x03, 0x00])

        # print some statistics
        song_end = "stopped" if terminated else "finished"
        song_idx = self.song_list.index(song_name)
        print(f'{song_end} playing song "{song_idx}: {song_name.name}"')
        print(f"notes_on: {notes_on}, notes_off: {notes_off}")
        print(f"file length: {file.length} seconds")

    def _play_midi_file_messages(self, file):
        notes_on = 0
        notes_off = 0
        terminated = False
        for msg in file:
            if self.kb_handler.get_input_available():
                if self.kb_handler.get_input().lower() == "q":
                    print("playback terminated!")
                    terminated = True
                    break
            print(msg)
            if msg.type == "note_on":
                notes_on += 1
            if msg.type == "note_off":
                notes_off += 1
            if msg.type in ["note_on", "note_off"]:
                time = int(msg.time * 1000)
                midi_msg = [*msg.bytes(), int(time / 256), int(time % 256)]
                if msg.time:
                    sleep(msg.time)
                new_midi_msg = [
                    0x02,
                    len(midi_msg),
                    *midi_msg,
                ]
                print("msg > ", new_midi_msg)
                self.sh.write_msg(new_midi_msg)
                print(self.sh.read_msg())
        return notes_on, notes_off, terminated


def main():
    temp_main = TempMain()
    temp_main.start()


if __name__ == "__main__":
    main()
