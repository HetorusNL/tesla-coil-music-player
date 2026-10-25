#!/bin/sh
#
# Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
#
# This file is part of the Tesla Coil Music Player.

# Runs all tests on the controller: flashes the test firmware, runs pytest with the hardware tests, and flashes the
# normal firmware again afterwards (also when a test fails or on ctrl+c). Wire D10 to A0 and disconnect the coil.
#
#   ./test-on-board.sh [port] [pytest arguments...]
#
# The port defaults to /dev/ttyACM0, the other arguments go to pytest (e.g. -k bend, -x).

set -u
cd "$(dirname "$0")"

PORT=/dev/ttyACM0
case "${1:-}" in
/dev/*)
    PORT=$1
    shift
    ;;
esac

PIO=$(command -v pio || echo "$HOME/.platformio/penv/bin/pio")
if [ ! -x "$PIO" ]; then
    echo "pio not found, install PlatformIO or add ~/.platformio/penv/bin to the PATH" >&2
    exit 1
fi

wait_for_port() {
    # the ST-LINK comes back as a new USB device after flashing
    sleep 2
    for _ in $(seq 40); do
        [ -w "$PORT" ] && sleep 1 && return 0
        sleep 0.5
    done
    echo "$PORT didn't come back after flashing" >&2
    return 1
}

flash() {
    echo "== flashing the $1 firmware"
    # the output of the flashing only when it fails
    if ! output=$("$PIO" run -e "$1" -t upload 2>&1); then
        echo "$output" >&2
        return 1
    fi
    wait_for_port
}

restore() {
    trap - EXIT INT TERM
    flash nucleo || echo "!! flashing the normal firmware failed, run: $PIO run -t upload" >&2
}

flash nucleo_spark_test || exit 1
trap restore EXIT
trap 'exit 130' INT TERM

echo "== running the tests on $PORT"
uv run pytest --port="$PORT" "$@"
