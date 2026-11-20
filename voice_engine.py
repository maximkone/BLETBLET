"""Continuous audio + bounded UI Automation. Never send a delayed blind Enter."""
from __future__ import annotations
import json
import logging
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

from command_rules import command_for, grammar_phrases, normalize

BASE = Path(__file__).resolve().parent
LOG = logging.getLogger(__name__)
RATE, FRAME = 16000, 480


class UIAService:
    """One warm UI Automation worker reused for every query and action.

    Spawning a fresh process per command cost 0.6-1.3 s; a wedged UIA provider
    can also block forever, so a timed-out process is killed rather than reused.
    Mutating commands are never resent after a timeout.
    """

    def __init__(self):
        self._call_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._process = None
        self._pending = {}
        self._seq = 0

    def _ensure(self):
        if self._process is not None and self._process.poll() is None:
            return self._process
        self._process = subprocess.Popen(
            [sys.executable, str(BASE / 'oc_uia_server.py')],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            encoding='utf-8', errors='replace', bufsize=1,
            creationflags=0x08000000)
        threading.Thread(target=self._read, args=(self._process,), daemon=True).start()
        LOG.info('UIA service started pid=%s', self._process.pid)
        return self._process

    def _read(self, process):
        try:
            for line in process.stdout:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                with self._pending_lock:
                    waiter = self._pending.pop(message.get('id'), None)
                if waiter is not None:
                    waiter.put(message)
        except Exception:
            LOG.exception('UIA service reader stopped')
        finally:
            with self._pending_lock:
                if self._process is not process:
                    return
                self._process = None
                waiters = list(self._pending.values())
                self._pending.clear()
            for waiter in waiters:
                waiter.put({'ok': False, 'error': 'uia_service_exited'})

    def _kill(self):
        process, self._process = self._process, None
        if process is None:
            return
        try:
            process.kill()
            process.wait(timeout=3)
        except Exception:
            LOG.exception('UIA service did not stop cleanly')

    def call(self, request, timeout=4.0):
        with self._call_lock:
            for attempt in (1, 2):
                waiter = queue.Queue(maxsize=1)
                with self._pending_lock:
                    self._seq += 1
                    request_id = self._seq
                    self._pending[request_id] = waiter
                try:
                    process = self._ensure()
                    payload = dict(request, id=request_id)
                    process.stdin.write(json.dumps(payload, ensure_ascii=False) + '\n')
                    process.stdin.flush()
                except Exception:
                    LOG.warning('UIA service write failed attempt=%s', attempt)
                    with self._pending_lock:
                        self._pending.pop(request_id, None)
                    self._kill()
                    continue
                try:
                    return waiter.get(timeout=timeout)
                except queue.Empty:
                    LOG.warning('UIA %s timed out; restarting service', request.get('command'))
                    with self._pending_lock:
                        self._pending.pop(request_id, None)
                    self._kill()
                    # The command may still be running inside a provider and can
                    # fire later. Repeating start/send/cancel could act twice.
                    if request.get('command') != 'inspect':
                        break
            return {'state': 'unknown', 'ok': False, 'error': 'uia_service_unavailable'}

    def shutdown(self):
        with self._call_lock:
            process = self._process
            if process is not None and process.poll() is None:
                try:
                    process.stdin.write(json.dumps({'command': 'quit', 'id': -1}) + '\n')
                    process.stdin.flush()
                    process.wait(timeout=2)
                except Exception:
                    pass
            self._kill()


SERVICE = UIAService()


def uia_call(action, hwnd=None, signature=None, focus=False):
    return SERVICE.call({'command': action, 'hwnd': hwnd,
                         'signature': signature, 'focus': bool(focus)})


