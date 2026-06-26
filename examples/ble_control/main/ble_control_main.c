// Connectable device: control + telemetry, drivable two ways over BLE. The
// differentiated shape — an agent connects, *reads/subscribes* to a running
// board's live state AND *commands* it (host-side eyes and hands), vs a beacon
// that only shouts a name. Builds for any IDF target with a BLE radio.
//
// Two GATT services in one box (see the NUS note below for why both):
//   custom service — structured chars for programmatic drive (the esp32loop CLI):
//     command   char (write):        2 bytes [gpio, level] -> set a pin
//     telemetry char (read + notify): live JSON {"up":s,"heap":b,"pin":n,"lvl":v}
//   Nordic UART Service — a typed line shell for any generic terminal:
//     help · gpio <pin> [0|1] · blink <pin> [n] · stat · name
//
// SAFETY: the command is a persistent set — fine for LEDs/relays, NOT motors
// (a dropped link latches the pin); motors want pulse + watchdog instead.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "driver/gpio.h"
#include "esp_mac.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"

#include "host/ble_hs.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"

static uint8_t s_addr_type;
static char s_name[20];
static uint16_t s_telem_handle;          // telemetry char value handle (for notify)
static uint16_t s_nus_tx_handle;         // NUS TX char value handle (device->host notify)
static uint16_t s_conn_handle = BLE_HS_CONN_HANDLE_NONE;  // active link, for notify
static int s_last_pin = -1, s_last_level = 0;

// Custom 128-bit UUIDs (LSB-first): service ...0001, command ...0002, telemetry ...0003.
static const ble_uuid128_t svc_uuid = BLE_UUID128_INIT(
    0xf0, 0xde, 0xbc, 0x9a, 0x78, 0x56, 0x34, 0x12, 0x00, 0x01, 0x32, 0x70, 0x6f, 0x6f, 0x6c, 0xe5);
static const ble_uuid128_t cmd_uuid = BLE_UUID128_INIT(
    0xf0, 0xde, 0xbc, 0x9a, 0x78, 0x56, 0x34, 0x12, 0x00, 0x02, 0x32, 0x70, 0x6f, 0x6f, 0x6c, 0xe5);
static const ble_uuid128_t telem_uuid = BLE_UUID128_INIT(
    0xf0, 0xde, 0xbc, 0x9a, 0x78, 0x56, 0x34, 0x12, 0x00, 0x03, 0x32, 0x70, 0x6f, 0x6f, 0x6c, 0xe5);

// Nordic UART Service (6e400001/2/3, LSB-first) — the de-facto serial-over-BLE
// profile a generic terminal (e.g. tab-device) filters for. RX = host->device
// (write), TX = device->host (notify). Added alongside the custom service so the
// CLI's cmd/telem verbs and a plain terminal both work against one firmware.
static const ble_uuid128_t nus_svc_uuid = BLE_UUID128_INIT(
    0x9e, 0xca, 0xdc, 0x24, 0x0e, 0xe5, 0xa9, 0xe0, 0x93, 0xf3, 0xa3, 0xb5, 0x01, 0x00, 0x40, 0x6e);
static const ble_uuid128_t nus_rx_uuid = BLE_UUID128_INIT(
    0x9e, 0xca, 0xdc, 0x24, 0x0e, 0xe5, 0xa9, 0xe0, 0x93, 0xf3, 0xa3, 0xb5, 0x02, 0x00, 0x40, 0x6e);
static const ble_uuid128_t nus_tx_uuid = BLE_UUID128_INIT(
    0x9e, 0xca, 0xdc, 0x24, 0x0e, 0xe5, 0xa9, 0xe0, 0x93, 0xf3, 0xa3, 0xb5, 0x03, 0x00, 0x40, 0x6e);

static void telem_str(char *buf, size_t n) {
    snprintf(buf, n, "{\"up\":%lld,\"heap\":%lu,\"pin\":%d,\"lvl\":%d}",
             esp_timer_get_time() / 1000000, (unsigned long)esp_get_free_heap_size(),
             s_last_pin, s_last_level);
}

