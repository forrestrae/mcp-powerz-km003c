"""Reading capture files: summary, messages, facts and full modes, filters, and damaged files."""

import json
import struct

import pytest

from capture_bench import GOLDEN, records
from km003c_mcp import capture_read as cr
from km003c_mcp.capture import CaptureSession, CaptureWriter

EXPECTED_LINES = [
    "t=14637003 connect (code 0x11) [45cb57df0011]",
    "t=14637199 disconnect (code 0x12) [458f58df0012]",
    "t=14637311 connect (code 0x11) [45ff58df0011]",
    "t=14637552 SOP' Discover_Identity REQ from port [8f10 01a000ff]",
    "t=14637555 SOP' Discover_Identity ACK VID 0x20c2 from cable "
    "[4f51 418000ff c220001c ba000000 10030500 52200821]",
    "t=14637557 SOP Source_Capabilities 5.0 V 3.0 A, 9.0 V 3.0 A, 15.0 V 3.0 A, 20.0 V 4.7 A "
    "[a141 2c918109 2cd10200 2cb10400 d6410600]",
    "t=14637562 SOP Request pos 4, 3.0 A [8210 2cb18441]",
    "t=14637564 SOP Accept [a303]",
    "t=14637780 SOP PS_RDY [a605]",
    "t=14640805 SOP Discover_Identity REQ from source/dfp [af17 01a000ff]",
    "t=14640811 SOP Discover_Identity ACK VID 0x0451 from sink/ufp "
    "[8f42 41a000ff 51044000 00000000 11090000]",
]


def size(obj) -> int:
    return len(json.dumps(obj, separators=(",", ":")))


# --- summary --------------------------------------------------------------------------------

def test_summary_of_the_golden_capture():
    s = cr.read(GOLDEN)
    assert s["lines"] == EXPECTED_LINES and s["truncated"] is False
    assert s["reason"] == "messages" and s["complete"] and s["meter_fw"] == "2.0.0"
    assert s["pd_monitor"] == "accepted" and s["conn"] == 3
    assert s["counts"] == {"Vendor_Defined": 4, "GoodCRC": 8, "Source_Capabilities": 1,
                           "Request": 1, "Accept": 1, "PS_RDY": 1}
    assert s["vbus_mv"] == {"first": 0, "last": 20506, "min": 0, "max": 20529}
    assert s["contract"] == {"t_ps_rdy": 14637780, "rdo": "4184b12c", "pdo": "000641d6",
                             "text": "pos 4, 3.0 A of 20.0 V 4.7 A"}
    assert s["warnings"] == []
    assert size(s) <= cr.SUMMARY_MAX_BYTES


def _write(tmp_path, records_, name="c.jsonl"):
    path = tmp_path / name
    path.write_text("".join(json.dumps(r) + "\n" for r in records_))
    return path


def test_summary_is_bounded_and_says_so(tmp_path):
    s = CaptureSession(CaptureWriter(tmp_path / "big.jsonl"), status_interval_ms=1000)
    s.write_header(meter={}, poll_ms=50, pd_monitor=None, label="big", provenance=None)
    caps = bytes.fromhex("a1412c9181092cd102002cb10400d6410600")
    for k in range(300):
        t = 1000 + 10 * k
        s.ingest(struct.pack("<IHhHH", t + 1, 5000, 0, 1600, 0)
                 + bytes([0x80 | (len(caps) + 5)]) + struct.pack("<I", t) + b"\x00" + caps)
    s.finish("manual")
    out = cr.read(tmp_path / "big.jsonl")
    assert out["truncated"] is True and size(out) <= cr.SUMMARY_MAX_BYTES
    marker = next(line for line in out["lines"] if line.startswith("..."))
    assert "lines omitted" in marker and "mode=messages" in marker
    assert out["lines"][0].startswith("t=1000 ") and out["lines"][-1].startswith("t=3990 ")
    assert out["counts"]["Source_Capabilities"] == 300


def test_sanity_rule_no_negotiation(tmp_path):
    path = _write(tmp_path, [
        {"type": "header", "format": "km003c-capture", "version": 1},
        {"type": "status", "t_host": 0, "t_meter_ms": 1000, "vbus_mv": 5010},
        {"type": "status", "t_host": 1, "t_meter_ms": 2000, "vbus_mv": 5020},
        {"type": "footer", "reason": "timeout"},
    ])
    warnings = cr.read(path)["warnings"]
    assert any(w.startswith("no Source_Capabilities, no Request, VBUS flat at 5010 to 5020 mV")
               for w in warnings)


