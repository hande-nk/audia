// Audia ADC streamer
// Samples one analog pin at a fixed rate and prints one integer per line.
// Pairs with diy_board.py (FS = 250, BAUD = 115200).
//
// Wiring (BioAmp EXG Pill or any 5 V analog front end):
//   front end VCC -> 5V, GND -> GND, OUT -> A0
//
// SAFETY: electrodes on your head only while the laptop runs on battery,
// charger unplugged. Never power the front end from a wall adapter.

const int  PIN        = A0;
const long SAMPLE_HZ  = 250;
const unsigned long PERIOD_US = 1000000UL / SAMPLE_HZ;

unsigned long nextSample;

void setup() {
  Serial.begin(115200);
  nextSample = micros();
}

void loop() {
  // Fixed-rate timing: schedule from the last deadline, not from "now",
  // so small delays do not add up into drift.
  if ((long)(micros() - nextSample) >= 0) {
    nextSample += PERIOD_US;
    Serial.println(analogRead(PIN));
  }
}
