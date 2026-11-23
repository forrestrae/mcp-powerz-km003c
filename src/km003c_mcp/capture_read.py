"""Read ``km003c-capture`` files: decode on read, and the summary, messages, facts and full modes.

Nothing here touches the meter. Every decoded value sits beside the wire hex it came from.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .capture import FORMAT, VERSION, RetryFilter, capture_dir, extend_timestamp24
from .pd import SOP_NAMES, PdDecoder
from .pd_objects import bits
from .protocol import PdEvent

MODES = ("summary", "messages", "facts", "full")
SUMMARY_MAX_BYTES = 2048
DEFAULT_LIMITS = {"messages": 50, "full": 20}
MAX_LIMIT = 500
# A capture of a long session is a few MB; refuse to load anything far larger into memory.
MAX_FILE_BYTES = 64 * 1024 * 1024
# Per-entry caps in summary, messages and full output; a longer value is cut and says so.
MAX_TEXT_CHARS = 200
MAX_HEX_CHARS = 160
# VBUS moving less than this across the capture counts as flat for the sanity rules.
VBUS_FLAT_MV = 500
_SOP_CODES = {name.lower(): code for code, name in SOP_NAMES.items()}
_REQUESTS = {"Request", "EPR_Request"}
_CAPABILITIES = {"Source_Capabilities", "EPR_Source_Capabilities"}


@dataclass
class Entry:
    """One line of the capture's timeline: a PD message, a connection event, a mark or an error."""

    kind: str  # "pd", "conn", "mark", "error"
    t: int | None  # meter ms; estimated for marks and errors
    record: dict
    decoded: dict | None = None
    sop: int | None = None
    retry: bool = False  # a retransmission of the sender's previous message
    line: int = 0  # the record's line number in the file: the position used for paging

    @property
    def msg(self) -> str | None:
        return self.decoded.get("type") if self.decoded else None

    @property
    def goodcrc(self) -> bool:
        return self.kind == "pd" and self.msg == "GoodCRC"


@dataclass
class Capture:
    path: Path
    header: dict
    footer: dict | None
    entries: list[Entry]
    status: list[dict]
    problems: list[str] = field(default_factory=list)

    @property
    def messages(self) -> list[Entry]:
        return [e for e in self.entries if e.kind == "pd"]


def resolve_path(path: str | Path) -> Path:
    """Absolute path of a capture file; a bare name missing here is looked up in the capture
    directory."""
    p = Path(path).expanduser()
    if not p.exists() and not p.is_absolute() and len(p.parts) == 1:
        candidate = capture_dir() / p
        if candidate.exists():
            p = candidate
    return p.resolve()


