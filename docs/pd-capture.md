# PD capture files

A PD capture writes a file in the `km003c-capture` format, version 1: JSON Lines, one
record per line, each written and flushed as it arrives, so a crash keeps everything
before it. The file holds the meter's raw bytes and only a header summary of each
message. Full decodes are computed when the file is read, so decoder fixes apply to old
captures and the files stay small.

This document covers the capture tools of the km003c-mcp server, the file format, the read
modes, and the `km003c-capture` command-line tool. The [README](../README.md) covers
installation, configuration, and the other tools.

## Starting, waiting, and stopping

`pd_capture_start(path?, label?, provenance?, stop_after_messages?, stop_on?, settle_ms=0,
timeout_s=60, status_interval_ms=100)` starts a capture and returns the file's absolute
path. Start the capture before the stimulus: the meter records only what happens after
its PD monitor is enabled.

- `path` names the file, or a directory when it exists or ends with `/`. Without it the
  file goes in `KM003C_CAPTURE_DIR`, else `./captures`, named from the UTC start time and
  the label. An existing file is never overwritten.
- `label` and `provenance` (any JSON object, such as the charger, the cable, and the
  firmware under test) go into the header.
- `stop_on` is `none`, `contract` (a PS_RDY on SOP after an Accept of a Request), or
  `identity` (a Discover Identity ACK on SOP, from either port).
- `stop_after_messages` stops after that many non-GoodCRC messages on any SOP.
- `settle_ms` keeps capturing for that long after the condition is met. A charger may send
  its own Discover Identity seconds after PS_RDY, so `stop_on="identity"` or a contract
  capture that should include identity needs a settle time.
- `timeout_s` always ends the capture. A condition met during its settle time keeps its own
  reason; otherwise the reason is `timeout`.
- `status_interval_ms` sets how often a status record is written. Every poll that carries
  events also writes one.

`settle_ms` and `status_interval_ms` count meter time, taken from the status blocks, so a
replayed capture stops at the same record; `timeout_s` counts host time.

`pd_capture_wait(timeout_s=60)` blocks until the capture stops or the timeout passes and
returns the capture's summary, with `running` saying which happened. `pd_capture_mark(text)`
writes an annotation, such as "stimulus GSrC sent". `pd_capture_stop()` stops the capture
and returns its summary. A capture holds its claim on the meter until it stops; reading a
file never claims the meter.

When the server receives SIGTERM, it stops a running capture with footer reason `manual`,
stops a running stream, releases the meter, and exits with status 0. It exits after 10 s
even if a cleanup step hangs.

## Record types

