# Test fixtures

All three files come from one ChargerLab POWER-Z KM003C on firmware 2.0.0, hardware 2.3,
between a 140 W USB PD charger and a USB PD sink board.

| File | What it is |
|---|---|
| `payloads_fw2.0.0.json` | Raw PdPacket attribute payloads, one per GetData poll, from two captures of the same cold attach at about 14.6 million ms of meter uptime: `poll_15ms` polled every 15 ms, `poll_1000ms` every 1000 ms. The capture tests replay them through the fake meter. |
| `golden_15ms.jsonl` | The capture file that replaying the 15 ms payloads produces, with host times blanked. Regenerate it with `KM003C_UPDATE_GOLDEN=1` after a deliberate format change. |
| `cold_attach_fw2.0.0.jsonl` | A capture file written by this server during a real cold attach at about 40.1 million ms of uptime, past 2^24 ms, stopped on `contract` with a 4 s settle time. It shows that connection events carry the full 32-bit uptime. It is the file as written, except for the serial and the label. |

The meter's serial number is replaced by the placeholder `000000` in every fixture and test.
