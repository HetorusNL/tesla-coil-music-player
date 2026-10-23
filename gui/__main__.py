#!/usr/bin/env python
#
# Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
#
# This file is part of the Tesla Coil Music Player.

import argparse
import os
import secrets
from pathlib import Path

from nicegui import app, ui

from gui.app import setup
from gui.coil_player import CoilPlayer

LOCAL_HOSTS = ["127.0.0.1", "localhost", "::1"]


def main():
    parser = argparse.ArgumentParser(description="Web interface that plays MIDI songs on the tesla coil")
    parser.add_argument("--host", default="0.0.0.0", help="address to listen on (default: all interfaces)")
    parser.add_argument("--port", type=int, default=8080, help="port of the web interface (default: 8080)")
    parser.add_argument("--serial-port", default="/dev/ttyACM0", help="serial port of the tesla coil controller")
    parser.add_argument(
        "--songs", type=Path, default=Path("songs"), help="folder to start browsing in (default: ./songs)"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("coil-config.json"),
        help="file with the settings of the sparks (default: ./coil-config.json)",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("TESLA_COIL_PASSWORD"),
        help="password of the web interface (default: $TESLA_COIL_PASSWORD)",
    )
    args = parser.parse_args()
    if not args.password and args.host not in LOCAL_HOSTS:
        parser.error("a password is required when listening on the network, use --password or --host 127.0.0.1")

    # start in the current directory when there is no songs folder here
    song_dir = args.songs if args.songs.is_dir() else Path.cwd()
    try:
        player = CoilPlayer(song_dir.resolve(), args.config)
    except ValueError as e:
        parser.error(str(e))
    setup(player, args.serial_port, args.password or None)

    # connect to the tesla coil controller right away when it's plugged in
    def auto_connect():
        if Path(args.serial_port).exists():
            try:
                player.connect(args.serial_port)
            except Exception as e:
                player.log(f"could not connect to {args.serial_port}: {e}")

    # nicegui types some of its handlers and containers without their element types
    app.on_startup(auto_connect)  # pyright: ignore[reportUnknownMemberType]
    ui.run(  # pyright: ignore[reportUnknownMemberType]
        host=args.host,
        port=args.port,
        title="Tesla Coil Music Player",
        favicon="⚡",
        dark=None,  # follow the theme of the system
        reload=False,
        show=False,
        storage_secret=secrets.token_hex(32),
    )


if __name__ == "__main__":
    main()
