
#include <Arduino.h>
#include <Preferences.h>
#include <NimBLEDevice.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <ctype.h>
#include <string.h>

// =====================================================
// KURSIMU BP BLE V13.5 - CONTROLLED POST-RESULT POWER OFF
// LEGACY POWER + RELIABLE BLE
// INTEGRATION CANDIDATE
//
// Board: Seeed XIAO ESP32-C3
// NimBLE-Arduino 2.x
//
// D4 / GPIO6  : Active-HIGH status LED (through 1k-2.2k resistor)
// D5 / GPIO7  : BP button driver
// D7 / GPIO20 : BP UART RX
// BP UART TX  : Disabled (RX only)
//
// Boot:
//   Wait 1500ms -> one 100ms D5 pulse
//
// Python START:
//   One 100ms D5 pulse
//
// Boot pulse DOES NOT set M_STARTING.
// Session created only upon BP measurement
// event, never simply upon button press.
//
// Compatible with KursiMu Python BLE V2.
// =====================================================

// ================= HARDWARE ==========================

#define STATUS_LED_PIN 6
#define STATUS_LED_ON HIGH
#define STATUS_LED_OFF LOW
#define BP_BUTTON_PIN 7
#define BP_RX_PIN 20

#define USB_BAUD 115200
#define BP_BAUD 115200

#define HEX_COUNT 14
#define MAX_UART_LINE 384

// ================= BUTTON TIMING =====================

const uint32_t BOOT_DELAY_MS = 1500;
const uint32_t PULSE_MS = 100;
const uint32_t PULSE_GUARD_MS = 700;
const uint32_t AUTO_OFF_SETTLE_MS = 1500;
const uint32_t AUTO_OFF_CONFIRM_MS = 5000;

// ================= SESSION TIMING ====================

const uint32_t START_TIMEOUT_MS = 30000;
const uint32_t MEASURE_TIMEOUT_MS = 180000;
const uint32_t RESULT_TIMEOUT_MS = 30000;

const uint32_t HEARTBEAT_MS = 2000;
const uint32_t RESULT_RESEND_MS = 3000;

// ================= BLE UUIDs =========================

#define BLE_NAME "KURSIMU-BP-C3"

#define SERVICE_UUID \
"7b973ce0-73f1-4c92-879f-91a395f6e101"

#define COMMAND_UUID \
"7b973ce0-73f1-4c92-879f-91a395f6e102"

#define RESPONSE_UUID \
"7b973ce0-73f1-4c92-879f-91a395f6e103"

#define RESULT_UUID \
"7b973ce0-73f1-4c92-879f-91a395f6e104"

#define STATUS_UUID \
"7b973ce0-73f1-4c92-879f-91a395f6e105"

// ================= BINARY PROTOCOL ===================

const uint8_t MAGIC = 0xA5;
const uint8_t VERSION = 1;

enum Opcode : uint8_t {
  OP_PING = 1,
  OP_START = 2,
  OP_STATUS = 3,
  OP_GET_RESULT = 4,
  OP_ACK_RESULT = 5
};

enum AckCode : uint8_t {
  ACK_OK = 0,
  ACK_BUSY = 1,
  ACK_DUPLICATE = 2,
  ACK_INVALID = 3,
  ACK_NOT_FOUND = 4,
  ACK_WRONG_SESSION = 5,
  ACK_NOT_READY = 6,
  ACK_RESULT_PENDING = 7
};

enum MeasureState : uint8_t {
  M_IDLE = 0,
  M_STARTING = 1,
  M_INFLATING = 2,
  M_HOLDING = 3,
  M_WAIT_RESULT = 4,
  M_COMPLETE = 5,
  M_ERROR = 6
};

enum PowerState : uint8_t {
  P_UNKNOWN = 0,
  P_ON = 1,
  P_OFF = 2
};

enum SessionOrigin : uint8_t {
  ORIGIN_UNKNOWN = 0,
  ORIGIN_BOOT = 1,
  ORIGIN_BLE = 2,
  ORIGIN_UNSOLICITED = 3
};

// ================= COMMAND QUEUE =====================

struct BLECommand {
  uint8_t frame[14];
  uint8_t length;
};

QueueHandle_t bleQueue = nullptr;

// ================= GLOBAL OBJECTS ====================

Preferences prefs;

NimBLECharacteristic* chCommand = nullptr;
NimBLECharacteristic* chResponse = nullptr;
NimBLECharacteristic* chResult = nullptr;
NimBLECharacteristic* chStatus = nullptr;

volatile bool bleConnected = false;
volatile bool connectionChanged = false;

// ================= BOOT STATE ========================

uint32_t bootStartedAt = 0;
uint32_t bootPulseAt = 0;

bool bootPulseDone = false;

// ================= BUTTON STATE ======================

bool buttonActive = false;
bool buttonEverPressed = false;

uint32_t buttonPressedAt = 0;
uint32_t buttonReleasedAt = 0;

// Single post-result OFF request; no blind repeated toggle.
bool autoOffPending = false;
bool autoOffSent = false;
uint32_t resultSavedAt = 0;
uint32_t autoOffSentAt = 0;

// ================= MEASUREMENT =======================

uint8_t measureState = M_IDLE;
uint8_t powerState = P_UNKNOWN;

uint8_t activeOrigin = ORIGIN_UNKNOWN;

bool sessionOpen = false;
bool endReceived = false;
bool hexReceived = false;

uint32_t startRequestedAt = 0;
uint32_t sessionStartedAt = 0;
uint32_t endReceivedAt = 0;

uint32_t sessionId = 0;

int bpSYS = -1;
int bpDIA = -1;
int bpPULSE = -1;
int bpERR = -1;

// ================= PERSISTENT COUNTERS ===============

uint32_t totalSessions = 0;
uint32_t successSessions = 0;
uint32_t failedSessions = 0;

uint32_t lastStartRequestId = 0;

// ================= RESULT / BLE FRAMES ===============

uint8_t resultFrame[16] = {0};
uint8_t responseFrame[16] = {0};
uint8_t statusFrame[20] = {0};

bool resultStored = false;

