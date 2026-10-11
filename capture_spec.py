# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jang cuber
"""Declarative Capture schematics: a typed JSON spec -> constrained generated Tcl.

The spec only names whitelisted symbols, grid coordinates, numeric values and net names.
Every string that reaches Tcl is validated here; nothing from the caller is evaluated.
Pin offsets were measured on the installed Capture 17.2 libraries (inches, y downward) and confirmed
against Capture-generated netlists (a 0.1 inch gap leaves uA741 power pins on global V+/V- nets).
"""
from __future__ import annotations

import math
import re

GRID = 0.1
STUB = 0.2
REF = re.compile(r'[A-Za-z][A-Za-z0-9_]{0,15}')
NET = re.compile(r'0|[A-Za-z][A-Za-z0-9_]{0,30}')
NUMBER = re.compile(r'(?i)[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?(?:meg|[tgkmunpf])?')
SCALE = {'t': 1e12, 'g': 1e9, 'meg': 1e6, 'k': 1e3, 'm': 1e-3, 'u': 1e-6, 'n': 1e-9, 'p': 1e-12, 'f': 1e-15}

# type: (library, symbol, {rotation: {pin: (dx, dy)}}, {pin: outward direction per rotation}, value properties)
H2 = {'1': (-1, 0), '2': (1, 0)}
V2 = {'1': (0, 1), '2': (0, -1)}  # rotated 90 deg: pin 1 ends up at the BOTTOM
SRC = {'+': (0, -1), '-': (0, 1)}
SYMBOLS = {
    'R':   ('analog', 'R', {0: {'1': (-.1, .1), '2': (.3, .1)}, 90: {'1': (.1, .3), '2': (.1, -.1)}}, {0: H2, 90: V2}, ('Value',)),
    'C':   ('analog', 'C', {0: {'1': (-.1, .1), '2': (.2, .1)}, 90: {'1': (.1, .2), '2': (.1, -.1)}}, {0: H2, 90: V2}, ('Value',)),
    'L':   ('analog', 'L', {0: {'1': (-.1, .2), '2': (.5, .2)}}, {0: H2}, ('Value',)),
    'D1N4148': ('eval', 'D1N4148', {0: {'1': (-.1, .1), '2': (.2, .1)}}, {0: H2}, ()),
    'VAC': ('source', 'VAC', {0: {'+': (.1, -.1), '-': (.1, .3)}}, {0: SRC}, ('ACMAG',)),
    'VDC': ('source', 'VDC', {0: {'+': (.1, 0), '-': (.1, .4)}}, {0: SRC}, ('DC',)),
    'VSIN': ('source', 'VSIN', {0: {'+': (.1, 0), '-': (.1, .3)}}, {0: SRC}, ('VOFF', 'VAMPL', 'FREQ')),
    'UA741': ('eval', 'uA741', {0: {'+': (-.1, .1), '-': (-.1, .5), 'out': (.7, .3), 'v+': (.3, 0), 'v-': (.3, .6)}},
              {0: {'+': (-1, 0), '-': (-1, 0), 'out': (1, 0), 'v+': (0, -1), 'v-': (0, 1)}}, ()),
}
PREFIX = {'R': 'R_', 'C': 'C_', 'L': 'L_', 'D1N4148': 'D_', 'VAC': 'V_', 'VDC': 'V_', 'VSIN': 'V_', 'UA741': 'X_'}
# Capture netlist pin order (verified against generated .net files).
NET_ORDER = {'R': ('1', '2'), 'C': ('1', '2'), 'L': ('1', '2'), 'D1N4148': ('1', '2'),
             'VAC': ('+', '-'), 'VDC': ('+', '-'), 'VSIN': ('+', '-'), 'UA741': ('+', '-', 'v+', 'v-', 'out')}
ANALYSIS = re.compile(r'(?i)\.(AC\s+(DEC|OCT|LIN)\s+\d{1,6}|TRAN)(\s+[0-9.eE+\-a-zA-Z]+){2,4}')


def number(text) -> float:
    text = str(text).strip()
    if not NUMBER.fullmatch(text):
        raise ValueError(f'"{text}" is not a numeric PSpice literal (e.g. 27k, 100n, 3.979m, 1meg).')
    m = re.fullmatch(r'(?i)(.*?)(meg|[tgkmunpf])?', text)
    value = float(m[1]) * SCALE.get((m[2] or '').lower(), 1)
    if not math.isfinite(value):
        raise ValueError('Values must be finite.')
    return value


def grid(value, name) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{name} must be a number in inches.')
    snapped = round(value / GRID) * GRID
    if abs(snapped - value) > 1e-6 or not 0 <= snapped <= 30:
        raise ValueError(f'{name}={value} must be on the 0.1 inch grid within 0..30.')
    return round(snapped, 1)


