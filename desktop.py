# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jang cuber
"""Standalone Windows UI backend for this user-owned MCP.

Independent implementation (pywinauto/pywin32); not affiliated with any vendor's
computer-use helper. Inputs are limited to configured Cadence executables. The caller must
inspect each returned frame and obtain human consent for destructive operations.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import ctypes
from ctypes import wintypes
import io
import math
from pathlib import Path
import re
import threading
import time
import uuid

from core import Settings


KEYS = {'ENTER': '{ENTER}', 'ESC': '{ESC}', 'TAB': '{TAB}', 'SPACE': '{SPACE}',
        'LEFT': '{LEFT}', 'RIGHT': '{RIGHT}', 'UP': '{UP}', 'DOWN': '{DOWN}',
        'HOME': '{HOME}', 'END': '{END}', 'PAGEUP': '{PGUP}', 'PAGEDOWN': '{PGDN}',
        'BACKSPACE': '{BACKSPACE}', 'DELETE': '{DELETE}'}
KEYS.update({f'F{i}': '{F' + str(i) + '}' for i in range(1, 13)})
BLOCKED_KEYS = {'ALT+F4', 'ALT+TAB', 'CTRL+ESC', 'CTRL+ALT+DELETE', 'CTRL+SHIFT+ESC'}


def key_spec(chord: str, confirmed: bool = False) -> str:
    parts = chord.upper().split('+')
    if len(set(parts)) != len(parts) or any(p not in {'CTRL', 'ALT', 'SHIFT'} for p in parts[:-1]):
        raise ValueError('Use a single chord such as Ctrl+S, R, F11, Enter or Esc. Windows keys forbidden.')
    if chord.upper() in BLOCKED_KEYS:
        raise ValueError('System/window switching shortcuts are forbidden.')
    key = parts[-1]
    if key in ('DELETE', 'BACKSPACE') or (key == 'W' and 'CTRL' in parts):
        if not confirmed:
            raise ValueError('Deleting/closing requires fresh human confirmation; set confirmed_by_user only after consent.')
    if key not in KEYS and not re.fullmatch('[A-Z0-9]', key):
        raise ValueError('Unsupported key. No raw pywinauto key expressions.')
    return ''.join({'CTRL': '^', 'ALT': '%', 'SHIFT': '+'}[p] for p in parts[:-1]) + KEYS.get(key, key.lower())


def literal_spec(text: str) -> str:
    if not text or len(text) > 1000 or any(ord(c) < 32 for c in text):
        raise ValueError('Type 1-1000 literal characters. Use separate key calls for Enter/Tab.')
    # These exact Cadence library basenames are data, not Tcl commands. Do not
    # allow arbitrary paths/commands merely because they end in a file suffix.
    command_text = re.sub(r'(?i)(?<![\w.])(?:source\.olb|eval\.lib)(?![\w.])', 'library', text)
    if re.search(r'(?i)\b(?:powershell|pwsh|cmd\.exe|wscript|cscript|shellexecute|exec|source|eval)\b', command_text):
        raise ValueError('Script/terminal commands are not accepted by this UI tool.')
    # No clipboard overwrite; escape pywinauto expression characters one by one.
    return ''.join({'{': '{{}', '}': '{}}', '+': '{+}', '^': '{^}', '%': '{%}',
                    '~': '{~}', '(': '{(}', ')': '{)}'}.get(c, c) for c in text)


def numeric_value(value: str) -> str:
    if len(value) > 64 or not re.fullmatch(r'(?i)[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?(?:meg|[tgkmunpf])?', value):
        raise ValueError('Only one numeric PSpice value is accepted, for example 1, 100k or 2e-6.')
    number = re.sub(r'(?i)(?:meg|[tgkmunpf])$', '', value)
    if not math.isfinite(float(number)):
        raise ValueError('The numeric value must be finite.')
    return value


@dataclass
class Observation:
    token: str
    hwnd: int
    pid: int
    process_created: float
    rect: tuple
    focus: int
    created: float
    image: object
    numeric_fields: dict = field(default_factory=dict)


class Desktop:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.lock = threading.RLock()
        self.observations: dict[str, Observation] = {}
        self.listed: dict[int, tuple[int, float]] = {}
        self._loaded = False

    def _load(self):
        if self._loaded:
            return
        if not hasattr(ctypes, 'windll'):
            raise RuntimeError('Windows desktop is required.')
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
        import psutil
        import win32gui
        import win32process
        import win32con
        from pywinauto import Desktop as WinDesktop, keyboard, mouse
        from PIL import ImageGrab, ImageChops
        self.psutil, self.gui, self.process, self.con = psutil, win32gui, win32process, win32con
        self.WinDesktop, self.keyboard, self.mouse = WinDesktop, keyboard, mouse
        self.ImageGrab, self.ImageChops = ImageGrab, ImageChops
        self._loaded = True

    def _unlocked(self):
        self._load()
        u = ctypes.windll.user32
        u.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        u.OpenInputDesktop.restype = wintypes.HANDLE
        u.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        u.CloseDesktop.argtypes = [wintypes.HANDLE]
        handle = u.OpenInputDesktop(0, False, 1)
        if not handle:
            raise RuntimeError('Desktop is locked or unavailable. User must unlock it.')
        try:
            buf = ctypes.create_unicode_buffer(256)
            needed = wintypes.DWORD()
            ok = u.GetUserObjectInformationW(handle, 2, buf, ctypes.sizeof(buf), ctypes.byref(needed))
            if not ok or buf.value.lower() != 'default':
                raise RuntimeError('Input desktop is not Default. No input sent.')
        finally:
            u.CloseDesktop(handle)

    def _identity(self, hwnd: int) -> tuple[int, float]:
        self._load()
        if not self.gui.IsWindow(hwnd):
            raise ValueError('Window no longer exists. List windows again.')
        _, pid = self.process.GetWindowThreadProcessId(hwnd)
        proc = self.psutil.Process(pid)
        exe = Path(proc.exe()).resolve()
        if exe not in {p.resolve() for p in self.settings.apps.values()}:
            raise ValueError('Only the configured Capture and PSpice executables can be controlled.')
        return pid, proc.create_time()

    def list_windows(self) -> list[dict]:
        self._unlocked()
        result = []
        with self.lock:
            self.listed.clear()
            def collect(hwnd, _):
                if not self.gui.IsWindowVisible(hwnd):
                    return
                try:
                    identity = self._identity(hwnd)
                except (ValueError, self.psutil.Error):
                    return
                self.listed[hwnd] = identity
                result.append({'window_id': hwnd, 'title': self.gui.GetWindowText(hwnd),
                               'pid': identity[0], 'rect': list(self.gui.GetWindowRect(hwnd)),
                               'enabled': bool(self.gui.IsWindowEnabled(hwnd)),
                               'foreground': self.gui.GetForegroundWindow() == hwnd})
            self.gui.EnumWindows(collect, None)
        return result

    def _selected(self, hwnd):
        self._unlocked()
        identity = self._identity(hwnd)
        if self.listed.get(hwnd) != identity:
            raise ValueError('Select a current window_id from list_windows, not an invented/stale handle.')
        if not self.gui.IsWindowVisible(hwnd) or not self.gui.IsWindowEnabled(hwnd):
            raise ValueError('Window is hidden/disabled; inspect the modal from list_windows.')
        return identity

    def _thread_info(self, hwnd):
        tid, _ = self.process.GetWindowThreadProcessId(hwnd)
        class GUITHREADINFO(ctypes.Structure):
            _fields_ = [('cbSize', wintypes.DWORD), ('flags', wintypes.DWORD),
                        ('hwndActive', wintypes.HWND), ('hwndFocus', wintypes.HWND),
                        ('hwndCapture', wintypes.HWND), ('hwndMenuOwner', wintypes.HWND),
                        ('hwndMoveSize', wintypes.HWND), ('hwndCaret', wintypes.HWND),
                        ('rcCaret', wintypes.RECT)]
        info = GUITHREADINFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetGUIThreadInfo(tid, ctypes.byref(info)):
            return None
        return info

    def _focus(self, hwnd):
        info = self._thread_info(hwnd)
        return int(info.hwndFocus or 0) if info else 0

    def _belongs_to_target(self, hit, hwnd):
        root = self.gui.GetAncestor(hit, 2)
        if root == hwnd:
            return True
        # Native popup menus are separate top-level #32768 windows, not children
        # of the frame. Require the exact GUI thread/process AND active menu owner.
        if self.gui.GetClassName(root) != '#32768':
            return False
        if self.process.GetWindowThreadProcessId(root) != self.process.GetWindowThreadProcessId(hwnd):
            return False
        info = self._thread_info(hwnd)
        owner = int(info.hwndMenuOwner or 0) if info else 0
        return bool(owner and self.gui.GetAncestor(owner, 2) == hwnd)

    def _frame(self, hwnd):
        if self.gui.GetForegroundWindow() != hwnd or self.gui.IsIconic(hwnd):
            raise ValueError('Target must be foreground and restored. Call activate_window then inspect.')
        rect = self.gui.GetWindowRect(hwnd)
        if rect[2] <= rect[0] or rect[3] <= rect[1]:
            raise ValueError('Invalid window bounds.')
        # Verify representative points are not covered by another top-level app.
        for fx, fy in ((.1, .1), (.5, .5), (.9, .9), (.1, .9), (.9, .1)):
            point = (int(rect[0] + (rect[2]-rect[0])*fx), int(rect[1]+(rect[3]-rect[1])*fy))
            hit = self.gui.WindowFromPoint(point)
            if not (self._belongs_to_target(hit, hwnd) or self._owned_tooltip(hit, hwnd)):
                raise ValueError('Target is partly occluded/off-screen. Uncover the window and observe again.')
        return rect, self.ImageGrab.grab(bbox=rect, all_screens=True)

    def _owned_tooltip(self, hit, hwnd):
        # Capture's pin hints are owned top-level windows. They may appear in a
        # screenshot, but _point still refuses to CLICK through/onto the overlay.
        root = self.gui.GetAncestor(hit, 2)
        return (self.gui.GetClassName(root) == 'tooltips_class32'
                and self.gui.GetAncestor(root, 3) == hwnd
                and self.process.GetWindowThreadProcessId(root)
                == self.process.GetWindowThreadProcessId(hwnd))

    def observe(self, hwnd: int, include_controls=False) -> tuple[dict, bytes]:
        with self.lock:
            pid, birth = self._selected(hwnd)
            rect, img = self._frame(hwnd)
            focus = self._focus(hwnd)
            token = uuid.uuid4().hex
            self.observations.clear()  # One globally current frame prevents cross-window stale actions.
            self.observations[token] = Observation(token, hwnd, pid, birth, tuple(rect), focus, time.monotonic(), img)
            info = {'window_id': hwnd, 'title': self.gui.GetWindowText(hwnd), 'observation_id': token,
                    'width': img.width, 'height': img.height, 'coordinate_space': 'window-relative physical pixels',
                    'focus': {'handle': focus, 'class': self.gui.GetClassName(focus) if focus else '',
                              'text': self.gui.GetWindowText(focus)[:400] if focus else ''},
                    'expires_in_seconds': self.settings.ttl,
                    'notice': 'Inspect the actual image before ONE action. No coordinates from older images.'}
            if include_controls:
                controls = []
                def child(ch, _):
                    if len(controls) < 150 and self.gui.IsWindowVisible(ch):
                        rc = self.gui.GetWindowRect(ch)
                        controls.append({'class': self.gui.GetClassName(ch), 'text': self.gui.GetWindowText(ch)[:180],
                                         'rect': [rc[0]-rect[0], rc[1]-rect[1], rc[2]-rect[0], rc[3]-rect[1]]})
                self.gui.EnumChildWindows(hwnd, child, None)
                info['controls'] = controls
                fields = self._numeric_fields(hwnd)
                self.observations[token].numeric_fields = fields
                info['numeric_fields'] = [{'field_id': key, **value} for key, value in fields.items()]
            data = io.BytesIO()
            img.save(data, format='PNG')
            return info, data.getvalue()

    def _numeric_fields(self, hwnd):
        # Capture 17.2 embeds CEF in Simulation Settings: native focus is only a
        # Chrome pane. Its UIA Edit + ValuePattern still identifies exact fields.
        if not self.gui.GetWindowText(hwnd).startswith('Simulation Settings - '):
            return {}
        window = self.WinDesktop(backend='uia').window(handle=hwnd)
        origin = self.gui.GetWindowRect(hwnd)
        pid, _ = self._identity(hwnd)
        fields = {}
        for control in window.descendants(control_type='Edit'):
            try:
                element = control.element_info
                if (element.process_id != pid or element.element.CurrentIsPassword
                        or not control.is_enabled() or not control.is_visible()
                        or control.parent().element_info.control_type == 'ComboBox'
                        or control.iface_value.CurrentIsReadOnly):
                    continue
                rc = control.rectangle()
                bounds = (rc.left, rc.top, rc.right, rc.bottom)
                if not (origin[0] <= rc.left < rc.right <= origin[2]
                        and origin[1] <= rc.top < rc.bottom <= origin[3]):
                    continue
                fields[str(len(fields)+1)] = {
                    'runtime_id': list(element.runtime_id),
                    'rect': [rc.left-origin[0], rc.top-origin[1], rc.right-origin[0], rc.bottom-origin[1]],
                    'value': control.iface_value.CurrentValue,
                }
            except Exception:
                continue  # Unsupported accessibility fields remain unavailable.
        return fields

    def set_numeric_field(self, token, field_id, value):
        value = numeric_value(value)
        with self.lock:
            obs = self._consume(token)
            saved = obs.numeric_fields.get(field_id)
            if saved is None:
                raise ValueError('Observe with include_controls=True and choose a returned numeric_fields field_id.')
            current = self._numeric_fields(obs.hwnd)
            if current.get(field_id) != saved:
                raise ValueError('Numeric field changed. Observe again.')
            control = next((c for c in self.WinDesktop(backend='uia').window(handle=obs.hwnd)
                            .descendants(control_type='Edit')
                            if list(c.element_info.runtime_id) == saved['runtime_id']), None)
            if control is None:
                raise ValueError('Observed numeric field no longer exists.')
            # CEF's ValuePattern setter raises E_NOTIMPL and disrupts native
            # focus even on failure. Read it only. Require a user/model-observed
            # click and exact UIA Edit focus before sending finite numeric text.
            from pywinauto.uia_defines import IUIA
            focused = IUIA().iuia.GetFocusedElement()
            if (list(focused.GetRuntimeId()) != saved['runtime_id']
                    or not focused.CurrentHasKeyboardFocus
                    or focused.CurrentControlType != 50004
                    or focused.CurrentProcessId != obs.pid):
                return {'action_sent': False, 'text_sent': False, 'verification_required': True,
                        'message': 'Click the observed numeric field, observe include_controls=True, then retry. No text sent.'}, None
            self._frame(obs.hwnd)
            self.keyboard.send_keys('^a', vk_packet=False, pause=.01)
            self.keyboard.send_keys(literal_spec(value), with_spaces=True, vk_packet=False, pause=.01)
            time.sleep(.2)
            actual = control.iface_value.CurrentValue
            if actual != value:
                return {'action_sent': True, 'verification_required': True,
                        'message': 'Numeric input did not read back as requested; inspect before retrying.',
                        'requested': value, 'actual': actual}, None
            return self._after(obs)

    def activate(self, hwnd: int):
        with self.lock:
            self._selected(hwnd)
            self.observations.clear()
            self.WinDesktop(backend='win32').window(handle=hwnd).set_focus()
            time.sleep(.25)
            return self.observe(hwnd)

    def set_layout(self, hwnd: int, state: str, x=None, y=None, width=None, height=None):
        """Maximize/restore or place ONE listed window inside its monitor work area."""
        if state not in ('maximize', 'restore', 'bounds'):
            raise ValueError('state must be maximize, restore or bounds.')
        with self.lock:
            self._selected(hwnd)
            self.observations.clear()
            if state == 'maximize':
                self.gui.ShowWindow(hwnd, self.con.SW_MAXIMIZE)
            else:
                self.gui.ShowWindow(hwnd, self.con.SW_RESTORE)
            if state == 'bounds':
                values = (x, y, width, height)
                if any(isinstance(v, bool) or not isinstance(v, int) for v in values):
                    raise ValueError('bounds requires integer x, y, width, height in physical pixels.')
                import win32api
                monitor = win32api.GetMonitorInfo(win32api.MonitorFromWindow(hwnd, 2))['Work']
                if not (width >= 400 and height >= 300 and monitor[0] <= x and monitor[1] <= y
                        and x + width <= monitor[2] and y + height <= monitor[3]):
                    raise ValueError(f'Bounds must be >= 400x300 and inside the monitor work area {list(monitor)}.')
                self.gui.SetWindowPos(hwnd, 0, x, y, width, height, self.con.SWP_NOZORDER | self.con.SWP_SHOWWINDOW)
            self.WinDesktop(backend='win32').window(handle=hwnd).set_focus()
            time.sleep(.6)
            return self.observe(hwnd)

    def _consume(self, token: str) -> Observation:
        with self.lock:
            obs = self.observations.pop(token, None)
            if not obs or time.monotonic() - obs.created > self.settings.ttl:
                raise ValueError('Expired/consumed observation. Observe the window again.')
            pid, birth = self._selected(obs.hwnd)
            if (pid, birth) != (obs.pid, obs.process_created):
                raise ValueError('Window identity changed.')
            rect, image = self._frame(obs.hwnd)
            if tuple(rect) != obs.rect or self._focus(obs.hwnd) != obs.focus:
                raise ValueError('Window moved/resized or focus changed. Observe again.')
            difference = self.ImageChops.difference(image.convert('RGB'), obs.image.convert('RGB')).convert('L')
            hist = difference.histogram()
            changed = sum(hist[25:]) / (image.width * image.height)
            if changed > .003:
                raise ValueError('Screen changed since observation (>0.3% pixels). Observe again.')
            return obs

    def _point(self, obs, x, y):
        if not (isinstance(x, int) and isinstance(y, int) and
                0 <= x < obs.rect[2]-obs.rect[0] and 0 <= y < obs.rect[3]-obs.rect[1]):
            raise ValueError('Coordinates must be inside the observed window in physical pixels.')
        point = (obs.rect[0]+x, obs.rect[1]+y)
        hit = self.gui.WindowFromPoint(point)
        if not self._belongs_to_target(hit, obs.hwnd):
            raise ValueError('Point is covered by a non-target window. No input sent.')
        return point

    def _after(self, obs):
        time.sleep(.15)
        try:
            return self.observe(obs.hwnd)
        except Exception as exc:
            # Action might have opened a modal; never silently repeat it.
            return {'action_sent': True, 'verification_required': True,
                    'message': str(exc), 'next_step': 'list_windows, choose the modal, then observe. Do not retry input.'}, None

    def click(self, token, x, y, button='left', count=1):
        if button not in ('left', 'right', 'middle') or count not in (1, 2):
            raise ValueError('Invalid mouse button or count.')
        with self.lock:
            obs = self._consume(token)
            point = self._point(obs, x, y)
            action = self.mouse.double_click if count == 2 else self.mouse.click
            action(button=button, coords=point)
            return self._after(obs)

    def drag(self, token, x1, y1, x2, y2, button='left', duration=.5):
        if button not in ('left', 'right') or not math.isfinite(duration) or not .1 <= duration <= 2:
            raise ValueError('Drag duration must be 0.1-2 seconds; left or right button.')
        with self.lock:
            obs = self._consume(token)
            # Validate the whole path before pressing any button.
            points = [self._point(obs, round(x1+(x2-x1)*i/20), round(y1+(y2-y1)*i/20)) for i in range(21)]
            last_point = points[0]
            try:
                self.mouse.press(button=button, coords=points[0])
                for point in points[1:]:
                    if self.gui.GetForegroundWindow() != obs.hwnd:
                        raise RuntimeError('Focus changed during drag; mouse released, outcome unknown.')
                    self.mouse.move(coords=point)
                    last_point = point
                    time.sleep(duration/20)
            finally:
                self.mouse.release(button=button, coords=last_point)
            return self._after(obs)

    def scroll(self, token, x, y, steps):
        if not -10 <= steps <= 10 or not steps:
            raise ValueError('steps must be -10..-1 or 1..10.')
        with self.lock:
            obs = self._consume(token)
            self.mouse.scroll(coords=self._point(obs, x, y), wheel_dist=steps)
            return self._after(obs)

    def _safe_focus(self, obs, text_entry=False):
        focus = obs.focus
        if not focus or self.gui.GetAncestor(focus, 2) != obs.hwnd:
            raise ValueError('No verified focus inside the selected window.')
        current = focus
        labels = []
        while current:
            labels.append(self.gui.GetWindowText(current))
            if current == obs.hwnd:
                break
            current = self.gui.GetParent(current)
        if re.search(r'(?i)\b(tcl|command|console|script|password|login|license|activation)\b', ' '.join(labels)):
            raise ValueError('Console, script, authentication and license input are disabled.')
        if text_entry and not re.search('(?i)(edit|grid|spread)', self.gui.GetClassName(focus)):
            raise ValueError('Text input requires an observed Edit/Grid field, not a canvas or console.')

    def press_key(self, token, chord, confirmed=False):
        spec = key_spec(chord, confirmed)
        with self.lock:
            obs = self._consume(token)
            self._safe_focus(obs)
            # Capture's canvas accelerators require physical virtual-key events.
            # Unicode VK_PACKET remains appropriate only for literal text fields.
            self.keyboard.send_keys(spec, vk_packet=False, pause=.01)
            return self._after(obs)

    def type_text(self, token, text):
        spec = literal_spec(text)
        with self.lock:
            obs = self._consume(token)
            self._safe_focus(obs, text_entry=True)
            self.keyboard.send_keys(spec, with_spaces=True, vk_packet=True, pause=.005)
            return self._after(obs)

    def save_screenshot(self, token, label, crop=None):
        with self.lock:
            obs = self.observations.get(token)
            if not obs or time.monotonic() - obs.created > self.settings.ttl:
                raise ValueError('Screenshot expired; observe again.')
            image = obs.image
            if crop is not None:
                if (len(crop) != 4 or any(isinstance(v, bool) or not isinstance(v, int) for v in crop)
                        or not (0 <= crop[0] < crop[2] <= image.width and 0 <= crop[1] < crop[3] <= image.height)
                        or crop[2] - crop[0] < 50 or crop[3] - crop[1] < 50):
                    raise ValueError('crop must be [left, top, right, bottom] inside the observed image, at least 50 px each way.')
                image = image.crop(tuple(crop))
            folder = self.settings.new_dir('captures', label)
            path = folder / 'original.png'
            image.save(path, format='PNG')
            return {'path': str(path), 'original': crop is None, 'cropped_from': crop, 'window_id': obs.hwnd,
                    'width': image.width, 'height': image.height,
                    'notice': 'Unmodified pixels of the observed desktop window (optionally cropped); not a reconstructed graph.'}
