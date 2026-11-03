English | [简体中文](bluetooth-multiplexing-exploration.zh-CN.md)

# Bluetooth Multi-Line Exploration

Date of the record: 2026-11-01. This document is a research log. It records all the tests that tried to get "many phones in calls at the same time, with two-way audio" on the Mac built-in Bluetooth controller. It shows each approach, the result of each step and the final conclusion.

The document adds the tests and the binary analysis of the evening of 2026-11-01 (about 19:50–22:30) to the earlier test records and protocol research. Section 4 gives a summary of the earlier results.

The document does not contain real device addresses, serial numbers or test phone numbers. The raw logs are not in the repository.

## Current decision

Decision of the project (2026-11-01): calls on more than one line at the same time are left for later research. The product supports only one line for now.

- The product supports **one call at a time**. Many phones can pair and stay connected, but only one call has audio at a time.
- Calls on many lines at the same time are not in the current scope. This document is a start point for later research and a list of approaches that failed, so that nobody repeats them.
- If the work starts again, read section 11 first. Then decide which constraint to relax.

## 1. Final conclusions (read first)

1. **On the built-in Bluetooth controller of this Mac, two HFP calls cannot be full-duplex at the same time.** Both eSCO links can be set up, and both receive without problems. But when the host sends call audio on both links, the controller firmware crashes (`bluetoothd` log: `Firmware crash detected`). All variables that the host can change were tested one by one and removed as causes.
2. **The root cause is the Apple design.** In normal Apple use, the controller sends call audio over I2S/PCM directly to the audio processor of the Mac. This is a physical audio interface, and it carries one audio stream at a time. The "SCO over HCI" path that this project uses is the diagnostic path of the Apple tool BlueTool. On this path, the firmware is stable only with one host-sent stream.
3. **The Bluetooth protocol does not limit the number of lines.** HFP 1.9 and Core 5.4 both let one Central connect to many phones and set up one eSCO link for each. The limit comes from this chip and its firmware, not from the protocol.
4. **"Many phones connected at the same time" and "many call audio streams at the same time" are different.** Many phones can keep HFP connections at the same time on this Mac (dial, hang up, state notifications). Many two-way call audio streams at the same time are not possible.
5. **All remaining possible directions are outside this chip**, for example one external Bluetooth controller for each phone. A similar open-source gateway (Asterisk chan_mobile) uses this method.

## 2. Limits of the exploration

Design requirements of this round:

- Continue on the Mac built-in controller. Do not use Linux. Do not add an external adapter.
- Research time-division or frequency-division multiplexing, as in 4G.
- Installing the necessary tools is permitted, but the user experience must not become worse. Link-level turn-taking of audio (approach A, see 5.4) is not acceptable.
- No adaptation for each platform. Do not assume iOS.
- The gateway must connect again to paired phones by itself.

The resulting limits:

- Use only the built-in controller of this Mac. No Linux host, no external adapter.
- The approach must work for all phone platforms. Use only classic HFP. Do not use features specific to iOS or Android.
- The user experience must not become worse: each line must have full, simultaneous, two-way call audio for all of the call. No turn-taking.

## 3. Test setup and pass criteria

| Item | Value |
|---|---|
| Host | Mac16,7, arm64, macOS 26.4 (25E246) |
| Controller | BCM_4388C2, PCIe (Apple ACIPC / Skywalk channel), firmware 23.5.224.1467 (the version for this logic board) |
| Host stack | Bumble 0.0.236 + the ACIPC C bridge of this project (`server/mac/native/acipc_bridge.c`) |
| Phones | Pixel 10 (controlled through wireless ADB), iPhone |
| POC | A separate scratch script `open_pairing.py` (not in the repository, not the product service) |
| Calls | Both phones call the same test number. The downlink is silence of the same length. |

Pass criteria for one round (the same as in the earlier tests):

- While both eSCO links exist, **the receive count of each line increases every second**, and the send count of each line that the host sends on also increases, for 15 seconds without a stop.
- No EIO (`-5`), and `bluetoothd` reports no firmware crash (a `log stream` monitor runs in the background all the time).
- Cleanup is normal, and the calls on both phones end.

The counts show only the packet flow at the HCI level. They are not a listening test.

## 4. Earlier results (summary)

The full earlier records are not in the repository. This is a summary.

