"""Isolated test directories with inherited ACLs on Windows Python 3.13."""
import shutil
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "cache" / "test-work"


def workspace_mkdtemp(suffix=None, prefix=None, dir=None):
    ROOT.mkdir(parents=True, exist_ok=True)
    directory = ROOT / ((prefix or "test-") + uuid.uuid4().hex + (suffix or ""))
    directory.mkdir()  # inherited ACLs; tempfile's 0700 ACL excludes some sandbox tokens
    return str(directory)


class TestDirectory:
    def __init__(self):
        self.name = workspace_mkdtemp()

    def cleanup(self):
        target = Path(self.name).resolve()
        if target.parent != ROOT.resolve():
            raise ValueError("Refusing cleanup outside the test workspace")
        shutil.rmtree(target)
