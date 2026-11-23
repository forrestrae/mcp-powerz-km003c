import asyncio

import pytest

import km003c_mcp.server as srv
from fakes import FakeSerial, FakeTransport, live_adc_with_tid, meter_info_replies
from km003c_mcp.server import Km003cService, build_tools
from km003c_mcp.trigger import TriggerPort
from km003c_mcp.usb_device import DeviceBusyError, DeviceNotFoundError, DeviceTimeoutError

ACCEPT_1 = bytes.fromhex("05010000")


def opened(*replies):
    """Session-open replies (Connect accept, StopGraph reject) followed by later replies."""
    return [ACCEPT_1, bytes.fromhex("06020000"), *replies]


def make_tools(script=None, serial_replies=None, factory=None):
    fs = FakeSerial(serial_replies or {})
    svc = Km003cService(
        transport_factory=factory or (lambda: FakeTransport(script or [])),
        trigger_factory=lambda port: TriggerPort(port, serial_factory=lambda *a, **k: fs),
        port_finder=lambda serial_number=None, override=None: "COMX",
    )
    tools = build_tools(svc, enable_trigger=True)
    tools._service = svc  # test-only handle
    return tools, fs


def test_measure_single():
    tools, _ = make_tools(opened(live_adc_with_tid(3)))
    r = tools.measure()
    assert r["vdd_v"] == pytest.approx(3.2504) and r["temp_c"] is None


def test_measure_average():
    tools, _ = make_tools(opened(*[live_adc_with_tid(t) for t in (3, 4, 5)]))
    r = tools.measure(samples=3, interval_ms=0)
    assert r["samples"] == 3
    assert r["vbus_v"]["mean"] == pytest.approx(0.002048)
    assert set(r["power_w"]) == {"mean", "min", "max"}


def test_measure_rejects_bad_sample_count():
    tools, _ = make_tools(opened())
    assert tools.measure(samples=0)["kind"] == "ValueError"


def test_busy_device_returns_error_dict():
    def busy():
        raise DeviceBusyError("close the POWER-Z PC software")
    tools, _ = make_tools(factory=busy)
    r = tools.measure()
    assert r["kind"] == "DeviceBusyError" and "POWER-Z PC software" in r["error"]


def test_reopens_after_error():
    calls = []

    def factory():
        calls.append(1)
        if len(calls) == 1:
            raise DeviceNotFoundError("not plugged in")
        return FakeTransport(opened(live_adc_with_tid(3)))
    tools, _ = make_tools(factory=factory)
    assert tools.measure()["kind"] == "DeviceNotFoundError"
    assert "vbus_v" in tools.measure()


def test_reopens_after_transport_timeout():
    transports = [FakeTransport(opened()), FakeTransport(opened(live_adc_with_tid(3)))]
    tools, _ = make_tools(factory=lambda: transports.pop(0))
    assert tools.measure()["kind"] == "DeviceTimeoutError"
    assert "vbus_v" in tools.measure()


def test_stream_rejects_unknown_rate():
    tools, _ = make_tools(opened())
    assert tools.stream_start(rate_sps=100)["kind"] == "ValueError"


def test_trigger_fast_charge_opens_pdm_first():
    tools, fs = make_tools(factory=lambda: (_ for _ in ()).throw(DeviceNotFoundError("x")),
                           serial_replies={"pdm open": [b"pdm mode entry\n"], "qc 9V": [b"ok\n"]})
    r = tools.trigger_fast_charge("qc", 9000)
    assert fs.written[:2] == [b"pdm open\r\n", b"qc 9V\r\n"]
    assert r == {"command": "qc 9V", "lines": ["ok"]}


def test_trigger_fast_charge_invalid_returns_error():
    tools, _ = make_tools()
    assert "error" in tools.trigger_fast_charge("qc", 7000)


def test_trigger_pd_request_builds_command():
    tools, fs = make_tools(serial_replies={"pdm open": [b"pdm mode entry\n"]})
    assert tools.trigger_pd_request(position=5, voltage_mv=12000, current_ma=2000)["command"] == \
        "pd req=5,volt=12000,cur=2000"