uint32_t lastHeartbeatAt = 0;
uint32_t lastResultSentAt = 0;

// ================= UART / USB ========================

String uartLine = "";

// Detect the full shutdown token across un-terminated v_b heartbeats.
// A byte-wise matcher avoids dependence on CR/LF and buffer overflow.
const char POWER_DOWN_TOKEN[] = "MSG_POWER_DOWN";
uint8_t powerDownMatch = 0;
String usbLine = "";

bool rawEcho = false;

uint32_t rxBytes = 0;
uint32_t rxLines = 0;
uint32_t badPackets = 0;
uint32_t lineOverflows = 0;

// =====================================================
// D4 STATUS LED - LOW POWER, NON-BLOCKING
//
// GPIO6 -> resistor (1k..2.2k) -> LED anode -> LED cathode -> GND
// Active HIGH; no continuous-on state.
// IDLE disconnected: 1 x 25ms / 6000ms
// IDLE connected:    2 x 25ms / 5000ms
// Measuring:         1 x 30ms / 1800ms
// BLE START accepted: 3 x 35ms
// Pump detected:      2 x 40ms
// Result saved:       2 x 100ms
// Error / timeout:    3 x 110ms
// No delay(), NVS, BLE messages, interrupts, or extra FreeRTOS task.
// =====================================================

bool ledLit = false;
uint8_t ledPulsesLeft = 0;
uint16_t ledOnMs = 25;
uint16_t ledGapMs = 95;
uint32_t ledSwitchedAt = 0;
uint32_t ledLastActivityAt = 0;

bool ledPreviousConnected = false;
bool ledPreviousBootDone = false;
uint8_t ledPreviousState = M_IDLE;

void ledStartPattern(uint8_t count, uint16_t onMs, uint16_t gapMs) {
  if (!count) return;
  ledPulsesLeft = count;
  ledOnMs = onMs;
  ledGapMs = gapMs;
  ledSwitchedAt = millis();
  ledLastActivityAt = ledSwitchedAt;
  ledLit = true;
  digitalWrite(STATUS_LED_PIN, STATUS_LED_ON);
}

void updateStatusLed() {
  uint32_t now = millis();

  // Observe EXISTING state only. Never modify BP, session or BLE control.
  // State events take precedence over connection-indicator events.
  if (measureState != ledPreviousState) {
    ledPreviousState = measureState;
    ledPreviousConnected = bleConnected;
    if (measureState == M_STARTING) {
      ledStartPattern(3, 35, 85);  // START command accepted
    } else if (measureState == M_INFLATING) {
      ledStartPattern(2, 40, 90);  // Physical pump confirmed
    } else if (measureState == M_COMPLETE) {
      ledStartPattern(2, 100, 120);  // NVS result saved
    } else if (measureState == M_ERROR) {
      ledStartPattern(3, 110, 125);  // Timeout/error
    }
  } else if (bleConnected != ledPreviousConnected) {
    ledPreviousConnected = bleConnected;
    ledStartPattern(bleConnected ? 2 : 1, 35, 115);
  }

  if (bootPulseDone != ledPreviousBootDone) {
    ledPreviousBootDone = bootPulseDone;
    // No new LED pattern required; preserve measurement/connection events.
  }

  // Advance short pulses without blocking BLE processing or BP UART RX.
  if (ledLit) {
    if ((uint32_t)(now - ledSwitchedAt) >= ledOnMs) {
      digitalWrite(STATUS_LED_PIN, STATUS_LED_OFF);
      ledLit = false;
      ledSwitchedAt = now;
      --ledPulsesLeft;
    }
  } else if (ledPulsesLeft > 0) {
    if ((uint32_t)(now - ledSwitchedAt) >= ledGapMs) {
      ledLit = true;
      digitalWrite(STATUS_LED_PIN, STATUS_LED_ON);
      ledSwitchedAt = now;
    }
  } else {
    const bool activeMeasurement =
        measureState == M_STARTING ||
        measureState == M_INFLATING ||
        measureState == M_HOLDING ||
        measureState == M_WAIT_RESULT;
    const uint32_t period = activeMeasurement ? 1800U
                          : bleConnected ? 5000U : 6000U;
    if ((uint32_t)(now - ledLastActivityAt) >= period) {
      if (activeMeasurement) ledStartPattern(1, 30, 100);
      else if (bleConnected) ledStartPattern(2, 25, 110);
      else ledStartPattern(1, 25, 110);
    }
  }
}

// =====================================================
// CRC16 CCITT-FALSE
// =====================================================

uint16_t crc16(
  const uint8_t* data,
  size_t length
) {
  uint16_t crc = 0xFFFF;

  for (size_t i = 0; i < length; i++) {
    crc ^= (uint16_t)data[i] << 8;

    for (int j = 0; j < 8; j++) {
      if (crc & 0x8000) {
        crc = (uint16_t)(
          (crc << 1) ^ 0x1021
        );
      } else {
        crc = (uint16_t)(crc << 1);
      }
    }
  }

  return crc;
}

void put16(uint8_t* p, uint16_t value) {
  p[0] = (uint8_t)value;
  p[1] = (uint8_t)(value >> 8);
}

void put32(uint8_t* p, uint32_t value) {
  p[0] = (uint8_t)value;
  p[1] = (uint8_t)(value >> 8);
  p[2] = (uint8_t)(value >> 16);
  p[3] = (uint8_t)(value >> 24);
}

uint32_t get32(const uint8_t* p) {
  return (uint32_t)p[0]
       | ((uint32_t)p[1] << 8)
       | ((uint32_t)p[2] << 16)
       | ((uint32_t)p[3] << 24);
}

void finishFrame(uint8_t* frame, size_t size) {
  put16(
    frame + size - 2,
    crc16(frame, size - 2)
  );
}

bool validFrame(
  const uint8_t* frame,
  size_t size
) {
  if (size < 4) return false;

  if (frame[0] != MAGIC ||
      frame[1] != VERSION) {
    return false;
  }

  uint16_t expected =
      (uint16_t)frame[size - 2]
    | ((uint16_t)frame[size - 1] << 8);

  return expected == crc16(
    frame,
    size - 2
  );
}