| Stage | Result |
|---|---|
| One line | Pixel and iPhone each sent and received for 15 seconds on one line without problems. With the Mac as Central, one Pixel line was also correct. |
| Early two-line tests | Two eSCO handles (6/7) were set up, but they flowed only for a short time. The old check looked only at total counts and reported a false pass. It now checks each second. |
| Two Peripheral roles (Mac is Peripheral to both phones) | When the second line started to flow, the first line stopped. Then the connection timed out. |
| Two Central roles (Mac is Central to both phones) | About 0.2 seconds after the second line started to flow, both lines stopped at the same time. |
| System log | The two-Central failure matched `Firmware crash detected` in `bluetoothd`. The crash fields were all zero, and there was no core dump. |
| Protocol research | HFP 1.9 / Core 5.4 do not prohibit two eSCO links. `Total_Num_Synchronous_Data_Packets=1` is the number of shared send buffers, not a limit on the number of connections. |

At the start of this round, the open question was: **what causes the firmware crash, and can the host prevent it?**

## 5. All approaches of this round and their results

The approaches are in the order of the tests. In the tables, "A" is the line whose eSCO link was set up first (Pixel). "B" is the line set up second (iPhone).

### 5.1 Find the trigger: is host send necessary?

| Round | Setting | Result |
|---|---|---|
| none | S1, the host sends no SCO on either line | **Pass**: in 15 seconds, A received 4000 packets and B received 3779 packets. No bad packets, no crash. |
| all (control) | S1, on each line the host sends one packet for each received packet | After about 1 second, both lines stopped at the same time. Firmware crash. |

Conclusion: **Host-sent SCO is the trigger of the firmware crash.** Only this one variable was different between the two rounds.

Side problem: in the first attempt, the POC delayed the SCO accept (to start both lines at the same time). During the wait, the Pixel did not reply to `AT+CLCC`, and the query timed out. A `deferAudio=false` switch was added. The POC now accepts SCO immediately.

### 5.2 Buffers

**Approach: configure the SCO buffers as Apple does**

- Clue: at each start, `bluetoothd` sends the vendor command `BD_VSC_SCO_BUFFER_SIZE_CONTROL`. Its log reads back `controllerBufferSize=1423 controllerNumBuffer=12`. With standard HCI, the POC reads only 254 bytes × 1 synchronous buffer.
- Static analysis of `bluetoothd`: the ACI version is 0xFE01 (4-byte parameters). The BCM version is 0xFE6E (5-byte parameters).
- Test: 0xFE01 returned `0x12` (invalid parameters) for all 80 parameter sets. 0xFE6E with `00 00 00 00 00` returned `00 00 8f05 0c`: buffer size 1423, 12 buffers, the same as the Apple log. After this command, standard HCI reads back 0/0 SCO buffers.

| Round | Result |
|---|---|
| Pixel, one line | Pass, 3997 packets sent and received |
| Two lines | Firmware crash. B sent and received only 10 packets. |

Conclusion: **The buffer configuration is not the cause.** This controller accepts the BCM vendor commands.

**Approach: synchronous flow control + shared credit scheduling**

- Design: turn on HCI synchronous flow control. All handles share the 1 controller buffer. Send the next packet only after Number Of Completed Packets. The two lines take turns fairly. Each line has a bounded queue (when full, drop the oldest packet). The scheduler passed offline tests.
- Test: the write of the flow-control setting gave no error, but it read back as 0.

Conclusion: **This controller does not support synchronous flow control.** There are no completed events, so the credit approach cannot work.

### 5.3 Send timing and codec

**Approach: global send pacer**

- A timer replaces the completed events. The packets of all handles go into one queue. Two packets are at least 1.5 ms apart. Only 1 packet is in flight at a time. Offline test: 400 packets, minimum gap 1.53 ms, no loss, 200 packets for each line.
- Test: full send on both lines. B sent 29 packets, then the firmware crashed.

Conclusion: **The cause is not two packets in the same buffer.**

**Approach: host-side CVSD pass-through (S1T)**

- Hypothesis: the controller has only one PCM-to-CVSD encoder. Two decoders (receive) are correct, but two encoders (send) crash.
- Method: keep the eSCO air parameters (S1). Set the host-side format to CVSD, 8 bits for each sample, 8000 bytes/s, PCM format "not applicable", so that the controller does no transcoding. The host sends CVSD silence bytes `0x55`.
- Check: the first received packet was 30 bytes (in PCM mode it is 60 bytes). Thus pass-through was really active.