def test_sanity_rule_only_connection_events(tmp_path):
    monitor = {"enabled": False, "outcome": "already enabled or refused", "attempts": 1}
    path = _write(tmp_path, [
        {"type": "header", "format": "km003c-capture", "version": 1, "pd_monitor": monitor},
        {"type": "status", "t_host": 0, "t_meter_ms": 1000, "vbus_mv": 0},
        {"type": "conn", "t_meter_ms": 990, "code": 17, "event": "connect",
         "raw_event": "45de03000011"},
        {"type": "status", "t_host": 1, "t_meter_ms": 2000, "vbus_mv": 20000},
        {"type": "footer", "reason": "timeout"},
    ])
    warnings = cr.read(path)["warnings"]
    assert any("PD monitoring was not active" in w and "already enabled or refused" in w
               for w in warnings)


def test_sanity_rule_request_without_source_capabilities():
    lines = records(GOLDEN)
    late = [r for r in lines if r.get("msg") != "Source_Capabilities"]
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        path = _write(Path(d), late)
        s = cr.read(path)
    assert any(w == "Request at t=14637562 has no Source_Capabilities before it: "
               "the capture started late" for w in s["warnings"])
    assert s["contract"]["pdo"] is None


def test_truncated_file_without_footer(tmp_path):
    text = GOLDEN.read_text().splitlines()
    # A crash in the middle of the last ACK's line: no GoodCRC after it and no footer.
    cut = "\n".join(text[:-3]) + "\n" + text[-3][:40]
    (tmp_path / "crash.jsonl").write_text(cut)
    s = cr.read(tmp_path / "crash.jsonl")
    assert s["complete"] is False and s["reason"] is None
    assert s["lines"] == EXPECTED_LINES[:-1]
    assert any(w.startswith("no footer") for w in s["warnings"])
    assert any("is incomplete (the capture was cut off while writing)" in w
               for w in s["warnings"])
    assert s["vbus_mv"]["max"] == 20529  # from the status records


def test_not_a_capture_file(tmp_path):
    (tmp_path / "x.jsonl").write_text('{"type": "poll"}\n')
    with pytest.raises(ValueError, match="not a km003c-capture file"):
        cr.read(tmp_path / "x.jsonl")
    (tmp_path / "v2.jsonl").write_text(
        '{"type": "header", "format": "km003c-capture", "version": 2}\n')
    with pytest.raises(ValueError, match="version 2"):
        cr.read(tmp_path / "v2.jsonl")


def test_bare_name_is_found_in_the_capture_dir(capture_dir):
    (capture_dir / "g.jsonl").write_text(GOLDEN.read_text())
    assert cr.read("g.jsonl")["path"] == str((capture_dir / "g.jsonl").resolve())


# --- timestamps -----------------------------------------------------------------------------

def test_reader_takes_connection_times_as_stored(tmp_path):
    # 32-bit times past 2^24 ms are used as they are, not adjusted from the status block.
    path = _write(tmp_path, [
        {"type": "header", "format": "km003c-capture", "version": 1},
        {"type": "status", "t_host": 1.0, "t_meter_ms": 40103699, "vbus_mv": 2},
        {"type": "conn", "t_meter_ms": 40103694, "code": 0x11, "event": "connect",
         "raw_event": "450eef630211"},
    ])
    assert [e.t for e in cr.load_capture(path).entries] == [40103694]


# --- messages and full ----------------------------------------------------------------------

def test_messages_filters():
    by_sop = cr.read(GOLDEN, "messages", sop="SOP'")["lines"]
    assert by_sop == EXPECTED_LINES[3:5]
    with_crc = cr.read(GOLDEN, "messages", sop="SOP'", include_goodcrc=True)["lines"]
    assert [line.split(" ")[2] for line in with_crc] == [
        "Discover_Identity", "GoodCRC", "Discover_Identity", "GoodCRC"]
    assert cr.read(GOLDEN, "messages", types=["request", "PS_RDY"])["lines"] == \
        [EXPECTED_LINES[6], EXPECTED_LINES[8]]
    assert cr.read(GOLDEN, "messages", types=["Discover_Identity"], sop="SOP")["lines"] == \
        EXPECTED_LINES[9:]
    assert cr.read(GOLDEN, "messages", types=["connect"])["count"] == 2
    assert cr.read(GOLDEN, "messages", since=14637780)["lines"] == EXPECTED_LINES[8:]
    with pytest.raises(ValueError, match="unknown sop"):
        cr.read(GOLDEN, "messages", sop="SOP'''")


