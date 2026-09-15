/*
 * swaptx_sniffer - listen-only radio sniffer for SWAPTX Evolver laser tag.
 *
 * Board: any classic ESP32 (ESP32-WROOM-32U/UE with external antenna recommended,
 *        e.g. Lonely Binary ESP32 WROOM-32UE IPEX kit). Arduino-ESP32 core 3.x.
 *        Needs the "Huge APP" partition scheme (BLE + WiFi): see flash.sh.
 *
 * What it does
 *   Puts the radio in promiscuous mode with 802.11b/g/n AND the Espressif long-range (LR)
 *   PHY enabled, so it hears EVERY ESP-NOW frame on the channel - broadcasts, host unicasts
 *   and player-to-player unicasts alike - without ever transmitting or ACKing anything.
 *   Each ESP-NOW frame is printed to USB serial as one JSON line:
 *     {"type":"frame","ms":1234,"src":"00:00:00:00:00:03","dst":"ff:ff:ff:ff:ff:ff",
 *      "rssi":-61,"ch":1,"seq":412,"rate":11,"len":18,"txt":"36,101,2,1,3,42","hex":"3336..."}
 *
 *   For mapping a system from scratch it also reports EVERY OTHER 802.11 management/data
 *   frame it hears, summarised and rate limited per (subtype, sender, receiver):
 *     {"type":"wifi","ms":..,"ft":"mgmt","sub":"beacon","src":..,"dst":..,"bssid":..,
 *      "rssi":-50,"ch":1,"len":231,"n":12,"ssid":"MyNetwork"}
 *   ("n" = frames of that kind since the previous line; "ssid" for beacons/probes, "hex" for
 *   action frames from other vendors). So a gun<->headset link that is NOT ESP-NOW still shows.
 *
 *   plus status lines ({"type":"boot"|"status"|"lock"|"scan"|"survey"|"ble"|"ack"|"error", ...}).
 *
 * Serial commands (115200 baud, newline terminated)
 *   chan,N        lock to WiFi channel N (1..13) and remember it
 *   scan          hop channels until an ESP-NOW frame is heard, then lock
 *   autoscan,0|1  hop automatically after 120 s of radio silence (default 1)
 *   all,0|1       report non-ESP-NOW frames as "wifi" summaries (default 1)
 *   survey[,S]    dwell S seconds (default 2) on every channel 1..13, counting ESP-NOW /
 *                 management / data frames and Espressif senders, then return to the channel
 *   ble[,S]       scan Bluetooth LE advertisements for S seconds (default 8): one "ble" line
 *                 per device (address, rssi, name, manufacturer data, service uuid). WiFi
 *                 capture pauses during the scan.
 *   mode,sniff    promiscuous capture (default)      mode,espnow  callback fallback (reboots)
 *   raw,0|1       include the hex payload (default 1)
 *   host,1|0      HOST MODE: take the host address 00:00:00:00:00:ff (so player headsets' unicasts
 *                 are ACKed by this radio) and allow transmitting. Sniffing keeps running. The
 *                 dongle never decides what to send: the backend does, with "tx".
 *   tx,<dst>,<payload>  send an ESP-NOW frame (host mode only). dst = "bcast" or a MAC; the payload is
 *                 the bare ASCII message, exactly what the stock headsets and the DIY host send.
 *   rate,<n>      ESP-NOW transmit rate: 0 = 1 Mbps (default, every 802.11 receiver decodes it),
 *                 25 = MCS1 short-GI, the rate the stock gear itself uses (wifi_phy_rate_t values)
 *   status        print a status line               help / reboot
 *
 * Optional IR receiver (TSOP4138 etc.) on IR_RX_PIN: reports Evolver IR tags seen by the
 * dongle itself as {"type":"ir","pulses":[...]} - off unless IR_RX_PIN >= 0.
 */
#include <Arduino.h>
#include <WiFi.h>
#include <esp_wifi.h>
#include <esp_now.h>
#include <Preferences.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <BLEDevice.h>
#include <BLEScan.h>
#include <BLEAdvertisedDevice.h>

