// click_logger_multi.ino -- long-term FRE relay monitor (Syna EK 393, K1..K4).
// Flash to the ESP32-S3 (Arduino IDE, board "ESP32S3 Dev Module").
// Read by logger/relay_logger.py on the Raspberry Pi.
//
// Wiring (Syna EK 393 "Relaisbelegung", potential-free changeover contacts):
//   Kx terminal c (COM) --- GND  (all four c's tied together)
//   Kx terminal a (NO)  --- ESP32 GPIO (K1=4, K2=5, K3=6, K4=7)
//   Kx terminal b (NC)  --- unconnected
// Internal pull-ups are used: LOW = contact at 'a' (command), HIGH = at 'b' (rest).
//
// Output lines (machine-readable, one per line, 115200 baud):
//   BOOT,<ms>,<fw>                          ESP just started (fw = firmware version)
//   EVT,<ms>,<relay>,<pos>                  a relay changed; pos = a or b
//   STATE,<ms>,K1=<pos>,K2=..,K3=..,K4=..   full snapshot (at boot + every 60 s)
// The STATE heartbeat proves the monitor was alive during quiet periods, so
// "no events" in the data really means "nothing happened".
//
// If you change the output format, bump FW_VERSION so old and new data can be
// told apart later.

const char *FW_VERSION = "v2";
const int NUM_CHANNELS = 4;
const int PINS[NUM_CHANNELS] = {4, 5, 6, 7};
const char *NAMES[NUM_CHANNELS] = {"K1", "K2", "K3", "K4"};
const unsigned long DEBOUNCE_MS = 20;
const unsigned long HEARTBEAT_MS = 60000;

volatile unsigned long lastChangeMs[NUM_CHANNELS] = {0, 0, 0, 0};
volatile bool pending[NUM_CHANNELS] = {false, false, false, false};

void IRAM_ATTR isr0() { lastChangeMs[0] = millis(); pending[0] = true; }
void IRAM_ATTR isr1() { lastChangeMs[1] = millis(); pending[1] = true; }
void IRAM_ATTR isr2() { lastChangeMs[2] = millis(); pending[2] = true; }
void IRAM_ATTR isr3() { lastChangeMs[3] = millis(); pending[3] = true; }
void (*ISRS[NUM_CHANNELS])() = {isr0, isr1, isr2, isr3};

bool reported[NUM_CHANNELS];      // last reported level (HIGH/LOW)
unsigned long lastHeartbeat = 0;

char pos(bool level) { return level ? 'b' : 'a'; }

void printState() {
  Serial.printf("STATE,%lu", millis());
  for (int i = 0; i < NUM_CHANNELS; i++) {
    Serial.printf(",%s=%c", NAMES[i], pos(digitalRead(PINS[i])));
  }
  Serial.println();
}

void setup() {
  Serial.begin(115200);
  delay(500);
  for (int i = 0; i < NUM_CHANNELS; i++) {
    pinMode(PINS[i], INPUT_PULLUP);
    reported[i] = digitalRead(PINS[i]);
    attachInterrupt(digitalPinToInterrupt(PINS[i]), ISRS[i], CHANGE);
  }
  Serial.printf("BOOT,%lu,%s\n", millis(), FW_VERSION);
  printState();
  lastHeartbeat = millis();
}

void loop() {
  for (int i = 0; i < NUM_CHANNELS; i++) {
    if (pending[i] && millis() - lastChangeMs[i] >= DEBOUNCE_MS) {
      noInterrupts();
      pending[i] = false;
      unsigned long t = lastChangeMs[i];
      interrupts();
      bool level = digitalRead(PINS[i]);
      if (level != reported[i]) {
        reported[i] = level;
        Serial.printf("EVT,%lu,%s,%c\n", t, NAMES[i], pos(level));
      }
    }
  }
  if (millis() - lastHeartbeat >= HEARTBEAT_MS) {
    lastHeartbeat = millis();
    printState();
  }
}
