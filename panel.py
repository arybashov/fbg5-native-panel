#!/usr/bin/env python3
"""Native 480x320 FBG5 panel. GPL-3.0-or-later. No browser or network listener.

Four independent Klipper socket clients handle status, drawing, touch, and
commands. emergency_stop gets its own connection and never enters G-code FIFO.
"""
import argparse
import array
import base64
import copy
import json
import logging
import math
import os
from pathlib import Path
import queue
import socket
import statistics
import sys
import threading
import time

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 480, 320
STOP = (400, 178, 472, 312)
BG, SURFACE, FG, MUTED = '#111823', '#202c3d', '#edf3fc', '#a2b1c7'
BLUE, BLUE_INK, RED, GREEN = '#9dc2ff', '#102039', '#a51f30', '#8cdcb0'
OBJECTS = {'webhooks': None, 'extruder': ['temperature', 'target', 'can_extrude', 'power'],
           'heater_bed': ['temperature', 'target', 'power'], 'print_stats': None,
           'virtual_sdcard': ['progress', 'is_active'], 'display_status': ['progress'],
           'pause_resume': ['is_paused']}
DEFAULTS = {'socket': '/opt/fbg5/printer_data/comms/klippy.sock',
            'font': '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
            'calibration': '/opt/fbg5/printer_data/config/panel-calibration.json',
            'state_file': '/opt/fbg5/printer_data/logs/panel-state.json',
            'temperature': 200, 'load_mm': 20, 'unload_mm': 30, 'purge_mm': 5,
            'speed': 2, 'touch_interval': .04}


class RPC:
    def __init__(self, path, timeout=4):
        self.path, self.timeout, self.sock, self.buf, self.seq = path, timeout, None, b'', 0

    def close(self):
        if self.sock:
            self.sock.close()
        self.sock, self.buf = None, b''

    def call(self, method, params=None):
        if self.sock is None:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            try:
                self.sock.connect(self.path)
            except Exception:
                self.close()
                raise
        self.seq += 1
        try:
            self.sock.sendall(json.dumps({'id': self.seq, 'method': method,
                                         'params': params or {}}).encode() + b'\x03')
            while True:
                while b'\x03' not in self.buf:
                    part = self.sock.recv(65536)
                    if not part:
                        raise ConnectionError('Klipper socket closed')
                    self.buf += part
                    if len(self.buf) > 2_000_000:
                        raise ValueError('Oversized RPC response')
                raw, self.buf = self.buf.split(b'\x03', 1)
                message = json.loads(raw)
                if message.get('id') != self.seq:
                    continue
                if 'error' in message:
                    raise RuntimeError(str(message['error']))
                return message.get('result', {})
        except Exception:
            # A command with an uncertain outcome is NEVER replayed.
            self.close()
            raise


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    os.replace(tmp, path)


def idle(status):
    return (status.get('webhooks', {}).get('state') == 'ready'
            and status.get('print_stats', {}).get('state') in ('standby', 'complete', 'cancelled')
            and not status.get('virtual_sdcard', {}).get('is_active', False)
            and not status.get('pause_resume', {}).get('is_paused', False))


def require_idle(status, hot=False, cold=False):
    if not idle(status):
        raise ValueError('Управление доступно только в простое')
    if hot and not status.get('extruder', {}).get('can_extrude', False):
        raise ValueError('Сначала нагрейте сопло')
    if cold and any(status.get(h, {}).get('target', 0) > 0 for h in ('extruder', 'heater_bed')):
        raise ValueError('Сначала выключите нагрев')


def progress_info(status):
    stats = status.get('print_stats', {})
    state = stats.get('state', 'standby')
    p = status.get('virtual_sdcard', {}).get('progress', 0)
    if not p:
        p = status.get('display_status', {}).get('progress', 0)
    p = max(0., min(1., float(p or 0)))
    if state == 'complete':
        p = 1.
    if state == 'standby':
        p = 0.
    duration = max(0., float(stats.get('print_duration', 0)))
    # Byte progress gives only a rough estimate, never promise an exact ETA.
    remaining = duration * (1-p)/p if state in ('printing', 'paused') and p >= .02 and duration >= 60 else None
    return p, remaining