| Round | Result |
|---|---|
| Pixel, one line | Pass, 3999 packets sent and received |
| Two lines | Firmware crash. B sent only 15 packets. |

Conclusion: **The controller transcoder is not the cause.**

**Approach: S4 parameters (recommended by the HFP specification)**

- 2-EV3 packets, maximum latency 12 ms. Negotiated result: interval 12 slots (7.5 ms), 60-byte air packets, 120-byte host packets. Each line uses half of the air slots of S1.

| Round | Result |
|---|---|
| Pixel, one line | Pass |
| Two lines | About 10 ms after the eSCO link of B was set up, the firmware crashed. B received no packets. |

Conclusion: **The air scheduling load is not the cause.** With S4, the failure came even faster.

### 5.4 Time division

One direction was time or frequency division, as in 4G. On the air, Bluetooth already uses time division (625 µs slots) and frequency division (hopping over 79 channels). The chip firmware schedules this, and the host cannot control it. The crash is on the host send side, so time division can only be done on the host side.

**Approach: only one line sends**

| Round | Setting | Result |
|---|---|---|
| first | Only A sends, B only receives | Passed the first time. A later round in the same state (the first window of tdm5000) crashed. Not stable. |
| second | Only B sends, A only receives | Pass |

**Approach: change the send right for each packet (alternate)**

- The two lines send on alternate packets. The total send rate is the same as one line.
- Result: A stopped after 41 received packets. B stayed correct. No crash.

**Approach: set up silently, then send together (holdTxUntilAll)**

- Hypothesis: the crash occurs when the second eSCO link is set up while the first line sends.
- Method: set up both eSCO links, wait 1.5 seconds in silence, then start to send on both lines together.
- Result: after about 70 ms (about 20 packets on each line), the firmware crashed.

Conclusion: **Host send on two lines at the same time causes the crash by itself.** The setup time has no effect.

**Approach: change the send right in time windows (tdm)**

| Round | Setting | Result |
|---|---|---|
| tdm5000 (1st) | 5000 ms window, and also try the 0xFC83 routing command | 0xFC83 returned `0x01` (unknown command), so the route did not change. After the change to B, A stopped and B was correct. No crash. |
| tdm5000 (2nd) | 5000 ms window, observe only, no fail on stop | After the change to B, B sent 34 packets. Then both lines stopped at the same time. Firmware crash. |

Conclusion: **The same change gives different results** (sometimes only the old line is pushed out, sometimes all the firmware crashes). Host-side time division of the send right is not reliable.

**Approach: link-level turn-taking (approach A, rejected)**

- Both phones keep the HFP connection. Only one eSCO link exists at a time. The gateway removes and sets up the link again when necessary. One change takes about 0.2–0.5 seconds.
- This works on this Mac, but only one line has audio at a time. The line that waits cannot hear or speak. This makes the user experience worse, so it is rejected.

**Frequency division**

- If the Mac is Central to one phone and Peripheral to the other, the two links use different hopping sequences.
- The earlier two-Peripheral test already failed. Also, the crash is on the host send path, not on the air. Thus this was not tested separately.

### 5.5 Apple controller configuration

**Approach: skip HCI Reset and keep the configuration from `bluetoothd`**

- Result: the synchronous buffers still read back as 254 × 1 (not 0/0 as after the Apple configuration). Thus the controller was already reset when the POC took control, and the configuration cannot be kept. To prevent one more crash, no two-line round was run.

**Approach: set the SCO route as Apple does**

- Static analysis of `/usr/sbin/BlueTool` and `bluetoothd`:
  - The ACI command group of BlueTool has `-t` (SCO over HCI), `-p` (SCO over PCM), `-S` (SCO over HCI test setting) and `-o hcivoice/pcmvoice`.
  - The ACI routing command is 0xFE10 with the parameters `<route> 78 ff 01 ff` (`00` is PCM, `01` is HCI). The BCM version is 0xFC1C (`SET_SCO_PATH`).
  - **In normal Apple use, SCO goes over I2S/PCM to the audio processor** (`setScoRouteI2S16K`, `setScoRouteI2S8K`, "Highland Park I2S to PCM route selected. Forcing CVSD."). "SCO over HCI" is the diagnostic path of BlueTool.
  - The "maximum number of SCO connections" of `bluetoothd` comes from runtime configuration. Static analysis cannot read the value.
