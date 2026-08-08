/*
 * Cyber Controller — wireless NODE (remote sensor) firmware.  Wire protocol: W1.0.
 *
 * Role: a remote ESP32 that talks to the host through the relay over ESP-NOW.  Unlike the relay, the node
 * DOES hold crypto: its own 32-byte key + node_id.  It unseals inbound command frames (dropping anything
 * that fails the tag or is a replay), runs them, and seals its own output back over ESP-NOW.  The envelope
 * is AES-256-GCM, byte-for-byte the frame in ../PROTOCOL.md and src/core/node_crypto.py:
 *     header = version u8 | node_id u16 BE | epoch u32 BE | counter u64 BE   (15 bytes, used as GCM AAD)
 *     nonce  = epoch ‖ counter                                              (12 bytes = header[3..15])
 *     wire   = header | AES-256-GCM ciphertext | tag(16)
 *
 * NONCE SAFETY: GCM's one hard rule is never reuse a (key, nonce) pair.  The counter is monotonic and the
 * epoch is persisted to NVS and bumped on every boot, so a reset can never replay an old (epoch, counter).
 *
 * SENSING (WS1): this node also runs Wi-Fi CSI presence/motion sensing. It turns received-packet
 * channel-state info into ONE compact verdict line — "csi presence=1 motion=0.42 conf=0.82" — and
 * emits only that (sealed) over the SAME bridge; raw CSI (~256 B) never leaves the node. The verdict
 * is byte-compatible with src/core/sensing.py parse_verdict(). This is the PROVEN tier of
 * SENSING_TIERS (commodity 2.4 GHz presence/motion), NOT through-wall imaging.
 *
 * STATUS: real implementation. COMPILE-VALIDATED against esp32:esp32@2.0.11 (arduino-cli), but NOT
 * yet HARDWARE-validated (no on-silicon CSI capture run here). Reviewed against ../PROTOCOL.md +
 * node_crypto.py + sensing.py. Target: ESP32 Arduino core 2.x (classic esp_now recv-cb + IDF 4.4
 * CSI API). On core 3.x update onEspNowRecv's first arg and re-check the wifi_csi_config_t fields.
 */
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <Preferences.h>
#include <math.h>
#include "mbedtls/gcm.h"

// ── provisioned identity — host node_provision.py OVERWRITES these at flash time. Never ship the demo key.
static const uint16_t NODE_ID = 1;
static uint8_t NODE_KEY[32] = {
  0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x0e, 0x0f,
  0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1a, 0x1b, 0x1c, 0x1d, 0x1e, 0x1f,
};  // PLACEHOLDER — provision a real per-node key before use.