def normalize(spec: dict) -> dict:
    if not isinstance(spec, dict) or set(spec) - {'parts', 'wires', 'grounds', 'aliases', 'analysis'}:
        raise ValueError('spec keys: parts, wires, grounds, aliases, analysis.')
    parts, refs = [], set()
    for p in spec.get('parts', []):
        if not isinstance(p, dict) or set(p) - {'ref', 'type', 'x', 'y', 'rotate', 'value', 'values', 'nets', 'expect'}:
            raise ValueError('part keys: ref, type, x, y, rotate, value | values, nets (drawn stubs), expect (check only).')
        kind = str(p.get('type', '')).upper()
        if kind not in SYMBOLS:
            raise ValueError(f'Unsupported part type {p.get("type")}. Allowed: {sorted(SYMBOLS)}')
        ref = str(p.get('ref', ''))
        if not REF.fullmatch(ref) or ref.upper() in refs:
            raise ValueError(f'Invalid or duplicate part reference "{ref}".')
        refs.add(ref.upper())
        rot = p.get('rotate', 0)
        if rot not in SYMBOLS[kind][2]:
            raise ValueError(f'{kind} supports rotate {sorted(SYMBOLS[kind][2])} (measured pin geometry only).')
        props = SYMBOLS[kind][4]
        values = p.get('values') or ({props[0]: p['value']} if 'value' in p and props else {})
        if set(values) != set(props):
            raise ValueError(f'{ref} needs values for {list(props)}.')
        values = {k: str(v).strip() for k, v in values.items()}
        for k, v in values.items():
            number(v)
        nets, expect = p.get('nets') or {}, p.get('expect') or {}
        pins = SYMBOLS[kind][2][rot]
        if set(nets) - set(pins) or set(expect) - set(pins) or set(nets) & set(expect):
            raise ValueError(f'{ref} pins are {list(pins)}; a pin is either drawn (nets) or only checked (expect).')
        for n in list(nets.values()) + list(expect.values()):
            if not NET.fullmatch(str(n)):
                raise ValueError(f'Net name "{n}" must be 0 or start with a letter (letters, digits, _).')
        parts.append({'ref': ref, 'type': kind, 'x': grid(p.get('x'), f'{ref}.x'), 'y': grid(p.get('y'), f'{ref}.y'),
                      'rotate': rot, 'values': values, 'nets': {k: str(v) for k, v in nets.items()},
                      'expect': {k: str(v) for k, v in expect.items()}})
    if not 1 <= len(parts) <= 120:
        raise ValueError('Give 1-120 parts.')
    wires = [[grid(v, 'wire') for v in w] for w in spec.get('wires', [])]
    if len(wires) > 600 or any(len(w) != 4 or (w[0] != w[2] and w[1] != w[3]) for w in wires):
        raise ValueError('wires: up to 600 orthogonal [x1, y1, x2, y2] segments.')
    grounds = [[grid(v, 'ground') for v in g] for g in spec.get('grounds', [])]
    if any(len(g) != 2 for g in grounds) or len(grounds) > 100:
        raise ValueError('grounds: [x, y] wire end points (up to 100).')
    aliases = []
    for a in spec.get('aliases', []):
        if len(a) != 3 or not NET.fullmatch(str(a[2])) or str(a[2]) == '0':
            raise ValueError('aliases: [x, y, name] on a wire; use grounds for net 0.')
        aliases.append([grid(a[0], 'alias'), grid(a[1], 'alias'), str(a[2])])
    analysis = [' '.join(str(s).split()) for s in spec.get('analysis', [])]
    if len(analysis) > 1 or any(not ANALYSIS.fullmatch(s) for s in analysis):
        raise ValueError('analysis: at most one ".AC DEC n fstart fstop" or ".TRAN tstep tstop [tstart [tmax]]".')
    result = {'parts': parts, 'wires': wires, 'grounds': grounds, 'aliases': aliases, 'analysis': analysis}
    check_stubs(result)
    return result


def _points(seg):
    x1, y1, x2, y2 = seg
    n = int(round(max(abs(x2 - x1), abs(y2 - y1)) / GRID))
    return {(round(x1 + (x2 - x1) * i / n, 1), round(y1 + (y2 - y1) * i / n, 1)) for i in range(n + 1)} if n else {(x1, y1)}


def stubs(spec):
    for p in spec['parts']:
        for pin, net in p['nets'].items():
            x, y = pin_point(p, pin)
            ux, uy = SYMBOLS[p['type']][3][p['rotate']][pin]
            yield p['ref'], pin, net, [x, y, round(x + ux * STUB, 1), round(y + uy * STUB, 1)]


