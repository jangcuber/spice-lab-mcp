# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jang cuber
"""File safety, PSpice execution and literal .PRINT data; no UI or MCP imports."""
from __future__ import annotations

import atexit
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import uuid

BASE = Path(__file__).resolve().parent
SUFFIXES = {'.cir', '.out', '.dat', '.prb', '.opj', '.dsn', '.sim', '.tcl', '.csv', '.png', '.txt', '.net'}
MAX_FILE = 25 * 1024 * 1024


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_text(path: Path) -> str:
    if path.stat().st_size > MAX_FILE:
        raise ValueError('File exceeds 25 MiB limit.')
    data = path.read_bytes()
    for enc in ('utf-8-sig', 'cp949', 'cp1252'):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode('utf-8', errors='replace')


def detect_cadence_bin() -> Path | None:
    """CADENCE_BIN env var, else CDSROOT, else the newest C:/Cadence/SPB_*/tools/bin that has pspice.exe."""
    candidates = []
    if os.environ.get('CADENCE_BIN'):
        candidates.append(Path(os.environ['CADENCE_BIN']))
    if os.environ.get('CDSROOT'):
        candidates.append(Path(os.environ['CDSROOT']) / 'tools' / 'bin')
    for drive in ('C:/', 'D:/'):
        candidates += sorted(Path(drive).glob('Cadence/SPB_*/tools/bin'), reverse=True)
    return next((c for c in candidates if (c / 'pspice.exe').is_file()), None)


def default_config() -> dict:
    """Used when config.json is absent. Copy config.example.json to config.json to customise."""
    cadence = detect_cadence_bin()
    if cadence is None:
        raise RuntimeError('Cadence PSpice not found. Set CADENCE_BIN (.../SPB_xx.x/tools/bin) or create '
                           'config.json from config.example.json.')
    return {'cadence_bin': str(cadence), 'model_library': str(cadence.parent / 'pspice' / 'library' / 'eval.lib'),
            'read_roots': [str(Path.home() / 'Documents')], 'workspace': str(BASE / 'runtime')}


class Settings:
    def __init__(self, path: Path | None = None):
        source = path or BASE / 'config.json'
        cfg = json.loads(source.read_text(encoding='utf-8')) if source.is_file() else default_config()
        self.bin = Path(cfg['cadence_bin']).resolve()
        self.library = Path(cfg['model_library']).resolve()
        workspace = Path(cfg['workspace'])
        self.workspace = (workspace if workspace.is_absolute() else BASE / workspace).resolve()
        self.roots = [Path(p).resolve() for p in cfg['read_roots']] + [self.workspace]
        self.ttl = min(60, max(5, int(cfg.get('observation_ttl_seconds', 30))))
        self.timeout = min(600, max(10, int(cfg.get('simulation_timeout_seconds', 120))))
        self.apps = {'capture': self.bin / 'capture.exe', 'probe': self.bin / 'pspice.exe'}

    def readable(self, value: str, suffixes: set[str] | None = None) -> Path:
        # Reject network/device paths and NTFS alternate data streams.
        if value.startswith(('\\\\', '//')) or ':' in value[2:]:
            raise ValueError('Network/device/alternate stream paths are not allowed.')
        p = Path(value).resolve(strict=True)
        if not any(p.is_relative_to(root) for root in self.roots):
            raise ValueError('Path is outside configured read_roots.')
        if not p.is_file() or p.suffix.lower() not in (suffixes or SUFFIXES):
            raise ValueError('Unsupported file type.')
        return p

    def new_dir(self, kind: str, label: str) -> Path:
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,60}', label):
            raise ValueError('label must be 1-60 ASCII letters, numbers, - or _.')
        parent = (self.workspace / kind).resolve()
        if not parent.is_relative_to(self.workspace):
            raise ValueError('Invalid output root.')
        parent.mkdir(parents=True, exist_ok=True)
        p = parent / (time.strftime('%Y%m%d-%H%M%S') + '-' + label + '-' + uuid.uuid4().hex[:10])
        p.mkdir()
        return p


