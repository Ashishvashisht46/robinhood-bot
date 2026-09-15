"""Atomic snapshots with a short retry for Windows scanner/share locks."""
import json
import os
from pathlib import Path
import time


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    for attempt in range(5):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.025 * (attempt + 1))
