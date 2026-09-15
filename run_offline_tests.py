"""Run regression suites in separate processes with external sockets blocked."""
import ast
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
SUITES = ("test_reliability.py", "test_execution_paths.py", "test_inventory_funding.py", "test_ca_resolution.py",
          "test_fill_measurement.py", "test_position_persistence.py", "test_dashboard_data.py", "test_dashboard_status.py")
BOOTSTRAP = r'''
import logging, runpy, socket, sys, tempfile
from test_support import workspace_mkdtemp
tempfile.mkdtemp = workspace_mkdtemp
logging.disable(logging.CRITICAL)
original = socket.socket.connect
def local_only(sock, address):
    if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1"):
        raise AssertionError("External sockets blocked by offline test runner")
    return original(sock, address)
socket.socket.connect = local_only
filename = sys.argv[1]
sys.argv = [filename]
runpy.run_path(filename, run_name="__main__")
'''


def main():
    for path in ROOT.glob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    print("PASS: syntax check for all Python files", flush=True)
    failed = 0
    total = 0
    for suite in SUITES:
        result = subprocess.run([sys.executable, "-B", "-c", BOOTSTRAP, suite], cwd=ROOT,
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        output = result.stdout + result.stderr
        tests = re.search(r"Ran (\d+) tests", output)
        count = int(tests[1]) if tests else len(re.findall(r"\bPASS\b", output))
        total += count
        print(f"{'PASS' if result.returncode == 0 else 'FAIL'}: {suite} ({count} checks)", flush=True)
        if result.returncode:
            failed += 1
            print(output)
    print(f"{total} checks across {len(SUITES)} suites; {failed} failed suites")
    return bool(failed)


if __name__ == "__main__":
    raise SystemExit(main())