def test_poller_transport_error_reopens_on_next_call():
    # The meter info reads (TIDs 3 to 6), Connect (TID 7) and EnablePdMonitor (TID 8) are
    # answered; the first poll then times out.
    transports = [FakeTransport(opened(*meter_info_replies(3), bytes.fromhex("05070000"),
                                       bytes.fromhex("05080000"))),
                  FakeTransport(opened(live_adc_with_tid(3)))]
    tools, _ = make_tools(factory=lambda: transports.pop(0))
    started = tools.pd_capture_start()
    assert started["started"] and started["path"].endswith(".jsonl")
    assert started["pd_monitor"] == {"accepted": True, "outcome": "accepted", "attempts": 1,
                                     "last_reply": "05080000"}
    assert tools._service.recorder.wait_pd(3)
    summary = tools.pd_capture_read()
    assert summary["reason"] == "error" and summary["running"] is False
    assert any("DeviceTimeoutError" in w for w in summary["warnings"])
    assert "vbus_v" in tools.measure()


def test_stream_start_bad_csv_returns_error(tmp_path):
    tools, _ = make_tools(opened())
    r = tools.stream_start(rate_sps=1000, csv_path=str(tmp_path / "missing" / "x.csv"))
    assert r["kind"] in ("FileNotFoundError", "OSError")


def test_invalidate_trigger_closes_serial_port():
    tools, fs = make_tools(serial_replies={"pdm open": [b"pdm mode entry\n"]})
    tools.trigger_raw("pd pdo", timeout_s=0.1)
    tools._service.invalidate_trigger()
    assert fs.closed


def test_trigger_close_sends_pdm_close():
    tools, fs = make_tools(serial_replies={"pdm open": [b"pdm mode entry\n"],
                                           "pdm close": [b"ok\n"]})
    tools.trigger_raw("pd pdo", timeout_s=0.1)
    assert tools.trigger_close() == {"command": "pdm close", "lines": ["ok"]}
    assert fs.closed


def test_pd_capture_start_reports_reject_as_non_fatal():
    tools, _ = make_tools(opened(*meter_info_replies(3), bytes.fromhex("05070000"),
                                 bytes.fromhex("06080000")))
    r = tools.pd_capture_start()
    assert r["started"] and r["pd_monitor"]["outcome"] == "already enabled or refused"
    tools._service.recorder.abort("test done")


def test_pd_capture_start_fails_when_monitor_never_enabled(monkeypatch):
    import km003c_mcp.usb_device as ud
    monkeypatch.setattr(ud, "PD_MONITOR_RETRY_DELAY_S", 0)
    replies = []
    for tid in range(7, 17, 2):
        replies += [bytes([0x05, tid, 0, 0]), bytes([0x03, tid + 1, 0, 0])]
    tools, _ = make_tools(opened(*meter_info_replies(3), *replies))
    r = tools.pd_capture_start()
    assert r["kind"] == "PdMonitorNotEnabledError"
    assert tools._service.recorder.pd_running is False
    # The file still records the attempt: header, the error, and a footer.
    summary = tools.pd_capture_read()
    assert summary["reason"] == "error" and summary["meter_fw"] == "2.0.0"
    assert any("PdMonitorNotEnabledError" in w for w in summary["warnings"])


def lagging_transport():
    """A meter answering one request late: each Connect reads the previous Connect's reply."""
    timeout = DeviceTimeoutError("no reply")
    return FakeTransport([timeout, bytes.fromhex("05010000"), timeout,
                          bytes.fromhex("05020000"), timeout])


def test_lagging_meter_is_reset_once_and_reopened(monkeypatch):
    monkeypatch.setattr(srv, "RESET_REOPEN_DELAY_S", 0)
    lagging, healthy = lagging_transport(), FakeTransport(opened(live_adc_with_tid(3)))
    transports = [lagging, healthy]
    tools, _ = make_tools(factory=lambda: transports.pop(0))
    assert "vbus_v" in tools.measure()
    assert lagging.resets == 1 and healthy.resets == 0 and transports == []


def test_lagging_meter_after_reset_gives_a_clear_error(monkeypatch):
    monkeypatch.setattr(srv, "RESET_REOPEN_DELAY_S", 0)
    opened_transports = [lagging_transport(), lagging_transport()]
    transports = list(opened_transports)
    tools, _ = make_tools(factory=lambda: transports.pop(0))
    r = tools.measure()
    assert r["kind"] == "Km003cError" and "after a USB reset" in r["error"]
    assert [t.resets for t in opened_transports] == [1, 0] and opened_transports[1].closed


