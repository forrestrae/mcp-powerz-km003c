"""Claim on demand and release when idle: leases, the idle timer, and device_release."""

import threading
import time

import pytest

import km003c_mcp.server as srv
from fakes import MeterFake
from km003c_mcp.protocol import CMD_CONNECT, CMD_DISABLE_PD_MONITOR, CMD_DISCONNECT
from km003c_mcp.server import Km003cService, build_tools, idle_release_from_env
from km003c_mcp.usb_device import DeviceTimeoutError, Km003cDevice


class ManualTimer:
    """threading.Timer stand-in that fires only when the test says so."""

    def __init__(self, delay, fn):
        self.delay, self.fn = delay, fn
        self.started = self.cancelled = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.fn()


class Bench:
    """A service on MeterFake transports, with every transport and timer recorded."""

    def __init__(self, idle_release_s, **meter_kwargs):
        self.meters: list[MeterFake] = []
        self.timers: list[ManualTimer] = []
        self.violations: list[dict] = []
        self.meter_kwargs = meter_kwargs
        self.service = Km003cService(
            transport_factory=self._open, port_finder=lambda *a, **k: "COMX",
            idle_release_s=idle_release_s, timer_factory=self._timer)
        self.tools = build_tools(self.service)

    def _open(self):
        meter = MeterFake(on_close=self._closing, **self.meter_kwargs)
        self.meters.append(meter)
        return meter

    def _closing(self, meter):
        held = self.service.leases()
        if held:
            self.violations.append(held)

    def _timer(self, delay, fn):
        timer = ManualTimer(delay, fn)
        self.timers.append(timer)
        return timer

    def released(self, meter):
        return meter.closed and meter.commands()[-1] == CMD_DISCONNECT


def wait_until(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.005)
    raise AssertionError("condition not reached")


def test_release_after_idle_timer_and_reopen_on_next_call():
    bench = Bench(idle_release_s=2.0)
    assert "vbus_v" in bench.tools.measure()
    assert bench.service.claimed and len(bench.timers) == 1
    assert bench.timers[0].delay == 2.0 and bench.timers[0].started
    bench.timers[0].fire()
    assert not bench.service.claimed and bench.released(bench.meters[0])
    assert "vbus_v" in bench.tools.measure()
    assert len(bench.meters) == 2 and bench.meters[1].commands()[0] == CMD_CONNECT
    assert bench.meters[0].writes_after_close == [] and bench.violations == []


def test_call_within_idle_period_cancels_the_timer():
    bench = Bench(idle_release_s=2.0)
    bench.tools.measure()
    bench.tools.measure()
    assert bench.timers[0].cancelled and not bench.timers[1].cancelled
    bench.timers[0].fire()  # a cancelled timer that fires anyway must not release
    assert bench.service.claimed and len(bench.meters) == 1
    bench.timers[1].fire()
    assert not bench.service.claimed


def test_zero_releases_at_once():
    bench = Bench(idle_release_s=0)
    assert "vbus_v" in bench.tools.measure()
    assert not bench.service.claimed and bench.released(bench.meters[0])
    assert bench.timers == []
    assert "vbus_v" in bench.tools.measure()
    assert len(bench.meters) == 2 and bench.released(bench.meters[1])


def test_default_idle_release_is_never():
    # Upstream's default: unset KM003C_IDLE_RELEASE_S keeps the meter claimed.
    assert srv.DEFAULT_IDLE_RELEASE_S is None


def test_upstream_default_never_releases(monkeypatch):
    monkeypatch.delenv("KM003C_IDLE_RELEASE_S", raising=False)
    monkeypatch.setattr(srv, "DEFAULT_IDLE_RELEASE_S", None)
    meters = []
    svc = Km003cService(transport_factory=lambda: meters.append(MeterFake()) or meters[-1],
                        timer_factory=lambda *a: pytest.fail("no timer expected"))
    tools = build_tools(svc)
    for _ in range(3):
        assert "vbus_v" in tools.measure()
    assert svc.idle_release_s is None and svc.claimed and len(meters) == 1
    assert not meters[0].closed
    svc.shutdown()
    assert meters[0].closed


def test_no_release_while_capture_runs():
    bench = Bench(idle_release_s=0)
    assert bench.tools.pd_capture_start()["started"]
    assert bench.service.claimed and bench.service.leases() == {"pd capture": 1}
    bench.tools.measure()  # a call during the capture ends its lease; the capture's remains
    time.sleep(0.05)
    assert bench.service.claimed and bench.service.recorder.pd_running
    assert bench.tools.pd_capture_read()["running"] is True
    assert bench.tools.pd_capture_stop()["stopped"]
    meter = bench.meters[0]
    assert not bench.service.claimed and bench.released(meter)
    # DisablePdMonitor went out before the release's Disconnect.
    assert meter.commands()[-2] == CMD_DISABLE_PD_MONITOR
    assert bench.violations == [] and meter.writes_after_close == []


