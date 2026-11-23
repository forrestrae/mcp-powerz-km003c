"""Hardware tests against a real KM003C. Run with KM003C_HW=1 and the POWER-Z PC software closed.

Tests that need a person to plug cables also need KM003C_HW_INTERACTIVE=1; they prompt on the
terminal (/dev/tty), so they work under pytest's output capture.
"""

import json
import os
import subprocess
import sys
import time

import pytest

from km003c_mcp.server import Km003cService, build_tools
from km003c_mcp.usb_device import DeviceBusyError, PyUsbTransport

pytestmark = [pytest.mark.hw,
              pytest.mark.skipif(os.environ.get("KM003C_HW") != "1", reason="set KM003C_HW=1")]


@pytest.fixture(scope="module")
def service():
    service = Km003cService()
    yield service
    service.shutdown()


@pytest.fixture(scope="module")
def tools(service):
    return build_tools(service)


def test_hw_device_info(tools):
    info = tools.device_info()
    assert info["model"] == "KM003C" and info["fw_version"] and info["usb_serial"]


def test_hw_measure(tools):
    r = tools.measure()
    assert 0 <= r["vbus_v"] < 50 and 3.0 < r["vdd_v"] < 3.6


def test_hw_stream_1000sps(tools):
    assert tools.stream_start(rate_sps=1000)["started"]
    time.sleep(2)
    st = tools.stream_stop()
    assert st["samples"] >= 1500 and st["dropped_samples"] <= st["samples"] * 0.01


def read_records(path):
    with open(path) as f:
        return [json.loads(line) for line in f]


def test_hw_pd_capture_idle(tools):
    started = tools.pd_capture_start(label="hw idle")
    assert started["started"] and os.path.isabs(started["path"])
    time.sleep(0.5)
    summary = tools.pd_capture_read()
    assert "error" not in summary and summary["running"] is True
    stopped = tools.pd_capture_stop()
    assert stopped["stopped"] and stopped["reason"] == "manual"
    recs = read_records(started["path"])
    assert recs[0]["meter"]["fw_version"] and recs[0]["meter"]["serial"]
    assert recs[0]["pd_monitor"]["outcome"] in ("accepted", "already enabled or refused")
    assert recs[-1]["type"] == "footer" and recs[-1]["vbus_mv"]["first"] is not None
    assert any(r["type"] == "status" for r in recs)


def test_hw_pd_capture_during_1000sps_stream(tools):
    # pd_capture_start sends Connect and EnablePdMonitor while the stream's poller runs.
    assert tools.stream_start(rate_sps=1000)["started"]
    time.sleep(0.3)
    assert tools.pd_capture_start()["started"]
    time.sleep(2)
    status = tools.stream_status()
    assert status["running"] and status["error"] is None
    assert "error" not in tools.pd_capture_read()
    assert tools.pd_capture_stop()["stopped"]
    st = tools.stream_stop()
    print(f"stream with capture: {st['samples']} samples, {st['dropped_samples']} dropped")
    assert st["samples"] >= 2000 and st["dropped_samples"] <= st["samples"] * 0.01


def test_hw_pd_monitor_enable(service, tools):
    # Firmware 2.0.0 answers Reject to a repeated enable with no Connect between them.
    # enable_pd_monitor() sends Connect first, which that firmware answers with Accept again.
    first = tools.pd_capture_start()["pd_monitor"]
    assert first["outcome"] in ("accepted", "already enabled or refused")
    again = service.device().enable_pd_monitor()
    assert again.outcome in ("accepted", "already enabled or refused")
    assert tools.pd_capture_stop()["stopped"]
    # stop disabled the monitor, so the next enable is accepted.
    assert tools.pd_capture_start()["pd_monitor"]["accepted"]
    assert tools.pd_capture_stop()["stopped"]


def test_hw_claim_and_release_10_times(service):
    service.release()  # free the module session's claim; later tests reopen on demand
    svc = Km003cService(idle_release_s=0)
    release_s = []
    devices = set()
    try:
        for _ in range(10):
            lease = svc.lease("hw test")
            device = svc.device()
            assert 3.0 < device.read_adc().vdd_v < 3.6
            devices.add(id(device))
            started = time.monotonic()
            lease.release()  # the last lease: releases at once
            release_s.append(time.monotonic() - started)
            assert not svc.claimed
    finally:
        svc.shutdown()
    print(f"release ms: {[round(t * 1000) for t in release_s]}")
    assert len(devices) == 10  # every cycle opened a new session
    # Disconnect is matched by type, so a release no longer waits out the 2 s timeout.
    assert max(release_s) < 0.5


