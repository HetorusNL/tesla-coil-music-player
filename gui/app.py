#!/usr/bin/env python
#
# Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
#
# This file is part of the Tesla Coil Music Player.

import asyncio
import hashlib
import json
import secrets
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import cast

from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import Client, app, events, run, ui
from serial.tools import list_ports
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from gui.coil_model import COIL_TIMERS, MeterReading, note_frequency, spark_on_time
from gui.coil_player import CoilPlayer, Delivery, LogLine, PlayerState
from gui.config import (
    ABSOLUTE_MAX_ON_TIME,
    DEFAULT_ON_TIMES,
    MAX_TABLE_ON_TIME,
    MAX_VOLUME,
    NUM_ON_TIMES,
    SparkConfig,
)
from gui.song import DRUM_CHANNEL, PartKey, Song

STATIC_DIR = Path(__file__).parent / "static"

# laptop keyboard layout like LMMS: the bottom two rows play the lower octave, the top two rows the upper octave
LOWER_ROW_KEYS = "KeyZ KeyS KeyX KeyD KeyC KeyV KeyG KeyB KeyH KeyN KeyJ KeyM Comma KeyL Period Semicolon Slash".split()
UPPER_ROW_KEYS = (
    "KeyQ Digit2 KeyW Digit3 KeyE KeyR Digit5 KeyT Digit6 KeyY Digit7 KeyU KeyI Digit9 KeyO Digit0 KeyP".split()
)
KEY_OFFSETS = {code: idx for idx, code in enumerate(LOWER_ROW_KEYS)} | {
    code: 12 + idx for idx, code in enumerate(UPPER_ROW_KEYS)
}
NUM_PIANO_KEYS = 29  # two octaves plus up to the E, matching the laptop keyboard layout
BLACK_KEYS = {1, 3, 6, 8, 10}
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
LIVE_VELOCITY = 100
SPEAKER_START_DELAY = 0.1  # matches START_DELAY of the synth
# colors of the parts in the piano roll, readable in both the light and dark theme
PART_COLORS = ["#4f8ef7", "#f2994a", "#27ae60", "#bb6bd9", "#eb5757", "#2bb3c0", "#f2c94c", "#e86fae", "#9b8f6b"]
LOAD_BINS = 400  # the number of time slices of the coil load strip
LOG_LINES = 1000  # the number of log lines shown
POWER_FULL_SCALE = 0.05  # the duty cycle of a full power meter
PEAK_HOLD = 1.5  # seconds the peak of the power meter holds before it falls back
SETTINGS_DELAY = 0.5  # seconds without changes before a changed setting is applied
SOFT_VELOCITY = 64  # the velocity of the dashed curve of the spark chart
# the pages of the sidebar: name, label and icon
PAGES = [
    ("player", "Player", "music_note"),
    ("keyboard", "Keyboard", "piano"),
    ("settings", "Settings", "tune"),
    ("log", "Log", "list"),
]

CSS = """
.piano { display: flex; align-items: flex-start; user-select: none; touch-action: none; }
.piano-key { border: 1px solid #333; border-radius: 0 0 4px 4px; cursor: pointer; }
.piano-key.white { width: 36px; height: 140px; background: white; }
.piano-key.black { width: 24px; height: 85px; margin: 0 -12px; background: #222; z-index: 1; }
.piano-key.pressed { background: var(--q-primary); }
.roll { position: relative; width: 100%; cursor: pointer; user-select: none; }
.roll-notes { display: block; width: 100%; height: 140px; background: rgba(128, 128, 128, 0.08); border-radius: 4px 4px 0 0; }
.roll-load { display: block; width: 100%; height: 8px; background: rgba(128, 128, 128, 0.18); border-radius: 0 0 4px 4px; }
.roll-played { position: absolute; left: 0; top: 0; bottom: 0; width: 0; pointer-events: none;
               background: rgba(128, 128, 128, 0.18); border-right: 2px solid var(--q-primary); }
.spark-chart { width: 100%; height: 260px; user-select: none; touch-action: none; }
.part-color { width: 10px; height: 10px; border-radius: 2px; flex-shrink: 0; }
.meter-leds { display: flex; gap: 2px; }
.meter-led { width: 7px; height: 14px; border-radius: 1px; background: rgba(255, 255, 255, 0.2); }
.meter-led.on { background: #6fcf97; }
.meter-led.on.busy { background: #f2c94c; }
.meter-led.on.full { background: #eb5757; }
.meter-power { position: relative; width: 96px; height: 10px; border-radius: 2px; background: rgba(255, 255, 255, 0.2); overflow: hidden; }
.meter-fill { height: 100%; width: 0; background: linear-gradient(90deg, #6fcf97 0%, #f2c94c 60%, #eb5757 100%);
              background-size: 96px 100%; transition: width 0.1s linear; }
.meter-peak { position: absolute; top: 0; bottom: 0; width: 2px; left: 0; background: white; opacity: 0; }
"""


def format_time(seconds: float) -> str:
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def note_name(note: int) -> str:
    return f"{NOTE_NAMES[note % 12]}{note // 12 - 1}"


@dataclass
class SpeakerPlayback:
    """A song playing on the speaker of a single browser tab."""

    song: Song
    parts: tuple[PartKey, ...] | None  # the previewed parts, or None to play the enabled parts
    label: str
    origin: float  # monotonic time of the start of the song
    muted_version: int

    @property
    def position(self) -> float:
        return min(max(monotonic() - self.origin, 0.0), self.song.length)