#define FW_VERSION "swaptx-sniffer 1.2"
#define LED_PIN 2            // on-board LED on most DevKitC-style boards; blinks per frame
#define IR_RX_PIN -1         // set to a GPIO with an IR receiver to also capture IR tags
#define DEFAULT_CHANNEL 1    // ESP-NOW default; SWAPTX gear has never been seen elsewhere
#define SILENCE_BEFORE_AUTOSCAN_MS 120000UL
#define SCAN_DWELL_MS 400
#define STATUS_EVERY_MS 10000UL
#define OTHER_EVERY_MS 5000UL   // one "wifi" summary per (subtype, src, dst) at most this often
#define MAX_BODY 250
#define OTHER_BODY 40

struct Capture {
  uint8_t kind;        // 0 = ESP-NOW frame, 1 = other 802.11 frame summary
  uint8_t ftype;       // 802.11 type (0 mgmt, 2 data) for kind 1
  uint8_t fsub;        // 802.11 subtype for kind 1
  uint32_t ms;
  uint8_t src[6];
  uint8_t dst[6];
  uint8_t bssid[6];
  int8_t rssi;
  uint8_t channel;
  uint16_t seq;
  uint8_t rate;
  uint8_t len;         // body bytes (ESP-NOW payload, or SSID / first bytes for kind 1)
  uint16_t flen;       // whole frame length for kind 1
  uint32_t count;      // frames folded into this summary (kind 1)
  uint8_t bodyIsSsid;  // kind 1: body holds an SSID (else raw bytes -> hex)
  uint8_t body[MAX_BODY];
};

static QueueHandle_t q;
static Preferences prefs;
static uint8_t channel = DEFAULT_CHANNEL;
static bool modeSniff = true;
static bool includeHex = true;
static bool autoscan = true;
static bool allFrames = true;
static bool hostMode = false;
static bool espnowReady = false;
static uint8_t txRate = 0;          // wifi_phy_rate_t; 0 = 1 Mbps long preamble
static uint32_t txTotal = 0, txFailed = 0;
static bool scanning = false;
static uint32_t lastFrameMs = 0;
static uint32_t lastScanHopMs = 0;
static uint32_t lastStatusMs = 0;
static uint32_t framesTotal = 0;
static uint32_t framesDropped = 0;
static uint32_t otherTotal = 0;
static uint32_t ledOffAt = 0;
static const uint8_t ESPRESSIF_OUI[3] = {0x18, 0xFE, 0x34};
static const uint8_t HOST_MAC[6] = {0x00, 0x00, 0x00, 0x00, 0x00, 0xFF};

// survey state (channel sweep with counters)
static bool surveying = false;
static uint8_t surveyCh = 1;
static uint8_t surveyReturnCh = 1;
static uint32_t surveyDwellMs = 2000;
static uint32_t surveyHopAt = 0;
static volatile uint32_t cntEspnow = 0, cntMgmt = 0, cntData = 0;

// rate limiter for "other" frames: 64 slots keyed by a hash of (type, subtype, src, dst)
struct Seen { uint32_t key; uint32_t lastMs; uint32_t count; };
static Seen seen[64];

// ---------------------------------------------------------------- helpers
static void macToStr(const uint8_t *m, char *out) {
  snprintf(out, 18, "%02x:%02x:%02x:%02x:%02x:%02x", m[0], m[1], m[2], m[3], m[4], m[5]);
}

static void printJsonEscaped(const uint8_t *b, uint16_t len) {
  for (uint16_t i = 0; i < len; i++) {
    uint8_t c = b[i];
    if (c == '"' || c == '\\') { Serial.write('\\'); Serial.write(c); }
    else if (c >= 0x20 && c < 0x7F) { Serial.write(c); }
    else { Serial.printf("\\u%04x", c); }
  }
}

static const char *subtypeName(uint8_t ftype, uint8_t fsub) {
  if (ftype == 0) {
    switch (fsub) {
      case 0: return "assoc_req"; case 1: return "assoc_resp"; case 2: return "reassoc_req"; case 3: return "reassoc_resp";
      case 4: return "probe_req"; case 5: return "probe_resp"; case 8: return "beacon"; case 9: return "atim";
      case 10: return "disassoc"; case 11: return "auth"; case 12: return "deauth"; case 13: return "action";
      default: return "mgmt";
    }
  }
  if (ftype == 2) {
    switch (fsub) {
      case 0: return "data"; case 4: return "null"; case 8: return "qos_data"; case 12: return "qos_null";
      default: return "data_other";
    }
  }
  return "other";
}

