#!/usr/bin/env python
#
# Copyright (c) 2026 Tim Klein Nijenhuis <tim@hetorus.nl>
#
# This file is part of the Tesla Coil Music Player.

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

# the limits of SparkConfig in the controller firmware
NUM_ON_TIMES = 20
ABSOLUTE_MAX_ON_TIME = 200  # us, no setting can make a spark longer than this
MAX_TABLE_ON_TIME = ABSOLUTE_MAX_ON_TIME  # us, the table holds the actual on-times at full volume
MAX_VOLUME = 1.0

DEFAULT_ON_TIMES = [80, 75, 70, 66, 62, 59, 57, 56, 55, 54, 53, 52, 51, 50, 50, 50, 50, 50, 50, 50]


@dataclass
class SparkConfig:
    """The settings of the sparks, mirrors SparkConfig of the controller firmware."""

    # on-time (us) per 100 Hz band at full volume, interpolated between the bands
    on_times: list[int] = field(default_factory=lambda: list(DEFAULT_ON_TIMES))
    min_on_time: int = 10
    max_on_time: int = 100
    # the factor of the on-times of the table, like a master fader
    volume: float = 1.0
    # how much a lower note velocity shortens the spark: 0 not at all, 1 a zero velocity gives no spark
    velocity_sensitivity: float = 0.0

    def errors(self) -> list[str]:
        errors: list[str] = []
        if len(self.on_times) != NUM_ON_TIMES:
            errors.append(f"the on-time table needs {NUM_ON_TIMES} entries")
        if any(not 0 <= on_time <= MAX_TABLE_ON_TIME for on_time in self.on_times):
            errors.append(f"the on-times of the table must be between 0 and {MAX_TABLE_ON_TIME} µs")
        if not 0 <= self.min_on_time <= self.max_on_time <= ABSOLUTE_MAX_ON_TIME:
            errors.append(f"the on-times must be 0 ≤ min ≤ max ≤ {ABSOLUTE_MAX_ON_TIME} µs")
        if not 0 <= self.volume <= MAX_VOLUME:
            errors.append(f"the volume must be between 0 and {MAX_VOLUME:.0%}")
        if not 0 <= self.velocity_sensitivity <= 1:
            errors.append("the velocity sensitivity must be between 0 and 100%")
        return errors


def load_config(path: Path) -> SparkConfig:
    """Loads the settings, the missing ones (or all when the file doesn't exist) get their default."""
    if not path.exists():
        return SparkConfig()
    data: dict[str, Any] = json.loads(path.read_text())
    names = {f.name for f in fields(SparkConfig)}
    config = SparkConfig(**{name: value for name, value in data.items() if name in names})
    if errors := config.errors():
        raise ValueError(f"invalid settings in {path}: {', '.join(errors)}")
    return config


def save_config(path: Path, config: SparkConfig):
    path.write_text(json.dumps(asdict(config), indent=4) + "\n")