def fit_affine(raw, targets):
    """Least squares 2D affine map; reject poor/collapsed calibration."""
    if len(raw) < 4 or len(raw) != len(targets):
        raise ValueError('Need at least four calibration points')
    if any(max(p[i] for p in raw)-min(p[i] for p in raw) < 500 for i in (0, 1)):
        raise ValueError('Недостаточный размах координат')
    rows = [[x/4095., y/4095., 1.] for x, y in raw]
    def solve(values):
        matrix = [[sum(r[i]*r[j] for r in rows) for j in range(3)]
                  + [sum(r[i]*v for r, v in zip(rows, values))] for i in range(3)]
        for i in range(3):
            pivot = max(range(i, 3), key=lambda k: abs(matrix[k][i]))
            matrix[i], matrix[pivot] = matrix[pivot], matrix[i]
            d = matrix[i][i]
            if abs(d) < 1e-8:
                raise ValueError('Вырожденная калибровка')
            matrix[i] = [v/d for v in matrix[i]]
            for k in range(3):
                if k != i:
                    d = matrix[k][i]
                    matrix[k] = [a-d*b for a, b in zip(matrix[k], matrix[i])]
        result = [matrix[i][-1] for i in range(3)]
        return [result[0]/4095., result[1]/4095., result[2]]
    transform = [solve([p[axis] for p in targets]) for axis in (0, 1)]
    errors = [math.dist(map_touch(transform, *p), t) for p, t in zip(raw, targets)]
    if max(errors) > 15:
        raise ValueError('Слишком большая ошибка калибровки')
    return transform, errors


def map_touch(transform, x, y):
    return tuple(a*x+b*y+c for a, b, c in transform)


def inside(point, rect, inset=0):
    x, y = point
    return rect[0]+inset <= x <= rect[2]-inset and rect[1]+inset <= y <= rect[3]-inset


