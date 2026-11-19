"""Local Russian voice commands for a specific OpenCode session."""
import ctypes
import queue
import re
import threading
import tkinter as tk
from tkinter import ttk

import numpy as np
import sounddevice as sd
import webrtcvad
from faster_whisper import WhisperModel

MODEL = 'small'
RATE = 16000
FRAME = 480
WAKE = re.compile(r'\bприложени(?:е|я|ю|ем|и)\b[\s,.:;!?—-]*', re.IGNORECASE)


def foreground_is_openchamber():
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    hwnd = user32.GetForegroundWindow()
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel32.OpenProcess(0x1000, False, pid.value)
    if not handle:
        return False
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = ctypes.c_ulong(len(buf))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return False
        return buf.value.lower().endswith('\\openchamber.exe')
    finally:
        kernel32.CloseHandle(handle)


class VoiceApp:
    def __init__(self, root):
        self.root = root
        self.root.title('OpenChamber — голос')
        self.root.geometry('570x350')
        self.status = tk.StringVar(value='Выключено')
        self.pending = tk.StringVar(value='')
        self.stop = threading.Event()
        self.running = False
        self.model = None
        ttk.Label(root, text='Голосовой ввод OpenChamber', font=('Segoe UI', 16)).pack(pady=12)
        ttk.Label(root, textvariable=self.status, font=('Segoe UI', 11)).pack(pady=5)
        ttk.Label(root, text='Скажите «приложение», затем продиктуйте сообщение.\nПауза 4,5 секунды завершает сообщение.', justify='center').pack(pady=8)
        ttk.Label(root, textvariable=self.pending, wraplength=520, justify='left').pack(pady=8)
        self.button = ttk.Button(root, text='Включить микрофон', command=self.toggle)
        self.button.pack(pady=10)
        ttk.Label(root, text='Микрофон активен только пока включён. Закройте окно для остановки.', wraplength=500).pack(pady=5)
        root.protocol('WM_DELETE_WINDOW', self.close)

    def post(self, fn, *args):
        self.root.after(0, fn, *args)

    def toggle(self):
        if self.running:
            self.stop.set()
            self.status.set('Останавливается…')
            self.button.config(state='disabled')
            return
        self.running = True
        self.stop.clear()
        self.button.config(text='Выключить микрофон')
        self.status.set('Загружаю модель; первый запуск может занять несколько минут…')
        threading.Thread(target=self.run, daemon=True).start()

    def close(self):
        self.stop.set()
        self.root.destroy()

    def run(self):
        try:
            if self.model is None:
                self.model = WhisperModel(MODEL, device='cpu', compute_type='int8')
            if self.stop.is_set():
                return
            self.listen()
        except Exception as exc:
            self.post(self.status.set, 'Ошибка: ' + str(exc))
        finally:
            self.post(self.finished)

    def finished(self):
        self.running = False
        self.button.config(text='Включить микрофон', state='normal')
        if self.stop.is_set():
            self.status.set('Микрофон выключен')

    def listen(self):
        frames = queue.Queue(maxsize=2000)
        jobs = queue.Queue(maxsize=12)
        error = threading.Event()
        vad = webrtcvad.Vad(2)

        def callback(data, count, time_info, status):
            if status:
                error.set()
            try:
                frames.put_nowait(bytes(data))
            except queue.Full:
                error.set()

        def decode():
            active = False
            parts = []
            while not self.stop.is_set() and not error.is_set():
                try:
                    data = jobs.get(timeout=0.2)
                except queue.Empty:
                    continue
                if data is None:
                    return
                try:
                    audio = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768
                    segments, _ = self.model.transcribe(audio, language='ru', beam_size=3,
                                                        condition_on_previous_text=False)
                    text = ' '.join(s.text.strip() for s in segments).strip()
                    if not active:
                        match = WAKE.search(text)
                        if not match:
                            continue
                        active = True
                        text = text[match.end():].strip()
                        self.post(self.status.set, 'Команда распознана — слушаю сообщение')
                    if text:
                        parts.append(text)
                        self.post(self.pending.set, 'Текст: ' + ' '.join(parts))
                    # Every completed segment ends in 4.5 seconds silence. If the
                    # wake word was said alone, continue waiting for the message.
                    if active and parts:
                        text = ' '.join(parts)
                        if not foreground_is_openchamber():
                            raise RuntimeError('Не отправлено: откройте нужный чат OpenChamber и поставьте курсор в поле ввода')
                        self.post(self.paste_and_send, text)
                        parts = []
                        active = False
                except Exception as exc:
                    self.post(self.status.set, str(exc))
                    error.set()

        threading.Thread(target=decode, daemon=True).start()
        self.post(self.status.set, 'Слушаю. Скажите «приложение»')
        buf = []
        silence = 0
        voiced = 0
        preroll = []
        with sd.RawInputStream(samplerate=RATE, channels=1, dtype='int16',
                               blocksize=FRAME, callback=callback):
            while not self.stop.is_set() and not error.is_set():
                try:
                    frame = frames.get(timeout=0.2)
                except queue.Empty:
                    continue
                speech = vad.is_speech(frame, RATE)
                if not buf:
                    preroll = (preroll + [frame])[-10:]
                    if speech:
                        buf = preroll[:]
                        voiced = 1
                    continue
                buf.append(frame)
                voiced += int(speech)
                silence = 0 if speech else silence + 1
                if silence >= 150:
                    if voiced >= 10:
                        try:
                            jobs.put_nowait(b''.join(buf))
                        except queue.Full:
                            error.set()
                            self.post(self.status.set, 'Очередь переполнена; микрофон остановлен')
                    buf, preroll, silence, voiced = [], [], 0, 0
        if error.is_set():
            self.post(self.status.set, 'Остановлено из-за ошибки микрофона или отправки; проверьте последнее сообщение')

    def paste_and_send(self, text):
        if not foreground_is_openchamber():
            self.status.set('Не отправлено: окно OpenChamber больше не активно. Текст сохранён выше.')
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()
        if not foreground_is_openchamber():
            self.status.set('Не отправлено: окно сменилось. Текст скопирован.')
            return
        user32 = ctypes.windll.user32
        user32.keybd_event(0x11, 0, 0, 0)
        user32.keybd_event(0x56, 0, 0, 0)
        user32.keybd_event(0x56, 0, 2, 0)
        user32.keybd_event(0x11, 0, 2, 0)
        self.root.after(300, self.send_enter)

    def send_enter(self):
        if not foreground_is_openchamber():
            self.status.set('Текст вставлен, но окно сменилось: Enter не нажат.')
            return
        user32 = ctypes.windll.user32
        user32.keybd_event(0x0D, 0, 0, 0)
        user32.keybd_event(0x0D, 0, 2, 0)
        self.status.set('Enter нажат. Жду слово «приложение»')


if __name__ == '__main__':
    VoiceApp(tk.Tk()).root.mainloop()