def _walk(path, mode="messages", **kwargs):
    """Every page of a read, following next_after."""
    pages, after = [], None
    while True:
        page = cr.read(path, mode, after=after, **kwargs)
        pages.append(page)
        if "next_after" not in page:
            return pages
        assert page["count"] > 0
        after = page["next_after"]


def test_messages_paging_walks_the_golden_file_once():
    pages = _walk(GOLDEN, limit=4, include_goodcrc=True)
    lines = [line for page in pages for line in page["lines"]]
    assert len(lines) == 19 and len(set(lines)) == 19 and len(pages) == 5
    assert all(page["total"] == 19 for page in pages)


def test_after_must_be_a_line_number():
    with pytest.raises(ValueError, match="after must be a line number"):
        cr.read(GOLDEN, "messages", after="14637555")


def test_full_mode_default_limit_and_hex_beside_decode():
    out = cr.read(GOLDEN, "full", include_goodcrc=True)
    assert out["count"] == 16 and "next_after" not in out
    first_pd = next(n for n, line in enumerate(GOLDEN.read_text().splitlines(), 1)
                    if '"type":"pd"' in line)
    assert out["messages"][0]["line"] == first_pd
    out = cr.read(GOLDEN, "full", types=["Request"])
    (msg,) = out["messages"]
    assert msg["wire"] == "82102cb18441" and msg["decoded"]["objects"] == ["4184b12c"]
    assert msg["decoded"]["request"]["object_position"] == 4
    assert cr._limit("full", None) == 20
    with pytest.raises(ValueError):
        cr.read(GOLDEN, "full", limit=0)


# --- facts ----------------------------------------------------------------------------------

def test_facts_for_a_cross_check():
    f = cr.read(GOLDEN, "facts")
    (caps,) = f["source_capabilities"]
    assert [p["hex"] for p in caps["pdos"]] == ["0981912c", "0002d12c", "0004b12c", "000641d6"]
    assert caps["pdos"][3]["voltage_v"] == 20.0 and caps["sender"] == "source/dfp"
    (req,) = f["requests"]
    assert req["rdo"]["hex"] == "4184b12c" and req["rdo"]["object_position"] == 4
    assert req["requested_pdo"]["hex"] == "000641d6"
    assert (req["outcome"], req["accept_t"], req["ps_rdy_t"]) == ("accepted", 14637564, 14637780)
    assert f["contract"]["request_index"] == 0 and f["contract"]["rdo"] == "4184b12c"
    ids = f["discover_identity_acks"]
    assert set(ids) == {"SOP", "SOP'"} and set(ids["SOP"]) == {"sink/ufp"}
    (board,) = ids["SOP"]["sink/ufp"]
    assert board["vdo_count"] == 3 and board["vdos"][0]["hex"] == "00400451"
    assert board["vdos"][0]["decoded"]["vid"] == "0x0451"
    (cable,) = ids["SOP'"]["cable"]
    assert cable["vdos"][3]["name"] == "passive_cable"
    assert f["warnings"] == []


def test_identity_vdos_of_a_dual_role_partner():
    # ID header: PDUSB peripheral UFP (2) and PDUSB host DFP (2): UFP VDO, pad, DFP VDO.
    id_header = (2 << 27) | (2 << 23) | 0x05AC
    vdos = [id_header, 0, 0x12340001, 0x11000000, 0, 0x22000000]
    from km003c_mcp.pd_vdm import decode_identity
    out = cr._identity_vdos(vdos, decode_identity(vdos, 0), 0)
    assert [v["name"] for v in out] == ["id_header", "cert_stat", "product", "ufp", "pad", "dfp"]
    assert out[5]["hex"] == "22000000" and out[5]["decoded"] is not None


# --- retransmissions and Soft_Reset ---------------------------------------------------------

