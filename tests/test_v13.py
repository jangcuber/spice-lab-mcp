# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jang cuber
"""v1.3.0 guards: Probe command validation, spec normalisation/geometry, layout/crop arguments. No desktop input."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import json
import unittest
from unittest.mock import patch

import capture_spec as cs
from capture_automation import CaptureAutomation, spec_script, word
from core import Settings
from probe_control import axis_value, search_command, trace_expression

R = {'ref': 'R1', 'type': 'R', 'x': 1.0, 'y': 1.0, 'value': '1k'}


def spec(*parts, **extra):
    return {'parts': list(parts) or [dict(R)], 'analysis': ['.AC DEC 10 1 1k'], **extra}


class ProbeCommands(unittest.TestCase):
    def test_trace_ok(self):
        for t in ('DB(V(VOUT))', 'V(in)-V(out)', 'VP(U1:OUT)', 'V(A)*2', 'I(R1)'):
            self.assertEqual(trace_expression(t), t)

    def test_trace_rejects(self):
        for t in ('V(a); exec x', 'system(1)', '', 'x' * 201, 'V(a)\nV(b)', '"V(a)"'):
            with self.subTest(t=t), self.assertRaises(ValueError):
                trace_expression(t)

    def test_search_ok(self):
        for s in ('search forward level(max-3)', 'search backward xvalue(1k)', 'search forward min',
                  'search  forward   level(-3.01, p)', 'search forward peak'):
            self.assertTrue(search_command(s).startswith('search '))

    def test_search_rejects(self):
        for s in ('level(max-3)', 'search forward exec(1)', 'search sideways min', 'search forward min; rm',
                  'search forward source x'):
            with self.subTest(s=s), self.assertRaises(ValueError):
                search_command(s)

    def test_axis_values(self):
        self.assertEqual(axis_value('1k'), '1k')
        self.assertEqual(axis_value(-100), '-100')
        for bad in ('1e999', 'abc', '1k;', ''):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                axis_value(bad)


class Spec(unittest.TestCase):
    def setUp(self):
        self.s = Settings()

    def test_number_scaling(self):
        self.assertAlmostEqual(cs.number('3.979m'), 3.979e-3)
        self.assertAlmostEqual(cs.number('1meg'), 1e6)
        self.assertAlmostEqual(cs.number('27K'), 27e3)

    def test_rejects(self):
        cases = [
            spec(dict(R, type='Q')),                              # unknown symbol
            spec(dict(R, x=1.05)),                                # off grid
            spec(dict(R), dict(R)),                               # duplicate ref
            spec(dict(R, value='1k}; exec')),                     # non numeric
            spec(dict(R, nets={'3': 'A'})),                       # unknown pin
            spec(dict(R, nets={'1': 'A b'})),                     # bad net name
            spec(dict(R, nets={'1': 'A'}, expect={'1': 'A'})),    # drawn and expected
            spec(dict(R, type='L', rotate=90)),                   # unmeasured rotation
            {'parts': [R], 'analysis': ['.INC "x"']},             # analysis whitelist
            {'parts': [R], 'wires': [[0, 0, 1, 1]], 'analysis': []},  # diagonal wire
        ]
        for c in cases:
            with self.subTest(c=json.dumps(c)[:80]), self.assertRaises(ValueError):
                cs.normalize(c)

    def test_rotated_pin_order_and_opamp_power(self):
        n = cs.normalize(spec(dict(R, rotate=90), {'ref': 'U1', 'type': 'UA741', 'x': 4.0, 'y': 2.0}))
        r, u = n['parts']
        self.assertEqual(cs.pin_point(r, '1'), (1.1, 1.3))   # pin 1 at the bottom after rotation
        self.assertEqual(cs.pin_point(r, '2'), (1.1, 0.9))
        self.assertEqual(cs.pin_point(u, 'v+'), (4.3, 2.0))  # confirmed by generated netlist
        self.assertEqual(cs.pin_point(u, 'v-'), (4.3, 2.6))

    def test_stub_collisions(self):
        u = {'ref': 'U1', 'type': 'UA741', 'x': 4.0, 'y': 3.0, 'nets': {'v-': 'VSS'}}
        with self.assertRaises(ValueError):   # stub tip lands mid-wire -> silent merge
            cs.normalize(spec(u, wires=[[4.0, 3.8, 4.6, 3.8]]))
        cs.normalize(spec(u, wires=[[4.3, 3.8, 4.6, 3.8]]))  # tip on a wire END is an intentional join
        with self.assertRaises(ValueError):   # two stubs overlapping
            cs.normalize(spec(dict(R, nets={'2': 'A'}), dict(R, ref='R2', x=1.6, nets={'1': 'B'})))

    def test_compare(self):
        n = cs.normalize(spec(dict(R, expect={'1': 'A', '2': '0'})))
        self.assertEqual(cs.compare(n, {'R_R1': ['A', '0', '1k', 'TC=0,0']}), [])
        self.assertTrue(cs.compare(n, {'R_R1': ['A', 'B', '1k', 'TC=0,0']}))
        self.assertTrue(cs.compare(n, {'R_R1': ['A', '0', '2k', 'TC=0,0']}))
        self.assertTrue(cs.compare(n, {}))

    def test_script_contains_only_validated_literals(self):
        n = cs.normalize(spec(dict(R, nets={'1': 'VIN', '2': '0'})))
        text = spec_script(self.s.workspace, n, self.s)
        for needle in ('PlacePart 1 1', '{1k}', 'PlaceNetAlias', 'PlaceGround'):
            self.assertIn(needle, text)
        self.assertNotIn('exec', text)

    def test_prepare_spec_and_tamper(self):
        cap = CaptureAutomation(self.s)
        record = cap.prepare_spec('unit_test', spec(dict(R, nets={'1': 'VIN', '2': '0'})))
        self.assertEqual(record['kind'], 'spec')
        folder = cap._folder(record['build_id'])
        (folder / 'build.tcl').write_text('exec calc', encoding='utf-8')
        with patch('capture_automation.psutil.process_iter', return_value=[]), self.assertRaises(ValueError):
            cap.launch(record['build_id'])

    def test_prepare_spec_requires_analysis(self):
        with self.assertRaises(ValueError):
            CaptureAutomation(self.s).prepare_spec('x', {'parts': [R]})

    def test_pin_reference(self):
        ref = cs.pin_reference()
        self.assertEqual(ref['UA741']['netlist_pin_order'], ['+', '-', 'v+', 'v-', 'out'])


class LayoutArgs(unittest.TestCase):
    def test_crop_validation(self):
        from desktop import Desktop, Observation
        from PIL import Image
        import time
        d = Desktop(Settings())
        d.observations['t'] = Observation('t', 1, 1, 0., (0, 0, 200, 200), 0, time.monotonic(), Image.new('RGB', (200, 200)))
        for crop in ([0, 0, 10, 10], [0, 0, 300, 100], [50, 50, 20, 120], [0, 0, 100]):
            with self.subTest(crop=crop), self.assertRaises(ValueError):
                d.save_screenshot('t', 'crop_test', crop)
        self.assertEqual(d.save_screenshot('t', 'crop_test', [10, 10, 110, 90])['width'], 100)


if __name__ == '__main__':
    unittest.main()
