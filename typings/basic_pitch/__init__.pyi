# type information for the parts of basic-pitch used by this project

from enum import Enum
from pathlib import Path

class FilenameSuffix(Enum):
    tf = 0
    coreml = 1
    tflite = 2
    onnx = 3

def build_icassp_2022_model_path(suffix: FilenameSuffix) -> Path: ...