static void emitFrame(const Capture &c) {
  char s[18], d[18], b[18];
  macToStr(c.src, s);
  macToStr(c.dst, d);
  if (c.kind == 1) {
    macToStr(c.bssid, b);
    Serial.printf("{\"type\":\"wifi\",\"ms\":%lu,\"ft\":\"%s\",\"sub\":\"%s\",\"src\":\"%s\",\"dst\":\"%s\",\"bssid\":\"%s\",\"rssi\":%d,\"ch\":%u,\"len\":%u,\"n\":%lu",
                  (unsigned long)c.ms, c.ftype == 0 ? "mgmt" : (c.ftype == 2 ? "data" : "ctrl"), subtypeName(c.ftype, c.fsub),
                  s, d, b, (int)c.rssi, (unsigned)c.channel, (unsigned)c.flen, (unsigned long)c.count);
    if (c.len) {
      if (c.bodyIsSsid) { Serial.print(",\"ssid\":\""); printJsonEscaped(c.body, c.len); Serial.print("\""); }
      else { Serial.print(",\"hex\":\""); for (uint8_t i = 0; i < c.len; i++) Serial.printf("%02x", c.body[i]); Serial.print("\""); }
    }
    Serial.println("}");
    return;
  }
  Serial.printf("{\"type\":\"frame\",\"ms\":%lu,\"src\":\"%s\",\"dst\":\"%s\",\"rssi\":%d,\"ch\":%u,\"seq\":%u,\"rate\":%u,\"len\":%u,\"txt\":\"",
                (unsigned long)c.ms, s, d, (int)c.rssi, (unsigned)c.channel, (unsigned)c.seq, (unsigned)c.rate, (unsigned)c.len);
  printJsonEscaped(c.body, c.len);
  Serial.print("\"");
  if (includeHex) {
    Serial.print(",\"hex\":\"");
    for (uint8_t i = 0; i < c.len; i++) Serial.printf("%02x", c.body[i]);
    Serial.print("\"");
  }
  Serial.println("}");
}

static void emitStatus() {
  Serial.printf("{\"type\":\"status\",\"fw\":\"%s\",\"mode\":\"%s\",\"ch\":%u,\"frames\":%lu,\"other\":%lu,\"dropped\":%lu,\"uptime_s\":%lu,\"heap\":%u,\"scanning\":%s,\"autoscan\":%s,\"all\":%s,\"surveying\":%s,\"host\":%s,\"tx\":%lu,\"tx_failed\":%lu,\"rate\":%u,\"mac\":\"%s\"}\n",
                FW_VERSION, modeSniff ? "sniff" : "espnow", (unsigned)channel, (unsigned long)framesTotal, (unsigned long)otherTotal,
                (unsigned long)framesDropped, (unsigned long)(millis() / 1000), (unsigned)ESP.getFreeHeap(),
                scanning ? "true" : "false", autoscan ? "true" : "false", allFrames ? "true" : "false", surveying ? "true" : "false",
                hostMode ? "true" : "false", (unsigned long)txTotal, (unsigned long)txFailed, (unsigned)txRate, WiFi.macAddress().c_str());
}

// ----------------------------------------------------------------- host mode (transmit)
// The stock admin headset is 00:00:00:00:00:ff. Taking that address makes the radio ACK the
// players' unicasts (report-ins, state reports) so they stop retrying, and lets us answer them.
// Sniffing keeps running in parallel: we still hear every frame, including player-to-player ones.
static bool parseMac(const char *txt, uint8_t *out) {
  if (!strcasecmp(txt, "bcast") || !strcasecmp(txt, "broadcast")) { memset(out, 0xff, 6); return true; }
  unsigned v[6];
  if (sscanf(txt, "%x:%x:%x:%x:%x:%x", &v[0], &v[1], &v[2], &v[3], &v[4], &v[5]) != 6) return false;
  for (int i = 0; i < 6; i++) out[i] = (uint8_t)v[i];
  return true;
}

static void applyTxRate() {
  if (!espnowReady) return;
  esp_wifi_config_espnow_rate(WIFI_IF_STA, (wifi_phy_rate_t)txRate);
}

static bool ensurePeer(const uint8_t *mac) {
  if (esp_now_is_peer_exist(mac)) return true;
  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, mac, 6);
  peer.channel = 0;            // current channel
  peer.ifidx = WIFI_IF_STA;
  peer.encrypt = false;
  return esp_now_add_peer(&peer) == ESP_OK;
}

