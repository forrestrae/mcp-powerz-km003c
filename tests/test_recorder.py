import json
import struct
import threading
import time

import pytest

from km003c_mcp.protocol import ATT_ADCQUEUE, ATT_PD, LogicalPacket
from km003c_mcp.recorder import Recorder
from km003c_mcp.usb_device import (
    Km003cError, PdMonitorEnable, PdMonitorNotEnabledError, TransportError,
)
from test_protocol import PD_STREAM


ACCEPTED = PdMonitorEnable(True, "accepted", 1, "05010000")


class FakeDevice:
    def __init__(self, replies=(), error=None):
        self.replies = list(replies)
        self.error = error
        self.masks = []
        self.graph = []
        self.monitor = []
        self.enable_result = ACCEPTED
        self.lock = threading.RLock()

    def get_data(self, mask):
        self.masks.append(mask)
        if self.replies:
            return self.replies.pop(0)
        if self.error:
            raise self.error
        return []

    def start_graph(self, rate):
        self.graph.append(("start", rate))

    def stop_graph(self):
        self.graph.append(("stop",))

    def enable_pd_monitor(self):
        self.monitor.append("enable")
        if isinstance(self.enable_result, Exception):
            raise self.enable_result
        return self.enable_result

    def disable_pd_monitor(self):
        self.monitor.append("disable")
        return True


def queue(seqs, uv=10_000_000, ua=1_000_000):
    payload = b"".join(struct.pack("<HHiiHHHH", s, 3, uv, ua, 0, 0, 0, 0) for s in seqs)
    return [LogicalPacket(ATT_ADCQUEUE, False, len(seqs), payload)]


def wait_until(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.005)
    raise AssertionError("condition not reached")


def test_stream_stats_energy_and_drops():
    dev = FakeDevice([queue([0, 1, 2, 5])])
    rec = Recorder(lambda: dev, poll_interval_s=0.001)
    rec.start_stream(3)
    wait_until(lambda: rec.stream_status()["stats"]["samples"] == 4)
    st = rec.stop_stream()
    assert dev.graph == [("start", 3), ("stop",)]
    assert st.dropped_samples == 2 and st.vbus_mean_v == pytest.approx(10.0)
    assert st.power_max_w == pytest.approx(10.0)
    assert st.energy_wh == pytest.approx(4 * 10 * 0.001 / 3600)
    assert st.charge_mah == pytest.approx(4 * 1 * 0.001 / 3.6)


def test_stream_warmup_discards_previous_rate_samples():
    # Rate 2 (50 SPS, step 20): leftover 1 ms-step samples must be ignored.
    dev = FakeDevice([queue([100, 101, 102]), queue([120, 140, 160])])
    rec = Recorder(lambda: dev, poll_interval_s=0.001)
    rec.start_stream(2)
    wait_until(lambda: len(dev.masks) >= 3)
    st = rec.stop_stream()
    assert st.samples == 3 and st.dropped_samples == 0


def test_stream_writes_csv(tmp_path):
    dev = FakeDevice([queue([0, 1])])
    rec = Recorder(lambda: dev, poll_interval_s=0.001)
    rec.start_stream(3, csv_path=str(tmp_path / "s.csv"))
    wait_until(lambda: rec.stream_status()["stats"]["samples"] == 2)
    assert rec.stop_stream().csv_path == str(tmp_path / "s.csv")
    lines = (tmp_path / "s.csv").read_text().splitlines()
    assert lines[0] == "sequence,marker,vbus_v,ibus_a,power_w,cc1_v,cc2_v,dp_v,dm_v"
    assert len(lines) == 3 and lines[1].startswith("0,3,10.0,1.0,10.0")


def test_stream_twice_raises():
    rec = Recorder(lambda: FakeDevice(), poll_interval_s=0.001)
    rec.start_stream(3)
    with pytest.raises(Km003cError):
        rec.start_stream(3)
    rec.stop_stream()


def test_empty_putdata_is_ignored():
    dev = FakeDevice()
    rec = Recorder(lambda: dev, poll_interval_s=0.001)
    rec.start_stream(3)
    wait_until(lambda: len(dev.masks) >= 5)
    status = rec.stream_status()
    assert status["running"] and status["error"] is None and status["stats"]["samples"] == 0
    rec.stop_stream()


