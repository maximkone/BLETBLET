"""Foreground-only OpenChamber dictation UIA adapter. Timeout is caller-owned.

Usage: oc_uia.py inspect|start|send|cancel [--hwnd HANDLE]
The recording selectors are deliberately exact and fail closed. They must be
confirmed against the installed application's actual recording UI.
"""
import argparse
import ctypes
from ctypes import wintypes
import json
import hashlib
import os
import sys

def _find_exe():
    """Locate the OpenChamber Electron executable; override with OPENCHAMBER_EXE."""
    import glob
    candidates = [os.environ.get('OPENCHAMBER_EXE')]
    roots = [os.path.join(os.path.expandvars(r'%LOCALAPPDATA%'), 'Programs')]
    for root in roots:
        for name in ('@openchamberelectron', 'openchamber', 'OpenChamber'):
            candidates += glob.glob(os.path.join(root, name, 'OpenChamber.exe'))
    for path in candidates:
        if path and os.path.isfile(path):
            return os.path.normcase(os.path.normpath(path))
    return ''


EXE = _find_exe()
START = "Start dictation"
CANCEL = "Discard dictation"
INSERT = "Insert transcript"
SEND = "Insert and send"
NAMES = frozenset((START, CANCEL, INSERT, SEND))


def foreground():
    """Return (HWND, executable path), without changing focus."""
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user.GetForegroundWindow.restype = wintypes.HWND
    user.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    hwnd = user.GetForegroundWindow()
    if not hwnd:
        raise RuntimeError("no_foreground_window")
    pid = wintypes.DWORD()
    user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel.OpenProcess(0x1000, False, pid.value)
    if not handle:
        raise RuntimeError("foreground_process_unavailable")
    try:
        buf = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(buf))
        if not kernel.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(length)):
            raise RuntimeError("foreground_process_path_unavailable")
        return int(hwnd), buf.value
    finally:
        kernel.CloseHandle(handle)


def window_process_path(hwnd):
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    pid = wintypes.DWORD()
    user.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    handle = kernel.OpenProcess(0x1000, False, pid.value)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buf))
        if not kernel.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value
    finally:
        kernel.CloseHandle(handle)


def focus_openchamber():
    """Bring the largest normal OpenChamber window forward for a deliberate voice command."""
    user = ctypes.WinDLL("user32", use_last_error=True)
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    targets = []
    def cb(hwnd, _):
        if not user.IsWindowVisible(hwnd) or user.IsIconic(hwnd):
            return True
        style = user.GetWindowLongW(hwnd, -20)
        if style & 0x80:  # WS_EX_TOOLWINDOW
            return True
        if os.path.normcase(os.path.normpath(window_process_path(hwnd))) != os.path.normcase(os.path.normpath(EXE)):
            return True
        rect = wintypes.RECT()
        if not user.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        targets.append(((rect.right - rect.left) * (rect.bottom - rect.top), int(hwnd)))
        return True
    user.EnumWindows(WNDENUMPROC(cb), 0)
    if not targets:
        raise RuntimeError("openchamber_window_not_found")
    hwnd = max(targets)[1]
    user.ShowWindow(hwnd, 9)  # SW_RESTORE
    user.SwitchToThisWindow(hwnd, True)
    return hwnd


def checked_foreground(expected=None, get=foreground, focus=False):
    try:
        hwnd, path = get()
    except RuntimeError:
        if not focus:
            raise
        focus_openchamber()
        hwnd, path = get()
    if os.path.normcase(os.path.normpath(path)) != os.path.normcase(os.path.normpath(EXE)):
        if focus:
            focus_openchamber()
            hwnd, path = get()
        if os.path.normcase(os.path.normpath(path)) != os.path.normcase(os.path.normpath(EXE)):
            raise RuntimeError("foreground_not_openchamber")
    if expected is not None and hwnd != expected:
        raise RuntimeError("foreground_changed")
    return hwnd


