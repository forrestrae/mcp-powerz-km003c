import threading

import pytest
import usb.core

import km003c_mcp.usb_device as ud
from fakes import FakeTransport, live_adc_with_tid, memory_read_replies
from km003c_mcp.crypto import ADDR_HARDWARE_ID
from km003c_mcp.usb_device import (
    DeviceBusyError, DeviceNotFoundError, DeviceTimeoutError, Km003cDevice, Km003cError,
    MeterLaggingError, PdMonitorNotEnabledError, PyUsbTransport,
)

HWID = bytes.fromhex("3037314b42500dff110affff")


def test_connect_retries_after_disconnect_reply():
    t = FakeTransport([bytes.fromhex("03010000"), bytes.fromhex("05020000")])
    Km003cDevice(t).connect()
    assert [w[0] for w in t.writes] == [0x02, 0x02]


def test_connect_timeouts_with_late_replies_mean_a_lagging_meter():
    timeout = DeviceTimeoutError("no reply")
    # Each read returns the reply to the previous Connect, so no attempt sees its own.
    t = FakeTransport([timeout, bytes.fromhex("05010000"), timeout,
                       bytes.fromhex("05020000"), timeout])
    with pytest.raises(MeterLaggingError, match="2 replies to earlier requests"):
        Km003cDevice(t).connect()


def test_connect_ignores_packets_that_are_not_late_connect_replies():
    timeout = DeviceTimeoutError("no reply")
    # The meter's Disconnect packet carries TID 0x11 whatever was sent; it is not lag.
    t = FakeTransport([timeout, bytes.fromhex("03110000"), timeout,
                       bytes.fromhex("41990000"), timeout])
    with pytest.raises(Km003cError, match="cannot connect") as info:
        Km003cDevice(t).connect()
    assert not isinstance(info.value, MeterLaggingError)


def test_connect_counts_only_replies_to_its_own_earlier_attempts():
    timeout = DeviceTimeoutError("no reply")
    dev = Km003cDevice(FakeTransport([bytes.fromhex("05010000")]))
    dev.connect()  # TID 1, accepted
    # A late reply to that earlier call's TID 1 is not a late reply to this call.
    dev.transport = FakeTransport([timeout, bytes.fromhex("05010000"), timeout, timeout])
    with pytest.raises(Km003cError) as info:
        dev.connect()
    assert not isinstance(info.value, MeterLaggingError)


def test_connect_timeouts_without_late_replies_are_a_plain_failure():
    with pytest.raises(Km003cError, match="cannot connect") as info:
        Km003cDevice(FakeTransport([])).connect()
    assert not isinstance(info.value, MeterLaggingError)


UNMATCHED = bytes.fromhex("41990000")


def test_disconnect_matches_reply_by_type():
    # Firmware 2.0.0 answers Disconnect with 03 11 00 00 whatever TID the request carried.
    t = FakeTransport([bytes.fromhex("03110000"), UNMATCHED])
    Km003cDevice(t).disconnect()
    assert t.writes == [bytes.fromhex("03010000")]
    assert t.script == [UNMATCHED]


def test_disconnect_accepts_same_tid_reply():
    t = FakeTransport([bytes.fromhex("05010000"), UNMATCHED])
    Km003cDevice(t).disconnect()
    assert t.script == [UNMATCHED]


def test_disconnect_ignores_timeout():
    t = FakeTransport([])
    Km003cDevice(t).disconnect()
    assert t.writes == [bytes.fromhex("03010000")]


@pytest.fixture
def no_retry_delay(monkeypatch):
    monkeypatch.setattr(ud, "PD_MONITOR_RETRY_DELAY_S", 0)


def test_enable_pd_monitor_answered_by_disconnect_then_accept(no_retry_delay):
    t = FakeTransport([bytes.fromhex("05010000"), bytes.fromhex("03020000"),
                       bytes.fromhex("05030000"), bytes.fromhex("05040000")])
    result = Km003cDevice(t).enable_pd_monitor()
    assert result.accepted and result.outcome == "accepted"
    assert result.attempts == 2 and result.last_reply == "05040000"
    assert t.writes == [bytes.fromhex("02010000"), bytes.fromhex("10020200"),
                        bytes.fromhex("02030000"), bytes.fromhex("10040200")]