// The one place a pin is driven — shared by the binary cmd characteristic and
// the typed `gpio` command, so both stay in sync with s_last_pin/s_last_level.
static void set_pin(int pin, int level) {
    s_last_pin = pin;
    s_last_level = level ? 1 : 0;
    gpio_reset_pin(s_last_pin);
    gpio_set_direction(s_last_pin, GPIO_MODE_OUTPUT);
    gpio_set_level(s_last_pin, s_last_level);
    printf("ble_control: gpio %d <- %d\n", s_last_pin, s_last_level);
}

// command: 2 bytes [gpio, level] -> set the pin (full output control).
static int cmd_write(uint16_t c, uint16_t a, struct ble_gatt_access_ctxt *ctxt, void *arg) {
    if (ctxt->op != BLE_GATT_ACCESS_OP_WRITE_CHR) return BLE_ATT_ERR_UNLIKELY;
    uint8_t b[2];
    uint16_t len = 0;
    ble_hs_mbuf_to_flat(ctxt->om, b, sizeof(b), &len);
    if (len < 2) return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
    set_pin(b[0], b[1]);
    return 0;
}

// telemetry: live state, readable on demand and notified once per second.
static int telem_read(uint16_t c, uint16_t a, struct ble_gatt_access_ctxt *ctxt, void *arg) {
    if (ctxt->op != BLE_GATT_ACCESS_OP_READ_CHR) return BLE_ATT_ERR_UNLIKELY;
    char buf[80];
    telem_str(buf, sizeof(buf));
    return os_mbuf_append(ctxt->om, buf, strlen(buf)) == 0 ? 0 : BLE_ATT_ERR_INSUFFICIENT_RES;
}

// blink: toggle a pin N times on its own task so the BLE host task never
// blocks. One blink at a time — a second request is rejected while busy.
static volatile bool s_blink_busy = false;
struct blink_req { int pin, count; };

static void blink_task(void *arg) {
    struct blink_req r = *(struct blink_req *)arg;
    free(arg);
    gpio_reset_pin(r.pin);
    gpio_set_direction(r.pin, GPIO_MODE_OUTPUT);
    for (int i = 0; i < r.count; i++) {
        gpio_set_level(r.pin, 1);
        vTaskDelay(pdMS_TO_TICKS(200));
        gpio_set_level(r.pin, 0);
        vTaskDelay(pdMS_TO_TICKS(200));
    }
    s_last_pin = r.pin;
    s_last_level = 0;
    s_blink_busy = false;
    vTaskDelete(NULL);
}

// Push a string to the host over NUS TX (no-op when nothing is connected).
static void tx_send(const char *s) {
    if (s_conn_handle == BLE_HS_CONN_HANDLE_NONE) return;
    struct os_mbuf *om = ble_hs_mbuf_from_flat(s, strlen(s));
    if (om) ble_gatts_notify_custom(s_conn_handle, s_nus_tx_handle, om);
}