def test_stream_samples_decimates():
    dev = FakeDevice([queue(list(range(100)))])
    rec = Recorder(lambda: dev, poll_interval_s=0.001)
    rec.start_stream(3)
    wait_until(lambda: rec.stream_status()["stats"]["samples"] == 100)
    out = rec.stream_samples(max_samples=10)
    assert len(out) == 10 and out[0]["sequence"] == 0
    assert len(rec.stream_status()["recent"]) == 20
    rec.stop_stream()


def records(path):
    return [json.loads(line) for line in open(path)]


def test_pd_capture_writes_records_and_stop_writes_the_footer():
    dev = FakeDevice([[LogicalPacket(ATT_PD, False, 0, PD_STREAM)]])
    rec = Recorder(lambda: dev, poll_interval_s=0.001)
    rec.start_pd(label="unit")
    path = rec.capture_path
    wait_until(lambda: len(records(path)) >= 5)
    assert rec.stop_pd() == path
    kinds = [r["type"] for r in records(path)]
    assert kinds[:5] == ["header", "status", "conn", "pd", "conn"] and kinds[-1] == "footer"
    pd = records(path)[3]
    assert pd["msg"] == "Source_Capabilities" and pd["wire"] == "81112c910100"
    assert records(path)[-1]["reason"] == "manual"
    assert rec.pd_running is False and rec.capture_path == path


def test_device_error_stops_and_reports():
    dev = FakeDevice(error=Km003cError("unplugged"))
    rec = Recorder(lambda: dev, poll_interval_s=0.001)
    rec.start_stream(3)
    wait_until(lambda: not rec.stream_status()["running"])
    assert "unplugged" in rec.stream_status()["error"]


def test_combined_mask_when_both_active():
    dev = FakeDevice()
    rec = Recorder(lambda: dev, poll_interval_s=0.001)
    rec.start_stream(3)
    rec.start_pd()
    wait_until(lambda: (ATT_ADCQUEUE | ATT_PD) in dev.masks)
    rec.stop_pd()
    rec.stop_stream()


def test_fast_stream_polls_at_least_every_20ms():
    # The device returns at most 63 queued samples per reply (6-bit chunk) and drops older ones,
    # so a 1000 SPS stream must be polled well within 63 ms regardless of the default interval.
    dev = FakeDevice()
    rec = Recorder(lambda: dev, poll_interval_s=0.05)
    rec.start_stream(3)
    time.sleep(0.3)
    rec.stop_stream()
    assert len(dev.masks) >= 10


def test_transport_error_triggers_callback():
    from km003c_mcp.usb_device import DeviceTimeoutError
    calls = []
    dev = FakeDevice(error=DeviceTimeoutError("unplugged"))
    rec = Recorder(lambda: dev, poll_interval_s=0.001, on_transport_error=lambda: calls.append(1))
    rec.start_pd()
    wait_until(lambda: calls == [1])


def test_logical_error_stops_device_stream():
    dev = FakeDevice(error=Km003cError("bad reply"))
    rec = Recorder(lambda: dev, poll_interval_s=0.001)
    rec.start_stream(3)
    wait_until(lambda: ("stop",) in dev.graph)
    assert not rec.stream_status()["running"]


def test_bad_csv_path_does_not_start_graph(tmp_path):
    dev = FakeDevice()
    rec = Recorder(lambda: dev, poll_interval_s=0.001)
    with pytest.raises(OSError):
        rec.start_stream(3, csv_path=str(tmp_path / "missing" / "s.csv"))
    assert dev.graph == [] and not rec.stream_status()["running"]


def test_abort_stops_running_capture():
    rec = Recorder(lambda: FakeDevice(), poll_interval_s=0.001)
    rec.start_stream(3)
    rec.abort("USB session reset")
    status = rec.stream_status()
    assert not status["running"] and "USB session reset" in status["error"]


def test_start_pd_enables_monitor_and_stop_disables_it():
    dev = FakeDevice()
    rec = Recorder(lambda: dev, poll_interval_s=0.001)
    assert rec.pd_monitor is None
    assert rec.start_pd() == ACCEPTED
    assert rec.pd_monitor == ACCEPTED and dev.monitor == ["enable"]
    rec.stop_pd()
    assert dev.monitor == ["enable", "disable"]
    rec.stop_pd()  # already stopped: nothing more is sent
    assert dev.monitor == ["enable", "disable"]
    assert rec.pd_monitor == ACCEPTED  # still readable after the capture stops


