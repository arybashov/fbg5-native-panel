# Support for an XPT2046 resistive touch controller (polled over SPI)
#
# Copyright (C) 2026  Aregus393 <97291209+Aregus393@users.noreply.github.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
from . import bus

# XPT2046 control bytes: start bit, channel, 12 bit, differential, PD=00
CMD_Z1, CMD_Z2, CMD_X, CMD_Y = 0xb0, 0xc0, 0xd0, 0x90
SAMPLES = 3
# Polling must never delay timed commands (see fsmc_display)
BACKGROUND_PRIORITY_CLOCK = 0x7fffffff00000000

class FSMCTouch:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.spi = bus.MCU_SPI_from_config(config, 0, default_speed=1000000)
        self.shutdown_ui = config.getboolean('shutdown_ui', False)
        self.touch_read = None
        if self.shutdown_ui:
            self.touch_oid = self.spi.get_mcu().create_oid()
            self.spi.get_mcu().register_config_callback(self.build_config)
        # Other chips sharing the bus (eg, the spi flash) are deselected
        ppins = self.printer.lookup_object('pins')
        self.idle_pins = []
        for pin in config.getlist('deselect_pins', [], sep=','):
            p = ppins.setup_pin('digital_out', pin.strip())
            p.setup_max_duration(0.)
            p.setup_start_value(1, 1)
            self.idle_pins.append(p)
        self.min_pressure = config.getint('min_pressure', 100, minval=0)
        cmds = [CMD_Z1, CMD_Z2] + [CMD_X] * SAMPLES + [CMD_Y] * SAMPLES
        self.request = []
        for c in cmds:
            self.request += [c, 0, 0]
        # Final dummy conversion with power down keeps PENIRQ enabled
        self.request += [0x80, 0, 0]
        # Only one spi_transfer query may wait for its response at a time
        self.mutex = self.printer.get_reactor().mutex()
        self.is_ready = False
        self.printer.register_event_handler("klippy:ready", self._ready)
        self.printer.register_event_handler("klippy:shutdown", self._shutdown)
        self.printer.register_event_handler("klippy:disconnect",
                                            self._not_ready)
        webhooks = self.printer.lookup_object('webhooks')
        webhooks.register_endpoint("fsmc_touch/read", self._handle_read)
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command('TOUCH_READ', self.cmd_TOUCH_READ,
                               desc=self.cmd_TOUCH_READ_help)
    def build_config(self):
        mcu = self.spi.get_mcu()
        if mcu.get_constants().get('FSMC_SHUTDOWN_UI') != 1:
            raise self.printer.config_error('Shutdown touch requires the matching FBG firmware')
        mcu.add_config_cmd('config_fsmc_touch oid=%d spi_oid=%d'
                           % (self.touch_oid, self.spi.get_oid()))
        self.touch_read = mcu.lookup_query_command(
            'fsmc_touch_read oid=%c', 'fsmc_touch_response oid=%c response=%*s',
            oid=self.touch_oid, cq=self.spi.get_command_queue())
    def read(self):
        with self.mutex:
            if self.touch_read is not None:
                params = self.touch_read.send([self.touch_oid],
                                              reqclock=BACKGROUND_PRIORITY_CLOCK)
            else:
                params = self.spi.spi_transfer(
                    self.request, reqclock=BACKGROUND_PRIORITY_CLOCK)
        resp = bytearray(params['response'])
        vals = [((resp[i + 1] << 8) | resp[i + 2]) >> 3
                for i in range(0, len(self.request) - 3, 3)]
        z1, z2 = vals[0], vals[1]
        xs = sorted(vals[2:2 + SAMPLES])
        ys = sorted(vals[2 + SAMPLES:2 + 2 * SAMPLES])
        x, y = xs[SAMPLES // 2], ys[SAMPLES // 2]
        pressure = z1 + 4095 - z2
        pressed = z1 >= self.min_pressure
        return {'x': x, 'y': y, 'z1': z1, 'z2': z2, 'pressure': pressure,
                'pressed': pressed, 'x_spread': xs[-1] - xs[0],
                'y_spread': ys[-1] - ys[0]}
    def _ready(self):
        self.is_ready = True
    def _not_ready(self):
        self.is_ready = False
    def _shutdown(self):
        self.is_ready = self.is_ready and self.shutdown_ui
    def get_status(self, eventtime):
        return {'available': self.is_ready, 'shutdown_supported': self.shutdown_ui}
    def _handle_read(self, web_request):
        if not self.is_ready:
            raise web_request.error("fsmc_touch not ready")
        try:
            result = self.read()
        except Exception as e:
            # A touch problem must never shut down the printer
            logging.exception("fsmc_touch: read failed")
            raise web_request.error("fsmc_touch read failed: %s" % (e,))
        web_request.send(result)
    cmd_TOUCH_READ_help = "Report raw XPT2046 touch readings"
    def cmd_TOUCH_READ(self, gcmd):
        r = self.read()
        gcmd.respond_info("touch: pressed=%s x=%d y=%d z1=%d z2=%d"
                          % (r['pressed'], r['x'], r['y'], r['z1'], r['z2']))

def load_config(config):
    return FSMCTouch(config)