// =====================================================
// BUTTON: LEGACY 100ms PULSE
// =====================================================

bool canPulse() {
  if (buttonActive) return false;

  if (buttonEverPressed &&
      (uint32_t)(
        millis() - buttonReleasedAt
      ) < PULSE_GUARD_MS) {
    return false;
  }

  return true;
}

bool pressBPButton(const char* reason) {
  if (!canPulse()) return false;

  digitalWrite(BP_BUTTON_PIN, HIGH);

  buttonActive = true;
  buttonEverPressed = true;
  buttonPressedAt = millis();

  // Physical outcome is not assumed.
  powerState = P_UNKNOWN;

  Serial.print("[CTRL] D5 HIGH: ");
  Serial.println(reason);

  return true;
}

void updateButton() {
  if (!buttonActive) return;

  if ((uint32_t)(
    millis() - buttonPressedAt
  ) >= PULSE_MS) {

    digitalWrite(BP_BUTTON_PIN, LOW);

    buttonActive = false;
    buttonReleasedAt = millis();

    Serial.println("[CTRL] D5 LOW");
  }
}

// =====================================================
// BOOT AUTOSTART
// Important: DOES NOT create a session.
// =====================================================

void updateBoot() {
  if (bootPulseDone) return;

  if ((uint32_t)(
    millis() - bootStartedAt
  ) < BOOT_DELAY_MS) {
    return;
  }

  if (!pressBPButton("BOOT_AUTOSTART")) {
    return;
  }

  bootPulseDone = true;
  bootPulseAt = millis();

  // FIX V13.2:
  // No M_STARTING here.
  // No session counter change.
  // No START timeout.
  measureState = M_IDLE;

  Serial.println(
    "[BOOT] TOGGLE COMPLETE"
  );

  Serial.println(
    "[BOOT] STATE IDLE - WAITING FOR PC"
  );
}

// =====================================================
// RESULT STORAGE
// =====================================================

bool hasPendingResult() {
  return resultStored &&
         ((resultFrame[3] & 0x01) != 0) &&
         ((resultFrame[3] & 0x02) == 0);
}

uint32_t storedResultSession() {
  return get32(resultFrame + 4);
}

void makeEmptyResult(
  uint8_t* frame
) {
  memset(frame, 0, 16);

  frame[0] = MAGIC;
  frame[1] = VERSION;
  frame[2] = 0x82;

  finishFrame(frame, 16);
}

bool persistResult(
  const uint8_t* frame
) {
  return prefs.putBytes(
    "lastresult",
    frame,
    16
  ) == 16;
}

void loadResult() {
  makeEmptyResult(resultFrame);

  resultStored = false;

  if (prefs.getBytesLength("lastresult") != 16) {
    Serial.println("[NVS] NO RESULT");
    return;
  }

  uint8_t saved[16] = {0};

  size_t size = prefs.getBytes(
    "lastresult",
    saved,
    16
  );

  if (size != 16 ||
      !validFrame(saved, 16) ||
      saved[2] != 0x82 ||
      !(saved[3] & 0x01)) {

    Serial.println("[NVS] INVALID RESULT");
    return;
  }

  memcpy(resultFrame, saved, 16);
  resultStored = true;

  Serial.printf(
    "[NVS] LAST RESULT SESSION=%lu\n",
    (unsigned long)storedResultSession()
  );

  Serial.println(
    hasPendingResult()
      ? "[NVS] RESULT PENDING PC ACK"
      : "[NVS] RESULT ALREADY ACKED"
  );

  // Recover monotonic session identity.
  if (storedResultSession() > totalSessions) {
    totalSessions = storedResultSession();
    prefs.putUInt("sessions", totalSessions);
  }
}

// =====================================================
// BLE RESULT CHARACTERISTIC
// =====================================================

void updateResultCharacteristic() {
  if (!chResult) return;

  if (hasPendingResult()) {
    chResult->setValue(
      resultFrame, 16
    );
  } else {
    uint8_t empty[16];
    makeEmptyResult(empty);
    chResult->setValue(empty, 16);
  }
}

void notifyResult() {
  if (!hasPendingResult() ||
      !bleConnected ||
      !chResult) {
    return;
  }

  updateResultCharacteristic();

  chResult->notify();
  lastResultSentAt = millis();
}

// =====================================================
// BLE STATUS
// =====================================================

bool measurementBusy() {
  return measureState == M_STARTING ||
         measureState == M_INFLATING ||
         measureState == M_HOLDING ||
         measureState == M_WAIT_RESULT ||
         sessionOpen;
}

uint32_t reportedSession() {
  // 0 means a START was requested,
  // but no pump session exists yet.
  if (measureState == M_STARTING &&
      !sessionOpen) {
    return 0;
  }

  return sessionId;
}

void updateStatus() {
  memset(statusFrame, 0, 20);

  statusFrame[0] = MAGIC;
  statusFrame[1] = VERSION;
  statusFrame[2] = 0x83;

  statusFrame[3] = measureState;
  statusFrame[4] = powerState;

  uint8_t flags = 0;

  // RESULT means NEW, unacknowledged result.
  if (hasPendingResult()) {
    flags |= 0x01;
    flags |= 0x02;
  }

  if (bleConnected) flags |= 0x04;

  // BLE controller ready.
  flags |= 0x08;

  if (buttonActive) flags |= 0x10;

  // START locked only during boot.
  if (!bootPulseDone) flags |= 0x20;

  // Boot pulse completed.
  if (bootPulseDone) flags |= 0x40;

  statusFrame[5] = flags;

  put32(
    statusFrame + 6,
    reportedSession()
  );

  put32(
    statusFrame + 10,
    totalSessions
  );

  put32(
    statusFrame + 14,
    successSessions
  );

  finishFrame(statusFrame, 20);

  if (chStatus) {
    chStatus->setValue(
      statusFrame, 20
    );
  }
}

void notifyStatus() {
  updateStatus();

  if (bleConnected && chStatus) {
    chStatus->notify();
  }
}

// =====================================================
// BLE RESPONSE
// =====================================================

