/*
 * Cyber Controller — wireless node RELAY (gateway) firmware.  Wire protocol: W1.0.
 *
 * Role: a USB-tethered ESP32 that bridges the host's serial link to the ESP-NOW radio the remote nodes
 * speak.  It is a DUMB PIPE by design — encryption is end-to-end between the host and each node
 * (AES-256-GCM, see ../PROTOCOL.md and src/core/node_crypto.py), so the relay holds no keys and can read
 * nothing.  Downlink frames are broadcast; only the node whose key matches can unseal one, every other
 * node drops it on the tag check.  That is why there is no routing table here.
 *
 * Serial (host):  one base64-encoded sealed frame per line, '\n' terminated.
 * ESP-NOW (nodes): the raw sealed frame bytes (<= 250, the ESP-NOW payload budget).
 *
 * STATUS: real implementation, but NOT compiled or flashed in this environment (no arduino-cli/toolchain
 * available here) and NOT yet validated on hardware.  Review against ../PROTOCOL.md before flashing.
 * Target: ESP32 Arduino core 2.x (the classic esp_now_recv_cb signature below).  On core 3.x the receive
 * callback's first argument becomes `const esp_now_recv_info_t *` — adjust onEspNowRecv accordingly.
 */
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include "mbedtls/base64.h"

static const uint8_t BROADCAST[6]   = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
static const uint8_t ESPNOW_CHANNEL = 1;      // relay + nodes must agree on one fixed channel
static const size_t  MAX_FRAME      = 250;    // ESP-NOW payload MTU (== node_crypto.ESP_NOW_MTU)
static const size_t  MAX_LINE       = 512;    // base64 of 250 bytes is ~344 chars; leave slack
static const size_t  B64_OUT        = 356;    // 4*ceil(250/3) rounded up

static char   lineBuf[MAX_LINE];
static size_t lineLen = 0;

// A raw sealed frame arrived from a node -> base64-encode -> emit one line to the host.
void onEspNowRecv(const uint8_t *mac, const uint8_t *data, int len) {
  (void)mac;
  if (len <= 0 || (size_t)len > MAX_FRAME) return;   // never forward an over-MTU/empty frame
  uint8_t out[B64_OUT];
  size_t olen = 0;
  if (mbedtls_base64_encode(out, sizeof(out), &olen, data, (size_t)len) != 0) return;
  Serial.write(out, olen);
  Serial.write('\n');
}

// A complete base64 line from the host = a sealed frame for a node -> decode -> broadcast over ESP-NOW.
static void handleLine(const char *b64, size_t n) {
  if (n == 0) return;
  uint8_t frame[MAX_FRAME];
  size_t flen = 0;
  if (mbedtls_base64_decode(frame, sizeof(frame), &flen, (const uint8_t *)b64, n) != 0) return; // drop junk
  if (flen == 0 || flen > MAX_FRAME) return;
  esp_now_send(BROADCAST, frame, flen);              // only the intended node's key can unseal it
}

void setup() {
  Serial.begin(115200);
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  esp_wifi_set_channel(ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE);

  if (esp_now_init() != ESP_OK) {
    // Without ESP-NOW there is nothing to relay; blink-idle so the failure is obvious on the bench.
    while (true) { delay(1000); }
  }
  esp_now_register_recv_cb(onEspNowRecv);

  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, BROADCAST, 6);
  peer.channel = ESPNOW_CHANNEL;
  peer.encrypt = false;                              // link-layer crypto off; the frames are already sealed
  esp_now_add_peer(&peer);
}

void loop() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (lineLen > 0) { handleLine(lineBuf, lineLen); lineLen = 0; }
    } else if (lineLen < MAX_LINE - 1) {
      lineBuf[lineLen++] = c;
    } else {
      lineLen = 0;                                    // oversized line -> drop it rather than overflow
    }
  }
}
