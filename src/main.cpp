// SMX UART frame logger (ESP32-S3)
//
// Receives the StepManiaX panel data bus (UART 250 kbaud 8N1, read-only) and
// hex-dumps each frame to the USB serial console. A frame ends on a BREAK or
// on an idle gap (the real bus separates frames with ~8 ms of idle-high).

#include <Arduino.h>
#include "driver/uart.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

#ifndef SMX_RX_PIN
#define SMX_RX_PIN 4
#endif
#ifndef SMX_RX_INVERT
#define SMX_RX_INVERT 0
#endif
#ifndef SMX_STRIP_BREAK_NULL
#define SMX_STRIP_BREAK_NULL 0
#endif
// 0: human-readable hex dump (smx_bus.py); 1: compact binary packets for
// the web/ overlay bridge (about 3x less USB traffic).
#ifndef SMX_OUTPUT_BINARY
#define SMX_OUTPUT_BINARY 0
#endif

static constexpr uart_port_t SMX_UART = UART_NUM_1;
static constexpr int SMX_BAUD = 250000;
static constexpr int RX_BUF_SIZE = 4096;   // driver ring buffer
static constexpr int MAX_FRAME = 2048;     // largest frame we keep
static constexpr int FRAME_QUEUE_LEN = 4;  // frames waiting to be printed

struct Frame {
  uint32_t seq;
  uint32_t t_us;      // micros() when BREAK was seen
  uint16_t len;
  bool overflow;      // frame was longer than MAX_FRAME (truncated)
  uint8_t data[MAX_FRAME];
};

struct Stats {
  volatile uint32_t frames;
  volatile uint32_t dropped;      // printer couldn't keep up
  volatile uint32_t overflows;    // frame > MAX_FRAME
  volatile uint32_t fifo_ovf;     // HW FIFO / driver buffer overflow
  volatile uint32_t frame_err;
  volatile uint32_t parity_err;
};

static QueueHandle_t uart_queue;   // IDF driver events
static QueueHandle_t free_q;       // Frame* pool: empty buffers
static QueueHandle_t full_q;       // Frame* pool: frames ready to print
static Frame frame_pool[FRAME_QUEUE_LEN + 1];
static Stats stats;

// Frames are separated by an idle gap (~8 ms observed); a BREAK is not
// reliably present. Any silence longer than this ends the current frame.
// Must be well above the interval between UART_DATA events during a burst
// (RX_FULL_THRESH bytes * 40 us), or a frame gets split mid-stream.
static constexpr TickType_t IDLE_GAP_TICKS = pdMS_TO_TICKS(4);
static constexpr int RX_FULL_THRESH = 32;  // bytes -> ~1.3 ms between events
static uint32_t last_rx_us = 0;  // micros() of the most recent data event

// ---------------------------------------------------------------------------
// Reader task: accumulates bytes, emits a frame on BREAK or idle gap.
// ---------------------------------------------------------------------------

static Frame *cur = nullptr;   // frame currently being filled (may be null)
static uint32_t seq = 0;
static bool in_frame = false;  // bytes received since the last frame end

static void append_bytes(size_t n) {
  uint8_t scratch[128];
  while (n > 0) {
    size_t chunk = n < sizeof(scratch) ? n : sizeof(scratch);
    int got = uart_read_bytes(SMX_UART, scratch, chunk, 0);
    if (got <= 0) break;
    n -= got;
    if (!cur) continue;  // no buffer available: discard
    size_t room = MAX_FRAME - cur->len;
    size_t take = (size_t)got < room ? (size_t)got : room;
    memcpy(cur->data + cur->len, scratch, take);
    cur->len += take;
    if (take < (size_t)got) cur->overflow = true;
  }
}

static void start_frame() {
  if (xQueueReceive(free_q, &cur, 0) != pdTRUE) {
    cur = nullptr;  // printer is behind; this frame will be dropped
    return;
  }
  cur->len = 0;
  cur->overflow = false;
}