def validate_netlist(text: str, settings: Settings) -> None:
    if not text.isascii() or len(text) > 250_000 or '\x00' in text:
        raise ValueError('Netlist must be ASCII text, at most 250,000 characters.')
    lines = [s.strip() for s in text.splitlines() if s.strip() and not s.lstrip().startswith('*')]
    if not lines or lines[-1].upper() != '.END':
        raise ValueError('Use a * comment title and terminate with .END.')
    if not text.lstrip().startswith('*'):
        raise ValueError('First line must be a * title comment (PSpice title line).')
    if not any(re.match(r'^\.(AC|DC|TRAN|OP)\b', s, re.I) for s in lines):
        raise ValueError('Missing .AC, .DC, .TRAN or .OP analysis.')
    allowed = {'AC', 'DC', 'TRAN', 'OP', 'PRINT', 'PROBE', 'PARAM', 'STEP', 'MODEL',
               'OPTIONS', 'TEMP', 'IC', 'NODESET', 'FUNC', 'END', 'LIB'}
    for s in lines:
        if not s.startswith('.'):
            continue
        directive = s.split()[0][1:].upper()
        if directive not in allowed:
            raise ValueError(f'.{directive} is not enabled. No .INC, file output, scripts or external code.')
        if directive == 'LIB':
            match = re.fullmatch(r'\.LIB\s+"([^"]+)"', s, re.I)
            if not match or Path(match[1]).resolve() != settings.library:
                raise ValueError('Only the exact configured eval.lib is permitted, in double quotes.')
            if not settings.library.is_file():
                raise ValueError('Configured model library is missing.')


def parse_print_tables(text: str) -> list[dict]:
    """Preserve separate sweeps, column groups and sample precision; never interpolate."""
    tables: list[dict] = []
    current = None
    header = None
    header_line = 0
    number = re.compile(r'^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][+-]?\d+)?$')
    for lineno, line in enumerate(text.splitlines(), 1):
        tokens = line.strip().split()
        # PSpice's fixed-width header can join long named-node columns:
        # FREQ VDB(OUT_MCP)VP(OUT_MCP). Split complete expressions, not spaces.
        head = re.fullmatch(r'\s*(FREQ|TIME|[VI][A-Z0-9_]+)\s+(.+?)\s*', line, re.I)
        if head:
            expression = r'[VI][A-Z]*\([^()\r\n]+\)'
            if re.fullmatch(r'(?:' + expression + r'\s*)+', head[2], re.I):
                header = [head[1]] + re.findall(expression, head[2], re.I)
                header_line = lineno
                current = None
                continue
        if header and len(tokens) == len(header) and all(number.fullmatch(t) for t in tokens):
            row = [float(t.replace('D', 'E').replace('d', 'e')) for t in tokens]
            if not all(math.isfinite(x) for x in row):
                raise ValueError(f'Non-finite .PRINT row at line {lineno}.')
            if current is None:
                # Repeated same header with strictly increasing X is pagination.
                if tables and tables[-1]['columns'] == header and row[0] >= tables[-1]['rows'][-1][0]:
                    current = tables[-1]
                else:
                    current = {'table_index': len(tables), 'columns': header, 'rows': [],
                               'header_line': header_line, 'row_lines': []}
                    tables.append(current)
            elif row[0] < current['rows'][-1][0]:
                current = {'table_index': len(tables), 'columns': header, 'rows': [],
                           'header_line': header_line, 'row_lines': []}
                tables.append(current)
            current['rows'].append(row)
            current['row_lines'].append(lineno)
        elif tokens and header and current is not None:
            # Page footers/end summaries are not data. A new header is required.
            header = None
            current = None
    return tables


