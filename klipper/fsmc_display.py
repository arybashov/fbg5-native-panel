# Support for parallel TFT displays on the stm32 FSMC bus (eg, ILI9488)
#
# Copyright (C) 2026  Aregus393 <97291209+Aregus393@users.noreply.github.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging, os, base64
import mcu

# Display traffic must never delay timed commands (moves, homing):
# send it like neopixel does, behind everything with a real deadline
BACKGROUND_PRIORITY_CLOCK = 0x7fffffff00000000

# Largest rectangle sent in a single fill command (keeps mcu tasks short)
FILL_MAX_PIXELS = 1024
# Most pixels written by one RLE message (mcu work per command stays
# well below Klipper's ~500us task guideline)
RLE_MAX_PIXELS = 1024
# Pixel bytes per fsmc_lcd_pixels command (must be even)
# (54 of the ~56 bytes that fit next to a 1 byte msgid, oid and length)
PIXELS_CHUNK = 54
# Wait for the mcu after this many pixel bytes, so other traffic (clock
# sync, moves) is never stuck behind a long run of display data
PIXELS_BATCH = 3072

# ILI9488 init sequence (from Marlin): (command, parameters, delay after)
ILI9488_INIT = [
    (0x01, [], .150),                       # SWRESET
    (0x11, [], .150),                       # SLPOUT
    (0x3a, [0x55], 0.),                     # COLMOD: 16 bits per pixel
    (0xb1, [0xa0], 0.),                     # FRMCTR1
    (0xb4, [0x02], 0.),                     # INVTR
    (0xb6, [0x02, 0x02], 0.),               # DISCTRL
    (0xc0, [0x15, 0x17], 0.),               # PWCTRL1
    (0xc1, [0x41], 0.),                     # PWCTRL2
    (0xc5, [0x00, 0x12, 0x80], 0.),         # VMCTRL
    (0xe9, [0x00], 0.),                     # SETIMAGE
    (0xf7, [0xa9, 0x51, 0x2c, 0x82], 0.),   # ADJCTL3
    (0xe0, [0x00, 0x03, 0x09, 0x08, 0x16, 0x0a, 0x3f, 0x78,
            0x4c, 0x09, 0x0a, 0x08, 0x16, 0x1a, 0x0f], 0.),  # PGAMCTRL
    (0xe1, [0x00, 0x16, 0x19, 0x03, 0x0f, 0x05, 0x32, 0x45,
            0x46, 0x04, 0x0e, 0x0d, 0x35, 0x37, 0x0f], 0.),  # NGAMCTRL
    (0x20, [], 0.),                         # INVOFF
    (0x13, [], 0.),                         # NORON
    (0x29, [], .050),                       # DISON
]
CMD_MADCTL = 0x36

def rgb565(rgb):
    r, g, b = (rgb >> 16) & 0xff, (rgb >> 8) & 0xff, rgb & 0xff
    return ((r & 0xf8) << 8) | ((g & 0xfc) << 3) | (b >> 3)

# Run length encoding understood by the fsmc_lcd_rle mcu command
RLE_MAX_RUN = 128
RLE_MAX_LITERAL = (PIXELS_CHUNK - 1) // 2

def rle_encode(data, yield_cb=None):
    # data: bytearray of little endian RGB565 pixels -> list of messages
    npix = len(data) // 2
    px = [data[2*i] | (data[2*i + 1] << 8) for i in range(npix)]
    items = []
    i = 0
    next_yield = 8192
    while i < npix:
        if yield_cb is not None and i >= next_yield:
            yield_cb()
            next_yield = i + 8192
        c = px[i]
        j = i + 1
        while j < npix and px[j] == c and j - i < RLE_MAX_RUN:
            j += 1
        if j - i >= 2:
            items.append(bytearray([0x80 | (j - i - 1), c & 0xff, c >> 8]))
            i = j
            continue
        k = i
        while (k < npix and k - i < RLE_MAX_LITERAL
               and not (k + 1 < npix and px[k + 1] == px[k])):
            k += 1
        k = max(k, i + 1)
        items.append(bytearray([k - i - 1]) + data[2*i:2*k])
        i = k
    msgs = []
    cur = bytearray()
    cur_px = 0
    for item in items:
        px = (item[0] & 0x7f) + 1
        if (len(cur) + len(item) > PIXELS_CHUNK
                or cur_px + px > RLE_MAX_PIXELS):
            msgs.append(cur)
            cur = bytearray()
            cur_px = 0
        cur += item
        cur_px += px
    if cur:
        msgs.append(cur)
    return msgs