def test_no_release_while_stream_runs(monkeypatch):
    def authenticate(self):
        self.auth_level = 1
        return 1
    monkeypatch.setattr(Km003cDevice, "authenticate", authenticate)
    bench = Bench(idle_release_s=0)
    assert bench.tools.stream_start(rate_sps=50)["started"]
    time.sleep(0.05)
    assert bench.service.claimed and bench.service.leases() == {"stream": 1}
    assert bench.tools.stream_status()["running"]
    bench.tools.stream_stop()
    assert not bench.service.claimed and bench.released(bench.meters[0])
    assert bench.violations == []


def test_reading_saved_data_does_not_claim():
    bench = Bench(idle_release_s=0)
    assert bench.tools.pd_capture_read()["kind"] == "Km003cError"  # no capture yet
    assert bench.tools.stream_status()["running"] is False
    assert bench.tools.stream_samples() == {"samples": []}
    assert bench.meters == []


def test_capture_failure_releases_its_lease():
    bench = Bench(idle_release_s=0)
    bench.tools.pd_capture_start()
    meter = bench.meters[0]
    # Make every later reply a timeout: the poller fails, drops the session, and its lease.
    meter.read = lambda size, timeout_ms: (_ for _ in ()).throw(DeviceTimeoutError("gone"))
    wait_until(lambda: not bench.service.recorder.pd_running)
    wait_until(lambda: bench.service.leases() == {})
    assert bench.tools.pd_capture_read()["reason"] == "error"
    assert not bench.service.claimed
    assert "vbus_v" in bench.tools.measure()
    assert len(bench.meters) == 2


def test_concurrent_calls_share_the_claim():
    bench = Bench(idle_release_s=0)
    errors = []

    def worker():
        for _ in range(20):
            r = bench.tools.measure()
            if "vbus_v" not in r:
                errors.append(r)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert errors == [] and bench.violations == []
    assert bench.service.leases() == {} and not bench.service.claimed
    assert all(m.closed and m.writes_after_close == [] for m in bench.meters)


def test_call_during_release_waits_then_reopens():
    hold = threading.Event()
    bench = Bench(idle_release_s=0, hold_disconnect=hold)
    first = threading.Thread(target=bench.tools.measure)
    first.start()
    wait_until(lambda: bench.meters and bench.meters[0].commands()[-1:] == [CMD_DISCONNECT])
    result = {}
    second = threading.Thread(target=lambda: result.update(bench.tools.measure()))
    second.start()
    time.sleep(0.1)
    assert result == {} and len(bench.meters) == 1  # waiting for the release to finish
    hold.set()
    first.join(5)
    second.join(5)
    assert "vbus_v" in result and len(bench.meters) == 2
    assert bench.violations == [] and bench.meters[0].writes_after_close == []


def test_device_release_releases_at_once():
    bench = Bench(idle_release_s=None)
    bench.tools.measure()
    assert bench.tools.device_release() == {"released": True}
    assert bench.released(bench.meters[0]) and not bench.service.claimed
    assert bench.tools.device_release() == {"released": False}
    assert "vbus_v" in bench.tools.measure() and len(bench.meters) == 2


def test_device_release_cancels_a_pending_timer():
    bench = Bench(idle_release_s=2.0)
    bench.tools.measure()
    assert bench.tools.device_release() == {"released": True}
    assert bench.timers[0].cancelled
    bench.timers[0].fire()
    assert len(bench.meters) == 1 and bench.meters[0].writes_after_close == []


def test_device_release_refused_while_capture_runs():
    bench = Bench(idle_release_s=None)
    bench.tools.pd_capture_start()
    r = bench.tools.device_release()
    assert r["kind"] == "Km003cError" and "pd capture" in r["error"]
    assert bench.service.claimed and bench.service.recorder.pd_running
    bench.tools.pd_capture_stop()
    assert bench.tools.device_release() == {"released": True}


def test_lease_release_is_idempotent():
    bench = Bench(idle_release_s=None)
    lease = bench.service.lease("test")
    lease.release()
    lease.release()
    assert bench.service.leases() == {}


@pytest.mark.parametrize("value, expected", [
    ("", None), ("0", 0.0), ("2", 2.0), ("0.5", 0.5), ("never", None), ("OFF", None),
    ("-1", None), ("soon", None), ("nan", None),
])
def test_idle_release_from_env(monkeypatch, value, expected):
    monkeypatch.setattr(srv, "DEFAULT_IDLE_RELEASE_S", None)
    monkeypatch.setenv("KM003C_IDLE_RELEASE_S", value)
    assert idle_release_from_env() == expected