static void hostEnable(bool on) {
  hostMode = on;
  prefs.putBool("host", on);
  if (on) {
    esp_wifi_set_promiscuous(false);
    esp_wifi_set_mac(WIFI_IF_STA, HOST_MAC);
    esp_wifi_set_promiscuous(true);
    setChannel(channel);
    if (!espnowReady && esp_now_init() == ESP_OK) espnowReady = true;
    if (espnowReady) {
      applyTxRate();
      uint8_t b[6]; memset(b, 0xff, 6); ensurePeer(b);
    }
    WiFi.setTxPower(WIFI_POWER_19_5dBm);
  }
  Serial.printf("{\"type\":\"host\",\"host\":%s,\"mac\":\"%s\",\"espnow\":%s,\"rate\":%u}\n",
                hostMode ? "true" : "false", WiFi.macAddress().c_str(), espnowReady ? "true" : "false", (unsigned)txRate);
}

static void txSend(const String &dstTxt, const String &payload) {
  if (!hostMode || !espnowReady) { Serial.println("{\"type\":\"error\",\"msg\":\"tx needs host,1 first\"}"); return; }
  uint8_t dst[6];
  if (!parseMac(dstTxt.c_str(), dst)) { Serial.println("{\"type\":\"error\",\"msg\":\"bad tx destination\"}"); return; }
  if (payload.length() == 0 || payload.length() > 200) { Serial.println("{\"type\":\"error\",\"msg\":\"bad tx payload\"}"); return; }
  if (!ensurePeer(dst)) { Serial.println("{\"type\":\"error\",\"msg\":\"could not add peer\"}"); txFailed++; return; }
  esp_err_t r = esp_now_send(dst, (const uint8_t *)payload.c_str(), payload.length());   // bare text, like the stock gear
  txTotal++;
  if (r != ESP_OK) txFailed++;
  char d[18]; macToStr(dst, d);
  Serial.printf("{\"type\":\"tx\",\"ms\":%lu,\"dst\":\"%s\",\"len\":%u,\"ok\":%s,\"txt\":\"", (unsigned long)millis(), d, (unsigned)payload.length(), r == ESP_OK ? "true" : "false");
  printJsonEscaped((const uint8_t *)payload.c_str(), (uint16_t)payload.length());
  Serial.println("\"}");
}

static void setChannel(uint8_t ch) {
  if (ch < 1 || ch > 13) return;
  channel = ch;
  esp_wifi_set_channel(channel, WIFI_SECOND_CHAN_NONE);
}

static void enqueue(const uint8_t *src, const uint8_t *dst, const uint8_t *body, int len,
                    int8_t rssi, uint8_t ch, uint16_t seq, uint8_t rate) {
  Capture c;
  memset(&c, 0, sizeof(c));
  c.kind = 0;
  c.ms = millis();
  memcpy(c.src, src, 6);
  memcpy(c.dst, dst, 6);
  c.rssi = rssi;
  c.channel = ch;
  c.seq = seq;
  c.rate = rate;
  c.len = (uint8_t)(len > MAX_BODY ? MAX_BODY : (len < 0 ? 0 : len));
  memcpy(c.body, body, c.len);
  if (xQueueSendFromISR(q, &c, NULL) != pdTRUE) framesDropped++;
}

static inline uint32_t fnv(uint32_t h, uint8_t b) { h ^= b; return h * 16777619u; }

