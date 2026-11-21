"""Interactive scheduled-task supervisor for the voice worker."""
import ctypes
from ctypes import wintypes
import os
import msvcrt
from pathlib import Path
import subprocess
import sys
import time

BASE = Path(__file__).resolve().parent
# Task Scheduler starts this file with pythonw.exe, so the worker simply reuses
# the interpreter that is already running: no second hardcoded path to fix on a
# machine with a different Python.
PYTHON = Path(sys.executable)
APP = BASE / 'wake.py'
HEARTBEAT = BASE / 'app.heartbeat'
SHOW = BASE / 'show.request'
GUARD_LOG = BASE / 'guard.log'
STDERR_LOG = BASE / 'stderr.log'
DUMP_PATH = BASE / 'worker-hang.dmp'
MUTEX_NAME = r'Local\OpenChamberVoiceGuard'
STARTUP_GRACE = 60
STALE_SECONDS = 120
POLL_SECONDS = 5
ERROR_ALREADY_EXISTS = 183


def log(message):
    with GUARD_LOG.open('a', encoding='utf-8') as handle:
        handle.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")} {message}\n')


def claim_mutex():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel.CloseHandle(handle)
        return None, kernel
    return handle, kernel


def heartbeat_age(start_wall, start_monotonic, now_wall, now_monotonic):
    """Only a file touched since this child started can count as its heartbeat."""
    elapsed = now_monotonic - start_monotonic
    try:
        modified = HEARTBEAT.stat().st_mtime
        if modified >= start_wall:
            return max(0.0, now_wall - modified)
    except OSError:
        pass
    return elapsed


def dump_child(child):
    """Best-effort Windows minidump of this child only; it may contain secrets."""
    try:
        dbghelp = ctypes.WinDLL('Dbghelp', use_last_error=True)
        dump = dbghelp.MiniDumpWriteDump
        dump.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.HANDLE,
                         wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        dump.restype = wintypes.BOOL
        with DUMP_PATH.open('wb') as output:
            ok = dump(wintypes.HANDLE(child._handle), child.pid,
                      wintypes.HANDLE(msvcrt.get_osfhandle(output.fileno())),
                      0, None, None, None)
            if not ok:
                raise ctypes.WinError(ctypes.get_last_error())
        log(f'worker minidump pid={child.pid} path={DUMP_PATH}')
    except Exception as exc:
        log(f'worker minidump failed pid={child.pid}: {exc!r}')


def supervise():
    delay = 3
    while True:
        try:
            started_wall = time.time()
            started = time.monotonic()
            # The task scheduler is the lifetime boundary; do not request an
            # unapproved job breakaway when running from an unknown parent.
            with STDERR_LOG.open('ab') as errors:
                child = subprocess.Popen([str(PYTHON), str(APP), '--listen'],
                                         cwd=str(BASE), stdout=errors, stderr=errors,
                                         creationflags=subprocess.NORMAL_PRIORITY_CLASS)
            log(f'worker started pid={child.pid} guard_pid={os.getpid()}')
            timed_out = False
            while child.poll() is None:
                now = time.monotonic()
                age = heartbeat_age(started_wall, started, time.time(), now)
                if now - started >= STARTUP_GRACE and age > STALE_SECONDS:
                    log(f'heartbeat stale pid={child.pid} age={age:.1f}s; dumping child')
                    dump_child(child)
                    timed_out = True
                    child.kill()
                    break
                time.sleep(POLL_SECONDS)
            code = child.wait()
            duration = time.monotonic() - started
            log(f'worker exited pid={child.pid} code={code} duration={duration:.1f}s '
                f'heartbeat_timeout={timed_out}')
            if code == 0 and not timed_out:
                log('worker quit normally; guard stopping')
                return 0
            if duration >= 300:
                delay = 3
        except Exception as exc:
            log(f'supervisor error: {exc!r}')
        log(f'retrying in {delay}s')
        time.sleep(delay)
        delay = min(delay * 2, 60)


def main():
    handle, kernel = claim_mutex()
    if handle is None:
        SHOW.write_text('show', encoding='utf-8')
        log('second guard requested show')
        return 0
    try:
        log(f'guard started pid={os.getpid()}')
        return supervise()
    finally:
        kernel.CloseHandle(handle)


if __name__ == '__main__':
    sys.exit(main())
