""" Support for Sony receivers with Control A1 input.

"""

import logging
import string
import time

import voluptuous as vol

from homeassistant.components.media_player import (
    PLATFORM_SCHEMA, SUPPORT_SELECT_SOURCE,
    SUPPORT_TURN_OFF, SUPPORT_TURN_ON,
    SUPPORT_VOLUME_STEP, SUPPORT_VOLUME_SET,
    SUPPORT_VOLUME_MUTE, MediaPlayerDevice)
from homeassistant.const import (
    CONF_NAME, STATE_OFF, STATE_ON, STATE_UNKNOWN)
import homeassistant.helpers.config_validation as cv

REQUIREMENTS = ['pyserial==3.4']

_LOGGER = logging.getLogger(__name__)

DOMAIN = "sony_control_a1"

DEFAULT_NAME = 'Sony receiver'

SUPPORT_SONY_CONTROL_A1 = SUPPORT_VOLUME_MUTE | SUPPORT_VOLUME_STEP | SUPPORT_VOLUME_SET | \
    SUPPORT_TURN_ON | SUPPORT_TURN_OFF | SUPPORT_SELECT_SOURCE

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
})

COMMAND_STATUS_SOURCE = 'c00f'
COMMAND_DEVICE_NAME = 'c06a'
COMMAND_SOURCE_NAME = 'c048'
COMMAND_MUTE = 'c006'
COMMAND_UNMUTE = 'c007'
COMMAND_VOLUME_UP = 'c014'
COMMAND_VOLUME_DOWN = 'c015'
COMMAND_POWER_ON = 'c02e'
COMMAND_POWER_OFF = 'c02f'
COMMAND_SELECT_SOURCE = 'c050'

VOLUME_STEPS = 20


def setup_platform(hass, config, add_devices, discovery_info=None):
    """Set up the Sony platform."""
    sony = SonyDevice(config.get(CONF_NAME))

    if sony.update():
        add_devices([sony])


class SonyDevice(MediaPlayerDevice):
    """Representation of a Sony device."""

    def __init__(self, name):
        """Initialize the Sony device."""
        self._name = name

        self._power_state = STATE_UNKNOWN
        self._available_inputs = {}
        self._active_input = None
        self._muted = False

        self._arduino = None
        self._response_buffer = bytes()

    def _check_arduino_connection(self):
        if self._arduino is None:
            try:
                import serial
                self._arduino = serial.Serial('/dev/serial/by-id/usb-1a86_USB2.0-Serial-if00-port0', 115200,
                                              timeout=0.5)
                _LOGGER.info("Connected to arduino")
            except OSError:
                _LOGGER.info("Failed to connect to arduino")
                return False

            # The arduino resets when a serial connection is
            # established -> wait for it to initialize
            time.sleep(4)

        return True

    def _send_sony_command(self, command, expect_response=True):
        if not self._check_arduino_connection():
            return  # Print error..
        _LOGGER.info("Sending command '%s'" % command)
        self._arduino.write((command + '\n').encode())
        if expect_response:
            self._read_sony_response()

    def _parse_sony_string(self, data):
        return bytes(data).decode('iso-8859-1').rstrip('\0').strip()

    def _read_sony_response(self):
        self._response_buffer += self._arduino.read(1000)
        while True:
            line_break_pos = self._response_buffer.find(b'\n')
            if line_break_pos == -1:
                break
            response = self._response_buffer[:line_break_pos]
            self._response_buffer = self._response_buffer[line_break_pos + 1:]

            _LOGGER.info("Handling '%s'", response)

            if not all(c in string.hexdigits for c in response.decode()):
                continue

            response_bytes = [int(response[x:x+2], 16) for x in range(
                0, len(response), 2)]
            #response_bytes = [int(x, 16) for x in response.split()]
            #response_bytes = bytes.fromhex(response)

#            if response_bytes.startswith(bytes([0xc8, 0x70])):
            if response_bytes[:2] == [0xc8, 0x70]:
                # Source status
                # c8 70 16 16 31 ff
                if len(response_bytes) == 6:
                    self._power_state = (
                        STATE_ON if response_bytes[4] & 0x1 else STATE_OFF)
                    self._muted = response_bytes[4] & 0x02 != 0
                    active_input_index = response_bytes[2]
                    for (name, index) in self._available_inputs.items():
                        if index == active_input_index:
                            self._active_input = name
            elif response_bytes[:2] == [0xc8, 0x6a]:
                # c8 6a 53 54 52 2d 44 45 36 33 35 20 00 00 00 00
                #        S  T  R  -  D  E  6  3  5
                if len(response_bytes) == 16:
                    self._device_model = self._parse_sony_string(
                        response_bytes[2:])
                    self._name = self._device_model
            elif response_bytes[:2] == [0xc8, 0x48]:
                # c8 48 00 20 54 55 4e 45 52 20 20 00 00 00 00 00
                #              T  U  N  E  R
                if len(response_bytes) == 16:
                    input_index = response_bytes[2]
                    input_name = self._parse_sony_string(response_bytes[3:])
                    self._available_inputs[input_name] = input_index
            else:
                _LOGGER.info('Unhandled command "%s"', response)

    def update(self):
        """Get the latest details from the device."""

        if not self._available_inputs:
            self._send_sony_command(COMMAND_DEVICE_NAME)
            for id in range(20):  # yes, decimal values
                self._send_sony_command(COMMAND_SOURCE_NAME + "%.2d" % id)

        self._send_sony_command(COMMAND_STATUS_SOURCE)
        return True

    @property
    def name(self):
        """Return the name of the device."""
        return self._name

    @property
    def state(self):
        """Return the state of the device."""
        return self._power_state

    @property
    def is_volume_muted(self):
        """Return boolean if volume is currently muted."""
        return self._muted

    @property
    def source_list(self):
        """Return the list of available input sources."""
        return sorted(list(self._available_inputs.keys()))

    @property
    def media_title(self):
        """Return the current media info."""
        return self._active_input or ""

    @property
    def supported_features(self):
        """Flag media player features that are supported."""
        return SUPPORT_SONY_CONTROL_A1

    @property
    def source(self):
        """Return the current input source."""
        return self._active_input or ""

    def turn_on(self):
        """Turn the media player on."""
        self._send_sony_command(COMMAND_POWER_ON)

    def turn_off(self):
        """Turn off media player."""
        self._send_sony_command(COMMAND_POWER_OFF)

    def volume_up(self):
        """Volume up media player."""
        for _ in range(VOLUME_STEPS):
            self._send_sony_command(COMMAND_VOLUME_UP, expect_response=False)

    def volume_down(self):
        """Volume down media player."""
        for _ in range(VOLUME_STEPS):
            self._send_sony_command(COMMAND_VOLUME_DOWN, expect_response=False)

    def mute_volume(self, mute):
        """Mute or unmute media player."""
        if mute:
            self._send_sony_command(COMMAND_MUTE)
        else:
            self._send_sony_command(COMMAND_UNMUTE)

    def select_source(self, source):
        """Select input source."""
        for (name, index) in self._available_inputs.items():
            if name == source:
                self._send_sony_command(COMMAND_SELECT_SOURCE + "%.2x" % index)
