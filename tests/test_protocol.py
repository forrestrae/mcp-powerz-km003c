import pytest

from fakes import LIVE_ADC
from km003c_mcp.protocol import (
    ATT_ADC, ATT_PD, CMD_GET_DATA, CMD_MEMORY_READ, CMD_START_GRAPH, ProtocolError,
    encode_ctrl, encode_raw, parse_adc, parse_putdata,
)


def test_encode_ctrl_get_adc():
    assert encode_ctrl(CMD_GET_DATA, 0, ATT_ADC) == bytes.fromhex("0c000200")
    assert encode_ctrl(CMD_GET_DATA, 7, ATT_ADC | ATT_PD) == bytes.fromhex("0c072200")
    assert encode_ctrl(CMD_START_GRAPH, 1, 3) == bytes.fromhex("0e010600")


def test_encode_raw():
    assert encode_raw(CMD_MEMORY_READ, 2, 0x0101) == bytes.fromhex("44020101")


def test_parse_putdata_live_adc():
    hdr, pkts = parse_putdata(LIVE_ADC)
    assert (hdr.cmd, hdr.tid, hdr.obj_count) == (0x41, 2, 10)
    assert len(pkts) == 1 and pkts[0].attribute == ATT_ADC
    assert len(pkts[0].payload) == 44 and not pkts[0].next


def test_parse_adc_live_values():
    r = parse_adc(parse_putdata(LIVE_ADC)[1][0].payload)
    assert r.vbus_v == pytest.approx(0.002048)
    assert r.ibus_a == pytest.approx(-0.000411)
    assert r.temp_c is None  # raw 0x8000
    assert r.cc1_v == pytest.approx(0.0780)
    assert r.vdd_v == pytest.approx(3.2504)
    assert (r.rate_index, r.flags) == (0, 0x80)
    assert r.dm_avg_v == pytest.approx(0.086)


def test_parse_adc_temperature():
    payload = bytearray(44)
    payload[24:26] = int(31.5 * 128).to_bytes(2, "little", signed=True)
    assert parse_adc(bytes(payload)).temp_c == pytest.approx(31.5)


def test_parse_putdata_chain_two_packets():
    adc = (ATT_ADC | (1 << 15) | (44 << 22)).to_bytes(4, "little") + bytes(44)
    pd = (ATT_PD | (12 << 22)).to_bytes(4, "little") + bytes(12)
    _, pkts = parse_putdata(bytes.fromhex("41058004") + adc + pd)
    assert [p.attribute for p in pkts] == [ATT_ADC, ATT_PD]


def test_parse_putdata_truncated_raises():
    with pytest.raises(ProtocolError):
        parse_putdata(LIVE_ADC[:30])


# --- Task 2: AdcQueue and PD parsing ---

import struct  # noqa: E402

from km003c_mcp.protocol import (  # noqa: E402
    ATT_ADCQUEUE, PdStatus, parse_adcqueue, parse_pd, sequence_step, split_pd,
)


def _sample(seq, uv, ua, aux):
    return struct.pack("<HHiiHHHH", seq, 3, uv, ua, *aux)


def test_parse_adcqueue_units_by_rate():
    p = _sample(1000, 5_000_000, -1_500_000, (780, 366, 840, 878))
    s_slow = parse_adcqueue(p, 0)[0]
    s_fast = parse_adcqueue(p, 3)[0]
    assert (s_slow.vbus_v, s_slow.ibus_a) == (5.0, -1.5)
    assert s_slow.cc1_v == pytest.approx(0.078) and s_fast.cc1_v == pytest.approx(0.780)
    assert (s_slow.sequence, s_slow.marker) == (1000, 3)


def test_parse_adcqueue_empty_and_bad_length():
    assert parse_adcqueue(b"", 3) == []
    with pytest.raises(ProtocolError):
        parse_adcqueue(bytes(19), 3)


def test_sequence_step():
    assert [sequence_step(i) for i in range(4)] == [500, 100, 20, 1]


def test_parse_pd_status_live_prefix():
    st = parse_pd(bytes.fromhex("55040200" "0300" "0000" "0000" "0000"))
    assert st == PdStatus(132181, 0.003, 0.0, 0.0, 0.0)


PD_STREAM = (bytes(12)
             + bytes.fromhex("451027000021")  # connect @ 10000 ms
             + bytes.fromhex("8b1a27000000") + bytes.fromhex("81112c910100")  # SOP Source_Capabilities
             + bytes.fromhex("452827000022"))  # disconnect @ 10024 ms