def inspect_output(path: Path) -> dict:
    text = read_text(path)
    errors = [s.strip() for s in text.splitlines()
              if re.search(r'(?:\bERROR\b|\bFATAL\b|\bABORTED\b)', s, re.I)
              and not s.lstrip().startswith('*')]
    tables = parse_print_tables(text)
    return {'path': str(path), 'sha256': digest(path.read_bytes()),
            'job_concluded': bool(re.search(r'\bJOB CONCLUDED\b', text)),
            'diagnostics': errors[:60],
            'tables': [{'table_index': t['table_index'], 'columns': t['columns'],
                        'row_count': len(t['rows']), 'header_line': t['header_line'],
                        'x_first': t['rows'][0][0], 'x_last': t['rows'][-1][0]} for t in tables],
            'measurement_basis': 'Literal PSpice .PRINT samples; rounded output precision, not GUI cursors.'}


def measured_table(path: Path, index: int) -> dict:
    evidence = inspect_output(path)
    if not evidence['job_concluded'] or evidence['diagnostics']:
        raise ValueError('Cannot measure failed or incomplete output; inspect_results for diagnostics.')
    tables = parse_print_tables(read_text(path))
    if not 0 <= index < len(tables):
        raise ValueError('table_index out of range; inspect_output first.')
    return tables[index]


def statistics(table: dict, column: str, start: float | None = None, stop: float | None = None) -> dict:
    if any(v is not None and (isinstance(v, bool) or not math.isfinite(v)) for v in (start, stop)):
        raise ValueError('Interval bounds must be finite numbers.')
    if start is not None and stop is not None and start > stop:
        raise ValueError('start must not exceed stop.')
    if column not in table['columns'][1:]:
        raise ValueError('Choose a dependent column from inspect_output.')
    idx = table['columns'].index(column)
    rows = [r for r in table['rows'] if (start is None or r[0] >= start) and (stop is None or r[0] <= stop)]
    if not rows:
        raise ValueError('No printed samples in the requested interval.')
    low = min(rows, key=lambda r: r[idx])
    high = max(rows, key=lambda r: r[idx])
    return {'column': column, 'sample_count': len(rows), 'x_first': rows[0][0], 'x_last': rows[-1][0],
            'minimum': low[idx], 'minimum_x': low[0], 'maximum': high[idx], 'maximum_x': high[0],
            'peak_to_peak': high[idx] - low[idx],
            'sample_arithmetic_mean': math.fsum(r[idx] for r in rows) / len(rows),
            'basis': '.PRINT samples only; sample mean is NOT a time-weighted DC average; not cursor measurements.'}


TEMPLATES = {
        'rc': {'r_ohm': 20000., 'c_farad': 220e-12, 'frequency_hz': 36000., 'amplitude_v': 1.},
        'rc_ac': {'r_ohm': 20000., 'c_farad': 220e-12},
        'cap_equivalence': {'r_ohm': 20000., 'c_farad': 100e-12},
        'res_equivalence': {'r_ohm': 1000.},
        'ind_equivalence': {'r_ohm': 1000., 'l_henry': .01, 'series_ohm': .001},
        'rectifier': {'r_ohm': 1000., 'c_farad': 1e-6, 'frequency_hz': 1000., 'amplitude_v': 5.},
        'clipper': {'r_ohm': 1000., 'frequency_hz': 1000., 'amplitude_v': 5.},
}


def parameters(kind: str, p: dict) -> dict:
    if kind not in TEMPLATES or set(p) - set(TEMPLATES[kind]):
        raise ValueError('Unknown template or parameter; see list_templates.')
    values = TEMPLATES[kind] | p
    for name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f'{name} must be finite numeric SI units.')
        if value < 0 or (value == 0 and not (name == 'c_farad' and kind == 'rectifier')):
            raise ValueError(f'{name} must be positive (rectifier c_farad=0 means no capacitor).')
        if not 1e-18 <= value <= 1e12 and value != 0:
            raise ValueError(f'{name} must be within 1e-18 .. 1e12 SI units.')
    return values


