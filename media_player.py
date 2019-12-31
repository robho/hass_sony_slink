""" Support for Sony receivers with S-Link/Control-A1 input.
"""
import collections
import logging
import serial
import string
import threading
import time
import voluptuous

from homeassistant.components.media_player import (
    PLATFORM_SCHEMA, SUPPORT_SELECT_SOURCE,
    SUPPORT_TURN_OFF, SUPPORT_TURN_ON,
    SUPPORT_VOLUME_STEP, SUPPORT_VOLUME_SET,
    SUPPORT_VOLUME_MUTE, MediaPlayerDevice)
from homeassistant.const import (
    CONF_NAME, STATE_OFF, STATE_ON, STATE_UNKNOWN)
import homeassistant.helpers.config_validation as cv

_LOGGER = logging.getLogger(__name__)

DOMAIN = "sony_slink"

CONF_SERIAL_PORT = 'serial_port'
CONF_BAUD_RATE = 'baud_rate'

DEFAULT_NAME = 'Sony device'
DEFAULT_BAUD_RATE = 115200

SUPPORT_SONY_CONTROL_A1 = SUPPORT_VOLUME_MUTE | SUPPORT_VOLUME_STEP | \
    SUPPORT_VOLUME_SET | SUPPORT_TURN_ON | SUPPORT_TURN_OFF | \
    SUPPORT_SELECT_SOURCE

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    voluptuous.Required(CONF_SERIAL_PORT): cv.string,
    voluptuous.Optional(CONF_BAUD_RATE, default=DEFAULT_BAUD_RATE):
        cv.positive_int,
    voluptuous.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
})

COMMAND_PREFIX_AMP = 'c0'
COMMAND_PREFIX_NEW_AMP = '70'

COMMAND_STATUS_SOURCE = '0f'
COMMAND_DEVICE_NAME = '6a'
COMMAND_SOURCE_NAME = '48'
COMMAND_MUTE = '06'
COMMAND_UNMUTE = '07'
COMMAND_VOLUME_UP = '14'
COMMAND_VOLUME_DOWN = '15'
COMMAND_POWER_ON = '2e'
COMMAND_POWER_OFF = '2f'
COMMAND_SELECT_SOURCE = '50'
COMMAND_QUERY_INPUT_MODE = '43'
COMMAND_INPUT_MODE = '83'

INPUT_MODES = {0x0: "auto", 0x1: "optical",
               0x2: "coaxial", 0x4: "analog"}

VOLUME_REPEAT_DELAY_S = 0.1
VOLUME_STEPS = 2

Source = collections.namedtuple("Source", "id input_mode name")


def setup_platform(hass, config, add_devices, discovery_info=None):
    """Set up the Sony platform."""
    sony = SonyDevice(config.get(CONF_NAME),
                      config.get(CONF_SERIAL_PORT),
                      config.get(CONF_BAUD_RATE))

    if sony.update():
        add_devices([sony])