class PlayerPage:
    """The UI of a single browser tab, all tabs share the same CoilPlayer."""

    def __init__(self, player: CoilPlayer, default_port: str, logout: bool):
        self.player = player
        self.folder = player.song_dir
        self.selected: Song | None = player.current_song or next(iter(player.list_folder(self.folder)[1]), None)
        self.output = "coil"
        # where the play button plays, follows the connection but can be changed to the speaker
        self.play_output = "speaker" if player.state == PlayerState.DISCONNECTED else "coil"
        self.was_connected = player.state != PlayerState.DISCONNECTED
        self.speaker: SpeakerPlayback | None = None
        # what the piano roll in the browser currently shows
        self.client = ui.context.client
        self.roll_song: Path | None = None
        self.roll_enabled: tuple[Path, frozenset[PartKey]] | None = None
        self.roll_position = 0.0
        self.base_note = 48
        # the notes held down on the keyboard, per source (laptop key code or on-screen key)
        self.held: dict[str, int] = {}
        self.piano_keys: list[ui.element] = []
        self.log_number = 0
        self.show_debug = False
        self.song_list_state: tuple[object, ...] = ()
        self.parts_state: tuple[object, ...] = ()

        ui.add_css(CSS)
        # the pages are hidden instead of removed, so the piano roll and the log keep their content
        self.pages: dict[str, ui.column] = {}
        drawer = self.build_sidebar()
        self.build_header(logout, drawer)
        with self.page("player"):
            with ui.row().classes("w-full items-start gap-4"):
                with ui.column().classes("w-full md:w-96 gap-4"):
                    self.build_connection(default_port)
                    self.build_songs()
                with ui.column().classes("flex-1 min-w-0 gap-4"):
                    self.build_now_playing()
                    self.build_parts()
        with self.page("keyboard"):
            self.build_keyboard()
        with self.page("settings"):
            self.build_settings()
        with self.page("log"):
            with ui.card().classes("w-full"):
                ui.label("Log").classes("text-h6")
                ui.switch("Debug messages (packets to and from the controller)", on_change=self.toggle_debug)
                self.log = ui.log(max_lines=LOG_LINES).classes("w-full h-[70vh]")
        self.show_page(self.current_page)

        ui.keyboard(on_key=self.on_key, ignore=["input", "select", "textarea"])
        # release all keys when the window loses focus, as the keyup events are lost then
        ui.on("window_blur", self.release_all_keys)
        ui.run_javascript("window.addEventListener('blur', () => emitEvent('window_blur'))")
        # nicegui types some of its handlers and containers without their element types
        ui.context.client.on_disconnect(self.release_all_keys)  # pyright: ignore[reportUnknownMemberType]
        ui.timer(0.2, self.update)
        ui.timer(0.1, self.update_meter)
        self.update_meter()
        self.update()

    # --- layout ---

    def build_sidebar(self) -> ui.left_drawer:
        # like Home Assistant: always shown on wide screens, behind the menu button of the header on small ones
        # the page this browser showed last, nicegui doesn't type the stored values
        storage = cast(dict[str, object], app.storage.user)  # pyright: ignore[reportUnknownMemberType]
        stored_page = storage.get("page")
        self.current_page = stored_page if isinstance(stored_page, str) else "player"
        with ui.left_drawer(bordered=True).props("width=180 breakpoint=1023") as drawer:
            with ui.tabs(on_change=self.change_page) as tabs:
                tabs.props("vertical inline-label active-color=primary indicator-color=primary align=left").classes(
                    "w-full"
                )
                for name, label, icon in PAGES:
                    ui.tab(name, label, icon).props("no-caps content-class=w-full justify-start")
            tabs.set_value(self.current_page)
        return drawer

    def page(self, name: str) -> ui.column:
        self.pages[name] = ui.column().classes("w-full gap-4")
        return self.pages[name]

    def change_page(self, e: events.ValueChangeEventArguments[str | ui.tab | ui.tab_panel | None]):
        if not isinstance(e.value, str):
            return
        self.current_page = e.value
        app.storage.user["page"] = e.value
        self.show_page(e.value)

    def show_page(self, name: str):
        for page_name, page in self.pages.items():
            page.set_visibility(page_name == name)

    def build_header(self, logout: bool, drawer: ui.left_drawer):
        # three columns: the outer ones share the remaining space equally, so the meter stays centered
        with ui.header().classes("items-center"):
            grid = "grid grid-cols-[1fr_auto_1fr] max-sm:flex max-sm:justify-between"
            with ui.element("div").classes(f"w-full {grid} items-center gap-4"):
                with ui.row().classes("items-center no-wrap min-w-0"):
                    menu = ui.button(icon="menu", on_click=drawer.toggle).props("flat round dense color=white")
                    menu.classes("lg:hidden")
                    ui.icon("bolt", size="md")
                    ui.label("Tesla Coil Music Player").classes("text-lg font-medium truncate max-md:hidden")
                self.build_meter()
                with ui.row().classes("items-center no-wrap justify-end min-w-0"):
                    self.status_badge = ui.badge().classes("text-sm truncate")
                    if logout:
                        ui.button(icon="logout", on_click=self.logout).props("flat round color=white").tooltip(
                            "Log out"
                        )

    def build_meter(self):
        with ui.row().classes("items-center gap-2 no-wrap max-sm:hidden") as meter:
            meter.tooltip(
                f"Notes on the {COIL_TIMERS} coil timers (red: all timers in use, extra notes are dropped) and the "
                f"estimated power as duty cycle of the coil, full scale is {POWER_FULL_SCALE:.0%}"
            )
            self.meter_source = ui.label().classes("text-caption opacity-70 w-12 text-right")
            with ui.element("div").classes("meter-leds"):
                self.meter_leds = [ui.element("div").classes("meter-led") for _ in range(COIL_TIMERS)]
            self.meter_timers_label = ui.label().classes("text-caption w-14")
            with ui.element("div").classes("meter-power"):
                self.meter_fill = ui.element("div").classes("meter-fill")
                self.meter_peak = ui.element("div").classes("meter-peak")
            self.meter_power_label = ui.label().classes("text-caption w-10")
        self.meter_state: tuple[object, ...] = ()
        self.meter_peak_value = 0.0
        self.meter_peak_time = 0.0

    def build_settings(self):
        with ui.card().classes("w-full"):
            with ui.row().classes("w-full items-center"):
                ui.label("Coil settings").classes("text-h6")
                ui.space()
                # whether the last change got saved and sent to the coil
                self.settings_status = ui.label().classes("text-caption")
            ui.label(
                "The on-time of a spark is the on-time of its frequency band in the table, times the volume, "
                "shortened for lower note velocities as far as the velocity sensitivity goes, and limited to the "
                f"min and max on-time (never beyond {ABSOLUTE_MAX_ON_TIME} µs). Changes apply on enter or when "
                "leaving the field, and are sent to the coil right away."
            ).classes("text-caption text-grey")
            with ui.row().classes("items-center gap-4"):
                self.volume_input = self.setting_input("Volume (%)", 0, MAX_VOLUME * 100, 1)
                self.velocity_input = self.setting_input("Velocity sensitivity (%)", 0, 100, 1)
                self.min_on_time_input = self.setting_input("Min on-time (µs)", 0, ABSOLUTE_MAX_ON_TIME, 1)
                self.max_on_time_input = self.setting_input("Max on-time (µs)", 0, ABSOLUTE_MAX_ON_TIME, 1)
                ui.button("Defaults", icon="settings_backup_restore", on_click=self.reset_settings).props("outline")
            self.spark_summary = ui.label().classes("text-caption")
            # drawn by spark_chart.js, dragging a point changes the on-time of its band in the table
            self.spark_chart = ui.element("div").classes("spark-chart")
            self.spark_chart.on("pointmoved", self.point_moved, args=["detail"])
            ui.label(
                "The points are the on-time table, drag them to change it. The line is the resulting spark at full "
                f"velocity (the table times the volume, within the min and max on-time), dashed at velocity "
                f"{SOFT_VELOCITY} when the velocity sensitivity is set."
            ).classes("text-caption text-grey")
            with ui.row().classes("w-full items-center"):
                ui.label("On-time table (µs per frequency band)").classes("text-subtitle2")
                ui.button("Defaults", icon="settings_backup_restore", on_click=self.reset_table).props("outline dense")
            with ui.element("div").classes("grid grid-cols-2 sm:grid-cols-5 gap-2 w-full"):
                self.on_time_inputs = [
                    self.setting_input(
                        f"{idx * 100}–{idx * 100 + 99} Hz" if idx < NUM_ON_TIMES - 1 else f"≥ {idx * 100} Hz",
                        0,
                        MAX_TABLE_ON_TIME,
                        1,
                    )
                    for idx in range(NUM_ON_TIMES)
                ]
        self.config_version = -1
        self.chart_version = -1
        self.settings_changes = 0

    def setting_input(self, label: str, low: float, high: float, step: float) -> ui.number:
        number = ui.number(label, min=low, max=high, step=step, on_change=self.setting_changed)
        number.props("dense").classes("w-40")
        number.on("blur", self.apply_settings)
        number.on("keydown.enter", self.apply_settings)
        return number

    def fill_settings(self, config: SparkConfig):
        self.volume_input.set_value(round(config.volume * 100, 1))
        self.velocity_input.set_value(round(config.velocity_sensitivity * 100))
        self.min_on_time_input.set_value(config.min_on_time)
        self.max_on_time_input.set_value(config.max_on_time)
        for number, on_time in zip(self.on_time_inputs, config.on_times):
            number.set_value(on_time)
        # the range of the sparks over all MIDI notes at full velocity, the table does nothing while it's one value
        on_times = [spark_on_time(note_frequency(note), 127, config) for note in range(128)]
        summary = f"Spark on-times of all notes at full velocity: {min(on_times)}–{max(on_times)} µs"
        if max(on_times) == config.min_on_time:
            summary += ", all at the min on-time: raise the volume to make the table count"
        self.spark_summary.set_text(summary)

    async def setting_changed(self):
        # filling in the settings in use changes the fields as well, that's not a change to apply
        if self.settings_from_inputs() == self.player.spark_config:
            return
        # the arrows of the fields only change the value, apply it once the changes stop for a moment
        self.settings_changes += 1
        changes = self.settings_changes
        self.set_settings_status("Applying…", "text-grey")
        await asyncio.sleep(SETTINGS_DELAY)
        if changes == self.settings_changes:
            await self.apply_settings()

    async def apply_settings(self):
        config = self.settings_from_inputs()
        if config is None:
            self.set_settings_status("Not applied, fill in all the settings", "text-negative")
            return
        if config == self.player.spark_config:
            # nothing changed after all, e.g. the arrows went up and down again
            if self.settings_status.text == "Applying…":
                self.set_settings_status("")
            return
        await self.save_settings(config)

    def settings_from_inputs(self) -> SparkConfig | None:
        """The settings as filled in, None when a field is empty."""
        inputs = [self.volume_input, self.velocity_input, self.min_on_time_input, self.max_on_time_input]
        values = [number.value for number in inputs + self.on_time_inputs]
        numbers = [value for value in values if value is not None]
        if len(numbers) != len(values):
            return None
        volume, velocity_sensitivity, min_on_time, max_on_time, *on_times = numbers
        return SparkConfig(
            on_times=[int(on_time) for on_time in on_times],
            min_on_time=int(min_on_time),
            max_on_time=int(max_on_time),
            volume=round(volume / 100, 4),
            velocity_sensitivity=round(velocity_sensitivity / 100, 4),
        )

    async def reset_settings(self):
        if await self.confirm("Reset all coil settings to their defaults?"):
            await self.save_settings(SparkConfig())

    async def reset_table(self):
        if await self.confirm("Reset the on-time table to its defaults? The other settings stay as they are."):
            await self.save_settings(replace(self.player.spark_config, on_times=list(DEFAULT_ON_TIMES)))

    @staticmethod
    async def confirm(question: str) -> bool:
        with ui.dialog() as dialog, ui.card():
            ui.label(question)
            with ui.row().classes("w-full justify-end"):
                ui.button("Cancel", on_click=lambda: dialog.submit(False)).props("flat")
                ui.button("Reset", on_click=lambda: dialog.submit(True)).props("color=negative")
        answer = await dialog
        dialog.delete()
        return answer is True

    async def save_settings(self, config: SparkConfig):
        try:
            delivery = await run.io_bound(self.player.set_spark_config, config)
        except ValueError as e:
            self.set_settings_status(f"Not applied, {e}", "text-negative")
            # show the settings that are in use again
            self.config_version = -1
        else:
            time = datetime.now().strftime("%H:%M:%S")
            if delivery is None:
                self.set_settings_status(f"✓ Saved · sent to the coil once connected · {time}", "text-positive")
            elif delivery == Delivery.ACKNOWLEDGED:
                self.set_settings_status(f"✓ Saved and confirmed by the coil · {time}", "text-positive")
            elif delivery == Delivery.REJECTED:
                self.set_settings_status(f"Saved, but the coil rejected it (see the log) · {time}", "text-negative")
            else:
                self.set_settings_status(f"Saved, but the coil didn't confirm it · {time}", "text-warning")
        self.update()

    async def point_moved(self, e: events.GenericEventArguments):
        detail = cast(dict[str, float], e.args["detail"])
        config = self.player.spark_config
        on_times = list(config.on_times)
        on_times[int(detail["index"])] = min(max(round(detail["value"]), 0), MAX_TABLE_ON_TIME)
        await self.save_settings(replace(config, on_times=on_times))

    def update_spark_chart(self, config: SparkConfig):
        velocity_factor = 1 - config.velocity_sensitivity * (1 - SOFT_VELOCITY / 127)
        data = {
            "points": config.on_times,
            "volume": config.volume,
            "min": config.min_on_time,
            "max": config.max_on_time,
            "absoluteMax": ABSOLUTE_MAX_ON_TIME,
            "softFactor": velocity_factor if config.velocity_sensitivity > 0 else None,
        }
        self.client.run_javascript(f"teslaSparkChart.set({self.spark_chart.id}, {json.dumps(data)})")

    def set_settings_status(self, text: str, color: str = ""):
        self.settings_status.set_text(text)
        self.settings_status.classes(replace=f"text-caption {color}")

    def build_connection(self, default_port: str):
        with ui.card().classes("w-full"):
            ui.label("Connection").classes("text-h6")
            with ui.row().classes("w-full items-center no-wrap"):
                ports = self.list_ports()
                port = self.player.port_name or (default_port if default_port in ports else next(iter(ports), None))
                if port is not None and port not in ports:
                    ports.append(port)
                self.port_select = ui.select(ports, value=port, label="Serial port", new_value_mode="add-unique")
                self.port_select.classes("flex-1")
                ui.button(icon="refresh", on_click=self.refresh_ports).props("flat round").tooltip("Refresh ports")
            with ui.row():
                self.connect_button = ui.button(on_click=self.toggle_connection)
                self.reset_button = ui.button("Reset coil", icon="restart_alt", on_click=self.reset)
                self.reset_button.props("outline")

    def build_songs(self):
        with ui.card().classes("w-full"):
            with ui.row().classes("w-full items-center no-wrap"):
                ui.label("Songs").classes("text-h6")
                ui.space()
                ui.button(icon="refresh", on_click=self.render_song_list).props("flat round dense").tooltip(
                    "Reload the folder"
                )
            self.folder_label = ui.label().classes("text-caption text-grey break-all")
            self.song_filter = ui.input(placeholder="Filter...", on_change=self.render_song_list)
            self.song_filter.props("dense clearable").classes("w-full")
            self.song_list = ui.list().props("dense separator").classes("w-full max-h-[50vh] overflow-y-auto")
            ui.upload(label="Upload MIDI files", multiple=True, auto_upload=True, on_upload=self.upload).props(
                "accept=.mid,.midi"
            ).classes("w-full")
            ui.label(".mid / .midi files are saved to the current folder").classes("text-caption text-grey")

    def build_now_playing(self):
        with ui.card().classes("w-full"):
            self.song_title = ui.label().classes("text-h6")
            # piano roll of the song with the coil load below it, drawn by roll.js
            with ui.element("div").classes("roll") as self.roll:
                ui.element("canvas").classes("roll-notes")
                ui.element("canvas").classes("roll-load")
                ui.element("div").classes("roll-played")
            self.roll.tooltip(
                "Click to play from here. The bottom strip shows the coil load: green up to 4, yellow up to 7, "
                f"orange up to {COIL_TIMERS} and red above {COIL_TIMERS} notes at once (the coil drops those notes)"
            )
            self.roll.on(
                "click",
                self.seek,
                # measure from the roll itself, offsetX is relative to the inner element that was clicked
                js_handler="(e) => { const r = e.currentTarget.getBoundingClientRect(); "
                "emit((e.clientX - r.left) / r.width) }",
            )
            with ui.row().classes("w-full justify-between text-caption"):
                self.time_label = ui.label()
                self.load_label = ui.label()
                self.notes_label = ui.label()
            with ui.row().classes("items-center"):
                self.play_button = ui.button("Play", icon="play_arrow", on_click=self.play)
                self.stop_button = ui.button("Stop", icon="stop", on_click=self.stop).props("color=negative")
                self.play_output_toggle = ui.toggle(
                    {"coil": "Coil", "speaker": "Speaker"},
                    value=self.play_output,
                    on_change=lambda e: setattr(self, "play_output", e.value),
                )

    def build_parts(self):
        with ui.card().classes("w-full"):
            with ui.row().classes("w-full items-center"):
                ui.label("Parts").classes("text-h6")
                ui.space()
                ui.button("All", on_click=lambda: self.set_all_muted(False)).props("flat dense")
                ui.button("None", on_click=lambda: self.set_all_muted(True)).props("flat dense")
            self.parts = ui.column().classes("w-full gap-0")

    def build_keyboard(self):
        with ui.card().classes("w-full"):
            with ui.row().classes("w-full items-center"):
                ui.label("Keyboard").classes("text-h6")
                ui.space()
                ui.toggle({"coil": "Coil", "speaker": "Speaker", "both": "Both"}, value=self.output).bind_value(
                    self, "output"
                )
                ui.button(icon="remove", on_click=lambda: self.shift_octave(-1)).props("flat round dense")
                self.octave_label = ui.label().classes("w-16 text-center")
                ui.button(icon="add", on_click=lambda: self.shift_octave(1)).props("flat round dense")
            with ui.element("div").classes("w-full overflow-x-auto"):
                with ui.element("div").classes("piano"):
                    for offset in range(NUM_PIANO_KEYS):
                        color = "black" if offset % 12 in BLACK_KEYS else "white"
                        key = ui.element("div").classes(f"piano-key {color}")
                        source = f"pointer:{offset}"
                        key.on("pointerdown", lambda _, s=source, o=offset: self.press(s, o))
                        for event in ["pointerup", "pointerleave", "pointercancel"]:
                            key.on(event, lambda _, s=source: self.release(s))
                        self.piano_keys.append(key)
            self.keyboard_hint = ui.label().classes("text-caption text-grey")
            self.update_octave_label()

    def render_song_list(self):
        query = (self.song_filter.value or "").lower()
        playing = self.player.current_song if self.player.state == PlayerState.PLAYING else None
        if self.speaker is not None:
            playing = self.speaker.song
        folders, songs = self.player.list_folder(self.folder)
        home = Path.home()
        self.folder_label.set_text(
            f"~/{self.folder.relative_to(home)}" if self.folder.is_relative_to(home) else str(self.folder)
        )
        self.song_list.clear()
        with self.song_list:
            if self.folder.parent != self.folder:
                self.folder_item("..", self.folder.parent)
            for folder in folders:
                if query in folder.name.lower():
                    self.folder_item(folder.name, folder)
            for song in songs:
                if query not in song.name.lower():
                    continue
                item = ui.item(on_click=lambda _, s=song: self.select(s)).props("clickable")
                if self.selected is not None and song.path == self.selected.path:
                    item.props("active")
                with item:
                    with ui.item_section().props("avatar"):
                        is_playing = playing is not None and song.path == playing.path
                        ui.icon("graphic_eq" if is_playing else "music_note", color="primary" if is_playing else None)
                    with ui.item_section():
                        ui.item_label(song.name)
                        ui.item_label(f"{format_time(song.length)} · {len(song.parts)} parts").props("caption")

    def folder_item(self, name: str, folder: Path):
        with ui.item(on_click=lambda: self.open_folder(folder)).props("clickable"):
            with ui.item_section().props("avatar"):
                ui.icon("arrow_upward" if name == ".." else "folder", color="amber")
            with ui.item_section():
                ui.item_label(name)

    def open_folder(self, folder: Path):
        self.folder = folder.resolve()
        self.song_filter.set_value("")
        self.render_song_list()

    def render_parts(self):
        song = self.selected
        self.parts.clear()
        with self.parts:
            if song is None:
                ui.label("Select a song to see its parts").classes("text-grey")
                return
            for track in song.tracks:
                parts = [(track.index, channel) for channel in track.channels]
                if len(parts) == 1:
                    self.part_row(song, parts, f"{track.name} · {self.channel_name(parts[0][1])}")
                    continue
                self.part_row(song, parts, track.name)
                with ui.column().classes("w-full gap-0 pl-8"):
                    for part in parts:
                        self.part_row(song, [part], self.channel_name(part[1]), f"{track.name} · ")

    def part_row(self, song: Song, parts: list[PartKey], label: str, preview_prefix: str = ""):
        enabled = all(not self.player.is_muted(song, part) for part in parts)
        notes = sum(song.parts[part] for part in parts)
        with ui.row().classes("w-full items-center no-wrap gap-1"):
            # the color of the part in the piano roll, tracks with multiple parts show them on the channel rows
            color = PART_COLORS[song.part_order.index(parts[0]) % len(PART_COLORS)] if len(parts) == 1 else None
            ui.element("div").classes("part-color").style(f"background: {color or 'transparent'}")
            ui.checkbox(
                label, value=enabled, on_change=lambda e: self.player.set_muted(song, parts, not e.value)
            ).classes("flex-1")
            ui.label(f"{notes} notes").classes("text-caption text-grey")
            previewing = self.is_previewing(song, parts)
            ui.button(
                icon="stop" if previewing else "headphones",
                color="primary" if previewing else None,
                on_click=lambda: self.toggle_preview(song, parts, preview_prefix + label),
            ).props("dense round" + ("" if previewing else " flat")).tooltip(
                "Stop the preview" if previewing else "Preview only this part on this device"
            )

    @staticmethod
    def channel_name(channel: int) -> str:
        return f"ch {channel + 1}" + (" (drums)" if channel == DRUM_CHANNEL else "")

    # --- periodic update of the shared player state ---

    def update(self):
        player = self.player
        state = player.state
        # show the settings when this or another tab changed them
        if self.config_version != player.config_version:
            self.config_version = player.config_version
            self.fill_settings(player.spark_config)
        # the chart is drawn in the browser, so wait for its connection
        if self.chart_version != player.config_version and self.client.has_socket_connection:
            self.chart_version = player.config_version
            self.update_spark_chart(player.spark_config)
        # a song that lost the connection keeps playing once reconnected, show the reconnecting meanwhile
        status = PlayerState.RECONNECTING if player.reconnecting else state
        self.status_badge.set_text(
            {
                PlayerState.DISCONNECTED: "disconnected",
                PlayerState.IDLE: f"connected · {player.port_name}",
                PlayerState.PLAYING: "playing",
                PlayerState.RECONNECTING: f"reconnecting · {player.port_name}",
            }[status]
        )
        self.status_badge.props(
            "color="
            + {
                PlayerState.DISCONNECTED: "grey",
                PlayerState.IDLE: "positive",
                PlayerState.PLAYING: "orange",
                PlayerState.RECONNECTING: "negative",
            }[status]
        )
        connected = state != PlayerState.DISCONNECTED
        if connected != self.was_connected:
            self.was_connected = connected
            self.play_output_toggle.set_value("coil" if connected else "speaker")
        self.connect_button.set_text("Disconnect" if connected else "Connect")
        self.connect_button.props(f"icon={'link_off' if connected else 'link'}")
        self.port_select.set_enabled(not connected)
        self.reset_button.set_enabled(connected and not player.reconnecting)

        # the speaker playback of this tab ends with the song, and follows the muted parts
        speaker = self.speaker
        if speaker is not None and speaker.position >= speaker.song.length:
            self.stop_speaker()
            speaker = None
        if speaker is not None and speaker.parts is None and speaker.muted_version != player.muted_version:
            self.play_on_speaker(speaker.song, None, speaker.label, speaker.position)
            speaker = self.speaker

        # show the speaker playback of this tab, otherwise the coil playback, otherwise the selected song
        song = player.current_song if state == PlayerState.PLAYING else self.selected
        if speaker is not None:
            song = speaker.song
            if speaker.parts is None:
                self.song_title.set_text(f"Playing on speaker: {song.name}")
            else:
                self.song_title.set_text(f"Previewing on speaker: {song.name} · {speaker.label}")
            self.show_progress(song, speaker.position)
            self.notes_label.set_text("")
            enabled = set(speaker.parts) if speaker.parts is not None else self.enabled_parts(song)
        elif state == PlayerState.PLAYING and song is not None:
            self.song_title.set_text(f"Playing on coil: {song.name}")
            self.show_progress(song, player.position)
            self.notes_label.set_text(f"notes on: {player.notes_on} · notes off: {player.notes_off}")
            enabled = self.enabled_parts(song)
        else:
            self.song_title.set_text(song.name if song else "No song selected")
            self.set_roll_position(0.0)
            self.time_label.set_text(f"0:00 / {format_time(song.length)}" if song else "")
            self.notes_label.set_text("")
            enabled = self.enabled_parts(song) if song else set[PartKey]()
        self.update_roll(song, enabled)
        can_play = self.play_output == "speaker" or connected
        self.play_button.set_enabled(can_play and self.selected is not None)
        self.stop_button.set_enabled(speaker is not None or state == PlayerState.PLAYING)

        if self.output != "speaker" and state != PlayerState.IDLE:
            reason = "while a song is playing" if state == PlayerState.PLAYING else "until connected"
            self.keyboard_hint.set_text(f"The coil doesn't play keyboard notes {reason}")
        else:
            self.keyboard_hint.set_text("Laptop keys: Z/S rows play the lower octave, Q/number rows the upper octave")

        # only rebuild the lists when something changed, so they don't flicker
        playing_path = player.current_song.path if player.current_song and state == PlayerState.PLAYING else None
        if speaker is not None:
            playing_path = speaker.song.path
        selected_path = self.selected.path if self.selected else None
        song_list_state = (player.songs_version, self.folder, selected_path, playing_path)
        if song_list_state != self.song_list_state:
            self.song_list_state = song_list_state
            self.render_song_list()
        preview = (speaker.song.path, speaker.parts) if speaker is not None else None
        parts_state = (player.muted_version, selected_path, preview)
        if parts_state != self.parts_state:
            self.parts_state = parts_state
            self.render_parts()

        self.push_log_lines(player.log_since(self.log_number))

    def update_meter(self):
        # this tab's speaker playback shows what the coil would do, otherwise show what the coil does
        speaker = self.speaker
        if speaker is not None:
            enabled = set(speaker.parts) if speaker.parts is not None else self.enabled_parts(speaker.song)
            reading = self.simulate_coil(speaker.song, enabled, speaker.position)
            source = "speaker"
        else:
            reading = self.player.coil_model.reading()
            source = "coil"
        duty = reading.duty_cycle(self.player.spark_config)
        now = monotonic()
        if duty >= self.meter_peak_value or now - self.meter_peak_time > PEAK_HOLD:
            self.meter_peak_value = duty
            self.meter_peak_time = now

        state = (source, len(reading.notes), reading.dropped, round(duty, 4), round(self.meter_peak_value, 4))
        if state == self.meter_state:
            return
        self.meter_state = state
        self.meter_source.set_text(source)
        # the lit leds turn yellow when the timers get busy and red when they're all in use
        level = "full" if len(reading.notes) >= COIL_TIMERS else "busy" if len(reading.notes) >= COIL_TIMERS - 2 else ""
        for idx, led in enumerate(self.meter_leds):
            led.classes(replace=f"meter-led {'on' if idx < len(reading.notes) else ''} {level}")
        dropped = f" +{reading.dropped}" if reading.dropped else ""
        self.meter_timers_label.set_text(f"{len(reading.notes)}/{COIL_TIMERS}{dropped}")
        fill = min(duty / POWER_FULL_SCALE, 1.0)
        peak = min(self.meter_peak_value / POWER_FULL_SCALE, 1.0)
        self.meter_fill.style(f"width: {fill * 100:.1f}%")
        self.meter_peak.style(f"left: calc({peak * 100:.1f}% - 2px); opacity: {1 if peak else 0}")
        self.meter_power_label.set_text(f"{duty:.1%}")

    @staticmethod
    def simulate_coil(song: Song, enabled: set[PartKey], position: float) -> MeterReading:
        # the notes sounding at the position, the ones that started last don't get a timer
        notes: list[tuple[int, int]] = []
        keys: set[tuple[int, int]] = set()
        for span in song.spans:
            if span.start > position:
                break
            key = (span.part[1], span.note)
            if span.part in enabled and position < span.end and key not in keys:
                keys.add(key)
                notes.append((span.note, span.velocity))
        return MeterReading(tuple(notes[:COIL_TIMERS]), max(len(notes) - COIL_TIMERS, 0))

    def push_log_lines(self, lines: list[LogLine]):
        if lines:
            self.log_number = lines[-1].number
        for line in [line for line in lines if self.show_debug or not line.debug][-LOG_LINES:]:
            self.log.push(line.text)

    def toggle_debug(self, e: events.ValueChangeEventArguments[bool | None]):
        # show the history again, with or without the debug messages
        self.show_debug = bool(e.value)
        self.log.clear()
        self.push_log_lines(self.player.log_since(0))

    def show_progress(self, song: Song, position: float):
        self.set_roll_position(position / song.length if song.length else 0.0)
        self.time_label.set_text(f"{format_time(position)} / {format_time(song.length)}")

    def enabled_parts(self, song: Song) -> set[PartKey]:
        return {part for part in song.parts if not self.player.is_muted(song, part)}

    def update_roll(self, song: Song | None, enabled: set[PartKey]):
        # the roll is drawn in the browser, so only send it what changed once the browser is connected
        if not self.client.has_socket_connection:
            return
        if song is None:
            if self.roll_song is not None:
                self.client.run_javascript(f"teslaRoll.setSong({self.roll.id}, null)")
                self.load_label.set_text("")
            self.roll_song = None
            self.roll_enabled = None
            return
        if song.path != self.roll_song:
            self.roll_song = song.path
            notes: list[list[float]] = [
                [round(span.start, 3), round(span.end - span.start, 3), span.note, song.part_order.index(span.part)]
                for span in song.spans
            ]
            pitches = [span.note for span in song.spans] or [60]
            data: dict[str, object] = {
                "length": song.length,
                "low": min(pitches) - 1,
                "high": max(pitches) + 1,
                "colors": [PART_COLORS[idx % len(PART_COLORS)] for idx in range(len(song.part_order))],
                "notes": notes,
            }
            self.client.run_javascript(f"teslaRoll.setSong({self.roll.id}, {json.dumps(data)})")
        enabled_key = (song.path, frozenset(enabled))
        if enabled_key != self.roll_enabled:
            self.roll_enabled = enabled_key
            load = song.load(enabled, LOAD_BINS)
            enabled_idx = [song.part_order.index(part) for part in enabled]
            self.client.run_javascript(
                f"teslaRoll.setEnabled({self.roll.id}, {json.dumps(enabled_idx)}, {json.dumps(load)}, {COIL_TIMERS})"
            )
            peak = max(load, default=0)
            self.load_label.set_text(f"peak {peak}/{COIL_TIMERS} notes at once")
            if peak > COIL_TIMERS:
                self.load_label.classes(add="text-negative")
            else:
                self.load_label.classes(remove="text-negative")

    def set_roll_position(self, fraction: float):
        if fraction == self.roll_position or not self.client.has_socket_connection:
            return
        # animate the small steps of the regular updates, jump on seeking or stopping
        animate = 0 < fraction - self.roll_position < 0.05
        self.roll_position = fraction
        self.client.run_javascript(f"teslaRoll.setPosition({self.roll.id}, {fraction}, {json.dumps(animate)})")

    # --- connection and playback ---

    @staticmethod
    def list_ports() -> list[str]:
        return sorted(port.device for port in list_ports.comports())

    def refresh_ports(self):
        ports = self.list_ports()
        if self.port_select.value and self.port_select.value not in ports:
            ports.append(self.port_select.value)
        self.port_select.set_options(ports, value=self.port_select.value)  # pyright: ignore[reportUnknownMemberType]

    async def toggle_connection(self):
        try:
            if self.player.state == PlayerState.DISCONNECTED:
                if not self.port_select.value:
                    ui.notify("Select a serial port first", type="warning")
                    return
                await run.io_bound(self.player.connect, self.port_select.value)
            else:
                await run.io_bound(self.player.disconnect)
        except Exception as e:
            ui.notify(f"Connection failed: {e}", type="negative")
        self.update()

    async def reset(self) -> None:
        await run.io_bound(self.player.reset)
        self.update()

    def select(self, song: Song):
        self.selected = song
        self.update()

    def play(self, start: float = 0.0):
        if self.selected is None:
            return
        if self.play_output == "speaker":
            self.play_on_speaker(self.selected, None, "", start)
        else:
            self.stop_speaker()
            self.release_all_keys()
            try:
                self.player.play(self.selected, start)
            except Exception as e:
                ui.notify(f"Could not play: {e}", type="negative")
        self.update()

    def stop(self):
        # stop what's shown: the speaker playback of this tab, otherwise the coil
        if self.speaker is not None:
            self.stop_speaker()
        else:
            self.player.stop()
        self.update()

    def seek(self, e: events.GenericEventArguments):
        # seek within the shown song, otherwise start the selected song from the clicked position
        fraction = min(max(float(e.args), 0.0), 1.0)
        if self.speaker is not None:
            speaker = self.speaker
            self.play_on_speaker(speaker.song, speaker.parts, speaker.label, fraction * speaker.song.length)
        elif self.player.state == PlayerState.PLAYING and self.player.current_song is not None:
            self.release_all_keys()
            try:
                self.player.play(self.player.current_song, fraction * self.player.current_song.length)
            except Exception as error:
                ui.notify(f"Could not play: {error}", type="negative")
        elif self.selected is not None and self.play_button.enabled:
            self.play(fraction * self.selected.length)
        self.update()

    async def upload(self, e: events.UploadEventArguments):
        name = Path(e.file.name).name
        if Path(name).suffix.lower() not in [".mid", ".midi"]:
            ui.notify(f'"{name}" is not a MIDI file', type="warning")
            return
        destination = self.folder / name
        counter = 1
        while destination.exists():
            destination = self.folder / f"{Path(name).stem} ({counter}){Path(name).suffix}"
            counter += 1
        await e.file.save(destination)
        try:
            Song(destination)
        except Exception as error:
            destination.unlink()
            ui.notify(f'"{name}" is not a valid MIDI file: {error}', type="negative")
            return
        self.player.songs_changed()
        ui.notify(f'Added "{destination.stem}"', type="positive")

    # --- parts and preview ---

    def set_all_muted(self, muted: bool):
        if self.selected is not None:
            self.player.set_muted(self.selected, list(self.selected.parts), muted)

    def is_previewing(self, song: Song, parts: list[PartKey]) -> bool:
        speaker = self.speaker
        return speaker is not None and speaker.song.path == song.path and speaker.parts == tuple(parts)

    def toggle_preview(self, song: Song, parts: list[PartKey], label: str):
        if self.is_previewing(song, parts):
            self.stop_speaker()
        else:
            self.play_on_speaker(song, tuple(parts), label)
        self.update()

    def play_on_speaker(self, song: Song, parts: tuple[PartKey, ...] | None, label: str, start: float = 0.0):
        if parts is None:
            enabled = {part for part in song.parts if not self.player.is_muted(song, part)}
        else:
            enabled = set(parts)
        notes = song.preview_notes(enabled)
        ui.run_javascript(f"teslaSynth.playSequence({json.dumps(notes)}, {start})")
        origin = monotonic() + SPEAKER_START_DELAY - start
        self.speaker = SpeakerPlayback(song, parts, label, origin, self.player.muted_version)

    def stop_speaker(self):
        if self.speaker is not None:
            ui.run_javascript("teslaSynth.stopSequence()")
        self.speaker = None

    # --- keyboard ---

    def shift_octave(self, direction: int):
        self.release_all_keys()
        self.base_note = min(max(self.base_note + 12 * direction, 0), 127 - NUM_PIANO_KEYS)
        self.update_octave_label()

    def update_octave_label(self):
        self.octave_label.set_text(note_name(self.base_note))

    def on_key(self, e: events.KeyEventArguments):
        if e.modifiers.ctrl or e.modifiers.alt or e.modifiers.meta or e.key.code not in KEY_OFFSETS:
            return
        source = f"key:{e.key.code}"
        # the laptop keys only play on the keyboard page, but always release the notes
        if e.action.keydown and not e.action.repeat and self.current_page == "keyboard":
            self.press(source, KEY_OFFSETS[e.key.code])
        elif e.action.keyup:
            self.release(source)

    def press(self, source: str, offset: int):
        if source in self.held:
            return
        note = self.base_note + offset
        already_sounding = note in self.held.values()
        self.held[source] = note
        if not already_sounding:
            if self.output in ["coil", "both"]:
                self.player.live_note_on(note, LIVE_VELOCITY)
            if self.output in ["speaker", "both"]:
                ui.run_javascript(f"teslaSynth.noteOn({note}, {LIVE_VELOCITY})")
        self.update_piano_keys()

    def release(self, source: str):
        note = self.held.pop(source, None)
        if note is None:
            return
        if note not in self.held.values():
            # always release on both outputs, the output may have changed while the key was held
            self.player.live_note_off(note)
            ui.run_javascript(f"teslaSynth.noteOff({note})")
        self.update_piano_keys()

    def release_all_keys(self):
        for source in list(self.held):
            self.release(source)

    def update_piano_keys(self):
        held = set(self.held.values())
        for offset, key in enumerate(self.piano_keys):
            if self.base_note + offset in held:
                key.classes(add="pressed")
            else:
                key.classes(remove="pressed")

    def logout(self):
        app.storage.user["authenticated"] = False
        ui.navigate.to("/login")


