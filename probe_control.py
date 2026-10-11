# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jang cuber
"""Deterministic Probe (pspice.exe) control through its native menu commands and dialog control IDs.

No screen coordinates, consoles or command files. Menu commands are located by their
visible label path at runtime (never trusted from a hard-coded table), and dialogs are
filled through their fixed Win32 control IDs, then confirmed with their own OK button.
Mark labels are drawn by Probe itself; values are read from the returned screenshot.
"""
from __future__ import annotations

import ctypes
import math
import re
import time

MF_CHECKED, MF_GRAYED = 0x8, 0x3
WM_COMMAND, WM_SETTEXT, BM_CLICK, BM_GETCHECK = 0x0111, 0x000C, 0x00F5, 0x00F0
WM_INITMENUPOPUP = 0x0117

# Probe trace expressions: node/device names, operators and Probe functions only.
TRACE = re.compile(r'[A-Za-z0-9_()+\-*/.,:\[\] ]{1,200}')
TRACE_WORDS = {'V', 'I', 'VDB', 'VP', 'VM', 'VR', 'VI', 'IDB', 'IP', 'IM', 'DB', 'P', 'M', 'R', 'IMG',
               'ABS', 'SQRT', 'LOG', 'LOG10', 'EXP', 'SIN', 'COS', 'TAN', 'ATAN', 'ARCTAN', 'D', 'S',
               'AVG', 'AVGX', 'RMS', 'MIN', 'MAX', 'SGN', 'G', 'PWR', 'W'}
# Probe cursor search grammar (PSpice User Guide, "Search commands").
SEARCH_WORDS = {'SEARCH', 'FORWARD', 'BACKWARD', 'LEVEL', 'XVALUE', 'MAX', 'MIN', 'PEAK', 'TROUGH',
                'SLOPE', 'POINT', 'FOR', 'REPEAT', 'P', 'N', 'POSITIVE', 'NEGATIVE'}
SEARCH = re.compile(r'(?i)search\s+(forward|backward)\b[A-Za-z0-9 ().,+\-#:%]{1,150}')
NUMBER = re.compile(r'(?i)[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?(?:meg|[tgkmunpf])?')
MENU_LABELS = {
    'add_trace': ('Trace', 'Add Trace...'),
    'cursor_display': ('Trace', 'Cursor', 'Display'),
    'cursor_search': ('Trace', 'Cursor', 'Search Commands...'),
    'axis_settings': ('Plot', 'Axis Settings...'),
    'mark': ('Plot', 'Label', 'Mark'),
}


def _words(text):
    return {w.upper() for w in re.findall(r'[A-Za-z_][A-Za-z0-9_]*', text)}


def trace_expression(text: str) -> str:
    text = text.strip()
    if not TRACE.fullmatch(text):
        raise ValueError('Trace expression may use node names, () [] + - * / . , : only (max 200 chars).')
    # Function names must be Probe functions; bare identifiers (node/alias names) are fine.
    for name in re.findall(r'([A-Za-z_][A-Za-z0-9_]*)\s*\(', text):
        if name.upper() not in TRACE_WORDS:
            raise ValueError(f'Unknown Probe function {name}().')
    return text


def search_command(text: str) -> str:
    text = ' '.join(text.split())
    if not SEARCH.fullmatch(text):
        raise ValueError('Use Probe search syntax, e.g. "search forward level(max-3)", "search forward min", '
                         '"search backward xvalue(1k)".')
    stripped = NUMBER.sub(' ', text)
    unknown = _words(stripped) - SEARCH_WORDS
    if unknown:
        raise ValueError(f'Unsupported search words: {sorted(unknown)}')
    return text


def axis_value(value) -> str:
    text = str(value).strip()
    if not NUMBER.fullmatch(text):
        raise ValueError('Axis limits must be numeric PSpice literals such as 20, 1k, -100 or 1e-3.')
    number = float(re.sub(r'(?i)(?:meg|[tgkmunpf])$', '', text))
    if not math.isfinite(number):
        raise ValueError('Axis limits must be finite.')
    return text