def load_capture(path: str | Path) -> Capture:
    """Parse a capture file, extend 24-bit timestamps, and decode every PD message.

    A malformed last line (a capture cut off mid-write) and other unreadable lines are
    reported in ``problems`` instead of raising. Raises ValueError when the file is not a
    ``km003c-capture`` file this reader understands.
    """
    p = resolve_path(path)
    if not p.is_file():
        raise ValueError(f"{p} is not a regular file" if p.exists() else f"no such file: {p}")
    size = p.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ValueError(f"{p} is {size} bytes; this reader loads at most {MAX_FILE_BYTES} "
                         "bytes (split the capture or read it with another tool)")
    records: list[dict] = []
    problems: list[str] = []
    with open(p, encoding="utf-8", errors="replace") as f:
        lines = f.read(MAX_FILE_BYTES + 1).split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for n, line in enumerate(lines, 1):
        try:
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError("not an object")
        except ValueError:
            problems.append(f"line {n} is incomplete (the capture was cut off while writing)"
                            if n == len(lines) else f"line {n} is not a JSON object; skipped")
            continue
        records.append((n, record))
    first = records[0][1] if records else {}
    if first.get("type") != "header" or first.get("format") != FORMAT:
        raise ValueError(f"{p} is not a {FORMAT} file (no header record)")
    if first.get("version") != VERSION:
        raise ValueError(f"{p} is {FORMAT} version {first.get('version')!r}; "
                         f"this reader understands version {VERSION}")
    header = _clean_header(first, problems)
    footer = None
    body = []
    for n, r in records[1:]:
        problem = _check_record(r)
        if problem is not None:
            problems.append(f"line {n}: {problem}; skipped")
        elif r["type"] == "footer":
            footer = footer or _clean_footer(r, problems)
        else:
            body.append((n, r))

    # Status blocks carry the 32-bit uptime; the nearest one at or before a record (else the
    # next one) is the reference for extending its 24-bit time and for placing marks.
    status = [r for _, r in body if r.get("type") == "status"]
    following: list[dict | None] = [None] * len(body)
    upcoming = None
    for k in range(len(body) - 1, -1, -1):
        if body[k][1].get("type") == "status":
            upcoming = body[k][1]
        following[k] = upcoming

    entries: list[Entry] = []
    decoder = PdDecoder()
    retries = RetryFilter()
    last_status: dict | None = None
    for k, (n, r) in enumerate(body):
        kind = r.get("type")
        if kind == "status":
            last_status = r
            continue
        ref = last_status or following[k]
        ref_t = ref.get("t_meter_ms") if ref else None
        ref_host = ref.get("t_host") if ref else None
        if kind == "pd":
            entry = _decode_entry(r, decoder)
            wire = bytes.fromhex(r.get("wire") or "") if _is_hex(r.get("wire")) else b""
            entry.retry = retries.is_retry(entry.sop, wire, entry.goodcrc)
            entry.line = n
            entries.append(entry)
        elif kind == "conn":
            entries.append(Entry("conn", extend_timestamp24(r.get("t_meter_ms", 0), ref_t), r,
                                 line=n))
        elif kind in ("mark", "error"):
            t = None
            if ref_t is not None and _is_number(r.get("t_host")) and _is_number(ref_host):
                try:
                    t = round(ref_t + (r["t_host"] - ref_host) * 1000)
                except (OverflowError, ValueError):  # inf or NaN host times
                    t = None
            entries.append(Entry(kind, t, r, line=n))
    return Capture(p, header, footer, entries, status, problems)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_text(value) -> bool:
    return isinstance(value, str)


def _optional(check):
    return lambda value: value is None or check(value)


# The fields each body record needs, with their checks; a record that fails is skipped.
_RECORD_FIELDS = {
    "status": {"t_meter_ms": _is_int, "vbus_mv": _is_int, "t_host": _optional(_is_number)},
    "pd": {"t_meter_ms": _is_int, "wire": lambda v: _is_text(v) and _is_hex(v),
           "raw_event": _optional(lambda v: _is_text(v) and _is_hex(v)),
           "sop": _optional(_is_text), "i": _optional(_is_int)},
    "conn": {"t_meter_ms": _is_int, "code": _optional(_is_int), "event": _optional(_is_text),
             "raw_event": _optional(_is_text)},
    "mark": {"t_host": _optional(_is_number), "text": _optional(_is_text)},
    "error": {"t_host": _optional(_is_number), "kind": _optional(_is_text),
              "message": _optional(_is_text)},
    "footer": {"reason": _optional(_is_text)},
}


def _check_record(r: dict) -> str | None:
    """Why a body record cannot be used, or None."""
    kind = r.get("type")
    fields = _RECORD_FIELDS.get(kind) if isinstance(kind, str) else None
    if fields is None:
        return f"unknown record type {clip(repr(kind), 40)}"
    for name, check in fields.items():
        if not check(r.get(name)):
            return f"{kind} record has a bad {name} ({clip(repr(r.get(name)), 40)})"
    return None


def _clean_header(header: dict, problems: list[str]) -> dict:
    """The header with every nested block a dict (or None) and the label text (or None)."""
    out = dict(header)
    for name in ("meter", "server", "pd_monitor", "stop"):
        if out.get(name) is not None and not isinstance(out[name], dict):
            problems.append(f"line 1: header {name} is not an object; ignored")
            out[name] = None
    if out.get("label") is not None and not isinstance(out["label"], str):
        out["label"] = str(out["label"])
    return out


def _clean_footer(footer: dict, problems: list[str]) -> dict:
    """The footer with malformed counts or VBUS statistics dropped."""
    out = dict(footer)
    counts = out.get("counts")
    if counts is not None and not (isinstance(counts, dict)
                                   and all(_is_int(v) for v in counts.values())):
        problems.append("footer counts are malformed; ignored")
        out.pop("counts")
    vbus = out.get("vbus_mv")
    if vbus is not None and not (isinstance(vbus, dict) and all(
            _optional(_is_int)(vbus.get(k)) for k in ("first", "last", "min", "max"))):
        problems.append("footer vbus_mv is malformed; ignored")
        out.pop("vbus_mv")
    return out