def setup(player: CoilPlayer, default_port: str, password: str | None):
    app.add_static_files("/static", STATIC_DIR)
    for script in ["synth.js", "roll.js", "spark_chart.js"]:
        # the content hash in the url makes browsers fetch the script again when it changed
        version = hashlib.sha256((STATIC_DIR / script).read_bytes()).hexdigest()[:12]
        ui.add_head_html(f'<script src="/static/{script}?v={version}"></script>', shared=True)

    @ui.page("/", title="Tesla Coil Music Player")
    def index():
        PlayerPage(player, default_port, logout=password is not None)

    # make sure the coil goes silent when the application stops
    app.on_shutdown(player.disconnect)  # pyright: ignore[reportUnknownMemberType]

    if password is None:
        return

    class AuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
            path = request.url.path
            if not app.storage.user.get("authenticated", False):  # pyright: ignore[reportUnknownMemberType]
                if path in Client.page_routes.values() and path != "/login":  # pyright: ignore[reportUnknownMemberType]
                    return RedirectResponse(f"/login?redirect_to={path}")
            return await call_next(request)

    app.add_middleware(AuthMiddleware)

    @ui.page("/login", title="Tesla Coil Music Player")
    def login(redirect_to: str = "/"):
        # only redirect within this application
        if not redirect_to.startswith("/") or redirect_to.startswith("//"):
            redirect_to = "/"

        def try_login():
            if secrets.compare_digest((password_input.value or "").encode(), password.encode()):
                app.storage.user["authenticated"] = True
                ui.navigate.to(redirect_to)
            else:
                ui.notify("Wrong password", type="negative")

        if app.storage.user.get("authenticated", False):  # pyright: ignore[reportUnknownMemberType]
            ui.navigate.to(redirect_to)
            return
        with ui.card().classes("absolute-center items-center"):
            ui.icon("bolt", size="xl", color="primary")
            ui.label("Tesla Coil Music Player").classes("text-h6")
            password_input = ui.input("Password", password=True, password_toggle_button=True)
            password_input.on("keydown.enter", try_login)
            ui.button("Log in", on_click=try_login).classes("w-full")