class FSMCDisplay:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        mcu_name = config.get('mcu', 'mcu')
        self.mcu = mcu.get_printer_mcu(self.printer, mcu_name)
        self.oid = self.mcu.create_oid()
        self.bank = config.getint('bank', 1, minval=1, maxval=4)
        self.rs_line = config.getint('rs_line', 16, minval=0, maxval=23)
        self.addset = config.getint('address_setup', 6, minval=0, maxval=15)
        self.datast = config.getint('data_setup', 6, minval=1, maxval=255)
        self.width = config.getint('width', 480, minval=1)
        self.height = config.getint('height', 320, minval=1)
        madctl = config.get('madctl', '0x28')
        try:
            self.madctl = int(madctl, 0)
        except ValueError:
            raise config.error("Invalid madctl '%s' in section '%s'"
                               % (madctl, config.get_name()))
        if not 0 <= self.madctl <= 0xff:
            raise config.error("madctl must be between 0 and 0xff")
        self.init_on_ready = config.getboolean('init_on_ready', False)
        self.shutdown_ui = config.getboolean('shutdown_ui', False)
        self.in_shutdown = self.initialized = False
        self.ui_epoch = 0
        # Panel tuning: lines of "CMD DATA..." (hex) replacing the same
        # command of the init sequence (eg, "0xC5 00 90 80" for VCOM)
        self.init_overrides = {}
        for line in config.get('init_overrides', '').splitlines():
            line = line.split('#')[0].strip()
            if not line:
                continue
            try:
                vals = [int(v, 16) for v in line.replace(',', ' ').split()]
            except ValueError:
                raise config.error("Invalid init_overrides line '%s'" % line)
            if any(not 0 <= v <= 0xff for v in vals):
                raise config.error("init_overrides values must be bytes")
            self.init_overrides[vals[0]] = vals[1:]
        # Incremented after every panel init (its reset clears the screen)
        self.init_epoch = 0
        # Reset and backlight are ordinary digital outputs
        ppins = self.printer.lookup_object('pins')
        self.reset_pin = self.backlight_pin = None
        if config.get('reset_pin', None) is not None:
            self.reset_pin = ppins.setup_pin('digital_out',
                                             config.get('reset_pin'))
            self.reset_pin.setup_max_duration(0.)
            self.reset_pin.setup_start_value(1, 1)
        if config.get('backlight_pin', None) is not None:
            self.backlight_pin = ppins.setup_pin('digital_out',
                                                 config.get('backlight_pin'))
            self.backlight_pin.setup_max_duration(0.)
            self.backlight_pin.setup_start_value(1, int(self.shutdown_ui))
        self.cmd_cmd = self.fill_cmd = self.pixels_cmd = None
        self.mcu.register_config_callback(self.build_config)
        self.is_ready = False
        self.printer.register_event_handler("klippy:ready", self.handle_ready)
        self.printer.register_event_handler("klippy:shutdown",
                                            self.handle_shutdown)
        self.printer.register_event_handler("klippy:disconnect",
                                            self.handle_not_ready)
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command('LCD_INIT', self.cmd_LCD_INIT,
                               desc=self.cmd_LCD_INIT_help)
        gcode.register_command('LCD_FILL', self.cmd_LCD_FILL,
                               desc=self.cmd_LCD_FILL_help)
        gcode.register_command('LCD_BACKLIGHT', self.cmd_LCD_BACKLIGHT,
                               desc=self.cmd_LCD_BACKLIGHT_help)
        gcode.register_command('LCD_TEST_PIXELS', self.cmd_LCD_TEST_PIXELS,
                               desc=self.cmd_LCD_TEST_PIXELS_help)
        gcode.register_command('LCD_TEST_IMAGE', self.cmd_LCD_TEST_IMAGE,
                               desc=self.cmd_LCD_TEST_IMAGE_help)
        gcode.register_command('LCD_PLAY', self.cmd_LCD_PLAY,
                               desc=self.cmd_LCD_PLAY_help)
        gcode.register_command('LCD_IMAGE', self.cmd_LCD_IMAGE,
                               desc=self.cmd_LCD_IMAGE_help)
        gcode.register_command('LCD_DEMO', self.cmd_LCD_DEMO,
                               desc=self.cmd_LCD_DEMO_help)
        gcode.register_command('LCD_CMD', self.cmd_LCD_CMD,
                               desc=self.cmd_LCD_CMD_help)
        self.image_cmd = None
        self.serial_errors_cmd = None
        self.anim_cache = {}
        # Window setup and pixel data of one drawing must not interleave
        self.draw_mutex = self.reactor.mutex()
        # Only one "get_uptime" query may wait for its response at a time
        self.sync_mutex = self.reactor.mutex()
        # API for external renderers (eg, a KlipperScreen mirror)
        webhooks = self.printer.lookup_object('webhooks')
        webhooks.register_endpoint("fsmc_display/draw_rle",
                                   self._handle_draw_rle)
        webhooks.register_endpoint("fsmc_display/serial_errors",
                                   self._handle_serial_errors)
        gcode.register_command('SERIAL_ERRORS', self.cmd_SERIAL_ERRORS,
                               desc=self.cmd_SERIAL_ERRORS_help)
    def build_config(self):
        if self.shutdown_ui and self.mcu.get_constants().get('FSMC_SHUTDOWN_UI') != 1:
            raise self.printer.config_error('Shutdown UI requires the matching FBG firmware')
        self.mcu.add_config_cmd(
            "config_fsmc_lcd oid=%d bank=%d rs_line=%d addset=%d datast=%d"
            % (self.oid, self.bank, self.rs_line, self.addset, self.datast))
        if self.shutdown_ui:
            self.mcu.add_config_cmd('config_fsmc_shutdown oid=%d' % self.oid)
        cq = self.mcu.alloc_command_queue()
        self.cmd_cmd = self.mcu.lookup_command(
            "fsmc_lcd_cmd oid=%c cmd=%c data=%*s", cq=cq)
        self.fill_cmd = self.mcu.lookup_command(
            "fsmc_lcd_fill oid=%c x0=%hu y0=%hu x1=%hu y1=%hu color=%hu",
            cq=cq)
        self.pixels_cmd = self.mcu.lookup_command(
            "fsmc_lcd_pixels oid=%c data=%*s", cq=cq)
        self.rle_cmd = self.mcu.lookup_command(
            "fsmc_lcd_rle oid=%c data=%*s", cq=cq)
        # Built-in images are optional in the mcu firmware; they are
        # drawn in parts (fsmc_lcd_image_next) to keep mcu commands short
        start_fmt = "fsmc_lcd_image_start oid=%c id=%c x=%hu y=%hu"
        if self.mcu.try_lookup_command(start_fmt) is not None:
            self.image_cmd = self.mcu.lookup_query_command(
                start_fmt, "fsmc_lcd_image_state oid=%c pixels=%u",
                oid=self.oid, cq=cq)
            self.image_next_cmd = self.mcu.lookup_command(
                "fsmc_lcd_image_next oid=%c", cq=cq)
        # Receive error counters of the dma serial driver (if built in)
        if self.mcu.try_lookup_command("get_serial_errors") is not None:
            self.serial_errors_cmd = self.mcu.lookup_query_command(
                "get_serial_errors",
                "serial_errors noise=%u frame=%u overrun=%u", cq=cq)
        # Answered only after all queued display commands have run
        self.sync_cmd = self.mcu.lookup_query_command(
            "get_uptime", "uptime high=%u clock=%u", cq=cq)
        if self.shutdown_ui:
            self.ui_ping = self.mcu.lookup_query_command(
                'fsmc_lcd_ping oid=%c shutdown_ui=%c',
                'fsmc_lcd_state oid=%c epoch=%u shutdown=%c', oid=self.oid, cq=cq)
    def handle_not_ready(self):
        self.is_ready = False
        self.initialized = False
    def handle_shutdown(self):
        self.in_shutdown = True
        self.is_ready = self.shutdown_ui and self.initialized
        self.init_epoch += 1
    def handle_ready(self):
        self.is_ready = True
        self.in_shutdown = False
        if self.init_on_ready:
            self.reactor.register_callback(lambda e: self.init_display())
    # Low level helpers
    def _pause(self, delay):
        self.reactor.pause(self.reactor.monotonic() + delay)
    def _set_pin(self, pin, value):
        print_time = self.mcu.estimated_print_time(self.reactor.monotonic())
        pin.set_digital(print_time + .100, value)
        self._pause(.110)
    def send_cmd(self, cmd, data=[]):
        self.cmd_cmd.send([self.oid, cmd, data], reqclock=BACKGROUND_PRIORITY_CLOCK)
    def init_display(self):
        # Holding draw_mutex keeps renderers out while the panel resets
        with self.draw_mutex:
            if self.reset_pin is not None:
                self._set_pin(self.reset_pin, 0)
                self._set_pin(self.reset_pin, 1)
                self._pause(.120)
            done = set()
            for cmd, data, delay in ILI9488_INIT:
                if cmd == 0x29:
                    # Extra override commands go before DISON
                    for ocmd, odata in sorted(self.init_overrides.items()):
                        if ocmd not in done:
                            self.send_cmd(ocmd, odata)
                self.send_cmd(cmd, self.init_overrides.get(cmd, data))
                done.add(cmd)
                if cmd == 0x11:
                    self.send_cmd(CMD_MADCTL, [self.madctl])
                    done.add(CMD_MADCTL)
                if delay:
                    self._pause(delay)
            if self.backlight_pin is not None:
                self._set_pin(self.backlight_pin, 1)
            self.wait_idle()
            self.init_epoch += 1
            self.initialized = not self.in_shutdown
        logging.info("fsmc_display: init sequence sent (overrides: %s)"
                     % (", ".join("%02X" % c for c in sorted(self.init_overrides))
                        or "none",))
    def fill(self, x0, y0, x1, y1, color):
        rows = max(1, FILL_MAX_PIXELS // (x1 - x0 + 1))
        y = y0
        while y <= y1:
            yend = min(y + rows - 1, y1)
            self.fill_cmd.send([self.oid, x0, y, x1, yend, color],
                               reqclock=BACKGROUND_PRIORITY_CLOCK)
            y = yend + 1
    def _set_window(self, x0, y0, x1, y1):
        self.send_cmd(0x2a, [x0 >> 8, x0 & 0xff, x1 >> 8, x1 & 0xff])
        self.send_cmd(0x2b, [y0 >> 8, y0 & 0xff, y1 >> 8, y1 & 0xff])
        self.send_cmd(0x2c)
    def draw_pixels(self, x0, y0, x1, y1, data):
        # data: little endian RGB565, (x1-x0+1)*(y1-y0+1) pixels
        with self.draw_mutex:
            self._set_window(x0, y0, x1, y1)
            for pos in range(0, len(data), PIXELS_CHUNK):
                self.pixels_cmd.send(
                    [self.oid, data[pos:pos + PIXELS_CHUNK]], reqclock=BACKGROUND_PRIORITY_CLOCK)
                if not (pos + PIXELS_CHUNK) % PIXELS_BATCH:
                    self.wait_idle()
    def draw_rle(self, x0, y0, x1, y1, msgs):
        with self.draw_mutex:
            self._set_window(x0, y0, x1, y1)
            sent = 0
            for msg in msgs:
                self.rle_cmd.send([self.oid, msg], reqclock=BACKGROUND_PRIORITY_CLOCK)
                sent += len(msg)
                if sent >= PIXELS_BATCH:
                    self.wait_idle()
                    sent = 0
    def draw_image(self, image_id, x, y):
        # Image from mcu flash: start (returns its size), then short parts
        with self.draw_mutex:
            params = self.image_cmd.send([self.oid, image_id, x, y],
                                         reqclock=BACKGROUND_PRIORITY_CLOCK)
            parts = (params['pixels'] + RLE_MAX_PIXELS - 1) // RLE_MAX_PIXELS
            for i in range(parts):
                self.image_next_cmd.send([self.oid],
                                         reqclock=BACKGROUND_PRIORITY_CLOCK)
                if not (i + 1) % 32:
                    self.wait_idle()
            self.wait_idle()
    def wait_idle(self):
        with self.sync_mutex:
            if self.shutdown_ui:
                result = self.ui_ping.send([self.oid, int(self.in_shutdown)],
                                           reqclock=BACKGROUND_PRIORITY_CLOCK)
                if result['epoch'] != self.ui_epoch:
                    self.ui_epoch = result['epoch']
                    self.init_epoch += 1
            else:
                self.sync_cmd.send(reqclock=BACKGROUND_PRIORITY_CLOCK)
    def get_status(self, eventtime):
        return {'available': self.is_ready and self.initialized,
                'shutdown_supported': self.shutdown_ui,
                'in_shutdown': self.in_shutdown}
    def _split_rle(self, stream):
        # Cut an RLE item stream into mcu messages on item boundaries
        msgs = []
        cur = bytearray()
        cur_px = 0
        pos = 0
        while pos < len(stream):
            hdr = stream[pos]
            px = (hdr & 0x7f) + 1
            size = 3 if hdr & 0x80 else 1 + 2 * px
            if size > PIXELS_CHUNK or pos + size > len(stream):
                raise self.printer.command_error("Invalid RLE stream")
            if (len(cur) + size > PIXELS_CHUNK
                    or cur_px + px > RLE_MAX_PIXELS):
                msgs.append(cur)
                cur = bytearray()
                cur_px = 0
            cur += stream[pos:pos + size]
            cur_px += px
            pos += size
        if cur:
            msgs.append(cur)
        return msgs
    def _handle_draw_rle(self, web_request):
        # params: {"rects": [[x0, y0, x1, y1, "<base64 RLE stream>"], ...]}
        if not self.is_ready:
            # Clients retry; never raise an internal error (it would shut
            # down the printer) while klippy starts or is shut down
            raise web_request.error("fsmc_display not ready")
        rects = web_request.get('rects')
        total = 0
        try:
            # Explicit handshake unlocks only LCD drawing in MCU shutdown.
            # Also clear the stale-frame watchdog before writing a new frame.
            if self.shutdown_ui:
                self.wait_idle()
            for rect in rects:
                x0, y0, x1, y1, data = rect
                if not (0 <= x0 <= x1 < self.width
                        and 0 <= y0 <= y1 < self.height):
                    raise web_request.error("Rectangle out of range")
                stream = bytearray(base64.b64decode(data))
                self.draw_rle(x0, y0, x1, y1, self._split_rle(stream))
                total += len(stream)
            self.wait_idle()
        except web_request.error:
            raise
        except Exception as e:
            # A display problem must never shut down the printer
            logging.exception("fsmc_display: draw_rle failed")
            raise web_request.error("fsmc_display draw failed: %s" % (e,))
        # Clients redraw everything when the epoch changes (panel init)
        web_request.send({'bytes': total, 'epoch': self.init_epoch})
    def _yield(self):
        self.reactor.pause(self.reactor.monotonic())
    def _diff_rects(self, old, new, band=16):
        # Changed area as one rectangle per band of rows
        w = self.width
        rowbytes = w * 2
        rects = []
        for y0 in range(0, self.height, band):
            y1 = min(y0 + band, self.height) - 1
            minx, maxx = w, -1
            for y in range(y0, y1 + 1):
                a = y * rowbytes
                if old[a:a + rowbytes] == new[a:a + rowbytes]:
                    continue
                x = 0
                while old[a + 2*x:a + 2*x + 2] == new[a + 2*x:a + 2*x + 2]:
                    x += 1
                minx = min(minx, x)
                x = w - 1
                while old[a + 2*x:a + 2*x + 2] == new[a + 2*x:a + 2*x + 2]:
                    x -= 1
                maxx = max(maxx, x)
            if maxx >= 0:
                rects.append((minx, y0, maxx, y1))
        return rects
    def _crop(self, frame, x0, y0, x1, y1):
        out = bytearray()
        for y in range(y0, y1 + 1):
            a = (y * self.width + x0) * 2
            out += frame[a:a + (x1 - x0 + 1) * 2]
        return out
    # G-Code commands
    cmd_LCD_INIT_help = "Reset and initialize the FSMC display"
    def cmd_LCD_INIT(self, gcmd):
        self.init_display()
    cmd_LCD_FILL_help = "Fill a rectangle: LCD_FILL COLOR=rrggbb [X0 Y0 X1 Y1]"
    def cmd_LCD_FILL(self, gcmd):
        color = gcmd.get('COLOR', '000000')
        try:
            rgb = int(color, 16)
        except ValueError:
            raise gcmd.error("Invalid COLOR '%s'" % (color,))
        x0 = gcmd.get_int('X0', 0, minval=0, maxval=self.width - 1)
        y0 = gcmd.get_int('Y0', 0, minval=0, maxval=self.height - 1)
        x1 = gcmd.get_int('X1', self.width - 1, minval=x0,
                          maxval=self.width - 1)
        y1 = gcmd.get_int('Y1', self.height - 1, minval=y0,
                          maxval=self.height - 1)
        self.fill(x0, y0, x1, y1, rgb565(rgb))
    def _timed_draw(self, gcmd, x0, y0, x1, y1, data):
        self.wait_idle()
        start = self.reactor.monotonic()
        self.draw_pixels(x0, y0, x1, y1, data)
        self.wait_idle()
        elapsed = self.reactor.monotonic() - start
        rate = len(data) / elapsed
        gcmd.respond_info(
            "%d pixels (%d bytes) in %.2fs: %.1f KiB/s, full screen %.1fs"
            % (len(data) // 2, len(data), elapsed, rate / 1024.,
               self.width * self.height * 2 / rate))
    def _test_image(self, step):
        w, h = self.width, self.height
        white, black, red = 0xffff, 0x0000, 0xf800
        bars = [0xffff, 0xffe0, 0x07ff, 0x07e0, 0xf81f, 0xf800, 0x001f, 0x0000]
        data = bytearray(w * h * 2)
        pos = 0
        for y in range(h):
            if not y % 8:
                # Generating the image is slow - let the reactor run
                self.reactor.pause(self.reactor.monotonic())
            for x in range(w):
                if x in (0, w - 1) or y in (0, h - 1):
                    c = red
                elif y < h // 3:
                    c = bars[x * 8 // w]
                elif y < 2 * h // 3:
                    v = x * 255 // (w - 1)
                    band = (y - h // 3) * 3 // (h // 3 + 1)
                    c = rgb565(v << (16 - 8 * band))
                elif x < w // 2:
                    c = white if (x + y) & 1 else black
                elif (x % 20 == 0) or (y % 20 == 0):
                    c = white
                elif (y > h - 30 and y < h - 10
                      and w - 20 - 24 * step < x <= w - 20
                      and (w - 20 - x) % 24 < 16):
                    c = 0xffe0
                else:
                    c = 0x0010
                data[pos] = c & 0xff
                data[pos + 1] = c >> 8
                pos += 2
        return data
    cmd_LCD_TEST_IMAGE_help = "Draw a full screen test image (STEP=n markers)"
    def cmd_LCD_TEST_IMAGE(self, gcmd):
        step = gcmd.get_int('STEP', 0, minval=0, maxval=15)
        data = self._test_image(step)
        self._timed_draw(gcmd, 0, 0, self.width - 1, self.height - 1, data)
    cmd_LCD_PLAY_help = ("Cycle raw RGB565 frames: LCD_PLAY DIR= FILES=a,b,c"
                         " MODE=raw|rle|diff CYCLES= DELAY=")
    def cmd_LCD_PLAY(self, gcmd):
        dirname = gcmd.get('DIR')
        names = [n.strip() for n in gcmd.get('FILES').split(',') if n.strip()]
        mode = gcmd.get('MODE', 'rle').lower()
        if mode not in ('raw', 'rle', 'diff'):
            raise gcmd.error("MODE must be raw, rle or diff")
        cycles = gcmd.get_int('CYCLES', 2, minval=1, maxval=1000)
        delay = gcmd.get_float('DELAY', 1., minval=0.)
        size = self.width * self.height * 2
        frames = []
        for name in names:
            path = "%s/%s.rgb565" % (dirname, name)
            try:
                f = open(path, 'rb')
                frame = bytearray(f.read())
                f.close()
            except IOError as e:
                raise gcmd.error("Unable to read %s: %s" % (path, e))
            if len(frame) != size:
                raise gcmd.error("%s: expected %d bytes" % (path, size))
            frames.append(frame)
        # Prepare all transfers before timing anything
        prep_start = self.reactor.monotonic()
        full = [rle_encode(fr, self._yield) for fr in frames]
        steps = []
        n = len(frames)
        for i in range(n):
            j = (i + 1) % n
            if mode == 'raw':
                ops = [('raw', (0, 0, self.width - 1, self.height - 1),
                        frames[j])]
            elif mode == 'rle':
                ops = [('rle', (0, 0, self.width - 1, self.height - 1),
                        full[j])]
            else:
                ops = []
                for rect in self._diff_rects(frames[i], frames[j]):
                    pix = self._crop(frames[j], *rect)
                    ops.append(('rle', rect, rle_encode(pix, self._yield)))
                    self._yield()
            nbytes = sum([len(d) if k == 'raw' else sum(map(len, d))
                          for k, r, d in ops])
            steps.append((names[i], names[j], ops, nbytes))
        prep_time = self.reactor.monotonic() - prep_start
        # Start from the first frame, then time every transition
        self.draw_rle(0, 0, self.width - 1, self.height - 1, full[0])
        self.wait_idle()
        times = []
        for c in range(cycles):
            for src, dst, ops, nbytes in steps:
                self._pause(delay)
                start = self.reactor.monotonic()
                for kind, rect, data in ops:
                    if kind == 'raw':
                        self.draw_pixels(*(rect + (data,)))
                    else:
                        self.draw_rle(*(rect + (data,)))
                self.wait_idle()
                times.append((src, dst, self.reactor.monotonic() - start,
                              nbytes, len(ops)))
        lines = ["LCD_PLAY mode=%s: %d transitions, host prep %.1fs"
                 % (mode, len(times), prep_time)]
        for src, dst, ops, nbytes in steps:
            t = [x[2] for x in times if x[0] == src and x[1] == dst]
            lines.append("  %s -> %s: %d bytes (%.0f%% of raw) in %d rect(s),"
                         " avg %.2fs min %.2fs max %.2fs"
                         % (src, dst, nbytes, 100. * nbytes / size,
                            len(ops), sum(t) / len(t), min(t), max(t)))
        allt = [x[2] for x in times]
        lines.append("  overall avg %.2fs per screen change"
                     % (sum(allt) / len(allt),))
        gcmd.respond_info("\n".join(lines))
    def get_serial_errors(self):
        # sync_mutex: only one query of this kind may wait at a time
        with self.sync_mutex:
            params = self.serial_errors_cmd.send(
                reqclock=BACKGROUND_PRIORITY_CLOCK)
        return {'noise': params['noise'], 'frame': params['frame'],
                'overrun': params['overrun']}
    def _handle_serial_errors(self, web_request):
        if not self.is_ready or self.serial_errors_cmd is None:
            raise web_request.error("serial error counters not available")
        try:
            result = self.get_serial_errors()
        except Exception as e:
            logging.exception("fsmc_display: get_serial_errors failed")
            raise web_request.error("get_serial_errors failed: %s" % (e,))
        web_request.send(result)
    cmd_SERIAL_ERRORS_help = "Report mcu serial receive error counters"
    def cmd_SERIAL_ERRORS(self, gcmd):
        if self.serial_errors_cmd is None:
            raise gcmd.error("mcu firmware has no serial error counters")
        e = self.get_serial_errors()
        gcmd.respond_info("mcu serial rx errors: noise=%d frame=%d overrun=%d"
                          % (e['noise'], e['frame'], e['overrun']))
    cmd_LCD_CMD_help = ("Send a raw controller command: LCD_CMD CMD=0xC5"
                        " DATA=00,12,80 (hex bytes)")
    def cmd_LCD_CMD(self, gcmd):
        try:
            cmd = int(gcmd.get('CMD'), 0)
            data = [int(b, 16) for b in
                    gcmd.get('DATA', '').replace(',', ' ').split()]
        except ValueError:
            raise gcmd.error("LCD_CMD: CMD and DATA must be hex numbers")
        if not 0 <= cmd <= 0xff or any(not 0 <= b <= 0xff for b in data):
            raise gcmd.error("LCD_CMD: values must be bytes")
        if len(data) > PIXELS_CHUNK:
            raise gcmd.error("LCD_CMD: at most %d data bytes" % PIXELS_CHUNK)
        with self.draw_mutex:
            self.send_cmd(cmd, data)
        self.wait_idle()
        gcmd.respond_info("LCD_CMD 0x%02X %s" % (
            cmd, " ".join("%02X" % b for b in data)))
    cmd_LCD_IMAGE_help = "Draw an image stored in mcu flash: LCD_IMAGE ID= [X Y]"
    def cmd_LCD_IMAGE(self, gcmd):
        if self.image_cmd is None:
            raise gcmd.error("mcu firmware has no built-in images")
        self.draw_image(gcmd.get_int('ID', 0, minval=0),
                        gcmd.get_int('X', 0, minval=0),
                        gcmd.get_int('Y', 0, minval=0))
    def _prepare_anim(self, dirname, prefix):
        # Cached full-frame and frame-to-frame (diff) encodings
        key = (dirname, prefix)
        if key in self.anim_cache:
            return self.anim_cache[key]
        names = sorted(n for n in os.listdir(dirname)
                       if n.startswith(prefix + "_") and n.endswith(".rgb565"))
        if not names:
            raise self.printer.command_error(
                "No %s_NN.rgb565 frames in %s" % (prefix, dirname))
        frames = []
        for n in names:
            f = open(os.path.join(dirname, n), 'rb')
            frames.append(bytearray(f.read()))
            f.close()
        full = [rle_encode(fr, self._yield) for fr in frames]
        diffs = []
        for i in range(len(frames)):
            j = (i + 1) % len(frames)
            ops = []
            for rect in self._diff_rects(frames[i], frames[j]):
                pix = self._crop(frames[j], *rect)
                ops.append((rect, rle_encode(pix, self._yield)))
                self._yield()
            diffs.append(ops)
        self.anim_cache[key] = (full, diffs)
        return self.anim_cache[key]
    def _animate(self, anim, seconds, start_index):
        # Show the next frame in full, then diff updates until time is up
        full, diffs = anim
        n = len(full)
        i = start_index % n
        end = self.reactor.monotonic() + seconds
        self.draw_rle(0, 0, self.width - 1, self.height - 1, full[i])
        self.wait_idle()
        updates = 0
        while True:
            now = self.reactor.monotonic()
            if now >= end:
                break
            for rect, msgs in diffs[i]:
                self.draw_rle(*(rect + (msgs,)))
            self.wait_idle()
            i = (i + 1) % n
            updates += 1
        return i, updates
    cmd_LCD_DEMO_help = "Run the FB5 display demo (LCD_DEMO DIR= [PREPARE=1])"
    def cmd_LCD_DEMO(self, gcmd):
        dirname = gcmd.get('DIR')
        pages = {'main': self._prepare_anim(dirname, 'mp'),
                 'job': self._prepare_anim(dirname, 'js')}
        if gcmd.get_int('PREPARE', 0):
            gcmd.respond_info("LCD_DEMO: frames prepared")
            return
        if self.image_cmd is None:
            raise gcmd.error("mcu firmware has no built-in images")
        scale = gcmd.get_float('SLIDE_SCALE', 1., above=0.)
        script = ([('image', 0, 5. * scale), ('image', 1, 3. * scale),
                   ('main', None, 5.), ('job', None, 5.),
                   ('main', None, 3.), ('job', None, 3.)]
                  + [('main', None, 1.), ('job', None, 1.)] * 3)
        pos = {'main': 0, 'job': 0}
        report = []
        for kind, arg, seconds in script:
            start = self.reactor.monotonic()
            if kind == 'image':
                self.draw_image(arg, 0, 0)
                drawn = self.reactor.monotonic() - start
                self._pause(max(0., seconds - drawn))
                report.append("image %d: drawn in %.3fs, shown %.2fs"
                              % (arg, drawn, seconds))
            else:
                pos[kind], updates = self._animate(pages[kind], seconds,
                                                   pos[kind])
                report.append("%s page %.0fs: %d updates (%.1f/s)"
                              % (kind, seconds, updates, updates / seconds))
        gcmd.respond_info("LCD_DEMO done\n" + "\n".join(report))
    cmd_LCD_TEST_PIXELS_help = "Draw a gradient band and report the speed"
    def cmd_LCD_TEST_PIXELS(self, gcmd):
        y0 = gcmd.get_int('Y0', 0, minval=0, maxval=self.height - 1)
        rows = gcmd.get_int('ROWS', 64, minval=1, maxval=self.height - y0)
        data = bytearray()
        for y in range(rows):
            for x in range(self.width):
                c = rgb565(((x * 255 // self.width) << 16)
                           | ((y * 255 // rows) << 8) | 0x80)
                data.append(c & 0xff)
                data.append(c >> 8)
        self._timed_draw(gcmd, 0, y0, self.width - 1, y0 + rows - 1, data)
    cmd_LCD_BACKLIGHT_help = "Turn the display backlight on or off"
    def cmd_LCD_BACKLIGHT(self, gcmd):
        if self.backlight_pin is None:
            raise gcmd.error("No backlight_pin configured")
        self._set_pin(self.backlight_pin, gcmd.get_int('ON', 1, 0, 1))

def load_config(config):
    return FSMCDisplay(config)
