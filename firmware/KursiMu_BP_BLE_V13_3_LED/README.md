# KursiMu BP BLE V13.3 — D4 Low-Power LED

Source: `KursiMu_BP_BLE_V13_3_LED.ino`, based on the exact user-supplied **V13.2** firmware.

## Wiring (Seeed XIAO ESP32-C3)

- **D4 = GPIO6** -> **series resistor 1 kΩ–2.2 kΩ** -> LED anode (+)
- LED cathode (-) -> **GND**
- Active HIGH. Do **not** connect a bare LED without a current-limiting resistor.
- Single-color external LED only. Reverse polarity or an active-LOW module will require changing `STATUS_LED_ON`/`STATUS_LED_OFF` in the sketch.

The resistor determines LED power draw. For example, assuming 3.3 V GPIO and a 2.1 V green LED, a 1 kΩ resistor gives about 1.2 mA **while lit**. Average current is much lower because flashes are rare and short.

## Status pattern

| Event | D4 LED |
| --- | --- |
| Boot finished initializing firmware | two quick 35 ms flashes |
| BLE disconnected / advertising | one 25 ms flash per 6 seconds |
| BLE connected, idle | two 25 ms flashes per 5 seconds |
| BLE START accepted | three 35 ms flashes |
| Actual BP pump/start event | two 40 ms flashes |
| Inflating / holding / waiting for result | one 30 ms flash per 1.8 seconds |
| BP result persisted to NVS | two 100 ms flashes |
| BP error / timeout | three 110 ms flashes |

Event patterns override the passive heartbeat and do not modify the measurement state, BP button output, NVS counters, UART parser, or BLE protocol.

## Critical unchanged behavior

- D5 / GPIO7: **100 ms** BP button pulse; boot auto-pulse **1500 ms** after MCU boot.
- D7 / GPIO20: RX-only BP UART at **115200 baud**.
- BLE UUIDs, packets, result ACK/recovery, counter storage unchanged.
- No auto START retries, no automatic BP OFF toggle, no extra FreeRTOS task.

## Arduino IDE

Open **`KursiMu_BP_BLE_V13_3_LED.ino`** under its matching folder. Select **Seeed XIAO ESP32C3** board, and use **NimBLE-Arduino 2.x** as with V13.2. Verify/compile in Arduino IDE and test LED/serial before attaching the cuff.

**Safety test:** ensure no unexpected D5 pulse, repeated pump start, session-count increment, or BLE disconnect occurs after upgrading. The LED is an indicator, not a medical-device alarm.

The firmware was checked against the V13.2 source structure, but **not yet compiled on the user's Arduino toolchain or tested on physical hardware**.