def _is_hex(value) -> bool:
    try:
        bytes.fromhex(value)
    except (TypeError, ValueError):
        return False
    return True


def _decode_entry(r: dict, decoder: PdDecoder) -> Entry:
    raw = bytes.fromhex(r.get("raw_event") or "")  # checked by _check_record
    sop = raw[5] if len(raw) >= 6 else _SOP_CODES.get(str(r.get("sop", "SOP")).lower(), 0)
    try:
        wire = bytes.fromhex(r.get("wire") or "")
        decoded = decoder.decode(PdEvent(r.get("t_meter_ms", 0), "message", sop, wire))
    except Exception as exc:  # noqa: BLE001 - a bad record must not stop the read
        decoded = {"error": f"{type(exc).__name__}: {exc}"}
    return Entry("pd", r.get("t_meter_ms"), r, decoded, sop)


# --- text -------------------------------------------------------------------------------

def _num(x: float) -> str:
    return f"{x:.1f}" if float(x).is_integer() else f"{x:g}"


def wire_groups(wire_hex: str) -> str:
    """Wire hex as the 2-byte header followed by 4-byte groups, in wire byte order."""
    return " ".join([wire_hex[:4]] + [wire_hex[i:i + 8] for i in range(4, len(wire_hex), 8)])


def pdo_text(p: dict) -> str:
    kind = p.get("type")
    if kind == "fixed":
        amps = p.get("max_current_a", p.get("operational_current_a", 0))
        return f"{_num(p['voltage_v'])} V {_num(amps)} A"
    if kind == "pps":
        return f"PPS {_num(p['min_voltage_v'])}-{_num(p['max_voltage_v'])} V {_num(p['max_current_a'])} A"
    if kind == "variable":
        return f"Var {_num(p['min_voltage_v'])}-{_num(p['max_voltage_v'])} V {_num(p['max_current_a'])} A"
    if kind == "battery":
        return f"Bat {_num(p['min_voltage_v'])}-{_num(p['max_voltage_v'])} V {_num(p['max_power_w'])} W"
    if kind == "epr_avs":
        return f"EPR AVS {_num(p['min_voltage_v'])}-{_num(p['max_voltage_v'])} V {p['pdp_w']} W"
    if kind == "spr_avs":
        return (f"SPR AVS {_num(p['max_current_15v_a'])} A to 15 V, "
                f"{_num(p['max_current_20v_a'])} A to 20 V")
    return str(kind)


def request_text(req: dict) -> str:
    out = f"pos {req.get('object_position')}"
    if "output_voltage_v" in req:
        return f"{out}, {_num(req['output_voltage_v'])} V {_num(req['operating_current_a'])} A"
    if "operating_power_w" in req:
        return f"{out}, {_num(req['operating_power_w'])} W"
    return f"{out}, {_num(req.get('operating_current_a', 0))} A"


def sender(entry: Entry) -> str:
    d = entry.decoded or {}
    if "power_role" in d:
        return f"{d['power_role']}/{d['data_role']}"
    if "cable_plug" in d:
        return "cable" if d["cable_plug"] else "port"
    return "unknown"


def message_text(entry: Entry) -> str:
    d = entry.decoded or {}
    name = d.get("type")
    if name is None:
        return f"invalid message ({d.get('error', 'undecodable')})"
    pdos = d.get("pdos") or (d.get("payload") or {}).get("pdos")
    if name in ("Source_Capabilities", "Sink_Capabilities", "EPR_Source_Capabilities",
                "EPR_Sink_Capabilities") and pdos:
        return f"{name} " + ", ".join(pdo_text(p) for p in pdos)
    if name in _REQUESTS and "request" in d:
        return f"{name} {request_text(d['request'])}"
    vdm = d.get("vdm")
    if vdm:
        if not vdm.get("structured"):
            return f"Vendor_Defined unstructured SVID {vdm['svid']} from {sender(entry)}"
        text = f"{vdm['command']} {vdm['command_type']}"
        vid = ((vdm.get("identity") or {}).get("id_header") or {}).get("vid")
        if vid:
            text += f" VID {vid}"
        return f"{text} from {sender(entry)}"
    return name


