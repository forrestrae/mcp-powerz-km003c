# km003c-mcp

MCP server for the ChargerLab POWER-Z KM003C USB-C power meter. It exposes
measurements, high-rate VBUS/IBUS streaming, USB PD capture with decoding, and the
meter's fast-charge protocol trigger as MCP tools (stdio transport).

## Install

The server requires Python 3.11 or later. Each platform section below installs the package
with its development dependencies into `.venv` in the clone.

### Windows

Run both commands in PowerShell, in the root of the clone:

```powershell
python -m venv .venv
```

```powershell
.venv\Scripts\pip install -e .[dev]
```

The meter's vendor interface already uses the WinUSB driver, so no extra driver is needed.
`libusb-package` ships the libusb DLL.

### macOS and Linux

Run this in the root of the clone. It needs [uv](https://docs.astral.sh/uv/):

```bash
uv sync --extra dev
```

On macOS, including Apple Silicon, the libusb copy bundled with `libusb-package` works
without a driver or `sudo`. If it does not load, install Homebrew's libusb
(`brew install libusb`); pyusb finds `/opt/homebrew/lib/libusb-1.0.dylib` on its own.

On Linux, the user also needs read and write access to the meter's USB device node,
usually through a udev rule for `5FC9:0063`.

### libusb lookup order

On every platform the server loads libusb-1.0 in this order: the file named by
`KM003C_LIBUSB`, the copy bundled with `libusb-package`, then the system library through
pyusb's normal search.

## Register with Claude Code

In both commands, `<repo>` is the absolute path of the clone.

### Windows

```powershell
claude mcp add km003c -- <repo>\.venv\Scripts\km003c-mcp.exe
```

### macOS and Linux

```bash
claude mcp add km003c -- uv --directory <repo> run km003c-mcp
```

## Allow the tools in Claude Code

Registration makes the tools available, but Claude Code still asks for approval on each
tool call until a permission rule allows it. Add the rules to the repository where Claude
Code runs, which is the project that uses the meter and not necessarily this clone. The
steps are the same on every platform.

1. Choose the file. `.claude/settings.json` is checked in and applies to everyone who
   works in the repository. `.claude/settings.local.json` applies to one person and stays
   out of version control.
2. Add the tools to `permissions.allow` in that file, merging with any rules already
   there. Each rule is `mcp__<server>__<tool>`, where `<server>` is the name given to
   `claude mcp add`, `km003c` in the commands above.

    ```json
    {
      "permissions": {
        "allow": [
          "mcp__km003c__device_info",
          "mcp__km003c__measure",
          "mcp__km003c__pd_status",
          "mcp__km003c__stream_start",
          "mcp__km003c__stream_status",
          "mcp__km003c__stream_samples",
          "mcp__km003c__stream_stop",
          "mcp__km003c__pd_capture_start",
          "mcp__km003c__pd_capture_wait",
          "mcp__km003c__pd_capture_mark",
          "mcp__km003c__pd_capture_stop",
          "mcp__km003c__pd_capture_read",
          "mcp__km003c__device_release"
        ]
      }
    }
    ```

3. Start a new Claude Code session in that repository. The listed tools now run without
   an approval prompt.

The list leaves out the `trigger_*` tools on purpose. They can make a charger change VBUS,
so with `KM003C_ENABLE_TRIGGER=1` set and no rule for them, Claude Code asks before each
trigger call. The shorter rule `mcp__km003c__*` allows every tool of the server, the
trigger tools included, so use it only where unprompted trigger calls are acceptable.

These rules cover tool calls only. `pd_capture_start` and `stream_start` write files at
the path the caller passes, and the server process writes them itself, outside Claude
Code's file permission rules.

## Register with Codex

`codex mcp add` writes the server to `~/.codex/config.toml` as `[mcp_servers.km003c]`, so
one registration serves every project. In both commands, `<repo>` is the absolute path of
the clone.

### Windows

```powershell
codex mcp add km003c -- <repo>\.venv\Scripts\km003c-mcp.exe
```

### macOS and Linux

```bash
codex mcp add km003c -- uv --directory <repo> run km003c-mcp
```

On either platform, confirm the registration. The output lists `km003c` with its command:

```bash
codex mcp list
```

To set one of the variables from the configuration table below, pass `--env` before the
server name, once per variable, for example `--env KM003C_CAPTURE_DIR=<directory>`.

## Allow the tools in Codex

Codex decides per tool whether to ask for approval. Its default mode, `auto`, asks unless
the tool's MCP annotations mark it read-only, or mark it as neither destructive nor
open-world. This server's tools carry no annotations, so Codex asks before each call until
the configuration approves the tool. The steps are the same on every platform.

1. Open `~/.codex/config.toml`, the file `codex mcp add` wrote the registration to. On
   Windows it is `%USERPROFILE%\.codex\config.toml`.
2. Add this table below the `[mcp_servers.km003c]` entry. `approve` runs the tool without
   asking.

    ```toml
    [mcp_servers.km003c.tools]
    device_info = { approval_mode = "approve" }
    measure = { approval_mode = "approve" }
    pd_status = { approval_mode = "approve" }
    stream_start = { approval_mode = "approve" }
    stream_status = { approval_mode = "approve" }
    stream_samples = { approval_mode = "approve" }
    stream_stop = { approval_mode = "approve" }
    pd_capture_start = { approval_mode = "approve" }
    pd_capture_wait = { approval_mode = "approve" }
    pd_capture_mark = { approval_mode = "approve" }
    pd_capture_stop = { approval_mode = "approve" }
    pd_capture_read = { approval_mode = "approve" }
    device_release = { approval_mode = "approve" }
    ```

3. Start a new Codex session. The listed tools now run without an approval prompt.

As in the Claude Code list, the `trigger_*` tools are left out so that Codex keeps asking
before each trigger call. Setting `default_tools_approval_mode = "approve"` in
`[mcp_servers.km003c]` approves every tool of the server, the trigger tools included, so
use it only where unprompted trigger calls are acceptable. The other modes are `prompt`,
which always asks, and `writes`, which asks for every tool not marked read-only and so
asks for all of this server's tools.

## Sharing the meter with other programs

Close the ChargerLab POWER-Z PC software before using the server: only one program can
hold the meter's USB interface. The server reports `DeviceBusyError` otherwise, with the
likely holders; on macOS it also names the holding process from `ioreg`, for example
`pid 46945, python3.14`. This server releases the meter 2 s after its last use, and
`device_release` releases it at once, so another program can use the meter while this
server keeps running.

## Configuration (environment variables)


| Variable | Purpose |
|---|---|
| `KM003C_SERIAL` | Pick a meter by USB serial number when several are attached |
| `KM003C_COM_PORT` | Override the trigger's virtual serial port. The value is a port name such as `COM14` on Windows, or a device path such as `/dev/cu.usbmodem1101` on macOS |
| `KM003C_ENABLE_TRIGGER` | `1` registers the `trigger_*` tools. Unset, or any other value such as `0` or `false`, leaves every one of them out of the tool list, so an agent can neither see nor call them |
| `KM003C_IDLE_RELEASE_S` | Release the meter's USB interface this many seconds after the last tool call, PD capture, or stream that uses it ends; the next call claims it again. Unset gives 2. `0` releases at once; `never` keeps the meter claimed until the server exits |
| `KM003C_PD_POLL_MS` | PD capture poll interval in milliseconds, 1 to 10000 (default 50; the meter buffers a whole negotiation between polls) |
| `KM003C_CAPTURE_DIR` | Directory for capture files when `pd_capture_start` gets no `path`. Unset uses `captures` under the server's working directory. The directory is created on first use |
| `KM003C_LIBUSB` | Load libusb-1.0 from this file instead of the bundled one (`KM003C_LIBUSB_DLL` is the older name and still works) |

This fork's defaults for two variables differ from upstream's. Unset in upstream,
`KM003C_ENABLE_TRIGGER` registers the trigger tools and `KM003C_IDLE_RELEASE_S` keeps the
meter claimed until the server exits. Here the trigger tools stay off unless explicitly
enabled, and the meter is released 2 s after its last use. Set both variables explicitly
to get the same behavior from either build.

## Tools

| Tool | What it does |
|---|---|
| `device_info` | Model, hardware/firmware version, serial, trigger serial port |
| `measure` | VBUS/IBUS/power, CC1/CC2, D+/D−, VDD, temperature; optional averaging |
| `pd_status` | PD-side snapshot (VBUS, IBUS, CC1, CC2). During a PD capture it returns the latest status block the capture received, with the meter's timestamp and its age in milliseconds |
| `stream_start` / `stream_status` / `stream_samples` / `stream_stop` | Background sampling at 2/10/50/1000 SPS with min/max/mean, energy (Wh), charge (mAh), dropped-sample count and optional CSV |
| `pd_capture_start` / `pd_capture_wait` / `pd_capture_mark` / `pd_capture_stop` | Background PD capture to a capture file, with stop conditions. Start enables the meter's PD monitor and reports the outcome; stop disables it. See [PD capture files](docs/pd-capture.md) |
| `pd_capture_read` | Read a capture file in a bounded mode: `summary`, `messages`, `facts`, or `full`. Decodes the full USB PD R3.1 message set: PDOs and RDOs (including PPS, AVS, and EPR), PD 3.x data objects, extended messages with chunk reassembly, and structured VDMs (Discover Identity including cable VDOs, SVIDs, DisplayPort and Thunderbolt 3 alternate modes). Never touches the meter |
| `device_release` | Release the meter's USB interface now so another program can claim it; refused while a PD capture or stream runs |
| `trigger_detect` / `trigger_enter` | Protocol detection (`entry list`, `entry list+`) and entering a protocol |
| `trigger_pd_source_caps` / `trigger_pd_request` | Read the charger's PDOs and request a fixed/PPS/AVS contract |
| `trigger_fast_charge` | QC2/QC3/FCP/AFC/SFCP/SCP/VFCP/TFCP/UFCS voltage requests |
| `trigger_reset` / `trigger_close` / `trigger_raw` | Reset, leave trigger mode, or send any documented command |

Trigger requests make the charger raise VBUS on the meter's output, so disconnect
sensitive loads first. Unless `KM003C_ENABLE_TRIGGER=1` is set, none of the trigger
tools is registered, `trigger_detect` included, because protocol probing can also make a
charger change VBUS.

Positive IBUS means current flows from the meter's USB-C receptacle (input) to its plug
(output).

The trigger's virtual serial port is found by USB VID and PID, so other USB serial devices
attached to the host do not confuse it. It is a `COM` port on Windows and the meter's
`/dev/cu.usbmodem*` device on macOS. `KM003C_COM_PORT` overrides the search.

## PD capture files

A PD capture writes a JSON Lines file that holds the meter's raw bytes; full decodes are
computed when the file is read. [docs/pd-capture.md](docs/pd-capture.md) describes the
capture tools and their options, the record types, the `pd_capture_read` modes and
filters, and the `km003c-capture` command-line tool, which summarizes capture files
without opening the meter.

## Tests

The unit tests need no hardware. The hardware tests need a meter attached, the POWER-Z PC
software closed, and `KM003C_HW=1`; without that variable they are skipped.

### Windows

Run the unit tests in PowerShell, in the root of the clone:

```powershell
.venv\Scripts\pytest
```

Run the hardware tests:

```powershell
$env:KM003C_HW="1"; .venv\Scripts\pytest tests\test_hw.py
```

### macOS and Linux

Run the unit tests in the root of the clone:

```bash
uv run --extra dev pytest -m "not hw"
```

Run the hardware tests:

```bash
KM003C_HW=1 uv run --extra dev pytest tests/test_hw.py
```

### Interactive tests and the golden capture

Hardware tests that need a person to plug cables, such as the cold attach that checks a
capture stops on `contract`, also need `KM003C_HW_INTERACTIVE=1`; they prompt on the
terminal. `tests/fixtures/s3_golden.jsonl` is the capture the fake meter produces from real
firmware 2.0.0 payloads; after a deliberate format change, regenerate it with
`KM003C_UPDATE_GOLDEN=1` and review the diff.

## Protocol sources

- [okhsunrog/km003c-protocol-research](https://github.com/okhsunrog/km003c-protocol-research) (MIT) and
  [okhsunrog/km003c-rs](https://github.com/okhsunrog/km003c-rs) (MIT/Apache-2.0): bulk protocol, AES framing, PD event format.
- ChargerLab's "KM003C/002C Protocol Trigger by Virtual Serial Port" sheet (installed with the POWER-Z software).
- Linux `drivers/hwmon/powerz.c`: ADC layout cross-check.

## License

MIT. See [LICENSE](LICENSE).
