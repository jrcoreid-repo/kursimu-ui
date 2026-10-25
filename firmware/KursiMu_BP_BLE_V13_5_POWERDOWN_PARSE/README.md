# KursiMu BP BLE V13.5 — UART power-down parser

Use **KursiMu_BP_BLE_V13_5_POWERDOWN_PARSE.ino** in this directory.

Based on V13.4, keeping BLE protocol, result persistence/ACK, D4 status LED, D5 100 ms power-button pulse, and the single 1500 ms post-result OFF pulse unchanged.

**Root cause from the physical BP module:** it emits `v_b` heartbeats **without line endings** while active. The shutdown token `MSG_POWER_DOWN` can be concatenated to those bytes. Matching a newline-terminated string with `startsWith` (and even a substring search if the buffer overflows first) is insufficient.

This revision adds a streaming byte matcher that recognizes the complete exact `MSG_POWER_DOWN` token without waiting for CR/LF and resets the buffered fragment to avoid duplicate events. Existing result parsing stays in place. It also detects `start test` after leading `v_b` bytes and logs `index=1,done=1` as preparation evidence, **not** as a new confirmed session. Raw UART remains accessible with `RAW ON`/`RAW OFF` via USB 115200 baud.

Expected after a successful measurement:

```
[BP] RESULT SAVED
[CTRL] D5 HIGH: POST_RESULT_OFF
[POWER] OFF PULSE SENT ONCE; AWAIT MSG_POWER_DOWN
MSG_POWER_DOWN
[POWER] OFF CONFIRMED BY UART
[BP] POWER DOWN
```

Wait until the OFF confirmation before attempting measurement #2. If the confirmation never arrives, the power state remains safely locked; no automatic second D5 pulse is issued. Test first on an **unworn cuff** and confirm Arduino compilation and the actual UART trace before use with people.

**Note:** GitHub CI checks Python tests, not Arduino compilation or hardware behavior.
