"""Run deterministic offline regressions; this does not simulate random profits."""
import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).with_name("test_reliability.py")), run_name="__main__")