def clip(text, limit: int = MAX_TEXT_CHARS) -> str:
    """``text`` cut to ``limit`` characters, saying how much was cut."""
    text = str(text)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... ({len(text) - limit} more chars)"


def entry_line(entry: Entry) -> str:
    r = entry.record
    if entry.kind == "pd":
        retry = " (retry)" if entry.retry else ""
        return (f"t={entry.t} {r.get('sop', 'SOP')} {clip(message_text(entry))}{retry} "
                f"[{clip(wire_groups(r.get('wire', '')), MAX_HEX_CHARS)}]")
    t = "?" if entry.t is None else entry.t
    if entry.kind == "conn":
        code = r.get("code")
        code_text = f"0x{code:02x}" if isinstance(code, int) else "?"
        return (f"t={t} {clip(r.get('event'), 40)} (code {code_text}) "
                f"[{clip(r.get('raw_event', ''), MAX_HEX_CHARS)}]")
    if entry.kind == "mark":
        return f"t~{t} mark: {clip(r.get('text', ''))}"
    return f"t~{t} error {clip(r.get('kind'), 60)}: {clip(r.get('message', ''))}"


# --- analysis ---------------------------------------------------------------------------

def _objects(entry: Entry) -> list[str]:
    return (entry.decoded or {}).get("objects") or []


def negotiations(capture: Capture) -> list[dict]:
    """Every Request on SOP with the PDO it names, its reply and the PS_RDY that ended it.

    Retransmissions count once. A Soft_Reset ends the negotiation in progress.
    """
    out: list[dict] = []
    caps: Entry | None = None
    current: dict | None = None
    for e in capture.messages:
        if e.sop != 0 or e.goodcrc or e.retry:
            continue
        name = e.msg
        if name == "Soft_Reset":
            current = None  # as in StopTracker: the negotiation starts over
        elif name in _CAPABILITIES:
            caps = e
        elif name in _REQUESTS and "request" in (e.decoded or {}):
            rdo_hex = _objects(e)[0] if _objects(e) else None
            req = e.decoded["request"]
            pos = req.get("object_position")
            pdo = None
            if caps is not None and pos:
                words = _objects(caps)
                pdos = caps.decoded.get("pdos") or (caps.decoded.get("payload") or {}).get("pdos")
                if words and pdos and pos <= min(len(words), len(pdos)):
                    pdo = {"hex": words[pos - 1], **pdos[pos - 1]}
            current = {"t": e.t, "i": e.record.get("i"), "wire": e.record.get("wire"),
                       "rdo": {"hex": rdo_hex, **req}, "requested_pdo": pdo,
                       "source_capabilities_t": caps.t if caps else None,
                       "source_capabilities_i": caps.record.get("i") if caps else None,
                       "outcome": None, "accept_t": None, "ps_rdy_t": None}
            out.append(current)
        elif current is not None and current["outcome"] is None and name in (
                "Accept", "Reject", "Wait"):
            current["outcome"] = {"Accept": "accepted", "Reject": "rejected", "Wait": "wait"}[name]
            if name == "Accept":
                current["accept_t"] = e.t
        elif current is not None and current["outcome"] == "accepted" and name == "PS_RDY" \
                and current["ps_rdy_t"] is None:
            current["ps_rdy_t"] = e.t
    return out


def final_contract(negs: list[dict]) -> dict | None:
    done = [n for n in negs if n["ps_rdy_t"] is not None]
    return done[-1] if done else None


def contract_brief(n: dict | None) -> dict | None:
    if n is None:
        return None
    pdo = n["requested_pdo"]
    rdo = n["rdo"]
    text = request_text(rdo)
    if pdo is not None:
        text += f" of {pdo_text(pdo)}"
    return {"t_ps_rdy": n["ps_rdy_t"], "rdo": rdo["hex"], "pdo": pdo["hex"] if pdo else None,
            "text": text}


def vbus_stats(capture: Capture) -> dict:
    footer_vbus = (capture.footer or {}).get("vbus_mv")
    if footer_vbus and footer_vbus.get("first") is not None:
        return footer_vbus
    values = [s["vbus_mv"] for s in capture.status if isinstance(s.get("vbus_mv"), int)]
    if not values:
        return {"first": None, "last": None, "min": None, "max": None}
    return {"first": values[0], "last": values[-1], "min": min(values), "max": max(values)}


