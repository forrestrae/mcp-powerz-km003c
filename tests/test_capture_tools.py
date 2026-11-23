"""PD capture tools end to end: real meter payloads through the fake transport into files.

Set KM003C_UPDATE_GOLDEN=1 to rewrite tests/fixtures/s3_golden.jsonl after a deliberate
format change, then review the diff.
"""

import json
import os
import time

import pytest

from capture_bench import GOLDEN, S3, CaptureBench, normalize, records, s3_until
from fakes import CORRUPT_PUTDATA, FakeTransport, live_adc_with_tid
from km003c_mcp.protocol import ATT_ADC, ATT_PD, CMD_PUT_DATA
from km003c_mcp.recorder import MAX_CONSECUTIVE_PROTOCOL_ERRORS
from km003c_mcp.server import Km003cService, build_tools


@pytest.fixture
def bench_factory(monkeypatch):
    benches = []

    def make(payloads=(), **kwargs):
        bench = CaptureBench(monkeypatch, payloads, **kwargs)
        benches.append(bench)
        return bench
    yield make
    for bench in benches:
        bench.close()


def non_goodcrc(recs):
    return [r for r in recs if r["type"] == "pd" and r["msg"] != "GoodCRC"]


def test_golden_jsonl_from_real_payloads(bench_factory, tmp_path):
    bench = bench_factory(S3)
    started = bench.tools.pd_capture_start(
        path=str(tmp_path / "s3.jsonl"), label="S3 replay",
        provenance={"source": "Phase 1 spike S3, firmware 2.0.0"}, stop_after_messages=8)
    assert started["started"] and started["path"] == str((tmp_path / "s3.jsonl").resolve())
    summary = bench.tools.pd_capture_wait(timeout_s=20)
    assert summary["reason"] == "messages" and summary["running"] is False
    produced = [normalize(r) for r in records(started["path"])]
    if os.environ.get("KM003C_UPDATE_GOLDEN") == "1":
        GOLDEN.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in produced))
    assert produced == records(GOLDEN)


def test_golden_header_and_footer():
    recs = records(GOLDEN)
    header, footer = recs[0], recs[-1]
    assert header["format"] == "km003c-capture" and header["version"] == 1
    assert header["meter"] == {"serial": "000000", "fw_version": "2.0.0", "hw_version": "2.3",
                               "model": "KM003C"}
    assert header["pd_monitor"]["enabled"] is True and header["pd_monitor"]["attempts"] == 1
    assert footer["type"] == "footer" and footer["reason"] == "messages"
    assert footer["messages"] == 8 and footer["counts"]["GoodCRC"] == 8
    assert footer["vbus_mv"]["max"] > 20000
    # Legacy connection codes kept raw, with the attach bounce of firmware 2.0.0.
    conn = [r for r in recs if r["type"] == "conn"]
    assert [(r["code"], r["event"]) for r in conn] == [
        (0x11, "connect"), (0x12, "disconnect"), (0x11, "connect")]
    assert conn[0]["raw_event"] == "45cb57df0011"
    request = next(r for r in recs if r.get("msg") == "Request")
    assert request["wire"] == "82102cb18441" and request["raw_event"] == "8bfa59df000082102cb18441"
    assert (request["power_role"], request["data_role"], request["msg_id"]) == ("sink", "ufp", 0)
    assert "objects" not in request and "pdos" not in request  # no full decode in the file


def test_stop_on_contract(bench_factory):
    bench = bench_factory(S3)
    path = bench.tools.pd_capture_start(stop_on="contract")["path"]
    assert bench.tools.pd_capture_wait(timeout_s=10)["reason"] == "contract"
    msgs = non_goodcrc(records(path))
    assert msgs[-1]["msg"] == "PS_RDY"
    assert not any(r["msg"] == "Vendor_Defined" and r["sop"] == "SOP" for r in msgs)
    assert not bench.service.claimed  # the capture's lease ended with it


def test_stop_on_contract_with_settle_keeps_capturing(bench_factory):
    bench = bench_factory(S3)
    path = bench.tools.pd_capture_start(stop_on="contract", settle_ms=3200)["path"]
    assert bench.tools.pd_capture_wait(timeout_s=10)["reason"] == "contract"
    recs = records(path)
    ps_rdy = next(r for r in recs if r.get("msg") == "PS_RDY")
    # The charger's Discover Identity came 3.0 s after PS_RDY; the settle time caught it.
    assert any(r.get("msg") == "Vendor_Defined" and r["sop"] == "SOP" for r in recs)
    last_status = [r for r in recs if r["type"] == "status"][-1]
    assert last_status["t_meter_ms"] >= ps_rdy["t_meter_ms"] + 3200