def test_parse_pd_event_stream():
    ev = parse_pd(PD_STREAM).events
    assert [e.kind for e in ev] == ["connect", "message", "disconnect"]
    assert (ev[0].timestamp_ms, ev[2].timestamp_ms) == (10000, 10024)
    assert ev[1].timestamp_ms == 10010 and ev[1].sop == 0
    assert ev[1].wire == bytes.fromhex("81112c910100")


def test_parse_pd_bad_size_flag_raises():
    with pytest.raises(ProtocolError):
        parse_pd(bytes(12) + bytes.fromhex("830000000000"))


def test_parse_putdata_adcqueue_payload_is_chunk_times_size():
    # Live capture: AdcQueue ext header carries sample count in `chunk` and bytes per sample in `size`.
    samples = b"".join(_sample(100 + i, 5_000_000, 0, (0, 0, 0, 0)) for i in range(3))
    ext = (ATT_ADCQUEUE | (3 << 16) | (20 << 22)).to_bytes(4, "little")
    _, pkts = parse_putdata(bytes.fromhex("41010000") + ext + samples)
    assert len(pkts[0].payload) == 60
    assert [s.sequence for s in parse_adcqueue(pkts[0].payload, 3)] == [100, 101, 102]


# Real captures from a KM003C (fw 1.8.4) between a PD charger and a phone.
REAL_ATTACH_RECORDS = bytes.fromhex("836c2f00a901000001000000" "458f602f0011" "459a622f0012")
REAL_CONTRACT = bytes.fromhex(
    "a1d02f009b14e6ff8b030100"
    "8b73d02f0000a1175a900127" "8774d02f00004106" "8b77d02f000082125a688113"
    "8778d02f00006103" "877cd02f0000a309" "877dd02f00004108")


def test_parse_pd_legacy_connection_codes():
    ev = parse_pd(REAL_ATTACH_RECORDS).events
    assert [e.kind for e in ev] == ["connect", "disconnect"]
    assert ev[0].timestamp_ms == 0x2F608F


def test_parse_pd_real_contract_capture():
    from km003c_mcp.pd import PdDecoder
    stream = parse_pd(REAL_CONTRACT)
    dec = PdDecoder()
    decoded = [dec.decode(e) for e in stream.events]
    assert [d["type"] for d in decoded] == [
        "Source_Capabilities", "GoodCRC", "Request", "GoodCRC", "Accept", "GoodCRC"]
    assert decoded[0]["pdos"][0]["voltage_v"] == 5.0 and decoded[0]["pdos"][0]["max_current_a"] == 0.9
    assert "dual_role_power" in decoded[0]["pdos"][0]["flags"]
    assert decoded[2]["request"]["object_position"] == 1


def test_split_pd_keeps_connection_codes_and_raw_records():
    split = split_pd(PD_STREAM)
    assert split.error is None and split.status_raw == bytes(12)
    connect, message, disconnect = split.events
    assert (connect.code, connect.raw) == (0x21, bytes.fromhex("451027000021"))
    assert message.code is None
    assert message.raw == bytes.fromhex("8b1a27000000") + bytes.fromhex("81112c910100")
    assert (disconnect.kind, disconnect.code) == ("disconnect", 0x22)


def test_split_pd_legacy_and_unknown_connection_codes():
    payload = bytes(12) + bytes.fromhex("45cb57df0011") + bytes.fromhex("45cc57df0033")
    legacy, unknown = split_pd(payload).events
    assert (legacy.kind, legacy.code, legacy.timestamp_ms) == ("connect", 0x11, 0xDF57CB)
    assert (unknown.kind, unknown.code, unknown.sop) == ("connection", 0x33, None)


def test_connection_timestamp_is_32_bit():
    # A real poll at 40.1 million ms of uptime, past 2^24: byte 4 is the uptime's top byte.
    payload = bytes.fromhex("13ef6302020000001d060000") + bytes.fromhex("450eef630211")
    split = split_pd(payload)
    (event,) = split.events
    assert (event.kind, event.code, event.timestamp_ms) == ("connect", 0x11, 40103694)
    assert split.status.timestamp_ms - event.timestamp_ms < 100


def test_split_pd_keeps_records_before_a_corrupt_one():
    payload = PD_STREAM[:18] + bytes.fromhex("8b1a27000000") + bytes.fromhex("8111")
    split = split_pd(payload)
    assert [e.kind for e in split.events] == ["connect"]
    assert "needs 6 wire bytes" in split.error
    with pytest.raises(ProtocolError):
        parse_pd(payload)
    short = split_pd(bytes(5))
    assert short.status is None and short.events == [] and "too short" in short.error