def warnings(capture: Capture, negs: list[dict] | None = None) -> list[str]:
    """The sanity rules to check before comparing a capture with anything else."""
    negs = negotiations(capture) if negs is None else negs
    out: list[str] = []
    messages = [e for e in capture.messages if not e.goodcrc]
    has_caps = any(e.msg in _CAPABILITIES for e in messages)
    has_request = any(e.msg in _REQUESTS for e in messages)
    vbus = vbus_stats(capture)
    flat = vbus["min"] is None or vbus["max"] - vbus["min"] < VBUS_FLAT_MV
    conn = [e for e in capture.entries if e.kind == "conn"]
    if not capture.messages and conn:
        monitor = capture.header.get("pd_monitor")
        out.append("only connection events, no PD messages: PD monitoring was not active "
                   f"(header pd_monitor: {json.dumps(monitor, separators=(',', ':'))})")
    elif not has_caps and not has_request and flat:
        span = "no VBUS reading" if vbus["min"] is None else \
            f"VBUS flat at {vbus['min']} to {vbus['max']} mV"
        out.append(f"no Source_Capabilities, no Request, {span}: no negotiation happened in "
                   "this window; check the stimulus, not the protocol")
    late = next((n for n in negs if n["source_capabilities_t"] is None), None)
    if late is not None:
        out.append(f"Request at t={late['t']} has no Source_Capabilities before it: "
                   "the capture started late")
    if capture.footer is None:
        out.append("no footer: the capture is still running or was cut off; "
                   "the summary covers the records present")
    elif capture.footer.get("reason") == "timeout" and \
            (capture.header.get("stop") or {}).get("stop_on", "none") != "none":
        out.append(f"stop condition {capture.header['stop']['stop_on']!r} not met "
                   "before the timeout")
    errors = [e for e in capture.entries if e.kind == "error"]
    if errors:
        first = errors[0].record
        out.append(f"{len(errors)} error record(s); first: {first.get('kind')}: "
                   f"{str(first.get('message', ''))[:100]}")
    out.extend(capture.problems[:3])
    if len(capture.problems) > 3:
        out.append(f"{len(capture.problems) - 3} more unreadable or malformed records")
    return out


# --- modes ------------------------------------------------------------------------------

def _compact(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))


SUMMARY_MAX_WARNINGS = 5
SUMMARY_MAX_COUNT_TYPES = 16


def _clip_left(text: str, limit: int) -> str:
    """``text`` cut from the left to ``limit`` characters; the end of a path matters most."""
    return text if len(text) <= limit else "..." + text[-(limit - 3):]


def _scalar(value, limit: int):
    return clip(value, limit) if isinstance(value, str) else value


