"""Helpers for the capture tests: the Phase 1 spike payloads and a service on MeterFake."""

import json
from pathlib import Path

from fakes import MeterFake
from km003c_mcp.server import Km003cService, build_tools

FIXTURES = Path(__file__).parent / "fixtures"
# Raw PdPacket payloads from a KM003C on firmware 2.0.0: an Apple 140 W charger attached to a
# USB PD sink, one payload per GetData poll.
SPIKE = json.loads((FIXTURES / "spike_fw2.0.0.json").read_text())
S3 = [bytes.fromhex(p) for p in SPIKE["s3"]["payloads"]]
S4 = [bytes.fromhex(p) for p in SPIKE["s4"]["payloads"]]
GOLDEN = FIXTURES / "s3_golden.jsonl"


def s3_until(marker: str) -> list[bytes]:
    """The S3 payloads up to and including the first one whose hex contains ``marker``."""
    for k, p in enumerate(S3):
        if marker in p.hex():
            return S3[:k + 1]
    raise ValueError(marker)


def records(path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


def normalize(record: dict) -> dict:
    """Blank the fields that depend on when the test ran."""
    r = dict(record)
    if "t_host" in r:
        r["t_host"] = 0
    for key in ("started_utc", "stopped_utc"):
        if key in r:
            r[key] = "<utc>"
    if r.get("type") == "header":
        r["server"] = {**r["server"], "version": "<version>"}
    return r


class CaptureBench:
    """A Km003cService whose meter replays PD payloads, polled every millisecond."""

    def __init__(self, monkeypatch, payloads=(), idle_release_s=0):
        monkeypatch.setenv("KM003C_PD_POLL_MS", "1")
        self.payloads = list(payloads)
        self.meters: list[MeterFake] = []
        self.service = Km003cService(transport_factory=self._open,
                                     port_finder=lambda *a, **k: "COMX",
                                     idle_release_s=idle_release_s)
        self.tools = build_tools(self.service, enable_trigger=False)

    def _open(self):
        meter = MeterFake(serial_number="000000", pd_payloads=self.payloads)
        self.meters.append(meter)
        return meter

    def close(self):
        self.service.shutdown()
