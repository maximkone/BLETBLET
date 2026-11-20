"""Small persistent UI for local Russian commands controlling OpenChamber dictation."""
from __future__ import annotations
import ctypes
from ctypes import wintypes
import faulthandler
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import sys
import time
import tkinter as tk
from tkinter import ttk

BASE = Path(__file__).resolve().parent
SETTINGS_PATH = BASE / 'settings.json'
SHOW_REQUEST = BASE / 'show.request'
HEARTBEAT = BASE / 'app.heartbeat'
LOG = logging.getLogger()
LOG.setLevel(logging.INFO)
handler = RotatingFileHandler(BASE / 'voice.log', maxBytes=1_000_000, backupCount=3, encoding='utf-8')
handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s pid=%(process)d %(message)s'))
LOG.addHandler(handler)

from command_rules import normalize
from voice_engine import Engine

DEFAULTS = {'settings_version': 3, 'start1': 'алло', 'start2': 'слышь',
            'silence_seconds': 3.0, 'start_listening': True, 'window_pos': [40, 60]}


class Settings:
    def __init__(self, path=SETTINGS_PATH):
        self.path = path
        self.data = dict(DEFAULTS)
        try:
            stored = json.loads(path.read_text(encoding='utf-8'))
            self.data.update({k: v for k, v in stored.items() if k in DEFAULTS})
            if float(stored.get('silence_seconds', 0) or 0) < 3:
                self.data['silence_seconds'] = 3.0
            self.data.update(self.validate(self.data))
        except FileNotFoundError:
            pass
        except Exception:
            LOG.exception('Invalid saved settings; using defaults')
            self.data = dict(DEFAULTS)

    @staticmethod
    def validate(values):
        data = {key: str(values[key]).strip().lower() for key in ('start1', 'start2')}
        if not (normalize(data['start1']) or normalize(data['start2'])):
            raise ValueError('Укажи хотя бы одно слово начала.')
        if any(len(p.split()) > 2 for p in data.values()):
            raise ValueError('В каждом поле — одно или два слова.')
        pause = float(str(values['silence_seconds']).replace(',', '.'))
        if not 3 <= pause <= 30:
            raise ValueError('Тишина до отправки: от 3 до 30 секунд.')
        data['silence_seconds'] = pause
        return data

    def save(self):
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.path)

    def __getitem__(self, key):
        return self.data[key]


def claim_instance():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    ctypes.set_last_error(0)
    handle = kernel.CreateMutexW(None, False, 'Local\\OpenChamberVoiceWorkerV2')
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle(handle)
        return None, kernel
    return handle, kernel


