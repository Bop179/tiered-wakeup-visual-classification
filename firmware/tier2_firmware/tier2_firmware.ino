/* Tier 2 -- Arduino Uno. Photosensor gate between Tier 1 (LM339N comparator)
 * and Tier 3 (Raspberry Pi 4).
 *
 * Contract: docs/INTERFACE.md. Section 1 is the wire protocol, section 2 the
 * electrical rules, section 3 wake and halt, section 6 these constants.
 *
 * THE ONE RULE THAT DESTROYS HARDWARE IF BROKEN
 * ---------------------------------------------
 * Pi GPIO3 is 3.3 V and NOT 5 V tolerant. This firmware drives the wake line
 * open-drain ONLY: pinMode(INPUT) releases it and lets the Pi's own 1.8 kOhm
 * pull-up hold it high; pinMode(OUTPUT) + digitalWrite(LOW) asserts it.
 * `digitalWrite(PIN_WAKE, HIGH)` while the pin is an OUTPUT puts 5 V on GPIO3
 * and kills the Pi, and INPUT_PULLUP does the same thing through ~30 kOhm.
 * Neither appears below, and neither may ever be added. wakeRelease() and
 * wakeAssert() are the only two functions that touch the pin.
 *
 * WHY THE UNO DOES NOT DEEP-SLEEP THE WHOLE TIME
 * ---------------------------------------------
 * SLEEP_MODE_PWR_DOWN stops the I/O clock, and with it both the millis() timer
 * and the USART. A powered-down Uno therefore cannot time the dormancy window
 * and cannot hear SET/GET from the Pi. So deep sleep is only entered once the
 * Pi is HALTED: at that point there is no dormancy timer left to run and nobody
 * on the other end to listen to, and the only thing that can legitimately
 * happen next is a Tier 1 trigger, which is exactly what INT0 wakes on. While
 * the Pi is awake this loop stays awake with it. That costs Tier 2 power the
 * brief only calls for during dormancy, and it is the only arrangement that
 * makes the dormancy sweep and the runtime SET/GET contract both work.
 */

#include <avr/interrupt.h>
#include <avr/power.h>
#include <avr/sleep.h>

/* ------------------------------------------------ section 6 constants ---- */

static const uint8_t PIN_TRIGGER  = 2;   /* INT0, comparator output           */
static const uint8_t PIN_WAKE     = 7;   /* to Pi GPIO3 via 1 kOhm, open-drain */
static const uint8_t PIN_PEAK_ADC = A0;  /* pre-comparator analog             */

static const uint16_t WAKE_ASSERT_MS  = 200;
static const uint16_t ACK_TIMEOUT_MS  = 2000;
static const uint16_t RES_TIMEOUT_MS  = 5000;
static const uint32_t BOOT_TIMEOUT_MS = 60000UL;
static const uint32_t HALT_SETTLE_MS  = 20000UL;

/* Runtime-settable (section 1.1). Defaults are the section 6 values, so a
 * firmware that has never been SET behaves exactly as the brief describes. */
static const int32_t  DORMANCY_DEFAULT   = 30000;
static const uint16_t PERSIST_DEFAULT    = 40;
static const uint16_t REFRACTORY_DEFAULT = 500;

static const int32_t  DORMANCY_MAX   = 3600000L;
static const uint16_t PERSIST_MAX    = 5000;
static const uint16_t REFRACTORY_MAX = 60000;

static int32_t  g_dormancy_ms   = DORMANCY_DEFAULT;   /* -1 = never halt */
static uint16_t g_persist_ms    = PERSIST_DEFAULT;
static uint16_t g_refractory_ms = REFRACTORY_DEFAULT;

/* A SET that lands mid-event is applied after the event completes; changing the
 * refractory period underneath a running timer is a race nobody needs. */
static bool     g_defer_pending   = false;
static int32_t  g_defer_dormancy;
static uint16_t g_defer_persist;
static uint16_t g_defer_refractory;

