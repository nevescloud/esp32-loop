// Connectable device: control + telemetry over one BLE GATT service. The
// differentiated shape — an agent connects, *reads/subscribes* to a running
// board's live state AND *commands* it (host-side eyes and hands), vs a beacon
// that only shouts a name. Builds for any IDF target with a BLE radio.
//
//   command   char (write):        2 bytes [gpio, level] -> set a pin
//   telemetry char (read + notify): live JSON {"up":s,"heap":b,"pin":n,"lvl":v}
//
// SAFETY: the command is a persistent set — fine for LEDs/relays, NOT motors
// (a dropped link latches the pin); motors want pulse + watchdog instead.
#include <stdio.h>
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
static int s_last_pin = -1, s_last_level = 0;

// Custom 128-bit UUIDs (LSB-first): service ...0001, command ...0002, telemetry ...0003.
static const ble_uuid128_t svc_uuid = BLE_UUID128_INIT(
    0xf0, 0xde, 0xbc, 0x9a, 0x78, 0x56, 0x34, 0x12, 0x00, 0x01, 0x32, 0x70, 0x6f, 0x6f, 0x6c, 0xe5);
static const ble_uuid128_t cmd_uuid = BLE_UUID128_INIT(
    0xf0, 0xde, 0xbc, 0x9a, 0x78, 0x56, 0x34, 0x12, 0x00, 0x02, 0x32, 0x70, 0x6f, 0x6f, 0x6c, 0xe5);
static const ble_uuid128_t telem_uuid = BLE_UUID128_INIT(
    0xf0, 0xde, 0xbc, 0x9a, 0x78, 0x56, 0x34, 0x12, 0x00, 0x03, 0x32, 0x70, 0x6f, 0x6f, 0x6c, 0xe5);

static void telem_str(char *buf, size_t n) {
    snprintf(buf, n, "{\"up\":%lld,\"heap\":%lu,\"pin\":%d,\"lvl\":%d}",
             esp_timer_get_time() / 1000000, (unsigned long)esp_get_free_heap_size(),
             s_last_pin, s_last_level);
}

// command: 2 bytes [gpio, level] -> set the pin (full output control).
static int cmd_write(uint16_t c, uint16_t a, struct ble_gatt_access_ctxt *ctxt, void *arg) {
    if (ctxt->op != BLE_GATT_ACCESS_OP_WRITE_CHR) return BLE_ATT_ERR_UNLIKELY;
    uint8_t b[2];
    uint16_t len = 0;
    ble_hs_mbuf_to_flat(ctxt->om, b, sizeof(b), &len);
    if (len < 2) return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
    s_last_pin = b[0];
    s_last_level = b[1] ? 1 : 0;
    gpio_reset_pin(s_last_pin);
    gpio_set_direction(s_last_pin, GPIO_MODE_OUTPUT);
    gpio_set_level(s_last_pin, s_last_level);
    printf("ble_control: gpio %d <- %d\n", s_last_pin, s_last_level);
    return 0;
}

// telemetry: live state, readable on demand and notified once per second.
static int telem_read(uint16_t c, uint16_t a, struct ble_gatt_access_ctxt *ctxt, void *arg) {
    if (ctxt->op != BLE_GATT_ACCESS_OP_READ_CHR) return BLE_ATT_ERR_UNLIKELY;
    char buf[80];
    telem_str(buf, sizeof(buf));
    return os_mbuf_append(ctxt->om, buf, strlen(buf)) == 0 ? 0 : BLE_ATT_ERR_INSUFFICIENT_RES;
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
            if (event->connect.status != 0) start_advertising();
            break;
        case BLE_GAP_EVENT_DISCONNECT:
            printf("ble_control: disconnect — re-advertising\n");
            start_advertising();
            break;
        case BLE_GAP_EVENT_ADV_COMPLETE:
            start_advertising();
            break;
    }
    return 0;
}

static void start_advertising(void) {
    struct ble_hs_adv_fields fields = {0};
    fields.flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP;
    fields.name = (uint8_t *)s_name;
    fields.name_len = strlen(s_name);
    fields.name_is_complete = 1;
    ble_gap_adv_set_fields(&fields);

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
