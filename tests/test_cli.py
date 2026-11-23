"""The km003c-capture command-line tool."""

import json
import subprocess
import sys

import km003c_mcp.server as srv
from capture_bench import GOLDEN, S3, records
from fakes import MeterFake
from km003c_mcp.cli import main


def test_summarize_prints_the_bounded_summary(capsys):
    assert main(["summarize", str(GOLDEN)]) == 0
    out = capsys.readouterr().out
    assert "reason messages (complete)" in out
    assert "contract: pos 4, 3.0 A of 20.0 V 4.7 A; RDO 4184b12c, PDO 000641d6" in out
    assert "  t=14637562 SOP Request pos 4, 3.0 A [8210 2cb18441]" in out


def test_summarize_json_and_facts(capsys):
    assert main(["summarize", "--json", str(GOLDEN)]) == 0
    assert json.loads(capsys.readouterr().out)["reason"] == "messages"
    assert main(["facts", str(GOLDEN)]) == 0
    facts = json.loads(capsys.readouterr().out)
    assert facts["requests"][0]["rdo"]["hex"] == "4184b12c"


def test_bad_file_exits_2(tmp_path, capsys):
    assert main(["facts", str(tmp_path / "missing.jsonl")]) == 2
    assert "km003c-capture:" in capsys.readouterr().err


BLOCK_USB = """
import sys
sys.modules["usb"] = None  # any import of pyusb now fails
from km003c_mcp.cli import main
sys.exit(main(sys.argv[1:]))
"""


def test_reading_works_without_usb_at_all():
    for command in ("summarize", "facts"):
        done = subprocess.run([sys.executable, "-c", BLOCK_USB, command, str(GOLDEN)],
                              capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr
        assert "4184b12c" in done.stdout


def test_record_announces_the_path_and_stops_on_contract(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("KM003C_PD_POLL_MS", "1")
    real = srv.Km003cService
    monkeypatch.setattr(srv, "Km003cService", lambda **kw: real(
        transport_factory=lambda: MeterFake(serial_number="000000", pd_payloads=S3), **kw))
    path = tmp_path / "cold.jsonl"
    code = main(["record", "--stop-on", "contract", "--timeout", "20", "--label", "cold",
                 "--provenance", '{"charger": "Apple 140 W"}', str(path)])
    out = capsys.readouterr().out.splitlines()
    assert code == 0 and out[0] == f"capturing {path.resolve()}"
    assert any(line == "path: " + str(path.resolve()) for line in out)
    recs = records(path)
    assert recs[0]["meter"]["fw_version"] == "2.0.0"
    assert recs[0]["provenance"] == {"charger": "Apple 140 W"}
    assert recs[-1]["reason"] == "contract"
