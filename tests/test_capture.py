"""Capture file writer, paths, timestamps, and the session's stop conditions."""

import struct
from datetime import datetime, timezone

import pytest

from capture_bench import S3, records
from km003c_mcp.capture import (
    CaptureSession, CaptureWriter, StopTracker, capture_dir,
    is_discover_identity_ack, new_capture_path,
)

NOW = datetime(2026, 11, 23, 8, 30, 5, tzinfo=timezone.utc)


def status(t_ms, vbus_mv=5000):
    return struct.pack("<IHhHH", t_ms, vbus_mv, 100, 1600, 0)


def conn(t_ms, code):
    return bytes([0x45]) + t_ms.to_bytes(4, "little") + bytes([code])


def message(t_ms, sop, wire):
    return bytes([0x80 | (len(wire) + 5)]) + struct.pack("<I", t_ms) + bytes([sop]) + wire


def session(tmp_path, name="c.jsonl", **kwargs):
    s = CaptureSession(CaptureWriter(tmp_path / name), **kwargs)
    s.write_header(meter={"serial": "X", "fw_version": "2.0.0", "hw_version": "2.3"},
                   poll_ms=50, pd_monitor={"enabled": True, "outcome": "accepted"},
                   label="t", provenance={"by": "test"})
    return s


# --- paths ----------------------------------------------------------------------------------

def test_capture_dir_from_env_else_cwd(monkeypatch, tmp_path):
    monkeypatch.setenv("KM003C_CAPTURE_DIR", str(tmp_path / "env"))
    assert capture_dir() == tmp_path / "env"
    monkeypatch.delenv("KM003C_CAPTURE_DIR")
    monkeypatch.chdir(tmp_path)
    assert capture_dir() == tmp_path / "captures"


def test_default_path_is_absolute_and_created(monkeypatch, tmp_path):
    monkeypatch.delenv("KM003C_CAPTURE_DIR")
    monkeypatch.chdir(tmp_path)
    p = new_capture_path(label="Apple 140W / cold", now=NOW)
    assert p.is_absolute() and p.parent.is_dir()
    assert p == (tmp_path / "captures" / "km003c-20261123T083005Z-Apple-140W-cold.jsonl").resolve()


def test_default_name_does_not_collide(tmp_path):
    first = new_capture_path(str(tmp_path) + "/", now=NOW)
    first.write_text("")
    assert new_capture_path(str(tmp_path), now=NOW).name == "km003c-20261123T083005Z-2.jsonl"


def test_explicit_file_path_creates_parents_and_refuses_existing(tmp_path):
    p = new_capture_path(str(tmp_path / "a" / "b" / "x.jsonl"))
    assert p.parent.is_dir() and not p.exists()
    p.write_text("")
    with pytest.raises(FileExistsError):
        new_capture_path(str(p))


# --- timestamps -----------------------------------------------------------------------------

def test_writer_keeps_32_bit_connection_times(tmp_path):
    s = session(tmp_path)
    # A real poll past 2^24 ms of uptime: status 40103699, connect at 40103694.
    s.ingest(bytes.fromhex("13ef6302020000001d060000") + bytes.fromhex("450eef630211"))
    s.finish("manual")
    (conn_record,) = [r for r in records(tmp_path / "c.jsonl") if r["type"] == "conn"]
    assert conn_record["t_meter_ms"] == 40103694 and conn_record["raw_event"] == "450eef630211"


# --- session --------------------------------------------------------------------------------

def test_status_interval_is_meter_time_and_event_polls_always_write(tmp_path):
    s = session(tmp_path, status_interval_ms=100)
    for t in (1000, 1050, 1099, 1100, 1150):
        s.ingest(status(t))
    s.ingest(status(1160) + conn(1155, 0x11))
    s.finish("manual")
    assert [r["t_meter_ms"] for r in records(tmp_path / "c.jsonl") if r["type"] == "status"] \
        == [1000, 1100, 1160]


def test_footer_vbus_covers_every_poll(tmp_path):
    s = session(tmp_path, status_interval_ms=10_000)
    for t, mv in ((1000, 5000), (1010, 20000), (1020, 9000)):
        s.ingest(status(t, mv))
    s.finish("manual")
    footer = records(tmp_path / "c.jsonl")[-1]
    assert footer["vbus_mv"] == {"first": 5000, "last": 9000, "min": 5000, "max": 20000}


def test_corrupt_payload_writes_an_error_and_keeps_earlier_records(tmp_path):
    s = session(tmp_path)
    good = message(2000, 0, bytes.fromhex("a303"))
    s.ingest(status(2001) + good + bytes.fromhex("8300"))  # truncated event header
    s.ingest(bytes(5))  # shorter than a status block
    s.ingest(status(2100) + message(2099, 0, bytes.fromhex("a605")))
    s.finish("manual")
    kinds = [(r["type"], r.get("msg")) for r in records(tmp_path / "c.jsonl")]
    assert kinds == [("header", None), ("status", None), ("pd", "Accept"), ("error", None),
                     ("error", None), ("status", None), ("pd", "PS_RDY"), ("footer", None)]
    assert records(tmp_path / "c.jsonl")[-1]["errors"] == 2


