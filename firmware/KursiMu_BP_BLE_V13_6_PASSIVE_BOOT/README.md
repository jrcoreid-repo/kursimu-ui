# KursiMu BP BLE V13.6 — passive ESP32 boot

**Purpose:** eliminate spontaneous pump inflation caused by an ESP32-side boot D5 toggle. Based on V13.5: byte-stream `MSG_POWER_DOWN` parsing, BLE, post-result OFF, NVS and LED are retained.

## Changed
- ESP32 boot initializes D5 LOW, but **does not pulse D5** at startup.
- The boot-origin automatic `addQas` path is removed. An unrequested `start test` can still be logged as unsolicited; software cannot physically stop a BP module that independently starts.
- A BLE START is the only ordinary path to request a measurement ON pulse. Post-result OFF remains one guarded D5 pulse.
- A request is rejected with `NOT_READY` if UART has already confirmed the BP is ON, because D5 would otherwise switch it OFF.

Expected boot serial:
```
[BOOT] PASSIVE READY - NO D5 PULSE
[BOOT] WAITING FOR PC START
```
There should be **no** `[CTRL] D5 HIGH: BOOT_AUTOSTART`.

## Bench test before human use
1. Flash and reboot ESP32 with the cuff **unworn**. Ensure it does not pulse D5 or start the pump for 60 seconds without any GUI request.
2. Connect BLE; click UKUR once and confirm `[CTRL] D5 HIGH: BLE START` and pump activity.
3. Complete a measurement and confirm exactly one `POST_RESULT_OFF` and `OFF CONFIRMED BY UART`.
4. Try second measurement and verify `ACK=0` on first attempt.
5. Repeat cold boots and compare unsolicited pump incidents to previous 2/4.

A physical pump that starts on its own independently of ESP32 cannot be fully prevented by this software-only change. Do not use on a person until bench tests pass. GitHub CI is for Python; Arduino compilation and hardware testing remain necessary.