def test_start_pd_with_reject_still_captures():
    dev = FakeDevice([[LogicalPacket(ATT_PD, False, 0, PD_STREAM)]])
    dev.enable_result = PdMonitorEnable(False, "already enabled or refused", 1, "06010000")
    rec = Recorder(lambda: dev, poll_interval_s=0.001)
    assert rec.start_pd().accepted is False
    wait_until(lambda: any(r["type"] == "pd" for r in records(rec.capture_path)))
    rec.stop_pd()
    assert records(rec.capture_path)[0]["pd_monitor"]["outcome"] == "already enabled or refused"


def test_start_pd_does_not_start_when_enable_fails():
    dev = FakeDevice()
    dev.enable_result = PdMonitorNotEnabledError("never accepted")
    rec = Recorder(lambda: dev, poll_interval_s=0.001)
    with pytest.raises(PdMonitorNotEnabledError):
        rec.start_pd()
    assert rec.pd_running is False and dev.masks == []
    assert rec.pd_monitor is None


def test_logical_error_disables_pd_monitor():
    dev = FakeDevice(error=Km003cError("bad reply"))
    rec = Recorder(lambda: dev, poll_interval_s=0.001)
    rec.start_pd()
    wait_until(lambda: dev.monitor == ["enable", "disable"])
    assert "bad reply" in rec.pd_capture_state()["error"]


class LockingDevice(FakeDevice):
    """FakeDevice whose commands take the device lock, as Km003cDevice's do."""

    def __init__(self):
        super().__init__()
        self.calls = []
        self.fail_next = False

    def get_data(self, mask):
        if self.fail_next:  # a reply that fails to parse; no lock needed to report it
            self.fail_next = False
            raise Km003cError("bad reply")
        with self.lock:
            return super().get_data(mask)

    def enable_pd_monitor(self):
        with self.lock:
            self.calls.append("enable")
            return super().enable_pd_monitor()

    def disable_pd_monitor(self):
        with self.lock:
            self.calls.append("disable")
            return super().disable_pd_monitor()


def test_restart_during_failure_cleanup_gets_a_poller_and_keeps_its_monitor():
    dev = LockingDevice()
    rec = Recorder(lambda: dev, poll_interval_s=0.001)
    rec.start_pd()
    with dev.lock:
        # The poller fails and its cleanup waits for the device lock: the window in which the
        # capture already reads as stopped but the old thread has not sent its disable yet.
        dev.fail_next = True
        wait_until(lambda: not rec.pd_running)
        rec.start_pd()
    polls = len(dev.masks)
    wait_until(lambda: len(dev.masks) > polls + 5)  # the new capture is polled
    time.sleep(0.05)
    # The old thread saw the newer start and skipped the disable it would have sent.
    assert dev.calls == ["enable", "enable"]
    state = rec.pd_capture_state()
    assert state["running"] and state["error"] is None
    rec.stop_pd()
    assert dev.calls == ["enable", "enable", "disable"]
    assert rec._thread is None


def test_failure_cleanup_runs_before_a_later_start():
    release = threading.Event()

    class SlowDisable(LockingDevice):
        def disable_pd_monitor(self):
            with self.lock:
                self.calls.append("disable")
                release.wait(2)
                return True

    dev = SlowDisable()
    rec = Recorder(lambda: dev, poll_interval_s=0.001)
    rec.start_pd()
    dev.fail_next = True
    wait_until(lambda: dev.calls == ["enable", "disable"])
    starter = threading.Thread(target=rec.start_pd)
    starter.start()
    time.sleep(0.05)
    release.set()
    starter.join(2)
    # The old disable went out first, so the new capture's enable is the last word.
    assert dev.calls == ["enable", "disable", "enable"]
    polls = len(dev.masks)
    wait_until(lambda: len(dev.masks) > polls + 5)
    assert rec.pd_running
    rec.stop_pd()