class Engine:
    def __init__(self, settings, emit):
        self.settings, self.emit = settings, emit
        self.shutdown_flag = threading.Event()
        self.stop_flag = threading.Event()
        self.stop_flag.set()
        self.thread = None
        self.control_thread = None
        self.model = None
        self.state = 'idle'
        self.observed = {}
        self.pending = False
        self.lock = threading.RLock()
        self.actions = queue.Queue(maxsize=2)
        self.epoch = 0
        self.last_trigger = 0.0
        self.last_frame = 0.0

    def config(self):
        return dict(self.settings.data)

    def start(self):
        with self.lock:
            self.epoch += 1
            self.stop_flag.clear()
            if not self.thread or not self.thread.is_alive():
                self.thread = threading.Thread(target=self.run, name='microphone', daemon=True)
                self.thread.start()
            if not self.control_thread or not self.control_thread.is_alive():
                self.control_thread = threading.Thread(target=self.controls, name='uia-control', daemon=True)
                self.control_thread.start()
        LOG.info('Listening requested')

    def stop(self):
        with self.lock:
            self.epoch += 1
            self.stop_flag.set()
            self.state = 'idle'
            self.observed = {}
        LOG.info('Paused by user; no more actions will be enqueued')

    def shutdown(self):
        self.stop()
        self.shutdown_flag.set()
        SERVICE.shutdown()

    def request(self, action, reason):
        with self.lock:
            if self.stop_flag.is_set() or self.pending or time.monotonic() - self.last_trigger < .35:
                return False
            self.pending = True
            self.last_trigger = time.monotonic()
            self.actions.put_nowait((action, reason, self.epoch, dict(self.observed)))
        LOG.info('Command recognized action=%s reason=%s', action, reason)
        return True

    def apply_observation(self, result):
        with self.lock:
            if self.stop_flag.is_set():
                return
            before = self.state
            self.observed = result
            self.state = 'dictating' if result.get('state') == 'recording' else 'idle'
        if self.state != before:
            self.emit('state', self.state)
            if self.state == 'dictating':
                pause = float(self.settings['silence_seconds'])
                self.emit('status', 'Идёт запись. Отправка после паузы %.1f с.' % pause)
            else:
                self.emit('status', 'Слушаю команды. Запись сейчас не обнаружена.')

    def controls(self):
        next_inspect = 0.0
        fast_until = 0.0
        while not self.shutdown_flag.is_set():
            try:
                item = self.actions.get(timeout=.1)
            except queue.Empty:
                item = None
            if item is not None:
                action, reason, epoch, observed = item
                started = time.monotonic()
                try:
                    if self.stop_flag.is_set() or epoch != self.epoch:
                        continue
                    # Manual recordings are valid: do not rely on a local toggle flag.
                    recording = observed.get('state') == 'recording'
                    result = uia_call(action,
                                      hwnd=observed.get('hwnd') if recording else None,
                                      signature=observed.get('signature') if action == 'send' else None,
                                      # Only a deliberate start may raise the app window;
                                      # send and cancel must never act on another window.
                                      focus=action == 'start')
                    LOG.info('UIA action=%s reason=%s ms=%d result=%s', action, reason,
                             (time.monotonic() - started) * 1000, result)
                    if result.get('result') in ('invoked', 'already_recording'):
                        if action == 'send':
                            self.emit('status', 'Отправлено кнопкой OpenChamber.')
                        else:
                            self.emit('status', 'Диктовка включена…')
                    elif action == 'start' and result.get('state') == 'recording':
                        self.emit('status', 'Диктовка уже шла.')
                    else:
                        self.emit('status', 'Не получилось: ' + str(result.get('error') or result.get('state')))
                    fast_until = time.monotonic() + 6
                    next_inspect = 0.0
                except Exception:
                    LOG.exception('UI action failed; no blind fallback')
                    self.emit('status', 'Не удалось проверить кнопку. Прослушивание продолжается.')
                finally:
                    with self.lock:
                        self.pending = False
                        self.last_trigger = time.monotonic()
            if not self.stop_flag.is_set() and time.monotonic() >= next_inspect:
                inspect_started = time.monotonic()
                try:
                    self.apply_observation(uia_call('inspect'))
                except Exception:
                    self.apply_observation({'state': 'unknown'})
                with self.lock:
                    recording = self.state == 'dictating'
                # Right after a command the recording overlay can appear within
                # a few hundred ms; polling fast there cuts the visible delay.
                fast = time.monotonic() < fast_until or recording
                next_inspect = max(next_inspect,
                                   inspect_started + (.3 if fast else 1.0))

    def run(self):
        while not self.shutdown_flag.is_set():
            if self.stop_flag.is_set():
                self.shutdown_flag.wait(.15)
                continue
            try:
                self.audio_session()
            except Exception as exc:
                LOG.exception('Audio session failed; will retry')
                self.emit('status', 'Ошибка микрофона; повтор через 4 секунды: ' + str(exc))
                self.shutdown_flag.wait(4)

    def ensure_model(self):
        if self.model is None:
            from vosk import Model, SetLogLevel
            SetLogLevel(-1)
            self.emit('status', 'Загружаю русские команды…')
            self.model = Model(str(BASE / 'vosk-model-small-ru-0.22'))
            LOG.info('Vosk model ready')
        return self.model

    def recognizer(self, model, config):
        from vosk import KaldiRecognizer
        phrases = grammar_phrases(config)
        # Fail visibly on words absent from the model, not silently ignore settings.
        for phrase in phrases[:-1]:
            for word in phrase.split():
                if model.vosk_model_find_word(word) < 0:
                    raise ValueError('Модель не знает слово «%s». Выбери другое слово.' % word)
        return KaldiRecognizer(model, RATE, json.dumps(phrases, ensure_ascii=False))

    def audio_session(self):
        LOG.info('Audio imports: sounddevice')
        import sounddevice as sd
        LOG.info('Audio imports: webrtcvad')
        import webrtcvad
        LOG.info('Audio imports complete; loading model')
        model = self.ensure_model()
        config = self.config()
        grammar = grammar_phrases(config)
        recognizer = self.recognizer(model, config)
        vad = webrtcvad.Vad(2)
        frames = queue.Queue(maxsize=80)
        dropped = threading.Event()

        def callback(data, count, timing, status):
            if status:
                dropped.set()
            try:
                frames.put_nowait(bytes(data))
            except queue.Full:
                dropped.set()
                try:
                    frames.get_nowait()
                except queue.Empty:
                    pass
                try:
                    frames.put_nowait(bytes(data))
                except queue.Full:
                    pass

        self.last_frame = time.monotonic()
        previous = ''
        recording_key = None
        speech_frames = silence_frames = 0
        self.emit('status', 'Слушаю три заданные команды. Текст диктует OpenChamber.')
        self.emit('state', 'listening')
        with sd.RawInputStream(samplerate=RATE, channels=1, dtype='int16', blocksize=FRAME, callback=callback):
            LOG.info('Microphone OPEN; commands grammar=%s', grammar)
            while not self.stop_flag.is_set() and not self.shutdown_flag.is_set():
                try:
                    frame = frames.get(timeout=.2)
                except queue.Empty:
                    if time.monotonic() - self.last_frame > 8:
                        raise RuntimeError('нет новых аудиокадров')
                    continue
                self.last_frame = time.monotonic()
                config = self.config()
                new_grammar = grammar_phrases(config)
                if new_grammar != grammar:
                    recognizer = self.recognizer(model, config)
                    grammar, previous = new_grammar, ''
                if dropped.is_set():
                    dropped.clear()
                    recognizer.Reset()
                    previous = ''
                    # Never execute stale queued audio after an overrun.
                    while not frames.empty():
                        try:
                            frames.get_nowait()
                        except queue.Empty:
                            break
                    continue
                with self.lock:
                    observed = dict(self.observed)
                    pending = self.pending
                key = (observed.get('hwnd'), observed.get('signature')) if observed.get('state') == 'recording' else None
                if key != recording_key:
                    recording_key = key
                    speech_frames = silence_frames = 0
                if key is not None and not pending:
                    if vad.is_speech(frame, RATE):
                        speech_frames += 1
                        silence_frames = 0
                    elif speech_frames >= 5:
                        silence_frames += 1
                    seconds = silence_frames * FRAME / RATE
                    if seconds >= float(config['silence_seconds']):
                        self.request('send', 'silence')
                        speech_frames = silence_frames = 0
                        recognizer.Reset()
                        previous = ''
                        continue
                final = recognizer.AcceptWaveform(frame)
                data = json.loads(recognizer.Result() if final else recognizer.PartialResult())
                text = normalize(data.get('text' if final else 'partial', ''))
                action = None
                if text and text != previous:
                    previous = text
                    self.emit('heard', text.replace('unk', '…'))
                    action = command_for(text, config, recording=key is not None, final=False)
                if final:
                    action = command_for(text, config, recording=key is not None, final=True) or action
                    previous = ''
                if action:
                    if self.request(action, 'keyword'):
                        recognizer.Reset()
                        previous = ''
                        speech_frames = silence_frames = 0
        LOG.info('Microphone CLOSED')