// A completed line typed at the terminal. Tiny command set; reuses set_pin and
// telem_str so the terminal and the binary characteristics share one behavior.
static void process_line(char *line) {
    char *cmd = strtok(line, " ");
    if (!cmd) { tx_send("> "); return; }

    if (!strcmp(cmd, "help")) {
        tx_send("commands:\r\n"
                "  gpio <pin> <0|1>  drive a pin\r\n"
                "  gpio <pin>        read a pin's level\r\n"
                "  blink <pin> [n]   blink a pin n times (default 3)\r\n"
                "  stat              telemetry json\r\n"
                "  name              device name\r\n"
                "  help              this list\r\n");
    } else if (!strcmp(cmd, "gpio")) {
        char *p = strtok(NULL, " "), *l = strtok(NULL, " ");
        char msg[48];
        if (!p) { tx_send("usage: gpio <pin> [0|1]\r\n"); }
        else if (!l) {
            int pin = atoi(p);
            snprintf(msg, sizeof(msg), "gpio %d = %d\r\n", pin, gpio_get_level(pin));
            tx_send(msg);
        } else {
            set_pin(atoi(p), atoi(l));
            snprintf(msg, sizeof(msg), "ok: gpio %d <- %d\r\n", s_last_pin, s_last_level);
            tx_send(msg);
        }
    } else if (!strcmp(cmd, "blink")) {
        char *p = strtok(NULL, " "), *n = strtok(NULL, " ");
        if (!p) { tx_send("usage: blink <pin> [n]\r\n"); }
        else if (s_blink_busy) { tx_send("busy: a blink is already running\r\n"); }
        else {
            struct blink_req *r = malloc(sizeof(*r));
            if (!r) { tx_send("err: out of memory\r\n"); }
            else {
                r->pin = atoi(p);
                r->count = n ? atoi(n) : 3;
                if (r->count < 1) r->count = 1;
                s_blink_busy = true;
                char msg[48];
                snprintf(msg, sizeof(msg), "ok: blink %d x%d\r\n", r->pin, r->count);
                tx_send(msg);
                xTaskCreate(blink_task, "blink", 2048, r, 4, NULL);
            }
        }
    } else if (!strcmp(cmd, "stat")) {
        char buf[80];
        telem_str(buf, sizeof(buf));
        tx_send(buf);
        tx_send("\r\n");
    } else if (!strcmp(cmd, "name")) {
        tx_send(s_name);
        tx_send("\r\n");
    } else {
        tx_send("unknown command — try 'help'\r\n");
    }
    tx_send("> ");
}

// NUS RX: host typed bytes. Echo each back over TX (a terminal has no local echo
// over BLE), expanding CR -> CRLF so Enter advances a line; accumulate into a
// line buffer and run the command once a line terminator arrives.
static char s_line[128];
static size_t s_line_len;

static int nus_rx_write(uint16_t c, uint16_t a, struct ble_gatt_access_ctxt *ctxt, void *arg) {
    if (ctxt->op != BLE_GATT_ACCESS_OP_WRITE_CHR) return BLE_ATT_ERR_UNLIKELY;
    uint8_t in[128];
    uint16_t len = 0;
    ble_hs_mbuf_to_flat(ctxt->om, in, sizeof(in), &len);
    if (s_conn_handle == BLE_HS_CONN_HANDLE_NONE) return 0;

    for (uint16_t i = 0; i < len; i++) {
        uint8_t ch = in[i];
        if (ch == '\r' || ch == '\n') {
            tx_send("\r\n");
            s_line[s_line_len] = '\0';
            process_line(s_line);
            s_line_len = 0;
        } else if (ch == 0x7f || ch == 0x08) {   // DEL / backspace
            if (s_line_len) { s_line_len--; tx_send("\b \b"); }
        } else if (s_line_len < sizeof(s_line) - 1) {
            s_line[s_line_len++] = ch;
            char e[2] = {(char)ch, 0};
            tx_send(e);                            // echo the typed char
        }
    }
    return 0;
}

// NUS TX is notify-only; the callback is never invoked for read/write but the
// stack requires a non-NULL access_cb to register the characteristic.
static int nus_tx_access(uint16_t c, uint16_t a, struct ble_gatt_access_ctxt *ctxt, void *arg) {
    return BLE_ATT_ERR_UNLIKELY;
}

static const struct ble_gatt_svc_def gatt_svcs[] = {
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = &svc_uuid.u,
        .characteristics = (struct ble_gatt_chr_def[]){
            {.uuid = &cmd_uuid.u, .access_cb = cmd_write, .flags = BLE_GATT_CHR_F_WRITE},
            {.uuid = &telem_uuid.u, .access_cb = telem_read,
             .flags = BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_NOTIFY, .val_handle = &s_telem_handle},
            {0},
        },
    },
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = &nus_svc_uuid.u,
        .characteristics = (struct ble_gatt_chr_def[]){
            {.uuid = &nus_rx_uuid.u, .access_cb = nus_rx_write,
             .flags = BLE_GATT_CHR_F_WRITE | BLE_GATT_CHR_F_WRITE_NO_RSP},
            {.uuid = &nus_tx_uuid.u, .access_cb = nus_tx_access,
             .flags = BLE_GATT_CHR_F_NOTIFY, .val_handle = &s_nus_tx_handle},
            {0},
        },
    },
    {0},
};