def summary(capture: Capture, max_bytes: int = SUMMARY_MAX_BYTES,
            extra: dict | None = None) -> dict:
    """Bounded overview: one line per non-GoodCRC message (plus connection events, marks and
    errors), counts, VBUS, the final contract and the sanity warnings.

    The compact JSON of the returned object, ``extra`` fields included, stays within
    ``max_bytes``: lines from the middle go first, then warnings, counts and the contract,
    and long fields are cut throughout.
    """
    negs = negotiations(capture)
    footer = capture.footer
    header = capture.header
    counts: dict[str, int] = {}
    for e in capture.messages:
        key = clip(e.msg or "invalid", 40)
        counts[key] = counts.get(key, 0) + 1
    if len(counts) > SUMMARY_MAX_COUNT_TYPES:
        ranked = sorted(counts.items(), key=lambda kv: -kv[1])
        counts = dict(ranked[:SUMMARY_MAX_COUNT_TYPES])
        counts["other types"] = sum(v for _, v in ranked[SUMMARY_MAX_COUNT_TYPES:])
    warns = [clip(w) for w in warnings(capture, negs)]
    if len(warns) > SUMMARY_MAX_WARNINGS:
        more = len(warns) - SUMMARY_MAX_WARNINGS + 1
        warns = warns[:SUMMARY_MAX_WARNINGS - 1] + [f"{more} more warnings; read mode=facts"]
    contract = contract_brief(final_contract(negs))
    if contract is not None:
        contract["text"] = clip(contract["text"], 120)
    out: dict = {
        "path": _clip_left(str(capture.path), 200),
        "label": _scalar(header.get("label") or None, 80),
        "meter_fw": _scalar((header.get("meter") or {}).get("fw_version"), 40),
        "pd_monitor": _scalar((header.get("pd_monitor") or {}).get("outcome"), 60),
        "reason": _scalar(footer.get("reason") if footer else None, 40),
        "complete": footer is not None,
        "counts": counts,
        "conn": sum(1 for e in capture.entries if e.kind == "conn"),
        "vbus_mv": vbus_stats(capture),
        "contract": contract,
        "warnings": warns,
        **(extra or {}),
    }
    lines = [entry_line(e) for e in capture.entries if not e.goodcrc]
    out["lines"] = lines
    out["truncated"] = False

    def fits() -> bool:
        return len(_compact(out)) <= max_bytes

    if fits():
        return out
    out["truncated"] = True

    # Keep the start and the end of the timeline and drop lines from the middle: the most
    # lines that fit, found by bisection (the size grows with the number kept).
    def trimmed(keep: int) -> list[str]:
        if keep >= len(lines):
            return lines
        tail = keep // 3
        head = keep - tail
        cut = lines[head:len(lines) - tail]
        marker = (f"... {len(cut)} lines omitted ({clip(cut[0].split(' ', 1)[0], 20)} to "
                  f"{clip(cut[-1].split(' ', 1)[0], 20)}); read them with mode=messages")
        return lines[:head] + [marker] + lines[len(lines) - tail:]

    low, high = 0, len(lines)
    while low < high:
        mid = (low + high + 1) // 2
        out["lines"] = trimmed(mid)
        if fits():
            low = mid
        else:
            high = mid - 1
    out["lines"] = trimmed(low)
    # The fixed fields alone can still be too large: give up detail in this order.
    for shrink in (lambda: out.update(warnings=out["warnings"][:1]),
                   lambda: out.update(counts={}),
                   lambda: out.update(warnings=[]),
                   lambda: out.update(contract=None if out["contract"] is None else
                                      {k: out["contract"][k] for k in ("rdo", "pdo")}),
                   lambda: out.update(label=None, path=_clip_left(out["path"], 60)),
                   lambda: out.update(lines=[])):
        if fits():
            return out
        shrink()
    if fits():
        return out
    return {"path": _clip_left(out["path"], 60), "reason": out["reason"], "truncated": True,
            "lines": [], "warnings": ["summary too large; read mode=messages or facts"]}


def _sop_filter(sop) -> int | None:
    if sop is None or isinstance(sop, int):
        return sop
    code = _SOP_CODES.get(str(sop).strip().lower())
    if code is None:
        raise ValueError(f"unknown sop {sop!r}; use one of {', '.join(SOP_NAMES.values())}")
    return code


def _entry_names(e: Entry) -> set[str]:
    if e.kind == "pd":
        names = {(e.msg or "invalid").lower()}
        vdm = (e.decoded or {}).get("vdm") or {}
        if vdm.get("command"):
            names.add(str(vdm["command"]).lower())
        return names
    if e.kind == "conn":
        return {"conn", str(e.record.get("event")).lower()}
    return {e.kind}


def select(capture: Capture, sop=None, types=None, since: int | None = None,
           include_goodcrc: bool = False, messages_only: bool = False) -> list[Entry]:
    """Filter the timeline. ``types`` matches message type names, VDM command names (such as
    Discover_Identity), ``conn``, ``connect``, ``disconnect``, ``mark`` and ``error``,
    ignoring case. ``sop`` keeps only PD messages on that SOP. ``since`` keeps entries at or
    after that meter time."""
    code = _sop_filter(sop)
    wanted = {t.lower() for t in ([types] if isinstance(types, str) else types or [])}
    out = []
    for e in capture.entries:
        if (messages_only or code is not None) and e.kind != "pd":
            continue
        if code is not None and e.sop != code:
            continue
        if e.goodcrc and not include_goodcrc and "goodcrc" not in wanted:
            continue
        if wanted and not wanted & _entry_names(e):
            continue
        if since is not None and (e.t is None or e.t < since):
            continue
        out.append(e)
    return out