def test_reopen_after_reset_waits_for_the_meter(monkeypatch):
    monkeypatch.setattr(srv, "RESET_REOPEN_DELAY_S", 0)
    attempts = [lagging_transport(), DeviceNotFoundError("re-enumerating"),
                FakeTransport(opened(live_adc_with_tid(3)))]

    def factory():
        item = attempts.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
    tools, _ = make_tools(factory=factory)
    assert "vbus_v" in tools.measure() and attempts == []


def test_plain_connect_failure_does_not_reset():
    silent = FakeTransport([])
    tools, _ = make_tools(factory=lambda: silent)
    r = tools.measure()
    assert r["kind"] == "Km003cError" and "cannot connect" in r["error"]
    assert silent.resets == 0 and silent.closed


def test_reopen_after_reset_says_the_meter_did_not_come_back(monkeypatch):
    monkeypatch.setattr(srv, "RESET_REOPEN_DELAY_S", 0)
    attempts = [lagging_transport()]

    def factory():
        if attempts:
            return attempts.pop(0)
        raise DeviceNotFoundError("no POWER-Z KM003C found")
    tools, _ = make_tools(factory=factory)
    r = tools.measure()
    assert r["kind"] == "Km003cError" and "did not come back" in r["error"]
    assert "answers late" not in r["error"]


def test_reopen_after_reset_skips_the_holder_lookup(monkeypatch):
    monkeypatch.setattr(srv, "RESET_REOPEN_DELAY_S", 0)
    lookups = []
    transports = [lagging_transport(), FakeTransport(opened(live_adc_with_tid(3)))]

    def factory(holder_lookup=None):
        lookups.append(holder_lookup)
        return transports.pop(0)
    tools, _ = make_tools(factory=factory)
    assert "vbus_v" in tools.measure()
    assert lookups[0] is None and lookups[1] is not None and lookups[1]("000000") is None


def registered_tools(enable_trigger=None):
    tools = build_tools(Km003cService(transport_factory=lambda: FakeTransport([])),
                        enable_trigger=enable_trigger)
    return {tool.name for tool in asyncio.run(srv.build_server(tools).list_tools())}


@pytest.mark.parametrize("value, present", [("1", True), ("0", False)])
def test_trigger_flag_registers_or_hides_every_trigger_tool(monkeypatch, value, present):
    monkeypatch.setenv("KM003C_ENABLE_TRIGGER", value)
    names = registered_tools()
    assert all((name in names) == present for name in srv.TRIGGER_TOOLS)
    assert {"measure", "pd_capture_start", "device_release"} <= names


def test_trigger_tools_follow_the_default_when_unset(monkeypatch):
    monkeypatch.delenv("KM003C_ENABLE_TRIGGER", raising=False)
    monkeypatch.setattr(srv, "DEFAULT_ENABLE_TRIGGER", True)
    assert set(srv.TRIGGER_TOOLS) <= registered_tools()
    monkeypatch.setattr(srv, "DEFAULT_ENABLE_TRIGGER", False)
    assert not set(srv.TRIGGER_TOOLS) & registered_tools()


def test_upstream_default_registers_trigger_tools(monkeypatch):
    monkeypatch.delenv("KM003C_ENABLE_TRIGGER", raising=False)
    assert srv.DEFAULT_ENABLE_TRIGGER is True
    assert set(srv.TRIGGER_TOOLS) <= registered_tools()


@pytest.mark.parametrize("value", ["false", "no", "off", "yes", "true", "2", " 0 "])
def test_any_trigger_flag_but_1_hides_the_tools(monkeypatch, value):
    # A value meant as "off" must never fall back to a default that registers the tools.
    monkeypatch.setenv("KM003C_ENABLE_TRIGGER", value)
    monkeypatch.setattr(srv, "DEFAULT_ENABLE_TRIGGER", True)
    assert srv.trigger_enabled_from_env() is False
    assert not set(srv.TRIGGER_TOOLS) & registered_tools()


def test_trigger_tools_list_matches_the_tools():
    names = set(vars(build_tools(Km003cService(), enable_trigger=True)))
    assert {name for name in names if name.startswith("trigger_")} == set(srv.TRIGGER_TOOLS)
