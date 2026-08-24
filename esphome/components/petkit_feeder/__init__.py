import esphome.codegen as cg
import esphome.config_validation as cv
from esphome import pins
from esphome.components import binary_sensor, sensor, uart
from esphome.const import (
    CONF_ID,
    DEVICE_CLASS_PROBLEM,
    ENTITY_CATEGORY_DIAGNOSTIC,
    STATE_CLASS_MEASUREMENT,
    UNIT_MILLIVOLT,
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
CONF_DOOR_FAULT = "door_fault"
CONF_ADAPTER_ADC = "adapter_adc"
CONF_ADAPTER_MV = "adapter_millivolts"
CONF_BATTERY_ADC = "battery_adc"
CONF_BATTERY_MV = "battery_millivolts"


def _adc_sensor():
    return sensor.sensor_schema(
        accuracy_decimals=0,
        state_class=STATE_CLASS_MEASUREMENT,
        entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
    )


def _mv_sensor():
    return sensor.sensor_schema(
        unit_of_measurement=UNIT_MILLIVOLT,
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
            cv.Optional(CONF_DOOR_FAULT): binary_sensor.binary_sensor_schema(
                device_class=DEVICE_CLASS_PROBLEM,
                entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
            ),
            cv.Optional(CONF_ADAPTER_ADC): _adc_sensor(),
            cv.Optional(CONF_ADAPTER_MV): _mv_sensor(),
            cv.Optional(CONF_BATTERY_ADC): _adc_sensor(),
            cv.Optional(CONF_BATTERY_MV): _mv_sensor(),
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
    if CONF_DOOR_FAULT in config:
        cg.add(var.set_door_fault(await binary_sensor.new_binary_sensor(config[CONF_DOOR_FAULT])))
    if CONF_ADAPTER_ADC in config:
        cg.add(var.set_adapter_adc(await sensor.new_sensor(config[CONF_ADAPTER_ADC])))
    if CONF_ADAPTER_MV in config:
        cg.add(var.set_adapter_mv(await sensor.new_sensor(config[CONF_ADAPTER_MV])))
    if CONF_BATTERY_ADC in config:
        cg.add(var.set_battery_adc(await sensor.new_sensor(config[CONF_BATTERY_ADC])))
    if CONF_BATTERY_MV in config:
        cg.add(var.set_battery_mv(await sensor.new_sensor(config[CONF_BATTERY_MV])))