/* ------------------------------------------------------------- state ----- */

enum PiState { PI_AWAKE, PI_BOOTING, PI_HALTED };
static PiState g_pi = PI_AWAKE;

static uint32_t g_last_link_activity = 0;   /* for the dormancy timer        */
static uint32_t g_boot_deadline      = 0;
static bool     g_boot_reasserted    = false;
static uint32_t g_refractory_until   = 0;
static bool     g_wait_release       = false; /* one EVT per assertion */

/* Exactly one pending event is buffered. Events arriving during a boot
 * overwrite it -- Tier 2 has no queue and does not need one (section 3). */
static bool     g_have_pending = false;
static uint32_t g_pend_t_ms;
static uint16_t g_pend_peak;
static uint32_t g_pend_duration_ms;

static volatile bool g_int0_fired = false;

/* --------------------------------------------------- the wake line ------- */

/* Release: high-Z. The Pi's 1.8 kOhm pull-up to 3V3 holds the line high. */
static void wakeRelease() { pinMode(PIN_WAKE, INPUT); }

/* Assert: pull low. OUTPUT and LOW are set in that order and never separated. */
static void wakeAssert() { pinMode(PIN_WAKE, OUTPUT); digitalWrite(PIN_WAKE, LOW); }

/* ------------------------------------------------------ the link -------- */

static char    g_line[72];
static uint8_t g_len = 0;
static bool    g_overlong = false;

static void sendLine(const char *s) { Serial.print(s); Serial.print('\n'); }

/* Never blocks. Returns true when a complete line is sitting in g_line.
 * Lines over 64 bytes including the terminator are dropped (section 1). */
static bool pollLine() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n') {
      bool ok = !g_overlong;
      g_line[g_len] = '\0';
      /* A trailing \r is accepted and stripped. */
      if (ok && g_len && g_line[g_len - 1] == '\r') g_line[--g_len] = '\0';
      g_len = 0;
      g_overlong = false;
      if (ok) return true;
      continue;
    }
    if (g_len >= 63) { g_overlong = true; continue; }
    g_line[g_len++] = c;
  }
  return false;
}

/* ---------------------------------------------- SET / GET / CFG ---------- */

static void applyDeferred() {
  if (!g_defer_pending) return;
  g_dormancy_ms   = g_defer_dormancy;
  g_persist_ms    = g_defer_persist;
  g_refractory_ms = g_defer_refractory;
  g_defer_pending = false;
}

static bool inEvent() { return g_have_pending || g_pi == PI_BOOTING; }

/* CFG reports the value IN EFFECT, not the value requested: an out-of-range
 * value is clamped and CFG returns the clamped figure. That is what makes this
 * a verification rather than a command -- run_experiment.py writes the CFG
 * value into the manifest, so the run record cannot disagree with the hardware. */
static void replyCfg(const char *key, int32_t value) {
  char out[40];
  snprintf(out, sizeof(out), "CFG,%s,%ld", key, (long)value);
  sendLine(out);
}

static int32_t clampL(int32_t v, int32_t lo, int32_t hi) {
  return v < lo ? lo : (v > hi ? hi : v);
}