def test_pd_capture_started_during_a_stream_keeps_the_stream_polled():
    dev = LockingDevice()
    rec = Recorder(lambda: dev, poll_interval_s=0.001)
    rec.start_stream(3)
    wait_until(lambda: len(dev.masks) >= 3)
    rec.start_pd()
    wait_until(lambda: dev.masks.count(ATT_ADCQUEUE | ATT_PD) >= 3)
    status = rec.stream_status()
    assert status["running"] and status["error"] is None
    assert dev.graph == [("start", 3)] and dev.calls == ["enable"]
    rec.stop_pd()
    rec.stop_stream()
    assert dev.graph == [("start", 3), ("stop",)]


@pytest.mark.parametrize("value, expected", [
    (None, 0.05), ("", 0.05), ("15", 0.015), ("1000", 1.0), ("2.5", 0.0025),
    ("0", 0.05), ("-5", 0.05), ("fast", 0.05), ("nan", 0.05),
    ("0.2", 0.001), ("60000", 10.0), ("inf", 10.0), ("1e308", 10.0),
])
def test_pd_poll_interval_from_env(monkeypatch, value, expected):
    from km003c_mcp.recorder import pd_poll_interval_s
    if value is None:
        monkeypatch.delenv("KM003C_PD_POLL_MS", raising=False)
    else:
        monkeypatch.setenv("KM003C_PD_POLL_MS", value)
    assert pd_poll_interval_s() == pytest.approx(expected)
    assert Recorder(lambda: FakeDevice())._poll == pytest.approx(expected)


def test_explicit_poll_interval_overrides_env(monkeypatch):
    monkeypatch.setenv("KM003C_PD_POLL_MS", "15")
    assert Recorder(lambda: FakeDevice(), poll_interval_s=0.2)._poll == 0.2


def test_long_pd_poll_interval_does_not_slow_a_stream():
    # A 50 SPS stream must still be drained every 50 ms when the PD interval is long.
    dev = FakeDevice()
    rec = Recorder(lambda: dev, poll_interval_s=5.0)
    rec.start_stream(2)
    time.sleep(0.3)
    rec.stop_stream()
    assert len(dev.masks) >= 4


def test_poller_failure_at_its_sleep_is_reported():
    # time.sleep(inf) raises OverflowError; it must end the capture visibly, not silently.
    dev = FakeDevice()
    rec = Recorder(lambda: dev, poll_interval_s=float("inf"))
    rec.start_pd()
    wait_until(lambda: not rec.pd_running)
    assert "OverflowError" in rec.pd_capture_state()["error"]
    assert dev.monitor == ["enable", "disable"]


class LeaseLog:
    """Records lease takes and releases, and what the device had received at each release."""

    def __init__(self, dev):
        self.dev = dev
        self.events = []

    def __call__(self, holder):
        self.events.append(("take", holder))
        log = self

        class _Lease:
            def release(self):
                log.events.append(("release", holder, list(log.dev.graph), list(log.dev.monitor)))
        return _Lease()


def test_stream_holds_a_lease_until_it_stops():
    dev = FakeDevice()
    leases = LeaseLog(dev)
    rec = Recorder(lambda: dev, poll_interval_s=0.001, lease=leases)
    rec.start_stream(3)
    assert leases.events == [("take", "stream")]
    rec.stop_stream()
    # The lease ends after StopGraph went out.
    assert leases.events[-1] == ("release", "stream", [("start", 3), ("stop",)], [])


def test_pd_capture_holds_a_lease_until_it_stops():
    dev = FakeDevice()
    leases = LeaseLog(dev)
    rec = Recorder(lambda: dev, poll_interval_s=0.001, lease=leases)
    rec.start_pd()
    assert leases.events == [("take", "pd capture")]
    rec.stop_pd()
    assert leases.events[-1] == ("release", "pd capture", [], ["enable", "disable"])
    rec.stop_pd()
    assert len(leases.events) == 2


def test_failed_start_returns_its_lease():
    dev = FakeDevice()
    dev.enable_result = PdMonitorNotEnabledError("never accepted")
    leases = LeaseLog(dev)
    rec = Recorder(lambda: dev, poll_interval_s=0.001, lease=leases)
    with pytest.raises(PdMonitorNotEnabledError):
        rec.start_pd()
    assert [e[0] for e in leases.events] == ["take", "release"]


def test_poller_failure_releases_lease_after_cleanup():
    dev = FakeDevice(error=Km003cError("bad reply"))
    leases = LeaseLog(dev)
    rec = Recorder(lambda: dev, poll_interval_s=0.001, lease=leases)
    rec.start_pd()
    wait_until(lambda: len(leases.events) == 2)
    # The capture's lease ended only after DisablePdMonitor went out.
    assert leases.events[1] == ("release", "pd capture", [], ["enable", "disable"])