def template(kind: str, p: dict, settings: Settings) -> str:
    values = parameters(kind, p)
    r = values['r_ohm']
    if kind in ('rc_ac', 'res_equivalence', 'ind_equivalence'):
        lines = [f'* {kind}: new verification, not historical measurement', 'V1 1 0 AC 1', f'RIN 1 2 {r:.12g}']
        if kind == 'rc_ac':
            lines += [f'C1 2 0 {values["c_farad"]:.12g}']
        else:
            edges = [(2, 0), (2, 3), (2, 3), (3, 4), (4, 0), (4, 0)]
            lines += [f'RREF 1 5 {r:.12g}']
            if kind == 'res_equivalence':
                lines += [f'R{i} {a} {b} {r:.12g}' for i, (a, b) in enumerate(edges, 1)]
                lines += [f'REQ 5 0 {2*r/3:.12g}']
            else:
                l, rs = values['l_henry'], values['series_ohm']
                lines += ['* Each coil has series resistance to avoid ideal inductor/voltage-source loops.',
                          '* Reference impedance is (2/3)*(Rs+j*w*L), NOT a lossless inductor.']
                for i, (a, b) in enumerate(edges, 1):
                    lines += [f'L{i} {a} {10+i} {l:.12g}', f'RS{i} {10+i} {b} {rs:.12g}']
                lines += [f'LEQ 5 20 {2*l/3:.12g}', f'RSEQ 20 0 {2*rs/3:.12g}']
        columns = 'VDB(2) VP(2)' if kind == 'rc_ac' else 'VDB(2) VDB(5) VP(2) VP(5)'
        lines += ['.AC DEC 50 1 10MEG', f'.PRINT AC {columns}', '.PROBE', '.END', '']
        return '\n'.join(lines)
    if kind == 'cap_equivalence':
        c = values['c_farad']
        return '\n'.join(['* Six-capacitor network vs equivalent: PSpice-only generalization',
             'V1 1 0 AC 1', f'RIN 1 2 {r:.12g}', f'CTOP 2 0 {c:.12g}',
             f'CLEFT1 2 3 {c:.12g}', f'CLEFT2 2 3 {c:.12g}', f'CMID 3 4 {c:.12g}',
             f'CRIGHT1 4 0 {c:.12g}', f'CRIGHT2 4 0 {c:.12g}',
             'RBLEED1 3 0 1G', 'RBLEED2 4 0 1G', f'RREF 1 5 {r:.12g}', f'CREF 5 0 {1.5*c:.12g}',
             '.AC DEC 50 100 10MEG', '.PRINT AC VDB(2) VDB(5) VP(2) VP(5)', '.PROBE V(2) V(5)', '.END', ''])
    f, amp = values['frequency_hz'], values['amplitude_v']
    lines = [f'* {kind} generated template; new simulation, not historical measurement']
    if kind != 'rc':
        lines += [f'.LIB "{settings.library}"']
    lines += [f'V1 1 0 AC 1 SIN(0 {amp:.12g} {f:.12g})']
    if kind == 'rectifier':
        lines += ['D1 1 2 D1N4148', f'R1 2 0 {r:.12g}']
        if values['c_farad'] > 0:
            lines += [f'C1 2 0 {values["c_farad"]:.12g}']
    else:
        lines += [f'R1 1 2 {r:.12g}']
        lines += ([f'C1 2 0 {values["c_farad"]:.12g}'] if kind == 'rc' else
                  ['D1 2 0 D1N4148', 'D2 0 2 D1N4148'])
    # At high f, seven periods may be only ~4 time constants for RC. Preserve
    # three periods of settled samples instead of mislabelling startup as steady state.
    tau = r * values.get('c_farad', 0)
    settling_periods = max(7, math.ceil(12*tau*f)) if kind in ('rc','rectifier') else 7
    lines += [f'* Save after >=7 periods and >=12 RC time constants; retain 3 periods.',
              f'.TRAN {1/f/500:.12g} {(settling_periods+3)/f:.12g} {settling_periods/f:.12g} {1/f/500:.12g}',
              '.PRINT TRAN V(1) V(2)', '.PROBE V(1) V(2)', '.END', '']
    return '\n'.join(lines)