static void handleSet(char *key, char *valstr) {
  char *end;
  long v = strtol(valstr, &end, 10);
  if (end == valstr) return;                       /* malformed: drop silently */

  /* Stage into whichever copy is live: the effective one when idle, the
   * deferred one when an event is in flight. */
  int32_t  dorm = g_defer_pending ? g_defer_dormancy   : g_dormancy_ms;
  uint16_t pers = g_defer_pending ? g_defer_persist    : g_persist_ms;
  uint16_t refr = g_defer_pending ? g_defer_refractory : g_refractory_ms;

  int32_t effective;
  if (!strcmp(key, "DORMANCY")) {
    dorm = (v < 0) ? -1 : clampL(v, 0, DORMANCY_MAX);
    effective = dorm;
  } else if (!strcmp(key, "PERSIST")) {
    pers = (uint16_t)clampL(v, 0, PERSIST_MAX);
    effective = pers;
  } else if (!strcmp(key, "REFRACTORY")) {
    refr = (uint16_t)clampL(v, 0, REFRACTORY_MAX);
    effective = refr;
  } else {
    /* Unknown key -> a comment and NO CFG. Silence on CFG is how the Pi knows
     * the firmware is older than the key it asked for. */
    char out[48];
    snprintf(out, sizeof(out), "# ERR unknown key %s", key);
    sendLine(out);
    return;
  }

  if (inEvent()) {
    g_defer_dormancy   = dorm;
    g_defer_persist    = pers;
    g_defer_refractory = refr;
    g_defer_pending    = true;
  } else {
    g_dormancy_ms = dorm; g_persist_ms = pers; g_refractory_ms = refr;
  }
  /* CFG reports what will be in effect for the next event either way. */
  replyCfg(key, effective);
}

static void handleGet(char *key) {
  int32_t  dorm = g_defer_pending ? g_defer_dormancy   : g_dormancy_ms;
  uint16_t pers = g_defer_pending ? g_defer_persist    : g_persist_ms;
  uint16_t refr = g_defer_pending ? g_defer_refractory : g_refractory_ms;
  if      (!strcmp(key, "DORMANCY"))   replyCfg(key, dorm);
  else if (!strcmp(key, "PERSIST"))    replyCfg(key, pers);
  else if (!strcmp(key, "REFRACTORY")) replyCfg(key, refr);
  else {
    char out[48];
    snprintf(out, sizeof(out), "# ERR unknown key %s", key);
    sendLine(out);
  }
}

/* ------------------------------------------------ inbound dispatch ------- */

static bool g_saw_ack = false;
static bool g_saw_res = false;

/* Splits on commas in place. Malformed lines are dropped silently -- never
 * block, never retry, never reset (section 1 rule 4). */
static void handleLine(char *s) {
  if (s[0] == '\0') return;

  if (s[0] == '#') {
    /* Comments are ignored -- except that "# ready" is how the daemon
     * announces it has finished booting (section 3, wake sequence step 4). */
    if (!strncmp(s, "# ready", 7) && g_pi == PI_BOOTING) {
      g_pi = PI_AWAKE;
      g_last_link_activity = millis();
      sendLine("# t2: pi ready");
    }
    return;
  }

  g_last_link_activity = millis();

  char *tok[6];
  uint8_t n = 0;
  char *p = s;
  while (n < 6) {
    tok[n++] = p;
    char *c = strchr(p, ',');
    if (!c) break;
    *c = '\0';
    p = c + 1;
  }

  if (!strcmp(tok[0], "ACK")) { g_saw_ack = true; return; }
  if (!strcmp(tok[0], "RES")) { g_saw_res = true; return; }
  if (!strcmp(tok[0], "SET") && n >= 3) { handleSet(tok[1], tok[2]); return; }
  if (!strcmp(tok[0], "GET") && n >= 2) { handleGet(tok[1]); return; }
  if (!strcmp(tok[0], "SYNC") && n >= 2) {
    char out[32];
    snprintf(out, sizeof(out), "SYNC,%lu", (unsigned long)millis());
    sendLine(out);
    return;
  }
  /* anything else: drop */
}

/* Pump the link for up to ms milliseconds, or until `stop` goes true. */
static void pump(uint32_t ms, const bool *stop) {
  uint32_t t0 = millis();
  while ((uint32_t)(millis() - t0) < ms) {
    if (pollLine()) handleLine(g_line);
    if (stop && *stop) return;
  }
}

/* ------------------------------------------------------ Tier 1 gate ----- */

static void onInt0() { g_int0_fired = true; }

