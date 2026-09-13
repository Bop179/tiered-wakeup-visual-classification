// INTERFACE 2.5 steps 4-5 without a multimeter. Uno on USB, NOTHING to the Pi.
// A1 = TX divider midpoint, A2 = wake line Pi-side (1.8k to 3V3 stands in for
// the Pi's pull-up), A3 = the Uno's 3V3 pin, used to calibrate VCC.
// D7 only ever INPUT or OUTPUT LOW, exactly like the firmware.
static float vcc() { analogRead(A3); return 3.30 * 1023.0 / analogRead(A3); }
static float volts(uint8_t pin, float v) { analogRead(pin); return analogRead(pin) * v / 1023.0; }
static void report(const char *state) {
  Serial.flush(); delay(20);                 // D1 idles HIGH while we sample
  float v = vcc(), m = volts(A1, v), w = volts(A2, v);
  Serial.print(state); Serial.print(",vcc="); Serial.print(v, 3);
  Serial.print(",divider="); Serial.print(m, 3);
  Serial.print(",wake="); Serial.println(w, 3);
}
void setup() { pinMode(7, INPUT); Serial.begin(9600); delay(200); Serial.println("# wirecheck up"); }
void loop() {
  pinMode(7, INPUT); delay(1200); report("released");
  pinMode(7, OUTPUT); digitalWrite(7, LOW); delay(300); report("asserted");
  pinMode(7, INPUT);
}