def test_enable_pd_monitor_reject_is_not_fatal():
    t = FakeTransport([bytes.fromhex("05010000"), bytes.fromhex("06020000")])
    result = Km003cDevice(t).enable_pd_monitor()
    assert not result.accepted and result.outcome == "already enabled or refused"
    assert result.attempts == 1 and result.last_reply == "06020000"


def test_enable_pd_monitor_never_accepted(no_retry_delay):
    script = []
    for tid in range(1, 11, 2):
        script += [bytes([0x05, tid, 0, 0]), DeviceTimeoutError("no reply")]
    t = FakeTransport(script)
    with pytest.raises(PdMonitorNotEnabledError, match="5 attempts.*last reply none"):
        Km003cDevice(t).enable_pd_monitor()
    assert [w[0] for w in t.writes] == [0x02, 0x10] * 5


def test_enable_pd_monitor_never_accepted_reports_last_reply(no_retry_delay):
    script = []
    for tid in range(1, 11, 2):
        script += [bytes([0x05, tid, 0, 0]), bytes([0x03, tid + 1, 0, 0])]
    with pytest.raises(PdMonitorNotEnabledError, match="last reply 030a0000"):
        Km003cDevice(FakeTransport(script)).enable_pd_monitor()


class TimedTransport(FakeTransport):
    def __init__(self, script):
        super().__init__(script)
        self.read_timeouts = []

    def read(self, size, timeout_ms):
        self.read_timeouts.append(timeout_ms)
        return super().read(size, timeout_ms)


def test_enable_pd_monitor_bounds_how_long_it_holds_the_lock(monkeypatch):
    t = TimedTransport([])  # the meter never answers
    dev = Km003cDevice(t)
    free_between_attempts = []

    def sleep(_seconds):
        # Another thread, such as a stream's poller, must be able to take the lock here.
        got = []

        def probe():
            acquired = dev.lock.acquire(timeout=0.5)
            if acquired:
                dev.lock.release()
            got.append(acquired)
        thread = threading.Thread(target=probe)
        thread.start()
        thread.join()
        free_between_attempts.append(got[0])
    monkeypatch.setattr(ud.time, "sleep", sleep)
    with pytest.raises(PdMonitorNotEnabledError):
        dev.enable_pd_monitor()
    assert free_between_attempts == [True] * 4
    assert t.read_timeouts and max(t.read_timeouts) <= ud.PD_MONITOR_TIMEOUT_MS


def test_disable_pd_monitor():
    t = FakeTransport([bytes.fromhex("05010000"), bytes.fromhex("06020000")])
    dev = Km003cDevice(t)
    assert dev.disable_pd_monitor() is True
    assert dev.disable_pd_monitor() is False  # Reject: already disabled
    assert t.writes == [bytes.fromhex("11010000"), bytes.fromhex("11020000")]


def test_get_data_skips_stale_packets():
    t = FakeTransport([bytes.fromhex("05090000"), live_adc_with_tid(1)])
    r = Km003cDevice(t).read_adc()
    assert r.vdd_v == pytest.approx(3.2504)
    assert t.writes == [bytes.fromhex("0c010200")]


def test_memory_read_flow():
    plain = bytes(range(20))
    t = FakeTransport(memory_read_replies(1, 0x420, plain))
    assert Km003cDevice(t).memory_read(0x420, 20) == plain
    assert t.writes[0][:4] == bytes.fromhex("44010101")


def test_memory_read_not_readable():
    with pytest.raises(Km003cError, match="not readable"):
        Km003cDevice(FakeTransport([bytes.fromhex("27010000")])).memory_read(0x0, 16)


def test_memory_read_refused_while_streaming():
    dev = Km003cDevice(FakeTransport([]))
    dev.graph_rate = 3
    with pytest.raises(Km003cError, match="stream"):
        dev.memory_read(0x420, 16)


def test_authenticate_accepts_tid_zero_response():
    t = FakeTransport(memory_read_replies(1, ADDR_HARDWARE_ID, HWID)
                      + [bytes.fromhex("4c000302") + bytes(32)])
    dev = Km003cDevice(t)
    assert dev.authenticate() == 1 and dev.auth_level == 1
    assert t.writes[1][:4] == bytes.fromhex("4c020002")