- Test: 0xFE10 returned `0x12`. 0xFC1C with `01 04 00 00 00` (route to the transport) returned `00`. The controller accepted the configuration.
- Two-line result: B sent 13 packets, then the firmware crashed.

Conclusion: **With an explicit HCI route, the crash still occurs.** In the firmware, the HCI path is designed for one line only. The normal path is one physical I2S interface.

### 5.6 Rejected platform-specific approach

**Approach C: LE Audio for Android, HFP for iPhone**

- Basis: the chip supports LE Audio (it has an `LEA` service and an `iso` pipe). The Pixel 10 has the BAP unicast client turned on. Bumble has PACS/ASCS/BAP/CAP modules. LE Audio call audio uses ISO channels and does not go through the SCO firmware code.
- Reason for rejection: iOS does not open LE Audio calls to third-party devices. This approach needs a different adaptation for each platform. The approach must work on all platforms, so it was not done. No files were changed.

## 6. Summary table

| Approach | Test | Conclusion |
|---|---|---|
| No send on either line | Two lines passed for 15 seconds | Receive is not the problem, but the other party hears nothing |
| Original configuration, both lines send | Crash | Host send is the trigger |
| Apple buffer configuration 0xFE6E | One line passed, two lines crashed | Not a buffer problem |
| Synchronous flow control + credit | Not supported by the controller | Cannot be done |
| Global pacer, 1.5 ms | Crash | Not a packet overlap problem |
| CVSD pass-through (no transcoding) | One line passed, two lines crashed | Not a transcoder problem |
| S4 (2-EV3) | One line passed, two lines crashed faster | Not an air load problem |
| Only one line sends | Sometimes passes, sometimes fails | Not stable |
| Alternate packets | First line stopped | The send right is taken away |
| Silent setup, then send together | Crash | Simultaneous send causes it by itself |
| 5-second window turn-taking | Once the first line stopped, once a crash | Host-side time division is not reliable |
| 0xFC83 voice route change | Unknown command | Not supported by the controller |
| Skip HCI Reset | Configuration not kept | No effect |
| 0xFC1C set HCI route | Accepted, two lines still crashed | The HCI path supports one line only |
| Link-level turn-taking (A) | Works, but only one line has audio | Rejected |
| LE Audio + HFP (C) | Not done | Platform-specific, rejected |

## 7. Comparison with external sources

- **Asterisk chan_mobile** (an open-source GSM gateway that uses HFP to make phones into telephone lines; the nearest use case to this project): the official documentation says that each adapter can connect to one device at a time. The author tried one adapter with many devices and found that it does not work. The recommended method is one adapter for each device. One user connected 4 phones to a Raspberry Pi with 4 Bluetooth modules.
- **TI CC256x**: the data sheet says that one piconet can have at most 2 SCO links, and users report that this works. This is a capability that the chip vendor promises in the data sheet. The chip of this Mac has no such public promise.
- **Android Automotive**: supports HFP connections to many phones at the same time, but an app must connect the call audio manually. The default dialer app does not support calls on many phones at the same time.
- No public record shows two call audio streams on Apple built-in Bluetooth.

Links:

- [Asterisk chan_mobile documentation](https://gitea.sysmocom.de/sysmocom/asterisk/src/commit/461dc0546591c3b421d916d6428171b19afa95d1/doc/tex/chan-mobile.tex)
- [Asterisk Mobile Channel features](https://docs.asterisk.org/Configuration/Channel-Drivers/Mobile-Channel/Mobile-Channel-Features/)
- [TI E2E: Dual concurrent SCO connections](https://e2e.ti.com/support/wireless-connectivity/bluetooth-group/bluetooth/f/bluetooth-forum/359567/dual-concurrent-sco-connections)
- [TI E2E: cc2564moda dual sco esco problem](https://e2e.ti.com/support/wireless-connectivity/bluetooth-group/bluetooth/f/bluetooth-forum/926993/cc2564moda-dual-sco-esco-problem)
- [Android Automotive Bluetooth](https://source.android.com/docs/automotive/ivi_connectivity)

## 8. Results that you can use again

**POC connects again by itself (no manual phone connection each time)**

- At start, set `PHONE_MAC_AUTOCONNECT=<address1>,<address2>`. Every 10 seconds, the POC checks the phones. For a phone that is not connected, the Mac starts the connection.
- Sequence: the Mac starts ACL → the existing connection handler does authentication and encryption → SDP finds the HFP AG channel of the phone → open RFCOMM → the same SLC setup as when the phone connects.
- `POST /connect {"device": "<address>"}` starts it manually.
- Test: both phones connected automatically and HFP became ready. No action on the phones was necessary.
- Note: the Pixel sometimes changes the Mac to Peripheral. If necessary, call `POST /role` to change back to Central.
- The first version of the reconnect failed once: two authentications started on the same connection, and the controller rejected the second. Now only the existing handler does authentication.

**POC test switches (body of `POST /test`)**

| Parameter | Function |
|---|---|
| `txMode` | `all` / `none` / `first` / `second` / `alternate` / `tdm` |
| `esco` | `S1`–`S4`, and the host-side CVSD pass-through versions `S1T`–`S4T` |
| `deferAudio` | Delay the SCO accept or not |
| `paceMs`, `maxQueue` | Gap of the global pacer and queue length of each line |
| `tdmMs`, `routeVsc` | Length of the time window; send 0xFC83 at each change or not |
| `holdTxUntilAll` | Start to send only after both lines are set up and 1 second of silence |
| `observeOnly` | Do not fail on a stop; run for all 15 seconds |

Start environment variables: `PHONE_MAC_SCO_VSC`, `PHONE_MAC_SCO_VSC_SWEEP`, `PHONE_MAC_SCO_VSC_OPCODE` (SCO buffer command), `PHONE_MAC_STARTUP_VSC` (vendor commands to send at start; only 0xFE10, 0xFE6E and 0xFC1C are permitted), `PHONE_MAC_SCO_FLOW` (synchronous flow control), `PHONE_MAC_SKIP_RESET`.

**Controller vendor commands confirmed on this Mac (BCM_4388C2)**

| Command | Opcode | Result on this Mac |
|---|---|---|
| SCO buffer configuration (BCM) | 0xFE6E, `00 00 00 00 00` | Accepted, returns 1423 × 12 |
| SCO buffer configuration (ACI) | 0xFE01 | Invalid parameters (0x12) |
| SCO route (BCM `SET_SCO_PATH`) | 0xFC1C, `01 04 00 00 00` | Accepted |
| SCO route (ACI) | 0xFE10, `01 78 ff 01 ff` | Invalid parameters (0x12) |
| Dynamic voice route change | 0xFC83 | Unknown command (0x01) |
| Write synchronous flow control | Standard HCI | No error, but reads back 0 |

## 9. Operation notes

- **After each firmware crash**, `bluetoothd` enters a restart loop of about 13 seconds, `blueutil --power` reads back 255, and the POC cannot take control of the controller. `sudo pkill bluetoothd` does not fix this. Turn on Bluetooth once in Control Center to recover. Then turn off system Bluetooth with `blueutil --power 0` and start the POC.
- **Do not turn on Bluetooth on the Mac during tests.** AI-Phone and the Mac system Bluetooth use the same controller and the same address. When system Bluetooth turns on, `bluetoothd` takes the iPhone connection and also makes the native channel of the POC unusable. After this, the iPhone can keep the service list of the Mac system in its cache, and it disconnects when it connects to AI-Phone again. Turn Bluetooth off and on again on the iPhone to clear the cache. Do not use "Forget This Device".
- **After a firmware crash, the calls on the phones do not end automatically.** Check each phone: on the Pixel, hang up through ADB; on the iPhone, check manually.

## 10. Evidence and file locations

The per-round test records, the raw logs (`round-*.log`, `round-*-state.json`, `poc-*.log`, `crash-monitor.log`), the static analysis of `bluetoothd` / BlueTool, the POC source `open_pairing.py` and its offline tests (`test_sched.py` for credit scheduling, `test_pace.py` for the pacer) are in a temporary directory outside the repository. They are not published.

All code changes of this round were in the scratch directory. No product code was changed. The raw logs can contain device addresses and similar data, so they are not in the repository.

## 11. Next directions

With the constraints "only the built-in controller of this Mac, all platforms, no worse user experience", no approach with evidence remains. To get calls on many lines at the same time, one of the constraints must be relaxed:

| Direction | Description |
|---|---|
| Add external Bluetooth controllers | One controller for each phone. Each controller runs only one line. One line is already stable, and this works for all phone platforms. First confirm that the SCO data path of an external controller (USB isochronous) works with macOS + Bumble. A chip that officially supports many SCO links is also an option. |
| Change the host platform | Linux is not considered for now. |

The POC reconnect and the test switches can be used again in each of these directions.
