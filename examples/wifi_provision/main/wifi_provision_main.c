// BLE-provisioned WiFi: the board boots with NO WiFi config and advertises over
// BLE. An agent connects and *writes* the network's credentials over BLE (the
// hands esp32-loop already has), then *subscribes* to a status characteristic to
// watch it join (the eyes) — no SSID baked at flash, no SoftAP, no menuconfig.
// Creds persist to NVS, so after the first provision it reconnects on reboot
// without BLE. Builds for any IDF target with WiFi + a BLE radio (esp32, esp32c3).
//
//   wifi   char (write):          "<ssid>\n<password>" -> connect + persist
//   status char (read + notify):  live JSON {"state","ip","rssi","ssid"}
//
// SAFETY: creds cross an UNENCRYPTED BLE link (plaintext on air) — fine for a
// bench/lab network, not a hostile-RF deployment. ESP-IDF's unified provisioning
// component (network_provisioning, formerly wifi_provisioning before IDF v6.0)
// adds session encryption (SRP6a / proof-of-possession) if you need it.
#include <stdio.h>
#include <string.h>

#include "esp_event.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_system.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs.h"
#include "nvs_flash.h"

#include "host/ble_hs.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"

#define NVS_NS "wifiprov"        // our own creds namespace (distinct from WiFi's)
#define MAX_RETRIES 5

static uint8_t s_addr_type;
static char s_name[20];
static uint16_t s_status_handle;            // status char value handle (for notify)
static char s_state[12] = "idle";           // idle | connecting | connected | failed
static char s_ip[16] = "";
static char s_ssid[33] = "";
static int s_retries = 0;

// Custom 128-bit UUIDs (LSB-first): service ...0010, wifi-creds ...0011, status ...0012.
static const ble_uuid128_t svc_uuid = BLE_UUID128_INIT(
    0xf0, 0xde, 0xbc, 0x9a, 0x78, 0x56, 0x34, 0x12, 0x00, 0x10, 0x32, 0x70, 0x6f, 0x6f, 0x6c, 0xe5);
static const ble_uuid128_t wifi_uuid = BLE_UUID128_INIT(
    0xf0, 0xde, 0xbc, 0x9a, 0x78, 0x56, 0x34, 0x12, 0x00, 0x11, 0x32, 0x70, 0x6f, 0x6f, 0x6c, 0xe5);
static const ble_uuid128_t status_uuid = BLE_UUID128_INIT(
    0xf0, 0xde, 0xbc, 0x9a, 0x78, 0x56, 0x34, 0x12, 0x00, 0x12, 0x32, 0x70, 0x6f, 0x6f, 0x6c, 0xe5);

static void status_str(char *buf, size_t n) {
    int rssi = 0;
    wifi_ap_record_t ap;
    if (strcmp(s_state, "connected") == 0 && esp_wifi_sta_get_ap_info(&ap) == ESP_OK)
        rssi = ap.rssi;
    snprintf(buf, n, "{\"state\":\"%s\",\"ip\":\"%s\",\"rssi\":%d,\"ssid\":\"%s\"}",
             s_state, s_ip, rssi, s_ssid);
}

// Persist the provisioned creds so the board rejoins on reboot without BLE.
static void creds_save(const char *ssid, const char *pass) {
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) return;
    nvs_set_str(h, "ssid", ssid);
    nvs_set_str(h, "pass", pass);
    nvs_commit(h);
    nvs_close(h);
}

static bool creds_load(char *ssid, size_t sn, char *pass, size_t pn) {
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) != ESP_OK) return false;
    bool ok = nvs_get_str(h, "ssid", ssid, &sn) == ESP_OK &&
              nvs_get_str(h, "pass", pass, &pn) == ESP_OK;
    nvs_close(h);
    return ok;
}

static void wifi_connect(const char *ssid, const char *pass) {
    wifi_config_t cfg = {0};
    snprintf((char *)cfg.sta.ssid, sizeof(cfg.sta.ssid), "%s", ssid);
    snprintf((char *)cfg.sta.password, sizeof(cfg.sta.password), "%s", pass);
    esp_wifi_set_config(WIFI_IF_STA, &cfg);
    snprintf(s_ssid, sizeof(s_ssid), "%s", ssid);
    snprintf(s_state, sizeof(s_state), "connecting");
    s_ip[0] = '\0';
    s_retries = 0;
    esp_wifi_disconnect();
    esp_wifi_connect();
    printf("wifi_provision: connecting to \"%s\"\n", ssid);
}