static void finish_frame(uint32_t now) {
  seq++;
  if (!cur) {
    stats.dropped = stats.dropped + 1;
  } else {
#if SMX_STRIP_BREAK_NULL
    if (cur->len > 0 && cur->data[cur->len - 1] == 0x00) cur->len--;
#endif
    cur->seq = seq;
    cur->t_us = now;
    if (cur->overflow) stats.overflows = stats.overflows + 1;
    stats.frames = stats.frames + 1;
    xQueueSend(full_q, &cur, 0);  // can't fail: pool size == queue capacity
  }
  start_frame();
}

static void reader_task(void *) {
  uart_event_t ev;
  start_frame();
  for (;;) {
    if (xQueueReceive(uart_queue, &ev, IDLE_GAP_TICKS) != pdTRUE) {
      // Line has been idle: close the frame if anything was received.
      if (in_frame) {
        in_frame = false;
        finish_frame(last_rx_us);  // counts a drop if cur is null
      } else if (!cur) {
        start_frame();  // retry getting a buffer once the printer caught up
      }
      continue;
    }
    switch (ev.type) {
      case UART_DATA:
        append_bytes(ev.size);
        last_rx_us = micros();
        in_frame = true;
        break;
      case UART_BREAK: {
        // Pull in anything still buffered so it belongs to this frame.
        size_t pending = 0;
        uart_get_buffered_data_len(SMX_UART, &pending);
        if (pending) append_bytes(pending);
        in_frame = false;
        finish_frame(micros());
        break;
      }
      case UART_FIFO_OVF:
      case UART_BUFFER_FULL:
        stats.fifo_ovf = stats.fifo_ovf + 1;
        uart_flush_input(SMX_UART);
        xQueueReset(uart_queue);
        if (cur) { cur->len = 0; cur->overflow = false; }
        break;
      case UART_FRAME_ERR:
        stats.frame_err = stats.frame_err + 1;
        break;
      case UART_PARITY_ERR:
        stats.parity_err = stats.parity_err + 1;
        break;
      default:
        break;
    }
  }
}

// ---------------------------------------------------------------------------
// Printer (runs in loop()): hex dump + once-per-second stats.
// ---------------------------------------------------------------------------

#if SMX_OUTPUT_BINARY
// Binary packet: A5 5A | type | u16 len | u32 dt_us | data[len] | u8 xor
// (little-endian; xor covers type..data). Types: 'F' frame, 'f' truncated
// frame, 'S' stats (see print_stats).
static void write_packet(char type, uint32_t dt_us, const uint8_t *data,
                         uint16_t len) {
  uint8_t hdr[9] = {0xA5, 0x5A, (uint8_t)type,
                    (uint8_t)(len & 0xFF), (uint8_t)(len >> 8),
                    (uint8_t)(dt_us & 0xFF), (uint8_t)(dt_us >> 8),
                    (uint8_t)(dt_us >> 16), (uint8_t)(dt_us >> 24)};
  uint8_t x = 0;
  for (int i = 2; i < 9; i++) x ^= hdr[i];
  for (uint16_t i = 0; i < len; i++) x ^= data[i];
  Serial.write(hdr, sizeof(hdr));
  Serial.write(data, len);
  Serial.write(&x, 1);
}
#endif

static void print_frame(const Frame *f, uint32_t prev_t_us) {
#if SMX_OUTPUT_BINARY
  write_packet(f->overflow ? 'f' : 'F', prev_t_us ? f->t_us - prev_t_us : 0,
               f->data, f->len);
  return;
#endif
  float dt_ms = prev_t_us ? (f->t_us - prev_t_us) / 1000.0f : 0.0f;
  Serial.printf("#%lu t=+%.1fms len=%u%s\n", (unsigned long)f->seq, dt_ms,
                f->len, f->overflow ? " (TRUNCATED)" : "");
  char line[16 * 3 + 16];
  for (uint16_t off = 0; off < f->len; off += 16) {
    int p = snprintf(line, sizeof(line), "  %04x:", off);
    for (uint16_t i = off; i < f->len && i < off + 16; i++) {
      p += snprintf(line + p, sizeof(line) - p, " %02x", f->data[i]);
    }
    Serial.println(line);
  }
}

// Length range of frames printed during the current stats window.
static uint16_t win_min_len = 0xFFFF, win_max_len = 0;