| `type` | Fields |
|---|---|
| `header` | `format` (`km003c-capture`), `version` (1), `started_utc`, `meter` {`serial`, `fw_version`, `hw_version`, `model`}, `server` {`version`, `poll_ms`}, `pd_monitor` {`enabled`, `accepted`, `outcome`, `attempts`, `last_reply`}, `label`, `provenance`, `stop` (the stop options) |
| `status` | `t_host`, `t_meter_ms`, `vbus_mv`, `ibus_ma`, `cc1_mv`, `cc2_mv`, `raw` (the 12-byte status block) |
| `pd` | `i`, `t_meter_ms`, `sop`, `wire` (the message bytes), `raw_event` (size flag, timestamp, SOP byte, and wire), and the header summary: `msg`, `msg_id`, `power_role` and `data_role` (SOP), `cable_plug` (SOP' and SOP''), `spec_rev`, `num_objects`, `extended` |
| `conn` | `t_meter_ms`, `code` (the raw connection code: 0x11 and 0x12 on firmware 2.0.0, 0x21 and 0x22 on others), `event` (`connect`, `disconnect`, or `connection` for an unknown code), `raw_event` |
| `mark` | `t_host`, `text` |
| `error` | `t_host`, `kind`, `message`, and `payload` (the PdPacket hex) when a payload failed to parse |
| `footer` | `stopped_utc`, `reason` (`messages`, `contract`, `identity`, `timeout`, `manual`, or `error`), `counts` by message type, `messages` (non-GoodCRC), `conn`, `marks`, `errors`, `vbus_mv` {`first`, `last`, `min`, `max`} over every poll |

`t_host` is Unix time in seconds; `t_meter_ms` is the meter's uptime in milliseconds, a
32-bit value that PD messages, connection events, and status blocks all carry in full.
Marks and errors have only `t_host`; readers place them in meter time from the nearest
status record. The footer is written on every stop, errors included. A payload that
fails to parse becomes an `error` record and the capture continues; ten failed polls in a
row end it with reason `error`.

## Reading a capture

`pd_capture_read(path?, mode="summary", sop?, types?, since?, after?, limit?, include_goodcrc=False)`
reads a file without touching the meter. Without `path` it reads this session's running
or last capture; a bare file name is also looked up in the capture directory. Every
decoded value sits beside the wire hex it came from.

| Mode | Returns |
|---|---|
| `summary` (default) | At most about 2 KB plus the file's path, which is never shortened: one line per non-GoodCRC message, such as `t=14637562 SOP Request pos 4, 3.0 A [8210 2cb18441]`, plus connection events, marks, and errors; counts by message type; VBUS first, last, min, and max; the final contract; and warnings. When the lines do not fit, lines from the middle are dropped, `truncated` is true, and a marker line names the omitted range |
| `messages` | The same lines, filtered and paged; `limit` defaults to 50. Pass a page's `next_after` as `after` for the next page; `next_after` is absent on the last page |
| `facts` | What a register cross-check needs: Source_Capabilities PDOs, each Request RDO with its object position, the PDO it names, and its Accept and PS_RDY times, the final contract, and Discover Identity ACKs grouped by SOP and by sender (power and data role on SOP, cable or port on SOP'), each VDO beside its decode. At most about 8 KB: a long capture keeps the latest of each kind, always including the final contract's Source_Capabilities and Request and the latest ACK of each SOP and sender, and `omitted` counts the rest |
| `full` | Complete decodes of the filtered messages, each with its `line`; `limit` defaults to 20, and paging works as in `messages` |

Filters: `sop` (`SOP`, `SOP'`, `SOP''`), `types` (message type names, VDM command names
such as `Discover_Identity`, and `connect`, `disconnect`, `mark`, `error`), `since` (keeps
entries at or after that meter time in ms; a mark or error without an estimated time drops
out), and `include_goodcrc`.

Pages advance by position, not by time: `after` is a line number in the file, exclusive,
and each page returns the `next_after` to pass next. Marks and errors carry times estimated
from the host clock, which need not be in order with the meter's, and several messages can
share a millisecond, so paging by `since` would repeat or skip entries. Use `since` to pick
a window and `after` to walk through it.

The summary and facts warnings implement the checks to make before comparing a capture
with anything else:

- No Source_Capabilities, no Request, and VBUS flat (within 500 mV): no negotiation
  happened in the window, which points at the stimulus, not the protocol.
- Only connection events: either the partners exchanged no PD messages, for example a
  source that does not use PD, or the meter's PD monitoring was not active. Firmware
  2.0.0 captures PD with the monitor off, so on that firmware the first is the likely
  cause. The warning quotes the header's `pd_monitor`.
- A Request with no Source_Capabilities before it: the capture started late.

Warnings also report a missing footer (a running or crashed capture), a timeout before
the stop condition, error records, and unreadable lines.

## Command-line tool

`km003c-capture` shares this code. `summarize` and `facts` read files only and never open
the meter, so they work where USB access is blocked.

The install puts `km003c-capture` in the project's virtual environment. On macOS and Linux,
prefix each command below with `uv run`, or activate `.venv` first. On Windows, run
`.venv\Scripts\km003c-capture.exe`.

Print the summary of a capture file:

```bash
km003c-capture summarize captures/km003c-20261123T083005Z.jsonl
```

Print its facts:

```bash
km003c-capture facts captures/km003c-20261123T083005Z.jsonl
```

Record a new capture into the `captures/` directory:

```bash
km003c-capture record --stop-on contract --timeout 120 --label "cold attach" captures/
```

`record` takes the same options as `pd_capture_start` (`--stop-on`, `--stop-after-messages`,
`--settle-ms`, `--timeout`, `--status-interval-ms`, `--label`, `--provenance`). It prints
`capturing <path>` once capturing has begun, so a script can start it in the background and
prompt for the stimulus after that line. It prints the summary when the capture stops.
Ctrl-C or SIGTERM stops the capture with reason `manual` within about 0.2 s, once a start
in progress has finished; further ones change nothing while the footer is written and the
summary printed.

| Exit code | Meaning |
|---|---|
| 0 | Success: the capture stopped on its condition, by hand, or at the timeout when it had no condition |
| 1 | The capture ended with an error, or the meter could not be used |
| 2 | An unreadable file or a bad argument |
| 3 | `record` reached `--timeout` before `--stop-on` or `--stop-after-messages` was met |