class SonyDevice(MediaPlayerDevice):
    """Representation of a Sony device."""

    def __init__(self, name, serial_port, baud_rate):
        """Initialize the Sony device."""
        self._serial_port = serial_port
        self._baud_rate = baud_rate
        self._name = name

        self._device_name = None
        self._device_command_prefix = COMMAND_PREFIX_AMP
        self._power_state = STATE_UNKNOWN
        self._available_sources = []
        self._active_source_id = None
        self._input_mode = None
        self._muted = False

        self._lock = threading.Lock()
        self._arduino = None
        self._response_buffer = bytes()

    def _check_arduino_connection(self):
        if self._arduino is None:
            try:
                self._arduino = serial.Serial(self._serial_port,
                                              self._baud_rate, timeout=0.1)
                _LOGGER.info("Connected to arduino")
            except OSError:
                _LOGGER.info("Failed to connect to arduino")
                return False

            # The arduino resets when a serial connection is
            # established -> wait for it to initialize
            time.sleep(4)

        return True

    def _should_split_source_by_input_mode(self, source_id):
        if self._device_name == "STR-DE635" and source_id == 0x19:
            return True
        return False

    def _send_sony_command(self, command, expect_response=True):
        if not self._check_arduino_connection():
            return  # Print error..
        _LOGGER.debug("Sending command '%s%s'" % (
            self._device_command_prefix, command))
        self._arduino.write((self._device_command_prefix + command + '\n').encode())
        if expect_response:
            self._read_sony_response()

    def _parse_sony_string(self, data):
        return bytes(data).decode('iso-8859-1').rstrip('\0').strip()

    def _read_sony_response(self):
        while True:
            input = self._arduino.read()
            if len(input) == 0:
                break
            self._response_buffer += input

        while True:
            line_break_pos = self._response_buffer.find(b'\n')
            if line_break_pos == -1:
                break
            response = self._response_buffer[:line_break_pos]
            self._response_buffer = self._response_buffer[line_break_pos + 1:]

            _LOGGER.debug("Handling '%s'", response)

            if not all(c in string.hexdigits for c in response.decode()):
                continue

            response_bytes = [int(response[x:x+2], 16) for x in range(
                0, len(response), 2)]

            if response_bytes[0] in [0xc8, 0x78] and response_bytes[1] == 0x70:
                # Source status
                # c8 70 16 16 31 ff
                if len(response_bytes) == 6:
                    self._power_state = (
                        STATE_ON if response_bytes[4] & 0x1 else STATE_OFF)
                    self._muted = response_bytes[4] & 0x02 != 0
                    self._active_source_id = response_bytes[2]
            elif response_bytes[0] in [0xc8, 0x78] and response_bytes[1] == 0x6a:
                # c8 6a 53 54 52 2d 44 45 36 33 35 20 00 00 00 00
                #        S  T  R  -  D  E  6  3  5
                if len(response_bytes) == 16:
                    self._device_name = self._parse_sony_string(
                        response_bytes[2:])
            elif response_bytes[0] in [0xc8, 0x78] and response_bytes[1] == 0x48:
                # c8 48 00 20 54 55 4e 45 52 20 20 00 00 00 00 00
                #              T  U  N  E  R
                def add_or_update_source(source_id, source_name,
                                         input_mode=None):
                    for source in self._available_sources:
                        if source.id == source_id and source.input_mode == input_mode:
                            source.name = source_name
                            return
                    self._available_sources.append(Source(
                            id=source_id, input_mode=input_mode, name=source_name))

                if len(response_bytes) == 16:
                    source_id = response_bytes[2]
                    source_name = self._parse_sony_string(response_bytes[3:])

                    if self._should_split_source_by_input_mode(source_id):
                        add_or_update_source(
                            source_id, source_name, "auto")
                        for input_mode in ["analog", "coaxial", "optical"]:
                            add_or_update_source(
                                source_id, source_name + " | " + input_mode,
                                input_mode)
                    else:
                        add_or_update_source(source_id, source_name)
            elif response_bytes[0] in [0xc8, 0x78] and response_bytes[1] == 0x43:
                # c8 43 00 03
                if len(response_bytes) == 4:
                    self._input_mode = INPUT_MODES[response_bytes[2]]
            else:
                _LOGGER.info('Unhandled response "%s"', response)

    def _get_active_source(self):
        for source in self._available_sources:
            if source.id == self._active_source_id and (
                    source.input_mode is None or
                    source.input_mode == self._input_mode):
                return source
        return None

    def update(self):
        """Get the latest details from the device."""
        with self._lock:
            if not self._available_sources:
                self._send_sony_command(COMMAND_DEVICE_NAME)
                if self._device_name is None:
                    self._device_command_prefix = COMMAND_PREFIX_NEW_AMP
                    self._send_sony_command(COMMAND_DEVICE_NAME)
                    if self._device_name is None:
                        _LOGGER.error("No device is responding to name queries")
                        return False

                # Device specific configuration to speed up
                # initialization and avoid duplicate sources
                if self._device_name == "STR-DE635":
                    source_ids_to_scan = [0, 1, 2, 4, 10, 11, 16, 19]
                elif self._device_name == "STR-DB840":
                    source_ids_to_scan = [0, 1, 2, 4, 10, 11, 12, 16, 19]
                elif self._device_name == "STR-DB2000":
                    source_ids_to_scan = [0, 1, 2, 4, 5, 10, 11, 12, 16, 19]
                else:
                    source_ids_to_scan = range(20)  # yes, decimal values

                for id in source_ids_to_scan:
                    self._send_sony_command(COMMAND_SOURCE_NAME + "%.2d" % id)

            self._send_sony_command(COMMAND_STATUS_SOURCE)
            if self._should_split_source_by_input_mode(self._active_source_id):
                self._send_sony_command(COMMAND_QUERY_INPUT_MODE)
            return len(self._available_sources) > 0

    @property
    def name(self):
        """Return the name of the device."""
        with self._lock:
            if self._name != DEFAULT_NAME:
                return self._name
            return self._device_name or self._name

    @property
    def state(self):
        """Return the state of the device."""
        with self._lock:
            return self._power_state

    @property
    def is_volume_muted(self):
        """Return boolean if volume is currently muted."""
        with self._lock:
            return self._muted

    @property
    def source_list(self):
        """Return the list of available input sources."""
        with self._lock:
            return sorted([s.name for s in self._available_sources])

    @property
    def media_title(self):
        """Return the current media info."""
        with self._lock:
            source = self._get_active_source()
            return source.name if source else ""

    @property
    def supported_features(self):
        """Flag media player features that are supported."""
        return SUPPORT_SONY_CONTROL_A1

    @property
    def source(self):
        """Return the current input source."""
        with self._lock:
            source = self._get_active_source()
            return source.name if source else ""

    def turn_on(self):
        """Turn the media player on."""
        with self._lock:
            self._send_sony_command(COMMAND_POWER_ON)

    def turn_off(self):
        """Turn off media player."""
        with self._lock:
            self._send_sony_command(COMMAND_POWER_OFF)

    def volume_up(self):
        """Volume up media player."""
        with self._lock:
            for _ in range(VOLUME_STEPS):
                self._send_sony_command(COMMAND_VOLUME_UP,
                                        expect_response=False)
                time.sleep(VOLUME_REPEAT_DELAY_S)

    def volume_down(self):
        """Volume down media player."""
        with self._lock:
            for _ in range(VOLUME_STEPS):
                self._send_sony_command(COMMAND_VOLUME_DOWN,
                                        expect_response=False)
                time.sleep(VOLUME_REPEAT_DELAY_S)

    def mute_volume(self, mute):
        """Mute or unmute media player."""
        with self._lock:
            if mute:
                self._send_sony_command(COMMAND_MUTE)
            else:
                self._send_sony_command(COMMAND_UNMUTE)

    def select_source(self, source_name):
        """Select input source."""
        with self._lock:
            for source in self._available_sources:
                if source.name == source_name:
                    self._send_sony_command(
                        COMMAND_SELECT_SOURCE + "%.2x" % source.id)
                    for input_mode_id, input_mode in INPUT_MODES.items():
                        if source.input_mode == input_mode:
                            self._send_sony_command(
                                COMMAND_INPUT_MODE + "%.2x" % input_mode_id)
                            break
                    break