static void print_stats() {
  static Stats last;
  Stats s;
  memcpy(&s, (const void *)&stats, sizeof(s));
  if (win_max_len == 0 && win_min_len == 0xFFFF) win_min_len = 0;
#if SMX_OUTPUT_BINARY
  // 'S' payload: u32 fps, u16 min_len, u16 max_len, u32 dropped, trunc,
  // fifo_ovf, frame_err, parity_err (all per-second deltas) = 28 bytes.
  uint32_t v[7] = {s.frames - last.frames, 0, s.dropped - last.dropped,
                   s.overflows - last.overflows, s.fifo_ovf - last.fifo_ovf,
                   s.frame_err - last.frame_err, s.parity_err - last.parity_err};
  v[1] = (uint32_t)win_min_len | ((uint32_t)win_max_len << 16);
  write_packet('S', 0, (const uint8_t *)v, sizeof(v));  // ESP32 is LE
#else
  Serial.printf(
      "[stats] fps=%lu len=%u..%u dropped=%lu trunc=%lu fifo_ovf=%lu "
      "frame_err=%lu parity_err=%lu\n",
      (unsigned long)(s.frames - last.frames), win_min_len, win_max_len,
      (unsigned long)(s.dropped - last.dropped),
      (unsigned long)(s.overflows - last.overflows),
      (unsigned long)(s.fifo_ovf - last.fifo_ovf),
      (unsigned long)(s.frame_err - last.frame_err),
      (unsigned long)(s.parity_err - last.parity_err));
#endif
  last = s;
  win_min_len = 0xFFFF;
  win_max_len = 0;
}

void setup() {
  Serial.begin(115200);
  delay(1500);  // give USB CDC a moment to enumerate
#if !SMX_OUTPUT_BINARY
  Serial.printf("\nSMX UART logger: UART%d RX=GPIO%d %d baud 8N1 invert=%d\n",
                (int)SMX_UART, SMX_RX_PIN, SMX_BAUD, SMX_RX_INVERT);
#endif

  free_q = xQueueCreate(FRAME_QUEUE_LEN + 1, sizeof(Frame *));
  full_q = xQueueCreate(FRAME_QUEUE_LEN + 1, sizeof(Frame *));
  for (auto &f : frame_pool) {
    Frame *p = &f;
    xQueueSend(free_q, &p, 0);
  }

  uart_config_t cfg = {};
  cfg.baud_rate = SMX_BAUD;
  cfg.data_bits = UART_DATA_8_BITS;
  cfg.parity = UART_PARITY_DISABLE;
  cfg.stop_bits = UART_STOP_BITS_1;
  cfg.flow_ctrl = UART_HW_FLOWCTRL_DISABLE;
  cfg.source_clk = UART_SCLK_DEFAULT;

  ESP_ERROR_CHECK(uart_driver_install(SMX_UART, RX_BUF_SIZE, 0, 32,
                                      &uart_queue, 0));
  ESP_ERROR_CHECK(uart_param_config(SMX_UART, &cfg));
  ESP_ERROR_CHECK(uart_set_pin(SMX_UART, UART_PIN_NO_CHANGE, SMX_RX_PIN,
                               UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE));
#if SMX_RX_INVERT
  ESP_ERROR_CHECK(uart_set_line_inverse(SMX_UART, UART_SIGNAL_RXD_INV));
#endif
  // Short RX timeout (in symbol times) so UART_DATA events arrive promptly.
  uart_set_rx_timeout(SMX_UART, 2);
  uart_set_rx_full_threshold(SMX_UART, RX_FULL_THRESH);

  xTaskCreatePinnedToCore(reader_task, "smx_rx", 4096, nullptr,
                          configMAX_PRIORITIES - 2, nullptr, 0);
}

void loop() {
  static uint32_t prev_t_us = 0;
  static uint32_t last_stats_ms = 0;

  Frame *f;
  if (xQueueReceive(full_q, &f, pdMS_TO_TICKS(20)) == pdTRUE) {
    print_frame(f, prev_t_us);
    prev_t_us = f->t_us;
    if (f->len < win_min_len) win_min_len = f->len;
    if (f->len > win_max_len) win_max_len = f->len;
    xQueueSend(free_q, &f, 0);
  }

  uint32_t now = millis();
  if (now - last_stats_ms >= 1000) {
    last_stats_ms = now;
    print_stats();
  }
}