// Non-ESP-NOW frame: fold repeats into one summary line per (type, subtype, src, dst) every OTHER_EVERY_MS.
static void IRAM_ATTR enqueueOther(const uint8_t *p, int total, int8_t rssi, uint8_t ch, uint8_t ftype, uint8_t fsub) {
  const uint8_t *a1 = p + 4, *a2 = p + 10, *a3 = p + 16;
  uint32_t key = 2166136261u;
  key = fnv(key, ftype); key = fnv(key, fsub);
  for (int i = 0; i < 6; i++) key = fnv(key, a2[i]);
  for (int i = 0; i < 6; i++) key = fnv(key, a1[i]);
  uint32_t now = millis();
  Seen &s = seen[key & 63];
  if (s.key == key && (now - s.lastMs) < OTHER_EVERY_MS) { s.count++; return; }
  uint32_t folded = (s.key == key) ? s.count + 1 : 1;
  s.key = key; s.lastMs = now; s.count = 0;

  Capture c;
  memset(&c, 0, sizeof(c));
  c.kind = 1; c.ftype = ftype; c.fsub = fsub;
  c.ms = now;
  memcpy(c.src, a2, 6); memcpy(c.dst, a1, 6); memcpy(c.bssid, a3, 6);
  c.rssi = rssi; c.channel = ch; c.flen = (uint16_t)total; c.count = folded;
  // SSID for beacons / probe responses (tagged params after 12 fixed bytes) and probe requests (right after the header)
  int tagAt = -1;
  if (ftype == 0 && (fsub == 8 || fsub == 5)) tagAt = 36;
  else if (ftype == 0 && fsub == 4) tagAt = 24;
  if (tagAt >= 0 && tagAt + 2 <= total && p[tagAt] == 0) {
    int l = p[tagAt + 1];
    if (l > 32) l = 32;
    if (tagAt + 2 + l <= total) { memcpy(c.body, p + tagAt + 2, l); c.len = l; c.bodyIsSsid = 1; }
  } else if (ftype == 0 && fsub == 13) {           // action frame from some other vendor: keep its head
    int l = total - 24; if (l > OTHER_BODY) l = OTHER_BODY; if (l < 0) l = 0;
    memcpy(c.body, p + 24, l); c.len = l; c.bodyIsSsid = 0;
  }
  if (xQueueSendFromISR(q, &c, NULL) != pdTRUE) framesDropped++;
}

// ------------------------------------------------- promiscuous (sniff) mode
// ESP-NOW frames are 802.11 vendor-specific action frames:
//   [0..1] FC 0xD0 0x00  [4..9] DA  [10..15] SA  [16..21] BSSID  [22..23] seq
//   [24] category 0x7F   [25..27] OUI 18:FE:34   [28..31] random
//   [32] element id 0xDD [33] length  [34..36] OUI 18:FE:34  [37] type 0x04  [38] version  [39..] body
static void IRAM_ATTR sniffCallback(void *buf, wifi_promiscuous_pkt_type_t type) {
  const wifi_promiscuous_pkt_t *pkt = (const wifi_promiscuous_pkt_t *)buf;
  const uint8_t *p = pkt->payload;
  int total = pkt->rx_ctrl.sig_len - 4;   // strip FCS
  if (total < 24) return;
  uint8_t ftype = (p[0] >> 2) & 3, fsub = (p[0] >> 4) & 0xF;
  if (type == WIFI_PKT_MGMT) cntMgmt++; else if (type == WIFI_PKT_DATA) cntData++;
  bool espnow = (type == WIFI_PKT_MGMT && total >= 40 && p[0] == 0xD0 && p[24] == 0x7F &&
                 memcmp(p + 25, ESPRESSIF_OUI, 3) == 0 && p[32] == 0xDD && memcmp(p + 34, ESPRESSIF_OUI, 3) == 0 && p[37] == 0x04);
  if (!espnow) {
    if (type != WIFI_PKT_MGMT && type != WIFI_PKT_DATA) return;
    if (allFrames && !surveying) enqueueOther(p, total, pkt->rx_ctrl.rssi, pkt->rx_ctrl.channel, ftype, fsub);
    return;
  }
  cntEspnow++;
  int elen = p[33];
  int bodyLen = elen - 5;
  if (bodyLen < 0) return;
  if (39 + bodyLen > total) bodyLen = total - 39;
  uint16_t seq = ((uint16_t)p[23] << 4) | (p[22] >> 4);
  enqueue(p + 10, p + 4, p + 39, bodyLen, pkt->rx_ctrl.rssi, pkt->rx_ctrl.channel, seq, pkt->rx_ctrl.rate);
}

static void startSniff() {
  WiFi.mode(WIFI_STA);
  WiFi.disconnect(true, true);
  esp_wifi_set_ps(WIFI_PS_NONE);
  esp_wifi_set_protocol(WIFI_IF_STA, WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N | WIFI_PROTOCOL_LR);
  wifi_promiscuous_filter_t filt = {};
  filt.filter_mask = WIFI_PROMIS_FILTER_MASK_MGMT | WIFI_PROMIS_FILTER_MASK_DATA;
  esp_wifi_set_promiscuous_filter(&filt);
  esp_wifi_set_promiscuous_rx_cb(&sniffCallback);
  esp_wifi_set_promiscuous(true);
  setChannel(channel);
}

