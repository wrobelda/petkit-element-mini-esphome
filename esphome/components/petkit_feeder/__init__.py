import esphome.codegen as cg
import esphome.config_validation as cv
from esphome import pins
from esphome.components import binary_sensor, sensor, uart
from esphome.const import (
    CONF_ID,
    ENTITY_CATEGORY_DIAGNOSTIC,
    STATE_CLASS_MEASUREMENT,
)

CODEOWNERS = ["@earlynerd"]
DEPENDENCIES = ["uart"]
AUTO_LOAD = ["sensor", "binary_sensor"]
MULTI_CONF = False

petkit_feeder_ns = cg.esphome_ns.namespace("petkit_feeder")
PetkitFeeder = petkit_feeder_ns.class_(
    "PetkitFeeder", cg.PollingComponent, uart.UARTDevice
)

CONF_RESET_PIN = "reset_pin"
CONF_SEND_INIT_SEQUENCE = "send_init_sequence"
CONF_FOOD_OK = "food_ok"
CONF_DOOR_FLAG = "door_flag"
CONF_ADAPTER_A = "adapter_raw_1"
CONF_ADAPTER_B = "adapter_raw_2"
CONF_BATTERY_A = "battery_raw_1"
CONF_BATTERY_B = "battery_raw_2"


def _raw_sensor():
    # Raw 16-bit status field. Units/scaling are NOT confirmed, so no unit or
    # device_class is declared — calibrate against a meter before trusting it.
    return sensor.sensor_schema(
        accuracy_decimals=0,
        state_class=STATE_CLASS_MEASUREMENT,
        entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
    )


CONFIG_SCHEMA = (
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(PetkitFeeder),
            cv.Optional(CONF_RESET_PIN): pins.gpio_output_pin_schema,
            cv.Optional(CONF_SEND_INIT_SEQUENCE, default=True): cv.boolean,
            cv.Optional(CONF_FOOD_OK): binary_sensor.binary_sensor_schema(),
            cv.Optional(CONF_DOOR_FLAG): binary_sensor.binary_sensor_schema(
                entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
            ),
            cv.Optional(CONF_ADAPTER_A): _raw_sensor(),
            cv.Optional(CONF_ADAPTER_B): _raw_sensor(),
            cv.Optional(CONF_BATTERY_A): _raw_sensor(),
            cv.Optional(CONF_BATTERY_B): _raw_sensor(),
        }
    )
    .extend(cv.polling_component_schema("10s"))
    .extend(uart.UART_DEVICE_SCHEMA)
)

# Bus is fixed at 115200 8N1 by the ISD91230 firmware.
FINAL_VALIDATE_SCHEMA = uart.final_validate_device_schema(
    "petkit_feeder", baud_rate=115200, require_tx=True, require_rx=True
)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    await uart.register_uart_device(var, config)

    cg.add(var.set_send_init(config[CONF_SEND_INIT_SEQUENCE]))
    if CONF_RESET_PIN in config:
        cg.add(var.set_reset_pin(await cg.gpio_pin_expression(config[CONF_RESET_PIN])))

    if CONF_FOOD_OK in config:
        cg.add(var.set_food_ok(await binary_sensor.new_binary_sensor(config[CONF_FOOD_OK])))
    if CONF_DOOR_FLAG in config:
        cg.add(var.set_door_flag(await binary_sensor.new_binary_sensor(config[CONF_DOOR_FLAG])))
    if CONF_ADAPTER_A in config:
        cg.add(var.set_adapter_a(await sensor.new_sensor(config[CONF_ADAPTER_A])))
    if CONF_ADAPTER_B in config:
        cg.add(var.set_adapter_b(await sensor.new_sensor(config[CONF_ADAPTER_B])))
    if CONF_BATTERY_A in config:
        cg.add(var.set_battery_a(await sensor.new_sensor(config[CONF_BATTERY_A])))
    if CONF_BATTERY_B in config:
        cg.add(var.set_battery_b(await sensor.new_sensor(config[CONF_BATTERY_B])))
