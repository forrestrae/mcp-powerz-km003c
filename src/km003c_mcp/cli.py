"""``km003c-capture``: record a PD capture, or summarize and extract facts from capture files.

``summarize`` and ``facts`` read files only and never open the meter. ``record`` claims the
meter for the length of the capture and prints ``capturing <path>`` once capturing has
begun, so a script can start it in the background and wait for that line.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys

from . import capture_read
from .capture import STOP_ON


def _summary_text(s: dict) -> str:
    out = [f"path: {s['path']}"]
    state = "complete" if s.get("complete") else "no footer"
    out.append(f"label: {s.get('label')}; meter fw {s.get('meter_fw')}; "
               f"pd_monitor {s.get('pd_monitor')}; reason {s.get('reason')} ({state})")
    counts = ", ".join(f"{k} {v}" for k, v in s.get("counts", {}).items()) or "none"
    out.append(f"messages: {counts}; connection events {s.get('conn', 0)}")
    v = s.get("vbus_mv") or {}
    out.append(f"VBUS mV: first {v.get('first')}, last {v.get('last')}, "
               f"min {v.get('min')}, max {v.get('max')}")
    c = s.get("contract")
    out.append("contract: none" if c is None else
               f"contract: {c['text']}; RDO {c['rdo']}, PDO {c['pdo']}, PS_RDY t={c['t_ps_rdy']}")
    out.extend(f"warning: {w}" for w in s.get("warnings", []))
    out.append("lines:" + (" (truncated)" if s.get("truncated") else ""))
    out.extend(f"  {line}" for line in s.get("lines", []))
    return "\n".join(out)


def _record(args: argparse.Namespace) -> int:
    from .server import Km003cService

    provenance = json.loads(args.provenance) if args.provenance else None
    if provenance is not None and not isinstance(provenance, dict):
        raise ValueError("--provenance must be a JSON object")

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    previous_handler = signal.signal(signal.SIGTERM, interrupted)
    service = Km003cService(idle_release_s=0)
    recorder = service.recorder
    try:
        try:
            monitor = recorder.start_pd(
                args.path, args.label, provenance, stop_on=args.stop_on,
                stop_after_messages=args.stop_after_messages, settle_ms=args.settle_ms,
                timeout_s=args.timeout, status_interval_ms=args.status_interval_ms)
        except Exception:
            if recorder.capture_path is not None:
                print(f"capture file {recorder.capture_path}", file=sys.stderr, flush=True)
            raise
        print(f"capturing {recorder.capture_path}", flush=True)
        print(f"pd_monitor {monitor.outcome}; stop_on {args.stop_on}; timeout {args.timeout:g} s; "
              "Ctrl-C stops", flush=True)
        try:
            while not recorder.wait_pd(0.5):
                pass
        except KeyboardInterrupt:
            recorder.stop_pd()
    finally:
        service.shutdown()
        signal.signal(signal.SIGTERM, previous_handler)
    path = recorder.capture_path
    if path is None:
        return 1
    summary = capture_read.read(path, "summary")
    print(json.dumps(summary, indent=1) if args.json else _summary_text(summary), flush=True)
    return 1 if summary.get("reason") == "error" else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="km003c-capture",
        description="Record USB PD captures from a ChargerLab POWER-Z KM003C, or read saved "
                    "capture files (summarize and facts never touch the meter).")
    sub = parser.add_subparsers(dest="command", required=True)

    rec = sub.add_parser("record", help="capture PD traffic to a new capture file")
    rec.add_argument("path", nargs="?",
                     help="file or directory (default: $KM003C_CAPTURE_DIR, else ./captures)")
    rec.add_argument("--label")
    rec.add_argument("--provenance", help="JSON object stored in the header")
    rec.add_argument("--stop-on", choices=STOP_ON, default="none")
    rec.add_argument("--stop-after-messages", type=int)
    rec.add_argument("--settle-ms", type=int, default=0)
    rec.add_argument("--timeout", type=float, default=60, help="seconds (default 60)")
    rec.add_argument("--status-interval-ms", type=int, default=100)
    rec.add_argument("--json", action="store_true", help="print the summary as JSON")

    summ = sub.add_parser("summarize", help="print the bounded summary of a capture file")
    summ.add_argument("path")
    summ.add_argument("--json", action="store_true", help="print the summary as JSON")
    summ.add_argument("--max-bytes", type=int, default=capture_read.SUMMARY_MAX_BYTES)

    facts = sub.add_parser("facts", help="print the cross-check facts of a capture file as JSON")
    facts.add_argument("path")

    args = parser.parse_args(argv)
    try:
        if args.command == "record":
            return _record(args)
        capture = capture_read.load_capture(args.path)
        if args.command == "facts":
            print(json.dumps(capture_read.facts(capture), indent=1))
            return 0
        summary = capture_read.summary(capture, args.max_bytes)
        print(json.dumps(summary, indent=1) if args.json else _summary_text(summary))
        return 0
    except (OSError, ValueError) as exc:
        print(f"km003c-capture: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - report meter errors without a traceback
        print(f"km003c-capture: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