def _page(entries: list[Entry], after: int | None, limit: int) -> tuple[list[Entry], dict]:
    """The first ``limit`` entries after line ``after``, and the paging fields.

    Pages go by position in the file, not by time: marks and errors carry estimated times
    that need not be in order, and several entries can share a millisecond. ``next_after``
    is present only when more entries follow.
    """
    if after is not None and not isinstance(after, int):
        raise ValueError("after must be a line number (next_after of the previous page)")
    rest = [e for e in entries if after is None or e.line > after]
    page = rest[:limit]
    fields = {"count": len(page), "total": len(entries)}
    if len(rest) > len(page):
        fields["next_after"] = page[-1].line
    return page, fields


def _limit(mode: str, limit: int | None) -> int:
    limit = DEFAULT_LIMITS[mode] if limit is None else limit
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be 1 to {MAX_LIMIT}")
    return limit


def messages(capture: Capture, sop=None, types=None, since=None, limit=None,
             include_goodcrc=False, after=None) -> dict:
    matched = select(capture, sop, types, since, include_goodcrc)
    page, fields = _page(matched, after, _limit("messages", limit))
    return {"path": str(capture.path), "lines": [entry_line(e) for e in page], **fields}


def full(capture: Capture, sop=None, types=None, since=None, limit=None,
         include_goodcrc=False, after=None) -> dict:
    matched = select(capture, sop, types, since, include_goodcrc, messages_only=True)
    page, fields = _page(matched, after, _limit("full", limit))
    return {"path": str(capture.path),
            "messages": [{"line": e.line, "i": e.record.get("i"), "t": e.t,
                          "sop": e.record.get("sop"),
                          "wire": clip(e.record.get("wire"), MAX_HEX_CHARS),
                          "decoded": e.decoded} for e in page],
            **fields}


_VDO_NAMES = {0: "id_header", 1: "cert_stat", 2: "product"}


def _identity_vdos(vdos: list[int], identity: dict, sop: int) -> list[dict]:
    """Pair every Discover Identity VDO with its decode (USB PD R3.1 Table 6-37 layout)."""
    keyed: dict[int, tuple[str, object]] = {
        0: ("id_header", identity.get("id_header")), 1: ("cert_stat", identity.get("cert_xid")),
        2: ("product", identity.get("product"))}
    if sop != 0:
        for key in ("passive_cable", "active_cable", "vpd"):
            if key in identity:
                keyed[3] = (key, identity[key])
        if "active_cable_2" in identity:
            keyed[4] = ("active_cable_2", identity["active_cable_2"])
    else:
        ptype = bits(vdos[0], 29, 27) if vdos else 0
        for key in ("ufp", "ama"):
            if key in identity:
                keyed[3] = (key, identity[key])
        if "dfp" in identity:
            index = 5 if ptype else 3
            keyed[index] = ("dfp", identity["dfp"])
            if ptype:
                keyed[4] = ("pad", None)
    out = []
    for k, v in enumerate(vdos):
        name, decoded = keyed.get(k, (_VDO_NAMES.get(k, "product_type"), None))
        out.append({"index": k, "hex": f"{v:08x}", "name": name, "decoded": decoded})
    return out


FACTS_MAX_BYTES = 8192
# How many of each kind facts keeps, tried in order until the result fits FACTS_MAX_BYTES:
# (Source_Capabilities, Requests, Discover Identity ACKs). The final contract's
# Source_Capabilities and Request, and the latest ACK of each SOP and sender, are kept on
# top of these counts while the ACK cap allows.
_FACTS_KEEP = ((4, 8, 8), (2, 4, 4), (1, 2, 2), (1, 1, 1), (1, 1, 0))


def _keep(items: list, n: int, must: set[int]) -> list[int]:
    """Indexes of the last ``n`` items plus the ``must`` indexes, in order."""
    return sorted(set(range(max(len(items) - n, 0), len(items))) | must)


