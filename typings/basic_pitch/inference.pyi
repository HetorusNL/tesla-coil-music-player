from pathlib import Path
from typing import Any

class Model:
    def __init__(self, model_path: str | Path) -> None: ...

# a note event: start and end in seconds, MIDI note number, amplitude and the optional pitch bends
type NoteEvent = tuple[float, float, int, float, list[int] | None]

def predict(
    audio_path: str | Path,
    model_or_model_path: Model | str | Path = ...,
    onset_threshold: float = 0.5,
    frame_threshold: float = 0.3,
    minimum_note_length: float = 127.7,
    minimum_frequency: float | None = None,
    maximum_frequency: float | None = None,
) -> tuple[dict[str, Any], Any, list[NoteEvent]]: ...
