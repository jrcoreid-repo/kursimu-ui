# KursiMu BP BLE V13.4 — Controlled Post-Result Power-Off

New firmware file: `KursiMu_BP_BLE_V13_4_AUTO_OFF.ino`.

Based on V13.3 including the D4/GPIO6 low-power status LED. The existing BLE protocol/UUIDs, UART RX (D7/GPIO20), NVS results, session counters, CRC, 100 ms D5/GPIO7 pulse and 1500 ms boot pulse remain unchanged.

## After a successful BP measurement

1. Validate actual BP result and persist it in NVS as before. Notify the PC as before.
2. Mark post-result OFF as pending. Wait **1500 ms** without blocking UART/BLE.
3. If no spontaneous `MSG_POWER_DOWN` arrived, issue **one** guarded 100 ms D5 toggle.
4. Listen for UART `MSG_POWER_DOWN`; only this confirms **P_OFF** and permits another START.
5. If no confirmation within **5000 ms**, do **not retry** D5. Mark power unknown and block START until an actual power-down message arrives.

**Why:** D5 is a toggle (ON/OFF), not a dedicated OFF pin. This prevents the first UKUR AKHIR command from switching an already-on device OFF; it also prevents double-toggling the physical monitor.

Log indicators:
- `[POWER] OFF PULSE SENT ONCE; AWAIT MSG_POWER_DOWN`
- `[POWER] OFF CONFIRMED BY UART`
- `[POWER] OFF NOT CONFIRMED; NO RETRY; START LOCKED`

The PC may temporarily receive `ACK_NOT_READY` if START is pressed before confirmed shutdown. Do not click START repeatedly. If the power state is uncertain, inspect USB UART logs and physically verify the BP module.

## Hardware verification before use

- Verify physical post-measurement power down, UART `MSG_POWER_DOWN`, absence of duplicate D5 pulses.
- Confirm the device does not begin another measurement unless powered off.
- Test valid and failed measurements separately; failed measurements do not trigger the new post-result OFF sequence.
- Verify no unintended cuff inflation in bench tests without a person wearing the cuff.

**Note:** GitHub Python CI does not compile Arduino firmware or validate the physical BP module. V13.4 is an integration candidate pending Arduino IDE compilation and hardware testing, not a medically certified final release.
