"""Offline transaction-boundary and fee regression checks (no real wallet key)."""
import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).with_name("test_execution_paths.py")), run_name="__main__")