def _pd(i, t, wire, sop="SOP"):
    code = {"SOP": 0, "SOP'": 1}[sop]
    head = bytes([0x80 | ((len(wire) // 2 + 5) & 0x3F)]) + t.to_bytes(4, "little") + bytes([code])
    return {"type": "pd", "i": i, "t_meter_ms": t, "sop": sop, "wire": wire,
            "raw_event": head.hex() + wire}


HEADER = {"type": "header", "format": "km003c-capture", "version": 1}
CAPS = "a1412c9181092cd102002cb10400d6410600"


def test_retransmitted_request_appears_once_in_facts(tmp_path):
    path = _write(tmp_path, [
        HEADER, _pd(0, 1000, CAPS), _pd(1, 1001, "4100"),
        _pd(2, 1005, "82102cb18441"), _pd(3, 1006, "82102cb18441"), _pd(4, 1007, "6101"),
        _pd(5, 1009, "a303"), _pd(6, 1010, "4102"), _pd(7, 1200, "a605"), _pd(8, 1201, "4104"),
    ])
    f = cr.read(path, "facts")
    assert len(f["requests"]) == 1 and f["requests"][0]["ps_rdy_t"] == 1200
    lines = cr.read(path)["lines"]
    assert lines[1].startswith("t=1005 SOP Request") and "(retry)" not in lines[1]
    assert lines[2].startswith("t=1006 SOP Request pos 4, 3.0 A (retry) [")


def test_soft_reset_ends_the_negotiation_in_progress(tmp_path):
    path = _write(tmp_path, [
        HEADER, _pd(0, 1000, CAPS), _pd(1, 1001, "4100"),
        _pd(2, 1005, "82102cb18441"), _pd(3, 1006, "6101"),
        _pd(4, 1008, "ad01"), _pd(5, 1009, "4102"),  # Soft_Reset from the source
        _pd(6, 1010, "a303"), _pd(7, 1011, "4104"),  # an Accept that answers nothing now
        _pd(8, 1200, "a605"), _pd(9, 1201, "4106"),
    ])
    (req,) = cr.read(path, "facts")["requests"]
    assert req["outcome"] is None and req["ps_rdy_t"] is None
    assert cr.read(path)["contract"] is None


# --- file checks and long fields ------------------------------------------------------------

def test_reader_requires_a_regular_file(tmp_path):
    with pytest.raises(ValueError, match="not a regular file"):
        cr.read(tmp_path)
    with pytest.raises(ValueError, match="no such file"):
        cr.read(tmp_path / "missing.jsonl")


def test_reader_refuses_a_file_over_the_size_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "MAX_FILE_BYTES", 1000)
    (tmp_path / "big.jsonl").write_text(GOLDEN.read_text())
    with pytest.raises(ValueError, match="this reader loads at most 1000 bytes"):
        cr.read(tmp_path / "big.jsonl")


def test_long_mark_error_and_wire_are_cut_with_an_indication(tmp_path):
    long_wire = "a303" + "00" * 400
    path = _write(tmp_path, [
        HEADER,
        {"type": "status", "t_host": 1.0, "t_meter_ms": 1000, "vbus_mv": 5000},
        {"type": "mark", "t_host": 1.0, "text": "m" * 5000},
        {"type": "error", "t_host": 1.0, "kind": "ProtocolError", "message": "e" * 5000},
        _pd(0, 1001, long_wire),
    ])
    lines = cr.read(path, "messages")["lines"]
    assert all(len(line) < 600 for line in lines)
    assert "(4800 more chars)" in lines[0] and "(4800 more chars)" in lines[1]
    assert "more chars)]" in lines[2]
    (msg,) = cr.read(path, "full")["messages"]
    assert len(msg["wire"]) < 200 and msg["wire"].endswith("more chars)")


# --- malformed records ----------------------------------------------------------------------

STATUS = {"type": "status", "t_host": 1.0, "t_meter_ms": 1000, "vbus_mv": 5000}


@pytest.mark.parametrize("bad, problem", [
    ({**HEADER, "meter": "KM003C"}, "header meter is not an object"),
    ({**HEADER, "pd_monitor": "accepted"}, "header pd_monitor is not an object"),
    ({**HEADER, "stop": "contract"}, "header stop is not an object"),
    ({"type": "conn", "t_meter_ms": "1000", "code": 17, "event": "connect",
      "raw_event": "45"}, "conn record has a bad t_meter_ms"),
    ({"type": "mark", "t_host": "now", "text": "x"}, "mark record has a bad t_host"),
    ({"type": "error", "t_host": 1.0, "kind": 5, "message": "x"}, "error record has a bad kind"),
    ({"type": "pd", "i": 0, "t_meter_ms": 1001, "sop": "SOP", "wire": "a303",
      "raw_event": 12}, "pd record has a bad raw_event"),
    ({"type": "pd", "i": 0, "t_meter_ms": 1001, "sop": "SOP", "wire": "a303",
      "raw_event": "zz"}, "pd record has a bad raw_event"),
    ({"type": "pd", "i": 0, "t_meter_ms": 1001, "sop": "SOP", "wire": None},
     "pd record has a bad wire"),
    ({"type": "status", "t_host": 1.0, "t_meter_ms": 2.5, "vbus_mv": 5000},
     "status record has a bad t_meter_ms"),
    ({"type": "footer", "reason": "manual", "vbus_mv": "flat"}, "footer vbus_mv is malformed"),
    ({"type": "footer", "reason": "manual", "vbus_mv": {"first": "0"}},
     "footer vbus_mv is malformed"),
    ({"type": "footer", "reason": ["manual"]}, "footer record has a bad reason"),
    ({"type": 7}, "unknown record type"),
])
def test_malformed_records_are_flagged_not_raised(tmp_path, capsys, bad, problem):
    lines = [bad, STATUS] if bad.get("type") == "header" else [HEADER, STATUS, bad]
    path = _write(tmp_path, lines + [_pd(1, 1002, "a605")])
    from km003c_mcp.cli import main
    for mode in cr.MODES:
        out = cr.read(path, mode)
        assert "path" in out
    assert any(problem in w for w in cr.read(path)["warnings"])
    assert cr.read(path)["lines"][-1].startswith("t=1002 SOP PS_RDY")
    assert main(["summarize", str(path)]) == 0 and main(["facts", str(path)]) == 0


def test_cli_exits_2_for_an_unreadable_file(tmp_path, capsys):
    from km003c_mcp.cli import main
    (tmp_path / "x.jsonl").write_text('"just a string"\n')
    assert main(["summarize", str(tmp_path / "x.jsonl")]) == 2
    assert main(["facts", str(tmp_path)]) == 2
    assert "cannot read" in capsys.readouterr().err


# --- paging by position ---------------------------------------------------------------------

# Marks carry host-estimated times that can run ahead of the meter's message times.
OUT_OF_ORDER = [
    HEADER,
    {"type": "status", "t_host": 100.0, "t_meter_ms": 980, "vbus_mv": 5000},
    _pd(0, 990, "a303"),
    {"type": "mark", "t_host": 100.07, "text": "stimulus"},  # estimated t=1050
    _pd(1, 1020, "a605"),
    _pd(2, 1040, "a303"),
    _pd(3, 1055, "a605"),
]


@pytest.mark.parametrize("limit", [1, 2])
def test_paging_by_position_neither_repeats_nor_skips(tmp_path, limit):
    path = _write(tmp_path, OUT_OF_ORDER)
    pages = _walk(path, limit=limit)
    lines = [line for page in pages for line in page["lines"]]
    assert [line.split(" ")[0] for line in lines] == ["t=990", "t~1050", "t=1020", "t=1040",
                                                      "t=1055"]
    assert all("next_after" in page for page in pages[:-1])
    assert "next_after" not in pages[-1]


def test_paging_includes_a_mark_without_a_time(tmp_path):
    # No status record: the mark's time cannot be estimated.
    path = _write(tmp_path, [HEADER, _pd(0, 990, "a303"),
                             {"type": "mark", "t_host": 1.0, "text": "no time"},
                             _pd(1, 1020, "a605")])
    lines = [line for page in _walk(path, limit=1) for line in page["lines"]]
    assert lines[1] == "t~? mark: no time" and len(lines) == 3
    # since is a window: it keeps timed entries at or after it and drops the untimed mark.
    assert [line.split(" ")[0] for line in cr.read(path, "messages", since=1000)["lines"]] == [
        "t=1020"]


# --- summary bound with oversized fixed fields ----------------------------------------------

def _oversized(tmp_path, body):
    deep = tmp_path
    for k in range(4):
        deep = deep / ("d" * 80 + str(k))
    deep.mkdir(parents=True)
    header = {**HEADER, "label": "L" * 10_000, "meter": {"fw_version": "F" * 10_000},
              "pd_monitor": {"outcome": "O" * 10_000}}
    bad = [{"type": "pd", "t_meter_ms": k, "wire": "zz"} for k in range(40)]
    return _write(deep, [header, *bad, *body], name="c" * 100 + ".jsonl")


@pytest.mark.parametrize("body", [[], [_pd(k, 1000 + k, CAPS) for k in range(200)]],
                         ids=["no lines", "many lines"])
def test_summary_bound_holds_with_oversized_fields(tmp_path, body):
    path = _oversized(tmp_path, body)
    extra = {"running": False, "stopped": True}
    out = cr.read(path, "summary", extra=extra)
    assert size(out) <= cr.SUMMARY_MAX_BYTES and out["truncated"] is bool(body)
    assert out["stopped"] is True and out["running"] is False
    assert out["path"].endswith(".jsonl") and out["path"].startswith("...")
    assert "more chars" in out["label"] and "more chars" in out["meter_fw"]
    assert len(out["warnings"]) <= cr.SUMMARY_MAX_WARNINGS


def test_summary_with_no_lines_is_not_truncated():
    out = cr.summary(cr.Capture(GOLDEN, HEADER, None, [], []))
    assert out["lines"] == [] and out["truncated"] is False


# --- facts bound ----------------------------------------------------------------------------

def test_facts_stay_within_their_bound_and_keep_the_contract(tmp_path):
    golden = records(GOLDEN)
    caps = next(r for r in golden if r.get("msg") == "Source_Capabilities")
    body = [r for r in golden[1:-1]]
    # 50 more Source_Capabilities and Requests that never complete, before the real ones.
    noise = []
    for k in range(50):  # each acknowledged, so none is a retransmission
        noise += [{**caps, "i": 1000 + 4 * k, "t_meter_ms": 100 + 4 * k},
                  _pd(1001 + 4 * k, 101 + 4 * k, "4100"),
                  _pd(1002 + 4 * k, 102 + 4 * k, "82102cb18441"),
                  _pd(1003 + 4 * k, 103 + 4 * k, "6101")]
    path = _write(tmp_path, [golden[0], *noise, *body, golden[-1]])
    f = cr.read(path, "facts")
    assert size(f) <= cr.FACTS_MAX_BYTES and f["truncated"] is True
    assert f["omitted"]["source_capabilities"] >= 40 and f["omitted"]["requests"] >= 40
    contract = f["requests"][f["contract"]["request_index"]]
    assert contract["ps_rdy_t"] == 14637780 and contract["rdo"]["hex"] == "4184b12c"
    assert any(c["i"] == contract["source_capabilities_i"] for c in f["source_capabilities"])
    # Both SOP and SOP' identity ACKs survive.
    assert set(f["discover_identity_acks"]) == {"SOP", "SOP'"}


def test_facts_of_a_short_capture_omit_nothing():
    f = cr.read(GOLDEN, "facts")
    assert f["truncated"] is False and set(f["omitted"].values()) == {0}


# --- a real cold attach past 2^24 ms of uptime ----------------------------------------------

COLD = GOLDEN.parent / "cold_attach_fw2.0.0.jsonl"


def test_cold_attach_summary_and_facts():
    s = cr.read(COLD)
    assert s["reason"] == "contract" and s["warnings"] == [] and s["meter_fw"] == "2.0.0"
    assert s["contract"] == {"t_ps_rdy": 40104470, "rdo": "4184b12c", "pdo": "000641d6",
                             "text": "pos 4, 3.0 A of 20.0 V 4.7 A"}
    f = cr.read(COLD, "facts")
    contract = f["requests"][f["contract"]["request_index"]]
    assert contract["rdo"]["object_position"] == 4 and contract["rdo"]["hex"] == "4184b12c"
    assert contract["ps_rdy_t"] == 40104470
    acks = f["discover_identity_acks"]
    assert set(acks) == {"SOP", "SOP'"}
    assert acks["SOP'"]["cable"][0]["vdos"][0]["decoded"]["vid"] == "0x20c2"
    assert acks["SOP"]["sink/ufp"][0]["vdos"][0]["decoded"]["vid"] == "0x0451"


def test_cold_attach_timestamps_are_32_bit_and_match_the_poll():
    from km003c_mcp.protocol import split_pd
    recs = records(COLD)
    events = [r for r in recs if r["type"] in ("pd", "conn")]
    assert {r["type"] for r in events} == {"pd", "conn"}
    status = None
    for r in recs:
        if r["type"] == "status":
            status = r
        elif r["type"] in ("pd", "conn"):
            # Reparse each raw event behind its poll's status block: bytes 1 to 4 are a u32.
            (event,) = split_pd(bytes.fromhex(status["raw"] + r["raw_event"])).events
            assert event.timestamp_ms == r["t_meter_ms"] > 1 << 24
            assert 0 <= status["t_meter_ms"] - r["t_meter_ms"] < 1000