def test_restart_during_failure_cleanup_moves_the_lease_to_the_new_capture():
    dev = LockingDevice()
    leases = LeaseLog(dev)
    rec = Recorder(lambda: dev, poll_interval_s=0.001, lease=leases)
    rec.start_pd()
    with dev.lock:
        dev.fail_next = True
        wait_until(lambda: not rec.pd_running)
        rec.start_pd()
    wait_until(lambda: len(leases.events) == 3)
    # The failed capture's lease ended; the new capture holds its own.
    assert [e[:2] for e in leases.events] == [
        ("take", "pd capture"), ("take", "pd capture"), ("release", "pd capture")]
    rec.stop_pd()
    assert [e[0] for e in leases.events].count("release") == 2


def test_stop_pd_releases_its_lease_when_the_link_fails():
    class BrokenDisable(FakeDevice):
        def disable_pd_monitor(self):
            raise TransportError("USB write failed")

    dev = BrokenDisable()
    leases = LeaseLog(dev)
    rec = Recorder(lambda: dev, poll_interval_s=0.001, lease=leases)
    rec.start_pd()
    with pytest.raises(TransportError):
        rec.stop_pd()
    assert [e[0] for e in leases.events] == ["take", "release"]


def test_stop_stream_releases_its_lease_when_the_link_fails():
    class BrokenStop(FakeDevice):
        def stop_graph(self):
            raise TransportError("USB write failed")

    dev = BrokenStop()
    leases = LeaseLog(dev)
    rec = Recorder(lambda: dev, poll_interval_s=0.001, lease=leases)
    rec.start_stream(3)
    with pytest.raises(TransportError):
        rec.stop_stream()
    assert [e[0] for e in leases.events] == ["take", "release"]


# --- stream and capture errors stay apart -------------------------------------------------

BAD_ADC = LogicalPacket(ATT_ADCQUEUE, True, 1, bytes(7))  # not a multiple of 20 bytes


def pd_reply():
    return LogicalPacket(ATT_PD, False, 0, PD_STREAM[:12])


def test_adc_parse_error_is_not_written_into_the_capture():
    dev = FakeDevice([[BAD_ADC, pd_reply()]] * 3)
    rec = Recorder(lambda: dev, poll_interval_s=0.001)
    rec.start_stream(3)
    rec.start_pd()
    wait_until(lambda: len(dev.masks) > 5)
    assert rec.pd_running and rec.stream_status()["running"]
    rec.stop_pd()
    rec.stop_stream()
    assert not any(r["type"] == "error" for r in records(rec.capture_path))


def test_repeated_adc_parse_errors_end_the_stream_not_the_capture():
    from km003c_mcp.recorder import MAX_CONSECUTIVE_PROTOCOL_ERRORS
    dev = FakeDevice([[BAD_ADC, pd_reply()]] * MAX_CONSECUTIVE_PROTOCOL_ERRORS)
    rec = Recorder(lambda: dev, poll_interval_s=0.001)
    rec.start_stream(3)
    rec.start_pd()
    wait_until(lambda: not rec.stream_status()["running"])
    assert "polls in a row failed to parse" in rec.stream_status()["error"]
    assert ("stop",) in dev.graph
    time.sleep(0.02)
    assert rec.pd_running and dev.monitor == ["enable"]
    rec.stop_pd()


def test_capture_file_failure_ends_the_capture_not_the_stream():
    dev = FakeDevice()
    rec = Recorder(lambda: dev, poll_interval_s=0.001)
    rec.start_stream(3)
    rec.start_pd()

    def full(record):
        raise OSError(28, "No space left on device")
    rec._pd.writer.write = full
    dev.replies = [[pd_reply()]]
    wait_until(lambda: not rec.pd_running)
    assert "No space left" in rec.pd_capture_state()["error"]
    assert dev.monitor == ["enable", "disable"]
    time.sleep(0.02)
    assert rec.stream_status()["running"] and dev.graph == [("start", 3)]
    rec.stop_stream()