// ------------------------------------------------- ESP-NOW callback fallback
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
static void espnowRecv(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
  int8_t rssi = info->rx_ctrl ? info->rx_ctrl->rssi : 0;
  uint8_t ch = info->rx_ctrl ? info->rx_ctrl->channel : channel;
  enqueue(info->src_addr, info->des_addr, data, len, rssi, ch, 0, 0);
}
#else
static void espnowRecv(const uint8_t *mac, const uint8_t *data, int len) {
  static const uint8_t bcast[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
  enqueue(mac, bcast, data, len, 0, channel, 0, 0);
}
#endif

static void startEspNow() {
  WiFi.mode(WIFI_STA);
  WiFi.disconnect(true, true);
  esp_wifi_set_protocol(WIFI_IF_STA, WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N | WIFI_PROTOCOL_LR);
  esp_wifi_set_mac(WIFI_IF_STA, HOST_MAC);   // hear unicasts meant for the host, like the stock transceiver
  setChannel(channel);
  if (esp_now_init() != ESP_OK) {
    Serial.println("{\"type\":\"error\",\"msg\":\"esp_now_init failed\"}");
    delay(2000);
    ESP.restart();
  }
  esp_now_register_recv_cb(espnowRecv);
}

// ----------------------------------------------------------------- survey
static void surveyStart(uint32_t dwellMs) {
  surveying = true;
  scanning = false;
  surveyReturnCh = channel;
  surveyDwellMs = dwellMs;
  surveyCh = 1;
  cntEspnow = cntMgmt = cntData = 0;
  setChannel(surveyCh);
  surveyHopAt = millis() + surveyDwellMs;
  Serial.printf("{\"type\":\"survey\",\"state\":\"start\",\"dwell_ms\":%lu}\n", (unsigned long)dwellMs);
}

static void surveyTick() {
  if (!surveying || (int32_t)(millis() - surveyHopAt) < 0) return;
  Serial.printf("{\"type\":\"survey\",\"ch\":%u,\"espnow\":%lu,\"mgmt\":%lu,\"data\":%lu}\n",
                (unsigned)surveyCh, (unsigned long)cntEspnow, (unsigned long)cntMgmt, (unsigned long)cntData);
  if (surveyCh >= 13) {
    surveying = false;
    setChannel(surveyReturnCh);
    Serial.printf("{\"type\":\"survey\",\"state\":\"done\",\"ch\":%u}\n", (unsigned)channel);
    return;
  }
  surveyCh++;
  cntEspnow = cntMgmt = cntData = 0;
  setChannel(surveyCh);
  surveyHopAt = millis() + surveyDwellMs;
}

// ----------------------------------------------------------------- BLE scan
class AdvCb : public BLEAdvertisedDeviceCallbacks {
  void onResult(BLEAdvertisedDevice d) override {
    Serial.printf("{\"type\":\"ble\",\"ms\":%lu,\"addr\":\"%s\",\"rssi\":%d", (unsigned long)millis(), d.getAddress().toString().c_str(), d.getRSSI());
    if (d.haveName()) {
      auto n = d.getName();
      Serial.print(",\"name\":\""); printJsonEscaped((const uint8_t *)n.c_str(), (uint16_t)n.length()); Serial.print("\"");
    }
    if (d.haveManufacturerData()) {
      auto m = d.getManufacturerData();
      Serial.print(",\"mfg\":\"");
      for (size_t i = 0; i < m.length(); i++) Serial.printf("%02x", (uint8_t)m[i]);
      Serial.print("\"");
    }
    if (d.haveServiceUUID()) Serial.printf(",\"svc\":\"%s\"", d.getServiceUUID().toString().c_str());
    if (d.haveTXPower()) Serial.printf(",\"txpower\":%d", (int)d.getTXPower());
    Serial.println("}");
  }
};

static void bleScan(int secs) {
  if (secs < 1) secs = 1;
  if (secs > 60) secs = 60;
  bool wasSniff = modeSniff;
  if (wasSniff) esp_wifi_set_promiscuous(false);      // WiFi and BLE share the radio; pause capture
  static bool inited = false;
  if (!inited) { BLEDevice::init(""); inited = true; }
  BLEScan *s = BLEDevice::getScan();
  s->setAdvertisedDeviceCallbacks(new AdvCb(), true);
  s->setActiveScan(true);
  s->setInterval(100);
  s->setWindow(99);
  Serial.printf("{\"type\":\"ble_scan\",\"state\":\"start\",\"secs\":%d}\n", secs);
  s->start(secs, false);
  s->clearResults();
  if (wasSniff) esp_wifi_set_promiscuous(true);
  Serial.println("{\"type\":\"ble_scan\",\"state\":\"done\"}");
}

// ----------------------------------------------------------------- IR (opt)
#if IR_RX_PIN >= 0
static void irTask(void *arg) {
  for (;;) {
    unsigned long sync = pulseIn(IR_RX_PIN, LOW, 150000);
    if (sync > 2250) {
      unsigned long pulses[23];
      int n = 0;
      for (; n < 23; n++) {
        pulses[n] = pulseIn(IR_RX_PIN, LOW, 5000);
        if (pulses[n] == 0) break;
      }
      Serial.printf("{\"type\":\"ir\",\"ms\":%lu,\"pulses\":[%lu", millis(), sync);
      for (int i = 0; i < n; i++) Serial.printf(",%lu", pulses[i]);
      Serial.println("]}");
    }
    vTaskDelay(1);
  }
}
#endif

// ----------------------------------------------------------------- commands
static void handleCommand(String line) {
  line.trim();
  if (!line.length()) return;
  String low = line;
  low.toLowerCase();
  if (low.startsWith("chan,")) {
    int ch = low.substring(5).toInt();
    if (ch >= 1 && ch <= 13) {
      scanning = false;
      surveying = false;
      setChannel(ch);
      prefs.putUChar("ch", channel);
      Serial.printf("{\"type\":\"lock\",\"ch\":%u,\"reason\":\"command\"}\n", (unsigned)channel);
    } else {
      Serial.println("{\"type\":\"error\",\"msg\":\"channel must be 1..13\"}");
    }
  } else if (low == "scan") {
    scanning = true;
    surveying = false;
    lastScanHopMs = millis();
    Serial.printf("{\"type\":\"scan\",\"ch\":%u}\n", (unsigned)channel);
  } else if (low.startsWith("autoscan,")) {
    autoscan = low.substring(9).toInt() != 0;
    prefs.putBool("autoscan", autoscan);
    Serial.printf("{\"type\":\"ack\",\"autoscan\":%s}\n", autoscan ? "true" : "false");
  } else if (low.startsWith("all,")) {
    allFrames = low.substring(4).toInt() != 0;
    prefs.putBool("all", allFrames);
    Serial.printf("{\"type\":\"ack\",\"all\":%s}\n", allFrames ? "true" : "false");
  } else if (low == "survey" || low.startsWith("survey,")) {
    int secs = low.startsWith("survey,") ? low.substring(7).toInt() : 2;
    if (secs < 1) secs = 1;
    if (secs > 30) secs = 30;
    surveyStart((uint32_t)secs * 1000UL);
  } else if (low == "ble" || low.startsWith("ble,")) {
    int secs = low.startsWith("ble,") ? low.substring(4).toInt() : 8;
    bleScan(secs);
  } else if (low.startsWith("host,")) {
    hostEnable(low.substring(5).toInt() != 0);
  } else if (low.startsWith("tx,")) {
    int c = line.indexOf(',', 3);
    if (c < 0) Serial.println("{\"type\":\"error\",\"msg\":\"tx,<dst>,<payload>\"}");
    else txSend(line.substring(3, c), line.substring(c + 1));
  } else if (low.startsWith("rate,")) {
    int v = low.substring(5).toInt();
    if (v < 0 || v > 63) { Serial.println("{\"type\":\"error\",\"msg\":\"rate 0..63\"}"); }
    else { txRate = (uint8_t)v; prefs.putUChar("rate", txRate); applyTxRate(); Serial.printf("{\"type\":\"ack\",\"rate\":%u}\n", (unsigned)txRate); }
  } else if (low.startsWith("raw,")) {
    includeHex = low.substring(4).toInt() != 0;
    Serial.printf("{\"type\":\"ack\",\"raw\":%s}\n", includeHex ? "true" : "false");
  } else if (low.startsWith("mode,")) {
    String m = low.substring(5);
    bool sniff = (m != "espnow");
    prefs.putBool("sniff", sniff);
    Serial.printf("{\"type\":\"ack\",\"mode\":\"%s\",\"rebooting\":true}\n", sniff ? "sniff" : "espnow");
    delay(100);
    ESP.restart();
  } else if (low == "status") {
    emitStatus();
  } else if (low == "reboot") {
    Serial.println("{\"type\":\"ack\",\"rebooting\":true}");
    delay(100);
    ESP.restart();
  } else if (low == "help") {
    Serial.println("{\"type\":\"help\",\"commands\":[\"chan,N\",\"scan\",\"autoscan,0|1\",\"all,0|1\",\"survey[,S]\",\"ble[,S]\",\"host,0|1\",\"tx,<dst>,<payload>\",\"rate,N\",\"mode,sniff|espnow\",\"raw,0|1\",\"status\",\"reboot\"]}");
  } else {
    Serial.println("{\"type\":\"error\",\"msg\":\"unknown command (this dongle only transmits in host mode)\"}");
  }
}

// --------------------------------------------------------------------- main
void setup() {
  Serial.begin(115200);
  delay(50);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);
  q = xQueueCreate(48, sizeof(Capture));
  memset(seen, 0, sizeof(seen));
  prefs.begin("swaptx", false);
  channel = prefs.getUChar("ch", DEFAULT_CHANNEL);
  if (channel < 1 || channel > 13) channel = DEFAULT_CHANNEL;
  modeSniff = prefs.getBool("sniff", true);
  autoscan = prefs.getBool("autoscan", true);
  allFrames = prefs.getBool("all", true);
  txRate = prefs.getUChar("rate", 0);

  if (modeSniff) startSniff(); else startEspNow();
  if (prefs.getBool("host", false)) hostEnable(true);   // a host that reboots mid-game stays the host
#if IR_RX_PIN >= 0
  pinMode(IR_RX_PIN, INPUT);
  xTaskCreatePinnedToCore(irTask, "ir", 4096, NULL, 1, NULL, 1);
#endif
  lastFrameMs = millis();
  Serial.printf("{\"type\":\"boot\",\"fw\":\"%s\",\"mac\":\"%s\",\"mode\":\"%s\",\"ch\":%u,\"lr\":true,\"autoscan\":%s,\"all\":%s,\"host\":%s}\n",
                FW_VERSION, WiFi.macAddress().c_str(), modeSniff ? "sniff" : "espnow", (unsigned)channel,
                autoscan ? "true" : "false", allFrames ? "true" : "false", hostMode ? "true" : "false");
}