class ProbeControl:
    def __init__(self, desktop):
        self.d = desktop
        self.u = ctypes.windll.user32 if hasattr(ctypes, 'windll') else None

    # ---------------------------------------------------------------- helpers
    def _probe(self, hwnd):
        from pathlib import Path
        pid, _ = self.d._selected(hwnd)
        gui = self.d.gui
        if Path(self.d.psutil.Process(pid).exe()).resolve() != self.d.settings.apps['probe'].resolve():
            raise ValueError('Select a PSpice A/D (Probe) window, not Capture.')
        if gui.GetClassName(hwnd).startswith('#32770'):
            raise ValueError('Select the main PSpice A/D frame window, not a dialog.')
        if '.dat' not in gui.GetWindowText(hwnd).lower():
            raise ValueError('No Probe data (.dat) is active in this window.')
        return pid

    def _label(self, menu, index):
        buf = ctypes.create_unicode_buffer(256)
        self.u.GetMenuStringW(menu, index, buf, 256, 0x400)
        return buf.value.replace('&', '').split('\t')[0].strip()

    def _menu_item(self, hwnd, key):
        """Return (submenu handle, position in parent, command id) found by visible label path."""
        gui = self.d.gui
        menu, parent, pos = gui.GetMenu(hwnd), None, None
        path = MENU_LABELS[key]
        for depth, label in enumerate(path):
            for i in range(gui.GetMenuItemCount(menu)):
                if self._label(menu, i) == label:
                    if depth == len(path) - 1:
                        return menu, pos, gui.GetMenuItemID(menu, i)
                    parent, pos, menu = menu, i, gui.GetSubMenu(menu, i)
                    break
            else:
                raise RuntimeError(f'Menu item {" > ".join(path)} not found in this PSpice version.')

    def _state(self, hwnd, key):
        submenu, pos, cid = self._menu_item(hwnd, key)
        # MFC refreshes check/enable state only when the popup is initialised.
        self.d.gui.SendMessage(hwnd, WM_INITMENUPOPUP, submenu, pos or 0)
        flags = self.u.GetMenuState(submenu, cid, 0)
        return cid, bool(flags & MF_CHECKED), not (flags & MF_GRAYED)

    def _command(self, hwnd, key, post=False):
        cid, _, enabled = self._state(hwnd, key)
        if not enabled:
            raise RuntimeError(f'{" > ".join(MENU_LABELS[key])} is disabled (no trace or no cursor?).')
        (self.d.gui.PostMessage if post else self.d.gui.SendMessage)(hwnd, WM_COMMAND, cid, 0)

    def _dialog(self, pid, title, timeout=6.0):
        gui, proc = self.d.gui, self.d.process
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            found = []
            def collect(w, _):
                if (gui.IsWindowVisible(w) and gui.GetClassName(w) == '#32770'
                        and proc.GetWindowThreadProcessId(w)[1] == pid and title in gui.GetWindowText(w)):
                    found.append(w)
            gui.EnumWindows(collect, None)
            if found:
                return found[0]
            time.sleep(.1)
        raise RuntimeError(f'"{title}" dialog did not open.')

    def _message_boxes(self, pid, frame):
        """Collect and dismiss PSpice error message boxes raised by the last command."""
        gui, proc, texts = self.d.gui, self.d.process, []
        def collect(w, _):
            if (w != frame and gui.IsWindowVisible(w) and gui.GetClassName(w) == '#32770'
                    and proc.GetWindowThreadProcessId(w)[1] == pid and gui.GetWindowText(w) in ('PSpice', 'PSpice A/D', 'Probe')):
                parts = []
                gui.EnumChildWindows(w, lambda c, _: parts.append(gui.GetWindowText(c)) if gui.GetClassName(c) == 'Static' else None, None)
                texts.append(' '.join(p for p in parts if p).strip())
                ok = gui.GetDlgItem(w, 1) or gui.GetDlgItem(w, 2)
                if ok:
                    gui.SendMessage(ok, BM_CLICK, 0, 0)
        gui.EnumWindows(collect, None)
        return texts

    def _set(self, dialog, control_id, text):
        item = self.d.gui.GetDlgItem(dialog, control_id)
        self.d.gui.SendMessage(item, WM_SETTEXT, 0, text)
        return item

    def _click(self, dialog, control_id):
        self.d.gui.SendMessage(self.d.gui.GetDlgItem(dialog, control_id), BM_CLICK, 0, 0)

    def _finish(self, hwnd, report):
        self.d.WinDesktop(backend='win32').window(handle=hwnd).set_focus()
        time.sleep(.5)
        info, image = self.d.observe(hwnd)
        info.update(report)
        return info, image

    # ---------------------------------------------------------------- tools
    def add_traces(self, hwnd, expressions):
        if not 1 <= len(expressions) <= 8:
            raise ValueError('Add 1-8 trace expressions per call.')
        exprs = [trace_expression(e) for e in expressions]
        with self.d.lock:
            pid = self._probe(hwnd)
            self.d.observations.clear()
            errors = []
            for expr in exprs:
                self._command(hwnd, 'add_trace', post=True)
                dlg = self._dialog(pid, 'Add Traces')
                self._set(dlg, 301, expr)
                self._click(dlg, 1)
                time.sleep(.6)
                errors += self._message_boxes(pid, hwnd)
            return self._finish(hwnd, {'traces_requested': exprs, 'probe_errors': errors})

    def set_axis(self, hwnd, axis, minimum=None, maximum=None, scale=None):
        if axis not in ('x', 'y') or scale not in (None, 'linear', 'log'):
            raise ValueError('axis must be x or y; scale linear, log or omitted.')
        auto = minimum is None and maximum is None
        if not auto:
            if minimum is None or maximum is None:
                raise ValueError('Give both minimum and maximum, or neither for Auto Range.')
            minimum, maximum = axis_value(minimum), axis_value(maximum)
        with self.d.lock:
            pid = self._probe(hwnd)
            self.d.observations.clear()
            self._command(hwnd, 'axis_settings', post=True)
            sheet = self._dialog(pid, 'Axis Settings')
            # Tab pages are switched by the tab control's TCN_SELCHANGE; pywinauto marshals that
            # notification into the Probe process (a bare TCM_SETCURSEL would not switch pages).
            tab = self.d.WinDesktop(backend='win32').window(handle=sheet).child_window(
                class_name='SysTabControl32').wrapper_object()
            want = '&X Axis' if axis == 'x' else '&Y Axis'
            if tab.get_tab_text(0 if axis == 'x' else 1) != want:
                self._click(sheet, 2)
                raise RuntimeError('Unexpected Axis Settings tab layout; nothing changed.')
            tab.select(0 if axis == 'x' else 1)
            time.sleep(.3)
            pages = []
            self.d.gui.EnumChildWindows(sheet, lambda c, _: pages.append(c) if (
                self.d.gui.GetClassName(c) == '#32770' and self.d.gui.IsWindowVisible(c)) else None, None)
            if len(pages) != 1:
                self._click(sheet, 2)
                raise RuntimeError('Could not identify the active Axis Settings page; nothing changed.')
            page = pages[0]
            self._click(page, 1004 if auto else 1005)
            if not auto:
                self._set(page, 1029, minimum)
                self._set(page, 1030, maximum)
            if scale:
                self._click(page, 1006 if scale == 'linear' else 1007)
            self._click(sheet, 1)
            time.sleep(.6)
            errors = self._message_boxes(pid, hwnd)
            if errors:
                try:
                    self._click(self._dialog(pid, 'Axis Settings', .5), 2)
                except RuntimeError:
                    pass
            return self._finish(hwnd, {'axis': axis, 'range': 'auto' if auto else [minimum, maximum],
                                       'scale': scale or 'unchanged', 'probe_errors': errors})

    def mark_points(self, hwnd, searches, mark=True, cursor=1, auto_x_first=True, hide_cursor=True):
        if not 1 <= len(searches) <= 12 or cursor not in (1, 2):
            raise ValueError('Give 1-12 search commands; cursor must be 1 or 2.')
        commands = [search_command(s) for s in searches]
        if auto_x_first:
            # Probe evaluates max/min/level only over the DISPLAYED x range.
            self.set_axis(hwnd, 'x')
        with self.d.lock:
            pid = self._probe(hwnd)
            self.d.observations.clear()
            _, shown, enabled = self._state(hwnd, 'cursor_display')
            if not enabled:
                raise RuntimeError('Cursor is unavailable: add a trace first.')
            if not shown:
                self._command(hwnd, 'cursor_display')
                time.sleep(.4)
            results = []
            for command in commands:
                self._command(hwnd, 'cursor_search', post=True)
                dlg = self._dialog(pid, 'Search Command')
                self._set(dlg, 301, command)
                self._click(dlg, 100 + cursor)
                self._click(dlg, 1)
                time.sleep(.5)
                errors = self._message_boxes(pid, hwnd)
                if errors:
                    results.append({'search': command, 'found': False, 'probe_errors': errors})
                    continue
                if mark:
                    self._command(hwnd, 'mark')
                    time.sleep(.3)
                results.append({'search': command, 'found': True, 'marked': mark})
            if hide_cursor:
                _, shown, _ = self._state(hwnd, 'cursor_display')
                if shown:
                    self._command(hwnd, 'cursor_display')
            return self._finish(hwnd, {
                'results': results,
                'values': 'Read the (x, y) mark labels from the returned screenshot; the cursor grid is not machine-readable. '
                          'Cross-check numbers with .PRINT data (measure_samples) when precision matters.',
                'note': 'Marks stay attached to data points; change axes afterwards with probe_set_axis if needed.'})