/* The comparator output is active LOW at PIN_TRIGGER (LM339N open-collector
 * with a 10 kOhm pull-up to 5 V -- without that pull-up it never goes high and
 * this reads as permanently triggered; see section 2.3). */
static bool triggerAsserted() { return digitalRead(PIN_TRIGGER) == LOW; }

/* Confirm the trigger held for PERSIST_MS, sampling A0 for the peak while we
 * wait. Returns false if it let go early -- that is the flicker rejection the
 * whole tier exists for. */
static bool confirmTrigger(uint16_t *peak_out, uint32_t *duration_out) {
  uint32_t t0 = millis();
  uint16_t peak = 0;
  while ((uint32_t)(millis() - t0) < g_persist_ms) {
    if (!triggerAsserted()) return false;
    uint16_t v = analogRead(PIN_PEAK_ADC);
    if (v > peak) peak = v;
  }
  /* Accept now. `duration` is the hold before acceptance (INTERFACE section 1);
   * waiting for release instead delays every EVT and wake by the stimulus length. */
  *peak_out = peak;
  *duration_out = millis() - t0;
  return true;
}

/* -------------------------------------------------------- sleeping ------ */

static void deepSleep() {
  Serial.flush();
  /* INT0 must be LEVEL-triggered (LOW): a powered-down Uno has its I/O clock
   * stopped, so edge detection does not work and LOW is the only option. */
  g_int0_fired = false;
  attachInterrupt(digitalPinToInterrupt(PIN_TRIGGER), onInt0, LOW);

  ADCSRA &= ~(1 << ADEN);          /* or the Uno sits at milliamps           */
  set_sleep_mode(SLEEP_MODE_PWR_DOWN);
  cli();
  sleep_enable();
  power_all_disable();             /* this also stops the USART              */
  sei();
  sleep_cpu();
  /* ---- woken by INT0 ---- */
  sleep_disable();
  power_all_enable();
  ADCSRA |= (1 << ADEN);

  /* Wake, then immediately detach; re-attached only on the next deep sleep,
   * after the refractory period has run. */
  detachInterrupt(digitalPinToInterrupt(PIN_TRIGGER));
}

/* ------------------------------------------------- event transmission --- */

static void sendPendingEvt() {
  char out[64];
  snprintf(out, sizeof(out), "EVT,%lu,%u,%lu",
           (unsigned long)g_pend_t_ms, g_pend_peak,
           (unsigned long)g_pend_duration_ms);
  sendLine(out);

  /* Every EVT gets exactly one ACK, sent before capture. No ACK within
   * ACK_TIMEOUT_MS means the Pi is halted or booting. */
  g_saw_ack = false;
  pump(ACK_TIMEOUT_MS, &g_saw_ack);
  if (!g_saw_ack) {
    sendLine("# t2: no ACK -- assuming halted");
    g_pi = PI_HALTED;
    return;                        /* keep the event pending; wake next loop */
  }
  /* An ACK promises a RES. No RES within RES_TIMEOUT_MS is a miss: log it and
   * carry on, never resend -- a lost event is data (section 1, timeouts). */
  g_saw_res = false;
  pump(RES_TIMEOUT_MS, &g_saw_res);
  if (!g_saw_res) sendLine("# t2: RES timeout -- miss");
  g_have_pending = false;
  applyDeferred();
  g_last_link_activity = millis();
}

static void beginWake() {
  /* Assert for >= WAKE_ASSERT_MS, then release. Do NOT send the EVT yet --
   * nothing is listening (section 3, wake sequence step 3). */
  wakeAssert();
  delay(WAKE_ASSERT_MS);
  wakeRelease();
  g_pi = PI_BOOTING;
  g_boot_deadline   = millis() + BOOT_TIMEOUT_MS;
  g_boot_reasserted = false;
  sendLine("# t2: wake asserted");
}

/* ------------------------------------------------------------ setup ----- */

