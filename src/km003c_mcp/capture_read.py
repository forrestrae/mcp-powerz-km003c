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
    records: list[dict] = []
    problems: list[str] = []
    with open(p, encoding="utf-8") as f:
        lines = f.read().split("\n")
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
        records.append(record)
    if not records or records[0].get("type") != "header" or records[0].get("format") != FORMAT:
        raise ValueError(f"{p} is not a {FORMAT} file (no header record)")
    if records[0].get("version") != VERSION:
        raise ValueError(f"{p} is {FORMAT} version {records[0].get('version')}; "
                         f"this reader understands version {VERSION}")
    header = records[0]
    footer = next((r for r in records if r.get("type") == "footer"), None)
    body = [r for r in records[1:] if r.get("type") != "footer"]

    # Status blocks carry the 32-bit uptime; the nearest one at or before a record (else the
    # next one) is the reference for extending its 24-bit time and for placing marks.
    status = [r for r in body if r.get("type") == "status"]
    following: list[dict | None] = [None] * len(body)
    upcoming = None
    for k in range(len(body) - 1, -1, -1):
        if body[k].get("type") == "status":
            upcoming = body[k]
        following[k] = upcoming

    entries: list[Entry] = []
    decoder = PdDecoder()
    retries = RetryFilter()
    last_status: dict | None = None
    for k, r in enumerate(body):
        kind = r.get("type")
        if kind == "status":
            last_status = r
            continue
        ref = last_status or following[k]
        ref_t = ref.get("t_meter_ms") if ref else None
        if kind == "pd":
            entry = _decode_entry(r, decoder)
            wire = bytes.fromhex(r.get("wire") or "") if _is_hex(r.get("wire")) else b""
            entry.retry = retries.is_retry(entry.sop, wire, entry.goodcrc)
            entries.append(entry)
        elif kind == "conn":
            entries.append(Entry("conn", extend_timestamp24(r.get("t_meter_ms", 0), ref_t), r))
        elif kind in ("mark", "error"):
            t = None
            if ref is not None and "t_host" in r and "t_host" in ref:
                t = round(ref_t + (r["t_host"] - ref["t_host"]) * 1000)
            entries.append(Entry(kind, t, r))
    return Capture(p, header, footer, entries, status, problems)


def _is_hex(value) -> bool:
    try:
        bytes.fromhex(value)
    except (TypeError, ValueError):
        return False
    return True


def _decode_entry(r: dict, decoder: PdDecoder) -> Entry:
    raw = bytes.fromhex(r.get("raw_event") or "")
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


def entry_line(entry: Entry) -> str:
    r = entry.record
    if entry.kind == "pd":
        retry = " (retry)" if entry.retry else ""
        return (f"t={entry.t} {r.get('sop', 'SOP')} {message_text(entry)}{retry} "
                f"[{wire_groups(r.get('wire', ''))}]")
    t = "?" if entry.t is None else entry.t
    if entry.kind == "conn":
        code = r.get("code")
        code_text = f"0x{code:02x}" if isinstance(code, int) else "?"
        return f"t={t} {r.get('event')} (code {code_text}) [{r.get('raw_event', '')}]"
    if entry.kind == "mark":
        return f"t~{t} mark: {r.get('text', '')}"
    return f"t~{t} error {r.get('kind')}: {str(r.get('message', ''))[:120]}"


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
    return out


# --- modes ------------------------------------------------------------------------------

def _compact(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))