def test_start_graph_fails_when_auth_level_zero():
    t = FakeTransport(memory_read_replies(1, ADDR_HARDWARE_ID, HWID)
                      + [bytes.fromhex("4c000102") + bytes(32)])
    with pytest.raises(Km003cError, match="streaming auth failed"):
        Km003cDevice(t).start_graph(3)


def test_start_and_stop_graph():
    t = FakeTransport([bytes.fromhex("05010000"), bytes.fromhex("06020000")])
    dev = Km003cDevice(t)
    dev.auth_level = 1
    dev.start_graph(3)
    assert dev.graph_rate == 3 and t.writes[0] == bytes.fromhex("0e010600")
    dev.stop_graph()  # Reject means "already stopped"
    assert dev.graph_rate is None


def test_timeout_when_no_reply():
    with pytest.raises(DeviceTimeoutError):
        Km003cDevice(FakeTransport([])).read_adc()


class StubUsb:
    def __init__(self, serial, fail=None):
        self.serial_number = serial
        self.fail = fail

    def set_configuration(self):
        if self.fail:
            raise self.fail


@pytest.fixture
def disposed(monkeypatch):
    """Records the devices whose handles open() closes."""
    devices = []
    monkeypatch.setattr(usb.util, "dispose_resources", devices.append)
    return devices


class ResettableUsb:
    def __init__(self):
        self.calls = []

    def reset(self):
        self.calls.append("reset")


def test_transport_reset_resets_and_disposes(disposed):
    dev = ResettableUsb()
    PyUsbTransport(dev, "A").reset()
    assert dev.calls == ["reset"] and disposed == [dev]


def test_open_busy_maps_to_device_busy(monkeypatch, disposed):
    err = usb.core.USBError("Access denied (insufficient permissions)", errno=13)
    stub = StubUsb("A", fail=err)
    monkeypatch.setattr(ud, "_find_devices", lambda: [stub])
    with pytest.raises(DeviceBusyError, match="POWER-Z PC software"):
        PyUsbTransport.open(holder_lookup=lambda serial: None)
    assert disposed == [stub]  # the failed attempt's handle is closed


ENODEV = usb.core.USBError("No such device (it may have been disconnected)", errno=19)


def test_busy_error_names_the_holder(monkeypatch, disposed):
    # macOS reports a held interface as errno 19; ioreg names the process holding it.
    stub = StubUsb("000000", fail=ENODEV)
    monkeypatch.setattr(ud, "_find_devices", lambda: [stub])
    asked = []

    def lookup(serial):
        asked.append(serial)
        return "pid 46945, python3.14"

    with pytest.raises(DeviceBusyError) as info:
        PyUsbTransport.open(holder_lookup=lookup)
    msg = str(info.value)
    assert asked == ["000000"]
    assert msg.startswith("the KM003C 000000 is held by another process (pid 46945, python3.14)")
    assert "No such device" in msg
    assert "km003c MCP server" in msg and "POWER-Z PC software" in msg and "script" in msg
    assert isinstance(info.value.__cause__, usb.core.USBError)
    assert disposed == [stub]


def test_busy_error_names_this_process_when_it_holds_the_meter(monkeypatch, disposed):
    monkeypatch.setattr(ud, "_find_devices", lambda: [StubUsb("000000", fail=ENODEV)])
    me = f"pid {ud.os.getpid()}, python3.14"
    with pytest.raises(DeviceBusyError) as info:
        PyUsbTransport.open(holder_lookup=lambda serial: me)
    msg = str(info.value)
    assert f"held by this same process ({me})" in msg and "was not released" in msg
    assert "another process" not in msg


def test_busy_error_own_pid_is_injectable():
    err = ud.busy_error(ENODEV, "A", "pid 42, python3.14", own_pid=42)
    assert "this same process" in str(err)
    err = ud.busy_error(ENODEV, "A", "pid 42, python3.14", own_pid=43)
    assert "another process (pid 42, python3.14)" in str(err)


