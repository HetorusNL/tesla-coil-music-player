# type information for the parts of demucs used by this project

from collections.abc import Callable
from pathlib import Path
from typing import TypedDict

from torch import Tensor

class ProgressInfo(TypedDict):
    model_idx_in_bag: int
    shift_idx: int
    segment_offset: int
    state: str
    audio_length: int
    models: int

class Separator:
    samplerate: int
    def __init__(self, model: str = "htdemucs", *, callback: Callable[[ProgressInfo], None] | None = None) -> None: ...
    def separate_audio_file(self, file: Path) -> tuple[Tensor, dict[str, Tensor]]: ...