void sendResponse(
  uint32_t requestId,
  uint8_t ack
) {
  memset(responseFrame, 0, 16);

  responseFrame[0] = MAGIC;
  responseFrame[1] = VERSION;
  responseFrame[2] = 0x81;
  responseFrame[3] = ack;

  put32(
    responseFrame + 4,
    requestId
  );

  put32(
    responseFrame + 8,
    reportedSession()
  );

  responseFrame[12] = measureState;
  responseFrame[13] = powerState;

  finishFrame(responseFrame, 16);

  if (chResponse) {
    chResponse->setValue(
      responseFrame, 16
    );

    if (bleConnected) {
      chResponse->notify();
    }
  }

  Serial.printf(
    "[BLE] REQ=%lu ACK=%u SESSION=%lu\n",
    (unsigned long)requestId,
    (unsigned)ack,
    (unsigned long)reportedSession()
  );
}

// =====================================================
// BLE CALLBACKS
// =====================================================

class BPServerCallbacks
  : public NimBLEServerCallbacks {

  void onConnect(
    NimBLEServer* server,
    NimBLEConnInfo& info
  ) override {

    bleConnected = true;
    connectionChanged = true;

    Serial.println("[BLE] CONNECTED");
  }

  void onDisconnect(
    NimBLEServer* server,
    NimBLEConnInfo& info,
    int reason
  ) override {

    bleConnected = false;
    connectionChanged = true;

    Serial.printf(
      "[BLE] DISCONNECTED reason=%d\n",
      reason
    );
  }
};

class BPCommandCallbacks
  : public NimBLECharacteristicCallbacks {

  void onWrite(
    NimBLECharacteristic* characteristic,
    NimBLEConnInfo& info
  ) override {

    std::string value =
      characteristic->getValue();

    BLECommand cmd = {};

    if (value.length() == 14) {
      cmd.length = 14;

      memcpy(
        cmd.frame,
        value.data(),
        14
      );
    } else {
      cmd.length = 0;

      if (value.length() >= 8) {
        memcpy(
          cmd.frame,
          value.data(),
          8
        );
      }
    }

    if (bleQueue) {
      if (xQueueSend(
        bleQueue,
        &cmd,
        0
      ) != pdTRUE) {

        Serial.println(
          "[BLE] COMMAND QUEUE FULL"
        );
      }
    }
  }
};

BPServerCallbacks serverCallbacks;
BPCommandCallbacks commandCallbacks;

// =====================================================
// BLE INITIALIZATION
// =====================================================

void setupBLE() {
  NimBLEDevice::init(BLE_NAME);

  NimBLEServer* server =
    NimBLEDevice::createServer();

  server->setCallbacks(&serverCallbacks);
  server->advertiseOnDisconnect(true);

  NimBLEService* service =
    server->createService(SERVICE_UUID);

  chCommand = service->createCharacteristic(
    COMMAND_UUID,
    NIMBLE_PROPERTY::WRITE
  );

  chResponse = service->createCharacteristic(
    RESPONSE_UUID,
    NIMBLE_PROPERTY::READ |
    NIMBLE_PROPERTY::NOTIFY
  );

  chResult = service->createCharacteristic(
    RESULT_UUID,
    NIMBLE_PROPERTY::READ |
    NIMBLE_PROPERTY::NOTIFY
  );

  chStatus = service->createCharacteristic(
    STATUS_UUID,
    NIMBLE_PROPERTY::READ |
    NIMBLE_PROPERTY::NOTIFY
  );

  chCommand->setCallbacks(
    &commandCallbacks
  );

  memset(responseFrame, 0, 16);

  responseFrame[0] = MAGIC;
  responseFrame[1] = VERSION;
  responseFrame[2] = 0x81;

  finishFrame(responseFrame, 16);

  chResponse->setValue(
    responseFrame, 16
  );

  updateResultCharacteristic();
  updateStatus();

  service->start();

  NimBLEAdvertising* advertising =
    NimBLEDevice::getAdvertising();

  advertising->enableScanResponse(true);
  advertising->setName(BLE_NAME);
  advertising->addServiceUUID(SERVICE_UUID);

  bool ok =
    NimBLEDevice::startAdvertising();

  Serial.printf(
    "[BLE] ADVERTISING=%s\n",
    ok ? "OK" : "FAILED"
  );
}

// =====================================================
// SESSION MANAGEMENT
// =====================================================

void resetMeasurementData() {
  endReceived = false;
  hexReceived = false;

  bpSYS = -1;
  bpDIA = -1;
  bpPULSE = -1;
  bpERR = -1;
}

void beginMeasurement(
  const char* source
) {
  if (sessionOpen) return;

  uint32_t now = millis();

  // Determine origin.
  if (measureState == M_STARTING) {
    activeOrigin = ORIGIN_BLE;

  } else if (bootPulseDone &&
             (uint32_t)(
               now - bootPulseAt
             ) < START_TIMEOUT_MS) {

    activeOrigin = ORIGIN_BOOT;

  } else {
    activeOrigin = ORIGIN_UNSOLICITED;
  }

  // A session only exists after an actual
  // BP measurement event has been seen.
  uint32_t nextId = totalSessions + 1;

  // Extremely unlikely overflow protection.
  if (nextId == 0) {
    Serial.println(
      "[NVS] SESSION COUNTER OVERFLOW"
    );
    measureState = M_ERROR;
    return;
  }

  if (prefs.putUInt(
    "sessions", nextId
  ) != sizeof(uint32_t)) {

    Serial.println(
      "[NVS] WARNING: SESSION NOT PERSISTED"
    );
  }

  totalSessions = nextId;
  sessionId = nextId;

  sessionOpen = true;

  resetMeasurementData();

  sessionStartedAt = now;

  measureState = M_INFLATING;
  powerState = P_ON;

  Serial.println();
  Serial.println("============================");

  Serial.printf(
    "[BP] SESSION #%lu\n",
    (unsigned long)sessionId
  );

  Serial.printf(
    "[BP] ORIGIN=%u\n",
    (unsigned)activeOrigin
  );

  Serial.print("[BP] START FLAG: ");
  Serial.println(source);

  Serial.println("============================");

  notifyStatus();
}