@pytest.mark.parametrize("failure", [
    usb.core.USBError("Input/Output Error", errno=5),
    usb.core.USBError("Other error"),
    NotImplementedError("Operation not supported or unimplemented on this platform"),
])
def test_other_claim_errors_are_busy_without_a_holder_lookup(monkeypatch, disposed, failure):
    stub = StubUsb("A", fail=failure)
    monkeypatch.setattr(ud, "_find_devices", lambda: [stub])
    looked_up = []
    with pytest.raises(DeviceBusyError) as info:
        PyUsbTransport.open(holder_lookup=looked_up.append)
    msg = str(info.value)
    assert msg.startswith(f"the USB interface of the KM003C A could not be claimed ({failure})")
    assert "likely cause is another program holding the meter" in msg
    assert "POWER-Z PC software" in msg and "held by another process" not in msg
    assert looked_up == [] and disposed == [stub]


def test_busy_error_without_a_holder(monkeypatch, disposed):
    monkeypatch.setattr(ud, "_find_devices", lambda: [StubUsb("000000", fail=ENODEV)])
    with pytest.raises(DeviceBusyError) as info:
        PyUsbTransport.open(holder_lookup=lambda serial: None)
    msg = str(info.value)
    assert msg.startswith("the KM003C 000000 is held by another process: ")
    assert "pid" not in msg and "Likely holders" in msg


def test_busy_error_on_claim_interface(monkeypatch, disposed):
    err = usb.core.USBError("Resource busy", errno=16)
    monkeypatch.setattr(ud, "_find_devices", lambda: [StubUsb("A")])

    def claim(dev, intf):
        raise err
    monkeypatch.setattr(usb.util, "claim_interface", claim)
    with pytest.raises(DeviceBusyError, match="held by another process: .*Resource busy"):
        PyUsbTransport.open(holder_lookup=lambda serial: None)


# Trimmed from `ioreg -l -w0` on macOS with a Python process holding the meter.
IOREG = """\
+-o Root  <class IORegistryEntry, id 0x100000100, retain 40>
  | +-o Camera@1  <class IOUSBHostInterface, id 0x100006000, registered, matched>
  | | {
  | |   "idVendor" = 1452
  | |   "idProduct" = 34064
  | |   "bInterfaceNumber" = 0
  | |   "UsbExclusiveOwner" = "pid 559, UVCAssistant"
  | | }
  | +-o POWER-Z KM003C@20110000  <class IOUSBHostDevice, id 0x100006043, registered>
  | | {
  | |   "idProduct" = 99
  | |   "kUSBSerialNumberString" = "000000"
  | |   "UsbExclusiveOwner" = "pid 46945, python3.14"
  | |   "idVendor" = 24521
  | |   "USB Serial Number" = "000000"
  | | }
  | |   +-o APP@0  <class IOUSBHostInterface, id 0x100006049, registered, matched>
  | |   | {
  | |   |   "idProduct" = 99
  | |   |   "UsbExclusiveOwner" = "pid 46945, python3.14"
  | |   |   "bInterfaceNumber" = 0
  | |   |   "idVendor" = 24521
  | |   |   "USB Serial Number" = "000000"
  | |   | }
  | |   +-o APP@1  <class IOUSBHostInterface, id 0x10000604d, registered, matched>
  | |   | {
  | |   |   "idProduct" = 99
  | |   |   "UsbExclusiveOwner" = "AppleUSBACMControl"
  | |   |   "bInterfaceNumber" = 1
  | |   |   "idVendor" = 24521
  | |   |   "USB Serial Number" = "000000"
  | |   | }
"""


def test_parse_exclusive_owner_reads_interface_zero():
    assert ud.parse_exclusive_owner(IOREG) == "pid 46945, python3.14"
    assert ud.parse_exclusive_owner(IOREG, "000000") == "pid 46945, python3.14"


def test_parse_exclusive_owner_other_serial_or_free_meter():
    assert ud.parse_exclusive_owner(IOREG, "014270") is None
    free = IOREG.replace('"UsbExclusiveOwner" = "pid 46945, python3.14"', '"Other" = 1')
    assert ud.parse_exclusive_owner(free) is None


def test_parse_exclusive_owner_falls_back_to_device_node():
    only_device = IOREG.replace(
        '|   |   "UsbExclusiveOwner" = "pid 46945, python3.14"', '|   |   "Other" = 1')
    assert ud.parse_exclusive_owner(only_device) == "pid 46945, python3.14"