void setup() {
  wakeRelease();                   /* before anything else                   */
  pinMode(PIN_TRIGGER, INPUT);     /* external 10 kOhm pull-up, section 2.3  */
  Serial.begin(9600);
  delay(50);
  sendLine("# t2: tier2_firmware up");
  g_last_link_activity = millis();
  /* The Pi's state is unknown at power-on. Assume awake: the first event will
   * find out for us when no ACK comes back, which costs one event and needs no
   * guessing. */
  g_pi = PI_AWAKE;
}

/* ------------------------------------------------------------- loop ----- */

void loop() {
  if (pollLine()) handleLine(g_line);

  /* ---- boot timeout: re-assert wake once, then flag and continue ---- */
  if (g_pi == PI_BOOTING && (int32_t)(millis() - g_boot_deadline) >= 0) {
    if (!g_boot_reasserted) {
      sendLine("# t2: boot timeout -- re-asserting wake once");
      g_boot_reasserted = true;
      wakeAssert(); delay(WAKE_ASSERT_MS); wakeRelease();
      g_boot_deadline = millis() + BOOT_TIMEOUT_MS;
    } else {
      sendLine("# t2: boot failed -- giving up on this event");
      g_pi = PI_AWAKE;             /* stop waiting; the event is a miss      */
      g_have_pending = false;
      applyDeferred();
    }
  }

  /* ---- a pending event, and the Pi is ready for it ---- */
  if (g_have_pending && g_pi == PI_AWAKE) sendPendingEvt();

  /* ---- a pending event, and the Pi is halted: wake it ---- */
  if (g_have_pending && g_pi == PI_HALTED) beginWake();

  /* ---- Tier 1 ---- */
  /* REFRACTORY runs from the last moment the trigger was seen asserted, so a
   * held trigger gives one EVT and contact bounce cannot re-arm it. */
  if (g_wait_release) {
    if (triggerAsserted()) g_refractory_until = millis() + g_refractory_ms;
    else if ((int32_t)(millis() - g_refractory_until) >= 0) g_wait_release = false;
  }
  bool armed = !g_wait_release;
  if (armed && triggerAsserted()) {
    uint16_t peak; uint32_t dur;
    if (confirmTrigger(&peak, &dur)) {
      /* Only one pending event is buffered; a new one overwrites it. */
      g_pend_t_ms        = millis();
      g_pend_peak        = peak;
      g_pend_duration_ms = dur;
      g_have_pending     = true;
      g_refractory_until = millis() + g_refractory_ms;
      g_wait_release     = true;
    } else {
      /* Did not persist: flicker, rejected. This is Tier 1's false-positive
       * rate being measured, not an error. */
    }
  }

  /* ---- dormancy ---- */
  if (g_pi == PI_AWAKE && !g_have_pending && g_dormancy_ms >= 0 &&
      (uint32_t)(millis() - g_last_link_activity) >= (uint32_t)g_dormancy_ms) {
    /* The wake line must be RELEASED before HALT is sent: a line held low
     * during shutdown stops the wake from ever being seen as an edge. */
    wakeRelease();
    sendLine("HALT");
    g_saw_ack = false;
    pump(ACK_TIMEOUT_MS, &g_saw_ack);
    if (!g_saw_ack) sendLine("# t2: no ACK to HALT -- halting anyway");

    /* HALT_SETTLE_MS: the wake line does nothing during shutdown, and
     * asserting it mid-shutdown is how you end up with a Pi that halts and
     * immediately refuses to wake. */
    uint32_t t0 = millis();
    while ((uint32_t)(millis() - t0) < HALT_SETTLE_MS) {
      if (pollLine()) handleLine(g_line);
    }
    g_pi = PI_HALTED;
    sendLine("# t2: pi halted, sleeping");

    /* Nothing left to time and nobody left to listen to: deep sleep is safe
     * only from here (see the header note). */
    deepSleep();
    sendLine("# t2: woke on trigger");
    g_last_link_activity = millis();
  }
}