def summary(capture: Capture, max_bytes: int = SUMMARY_MAX_BYTES) -> dict:
    """Bounded overview: one line per non-GoodCRC message (plus connection events, marks and
    errors), counts, VBUS, the final contract and the sanity warnings."""
    negs = negotiations(capture)
    footer = capture.footer
    header = capture.header
    counts: dict[str, int] = {}
    for e in capture.messages:
        key = e.msg or "invalid"
        counts[key] = counts.get(key, 0) + 1
    out: dict = {
        "path": str(capture.path), "label": (header.get("label") or None),
        "meter_fw": (header.get("meter") or {}).get("fw_version"),
        "pd_monitor": (header.get("pd_monitor") or {}).get("outcome"),
        "reason": footer.get("reason") if footer else None,
        "complete": footer is not None,
        "counts": counts,
        "conn": sum(1 for e in capture.entries if e.kind == "conn"),
        "vbus_mv": vbus_stats(capture),
        "contract": contract_brief(final_contract(negs)),
        "warnings": [w[:200] for w in warnings(capture, negs)],
    }
    if isinstance(out["label"], str):
        out["label"] = out["label"][:80]
    lines = [entry_line(e) for e in capture.entries if not e.goodcrc]
    out["lines"] = lines
    out["truncated"] = False
    if len(_compact(out)) <= max_bytes:
        return out
    # Keep the start and the end of the timeline and drop lines from the middle: the most
    # lines that fit, found by bisection (the size grows with the number kept).
    def trimmed(keep: int) -> list[str]:
        tail = keep // 3
        head = keep - tail
        cut = lines[head:len(lines) - tail]
        marker = (f"... {len(cut)} lines omitted ({cut[0].split(' ', 1)[0]} to "
                  f"{cut[-1].split(' ', 1)[0]}); read them with mode=messages")
        return lines[:head] + [marker] + lines[len(lines) - tail:]

    out["truncated"] = True
    low, high = 0, len(lines) - 1
    while low < high:
        mid = (low + high + 1) // 2
        out["lines"] = trimmed(mid)
        if len(_compact(out)) <= max_bytes:
            low = mid
        else:
            high = mid - 1
    out["lines"] = trimmed(low)
    return out


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


def _page(entries: list[Entry], limit: int) -> tuple[list[Entry], int | None]:
    """Up to ``limit`` entries, extended to keep entries with equal times together, and the
    ``since`` value of the next page."""
    if len(entries) <= limit:
        return entries, None
    end = limit
    while end < len(entries) and entries[end].t is not None and entries[end].t == entries[end - 1].t:
        end += 1
    return entries[:end], (entries[end].t if end < len(entries) else None)


def _limit(mode: str, limit: int | None) -> int:
    limit = DEFAULT_LIMITS[mode] if limit is None else limit
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be 1 to {MAX_LIMIT}")
    return limit


def messages(capture: Capture, sop=None, types=None, since=None, limit=None,
             include_goodcrc=False) -> dict:
    matched = select(capture, sop, types, since, include_goodcrc)
    page, next_since = _page(matched, _limit("messages", limit))
    return {"path": str(capture.path), "lines": [entry_line(e) for e in page],
            "count": len(page), "total": len(matched), "next_since": next_since}


def full(capture: Capture, sop=None, types=None, since=None, limit=None,
         include_goodcrc=False) -> dict:
    matched = select(capture, sop, types, since, include_goodcrc, messages_only=True)
    page, next_since = _page(matched, _limit("full", limit))
    return {"path": str(capture.path),
            "messages": [{"i": e.record.get("i"), "t": e.t, "sop": e.record.get("sop"),
                          "wire": e.record.get("wire"), "decoded": e.decoded} for e in page],
            "count": len(page), "total": len(matched), "next_since": next_since}


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


def _contract_fact(negs: list[dict]) -> dict | None:
    final = final_contract(negs)
    if final is None:
        return None
    return {"request_index": negs.index(final), **contract_brief(final)}


def facts(capture: Capture) -> dict:
    """The objects a register cross-check needs, each with its hex."""
    negs = negotiations(capture)
    caps = []
    identity: dict[str, dict[str, list]] = {}
    for e in capture.messages:
        d = e.decoded or {}
        if e.goodcrc:
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
            sop_name = e.record.get("sop", "SOP")
            identity.setdefault(sop_name, {}).setdefault(sender(e), []).append(ack)
    return {
        "path": str(capture.path),
        "label": capture.header.get("label"),
        "source_capabilities": caps,
        "requests": negs,
        "contract": _contract_fact(negs),
        "discover_identity_acks": identity,
        "warnings": warnings(capture, negs),
    }


def read(path: str | Path, mode: str = "summary", sop=None, types=None, since=None,
         limit=None, include_goodcrc: bool = False) -> dict:
    """Load ``path`` and render it in ``mode``."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    capture = load_capture(path)
    if mode == "summary":
        return summary(capture)
    if mode == "facts":
        return facts(capture)
    if mode == "messages":
        return messages(capture, sop, types, since, limit, include_goodcrc)
    return full(capture, sop, types, since, limit, include_goodcrc)