def test_hw_pd_capture_idle_2s(service):
    service.release()
    svc = Km003cService(idle_release_s=0)
    tools = build_tools(svc)
    try:
        assert tools.pd_capture_start()["started"]
        time.sleep(2)
        summary = tools.pd_capture_read()
        assert summary["running"] and "error" not in summary
        assert svc.claimed  # the capture's lease keeps the meter claimed
        assert tools.pd_capture_stop()["stopped"]
        assert tools.pd_capture_read()["reason"] == "manual"
        assert not svc.claimed
    finally:
        svc.shutdown()


HOLDER = """
import sys
from km003c_mcp.usb_device import PyUsbTransport
t = PyUsbTransport.open()
print("holding", flush=True)
sys.stdin.read()
t.close()
"""


def test_hw_busy_error_names_the_holder(service):
    service.shutdown()  # release this session's claim; later tests reopen on demand
    holder = subprocess.Popen([sys.executable, "-c", HOLDER], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "holding"
        with pytest.raises(DeviceBusyError) as info:
            PyUsbTransport.open()
    finally:
        holder.communicate(timeout=10)
    msg = str(info.value)
    assert "held by another process" in msg and "Likely holders" in msg
    if sys.platform == "darwin":
        assert f"pid {holder.pid}," in msg
    PyUsbTransport.open().close()  # free again once the holder exits


@pytest.fixture
def trigger_tools(tools):
    if not hasattr(tools, "trigger_raw"):
        pytest.skip("trigger tools are not registered (KM003C_ENABLE_TRIGGER)")
    return tools


def test_hw_trigger_open_close(trigger_tools):
    tools = trigger_tools
    lines = tools.trigger_raw("pd pdo")["lines"]
    assert any(line.startswith("max power") for line in lines)
    assert tools.trigger_close()["lines"] == ["ok"]


def test_hw_trigger_and_usb_concurrently(trigger_tools):
    tools = trigger_tools
    tools.stream_start(rate_sps=50)
    assert "lines" in tools.trigger_raw("pd pdo")
    time.sleep(0.5)
    assert tools.stream_status()["error"] is None
    tools.stream_stop()
    tools.trigger_close()


def test_hw_capture_stops_on_timeout(tools):
    started = tools.pd_capture_start(stop_on="contract", timeout_s=1)
    summary = tools.pd_capture_wait(timeout_s=5)
    assert summary["running"] is False and summary["reason"] == "timeout"
    assert read_records(started["path"])[-1]["reason"] == "timeout"


def prompt(text: str, wait_for_enter: bool = True) -> None:
    with open("/dev/tty", "r+") as tty:
        tty.write(f"\n>>> {text}" + (" " if wait_for_enter else "\n"))
        tty.flush()
        if wait_for_enter:
            tty.readline()


@pytest.mark.skipif(os.environ.get("KM003C_HW_INTERACTIVE") != "1",
                    reason="set KM003C_HW_INTERACTIVE=1; needs a cold attach by a person")
def test_hw_cold_attach_stops_on_contract(tools):
    firmware = tools.device_info()["fw_version"]
    prompt("Unplug the meter from both the charger and the sink, then press Enter")
    started = tools.pd_capture_start(label="hw cold attach", stop_on="contract", timeout_s=120,
                                     provenance={"test": "test_hw_cold_attach_stops_on_contract"})
    assert started["started"], started
    prompt("Plug the meter into the sink, then the charger into the meter", wait_for_enter=False)
    summary = tools.pd_capture_wait(timeout_s=130)
    print(json.dumps(summary, indent=1))
    recs = read_records(started["path"])
    assert recs[0]["meter"]["fw_version"] == firmware
    assert recs[-1]["type"] == "footer" and recs[-1]["reason"] == "contract"
    assert summary["contract"] is not None and summary["warnings"] == []
