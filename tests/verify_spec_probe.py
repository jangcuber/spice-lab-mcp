# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jang cuber
"""Live check of v1.3.0 additions on the installed Capture/PSpice (bootstrapped Twin-T notch, uA741).

Stage 1 (default): prepare spec -> launch Capture build (Capture must be closed) -> wait for receipt.
Stage 2 (--verify BUILD_ID): verify native .net -> batch PSpice run -> open Probe -> traces/axis/marks.
Evidence goes to runtime/; screenshots are saved unmodified (window bounds fixed for legibility).
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import json
import sys
import time
from pathlib import Path

from core import Settings, Jobs
from capture_automation import CaptureAutomation
from desktop import Desktop
from probe_control import ProbeControl

NOTCH = {
    'parts': [
        {'ref': 'VIN', 'type': 'VAC', 'x': 0.6, 'y': 1.6, 'value': '1', 'nets': {'-': '0'}, 'expect': {'+': 'VIN'}},
        {'ref': 'RA', 'type': 'R', 'x': 1.7, 'y': 1.4, 'value': '27k', 'expect': {'1': 'VIN', '2': 'A'}},
        {'ref': 'RB', 'type': 'R', 'x': 2.5, 'y': 1.4, 'value': '27k', 'expect': {'1': 'A', '2': 'B'}},
        {'ref': 'C2C', 'type': 'C', 'x': 2.2, 'y': 1.8, 'rotate': 90, 'value': '200n', 'expect': {'1': 'G', '2': 'A'}},
        {'ref': 'RH', 'type': 'R', 'x': 2.2, 'y': 2.3, 'rotate': 90, 'value': '13.5k', 'expect': {'1': 'D', '2': 'G'}},
        {'ref': 'CA', 'type': 'C', 'x': 1.9, 'y': 2.7, 'value': '100n', 'expect': {'1': 'VIN', '2': 'D'}},
        {'ref': 'CB', 'type': 'C', 'x': 2.5, 'y': 2.7, 'value': '100n', 'expect': {'1': 'D', '2': 'B'}},
        {'ref': 'U1', 'type': 'UA741', 'x': 4.0, 'y': 1.4, 'nets': {'v+': 'VDD', 'v-': 'VSS'},
         'expect': {'+': 'B', '-': 'VOUT', 'out': 'VOUT'}},
        {'ref': 'R1', 'type': 'R', 'x': 5.2, 'y': 1.9, 'rotate': 90, 'value': '1k', 'expect': {'1': 'F', '2': 'VOUT'}},
        {'ref': 'R2', 'type': 'R', 'x': 5.2, 'y': 2.5, 'rotate': 90, 'value': '20k', 'nets': {'1': '0'}, 'expect': {'2': 'F'}},
        {'ref': 'U2', 'type': 'UA741', 'x': 4.0, 'y': 3.0, 'nets': {'v+': 'VDD', 'v-': 'VSS', '+': 'F', 'out': 'G'},
         'expect': {'-': 'G'}},
        {'ref': 'VDD', 'type': 'VDC', 'x': 6.2, 'y': 0.5, 'value': '15', 'nets': {'+': 'VDD', '-': '0'}},
        {'ref': 'VSS', 'type': 'VDC', 'x': 7.2, 'y': 0.5, 'value': '-15', 'nets': {'+': 'VSS', '-': '0'}},
    ],
    'wires': [[0.7, 1.5, 1.5, 1.5], [1.5, 1.5, 1.6, 1.5], [2.0, 1.5, 2.3, 1.5], [2.3, 1.5, 2.4, 1.5],
              [2.8, 1.5, 3.1, 1.5], [3.1, 1.5, 3.9, 1.5], [2.3, 1.5, 2.3, 1.7], [2.3, 2.0, 2.3, 2.2],
              [2.3, 2.6, 2.3, 2.8], [2.3, 2.2, 2.7, 2.2], [1.5, 1.5, 1.5, 2.8], [1.5, 2.8, 1.8, 2.8],
              [2.1, 2.8, 2.3, 2.8], [2.3, 2.8, 2.4, 2.8], [2.7, 2.8, 3.1, 2.8], [3.1, 2.8, 3.1, 1.5],
              [3.9, 1.9, 3.6, 1.9], [3.6, 1.9, 3.6, 2.5], [3.6, 2.5, 4.9, 2.5], [4.9, 2.5, 4.9, 1.7],
              [4.7, 1.7, 4.9, 1.7], [4.9, 1.7, 5.3, 1.7], [5.3, 1.7, 5.7, 1.7], [5.3, 1.7, 5.3, 1.8],
              [5.3, 2.2, 5.3, 2.4], [5.3, 2.4, 5.7, 2.4],
              [3.9, 3.5, 3.6, 3.5], [3.6, 3.5, 3.6, 3.9], [3.6, 3.9, 4.9, 3.9], [4.9, 3.9, 4.9, 3.3]],
    'aliases': [[1.0, 1.5, 'VIN'], [2.1, 1.5, 'A'], [3.5, 1.5, 'B'], [2.6, 2.2, 'G'], [2.2, 2.8, 'D'],
                [5.6, 1.7, 'VOUT'], [5.6, 2.4, 'F']],
    'analysis': ['.AC DEC 2000 1 10k'],
}


def stage1(cap):
    record = cap.prepare_spec('twin_t_notch', NOTCH)
    print('prepared', record['build_id'])
    cap.launch(record['build_id'])
    for _ in range(120):
        time.sleep(2)
        status = cap.status(record['build_id'])
        if status.get('blocking_dialogs'):
            print('BLOCKED BY', status['blocking_dialogs']); break
        if status['status'] in ('tcl_completed_needs_verification', 'tcl_failed'):
            break
    print(json.dumps({k: status[k] for k in ('build_id', 'status', 'netlists')}, indent=1))
    print(status.get('receipt', ''))


def stage2(cap, settings, build_id):
    evidence = cap.verify(build_id)
    print('connectivity_verified', evidence['connectivity_verified'], evidence['mismatches'])
    if not evidence['connectivity_verified']:
        print(json.dumps(evidence['devices'], indent=1))
        return
    run = cap.simulation_input(build_id)
    jobs = Jobs(settings)
    job = jobs.start(Path(run['path']))
    while (st := jobs.status(job['job_id']))['status'] == 'running':
        time.sleep(1)
    print('simulation', st['status'])
    dat = Path(st['data_file'])
    desk = Desktop(settings)
    probe = ProbeControl(desk)
    import subprocess
    subprocess.Popen([str(settings.apps['probe']), str(dat)], cwd=dat.parent)
    for _ in range(40):
        time.sleep(1)
        wins = [w for w in desk.list_windows() if dat.name in w['title'] and 'PSpice A/D' in w['title']]
        if wins:
            break
    hwnd = wins[0]['window_id']
    time.sleep(2)
    desk.set_layout(hwnd, 'bounds', 0, 0, 1800, 1050)
    probe.add_traces(hwnd, ['DB(V(VOUT))'])
    info, _ = probe.mark_points(hwnd, ['search forward level(max-3)', 'search forward min', 'search forward level(max-3)'])
    print(json.dumps(info['results'], indent=1))
    probe.set_axis(hwnd, 'x', '35', '100')
    info, _ = probe.set_axis(hwnd, 'y', '-70', '5')
    print(json.dumps(desk.save_screenshot(info['observation_id'], 'spec-notch-probe'), indent=1))


if __name__ == '__main__':
    settings = Settings()
    cap = CaptureAutomation(settings)
    if len(sys.argv) == 3 and sys.argv[1] == '--verify':
        stage2(cap, settings, sys.argv[2])
    else:
        stage1(cap)