def test_finish_twice_and_error_footer(tmp_path):
    s = session(tmp_path)
    s.finish("error", ("TransportError", "USB gone"))
    s.finish("manual")
    recs = records(tmp_path / "c.jsonl")
    assert [r["type"] for r in recs] == ["header", "error", "footer"]
    assert recs[-1]["reason"] == "error" and recs[1]["message"] == "USB gone"
    with pytest.raises(ValueError):
        s.mark("after the end")


def test_timeout_uses_the_host_clock(tmp_path):
    now = [100.0]
    s = session(tmp_path, timeout_s=5, clock=lambda: now[0])
    assert s.check_timeout() is None
    now[0] = 105.0
    assert s.check_timeout() == "timeout"
    s.finish("timeout")


def test_session_replays_s3_to_contract(tmp_path):
    s = session(tmp_path, stop_on="contract")
    reason = None
    for payload in S3:
        reason = s.ingest(payload)
        if reason:
            break
    assert reason == "contract" and s.tracker.messages == 6


@pytest.mark.parametrize("bad", [
    {"stop_on": "pdo"}, {"stop_after_messages": 0}, {"settle_ms": -1}, {"timeout_s": 0},
    {"status_interval_ms": -5},
])
def test_session_rejects_bad_options(tmp_path, bad):
    with pytest.raises(ValueError):
        CaptureSession(CaptureWriter(tmp_path / "c.jsonl"), **bad)


# --- stop tracker ---------------------------------------------------------------------------

def test_contract_needs_request_accept_ps_rdy_in_order():
    t = StopTracker("contract")
    assert t.feed(0, b"", "PS_RDY") is None  # no Request yet
    assert t.feed(0, b"", "Request") is None
    assert t.feed(0, b"", "Reject") is None
    assert t.feed(0, b"", "Accept") is None  # the Accept of nothing
    assert t.feed(0, b"", "PS_RDY") is None
    assert t.feed(0, b"", "Request") is None
    assert t.feed(1, b"", "Accept") is None  # SOP' does not count
    assert t.feed(0, b"", "Accept") is None
    assert t.feed(0, b"", "PS_RDY") == "contract"


def test_identity_ack_only_on_sop():
    req = bytes.fromhex("af1701a000ff")
    cable_ack = bytes.fromhex("4f51418000ffc220001cba0000001003050052200821")
    board_ack = bytes.fromhex("8f4241a000ff510440000000000011090000")
    assert not is_discover_identity_ack(req) and is_discover_identity_ack(board_ack)
    t = StopTracker("identity")
    assert t.feed(0, req, "Vendor_Defined") is None
    assert t.feed(1, cable_ack, "Vendor_Defined") is None
    assert t.feed(0, board_ack, "Vendor_Defined") == "identity"


def test_stop_after_messages():
    t = StopTracker("none", stop_after_messages=2)
    assert t.feed(0, b"", "Accept") is None
    assert t.feed(1, b"", "Vendor_Defined") == "messages"


# --- retransmissions ------------------------------------------------------------------------

REQUEST = bytes.fromhex("82102cb18441")
GOODCRC_FROM_SOURCE = bytes.fromhex("6101")
ACCEPT = bytes.fromhex("a303")


def test_retransmission_counts_once_toward_stop_after_messages(tmp_path):
    s = session(tmp_path, stop_after_messages=2)
    # The Request goes out twice before its GoodCRC: one message, not two.
    assert s.ingest(status(1010) + message(1000, 0, REQUEST) + message(1001, 0, REQUEST)
                    + message(1002, 0, GOODCRC_FROM_SOURCE)) is None
    assert s.tracker.messages == 1
    assert s.ingest(status(1020) + message(1015, 0, ACCEPT)) == "messages"
    s.finish("messages")
    footer = records(tmp_path / "c.jsonl")[-1]
    assert footer["messages"] == 2 and footer["counts"]["Request"] == 2


def test_same_bytes_after_a_goodcrc_are_a_new_message():
    from km003c_mcp.capture import RetryFilter
    f = RetryFilter()
    assert f.is_retry(0, REQUEST, False) is False
    assert f.is_retry(0, REQUEST, False) is True
    assert f.is_retry(1, REQUEST, False) is False  # another SOP
    assert f.is_retry(0, GOODCRC_FROM_SOURCE, True) is False
    assert f.is_retry(0, REQUEST, False) is False