static void telem_task(void *arg) {
    for (;;) {
        ble_gatts_chr_updated(s_telem_handle);   // notify subscribers with the current value
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

static void start_advertising(void);

static int gap_event(struct ble_gap_event *event, void *arg) {
    switch (event->type) {
        case BLE_GAP_EVENT_CONNECT:
            printf("ble_control: connect status=%d\n", event->connect.status);
            if (event->connect.status == 0) s_conn_handle = event->connect.conn_handle;
            else start_advertising();
            break;
        case BLE_GAP_EVENT_DISCONNECT:
            printf("ble_control: disconnect — re-advertising\n");
            s_conn_handle = BLE_HS_CONN_HANDLE_NONE;
            start_advertising();
            break;
        case BLE_GAP_EVENT_ADV_COMPLETE:
            start_advertising();
            break;
        case BLE_GAP_EVENT_SUBSCRIBE:
            // Host just enabled TX notifications — safe to greet now.
            if (event->subscribe.attr_handle == s_nus_tx_handle && event->subscribe.cur_notify) {
                s_line_len = 0;
                tx_send("esp32-loop ble_control — type 'help'\r\n> ");
            }
            break;
    }
    return 0;
}

static void start_advertising(void) {
    // 31-byte advert budget can't hold flags + a 128-bit UUID + the name. Keep
    // the NAME in the primary advert (passively scannable -> robust name-based
    // discovery for the CLI) and put the NUS UUID in the scan response, which an
    // active scanner (Chrome's Web Bluetooth) aggregates when matching filters.
    struct ble_hs_adv_fields fields = {0};
    fields.flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP;
    fields.name = (uint8_t *)s_name;
    fields.name_len = strlen(s_name);
    fields.name_is_complete = 1;
    ble_gap_adv_set_fields(&fields);

    struct ble_hs_adv_fields rsp = {0};
    rsp.uuids128 = (ble_uuid128_t *)&nus_svc_uuid;
    rsp.num_uuids128 = 1;
    rsp.uuids128_is_complete = 1;
    ble_gap_adv_rsp_set_fields(&rsp);

    struct ble_gap_adv_params adv = {0};
    adv.conn_mode = BLE_GAP_CONN_MODE_UND;   // CONNECTABLE
    adv.disc_mode = BLE_GAP_DISC_MODE_GEN;
    int rc = ble_gap_adv_start(s_addr_type, NULL, BLE_HS_FOREVER, &adv, gap_event, NULL);
    if (rc != 0) { printf("ble_control: adv_start rc=%d\n", rc); return; }
    printf("esp32-loop ble_control: connectable, advertising as %s\n", s_name);
}

static void on_sync(void) {
    ble_hs_id_infer_auto(0, &s_addr_type);
    start_advertising();
}

static void host_task(void *arg) {
    nimble_port_run();
    nimble_port_freertos_deinit();
}

void app_main(void) {
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }
    uint8_t mac[6] = {0};
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
#ifdef ESP32LOOP_NAME
    snprintf(s_name, sizeof(s_name), "%s", ESP32LOOP_NAME);
#else
    snprintf(s_name, sizeof(s_name), "esp32-loop-%02X%02X", mac[4], mac[5]);
#endif
    printf("esp32-loop ble_control: boot as %s\n", s_name);

    ESP_ERROR_CHECK(nimble_port_init());
    ble_hs_cfg.sync_cb = on_sync;
    ble_svc_gap_init();
    ble_svc_gatt_init();
    ble_gatts_count_cfg(gatt_svcs);
    ble_gatts_add_svcs(gatt_svcs);
    ble_svc_gap_device_name_set(s_name);
    nimble_port_freertos_init(host_task);
    xTaskCreate(telem_task, "telem", 3072, NULL, 4, NULL);
}
