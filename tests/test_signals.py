"""SIGTERM to the server and Ctrl-C or SIGTERM to km003c-capture record leave a footer."""

import signal
import subprocess
import sys
from pathlib import Path

import pytest

from capture_bench import records

TESTS = str(Path(__file__).parent)

SERVER = f"""
import os, sys, time
sys.path.insert(0, {TESTS!r})
os.environ["KM003C_PD_POLL_MS"] = "1"
from capture_bench import S3
from fakes import MeterFake
from km003c_mcp.server import Km003cService, build_tools, install_sigterm_handler
service = Km003cService(transport_factory=lambda: MeterFake(pd_payloads=S3), idle_release_s=0)
tools = build_tools(service, enable_trigger=False)
assert tools.pd_capture_start(path=sys.argv[1])["started"]
install_sigterm_handler(service)
print("ready", flush=True)
while True:
    time.sleep(0.1)
"""

RECORD = f"""
import os, sys
sys.path.insert(0, {TESTS!r})
os.environ["KM003C_PD_POLL_MS"] = "1"
from capture_bench import S3
from fakes import MeterFake
import km003c_mcp.server as srv
from km003c_mcp.cli import main
real = srv.Km003cService
srv.Km003cService = lambda **kw: real(
    transport_factory=lambda: MeterFake(pd_payloads=S3), **kw)
sys.exit(main(["record", "--timeout", "60", sys.argv[1]]))
"""


def run_until(script, path, ready, sig):
    proc = subprocess.Popen([sys.executable, "-c", script, str(path)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        line = proc.stdout.readline()
        assert line.startswith(ready), (line, proc.stderr.read() if proc.poll() else "")
        proc.send_signal(sig)
        out, err = proc.communicate(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
    return proc.returncode, line + out, err


def test_server_sigterm_stops_the_capture_with_a_footer(tmp_path):
    path = tmp_path / "server.jsonl"
    code, _, err = run_until(SERVER, path, "ready", signal.SIGTERM)
    assert code == 0, err
    footer = records(path)[-1]
    assert footer["type"] == "footer" and footer["reason"] == "manual"


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM], ids=["ctrl-c", "sigterm"])
def test_record_stops_cleanly_on_a_signal(tmp_path, sig):
    path = tmp_path / "record.jsonl"
    code, out, err = run_until(RECORD, path, "capturing", sig)
    assert code == 0 and "Traceback" not in err, err
    assert "stopping: interrupted" in err and "reason manual (complete)" in out
    assert records(path)[-1]["reason"] == "manual"