def test_ioreg_lookup_skips_other_platforms_and_failures(monkeypatch):
    monkeypatch.setattr(ud.sys, "platform", "linux")
    assert ud.ioreg_exclusive_owner() is None
    monkeypatch.setattr(ud.sys, "platform", "darwin")

    def fail(*args, **kwargs):
        raise FileNotFoundError("ioreg")
    monkeypatch.setattr(ud.subprocess, "run", fail)
    assert ud.ioreg_exclusive_owner() is None


def test_ioreg_runs_by_absolute_path(monkeypatch):
    monkeypatch.setattr(ud.sys, "platform", "darwin")
    calls = []

    def run(args, **kwargs):
        calls.append(args[0])
        raise FileNotFoundError(args[0])
    monkeypatch.setattr(ud.subprocess, "run", run)
    monkeypatch.setattr(ud.os.path, "exists", lambda path: path == "/usr/sbin/ioreg")
    ud.ioreg_exclusive_owner()
    monkeypatch.setattr(ud.os.path, "exists", lambda path: False)
    ud.ioreg_exclusive_owner()
    assert calls == ["/usr/sbin/ioreg", "ioreg"]


def test_open_no_device(monkeypatch):
    monkeypatch.setattr(ud, "_find_devices", lambda: [])
    with pytest.raises(DeviceNotFoundError):
        PyUsbTransport.open()


def test_open_multiple_devices_requires_serial(monkeypatch):
    monkeypatch.setattr(ud, "_find_devices", lambda: [StubUsb("A"), StubUsb("B")])
    monkeypatch.setattr(usb.util, "claim_interface", lambda dev, intf: None)
    with pytest.raises(Km003cError, match="A.*B"):
        PyUsbTransport.open()
    assert PyUsbTransport.open("B").serial_number == "B"


class BackendCalls:
    """Records which libusb lookups _backend() makes; each returns its scripted result."""

    def __init__(self, monkeypatch, bundled=None, system=None, explicit=None):
        import libusb_package
        import usb.backend.libusb1

        self.calls = []

        def get_backend(find_library=None):
            if find_library is None:
                self.calls.append("system")
                return system
            self.calls.append(("explicit", find_library("usb-1.0")))
            return explicit

        def get_bundled():
            self.calls.append("bundled")
            return bundled

        monkeypatch.setattr(usb.backend.libusb1, "get_backend", get_backend)
        monkeypatch.setattr(libusb_package, "get_libusb1_backend", get_bundled)
        monkeypatch.delenv("KM003C_LIBUSB", raising=False)
        monkeypatch.delenv("KM003C_LIBUSB_DLL", raising=False)


def test_backend_prefers_explicit_path(monkeypatch):
    calls = BackendCalls(monkeypatch, bundled="B", explicit="E")
    monkeypatch.setenv("KM003C_LIBUSB", "/opt/lib/libusb-1.0.dylib")
    assert ud._backend() == "E"
    assert calls.calls == [("explicit", "/opt/lib/libusb-1.0.dylib")]


def test_backend_accepts_dll_alias(monkeypatch):
    calls = BackendCalls(monkeypatch, explicit="E")
    monkeypatch.setenv("KM003C_LIBUSB_DLL", r"C:\libusb-1.0.dll")
    assert ud._backend() == "E"
    assert calls.calls == [("explicit", r"C:\libusb-1.0.dll")]


def test_backend_explicit_path_that_fails_raises(monkeypatch):
    BackendCalls(monkeypatch, bundled="B", system="S", explicit=None)
    monkeypatch.setenv("KM003C_LIBUSB", "/missing/libusb-1.0.dylib")
    with pytest.raises(Km003cError, match="/missing/libusb-1.0.dylib"):
        ud._backend()


def test_backend_uses_bundled_library(monkeypatch):
    calls = BackendCalls(monkeypatch, bundled="B", system="S")
    assert ud._backend() == "B"
    assert calls.calls == ["bundled"]


def test_backend_falls_back_to_system_library(monkeypatch):
    calls = BackendCalls(monkeypatch, bundled=None, system="S")
    assert ud._backend() == "S"
    assert calls.calls == ["bundled", "system"]


def test_backend_reports_missing_libusb(monkeypatch):
    BackendCalls(monkeypatch)
    with pytest.raises(Km003cError, match="KM003C_LIBUSB"):
        ud._backend()