void failMeasurement(
  const char* reason
) {
  if (!sessionOpen) return;

  sessionOpen = false;
  measureState = M_ERROR;

  failedSessions++;

  prefs.putUInt(
    "failed", failedSessions
  );

  Serial.print("[BP] FAILED: ");
  Serial.println(reason);

  notifyStatus();
}

// =====================================================
// RESULT CONFIRMATION
// =====================================================

void confirmMeasurement() {
  if (!sessionOpen ||
      !endReceived ||
      !hexReceived ||
      bpERR != 0) {
    return;
  }

  // Never overwrite undelivered result.
  if (hasPendingResult()) {
    failMeasurement(
      "Previous result pending ACK"
    );
    return;
  }

  uint8_t newFrame[16] = {0};

  newFrame[0] = MAGIC;
  newFrame[1] = VERSION;
  newFrame[2] = 0x82;

  // bit0 = valid
  newFrame[3] = 0x01;

  // bit2 = boot-origin or unsolicited
  if (activeOrigin != ORIGIN_BLE) {
    newFrame[3] |= 0x04;
  }

  put32(
    newFrame + 4,
    sessionId
  );

  // Verified legacy mapping
  newFrame[8] = (uint8_t)bpSYS;
  newFrame[9] = (uint8_t)bpDIA;
  newFrame[10] = (uint8_t)bpPULSE;
  newFrame[11] = 0;

  finishFrame(newFrame, 16);

  // Durable storage BEFORE notifying PC.
  if (!persistResult(newFrame)) {
    failMeasurement(
      "NVS result save failed"
    );
    return;
  }

  memcpy(resultFrame, newFrame, 16);

  resultStored = true;

  sessionOpen = false;
  measureState = M_COMPLETE;

  // Pump was known ON during measurement. Request exactly one OFF
  // toggle after an NVS-persisted result and a settling interval.
  // Physical OFF is confirmed exclusively by MSG_POWER_DOWN.
  powerState = P_ON;
  autoOffPending = true;
  autoOffSent = false;
  resultSavedAt = millis();

  successSessions++;

  prefs.putUInt(
    "success", successSessions
  );

  Serial.println();
  Serial.println("============================");
  Serial.println("      BP RESULT SAVED");
  Serial.println("============================");

  Serial.printf(
    "SESSION : %lu\n",
    (unsigned long)sessionId
  );

  Serial.printf(
    "SYS     : %d mmHg\n",
    bpSYS
  );

  Serial.printf(
    "DIA     : %d mmHg\n",
    bpDIA
  );

  Serial.printf(
    "PULSE   : %d BPM\n",
    bpPULSE
  );

  Serial.printf(
    "ORIGIN  : %u\n",
    (unsigned)activeOrigin
  );
  Serial.println("ERR     : 0");
  Serial.println("============================");

  notifyResult();
  notifyStatus();

  // No automatic OFF toggle.
}

// =====================================================
// STRICT HEX14 DECODER
// =====================================================

bool parseHex14(
  const String& line,
  uint8_t* output
) {
  int pos = 0;
  int count = 0;
  int length = line.length();

  while (pos < length) {
    while (
      pos < length &&
      (line[pos] == ' ' ||
       line[pos] == '\t')
    ) {
      pos++;
    }

    if (pos >= length) break;

    int start = pos;

    while (
      pos < length &&
      line[pos] != ' ' &&
      line[pos] != '\t'
    ) {
      pos++;
    }

    if (pos - start != 2 ||
        count >= HEX_COUNT) {
      return false;
    }

    if (!isxdigit(
          (unsigned char)line[start]
        ) ||
        !isxdigit(
          (unsigned char)line[start + 1]
        )) {
      return false;
    }

    String token =
      line.substring(start, pos);

    output[count++] =
      (uint8_t)strtoul(
        token.c_str(),
        nullptr,
        16
      );
  }

  return count == HEX_COUNT;
}

bool plausibleBP(
  const uint8_t* p
) {
  int s = p[0];
  int d = p[1];
  int hr = p[3];

  // Sanity checks only.
  // Not clinical validation.
  return s >= 40 &&
         s <= 255 &&
         d >= 20 &&
         d <= 200 &&
         hr >= 25 &&
         hr <= 240 &&
         s > d;
}

void decodeBPResult(
  const uint8_t* packet
) {
  if (!sessionOpen || hexReceived) {
    return;
  }

  if (!plausibleBP(packet)) {
    badPackets++;

    Serial.println(
      "[BP] HEX14 VALUES INVALID"
    );
    return;
  }

  bpSYS = packet[0];
  bpDIA = packet[1];
  bpPULSE = packet[3];

  hexReceived = true;

  Serial.println("[BP] HEX14 VALID");

  if (endReceived && bpERR == 0) {
    confirmMeasurement();
  }
}

// =====================================================
// BP UART EVENT PARSER
// =====================================================

bool isSoc3(const String& line) {
  int eq = line.indexOf('=');

  if (eq < 0) return false;

  String key = line.substring(0, eq);
  String value = line.substring(eq + 1);

  key.trim();
  value.trim();

  return key == "soc" && value == "3";
}

