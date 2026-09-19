// Tier 1 bench scope: streams A0 (low-pass output) and D2 (comparator OUT) at
// 100 Hz for the Arduino IDE Serial Plotter, 115200 baud. Both in volts, so D2
// reads 5 idle and 0 when the comparator fires.
// Reflash firmware/tier2_firmware before any run with the Pi.
// D7 stays INPUT: it is still wired to the Pi's GPIO3.
void setup() {
  pinMode(7, INPUT);
  pinMode(2, INPUT);
  Serial.begin(115200);
}

void loop() {
  float a0 = analogRead(A0) * 5.0 / 1023.0;
  Serial.print("A0:"); Serial.print(a0, 3);
  Serial.print(" D2:"); Serial.println(digitalRead(2) ? 5 : 0);
  delay(10);
}