def decide(command, groups):
    """Pure guard: groups map exact names to visible enabled elements.

    A recording group must be verified separately by the UIA adapter. Never
    guess from a generic send button or from a partial recording state.
    """
    counts = {name: len(groups.get(name, ())) for name in NAMES}
    recording = counts[START] == 0 and all(counts[name] == 1 for name in (CANCEL, INSERT, SEND))
    idle = counts[START] == 1 and not any(counts[n] for n in (CANCEL, INSERT, SEND))
    state = "recording" if recording else "idle" if idle else "unknown"
    if command == "inspect":
        return state, None
    if command == "start" and state == "recording":
        return state, "already_recording"
    if command == "start" and state == "idle":
        return state, START
    if command in ("send", "cancel") and state == "recording":
        return state, SEND if command == "send" else CANCEL
    return state, "unsafe_or_ambiguous_state"


class Adapter:
    def __init__(self, hwnd):
        import comtypes.client
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as uia
        self.uia = uia
        self.ui = comtypes.client.CreateObject(uia.CUIAutomation, interface=uia.IUIAutomation)
        self.root = self.ui.ElementFromHandle(hwnd)
        self.walker = self.ui.ControlViewWalker
        self.button_condition = self.ui.CreatePropertyCondition(uia.UIA_ControlTypePropertyId, uia.UIA_ButtonControlTypeId)

    def buttons(self, root):
        collection = root.FindAll(self.uia.TreeScope_Descendants, self.button_condition)
        result = {name: [] for name in NAMES}
        for i in range(collection.Length):
            item = collection.GetElement(i)
            name = item.CurrentName
            if name in result and item.CurrentIsEnabled and not item.CurrentIsOffscreen:
                result[name].append(item)
        return result

    def recording_container(self, groups):
        """Require all three uniquely in one nearby ancestor of explicit send."""
        if not all(len(groups[n]) == 1 for n in (CANCEL, INSERT, SEND)):
            return False
        ancestor = groups[SEND][0]
        for _ in range(4):
            ancestor = self.walker.GetParentElement(ancestor)
            if not ancestor or self.ui.CompareElements(ancestor, self.root):
                return False
            nearby = self.buttons(ancestor)
            if all(len(nearby[n]) == 1 and self.ui.CompareElements(nearby[n][0], groups[n][0]) for n in (CANCEL, INSERT, SEND)):
                return ancestor
        return None

    def signature(self, groups, container):
        # Runtime IDs are ephemeral, not a durable identity or a chat/session ID.
        ids = [list(container.GetRuntimeId())]
        ids.extend(list(groups[n][0].GetRuntimeId()) for n in (CANCEL, INSERT, SEND))
        return hashlib.sha256(json.dumps(ids, separators=(",", ":")).encode("ascii")).hexdigest()

    def invoke(self, item):
        import comtypes
        pattern = item.GetCurrentPattern(self.uia.UIA_InvokePatternId)
        if not pattern:
            raise RuntimeError("invoke_pattern_unavailable")
        pattern.QueryInterface(self.uia.IUIAutomationInvokePattern).Invoke()


def run(command, expected=None, adapter_type=Adapter, get=foreground, signature=None, focus=False):
    hwnd = checked_foreground(expected, get, focus)
    adapter = adapter_type(hwnd)
    groups = adapter.buttons(adapter.root)
    state, action = decide(command, groups)
    current_signature = None
    if state == "recording":
        container = adapter.recording_container(groups)
        if not container:
            state, action = "unknown", "unsafe_or_ambiguous_state" if command != "inspect" else None
        else:
            current_signature = adapter.signature(groups, container)
    result = {"state": state, "hwnd": hwnd,
              "names": {name: len(groups[name]) for name in sorted(NAMES) if groups[name]}}
    if current_signature:
        result["signature"] = current_signature
    if command == "inspect":
        return result
    if signature is not None and signature != current_signature:
        result["result"] = "signature_mismatch"
        return result
    if action in NAMES:
        # Re-query the entire recording overlay and its identity immediately before invoking.
        if action in (SEND, CANCEL):
            groups = adapter.buttons(adapter.root)
            if decide(command, groups)[0] != "recording":
                raise RuntimeError("recording_changed")
            container = adapter.recording_container(groups)
            if not container or adapter.signature(groups, container) != current_signature:
                raise RuntimeError("signature_mismatch")
        checked_foreground(hwnd, get, focus)
        item = groups[action][0]
        if item.CurrentName != action or not item.CurrentIsEnabled or item.CurrentIsOffscreen:
            raise RuntimeError("control_changed")
        adapter.invoke(item)
        result["result"] = "invoked"
    else:
        result["result"] = action
    return result