static void wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data) {
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        if (strcmp(s_state, "connected") == 0 || s_retries++ < MAX_RETRIES) {
            snprintf(s_state, sizeof(s_state), "connecting");
            s_ip[0] = '\0';
            esp_wifi_connect();
        } else {
            snprintf(s_state, sizeof(s_state), "failed");
            printf("wifi_provision: failed to join \"%s\"\n", s_ssid);
        }
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = (ip_event_got_ip_t *)data;
        snprintf(s_ip, sizeof(s_ip), IPSTR, IP2STR(&e->ip_info.ip));
        snprintf(s_state, sizeof(s_state), "connected");
        s_retries = 0;
        printf("wifi_provision: connected, ip=%s\n", s_ip);
    }
}

static void wifi_init(void) {
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event, NULL, NULL);
    esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event, NULL, NULL);
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));  // we own persistence (NVS_NS)
    ESP_ERROR_CHECK(esp_wifi_start());
}

// wifi: "<ssid>\n<password>" -> persist + (re)connect. Long writes carry the full
// payload past the 20-byte default MTU, so a single write is enough.
static int wifi_write(uint16_t c, uint16_t a, struct ble_gatt_access_ctxt *ctxt, void *arg) {
    if (ctxt->op != BLE_GATT_ACCESS_OP_WRITE_CHR) return BLE_ATT_ERR_UNLIKELY;
    char buf[160];
    uint16_t len = 0;
    ble_hs_mbuf_to_flat(ctxt->om, buf, sizeof(buf) - 1, &len);
    buf[len] = '\0';
    char *nl = strchr(buf, '\n');
    if (!nl) return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;  // need "ssid\npass"
    *nl = '\0';
    creds_save(buf, nl + 1);
    wifi_connect(buf, nl + 1);
    return 0;
}

// status: live join state, readable on demand and notified once per second.
static int status_read(uint16_t c, uint16_t a, struct ble_gatt_access_ctxt *ctxt, void *arg) {
    if (ctxt->op != BLE_GATT_ACCESS_OP_READ_CHR) return BLE_ATT_ERR_UNLIKELY;
    char buf[96];
    status_str(buf, sizeof(buf));
    return os_mbuf_append(ctxt->om, buf, strlen(buf)) == 0 ? 0 : BLE_ATT_ERR_INSUFFICIENT_RES;
}

static const struct ble_gatt_svc_def gatt_svcs[] = {
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = &svc_uuid.u,
        .characteristics = (struct ble_gatt_chr_def[]){
            {.uuid = &wifi_uuid.u, .access_cb = wifi_write, .flags = BLE_GATT_CHR_F_WRITE},
            {.uuid = &status_uuid.u, .access_cb = status_read,
             .flags = BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_NOTIFY, .val_handle = &s_status_handle},
            {0},
        },
    },
    {0},
};

static void status_task(void *arg) {
    for (;;) {
        ble_gatts_chr_updated(s_status_handle);   // notify subscribers with the current value
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

static void start_advertising(void);

static int gap_event(struct ble_gap_event *event, void *arg) {
    switch (event->type) {
        case BLE_GAP_EVENT_CONNECT:
            printf("wifi_provision: connect status=%d\n", event->connect.status);
            if (event->connect.status != 0) start_advertising();
            break;
        case BLE_GAP_EVENT_DISCONNECT:
            printf("wifi_provision: disconnect — re-advertising\n");
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
    if (rc != 0) { printf("wifi_provision: adv_start rc=%d\n", rc); return; }
    printf("esp32-loop wifi_provision: connectable, advertising as %s\n", s_name);
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
    printf("esp32-loop wifi_provision: boot as %s\n", s_name);

    wifi_init();

    // Already provisioned? Rejoin from NVS — no BLE needed after the first time.
    char ssid[33] = "", pass[65] = "";
    if (creds_load(ssid, sizeof(ssid), pass, sizeof(pass)) && ssid[0]) {
        printf("wifi_provision: stored creds for \"%s\" — auto-connecting\n", ssid);
        wifi_connect(ssid, pass);
    }

    ESP_ERROR_CHECK(nimble_port_init());
    ble_hs_cfg.sync_cb = on_sync;
    ble_svc_gap_init();
    ble_svc_gatt_init();
    ble_gatts_count_cfg(gatt_svcs);
    ble_gatts_add_svcs(gatt_svcs);
    ble_svc_gap_device_name_set(s_name);
    nimble_port_freertos_init(host_task);
    xTaskCreate(status_task, "status", 3072, NULL, 4, NULL);
}