def test_stop_on_identity(bench_factory):
    bench = bench_factory(S3)
    path = bench.tools.pd_capture_start(stop_on="identity")["path"]
    assert bench.tools.pd_capture_wait(timeout_s=10)["reason"] == "identity"
    msgs = non_goodcrc(records(path))
    # The SOP' cable ACK does not count; the ACK on SOP does.
    assert msgs[-1]["sop"] == "SOP" and msgs[-1]["wire"].startswith("8f4241a000ff")


def test_stop_after_messages_counts_non_goodcrc(bench_factory):
    bench = bench_factory(S3)
    path = bench.tools.pd_capture_start(stop_after_messages=3)["path"]
    assert bench.tools.pd_capture_wait(timeout_s=10)["reason"] == "messages"
    recs = records(path)
    assert len(non_goodcrc(recs)) == 3 and recs[-1]["messages"] == 3


def test_timeout_when_the_condition_is_not_met(bench_factory):
    bench = bench_factory(s3_until("82102cb18441"))  # stops before Accept and PS_RDY
    bench.tools.pd_capture_start(stop_on="contract", timeout_s=0.3)
    summary = bench.tools.pd_capture_wait(timeout_s=5)
    assert summary["reason"] == "timeout"
    assert any("'contract' not met" in w for w in summary["warnings"])


def test_timeout_during_settle_keeps_the_condition_as_reason(bench_factory):
    bench = bench_factory(S3)
    bench.tools.pd_capture_start(stop_on="contract", settle_ms=10_000_000, timeout_s=0.5)
    assert bench.tools.pd_capture_wait(timeout_s=5)["reason"] == "contract"


def test_wait_returns_while_running_and_mark_and_manual_stop(bench_factory):
    bench = bench_factory(S3)
    path = bench.tools.pd_capture_start()["path"]
    early = bench.tools.pd_capture_wait(timeout_s=0.05)
    assert early["running"] is True and early["reason"] is None
    assert bench.tools.pd_capture_mark("stimulus GSrC sent")["marked"]
    stopped = bench.tools.pd_capture_stop()
    assert stopped["stopped"] and stopped["reason"] == "manual" and stopped["running"] is False
    assert any(line.startswith("t~") and "mark: stimulus GSrC sent" in line
               for line in stopped["lines"])
    recs = records(path)
    assert [r["type"] for r in recs].count("mark") == 1 and recs[-1]["reason"] == "manual"
    assert bench.tools.pd_capture_mark("late")["kind"] == "Km003cError"
    again = bench.tools.pd_capture_stop()
    assert again["stopped"] is False and again["path"] == path


def test_second_start_is_refused_while_running(bench_factory):
    bench = bench_factory(S3)
    bench.tools.pd_capture_start()
    assert bench.tools.pd_capture_start()["kind"] == "Km003cError"
    bench.tools.pd_capture_stop()


def test_corrupt_payload_mid_capture_is_recorded_and_capture_continues(bench_factory):
    payloads = list(S3)
    k = next(i for i, p in enumerate(payloads) if "82102cb18441" in p.hex())
    payloads[k] = payloads[k][:-1]  # the last record (a GoodCRC) loses a byte
    bench = bench_factory(payloads)
    path = bench.tools.pd_capture_start(stop_on="contract")["path"]
    assert bench.tools.pd_capture_wait(timeout_s=10)["reason"] == "contract"
    recs = records(path)
    errors = [r for r in recs if r["type"] == "error"]
    assert len(errors) == 1 and errors[0]["kind"] == "ProtocolError"
    assert errors[0]["payload"] == payloads[k].hex()
    # The records before the broken one were kept, and the capture went on to PS_RDY.
    assert [r["msg"] for r in non_goodcrc(recs)][-3:] == ["Request", "Accept", "PS_RDY"]


def test_corrupt_putdata_is_recorded_and_capture_continues(bench_factory):
    bench = bench_factory([S3[0], CORRUPT_PUTDATA, *S3[1:]])
    path = bench.tools.pd_capture_start(stop_on="contract")["path"]
    assert bench.tools.pd_capture_wait(timeout_s=10)["reason"] == "contract"
    errors = [r for r in records(path) if r["type"] == "error"]
    assert len(errors) == 1 and "logical packet needs 200 bytes" in errors[0]["message"]