def self_test():
    import unittest

    class Item:
        CurrentIsEnabled = True
        CurrentIsOffscreen = False

        def __init__(self, name):
            self.CurrentName = name

    class Fake:
        names = ()
        container = True
        calls = []

        def __init__(self, hwnd):
            self.root = object()

        def buttons(self, root):
            return {n: [Item(x) for x in self.names if x == n] for n in NAMES}

        def recording_container(self, groups):
            return self.container

        def signature(self, groups, container):
            return "test-signature"

        def invoke(self, item):
            self.calls.append(item.CurrentName)

    class Tests(unittest.TestCase):
        def setUp(self):
            Fake.names, Fake.container, Fake.calls = (), True, []
            self.good = lambda: (123, EXE)

        def test_idle_and_start(self):
            Fake.names = (START,)
            self.assertEqual(run("inspect", adapter_type=Fake, get=self.good)["state"], "idle")
            self.assertEqual(run("start", adapter_type=Fake, get=self.good)["result"], "invoked")
            self.assertEqual(Fake.calls, [START])

        def test_recording_actions(self):
            Fake.names = (CANCEL, INSERT, SEND)
            self.assertEqual(run("start", adapter_type=Fake, get=self.good)["result"], "already_recording")
            self.assertEqual(Fake.calls, [])
            for command, name in (("send", SEND), ("cancel", CANCEL)):
                self.assertEqual(run(command, adapter_type=Fake, get=self.good)["result"], "invoked")
                self.assertEqual(Fake.calls.pop(), name)

        def test_reject_ambiguous_partial_and_wrong_container(self):
            for names, container in (((CANCEL, INSERT, SEND, SEND), True),
                                     ((CANCEL, SEND), True),
                                     ((START, CANCEL, INSERT, SEND), True),
                                     ((CANCEL, INSERT, SEND), False),
                                     ((START, START), True)):
                Fake.names, Fake.container = names, container
                for cmd in ("start", "send", "cancel"):
                    self.assertEqual(run(cmd, adapter_type=Fake, get=self.good)["result"], "unsafe_or_ambiguous_state")
            self.assertEqual(Fake.calls, [])

        def test_foreground_guard_and_recheck(self):
            Fake.names = (START,)
            with self.assertRaisesRegex(RuntimeError, "not_openchamber"):
                run("start", adapter_type=Fake, get=lambda: (123, "C:\\other.exe"))
            with self.assertRaisesRegex(RuntimeError, "foreground_changed"):
                run("start", 124, Fake, self.good)
            values = iter(((123, EXE), (456, EXE)))
            with self.assertRaisesRegex(RuntimeError, "foreground_changed"):
                run("start", adapter_type=Fake, get=lambda: next(values))
            self.assertEqual(Fake.calls, [])

        def test_signature_pin(self):
            Fake.names = (CANCEL, INSERT, SEND)
            self.assertEqual(run("inspect", adapter_type=Fake, get=self.good)["signature"], "test-signature")
            self.assertEqual(run("send", adapter_type=Fake, get=self.good, signature="wrong")["result"], "signature_mismatch")
            self.assertEqual(Fake.calls, [])
            self.assertEqual(run("send", adapter_type=Fake, get=self.good, signature="test-signature")["result"], "invoked")

    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=1).run(unittest.defaultTestLoader.loadTestsFromTestCase(Tests))
    return 0 if result.wasSuccessful() else 1


def main():
    if sys.argv[1:] == ["--self-test"]:
        return self_test()
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("inspect", "start", "send", "cancel"))
    parser.add_argument("--hwnd", type=lambda s: int(s, 0))
    parser.add_argument("--signature")
    parser.add_argument("--focus", action="store_true")
    args = parser.parse_args()
    try:
        output = run(args.command, args.hwnd, signature=args.signature, focus=args.focus)
        code = 0 if output.get("result") not in ("unsafe_or_ambiguous_state", "signature_mismatch") else 2
    except Exception as exc:
        output = {"state": "unknown", "hwnd": 0, "error": str(exc)}
        code = 2
    print(json.dumps(output, ensure_ascii=False), flush=True)
    return code


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
