# km003c-mcp

MCP server for the **ChargerLab POWER-Z KM003C** USB-C power meter. It exposes
measurements, high-rate VBUS/IBUS streaming, USB PD capture with decoding, and the
meter's fast-charge protocol trigger as MCP tools (stdio transport).

## Install

```powershell
python -m venv .venv
.venv\Scripts\pip install -e .[dev]
```

Requires Python 3.11+. On Windows the meter's vendor interface already uses the WinUSB
driver, so no extra driver is needed; `libusb-package` ships the libusb DLL.

**Close the ChargerLab POWER-Z PC software first** — only one program can hold the
meter's USB interface. The server reports `DeviceBusyError` otherwise, with the likely
holders; on macOS it also names the holding process from `ioreg`, for example
`pid 46945, python3.14`. By default the server keeps the meter claimed until it exits. Set
`KM003C_IDLE_RELEASE_S`, or call `device_release`, to let another program use the meter
while this server keeps running.

### macOS and Linux

```sh
uv sync --extra dev
```

The server loads libusb-1.0 in this order: the file named by `KM003C_LIBUSB`, the copy
bundled with `libusb-package`, then the system library through pyusb's normal search.
On macOS, including Apple Silicon, the bundled copy works without a driver or `sudo`.
If it does not load, install Homebrew's libusb (`brew install libusb`); pyusb finds
`/opt/homebrew/lib/libusb-1.0.dylib` on its own. On Linux, the user also needs read and
write access to the meter's USB device node, usually through a udev rule for
`5FC9:0063`.

The trigger port is found by USB VID and PID, so on macOS it is the meter's
`/dev/cu.usbmodem*` device even when other USB serial devices are attached. Register the
server with Claude Code through `uv`:

```sh
claude mcp add km003c -- uv --directory <repo> run km003c-mcp
```

## Register with Claude Code

```powershell
claude mcp add km003c -- <repo>\.venv\Scripts\km003c-mcp.exe
```

## Configuration (environment variables)

| Variable | Purpose |
|---|---|
| `KM003C_SERIAL` | Pick a meter by USB serial number when several are attached |
| `KM003C_COM_PORT` | Override the trigger's virtual serial port (e.g. `COM14`) |
| `KM003C_IDLE_RELEASE_S` | Release the meter's USB interface this many seconds after the last tool call, PD capture, or stream that uses it ends; the next call claims it again. `0` releases at once. Unset, or `never`, keeps the meter claimed until the server exits |
| `KM003C_PD_POLL_MS` | PD capture poll interval in milliseconds, 1 to 10000 (default 50; the meter buffers a whole negotiation between polls) |
| `KM003C_LIBUSB` | Load libusb-1.0 from this file instead of the bundled one (`KM003C_LIBUSB_DLL` is the older name and still works) |

## Tools

| Tool | What it does |
|---|---|
| `device_info` | Model, hardware/firmware version, serial, trigger COM port |
| `measure` | VBUS/IBUS/power, CC1/CC2, D+/D−, VDD, temperature; optional averaging |
| `pd_status` | PD-side snapshot (VBUS, IBUS, CC1, CC2) |
| `stream_start` / `stream_status` / `stream_samples` / `stream_stop` | Background sampling at 2/10/50/1000 SPS with min/max/mean, energy (Wh), charge (mAh), dropped-sample count and optional CSV |
| `pd_capture_start` / `pd_capture_read` / `pd_capture_stop` | Background PD sniffing (start enables the meter's PD monitor and reports the outcome; stop disables it): attach/detach and the full USB PD R3.1 message set decoded - PDOs/RDOs (incl. PPS, AVS, EPR), PD 3.x data objects, extended messages with chunk reassembly, structured VDMs (Discover Identity incl. cable VDOs, SVIDs, DisplayPort and Thunderbolt 3 alt modes) |
| `device_release` | Release the meter's USB interface now so another program can claim it; refused while a PD capture or stream runs |
| `trigger_detect` / `trigger_enter` | Protocol detection (`entry list`, `entry list+`) and entering a protocol |
| `trigger_pd_source_caps` / `trigger_pd_request` | Read the charger's PDOs and request a fixed/PPS/AVS contract |
| `trigger_fast_charge` | QC2/QC3/FCP/AFC/SFCP/SCP/VFCP/TFCP/UFCS voltage requests |
| `trigger_reset` / `trigger_close` / `trigger_raw` | Reset, leave trigger mode, or send any documented command |

Trigger requests make the charger raise VBUS on the meter's output — disconnect
sensitive loads first. Positive IBUS means current flows from the meter's USB-C
receptacle (input) to its plug (output).

## Tests

```powershell
.venv\Scripts\pytest                      # unit tests (no hardware)
$env:KM003C_HW="1"; .venv\Scripts\pytest tests\test_hw.py   # against a real meter
```

## Protocol sources

- [okhsunrog/km003c-protocol-research](https://github.com/okhsunrog/km003c-protocol-research) (MIT) and
  [okhsunrog/km003c-rs](https://github.com/okhsunrog/km003c-rs) (MIT/Apache-2.0): bulk protocol, AES framing, PD event format.
- ChargerLab's "KM003C/002C Protocol Trigger by Virtual Serial Port" sheet (installed with the POWER-Z software).
- Linux `drivers/hwmon/powerz.c`: ADC layout cross-check.

## License

MIT — see [LICENSE](LICENSE).