static const uint8_t  VERSION      = 1;
static const uint8_t  BROADCAST[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
static const uint8_t  CHANNEL      = 1;
static const size_t   HEADER_LEN   = 15;
static const size_t   TAG_LEN      = 16;
static const size_t   NONCE_LEN    = 12;
static const size_t   MTU          = 250;   // ESP-NOW payload budget (== node_crypto.ESP_NOW_MTU)
static const size_t   MAX_PT       = MTU - HEADER_LEN - TAG_LEN;   // 219

Preferences prefs;
static uint32_t txEpoch = 0;   // persisted; bumped every boot so a reset can't reuse a nonce
static uint64_t txCounter = 0;
static bool     haveRx = false;
static uint32_t rxEpoch = 0;
static uint64_t rxHighest = 0;

// ── big-endian helpers (match struct ">BHIQ") ──
static void putBE16(uint8_t *p, uint16_t v) { p[0] = v >> 8; p[1] = v; }
static void putBE32(uint8_t *p, uint32_t v) { for (int i = 0; i < 4; i++) p[i] = v >> (24 - 8 * i); }
static void putBE64(uint8_t *p, uint64_t v) { for (int i = 0; i < 8; i++) p[i] = v >> (56 - 8 * i); }
static uint16_t getBE16(const uint8_t *p) { return ((uint16_t)p[0] << 8) | p[1]; }
static uint32_t getBE32(const uint8_t *p) { uint32_t v = 0; for (int i = 0; i < 4; i++) v = (v << 8) | p[i]; return v; }
static uint64_t getBE64(const uint8_t *p) { uint64_t v = 0; for (int i = 0; i < 8; i++) v = (v << 8) | p[i]; return v; }

// Seal plaintext into a wire frame. Returns wire length, or 0 on error.
static size_t sealFrame(const uint8_t *pt, size_t ptLen, uint8_t *wire, size_t wireCap) {
  if (ptLen > MAX_PT || HEADER_LEN + ptLen + TAG_LEN > wireCap) return 0;
  if (txCounter == UINT64_MAX) {           // counter overflow -> rotate epoch, persist, reset counter
    txEpoch++;
    txCounter = 0;
    prefs.putUInt("epoch", txEpoch);
  }
  wire[0] = VERSION;
  putBE16(wire + 1, NODE_ID);
  putBE32(wire + 3, txEpoch);
  putBE64(wire + 7, txCounter);
  const uint8_t *nonce = wire + 3;         // epoch‖counter, a 12-byte slice of the header
  uint8_t *ct  = wire + HEADER_LEN;
  uint8_t *tag = wire + HEADER_LEN + ptLen;

  mbedtls_gcm_context g;
  mbedtls_gcm_init(&g);
  int rc = mbedtls_gcm_setkey(&g, MBEDTLS_CIPHER_ID_AES, NODE_KEY, 256);
  if (rc == 0) {
    rc = mbedtls_gcm_crypt_and_tag(&g, MBEDTLS_GCM_ENCRYPT, ptLen, nonce, NONCE_LEN,
                                   wire, HEADER_LEN, pt, ct, TAG_LEN, tag);
  }
  mbedtls_gcm_free(&g);
  if (rc != 0) return 0;
  txCounter++;
  return HEADER_LEN + ptLen + TAG_LEN;
}

// Verify + decrypt a wire frame addressed to this node. Returns plaintext length, or -1 to drop.
static int openFrame(const uint8_t *wire, size_t len, uint8_t *pt, size_t ptCap) {
  if (len < HEADER_LEN + TAG_LEN || len > MTU) return -1;
  if (wire[0] != VERSION) return -1;
  if (getBE16(wire + 1) != NODE_ID) return -1;         // not for us (or a frame for another node)
  size_t ctLen = len - HEADER_LEN - TAG_LEN;
  if (ctLen > ptCap) return -1;
  const uint8_t *nonce = wire + 3;
  const uint8_t *ct    = wire + HEADER_LEN;
  const uint8_t *tag   = wire + HEADER_LEN + ctLen;

  mbedtls_gcm_context g;
  mbedtls_gcm_init(&g);
  int rc = mbedtls_gcm_setkey(&g, MBEDTLS_CIPHER_ID_AES, NODE_KEY, 256);
  if (rc == 0) {
    rc = mbedtls_gcm_auth_decrypt(&g, ctLen, nonce, NONCE_LEN, wire, HEADER_LEN, tag, TAG_LEN, ct, pt);
  }
  mbedtls_gcm_free(&g);
  if (rc != 0) return -1;                              // bad tag: forged / tampered / wrong key -> drop

  // Anti-replay: strictly-monotonic (stricter than the host's sliding window, but safe for a command link).
  uint32_t e = getBE32(wire + 3);
  uint64_t c = getBE64(wire + 7);
  if (!haveRx || e > rxEpoch) { haveRx = true; rxEpoch = e; rxHighest = c; }
  else if (e < rxEpoch || c <= rxHighest) return -1;   // stale epoch, replay, or duplicate -> drop
  else rxHighest = c;
  return (int)ctLen;
}

// Seal a reply line and broadcast it; the relay forwards it to the host.
static void sendLine(const char *s) {
  uint8_t wire[MTU];
  size_t n = sealFrame((const uint8_t *)s, strlen(s), wire, sizeof(wire));
  if (n > 0) esp_now_send(BROADCAST, wire, n);
}

// ── CSI sensing (WS1) ────────────────────────────────────────────────────────────────────────
// The node turns received-packet CSI into a compact presence/motion verdict and emits ONLY that.
// Metric: per-packet CSI amplitude energy -> an EWMA baseline + the mean packet-to-packet energy
// flux over a ~1 s window. Motion tracks flux; presence trips when motion OR the baseline deviation
// crosses a threshold. CSI only updates while Wi-Fi packets arrive on CHANNEL (relay/peer ESP-NOW
// frames or ambient traffic), so confidence scales with the packet count seen in the window — a
// silent RF environment honestly reports low confidence instead of a confident guess. Thresholds
// are first-cut and meant to be tuned on-site once hardware CSI is captured.
static const float    CSI_MOTION_SCALE    = 6.0f;    // mean-flux -> motion normalizer
static const float    CSI_PRESENCE_MOTION = 0.15f;   // motion at/above this = occupied
static const float    CSI_PRESENCE_DEV    = 0.08f;   // |energy-baseline|/baseline at/above = occupied
static const uint32_t CSI_CONF_SAT_PKTS   = 50;      // packets that saturate confidence to 1.0
static const uint32_t CSI_MIN_PKTS        = 5;       // fewer than this -> hedge the confidence

static portMUX_TYPE csiMux = portMUX_INITIALIZER_UNLOCKED;
static volatile uint32_t csiPkts = 0;          // packets seen this window
static volatile float    csiEnergyAcc = 0.0f;  // sum of per-packet energies this window
static volatile float    csiFluxAcc   = 0.0f;  // sum of |energy - prevEnergy| this window
static float             csiPrevEnergy = 0.0f; // touched only inside the callback
static float             csiBaseline = 0.0f;   // EWMA of window mean-energy (the "empty room")
static bool              csiBaselineInit = false;

// Wi-Fi CSI receive callback (WiFi task context). Keep it cheap: fold each packet into the window
// accumulators under the spinlock and return; all thresholding happens later in emitVerdict().
static void onCsi(void *ctx, wifi_csi_info_t *info) {
  (void)ctx;
  if (!info || !info->buf || info->len <= 0) return;
  const int8_t *buf = info->buf;
  int n = info->len / 2;                        // raw CSI is (imag, real) int8 pairs per subcarrier
  if (n <= 0) return;
  float energy = 0.0f;
  for (int i = 0; i < n; i++) {
    int im = buf[2 * i];
    int re = buf[2 * i + 1];
    energy += sqrtf((float)(re * re + im * im));
  }
  energy /= (float)n;                           // mean subcarrier amplitude for this packet
  float flux = fabsf(energy - csiPrevEnergy);
  csiPrevEnergy = energy;
  portENTER_CRITICAL_ISR(&csiMux);
  csiPkts++;
  csiEnergyAcc += energy;
  csiFluxAcc   += flux;
  portEXIT_CRITICAL_ISR(&csiMux);
}

// Drain the window, derive presence/motion/confidence, and emit the sealed verdict line.
static void emitVerdict() {
  uint32_t pkts;
  float energySum, fluxSum;
  portENTER_CRITICAL(&csiMux);
  pkts = csiPkts;  energySum = csiEnergyAcc;  fluxSum = csiFluxAcc;
  csiPkts = 0;  csiEnergyAcc = 0.0f;  csiFluxAcc = 0.0f;
  portEXIT_CRITICAL(&csiMux);

  float motion = 0.0f, conf = 0.0f;
  bool presence = false;
  if (pkts > 0) {
    float meanEnergy = energySum / (float)pkts;
    float meanFlux   = fluxSum / (float)pkts;
    if (!csiBaselineInit) { csiBaseline = meanEnergy; csiBaselineInit = true; }
    float dev = csiBaseline > 0.0f ? fabsf(meanEnergy - csiBaseline) / csiBaseline : 0.0f;
    motion = meanFlux / CSI_MOTION_SCALE;
    if (motion > 1.0f) motion = 1.0f;
    presence = (motion >= CSI_PRESENCE_MOTION) || (dev >= CSI_PRESENCE_DEV);
    // Adapt the baseline SLOWLY while occupied so a present body isn't absorbed into "normal".
    float alpha = presence ? 0.01f : 0.1f;
    csiBaseline = (1.0f - alpha) * csiBaseline + alpha * meanEnergy;
    conf = (float)pkts / (float)CSI_CONF_SAT_PKTS;
    if (conf > 1.0f) conf = 1.0f;
    if (pkts < CSI_MIN_PKTS) conf *= 0.4f;      // too little evidence this window
  }
  char line[48];                                // "csi presence=1 motion=0.42 conf=0.82" = 36 B < 219
  snprintf(line, sizeof(line), "csi presence=%d motion=%.2f conf=%.2f",
           presence ? 1 : 0, motion, conf);
  sendLine(line);
}

// Handle one authenticated command. Customise per deployment; the default proves the round-trip.
static void handleCommand(const uint8_t *pt, size_t len) {
  char cmd[MAX_PT + 1];
  size_t n = len < MAX_PT ? len : MAX_PT;
  memcpy(cmd, pt, n);
  cmd[n] = '\0';
  char reply[MAX_PT + 1];
  if (strcmp(cmd, "ping") == 0) {
    snprintf(reply, sizeof(reply), "node %u: pong", (unsigned)NODE_ID);
  } else {
    snprintf(reply, sizeof(reply), "node %u: ran '%s'", (unsigned)NODE_ID, cmd);
  }
  sendLine(reply);
}

void onEspNowRecv(const uint8_t *mac, const uint8_t *data, int len) {
  (void)mac;
  if (len <= 0) return;
  uint8_t pt[MAX_PT];
  int n = openFrame(data, (size_t)len, pt, sizeof(pt));
  if (n >= 0) handleCommand(pt, (size_t)n);
}

void setup() {
  Serial.begin(115200);
  prefs.begin("ccnode", false);
  txEpoch = prefs.getUInt("epoch", 0) + 1;   // bump every boot so a reset never reuses a nonce
  prefs.putUInt("epoch", txEpoch);
  txCounter = 0;

  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  esp_wifi_set_channel(CHANNEL, WIFI_SECOND_CHAN_NONE);
  if (esp_now_init() != ESP_OK) { while (true) { delay(1000); } }
  esp_now_register_recv_cb(onEspNowRecv);

  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, BROADCAST, 6);
  peer.channel = CHANNEL;
  peer.encrypt = false;                      // frames are already sealed end-to-end
  esp_now_add_peer(&peer);

  // WS1 CSI: capture channel-state on received packets and route it to onCsi(). lltf+htltf give the
  // most subcarriers; channel_filter keeps only the operating channel; manu_scale off lets the radio
  // auto-scale amplitudes (we only compare relative energy, so absolute scale doesn't matter).
  wifi_csi_config_t csiCfg = {};
  csiCfg.lltf_en          = true;
  csiCfg.htltf_en         = true;
  csiCfg.stbc_htltf2_en   = true;
  csiCfg.ltf_merge_en     = true;
  csiCfg.channel_filter_en = true;
  csiCfg.manu_scale       = false;
  csiCfg.shift            = 0;
  esp_wifi_set_csi_config(&csiCfg);
  esp_wifi_set_csi_rx_cb(onCsi, NULL);
  esp_wifi_set_csi(true);

  sendLine("online");                        // announce ourselves through the relay
}

void loop() {
  static uint32_t lastVerdict = 0;
  static uint32_t lastBeat = 0;
  uint32_t now = millis();
  if (now - lastVerdict >= 1000) {           // one CSI verdict per second
    lastVerdict = now;
    emitVerdict();
  }
  if (now - lastBeat >= 30000) {             // liveness heartbeat every 30 s
    lastBeat = now;
    sendLine("heartbeat");
  }
  delay(10);
}