def facts(capture: Capture, max_bytes: int = FACTS_MAX_BYTES) -> dict:
    """The objects a register cross-check needs, each with its hex, within ``max_bytes``.

    A long capture keeps the latest Source_Capabilities, Requests, and Discover Identity
    ACKs, always including the final contract's Source_Capabilities and Request; ``omitted``
    counts what was left out.
    """
    negs = negotiations(capture)
    caps: list[dict] = []
    acks: list[tuple[str, str, dict]] = []
    for e in capture.messages:
        d = e.decoded or {}
        if e.goodcrc or e.retry:
            continue
        if e.msg in _CAPABILITIES and e.sop == 0:
            words = _objects(e)
            pdos = d.get("pdos") or (d.get("payload") or {}).get("pdos") or []
            caps.append({"t": e.t, "i": e.record.get("i"), "msg": e.msg, "sender": sender(e),
                         "msg_id": d.get("message_id"), "wire": e.record.get("wire"),
                         "pdos": [{"position": k + 1,
                                   "hex": words[k] if k < len(words) else None, **p}
                                  for k, p in enumerate(pdos)]})
        vdm = d.get("vdm") or {}
        if vdm.get("command") == "Discover_Identity" and vdm.get("command_type") == "ACK":
            words = _objects(e)
            vdos = [int(w, 16) for w in words[1:]]
            ack = {"t": e.t, "i": e.record.get("i"), "msg_id": d.get("message_id"),
                   "wire": e.record.get("wire"), "header": (e.record.get("wire") or "")[:4],
                   "vdm_header": words[0] if words else None, "vdo_count": len(vdos),
                   "vdos": _identity_vdos(vdos, vdm.get("identity") or {}, e.sop or 0)}
            acks.append((e.record.get("sop", "SOP"), sender(e), ack))

    final = final_contract(negs)
    must_req = {negs.index(final)} if final is not None else set()
    must_caps = {k for k, c in enumerate(caps)
                 if final is not None and c["i"] == final.get("source_capabilities_i")}
    latest_per_group = {}
    for k, (sop_name, who, _) in enumerate(acks):
        latest_per_group[(sop_name, who)] = k
    warns = warnings(capture, negs)
    out: dict = {}
    for keep_caps, keep_reqs, keep_acks in _FACTS_KEEP:
        cap_idx = _keep(caps, keep_caps, must_caps)
        req_idx = _keep(negs, keep_reqs, must_req)
        # The latest ACK of each SOP and sender first, then the most recent others.
        chosen = sorted(latest_per_group.values(), reverse=True)[:keep_acks]
        for k in range(len(acks) - 1, -1, -1):
            if len(chosen) >= keep_acks:
                break
            if k not in chosen:
                chosen.append(k)
        ack_idx = sorted(chosen)
        identity: dict[str, dict[str, list]] = {}
        for k in ack_idx:
            sop_name, who, ack = acks[k]
            identity.setdefault(sop_name, {}).setdefault(who, []).append(ack)
        requests = [negs[k] for k in req_idx]
        contract = None
        if final is not None:
            contract = {"request_index": requests.index(final), **contract_brief(final)}
        out = {
            "path": str(capture.path),
            "label": _scalar(capture.header.get("label"), 80),
            "source_capabilities": [caps[k] for k in cap_idx],
            "requests": requests,
            "contract": contract,
            "discover_identity_acks": identity,
            "omitted": {"source_capabilities": len(caps) - len(cap_idx),
                        "requests": len(negs) - len(req_idx),
                        "discover_identity_acks": len(acks) - len(ack_idx)},
            "warnings": [clip(w) for w in warns[:SUMMARY_MAX_WARNINGS]],
        }
        out["truncated"] = any(out["omitted"].values())
        if len(_compact(out)) <= max_bytes:
            return out
    out["truncated"] = True
    out["warnings"] = out["warnings"][:1] + ["facts exceed their size bound even at the "
                                             "smallest selection; read mode=full"]
    return out


def read(path: str | Path, mode: str = "summary", sop=None, types=None, since=None,
         limit=None, include_goodcrc: bool = False, after: int | None = None,
         extra: dict | None = None) -> dict:
    """Load ``path`` and render it in ``mode``; ``extra`` fields are added to the result
    (inside the size bound for ``summary``).

    Raises ValueError for a file that cannot be read as a capture, including one whose
    records are malformed in a way the loader's checks did not catch.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    capture = load_capture(path)
    try:
        if mode == "summary":
            return summary(capture, extra=extra)
        if mode == "facts":
            out = facts(capture)
        elif mode == "messages":
            out = messages(capture, sop, types, since, limit, include_goodcrc, after)
        else:
            out = full(capture, sop, types, since, limit, include_goodcrc, after)
        out.update(extra or {})
        return out
    except (AttributeError, TypeError, KeyError, IndexError) as exc:
        raise ValueError(f"{capture.path}: malformed capture file "
                         f"({type(exc).__name__}: {exc})") from exc