class App:
    def __init__(self, root, settings):
        self.root, self.settings = root, settings
        self.events = queue.Queue()
        self.engine = Engine(settings, self.emit)
        self.closed = False
        self.last_heartbeat = 0
        self.save_timer = None
        self.tray = None
        self.variables = {}
        root.title('Голос OpenChamber')
        root.overrideredirect(True)
        root.attributes('-topmost', True)
        root.protocol('WM_DELETE_WINDOW', self.hide)
        root.report_callback_exception = self.tk_error
        bar = ttk.Frame(root)
        bar.pack(fill='x')
        heading = ttk.Label(bar, text='Голос OpenChamber  ⠿', cursor='fleur')
        heading.pack(side='left', padx=8, pady=5)
        heading.bind('<Button-1>', self.drag_start)
        heading.bind('<B1-Motion>', self.drag_move)
        heading.bind('<ButtonRelease-1>', self.save_position)
        ttk.Button(bar, text='×', width=3, command=self.hide).pack(side='right')
        body = ttk.Frame(root, padding=(8, 2, 8, 8))
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='Начало — одно из двух:').pack(anchor='w')
        row = ttk.Frame(body)
        row.pack(fill='x', pady=(2, 5))
        for key in ('start1', 'start2'):
            if key == 'start2':
                ttk.Label(row, text='  или  ').pack(side='left')
            variable = tk.StringVar(value=settings[key])
            self.variables[key] = variable
            ttk.Entry(row, textvariable=variable, width=15).pack(side='left', fill='x', expand=True)
        row = ttk.Frame(body)
        row.pack(fill='x', pady=(7, 5))
        ttk.Label(row, text='Отправить после тишины:').pack(side='left')
        variable = tk.StringVar(value=str(settings['silence_seconds']))
        self.variables['silence_seconds'] = variable
        ttk.Entry(row, textvariable=variable, width=5).pack(side='left', padx=5)
        ttk.Label(row, text='сек.').pack(side='left')
        ttk.Label(body, text='Запись останавливается только паузой.', wraplength=318).pack(anchor='w')
        row = ttk.Frame(body)
        row.pack(fill='x', pady=7)
        self.button = ttk.Button(row, text='Включить', command=self.toggle)
        self.button.pack(side='left')
        ttk.Button(row, text='Сохранить', command=self.save_settings).pack(side='left', padx=5)
        self.status = tk.StringVar(value='Запускаю…')
        self.heard = tk.StringVar(value='')
        ttk.Label(body, textvariable=self.status, wraplength=318).pack(anchor='w')
        ttk.Label(body, textvariable=self.heard, wraplength=318).pack(anchor='w')
        for variable in self.variables.values():
            variable.trace_add('write', self.schedule_save)
        x, y = settings['window_pos']
        x = max(0, min(int(x), root.winfo_screenwidth() - 340))
        y = max(0, min(int(y), root.winfo_screenheight() - 270))
        root.geometry('340x250+%d+%d' % (x, y))
        self.make_tray()
        root.after(80, self.pump)
        if settings['start_listening'] or '--listen' in sys.argv:
            root.after(350, self.toggle)

    def emit(self, kind, value):
        self.events.put((kind, value))

    def tk_error(self, cls, value, trace):
        LOG.error('Tk callback error', exc_info=(cls, value, trace))

    def make_tray(self):
        import pystray
        from PIL import Image, ImageDraw
        image = Image.new('RGB', (64, 64), '#1953a4')
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((24, 9, 40, 37), radius=8, fill='white')
        draw.arc((16, 18, 48, 48), 0, 180, fill='white', width=5)
        draw.line((32, 47, 32, 55), fill='white', width=5)
        menu = pystray.Menu(
            pystray.MenuItem('Показать', lambda: self.emit('cmd', 'show'), default=True),
            pystray.MenuItem('Слушать / пауза', lambda: self.emit('cmd', 'toggle')),
            pystray.MenuItem('Выход', lambda: self.emit('cmd', 'quit')))
        self.tray = pystray.Icon('openchamber-voice', image, 'Голос OpenChamber', menu)

        def ready(icon):
            icon.visible = True
            LOG.info('Tray icon visible=True')
        self.tray.run_detached(setup=ready)

    def drag_start(self, e):
        self.drag = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()

    def drag_move(self, e):
        self.root.geometry('+%d+%d' % (e.x_root - self.drag[0], e.y_root - self.drag[1]))

    def save_position(self, _=None):
        self.settings.data['window_pos'] = [self.root.winfo_x(), self.root.winfo_y()]
        self.settings.save()

    def hide(self):
        if self.tray is None or not self.tray.visible:
            self.status.set('Трей недоступен — окно оставлено открытым.')
            return
        LOG.info('Window hidden by user; listening unchanged')
        self.save_position()
        self.root.withdraw()

    def show(self):
        self.root.deiconify()
        self.root.lift()

    def schedule_save(self, *_):
        if self.save_timer:
            self.root.after_cancel(self.save_timer)
        self.save_timer = self.root.after(700, self.save_settings)

    def save_settings(self):
        self.save_timer = None
        try:
            validated = Settings.validate({k: v.get() for k, v in self.variables.items()})
            self.settings.data.update(validated)
            self.settings.save()
            self.status.set('Сохранено. Команды применены.')
            return True
        except Exception as exc:
            self.status.set(str(exc))
            return False

    def toggle(self):
        if self.engine.stop_flag.is_set():
            if not self.save_settings():
                return
            self.engine.start()
            self.button.config(text='Пауза')
            self.status.set('Включаю микрофон…')
        else:
            self.engine.stop()
            self.button.config(text='Включить')
            self.status.set('Пауза. Микрофон выключается.')

    def pump(self):
        if self.closed:
            return
        try:
            for _ in range(50):
                kind, value = self.events.get_nowait()
                if kind == 'status':
                    self.status.set(value)
                elif kind == 'heard':
                    self.heard.set('Команда: ' + value[:90])
                elif kind == 'cmd':
                    {'show': self.show, 'toggle': self.toggle, 'quit': self.quit}[value]()
        except queue.Empty:
            pass
        except Exception:
            LOG.exception('UI event processing error')
        if self.closed:
            return
        if time.monotonic() - self.last_heartbeat >= 5:
            HEARTBEAT.write_text(json.dumps({'pid': os.getpid(), 'time': time.time(),
                                            'state': self.engine.state}), encoding='utf-8')
            self.last_heartbeat = time.monotonic()
            if SHOW_REQUEST.exists():
                SHOW_REQUEST.unlink(missing_ok=True)
                self.show()
        self.root.after(80, self.pump)

    def quit(self):
        LOG.info('Explicit quit requested')
        self.closed = True
        self.engine.shutdown()
        if self.tray:
            self.tray.stop()
        self.root.destroy()


def main():
    LOG.info('Starting worker')
    handle, kernel = claim_instance()
    if handle is None:
        SHOW_REQUEST.write_text('show', encoding='utf-8')
        return 0
    try:
        root = tk.Tk()
        app = App(root, Settings())
        try:
            root.mainloop()
        finally:
            app.engine.shutdown()
        LOG.info('Worker main loop ended normally')
        return 0
    finally:
        kernel.CloseHandle(handle)


if __name__ == '__main__':
    with (BASE / 'crash.log').open('a', encoding='utf-8') as crash:
        faulthandler.enable(crash)
        faulthandler.dump_traceback_later(60, file=crash)
        try:
            raise SystemExit(main())
        except Exception:
            LOG.exception('Worker crashed')
            raise