def test_csv_failure_ends_the_stream_not_the_capture(tmp_path):
    dev = FakeDevice()
    rec = Recorder(lambda: dev, poll_interval_s=0.001)
    rec.start_stream(3, csv_path=str(tmp_path / "s.csv"))
    rec.start_pd()

    class FullCsv:
        def writerow(self, row):
            raise OSError(28, "No space left on device")
    rec._stream._csv = FullCsv()
    dev.replies = [queue(range(0, 5))]
    wait_until(lambda: not rec.stream_status()["running"])
    assert "No space left" in rec.stream_status()["error"]
    time.sleep(0.02)
    assert rec.pd_running and dev.monitor == ["enable"]
    rec.stop_pd()
    assert records(rec.capture_path)[-1]["reason"] == "manual"


def test_manual_stop_does_not_disable_a_newer_captures_monitor():
    class Dev(FakeDevice):
        """enable and disable take the device lock; polling does not."""

        def enable_pd_monitor(self):
            with self.lock:
                return super().enable_pd_monitor()

        def disable_pd_monitor(self):
            with self.lock:
                return super().disable_pd_monitor()

    dev = Dev()
    rec = Recorder(lambda: dev, poll_interval_s=0.001)
    rec.start_pd()
    first = rec.capture_path
    with dev.lock:
        stopper = threading.Thread(target=rec.stop_pd)
        stopper.start()
        wait_until(lambda: not rec.pd_running)
        time.sleep(0.05)  # the stop is now waiting for the device lock
        rec.start_pd()  # a new capture enables the monitor before the old stop disables it
    stopper.join(3)
    assert dev.monitor == ["enable", "enable"]  # the old stop's disable was skipped
    assert rec.pd_running and rec.capture_path != first
    assert records(first)[-1]["reason"] == "manual"
    rec.stop_pd()
    assert dev.monitor == ["enable", "enable", "disable"]


def test_wait_returns_after_a_manual_stop_has_disabled_the_monitor():
    release = threading.Event()

    class SlowDisable(FakeDevice):
        def disable_pd_monitor(self):
            release.wait(2)
            return super().disable_pd_monitor()

    dev = SlowDisable()
    rec = Recorder(lambda: dev, poll_interval_s=0.001)
    rec.start_pd()
    stopper = threading.Thread(target=rec.stop_pd)
    stopper.start()
    wait_until(lambda: not rec.pd_running)
    assert rec.wait_pd(0.05) is False  # the disable has not gone out yet
    release.set()
    assert rec.wait_pd(2) is True and dev.monitor == ["enable", "disable"]
    stopper.join(2)


# --- a failed start leaves nothing behind -------------------------------------------------

def test_header_failure_after_enable_closes_the_file_releases_and_disables(monkeypatch):
    from km003c_mcp.capture import CaptureSession
    dev = FakeDevice()
    leases = LeaseLog(dev)
    rec = Recorder(lambda: dev, poll_interval_s=0.001, lease=leases)
    sessions = []
    original = CaptureSession.__init__

    def remember(self, *a, **k):
        original(self, *a, **k)
        sessions.append(self)

    def broken_header(self, **kwargs):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(CaptureSession, "__init__", remember)
    monkeypatch.setattr(CaptureSession, "write_header", broken_header)
    with pytest.raises(OSError):
        rec.start_pd()
    assert sessions[0].writer.closed
    assert [e[0] for e in leases.events] == ["take", "release"]
    assert dev.monitor == ["enable", "disable"]
    assert not rec.pd_running


def test_lost_start_race_releases_its_lease_when_its_file_fails(monkeypatch):
    from km003c_mcp.capture import CaptureSession
    dev = FakeDevice()
    leases = LeaseLog(dev)
    rec = Recorder(lambda: dev, poll_interval_s=0.001, lease=leases)

    def enable_while_another_start_wins():
        dev.monitor.append("enable")
        rec._pd_active = True  # the other start finished first
        return ACCEPTED
    dev.enable_pd_monitor = enable_while_another_start_wins
    original_finish = CaptureSession.finish

    def failing_finish(self, *a, **k):
        original_finish(self, *a, **k)
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(CaptureSession, "finish", failing_finish)
    with pytest.raises(Km003cError, match="already running"):
        rec.start_pd()
    assert [e[0] for e in leases.events] == ["take", "release"]
    assert dev.monitor == ["enable"]  # the winner keeps the monitor
    rec._pd_active = False
