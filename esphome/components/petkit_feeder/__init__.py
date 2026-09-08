import esphome.codegen as cg
import esphome.config_validation as cv
from esphome import pins
from esphome.components import binary_sensor, sensor, text_sensor, uart
from esphome.const import (
    CONF_ID,
    DEVICE_CLASS_VOLTAGE,
    ENTITY_CATEGORY_DIAGNOSTIC,
    STATE_CLASS_MEASUREMENT,
    UNIT_VOLT,
)

CODEOWNERS = ["@wrobelda"]
DEPENDENCIES = ["uart"]
AUTO_LOAD = ["sensor", "binary_sensor", "text_sensor"]
MULTI_CONF = False

petkit_feeder_ns = cg.esphome_ns.namespace("petkit_feeder")
PetkitFeeder = petkit_feeder_ns.class_(
    "PetkitFeeder", cg.PollingComponent, uart.UARTDevice
)

CONF_RESET_PIN = "reset_pin"
CONF_SEND_INIT_SEQUENCE = "send_init_sequence"
CONF_DISPENSER_DOOR_FEEDBACK = "dispenser_door_feedback"
CONF_FOOD_DETECTED = "food_detected"
CONF_DISPENSER_WHEEL_FEEDBACK = "dispenser_wheel_feedback"
CONF_ADAPTER_ADC = "adapter_adc"
CONF_ADAPTER_VOLTAGE = "adapter_voltage"
CONF_BATTERY_ADC = "battery_adc"
CONF_BATTERY_VOLTAGE = "battery_voltage"
CONF_POWER_SOURCE = "power_source"


def _raw_sensor():
    return sensor.sensor_schema(
        accuracy_decimals=0,
        state_class=STATE_CLASS_MEASUREMENT,
        entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
    )


def _voltage_sensor():
    return sensor.sensor_schema(
        unit_of_measurement=UNIT_VOLT,
        accuracy_decimals=2,
        device_class=DEVICE_CLASS_VOLTAGE,
        state_class=STATE_CLASS_MEASUREMENT,
    )


CONFIG_SCHEMA = (
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(PetkitFeeder),
            cv.Required(CONF_RESET_PIN): pins.gpio_output_pin_schema,
            cv.Optional(CONF_SEND_INIT_SEQUENCE, default=True): cv.boolean,
            cv.Optional(CONF_DISPENSER_DOOR_FEEDBACK): _raw_sensor(),
            cv.Optional(CONF_FOOD_DETECTED): binary_sensor.binary_sensor_schema(),
            cv.Optional(CONF_DISPENSER_WHEEL_FEEDBACK): _raw_sensor(),
            cv.Optional(CONF_ADAPTER_ADC): _raw_sensor(),
            cv.Optional(CONF_ADAPTER_VOLTAGE): _voltage_sensor(),
            cv.Optional(CONF_BATTERY_ADC): _raw_sensor(),
            cv.Optional(CONF_BATTERY_VOLTAGE): _voltage_sensor(),
            cv.Optional(CONF_POWER_SOURCE): text_sensor.text_sensor_schema(
                icon="mdi:power-plug-battery",
                entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
            ),
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
    reset_pin = await cg.gpio_pin_expression(config[CONF_RESET_PIN])
    var = cg.new_Pvariable(config[CONF_ID], reset_pin)
    await cg.register_component(var, config)
    await uart.register_uart_device(var, config)

    cg.add(var.set_send_init(config[CONF_SEND_INIT_SEQUENCE]))
    if CONF_DISPENSER_DOOR_FEEDBACK in config:
        sensor_var = await sensor.new_sensor(config[CONF_DISPENSER_DOOR_FEEDBACK])
        cg.add(var.set_dispenser_door_feedback(sensor_var))
    if CONF_FOOD_DETECTED in config:
        sensor_var = await binary_sensor.new_binary_sensor(config[CONF_FOOD_DETECTED])
        cg.add(var.set_food_detected(sensor_var))
    if CONF_DISPENSER_WHEEL_FEEDBACK in config:
        sensor_var = await sensor.new_sensor(config[CONF_DISPENSER_WHEEL_FEEDBACK])
        cg.add(var.set_dispenser_wheel_feedback(sensor_var))
    if CONF_ADAPTER_ADC in config:
        cg.add(var.set_adapter_adc(await sensor.new_sensor(config[CONF_ADAPTER_ADC])))
    if CONF_ADAPTER_VOLTAGE in config:
        cg.add(var.set_adapter_voltage(await sensor.new_sensor(config[CONF_ADAPTER_VOLTAGE])))
    if CONF_BATTERY_ADC in config:
        cg.add(var.set_battery_adc(await sensor.new_sensor(config[CONF_BATTERY_ADC])))
    if CONF_BATTERY_VOLTAGE in config:
        cg.add(
            var.set_battery_voltage(
                await sensor.new_sensor(config[CONF_BATTERY_VOLTAGE])
            )
        )
    if CONF_POWER_SOURCE in config:
        cg.add(
            var.set_power_source(
                await text_sensor.new_text_sensor(config[CONF_POWER_SOURCE])
            )
        )
