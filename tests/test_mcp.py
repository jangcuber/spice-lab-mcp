# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jang cuber
"""No live desktop inputs. Unit guards + real stdio protocol; --live runs PSpice."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch, call

from core import Settings, inspect_output, measured_table, parse_print_tables, statistics, template, validate_netlist
from desktop import Desktop, Observation, key_spec, literal_spec, numeric_value

ROOT = Path(__file__).resolve().parent.parent


class Tests(unittest.TestCase):
    def setUp(self):
        self.s = Settings()

    def test_templates_validate(self):
        for kind in ('rc', 'cap_equivalence', 'rectifier', 'clipper'):
            validate_netlist(template(kind, {}, self.s), self.s)

    def test_negative_value(self):
        with self.assertRaises(ValueError):
            template('rc', {'r_ohm': -1}, self.s)

    def test_infinite_value(self):
        with self.assertRaises(ValueError):
            template('rc', {'r_ohm': float('inf')}, self.s)

    def test_unknown_parameter(self):
        with self.assertRaises(ValueError):
            template('rc', {'frequency': 42}, self.s)

    def test_zero_rectifier_cap(self):
        text = template('rectifier', {'c_farad': 0}, self.s)
        self.assertNotIn('C1 ', text)

    def test_cap_topology(self):
        text = template('cap_equivalence', {}, self.s)
        self.assertIn('CREF 5 0 1.5e-10', text)
        self.assertEqual(text.count(' 1G'), 2)

    def test_external_include_rejected(self):
        for bad in ('.INC "secret.txt"', '.LIB "C:/secret.lib"', '.SAVE text', '.CONTROL'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_netlist('* test\nV1 1 0 1\n.OP\n'+bad+'\n.END', self.s)

    def test_missing_end(self):
        with self.assertRaises(ValueError):
            validate_netlist('* test\n.OP', self.s)

    def test_missing_title(self):
        with self.assertRaises(ValueError):
            validate_netlist('V1 1 0 1\n.OP\n.END', self.s)

    def test_path_restrictions(self):
        for path in ('//server/share/data.cir', 'C:/file.cir:stream', str(ROOT / 'server.py')):
            with self.subTest(path=path), self.assertRaises((ValueError, FileNotFoundError)):
                self.s.readable(path)

    def test_output_label(self):
        for label in ('../escape', 'bad/name', '', 'a'*61):
            with self.subTest(label=label), self.assertRaises(ValueError):
                self.s.new_dir('drafts', label)

    def test_parser_pagination(self):
        t = parse_print_tables('FREQ V(1) V(2)\n\n1 2 3\n2 4 5\nfooter\nFREQ V(1) V(2)\n3 6 7\n')
        self.assertEqual(len(t), 1)
        self.assertEqual(len(t[0]['rows']), 3)

    def test_parser_sweep_reset(self):
        t = parse_print_tables('TIME V(1)\n0 1\n1 2\n\nTIME V(1)\n0 3\n1 4\n')
        self.assertEqual(len(t), 2)

    def test_printed_time_rounding_duplicates(self):
        t = parse_print_tables('TIME V(1)\n1.944E-4 1\n1.944E-4 2\n1.945E-4 3\nTIME V(1)\n1.945E-4 4\n')
        self.assertEqual(len(t), 1)
        self.assertEqual(len(t[0]['rows']), 4)

    def test_parser_different_column_groups(self):
        t = parse_print_tables('FREQ VDB(1)\n1 -2\n2 -3\nFREQ VP(1)\n1 -20\n2 -30\n')
        self.assertEqual(len(t), 2)

    def test_parser_exponents_and_lines(self):
        t = parse_print_tables('TIME V(1)\n1D-3 -2.5E+0\n')[0]
        self.assertEqual(t['rows'], [[.001, -2.5]])
        self.assertEqual(t['row_lines'], [2])

    def test_stats_interval(self):
        t = parse_print_tables('TIME V(1)\n0 10\n1 2\n2 4\n')[0]
        r = statistics(t, 'V(1)', 1, 2)
        self.assertEqual(r['peak_to_peak'], 2)
        self.assertEqual(r['sample_arithmetic_mean'], 3)

    def test_stats_empty(self):
        t = parse_print_tables('TIME V(1)\n0 10\n')[0]
        with self.assertRaises(ValueError):
            statistics(t, 'V(1)', 1, 2)

    def test_incomplete_evidence_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'bad.out'
            path.write_text('TIME V(1)\n0 1\n')
            with self.assertRaises(ValueError):
                measured_table(path, 0)

    def test_keys(self):
        self.assertEqual(key_spec('Ctrl+S'), '^s')
        self.assertEqual(key_spec('F11'), '{F11}')
        self.assertEqual(key_spec('Shift+Tab'), '+{TAB}')

    def test_shortcut_uses_virtual_key_not_unicode_packet(self):
        d = Desktop(self.s)
        d._consume = Mock(return_value=object())
        d._safe_focus = Mock()
        d._after = Mock(return_value='observed')
        d.keyboard = Mock()
        self.assertEqual(d.press_key('token', 'P'), 'observed')
        d.keyboard.send_keys.assert_called_once_with('p', vk_packet=False, pause=.01)

    def test_literal_text_keeps_unicode_packet(self):
        d = Desktop(self.s)
        d._consume = Mock(return_value=object())
        d._safe_focus = Mock()
        d._after = Mock(return_value='observed')
        d.keyboard = Mock()
        d.type_text('token', '20k')
        d.keyboard.send_keys.assert_called_once_with('20k', with_spaces=True, vk_packet=True, pause=.005)

    def test_system_keys_blocked(self):
        for value in ('Win+R', 'Meta', 'Alt+Tab', 'Ctrl+Esc', 'Alt+F4', '{ENTER}', 'A B', 'Ctrl+Ctrl+S'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                key_spec(value)

    def test_delete_confirmation(self):
        with self.assertRaises(ValueError):
            key_spec('Delete')
        self.assertEqual(key_spec('Delete', True), '{DELETE}')

    def test_literal_typing(self):
        self.assertEqual(literal_spec('20k'), '20k')
        self.assertIn('{+}', literal_spec('V(1)+V(2)'))
        self.assertNotEqual(literal_spec('{ENTER}'), '{ENTER}')

    def test_console_input_blocked(self):
        for value in ('\n', 'hello\tworld', 'exec calc.exe', 'powershell', 'source file.tcl'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                literal_spec(value)

    def test_cadence_library_names_are_literal_data(self):
        for path in (r'C:\Cadence\SPB_17.2\tools\capture\library\pspice\source.olb', 'eval.lib'):
            self.assertEqual(literal_spec(path), path)
        for value in ('source source.olb', 'exec eval.lib', 'eval source.olb', 'powershell source.olb'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                literal_spec(value)

    def test_numeric_field_literals(self):
        for value in ('1', '100k', '100', '2e-6', '-.5', '1meg'):
            self.assertEqual(numeric_value(value), value)
        for value in ('', 'NaN', 'inf', '1e999', '1 2', '1;exec', 'source', '1\n', '0'*65):
            with self.subTest(value=value), self.assertRaises(ValueError):
                numeric_value(value)

    def test_unobserved_numeric_field_rejected(self):
        d = Desktop(self.s)
        d._consume = Mock(return_value=Observation('x',1,2,3,(0,0,100,100),1,0,None))
        with self.assertRaises(ValueError):
            d.set_numeric_field('x','1','100k')

    def test_changed_numeric_field_rejected(self):
        d = Desktop(self.s)
        obs = Observation('x',1,2,3,(0,0,100,100),1,0,None,{'1':{'value':'1'}})
        d._consume = Mock(return_value=obs)
        d._numeric_fields = Mock(return_value={'1':{'value':'2'}})
        with self.assertRaises(ValueError):
            d.set_numeric_field('x','1','100k')

    def numeric_field_fixture(self, actual='100k'):
        d = Desktop(self.s)
        saved = {'runtime_id': [42, 9], 'rect': [1, 1, 30, 20], 'value': '1'}
        obs = Observation('x', 1, 2, 3, (0, 0, 100, 100), 1, 0, None, {'1': saved})
        control = Mock()
        control.element_info.runtime_id = [42, 9]
        control.iface_value.CurrentValue = actual
        d._consume = Mock(return_value=obs)
        d._numeric_fields = Mock(return_value={'1': saved})
        d.WinDesktop = Mock()
        d.WinDesktop.return_value.window.return_value.descendants.return_value = [control]
        d._frame = Mock()
        d._after = Mock(return_value=('observed', None))
        d.keyboard = Mock()
        focused = Mock()
        focused.GetRuntimeId.return_value = [42, 9]
        focused.CurrentHasKeyboardFocus = True
        focused.CurrentControlType = 50004
        focused.CurrentProcessId = 2
        return d, focused, control

    def test_numeric_field_exact_focus_and_readback(self):
        d, focused, control = self.numeric_field_fixture()
        with patch('pywinauto.uia_defines.IUIA') as uia, patch('desktop.time.sleep'):
            uia.return_value.iuia.GetFocusedElement.return_value = focused
            self.assertEqual(d.set_numeric_field('x', '1', '100k'), ('observed', None))
        self.assertEqual(d.keyboard.send_keys.call_args_list, [
            call('^a', vk_packet=False, pause=.01),
            call('100k', with_spaces=True, vk_packet=False, pause=.01)])
        control.set_focus.assert_not_called()
        control.iface_value.SetValue.assert_not_called()

    def test_numeric_field_wrong_focus_sends_nothing(self):
        for change in ('runtime_id', 'keyboard_focus', 'control_type', 'process_id'):
            with self.subTest(change=change):
                d, focused, _ = self.numeric_field_fixture()
                if change == 'runtime_id': focused.GetRuntimeId.return_value = [99]
                if change == 'keyboard_focus': focused.CurrentHasKeyboardFocus = False
                if change == 'control_type': focused.CurrentControlType = 50033
                if change == 'process_id': focused.CurrentProcessId = 999
                with patch('pywinauto.uia_defines.IUIA') as uia:
                    uia.return_value.iuia.GetFocusedElement.return_value = focused
                    result, _ = d.set_numeric_field('x', '1', '100k')
                self.assertFalse(result['action_sent'])
                d.keyboard.send_keys.assert_not_called()

    def test_numeric_field_readback_mismatch_not_success(self):
        d, focused, _ = self.numeric_field_fixture(actual='1')
        with patch('pywinauto.uia_defines.IUIA') as uia, patch('desktop.time.sleep'):
            uia.return_value.iuia.GetFocusedElement.return_value = focused
            result, _ = d.set_numeric_field('x', '1', '100k')
        self.assertTrue(result['action_sent'])
        self.assertTrue(result['verification_required'])
        self.assertEqual(result['actual'], '1')
        d._after.assert_not_called()

    def test_numeric_field_foreground_guard_precedes_typing(self):
        d, focused, _ = self.numeric_field_fixture()
        d._frame.side_effect = ValueError('foreground changed')
        with patch('pywinauto.uia_defines.IUIA') as uia:
            uia.return_value.iuia.GetFocusedElement.return_value = focused
            with self.assertRaises(ValueError):
                d.set_numeric_field('x', '1', '100k')
        d.keyboard.send_keys.assert_not_called()

    def test_expired_observation(self):
        d = Desktop(self.s)
        d._selected = Mock(side_effect=AssertionError('must not touch desktop'))
        d.observations['x'] = Observation('x', 1, 2, 3, (0,0,100,100), 1, time.monotonic()-100, None)
        with self.assertRaises(ValueError):
            d._consume('x')
        self.assertNotIn('x', d.observations)

    def test_unknown_observation(self):
        with self.assertRaises(ValueError):
            Desktop(self.s)._consume('invented')

    def test_geometry_change(self):
        d = Desktop(self.s)
        d._selected = Mock(return_value=(2, 3))
        d._frame = Mock(return_value=((5,5,105,105), None))
        d.observations['x'] = Observation('x', 1, 2, 3, (0,0,100,100), 1, time.monotonic(), None)
        with self.assertRaises(ValueError):
            d._consume('x')

    def test_point_outside_window(self):
        d = Desktop(self.s)
        obs = Observation('x', 1, 2, 3, (0,0,100,100), 1, 0, None)
        for x, y in ((-1,2), (100,2), (2,100), (float('nan'), 2)):
            with self.subTest(x=x, y=y), self.assertRaises(ValueError):
                d._point(obs, x, y)

    def test_invalid_drag_arguments(self):
        with self.assertRaises(ValueError):
            Desktop(self.s).drag('x', 0, 0, 1, 1, duration=float('nan'))

    def test_owned_native_popup_menu(self):
        from types import SimpleNamespace
        d = Desktop(self.s)
        d.gui = Mock()
        d.gui.GetAncestor.side_effect = lambda handle, flag: handle
        d.gui.GetClassName.return_value = '#32768'
        d.process = Mock()
        d.process.GetWindowThreadProcessId.return_value = (10, 20)
        d._thread_info = Mock(return_value=SimpleNamespace(hwndMenuOwner=1))
        self.assertTrue(d._belongs_to_target(2, 1))
        d._thread_info.return_value.hwndMenuOwner = 3
        self.assertFalse(d._belongs_to_target(2, 1))

    def test_other_process_popup_menu_rejected(self):
        d = Desktop(self.s)
        d.gui = Mock()
        d.gui.GetAncestor.return_value = 2
        d.gui.GetClassName.return_value = '#32768'
        d.process = Mock()
        d.process.GetWindowThreadProcessId.side_effect = [(10, 20), (11, 21)]
        self.assertFalse(d._belongs_to_target(2, 1))

    def test_other_same_process_window_rejected(self):
        d = Desktop(self.s)
        d.gui = Mock()
        d.gui.GetAncestor.return_value = 2
        d.gui.GetClassName.return_value = 'OtherAppWindow'
        self.assertFalse(d._belongs_to_target(2, 1))

    def test_owned_tooltip_visible_but_not_clickable(self):
        d = Desktop(self.s)
        d.gui = Mock()
        d.gui.GetAncestor.side_effect = lambda handle, flag: 2 if flag == 2 else 1
        d.gui.GetClassName.return_value = 'tooltips_class32'
        d.process = Mock()
        d.process.GetWindowThreadProcessId.return_value = (10, 20)
        self.assertTrue(d._owned_tooltip(2, 1))
        self.assertFalse(d._belongs_to_target(2, 1))

    def test_unowned_tooltip_rejected(self):
        d = Desktop(self.s)
        d.gui = Mock()
        d.gui.GetAncestor.return_value = 2
        d.gui.GetClassName.return_value = 'tooltips_class32'
        self.assertFalse(d._owned_tooltip(2, 1))

    def test_foreign_tooltip_rejected(self):
        d = Desktop(self.s)
        d.gui = Mock()
        d.gui.GetAncestor.side_effect = lambda handle, flag: 2 if flag == 2 else 1
        d.gui.GetClassName.return_value = 'tooltips_class32'
        d.process = Mock()
        d.process.GetWindowThreadProcessId.side_effect = [(10, 20), (11, 21)]
        self.assertFalse(d._owned_tooltip(2, 1))

    def test_image_content_transport(self):
        # Exercise MCP's real result conversion with a synthetic TEST image;
        # never save this as a circuit screenshot or simulation result.
        import server
        from PIL import Image
        import io
        from unittest.mock import patch
        buffer = io.BytesIO()
        Image.new('RGB', (1, 1), 'white').save(buffer, format='PNG')
        async def check():
            with patch.object(server.desktop, 'observe', return_value=({'test_fixture': True}, buffer.getvalue())):
                blocks = await server.mcp.call_tool('observe_window', {'window_id': 1})
                self.assertEqual([b.type for b in blocks], ['text', 'image'])
        asyncio.run(check())


async def protocol_test(live=False):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT/'server.py')], cwd=str(ROOT))
    report = {'time_unix': time.time(), 'ui_live_input_tested_by_this_runner': False,
              'separate_gui_evidence': 'runtime/gui-verification; see VERIFICATION.md',
              'real_simulations': []}
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            init = await session.initialize()
            listed = await session.list_tools()
            report['server'] = init.serverInfo.model_dump()
            report['tools'] = [t.name for t in listed.tools]
            assert {'click','drag','press_key','type_text','set_numeric_field','start_simulation','inspect_results'} <= set(report['tools'])
            async def call(name, args=None):
                response = await session.call_tool(name, args or {})
                if response.isError:
                    raise AssertionError(str(response.content))
                if response.structuredContent:
                    return response.structuredContent
                return json.loads(response.content[0].text)
            report['environment'] = await call('environment')
            if live:
                for kind in ('cap_equivalence', 'clipper', 'rectifier', 'rc'):
                    draft = await call('create_from_template', {'kind': kind})
                    job = await call('start_simulation', {'path': draft['path']})
                    deadline = time.monotonic()+150
                    while time.monotonic() < deadline:
                        status = await call('simulation_status', {'job_id': job['job_id']})
                        if status['status'] not in ('starting','running'):
                            break
                        await asyncio.sleep(.5)
                    assert status['status'] == 'completed', status
                    entry = {'kind': kind, 'job': status}
                    if kind == 'cap_equivalence':
                        entry['comparison'] = await call('compare_columns', {'path': job['output'], 'column_a':'VDB(2)', 'column_b':'VDB(5)'})
                        assert entry['comparison']['max_absolute_difference'] <= .002
                    else:
                        entry['measurement'] = await call('measure_samples', {'path': job['output'], 'column':'V(2)'})
                        assert entry['measurement']['sample_count'] >= 1400, entry
                    report['real_simulations'].append(entry)
                    print(f'Real PSpice {kind}: completed', flush=True)
    target = ROOT/'runtime'/'verification.json'
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'MCP protocol verified, {len(report["tools"])} tools. Evidence: {target}')


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Tests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        sys.exit(1)
    asyncio.run(protocol_test('--live' in sys.argv))