void loop() {
  Capture c;
  while (xQueueReceive(q, &c, 0) == pdTRUE) {
    if (c.kind == 0) {
      framesTotal++;
      lastFrameMs = millis();
      if (scanning) {
        scanning = false;
        prefs.putUChar("ch", channel);
        Serial.printf("{\"type\":\"lock\",\"ch\":%u,\"reason\":\"heard traffic\"}\n", (unsigned)channel);
      }
      digitalWrite(LED_PIN, HIGH);
      ledOffAt = millis() + 30;
    } else {
      otherTotal++;
    }
    emitFrame(c);
  }
  if (ledOffAt && (int32_t)(millis() - ledOffAt) >= 0) { digitalWrite(LED_PIN, LOW); ledOffAt = 0; }

  if (Serial.available()) {
    String line = Serial.readStringUntil('\n');
    handleCommand(line);
  }

  surveyTick();
  uint32_t now = millis();
  if (!scanning && !surveying && autoscan && (now - lastFrameMs) > SILENCE_BEFORE_AUTOSCAN_MS) {
    scanning = true;
    lastScanHopMs = now;
    Serial.printf("{\"type\":\"scan\",\"ch\":%u,\"reason\":\"silence\"}\n", (unsigned)channel);
  }
  if (scanning && (now - lastScanHopMs) > SCAN_DWELL_MS) {
    lastScanHopMs = now;
    setChannel(channel >= 13 ? 1 : channel + 1);
  }
  if ((now - lastStatusMs) > STATUS_EVERY_MS) {
    lastStatusMs = now;
    emitStatus();
  }
}