void processBPLine(String line) {
  line.trim();

  if (!line.length()) return;

  rxLines++;

  if (line.indexOf("memory full!") >= 0) {
    return;
  }

  // ------------ POWER DOWN ----------------
  // A stream matcher also recognizes shutdown without a newline.
  // Keep the substring fallback for complete UART lines.

  if (line.indexOf("MSG_POWER_DOWN") >= 0) {
    powerState = P_OFF;
    if (autoOffPending) {
      Serial.println("[POWER] OFF CONFIRMED BY UART");
      autoOffPending = false;
      autoOffSent = false;
    }

    Serial.println("[BP] POWER DOWN");

    if (measureState == M_STARTING) {
      measureState = M_ERROR;

      Serial.println(
        "[BP] START ENDED IN POWER DOWN"
      );
    }

    if (sessionOpen &&
        (measureState == M_INFLATING ||
         measureState == M_HOLDING)) {

      failMeasurement(
        "Power down during measurement"
      );
    }

    notifyStatus();
    return;
  }

  // ------------ WAKE EVENT ----------------

  if (line.startsWith("port wake up")) {
    powerState = P_ON;

    Serial.println("[BP] WAKE EVENT");

    notifyStatus();
    return;
  }

  // Pre-measurement indication only; not authoritative until start test.
  if (line.indexOf("index=1,done=1") >= 0) {
    Serial.println("[BP] PREPARE INDICATOR index=1,done=1");
  }

  // ------------ PUMP START ----------------

  if (isSoc3(line) ||
      line.indexOf("start test") >= 0 ||
      line.indexOf("startBPTest") >= 0) {

    if (!sessionOpen) {
      beginMeasurement("soc=3/start test");
    }

    return;
  }

  // ------------ HOLDING -------------------

  if (line == "addQas") {
    if (!sessionOpen) {
      if (measureState == M_STARTING ||
          (bootPulseDone &&
           (uint32_t)(
             millis() - bootPulseAt
           ) < START_TIMEOUT_MS)) {

        beginMeasurement("addQas");
      }
    }

    if (sessionOpen) {
      measureState = M_HOLDING;
      powerState = P_ON;

      Serial.println("[BP] HOLDING");

      notifyStatus();
    }

    return;
  }

  // ------------ END TEST ------------------

  if (line.startsWith("end test,err:")) {
    String value =
      line.substring(
        line.indexOf(':') + 1
      );

    value.trim();

    if (!value.length()) return;

    for (
      size_t i = 0;
      i < value.length();
      i++
    ) {
      if (!isdigit(
        (unsigned char)value[i]
      )) {
        return;
      }
    }

    // Do not create a session from END alone.
    // Need pump/holding evidence.
    if (!sessionOpen || endReceived) {
      Serial.println(
        "[BP] END WITHOUT ACTIVE SESSION"
      );
      return;
    }

    bpERR = value.toInt();

    endReceived = true;
    endReceivedAt = millis();

    Serial.printf(
      "[BP] END err=%d\n",
      bpERR
    );

    if (bpERR != 0) {
      failMeasurement("BP module error");
      return;
    }

    if (hexReceived) {
      confirmMeasurement();

    } else {
      measureState = M_WAIT_RESULT;

      Serial.println(
        "[BP] WAITING FOR HEX14"
      );

      notifyStatus();
    }

    return;
  }

  // ------------ HEX14 RESULT -------------

  uint8_t packet[HEX_COUNT];

  if (parseHex14(line, packet)) {
    decodeBPResult(packet);
  }
}

// =====================================================
// UART RX ONLY
// =====================================================

void readBPUART() {
  while (Serial1.available() > 0) {
    int incoming = Serial1.read();

    if (incoming < 0) break;

    uint8_t b = (uint8_t)incoming;

    rxBytes++;

    if (rawEcho) {
      Serial.write(b);
    }

    // The BP prints "v_b" without newlines. Detect shutdown as a
    // byte stream: v_bv_bMSG_POWER_DOWN must work even if the line
    // buffer never terminates or previously overflowed.
    if (b == (uint8_t)POWER_DOWN_TOKEN[powerDownMatch]) {
      powerDownMatch++;
      if (POWER_DOWN_TOKEN[powerDownMatch] == '\0') {
        powerDownMatch = 0;
        uartLine = "";
        processBPLine(String(POWER_DOWN_TOKEN));
        continue;  // CR/LF, if any, will be consumed normally.
      }
    } else {
      powerDownMatch = (b == (uint8_t)POWER_DOWN_TOKEN[0]) ? 1 : 0;
    }

    if (b == '\r' || b == '\n') {
      if (uartLine.length() > 0) {
        processBPLine(uartLine);
        uartLine = "";
      }

    } else if (
      uartLine.length() < MAX_UART_LINE
    ) {
      uartLine += (char)b;

    } else {
      uartLine = "";
      lineOverflows++;

      Serial.println(
        "[UART] LINE OVERFLOW"
      );
    }
  }
}

// =====================================================
// START COMMAND
// =====================================================

uint8_t handleStart(
  uint32_t requestId
) {
  if (requestId == 0) {
    return ACK_INVALID;
  }

  if (requestId == lastStartRequestId) {
    return ACK_DUPLICATE;
  }

  // Wait for the initial boot pulse.
  if (!bootPulseDone) {
    return ACK_NOT_READY;
  }

  if (hasPendingResult()) {
    return ACK_RESULT_PENDING;
  }

  // Never issue START during post-result power-down: D5 is a toggle.
  if (autoOffPending || (measureState == M_COMPLETE && powerState != P_OFF)) {
    return ACK_NOT_READY;
  }

  if (measurementBusy()) {
    return ACK_BUSY;
  }

  if (!canPulse()) {
    return ACK_BUSY;
  }

  // Persist request identity before pulse.
  // Duplicate IDs cannot retrigger D5.
  if (prefs.putUInt(
    "lastreq", requestId
  ) != sizeof(uint32_t)) {

    return ACK_NOT_READY;
  }

  lastStartRequestId = requestId;

  if (!pressBPButton("BLE START")) {
    return ACK_BUSY;
  }

  resetMeasurementData();

  // Only START requested.
  // NOT counted as a session yet.
  measureState = M_STARTING;
  startRequestedAt = millis();

  // The previous session number is not
  // assigned to this pending measurement.
  Serial.println();
  Serial.println("[START] REQUEST ACCEPTED");
  Serial.println("[START] D5 PULSE SENT");

  Serial.println(
    "[START] SESSION NOT COUNTED YET"
  );

  Serial.println(
    "[START] WAITING FOR soc=3"
  );

  return ACK_OK;
}

// =====================================================
// BLE COMMAND PROCESSOR
// =====================================================