def test_repeated_protocol_errors_end_the_capture_with_a_footer(bench_factory):
    bench = bench_factory([S3[0]] + [CORRUPT_PUTDATA] * MAX_CONSECUTIVE_PROTOCOL_ERRORS)
    path = bench.tools.pd_capture_start()["path"]
    summary = bench.tools.pd_capture_wait(timeout_s=5)
    assert summary["reason"] == "error"
    recs = records(path)
    # One error record per failed poll, then the one that ended the capture.
    assert recs[-1]["type"] == "footer" and recs[-1]["errors"] == MAX_CONSECUTIVE_PROTOCOL_ERRORS + 1
    assert "polls in a row failed to parse" in recs[-2]["message"]


def test_whole_negotiation_in_one_poll(bench_factory):
    from capture_bench import S4
    bench = bench_factory(S4)
    path = bench.tools.pd_capture_start(stop_on="contract")["path"]
    assert bench.tools.pd_capture_wait(timeout_s=10)["reason"] == "contract"
    assert [r["msg"] for r in non_goodcrc(records(path))] == [
        "Vendor_Defined", "Vendor_Defined", "Source_Capabilities", "Request", "Accept", "PS_RDY"]


def test_default_path_uses_capture_dir_and_label(bench_factory, capture_dir):
    bench = bench_factory(S3)
    path = bench.tools.pd_capture_start(label="cold attach #1")["path"]
    bench.tools.pd_capture_stop()
    assert path.startswith(str(capture_dir.resolve())) and path.endswith("-cold-attach-1.jsonl")


def test_bad_options_return_errors_without_claiming(bench_factory):
    bench = bench_factory(S3)
    assert bench.tools.pd_capture_start(stop_on="never")["kind"] == "ValueError"
    assert bench.tools.pd_capture_start(timeout_s=0)["kind"] == "ValueError"
    assert bench.meters == []


def test_existing_file_is_not_overwritten(bench_factory, tmp_path):
    target = tmp_path / "keep.jsonl"
    target.write_text("precious\n")
    bench = bench_factory(S3)
    assert bench.tools.pd_capture_start(path=str(target))["kind"] == "FileExistsError"
    assert target.read_text() == "precious\n" and bench.meters == []


def test_reading_a_file_never_claims_the_meter(tmp_path):
    def no_meter():
        raise AssertionError("pd_capture_read opened the meter")
    service = Km003cService(transport_factory=no_meter, idle_release_s=0)
    tools = build_tools(service, enable_trigger=False)
    for mode in ("summary", "messages", "facts", "full"):
        out = tools.pd_capture_read(path=str(GOLDEN), mode=mode)
        assert "error" not in out and out["path"] == str(GOLDEN.resolve())
    assert service.leases() == {} and not service.claimed


# --- ProtocolError from the meter becomes an error result -----------------------------------

def _putdata(tid, attribute, payload):
    import struct
    return bytes([CMD_PUT_DATA, tid, 0, 0]) + struct.pack("<I", attribute | len(payload) << 22) \
        + payload


def _tools(script):
    opened = [bytes.fromhex("05010000"), bytes.fromhex("06020000")]
    service = Km003cService(transport_factory=lambda: FakeTransport(opened + script),
                            idle_release_s=None)
    return build_tools(service, enable_trigger=False)


def test_measure_returns_protocol_error_result():
    tools = _tools([_putdata(3, ATT_ADC, bytes(10))])
    assert tools.measure()["kind"] == "ProtocolError"


def test_pd_status_returns_protocol_error_result():
    tools = _tools([_putdata(3, ATT_PD, bytes(5))])
    out = tools.pd_status()
    assert out["kind"] == "ProtocolError" and "too short" in out["error"]
    # The session survives: the next call works.
    tools2 = _tools([_putdata(3, ATT_PD, bytes(5)), live_adc_with_tid(4)])
    tools2.pd_status()
    assert "vbus_v" in tools2.measure()


def test_capture_survives_poll_timing(bench_factory):
    # A slow reader of the file sees whole records while the capture writes.
    bench = bench_factory(S3)
    path = bench.tools.pd_capture_start()["path"]
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and bench.meters[0].pd_polls < 50:
        out = bench.tools.pd_capture_read(mode="messages")
        assert "error" not in out
    bench.tools.pd_capture_stop()
    assert records(path)[-1]["type"] == "footer"