class Jobs:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.lock = threading.RLock()
        self.active: dict[str, subprocess.Popen] = {}
        self.starting = False
        self.reservation = None
        self.cancelled = set()
        self.closing = False
        atexit.register(self.shutdown)

    def _record(self, folder: Path, data: dict):
        target = folder / 'run.json'
        temp = folder / 'run.json.tmp'
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temp, target)

    def start(self, source: Path, owner: str | None = None) -> dict:
        text = read_text(source)
        validate_netlist(text, self.settings)
        exe = self.settings.bin / 'psp_cmd.exe'
        if not exe.is_file():
            raise ValueError('psp_cmd.exe not found.')
        with self.lock:
            if self.closing or self.starting or self.active or (self.reservation and self.reservation != owner):
                raise ValueError('One PSpice job at a time; wait for the current job.')
            self.starting = True
            try:
                folder = self.settings.new_dir('runs', 'pspice')
                cir = folder / 'circuit.cir'
                cir.write_text(text, encoding='ascii')
                data = {'job_id': folder.name, 'status': 'starting', 'source': str(source),
                        'source_sha256': digest(source.read_bytes()), 'netlist_sha256': digest(cir.read_bytes()),
                        'created_unix': time.time(), 'engine': str(exe), 'directory': str(folder),
                        'output': str(folder / 'circuit.out'), 'data_file': str(folder / 'circuit.dat')}
                self._record(folder, data)
                # Never use a shell. Only the fixed installed PSpice command is invoked.
                with (folder / 'engine.log').open('wb') as log:
                    proc = subprocess.Popen([str(exe), '/r', str(cir)], cwd=folder,
                                            stdout=log, stderr=subprocess.STDOUT,
                                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                data.update(status='running', pid=proc.pid)
                self.active[folder.name] = proc
                self._record(folder, data)
                threading.Thread(target=self._finish, args=(folder, data, proc), daemon=True).start()
                return json.loads(json.dumps(data))
            finally:
                self.starting = False

    def _finish(self, folder, data, proc):
        try:
            code = proc.wait(timeout=self.settings.timeout)
            out = folder / 'circuit.out'
            details = inspect_output(out) if out.is_file() else {}
            success = code == 0 and details.get('job_concluded') and not details.get('diagnostics')
            data.update(status='completed' if success else 'failed', exit_code=code, result=details)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            data.update(status='timed_out', error='Owned simulation process timed out and was stopped.')
        except Exception as exc:
            data.update(status='failed', error=str(exc))
        finally:
            data['finished_unix'] = time.time()
            with self.lock:
                if folder.name in self.cancelled:
                    data.update(status='cancelled', note='Only this server-owned simulation process was stopped.')
                    self.cancelled.discard(folder.name)
                self._record(folder, data)
                self.active.pop(folder.name, None)

    def cancel(self, job_id: str) -> dict:
        with self.lock:
            current = self.status(job_id)
            proc = self.active.get(job_id)
            if proc is None or proc.poll() is not None:
                return {'job_id':job_id, 'stop_requested':False, 'status':current['status']}
            self.cancelled.add(job_id)
            proc.kill()
            return {'job_id':job_id,'stop_requested':True,'status':'cancelling'}

    def status(self, job_id: str) -> dict:
        if not re.fullmatch(r'\d{8}-\d{6}-pspice-[a-f0-9]{10}', job_id):
            raise ValueError('Invalid job_id.')
        path = (self.settings.workspace / 'runs' / job_id / 'run.json').resolve(strict=True)
        if not path.is_relative_to(self.settings.workspace):
            raise ValueError('Invalid job path.')
        with self.lock:
            data = json.loads(path.read_text(encoding='utf-8'))
            if data['status'] in ('running', 'starting') and job_id not in self.active:
                data['status'] = 'interrupted_or_untracked'
                data['note'] = 'Server restarted. Inspect raw output; never infer successful completion.'
        return data

    def shutdown(self):
        with self.lock:
            self.closing = True
            for proc in list(self.active.values()):
                if proc.poll() is None:
                    proc.kill()
