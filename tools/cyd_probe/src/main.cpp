// CYD board probe v4 — identifies the panel electrically and maps to the correct Marauder variant,
// AND decides whether a CYD panel is present at all (so a bare ESP32 with no display is not misread as
// a mute-ID ST7789 CYD). Signals: display controller read-ID (bit-banged SPI, 1 dummy bit); 0x09 status
// liveness (a real controller answers, a floating MISO reads 0x00/0xFF); capacitive-touch I2C presence;
// LDR divider presence. Prints a structured, host-parseable report.
#include <Arduino.h>
#include <Wire.h>

#define TFT_SCLK 14
#define TFT_MOSI 13
#define TFT_MISO 12
#define TFT_CS   15
#define TFT_DC   2
#define TFT_BL   21
#define T_CS  33   // capacitive SDA
#define T_MOSI 32  // capacitive SCL
#define LDR_PIN 34
#define LED_R 4
#define LED_G 16
#define LED_B 17

static inline void clk() { digitalWrite(TFT_SCLK, HIGH); delayMicroseconds(2); digitalWrite(TFT_SCLK, LOW); delayMicroseconds(2); }
static void bbWrite(uint8_t b) { for (int i = 7; i >= 0; i--) { digitalWrite(TFT_MOSI, (b >> i) & 1); clk(); } }
static uint8_t bbRead() { uint8_t v = 0; for (int i = 7; i >= 0; i--) { digitalWrite(TFT_SCLK, HIGH); delayMicroseconds(2); v = (v << 1) | (digitalRead(TFT_MISO) & 1); digitalWrite(TFT_SCLK, LOW); delayMicroseconds(2); } return v; }

static uint32_t readReg(uint8_t cmd, int nbytes) {
  digitalWrite(TFT_CS, LOW);
  digitalWrite(TFT_DC, LOW);  bbWrite(cmd);
  digitalWrite(TFT_DC, HIGH); clk();               // 1 dummy bit
  uint32_t v = 0; for (int i = 0; i < nbytes; i++) v = (v << 8) | bbRead();
  digitalWrite(TFT_CS, HIGH); return v;
}

static int capTouchAddr() {
  Wire.begin(T_CS, T_MOSI); Wire.setClock(100000);
  const uint8_t addrs[] = {0x15, 0x38, 0x5D, 0x14, 0x2E, 0x48};
  for (uint8_t i = 0; i < sizeof(addrs); i++) { Wire.beginTransmission(addrs[i]); if (Wire.endTransmission() == 0) return addrs[i]; }
  return 0;
}

void setup() {
  Serial.begin(115200);
  delay(400);
  pinMode(TFT_SCLK, OUTPUT); pinMode(TFT_MOSI, OUTPUT); pinMode(TFT_MISO, INPUT);
  pinMode(TFT_CS, OUTPUT); pinMode(TFT_DC, OUTPUT); pinMode(TFT_BL, OUTPUT);
  digitalWrite(TFT_CS, HIGH); digitalWrite(TFT_SCLK, LOW); digitalWrite(TFT_BL, HIGH);
  delay(60);

  // Exact controller ID — read a few times and trust any clean 0x9341/0x7796 (noisy panels miss it once).
  uint32_t id = 0;
  for (int i = 0; i < 5 && id == 0; i++) { uint32_t d = readReg(0xD3, 4) & 0xFFFF; if (d == 0x9341 || d == 0x7796) id = d; }
  uint32_t d3raw = readReg(0xD3, 4);
  uint32_t r04 = readReg(0x04, 4);
  // Liveness — a present controller answers 0x09 stably with a non-0x00/0xFF status byte.
  uint32_t s1 = readReg(0x09, 4), s2 = readReg(0x09, 4);
  uint8_t sb = (s1 >> 16) & 0xFF;
  bool alive = (s1 == s2) && sb != 0x00 && sb != 0xFF;
  int cap = capTouchAddr();
  int ldr = analogRead(LDR_PIN);
  bool ldr_ok = (ldr > 300 && ldr < 3800);

  const char* ctrl = id == 0x9341 ? "ILI9341" : id == 0x7796 ? "ST7796" : alive ? "ST7789" : "none";
  const char* touch = cap ? "capacitive" : "resistive";

  bool cyd; const char* conf;
  if (id == 0x9341 || id == 0x7796) { cyd = true; conf = "high"; }
  else if (alive)  { cyd = true; conf = ldr_ok ? "high" : "medium"; }
  else if (ldr_ok) { cyd = true; conf = "low"; }
  else { cyd = false; conf = "none"; }

  const char* variant;
  if (!cyd) variant = "none";
  else if (id == 0x9341) variant = "cyd_2432S028";
  else if (id == 0x7796) variant = "cyd_3_5_inch";
  else {
    uint8_t r04hi = (r04 >> 24) & 0xFF; if (!r04hi) r04hi = (r04 >> 16) & 0xFF;
    if (r04hi == 0x85) variant = "cyd_2432S028_2usb";
    else if (r04hi == 0x81) variant = "cyd_2432S024_guition";
    else variant = cap ? "cyd_2432S024_guition" : "cyd_2432S028_2usb";
  }

  for (int rep = 0; rep < 6; rep++) {
    Serial.println();
    Serial.println("=====CYD_PROBE=====");
    Serial.printf("CYD=%s CONF=%s CONTROLLER=%s TOUCH=%s\n", cyd ? "yes" : "no", conf, ctrl, touch);
    Serial.printf("VARIANT=%s\n", variant);
    Serial.printf("D3=0x%08X 04=0x%08X 09=0x%08X alive=%d cap_i2c=0x%02X LDR=%d\n", d3raw, r04, s1, alive, cap, ldr);
    Serial.println("=====END=====");
    delay(800);
  }
}

void loop() {
  pinMode(LED_R, OUTPUT); pinMode(LED_G, OUTPUT); pinMode(LED_B, OUTPUT);
  digitalWrite(LED_R, LOW); delay(200); digitalWrite(LED_R, HIGH);
  digitalWrite(LED_G, LOW); delay(200); digitalWrite(LED_G, HIGH);
  digitalWrite(LED_B, LOW); delay(200); digitalWrite(LED_B, HIGH);
}
