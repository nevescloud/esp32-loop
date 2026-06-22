idf_component_register(SRCS "wifi_provision_main.c" INCLUDE_DIRS ""
                       REQUIRES bt nvs_flash esp_wifi esp_netif esp_event)

# `esp32loop flash` passes -DESP32LOOP_NAME=<board.toml name> — bake it in.
if(DEFINED ESP32LOOP_NAME)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE ESP32LOOP_NAME="${ESP32LOOP_NAME}")
endif()
