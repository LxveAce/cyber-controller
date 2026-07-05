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
 * STATUS: real implementation, but NOT compiled or flashed in this environment (no arduino-cli/toolchain
 * here) and NOT hardware-validated.  Reviewed against ../PROTOCOL.md + node_crypto.py.  Target: ESP32
 * Arduino core 2.x (classic esp_now recv-cb signature).  On core 3.x update onEspNowRecv's first arg.
 */
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <Preferences.h>
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

  sendLine("online");                        // announce ourselves through the relay
}

void loop() {
  static uint32_t last = 0;
  if (millis() - last > 30000) {             // heartbeat every 30 s
    last = millis();
    sendLine("heartbeat");
  }
  delay(10);
}