def check_stubs(spec):
    """Reject stubs that land on the MIDDLE of a wire or overlap other stubs (silent net merges).

    A stub tip meeting a wire END point is an intentional join and stays allowed.
    """
    interior, ends, used = {}, set(), {}
    for i, w in enumerate(spec['wires']):
        pts = _points(w)
        tips = {(w[0], w[1]), (w[2], w[3])}
        ends |= tips
        for pt in pts - tips:
            interior.setdefault(pt, f'wire #{i} {w}')
    for ref, pin, net, seg in stubs(spec):
        body = _points(seg) - {(seg[0], seg[1])}
        tip = (seg[2], seg[3])
        clash = sorted({interior[pt] for pt in body if pt in interior}
                       | {f'wire end {pt}' for pt in body - {tip} if pt in ends}
                       | {used[pt] for pt in body if pt in used})
        if clash:
            raise ValueError(f'{ref} pin {pin} stub {seg} touches {clash}; move the part or wire the pin explicitly.')
        for pt in body:
            used[pt] = f'{ref}.{pin} stub'


def pin_point(part, pin):
    dx, dy = SYMBOLS[part['type']][2][part['rotate']][pin]
    return round(part['x'] + dx, 1), round(part['y'] + dy, 1)


def layout_lines(spec, libs, word):
    """Tcl body lines (without project open/save) for a normalized spec."""
    lines, extra_wires, extra_grounds, extra_aliases = [], [], [], []
    for p in spec['parts']:
        library, symbol = SYMBOLS[p['type']][:2]
        lines.append(f'PlacePart {p["x"]:g} {p["y"]:g} {word(libs / (library + ".olb"))} {word(symbol)} {{}} FALSE')
        if p['rotate'] == 90:
            lines.append('Rotate')
        lines.append(f'SetProperty {{Part Reference}} {word(p["ref"])}')
        for key, value in p['values'].items():
            lines.append(f'SetProperty {word(key)} {word(value)}')
        lines.extend(['EndPlace', 'UnSelectAll'])
    for ref, pin, net, (x, y, ex, ey) in stubs(spec):
        extra_wires.append([x, y, ex, ey])
        (extra_grounds if net == '0' else extra_aliases).append([ex, ey] if net == '0' else [ex, ey, net])
    for a, b, c, d in spec['wires'] + extra_wires:
        lines.extend([f'PlaceWire {a:g} {b:g} {c:g} {d:g}', 'EndPlace', 'UnSelectAll'])
    for x, y in spec['grounds'] + extra_grounds:
        # The installed GND symbol's pin sits 0.1 inch right of its placement point.
        lines.extend([f'PlaceGround {x - .1:g} {y:g} {word(libs / "source.olb")} {{0}} {{0}}', 'EndPlace', 'UnSelectAll'])
    for x, y, name in spec['aliases'] + extra_aliases:
        lines.extend([f'PlaceNetAlias {x:g} {y:g} {word(name)}', 'EndPlace', 'UnSelectAll'])
    return lines


def expected_nets(spec):
    """Device name -> ordered expected nets (None = not declared; checked by the caller)."""
    result = {}
    for p in spec['parts']:
        name = (PREFIX[p['type']] + p['ref']).upper()
        declared = {**p['nets'], **p['expect']}
        result[name] = [declared.get(pin) for pin in NET_ORDER[p['type']]]
    return result


def compare(spec, actual: dict) -> list[str]:
    """Return human-readable mismatches between declared nets/values and the native netlist."""
    problems = []
    expected = expected_nets(spec)
    if set(actual) != set(expected):
        problems.append(f'device set differs: netlist={sorted(actual)}, spec={sorted(expected)}')
    for p in spec['parts']:
        name = (PREFIX[p['type']] + p['ref']).upper()
        tokens = actual.get(name)
        if tokens is None:
            continue
        want = expected[name]
        got = tokens[:len(want)]
        for pin, w, g in zip(NET_ORDER[p['type']], want, got):
            if w is not None and g.upper() != w.upper():
                problems.append(f'{p["ref"]} pin {pin}: netlist {g} != spec {w}')
        if p['type'] in ('R', 'C', 'L') and len(tokens) > 2:
            try:
                if not math.isclose(number(tokens[2]), number(p['values']['Value']), rel_tol=1e-9):
                    problems.append(f'{p["ref"]} value: netlist {tokens[2]} != spec {p["values"]["Value"]}')
            except ValueError:
                problems.append(f'{p["ref"]} value unreadable in netlist: {tokens[2]}')
    return problems


def pin_reference() -> dict:
    return {kind: {'library': f'{lib}.olb', 'symbol': sym,
                   'pins_by_rotation': {str(r): {k: list(v) for k, v in pins.items()} for r, pins in geo.items()},
                   'values': list(props), 'netlist_pin_order': list(NET_ORDER[kind])}
            for kind, (lib, sym, geo, _, props) in SYMBOLS.items()}
