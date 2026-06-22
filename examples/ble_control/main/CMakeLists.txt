idf_component_register(SRCS "ble_control_main.c" INCLUDE_DIRS "" REQUIRES bt nvs_flash driver)

# `esp32loop flash` passes -DESP32LOOP_NAME=<board.toml name> — bake it in.
if(DEFINED ESP32LOOP_NAME)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE ESP32LOOP_NAME="${ESP32LOOP_NAME}")
endif()