void executeCommand(
  uint8_t opcode,
  uint32_t requestId,
  uint32_t argument,
  bool valid
) {
  if (!valid) {
    sendResponse(
      requestId, ACK_INVALID
    );
    return;
  }

  switch (opcode) {

    case OP_PING:
      sendResponse(
        requestId, ACK_OK
      );
      break;

    case OP_STATUS:
      sendResponse(
        requestId, ACK_OK
      );
      notifyStatus();
      break;

    case OP_GET_RESULT:
      if (!hasPendingResult()) {
        // ACKed result is not new.
        sendResponse(
          requestId, ACK_NOT_FOUND
        );

      } else {
        sendResponse(
          requestId, ACK_OK
        );

        notifyResult();
      }
      break;

    case OP_ACK_RESULT: {
      if (!resultStored) {
        sendResponse(
          requestId, ACK_NOT_FOUND
        );
        break;
      }

      if (
        argument != storedResultSession()
      ) {
        sendResponse(
          requestId, ACK_WRONG_SESSION
        );
        break;
      }

      if (hasPendingResult()) {
        uint8_t updated[16];

        memcpy(
          updated,
          resultFrame,
          16
        );

        updated[3] |= 0x02;

        finishFrame(updated, 16);

        if (!persistResult(updated)) {
          sendResponse(
            requestId, ACK_NOT_READY
          );
          break;
        }

        memcpy(
          resultFrame,
          updated,
          16
        );

        // RESULT is now consumed.
        updateResultCharacteristic();

        Serial.printf(
          "[NVS] PC ACK SESSION=%lu\n",
          (unsigned long)argument
        );
      }

      // Idempotent ACK for this result.
      sendResponse(
        requestId, ACK_OK
      );

      notifyStatus();
      break;
    }

    case OP_START: {
      uint8_t ack =
        handleStart(requestId);

      sendResponse(
        requestId, ack
      );

      notifyStatus();
      break;
    }

    default:
      sendResponse(
        requestId, ACK_INVALID
      );
      break;
  }
}

void processBLEQueue() {
  BLECommand command;

  for (int i = 0; i < 4; i++) {
    if (xQueueReceive(
      bleQueue,
      &command,
      0
    ) != pdTRUE) {
      break;
    }

    uint32_t requestId = get32(
      command.frame + 4
    );

    if (command.length != 14) {
      sendResponse(
        requestId, ACK_INVALID
      );
      continue;
    }

    bool valid =
      validFrame(command.frame, 14) &&
      command.frame[3] == 0;

    executeCommand(
      command.frame[2],
      requestId,
      get32(command.frame + 8),
      valid
    );
  }
}

// =====================================================
// TIMERS / HEARTBEAT / RECONNECT REPORTING
// =====================================================

void updatePostResultPower() {
  if (!autoOffPending) return;
  uint32_t now = millis();

  // A spontaneous OFF UART event cancels the pending pulse.
  if (powerState == P_OFF) {
    autoOffPending = false;
    autoOffSent = false;
    return;
  }

  if (!autoOffSent) {
    if ((uint32_t)(now - resultSavedAt) < AUTO_OFF_SETTLE_MS) return;
    if (!canPulse()) return;
    if (pressBPButton("POST_RESULT_OFF")) {
      autoOffSent = true;
      autoOffSentAt = now;
      Serial.println("[POWER] OFF PULSE SENT ONCE; AWAIT MSG_POWER_DOWN");
    }
  } else if ((uint32_t)(now - autoOffSentAt) >= AUTO_OFF_CONFIRM_MS) {
    // No blind repeated toggle; physical power state is unknown.
    autoOffPending = false;
    powerState = P_UNKNOWN;
    Serial.println("[POWER] OFF NOT CONFIRMED; NO RETRY; START LOCKED");
    notifyStatus();
  }
}

void updateTimers() {
  uint32_t now = millis();

  updatePostResultPower();

  // START accepted but pump not confirmed.
  if (measureState == M_STARTING &&
      (uint32_t)(
        now - startRequestedAt
      ) >= START_TIMEOUT_MS) {

    measureState = M_ERROR;
    powerState = P_UNKNOWN;

    Serial.println("[START] TIMEOUT");
    Serial.println(
      "[START] NO SESSION CREATED"
    );
    Serial.println(
      "[START] NO AUTOMATIC RETRY"
    );

    notifyStatus();
  }

  // Pump session timed out.
  if (sessionOpen &&
      (measureState == M_INFLATING ||
       measureState == M_HOLDING) &&
      (uint32_t)(
        now - sessionStartedAt
      ) >= MEASURE_TIMEOUT_MS) {

    failMeasurement(
      "Measurement timeout"
    );
  }

  // No HEX result after END.
  if (sessionOpen &&
      measureState == M_WAIT_RESULT &&
      (uint32_t)(
        now - endReceivedAt
      ) >= RESULT_TIMEOUT_MS) {

    failMeasurement(
      "HEX14 timeout"
    );
  }

  // BLE connection state changes
  // never touch D5.
  if (connectionChanged) {
    connectionChanged = false;

    if (bleConnected) {
      notifyStatus();

      // GET_RESULT on Python reconnect will
      // recover any pending result.
      // No extra forced notification here.
    }
  }

  // Heartbeat every 2 seconds.
  if ((uint32_t)(
    now - lastHeartbeatAt
  ) >= HEARTBEAT_MS) {

    lastHeartbeatAt = now;

    if (bleConnected) {
      notifyStatus();
    } else {
      updateStatus();
    }
  }

  // Periodic resend only until ACK.
  if (bleConnected &&
      hasPendingResult() &&
      (uint32_t)(
        now - lastResultSentAt
      ) >= RESULT_RESEND_MS) {

    notifyResult();
  }
}

// =====================================================
// USB DIAGNOSTICS
// =====================================================