def rgb565(image):
    # No antialiasing: fewer colours and far less UART traffic on this TFT.
    pixels = array.array('H', (((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
                            for r, g, b in image.getdata()))
    if sys.byteorder != 'little':
        pixels.byteswap()
    return pixels.tobytes()


def rle(data):
    pixels = array.array('H')
    pixels.frombytes(data)
    if sys.byteorder != 'little':
        pixels.byteswap()
    out = bytearray()
    pos = 0
    while pos < len(pixels):
        color, end = pixels[pos], pos+1
        while end < len(pixels) and end-pos < 128 and pixels[end] == color:
            end += 1
        out.extend((0x80 | (end-pos-1), color & 255, color >> 8))
        pos = end
    return base64.b64encode(out).decode()


def rectangles(old, new):
    for y in range(0, HEIGHT, 8):
        bottom = min(y+8, HEIGHT)
        a, b = (old[y*WIDTH*2:bottom*WIDTH*2] if old else None), new[y*WIDTH*2:bottom*WIDTH*2]
        if a == b:
            continue
        left, right = WIDTH, -1
        if a is None:
            left, right = 0, WIDTH-1
        else:
            for row in range(bottom-y):
                start = row*WIDTH*2
                aa, bb = a[start:start+WIDTH*2], b[start:start+WIDTH*2]
                if aa == bb:
                    continue
                lo, hi = 0, WIDTH-1
                while aa[lo*2:lo*2+2] == bb[lo*2:lo*2+2]:
                    lo += 1
                while aa[hi*2:hi*2+2] == bb[hi*2:hi*2+2]:
                    hi -= 1
                left, right = min(left, lo), max(right, hi)
        cropped = b''.join(new[(row*WIDTH+left)*2:(row*WIDTH+right+1)*2] for row in range(y, bottom))
        yield [left, y, right, bottom-1, rle(cropped)]


class Renderer:
    def __init__(self, font):
        self.fonts = {s: ImageFont.truetype(font, s) for s in (11, 12, 13, 14, 15, 16, 18, 20, 22, 24)}

    def render(self, model):
        image = Image.new('RGB', (WIDTH, HEIGHT), BG)
        draw = ImageDraw.Draw(image)
        draw.fontmode = '1'
        buttons = []
        def text(x, y, value, size=14, fill=FG, width=None):
            value = str(value)
            font = self.fonts[size]
            if width:
                while value and draw.textlength(value, font=font) > width:
                    value = value[:-2] + '…' if len(value)>1 else ''
            draw.text((x, y), value, font=font, fill=fill, anchor='lt')
        def btn(rect, label, action, enabled=True, primary=False, danger=False, size=16):
            fill = RED if danger else BLUE if primary and enabled else SURFACE
            ink = '#ffffff' if danger else BLUE_INK if primary and enabled else FG if enabled else '#667489'
            draw.rounded_rectangle(rect, radius=6, fill=fill)
            lines = label.split('\n')
            for i, line in enumerate(lines):
                font = self.fonts[size]
                w = draw.textlength(line, font=font)
                text((rect[0]+rect[2]-w)/2, (rect[1]+rect[3])/2-len(lines)*(size+3)/2+i*(size+3), line, size, ink)
            if enabled:
                buttons.append((action, rect))
        s = model['status']
        connected = model['fresh'] and s.get('webhooks', {}).get('state') == 'ready'
        text(8, 9, 'GHOST 5 / KLIPPER', 12)
        text(312, 9, 'USB • подключён' if connected else 'Связь потеряна', 12, GREEN if connected else MUTED)
        for x, title, obj in [(8, 'СОПЛО', 'extruder'), (244, 'СТОЛ', 'heater_bed')]:
            draw.rounded_rectangle((x, 33, x+228, 89), radius=6, fill=SURFACE)
            text(x+10, 39, title, 11, MUTED)
            vals = s.get(obj, {})
            value = lambda key: str(round(vals.get(key, 0))) if connected else '—'
            text(x+10, 55, value('temperature'), 24)
            text(x+72, 63, '/ '+value('target')+' °C', 14, MUTED)
        p, remain = progress_info(s)
        names = {'printing': 'Печать', 'paused': 'Пауза', 'complete': 'Готово', 'cancelled': 'Отменено', 'error': 'Ошибка', 'standby': 'Нет печати'}
        state = s.get('print_stats', {}).get('state', 'standby')
        text(8, 96, (names.get(state, state)+' • %d%%' % round(p*100)) if connected else 'Нет данных о печати', 12)
        if remain is None:
            eta = 'Осталось —'
        else:
            minutes = max(1, round(remain/60))
            eta = 'Осталось ≈ %d ч %02d мин' % divmod(minutes, 60)
        text(270, 96, eta, 11, MUTED)
        draw.rounded_rectangle((8, 114, 472, 118), radius=2, fill=SURFACE)
        if p > 0 and connected:
            draw.rectangle((8, 114, 8+round(464*p), 118), fill=BLUE)

        cal = model['calibration']
        if cal is not None:
            text(106, 194, 'Калибровка сенсора', 20)
            text(100, 223, 'Нажмите и отпустите крестик', 14, MUTED)
            text(180, 246, 'Точка %d из 5' % (cal['index']+1), 16)
            text(85, 270, 'Нагрев и движение недоступны', 14, MUTED)
            x, y = cal['target']
            draw.ellipse((x-14, y-14, x+14, y+14), outline=BLUE, width=2)
            draw.line((x-20, y, x+20, y), fill=FG, width=2)
            draw.line((x, y-20, x, y+20), fill=FG, width=2)
            if model['notice']:
                text(12, 294, model['notice'], 11, MUTED, 456)
            return image, buttons

        btn((400, 126, 472, 172), 'Назад' if model['page'] != 'home' else 'Домой', 'home', size=13)
        btn(STOP, 'СТОП', 'stop', connected, danger=True, size=18)
        can = connected and idle(s) and not model['busy']
        hot = can and s.get('extruder', {}).get('can_extrude', False)
        page = model['page']
        if model['confirm']:
            text(12, 136, 'Подтвердите действие', 18)
            text(12, 167, model['confirm'][1], 15, width=376)
            text(12, 194, 'Печать должна быть остановлена', 12, MUTED)
            btn((8, 230, 196, 287), 'Отмена', 'dismiss')
            btn((202, 230, 392, 287), 'Подтвердить', 'confirm', can, primary=True, size=15)
        elif model['busy']:
            text(12, 140, 'Подача филамента', 20)
            text(12, 173, model['operation'], 16, width=376)
            text(12, 203, model['notice'], 13, MUTED, 376)
            btn((8, 250, 392, 308), 'Отменить подачу', 'cancel', size=18)
        elif page == 'home':
            title = 'Идёт печать' if state=='printing' else 'Пауза' if state=='paused' else 'Готов к работе' if connected else 'Нет подключения'
            text(10, 128, title, 18)
            text(10, 151, Path(s.get('print_stats', {}).get('filename', '')).name or model['notice'] or 'Выберите действие', 11, MUTED, 378)
            btn((8, 174, 196, 239), 'Загрузить', 'load-page', primary=True)
            btn((202, 174, 392, 239), 'Выгрузить', 'unload-page')
            btn((8, 245, 196, 312), 'Подать\nвручную', 'manual-page')
            btn((202, 245, 392, 312), 'Перезапуск', 'restart-page')
        elif page in ('filament', 'manual'):
            btn((8, 126, 196, 158), 'Смена филамента', 'load-page', primary=page=='filament', size=13)
            btn((202, 126, 392, 158), 'Ручная подача', 'manual-page', primary=page=='manual', size=13)
            if page=='filament':
                btn((8, 164, 50, 208), '−', 'temp-minus', can, size=22)
                text(65, 177, '%d °C' % model['target'], 20)
                btn((154, 164, 196, 208), '+', 'temp-plus', can, size=22)
                btn((202, 164, 314, 208), 'Нагреть', 'heat', can, primary=True, size=14)
                btn((320, 164, 392, 208), 'Выкл.', 'cool', can, size=14)
                btn((8, 214, 196, 259), 'Загрузить', 'load', hot)
                btn((202, 214, 392, 259), 'Выгрузить', 'unload', hot)
                btn((8, 265, 196, 312), 'Продавить 5 мм', 'purge', hot, size=14)
                message = model['notice'] or ('Идёт печать' if not can else 'Можно подавать' if hot else 'Нагрейте сопло')
                text(204, 270, message, 12, MUTED, 184)
                text(204, 291, 'Директ • шаги по 1 мм', 11, MUTED)
            else:
                for i, step in enumerate((1, 5, 10, 25)):
                    btn((8+i*98, 164, 98+i*98, 208), '%d мм' % step, 'step:%d' % step, can, primary=model['step']==step, size=15)
                btn((8, 214, 196, 259), '← Назад', 'retract', hot)
                btn((202, 214, 392, 259), 'Вперёд →', 'extrude', hot)
                btn((8, 265, 196, 312), '%g мм/с' % model['speed'], 'speed', can)
                btn((202, 265, 392, 312), 'Температура', 'load-page', size=14)
        elif page=='restart':
            text(12, 129, 'Перезапуск', 18)
            btn((8, 156, 392, 205), 'Klipper', 'restart-host', can)
            btn((8, 211, 392, 260), 'Плата принтера', 'restart-mcu', can)
            text(12, 275, model['notice'] or 'После СТОП восстановление — с ПК', 12, MUTED, 376)
            text(12, 295, 'Без перезагрузки Windows', 11, MUTED)
        return image, buttons


class Panel:
    TARGETS = [(35, 40), (445, 40), (445, 280), (35, 280), (240, 160)]

    def __init__(self, settings):
        self.settings = {**DEFAULTS, **settings}
        for name in ('load_mm', 'unload_mm', 'purge_mm'):
            if not 0 < float(self.settings[name]) <= 50:
                raise ValueError('Filament distance must be in (0, 50]')
        if not 170 <= self.settings['temperature'] <= 240 or not 1 <= self.settings['speed'] <= 5:
            raise ValueError('Temperature/speed outside panel limits')
        self.lock = threading.RLock()
        self.status, self.status_time = {}, 0.
        self.page, self.target, self.step, self.speed = 'home', self.settings['temperature'], 5, self.settings['speed']
        self.notice, self.confirm, self.busy, self.operation = '', None, False, ''
        self.revision, self.displayed_revision, self.buttons = 0, -1, []
        self.cancel, self.changed = threading.Event(), threading.Event()
        self.commands = queue.Queue(maxsize=1)
        self.raw_points, self.transform = [], None
        self.cal_index, self.cal_ready_at = 0, math.inf
        self.last_touch, self.read_count, self.read_errors = None, 0, 0
        self.latencies, self.last_heartbeat = [], 0.
        self.touch_rpc = RPC(self.settings['socket'], 1.5)
        path = Path(self.settings['calibration'])
        if path.exists():
            try:
                saved = json.loads(path.read_text())
                transform = saved['transform']
                if saved.get('version') != 1 or len(transform)!=2 or any(len(row)!=3 or any(not math.isfinite(float(v)) for v in row) for row in transform):
                    raise ValueError('Invalid calibration')
                self.transform = transform
            except Exception:
                logging.exception('Invalid calibration; using calibration screen')
        self.renderer = Renderer(self.settings['font'])

    def bump(self):
        self.revision += 1
        self.changed.set()

    def model(self):
        with self.lock:
            return {'status': copy.deepcopy(self.status), 'fresh': time.monotonic()-self.status_time<2,
                    'page': self.page, 'target': self.target, 'step': self.step, 'speed': self.speed,
                    'notice': self.notice, 'confirm': self.confirm, 'busy': self.busy,
                    'operation': self.operation, 'revision': self.revision,
                    'calibration': None if self.transform else {'index': self.cal_index, 'target': self.TARGETS[self.cal_index]}}

    def read_status(self, rpc):
        return rpc.call('objects/query', {'objects': OBJECTS})['status']

    def status_loop(self):
        rpc = RPC(self.settings['socket'])
        while True:
            try:
                status = self.read_status(rpc)
                with self.lock:
                    self.status, self.status_time = status, time.monotonic()
                    if self.busy and not idle(status):
                        self.cancel.set()
            except Exception as exc:
                self.cancel.set()
                logging.warning('Status unavailable: %s', exc)
            time.sleep(.5)

    def render_loop(self):
        rpc = RPC(self.settings['socket'], 3)
        old, epoch = None, None
        while True:
            try:
                model = self.model()
                if not model['fresh'] or model['status'].get('webhooks', {}).get('state') != 'ready':
                    old = None
                    time.sleep(.5)
                    continue
                image, buttons = self.renderer.render(model)
                frame = rgb565(image)
                changed_epoch = False
                rects = list(rectangles(old, frame))
                # One 8-row stripe per request: bounded work, yields to touch.
                for rect in rects or [None]:
                    result = rpc.call('fsmc_display/draw_rle', {'rects': [rect] if rect else []})
                    new_epoch = result.get('epoch')
                    changed_epoch |= epoch is not None and epoch != new_epoch
                    epoch = new_epoch
                old = None if changed_epoch else frame
                with self.lock:
                    if self.revision == model['revision'] and not changed_epoch:
                        self.buttons = buttons
                        self.displayed_revision = self.revision
                        if not self.transform and not math.isfinite(self.cal_ready_at):
                            self.cal_ready_at = time.monotonic()+.25
                self.heartbeat()
                self.changed.wait(.5)
                self.changed.clear()
            except Exception as exc:
                old = None
                with self.lock:
                    self.displayed_revision = -1
                logging.warning('Display unavailable: %s', exc)
                time.sleep(1)

    def heartbeat(self):
        if time.monotonic()-self.last_heartbeat < 2:
            return
        self.last_heartbeat = time.monotonic()
        model = self.model()
        with self.lock:
            report = {'updated_unix': time.time(), 'page': self.page, 'calibrated': self.transform is not None,
                      'calibration_point': None if self.transform else self.cal_index+1,
                      'last_raw_touch': self.last_touch, 'touch_reads': self.read_count,
                      'touch_errors': self.read_errors, 'busy': self.busy, 'notice': self.notice,
                      'ready': model['fresh'], 'displayed_revision': self.displayed_revision,
                      'revision': self.revision, 'touch_p95_ms': round(sorted(self.latencies)[int(.95*(len(self.latencies)-1))]*1000, 1) if self.latencies else None}
        atomic_json(self.settings['state_file'], report)

    def calibration_tap(self, samples):
        if len(samples) < 3 or time.monotonic() < self.cal_ready_at:
            return
        raw = tuple(statistics.median(p[i] for p in samples) for i in (0,1))
        with self.lock:
            self.raw_points.append(raw)
            logging.info('Calibration point %d raw=%s', self.cal_index+1, raw)
            if self.cal_index < 4:
                self.cal_index += 1
                self.cal_ready_at = math.inf
                self.bump()
                return
            try:
                transform, errors = fit_affine(self.raw_points, self.TARGETS)
                atomic_json(self.settings['calibration'], {'version':1, 'transform':transform,
                            'raw':self.raw_points, 'targets':self.TARGETS, 'errors_px':errors})
                self.transform = transform
                self.notice = 'Сенсор настроен'
                logging.info('Calibration saved, errors_px=%s', errors)
            except ValueError as exc:
                self.raw_points, self.cal_index = [], 0
                self.cal_ready_at = math.inf
                self.notice = 'Повторите: '+str(exc)
            self.bump()

    def touch_loop(self):
        samples, released, armed, stop_sent = [], 0, False, False
        start_revision = -1
        while True:
            try:
                started = time.monotonic()
                sample = self.touch_rpc.call('fsmc_touch/read')
                latency = time.monotonic()-started
                with self.lock:
                    self.read_count += 1
                    self.latencies = (self.latencies+[latency])[-500:]
                    self.last_touch = sample
                # Reject noisy coordinate samples without interpreting them as release.
                pressed = sample.get('pressed', False)
                stable = sample.get('x_spread', 4095)<100 and sample.get('y_spread', 4095)<100
                if pressed:
                    released = 0
                    if armed and stable:
                        if not samples:
                            start_revision = self.revision
                        samples.append((sample['x'], sample['y']))
                        samples = samples[-100:]
                        if self.transform and not stop_sent and len(samples)>=2:
                            point = map_touch(self.transform, *samples[-1])
                            if inside(point, STOP, 3) and inside(map_touch(self.transform, *samples[-2]), STOP, 3):
                                stop_sent = True
                                self.emergency()
                else:
                    released += 1
                    if released >= 2:
                        if samples and not stop_sent:
                            if not self.transform and start_revision == self.revision == self.displayed_revision:
                                self.calibration_tap(samples)
                            elif start_revision == self.revision == self.displayed_revision:
                                raw = tuple(statistics.median(p[i] for p in samples) for i in (0,1))
                                point = map_touch(self.transform, *raw)
                                if all(math.dist(map_touch(self.transform,*p), point)<18 for p in samples):
                                    for action, rect in self.buttons:
                                        if action!='stop' and inside(point, rect, 3):
                                            self.action(action)
                                            break
                        samples, stop_sent, armed = [], False, True
                time.sleep(max(.005, self.settings['touch_interval']-(time.monotonic()-started)))
            except Exception as exc:
                with self.lock:
                    self.read_errors += 1
                armed, samples, released = False, [], 0
                logging.warning('Touch unavailable: %s', exc)
                time.sleep(1)

    def emergency(self):
        self.cancel.set()
        # Never wait for ordinary command/drawing locks, completion, or confirmation.
        def send():
            rpc = RPC(self.settings['socket'], 1.5)
            try:
                rpc.call('emergency_stop')
                logging.warning('Emergency stop sent from physical touchscreen')
            except Exception:
                logging.exception('Emergency stop could not be delivered')
            finally:
                rpc.close()
        threading.Thread(target=send, daemon=True, name='panel-emergency').start()

    def action(self, action):
        with self.lock:
            if action=='cancel':
                self.cancel.set()
                self.notice = 'Останавливаю после текущего шага'
                self.bump()
                return
            if self.busy:
                return
            self.notice = ''
            if action=='home':
                self.page, self.confirm = 'home', None
            elif action in ('load-page','unload-page'):
                self.page, self.confirm = 'filament', None
            elif action=='manual-page':
                self.page, self.confirm = 'manual', None
            elif action=='restart-page':
                self.page, self.confirm = 'restart', None
            elif action=='dismiss':
                self.confirm = None
            elif action=='temp-plus':
                self.target = min(240, self.target+5)
            elif action=='temp-minus':
                self.target = max(170, self.target-5)
            elif action.startswith('step:'):
                self.step = int(action.split(':')[1])
            elif action=='speed':
                self.speed = {1:2, 2:5, 5:1}[self.speed]
            elif action in ('restart-host','restart-mcu','heat'):
                labels={'restart-host':'Перезапустить Klipper?', 'restart-mcu':'Сбросить плату принтера?', 'heat':'Нагреть сопло до %d °C?' % self.target}
                self.confirm = (action, labels[action])
            else:
                if action=='confirm':
                    if not self.confirm:
                        return
                    action=self.confirm[0]
                    self.confirm=None
                try:
                    self.commands.put_nowait((action,self.target,self.step,self.speed))
                    self.busy=True
                    self.operation='Подготовка'
                    self.cancel.clear()
                except queue.Full:
                    self.notice='Команда уже выполняется'
            self.bump()

    def execute(self, command, rpc):
        action, target, step, speed = command
        require_idle(self.read_status(rpc))
        script = lambda code: rpc.call('gcode/script', {'script':code})
        if action=='heat':
            script('FBG5_PANEL_HEAT TEMP=%d' % target)
            return 'Нагрев до %d °C' % target
        if action=='cool':
            script('FBG5_PANEL_HEAT TEMP=0')
            return 'Нагрев сопла выключен'
        if action in ('restart-host','restart-mcu'):
            require_idle(self.read_status(rpc), cold=True)
            # Macro rechecks print/heater state in the same G-code execution slot.
            script('FBG5_PANEL_RESTART MCU=%d' % (action=='restart-mcu'))
            return 'Перезапуск запрошен'
        amounts={'load':self.settings['load_mm'], 'unload':-self.settings['unload_mm'],
                 'purge':self.settings['purge_mm'], 'extrude':step, 'retract':-step}
        if action not in amounts:
            raise ValueError('Unknown action')
        distance=float(amounts[action])
        require_idle(self.read_status(rpc), hot=True)
        done=0.
        while abs(done-distance)>.001 and not self.cancel.is_set():
            require_idle(self.read_status(rpc), hot=True)
            amount=math.copysign(min(1.,abs(distance-done)),distance)
            with self.lock:
                self.operation='%s %g / %g мм' % ('Подача' if distance>0 else 'Отвод', abs(done), abs(distance))
                self.changed.set()
            # The macro enforces idle/hot/range limits again at execution time.
            script('FBG5_PANEL_EXTRUDE DIST=%.3f SPEED=%.3f' % (amount,speed))
            done+=amount
        return 'Подача отменена' if self.cancel.is_set() else 'Готово: %g мм' % abs(done)

    def command_loop(self):
        rpc=RPC(self.settings['socket'], 6)
        while True:
            command=self.commands.get()
            try:
                self.notice=self.execute(command,rpc)
                logging.info('Panel action completed: %s', command[0])
            except Exception as exc:
                # RESTORE only if a move macro ran and left its saved state behind.
                if command[0] in ('load','unload','purge','extrude','retract'):
                    try:
                        if self.read_status(rpc).get('webhooks',{}).get('state')=='ready':
                            rpc.call('gcode/script', {'script':'RESTORE_GCODE_STATE NAME=FBG5_PANEL MOVE=0'})
                    except Exception:
                        pass
                self.notice=str(exc)
                logging.exception('Panel action failed: %s', command[0])
            finally:
                with self.lock:
                    self.busy=False
                    self.bump()
                self.commands.task_done()

    def run(self):
        for name, target in [('status',self.status_loop),('touch',self.touch_loop),('commands',self.command_loop)]:
            threading.Thread(target=target, daemon=True, name='panel-'+name).start()
        self.render_loop()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings')
    parser.add_argument('--preview', help='Render a synthetic preview without accessing the printer')
    parser.add_argument('--probe', type=int, default=0, help='Read N raw touch samples, no writes')
    args=parser.parse_args()
    settings=json.loads(Path(args.settings).read_text()) if args.settings else {}
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(threadName)s %(message)s')
    panel=Panel(settings)
    if args.preview:
        panel.transform=[[1,0,0],[0,1,0]]
        panel.status={'webhooks':{'state':'ready'}, 'print_stats':{'state':'printing','filename':'vase-ribbed.gcode','print_duration':2800},
                      'virtual_sdcard':{'progress':.36}, 'extruder':{'temperature':210,'target':210},'heater_bed':{'temperature':60,'target':60}}
        panel.status_time=time.monotonic()
        panel.renderer.render(panel.model())[0].save(args.preview)
    elif args.probe:
        samples=[]
        rpc=RPC(panel.settings['socket'])
        for _ in range(args.probe):
            start=time.monotonic()
            sample=rpc.call('fsmc_touch/read')
            sample['latency_ms']=round((time.monotonic()-start)*1000,2)
            samples.append(sample)
            time.sleep(.04)
        print(json.dumps({'samples':samples},indent=2))
    else:
        panel.run()


if __name__=='__main__':
    main()