void printStatus() {
  Serial.println();
  Serial.println("======= KURSIMU V13.5 =======");

  Serial.print("BLE: ");
  Serial.println(
    bleConnected
      ? "CONNECTED"
      : "DISCONNECTED"
  );

  Serial.print("BOOT PULSE: ");
  Serial.println(
    bootPulseDone
      ? "DONE"
      : "PENDING"
  );

  Serial.print("MEASUREMENT STATE: ");
  Serial.println(measureState);

  Serial.print("POWER: ");
  Serial.println(powerState);

  Serial.print("CURRENT SESSION: ");
  Serial.println(reportedSession());

  Serial.print("TOTAL: ");
  Serial.println(totalSessions);

  Serial.print("SUCCESS: ");
  Serial.println(successSessions);

  Serial.print("FAILED: ");
  Serial.println(failedSessions);

  Serial.print("RESULT STORED: ");
  Serial.println(
    resultStored ? "YES" : "NO"
  );

  Serial.print("RESULT PENDING ACK: ");
  Serial.println(
    hasPendingResult() ? "YES" : "NO"
  );

  Serial.print("RX BYTES: ");
  Serial.println(rxBytes);

  Serial.print("RX LINES: ");
  Serial.println(rxLines);

  Serial.print("BAD HEX14: ");
  Serial.println(badPackets);

  Serial.print("OVERFLOWS: ");
  Serial.println(lineOverflows);

  Serial.println("BOOT TOGGLE: 1500ms");
  Serial.println("PULSE: 100ms");
  Serial.println("PULSE GUARD: 700ms");
  Serial.println("HEARTBEAT: 2000ms");

  Serial.println("AUTO RETRY: OFF");
  Serial.println("AUTO OFF: ONE SHOT AFTER STORED RESULT");
  Serial.print("AUTO OFF PENDING: ");
  Serial.println(autoOffPending ? "YES" : "NO");

  Serial.println("=============================");
}

void printLastResult() {
  if (!resultStored) {
    Serial.println("[LAST] NONE");
    return;
  }

  Serial.printf(
    "[LAST] session=%lu sys=%u dia=%u "
    "pulse=%u acked=%u\n",
    (unsigned long)storedResultSession(),
    (unsigned)resultFrame[8],
    (unsigned)resultFrame[9],
    (unsigned)resultFrame[10],
    hasPendingResult() ? 0U : 1U
  );
}

void handleUSB(String command) {
  command.trim();
  command.toUpperCase();

  if (command == "PING") {
    Serial.println("PONG");

  } else if (command == "STATUS") {
    printStatus();

  } else if (command == "LAST") {
    printLastResult();

  } else if (command == "RAW ON") {
    rawEcho = true;
    Serial.println("[USB] RAW ON");

  } else if (command == "RAW OFF") {
    rawEcho = false;
    Serial.println("[USB] RAW OFF");

  } else if (command == "HELP") {
    Serial.println(
      "PING | STATUS | LAST | RAW ON | RAW OFF"
    );

  } else {
    Serial.println(
      "[USB] UNKNOWN COMMAND"
    );
  }
}

void readUSB() {
  while (Serial.available() > 0) {
    char c = Serial.read();

    if (c == '\r' || c == '\n') {
      if (usbLine.length() > 0) {
        handleUSB(usbLine);
        usbLine = "";
      }

    } else if (usbLine.length() < 80) {
      usbLine += c;

    } else {
      usbLine = "";
    }
  }
}

// =====================================================
// SETUP
// =====================================================

void setup() {
  bootStartedAt = millis();

  // Initialize LED OFF before any BLE/button work.
  digitalWrite(STATUS_LED_PIN, STATUS_LED_OFF);
  pinMode(STATUS_LED_PIN, OUTPUT);
  digitalWrite(STATUS_LED_PIN, STATUS_LED_OFF);
  ledLastActivityAt = bootStartedAt;

  // Keep the button driver inactive on boot.
  digitalWrite(BP_BUTTON_PIN, LOW);
  pinMode(BP_BUTTON_PIN, OUTPUT);
  digitalWrite(BP_BUTTON_PIN, LOW);

  Serial.begin(USB_BAUD);

  // BP UART RX only.
  Serial1.setRxBufferSize(2048);

  Serial1.begin(
    BP_BAUD,
    SERIAL_8N1,
    BP_RX_PIN,
    -1
  );

  prefs.begin("bpmon", false);

  // Preserve legacy lifetime counters.
  totalSessions =
    prefs.getUInt("sessions", 0);

  successSessions =
    prefs.getUInt("success", 0);

  failedSessions =
    prefs.getUInt("failed", 0);

  lastStartRequestId =
    prefs.getUInt("lastreq", 0);

  sessionId = totalSessions;

  loadResult();

  bleQueue = xQueueCreate(
    8,
    sizeof(BLECommand)
  );

  if (!bleQueue) {
    Serial.println(
      "[FATAL] BLE QUEUE FAILED"
    );

    while (true) {
      delay(1000);
    }
  }

  setupBLE();

  Serial.println();
  Serial.println("============================");
  Serial.println("KURSIMU BP BLE V13.5");
  Serial.println("============================");

  Serial.println("LEGACY POWER: ENABLED");
  Serial.println("BOOT PULSE: 1500ms");
  Serial.println("BUTTON PULSE: 100ms");
  Serial.println("PULSE GUARD: 700ms");

  Serial.println("BOOT STATE: IDLE");
  Serial.println("SESSION ON PUMP FLAG ONLY");

  Serial.println("BLE GATT: ENABLED");
  Serial.println("HEARTBEAT: 2000ms");
  Serial.println("CRC16: ENABLED");

  Serial.println("RESULT NVS: ENABLED");
  Serial.println("ACK + RECOVERY: ENABLED");
  Serial.println("AUTO RESEND: ENABLED");

  Serial.println("AUTO RETRY: DISABLED");
  Serial.println("AUTO OFF: CONTROLLED ONE SHOT");

  Serial.println("============================");
  Serial.println("[BOOT] CONTROLLER READY");
  Serial.println("[LED] D4/GPIO6 LOW-POWER STATUS ENABLED");
  ledStartPattern(2, 35, 95);
}

// =====================================================
// MAIN LOOP
// =====================================================

void loop() {
  updateButton();

  // Exactly one boot pulse per MCU boot.
  updateBoot();

  // Continue receiving BP data even
  // when Bluetooth is disconnected.
  readBPUART();

  processBLEQueue();
  updateTimers();

  readUSB();

  // LAST: visual indication never delays critical BP processing.
  updateStatusLed();

  delay(1);
}