# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jang cuber
"""Unit fixtures are NOT simulation evidence. Live runner is verify_extensions.py."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, Mock
from core import Settings, Jobs, TEMPLATES, template, validate_netlist, statistics, parse_print_tables
from capture_automation import CaptureAutomation, build_script, word, KINDS, profile_text, analysis_lines
from batch import Batches
from probe_data import probe_table, measure_export


class Extensions(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s=Settings()
        self.s.workspace=Path(self.tmp.name)

    def test_all_templates(self):
        for kind in TEMPLATES:
            validate_netlist(template(kind,{},self.s),self.s)

    def test_res_equivalent(self):
        t=template('res_equivalence',{'r_ohm':3000},self.s)
        self.assertIn('REQ 5 0 2000',t)

    def test_inductor_nonideal_reference(self):
        t=template('ind_equivalence',{'l_henry':.03,'series_ohm':.003},self.s)
        self.assertIn('LEQ 5 20 0.02',t)
        self.assertIn('RSEQ 20 0 0.002',t)
        self.assertEqual(sum(line.startswith('RS') for line in t.splitlines()),7)

    def test_parameter_injection(self):
        for p in ({'r_ohm':'1;exec cmd'},{'r_ohm':True},{'r_ohm':float('nan')},{'r_ohm':1e200}):
            with self.subTest(p=p), self.assertRaises(ValueError):
                template('rc',p,self.s)

    def test_invalid_interval(self):
        t={'columns':['Time','V(1)'],'rows':[[0,1],[1,2]]}
        for a,b in ((2,1),(float('nan'),None),(None,float('inf'))):
            with self.subTest(a=a,b=b),self.assertRaises(ValueError):
                statistics(t,'V(1)',a,b)

    def test_print_adjacent_named_columns(self):
        data='FREQ VDB(OUT_MCP)VP(OUT_MCP)\n1 -0.001 -0.72\n2 -0.002 -1.44\n'
        t=parse_print_tables(data)[0]
        self.assertEqual(t['columns'],['FREQ','VDB(OUT_MCP)','VP(OUT_MCP)'])
        self.assertEqual(len(t['rows']),2)

    def test_print_header_garbage_not_accepted(self):
        self.assertEqual(parse_print_tables('FREQ VDB(OUT_MCP)garbage\n1 2\n'),[])

    def test_print_named_node_actual_output(self):
        path=Path(__file__).parent/'fixtures/rc-named-nodes.out'
        tables=parse_print_tables(path.read_text())
        self.assertEqual(tables[0]['columns'],['FREQ','VDB(OUT_MCP)','VP(OUT_MCP)'])
        self.assertEqual(len(tables[0]['rows']),351)

    def test_tcl_literals(self):
        self.assertEqual(word('C:\\a b\\file'),'{C:/a b/file}')
        for s in ('x}\nexec bad','x{y','x\x00'):
            with self.assertRaises(ValueError):
                word(s)

    def test_all_capture_templates_prepare(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                b=CaptureAutomation(self.s).prepare(kind,{})
                self.assertTrue(Path(b['profile']).is_file())
                self.assertTrue((Path(b['profile']).parent/'mcp_analysis').is_dir())
                self.assertIn('mcp_analysis.sim',Path(b['project']).read_text())
                self.assertIn('PSpice::Create Netlist',Path(b['script']).read_text())

    def test_generated_profiles_match_analysis(self):
        for kind in KINDS:
            text=profile_text(kind,{},self.s)
            lines=analysis_lines(kind,{},self.s)
            ac=next((s.split() for s in lines if s.startswith('.AC')),None)
            self.assertIn('@Analysis: 3 0',text)
            if ac:
                self.assertIn('@Analysis: 1 1',text)
                for i,value in enumerate(ac[2:]):self.assertIn(f'+{i} "{value}"',text)
            else:
                tran=next(s.split() for s in lines if s.startswith('.TRAN'))
                self.assertIn('@Analysis: 0 1',text)
                for i,j in ((0,2),(2,3),(3,4)):self.assertIn(f'+{i} "{tran[j]}"',text)

    def test_profile_tamper_blocks_launch(self):
        manager=CaptureAutomation(self.s)
        b=manager.prepare('clipper',{})
        Path(b['profile']).write_text('changed')
        with patch('capture_automation.subprocess.Popen') as launch,self.assertRaises(ValueError):
            manager.launch(b['build_id'])
        launch.assert_not_called()

    def test_profile_run_directory_tamper_blocks_launch(self):
        manager=CaptureAutomation(self.s)
        for missing in (True,False):
            b=manager.prepare('clipper',{})
            run_dir=Path(b['profile']).parent/'mcp_analysis'
            if missing:
                run_dir.rmdir()  # Empty unit-test fixture only.
            else:
                (run_dir/'unexpected.txt').write_text('unit fixture')
            with patch('capture_automation.subprocess.Popen') as launch,self.assertRaises(ValueError):
                manager.launch(b['build_id'])
            launch.assert_not_called()

    def test_inductor_library_pin_geometry(self):
        text=build_script(self.s.workspace,'ind_equivalence',{},self.s)
        # L1 at (3.4,1.85): installed ANALOG L pins are one grid below R/C
        # and the right pin is wider. These were confirmed by actual .net.
        self.assertIn('PlaceWire 2.8 2.05 3.3 2.05',text)
        self.assertIn('PlaceWire 3.9 2.05 4.25 2.05',text)

    def test_capture_rc_rejects_transient_parameters(self):
        with self.assertRaises(ValueError):
            CaptureAutomation(self.s).prepare('rc',{'frequency_hz':100})

    def test_capture_equivalence_reference_named_outputs(self):
        for kind in ('cap_equivalence','res_equivalence','ind_equivalence'):
            lines=analysis_lines(kind,{},self.s)
            self.assertIn('VDB([REF_MCP])',' '.join(lines))
            self.assertNotIn('VDB(5)',' '.join(lines))

    def test_tcl_new_copy(self):
        b=CaptureAutomation(self.s).prepare('rc',{'c_farad':100e-9})
        text=Path(b['script']).read_text(encoding='utf-8')
        self.assertIn('SetProperty {Value} {1e-07}',text)
        self.assertIn('PSpice::Create Netlist',text)
        self.assertFalse(b['simulated'])
        self.assertTrue(Path(b['project']).is_file())

    def test_tcl_script_tamper_blocked(self):
        manager=CaptureAutomation(self.s)
        b=manager.prepare('rc',{})
        Path(b['script']).write_text('exec malicious',encoding='utf-8')
        with patch('capture_automation.subprocess.Popen') as launch,self.assertRaises(ValueError):
            manager.launch(b['build_id'])
        launch.assert_not_called()

    def test_existing_capture_blocks_launch(self):
        manager=CaptureAutomation(self.s)
        b=manager.prepare('rc',{})
        proc=Mock(info={'name':'Capture.exe'})
        with patch('capture_automation.psutil.process_iter',return_value=[proc]),patch('capture_automation.subprocess.Popen') as launch:
            with self.assertRaises(ValueError):
                manager.launch(b['build_id'])
        launch.assert_not_called()

    def test_tcl_launch_single_use_posix_path(self):
        manager=CaptureAutomation(self.s)
        b=manager.prepare('rectifier',{'c_farad':0})
        with patch('capture_automation.psutil.process_iter',return_value=[]),patch('capture_automation.subprocess.Popen') as launch:
            launch.return_value.pid=123
            manager.launch(b['build_id'])
            self.assertNotIn('\\',launch.call_args.args[0][1])
            with self.assertRaises(ValueError):
                manager.launch(b['build_id'])
            self.assertEqual(launch.call_count,1)

    def test_tcl_bad_id(self):
        with self.assertRaises(ValueError):
            CaptureAutomation(self.s).status('../original')

    def test_tcl_wrapper_tamper_blocked(self):
        manager=CaptureAutomation(self.s)
        b=manager.prepare('rc',{})
        (Path(b['directory'])/'launch.tcl').write_text('exec bad')
        with self.assertRaises(ValueError):
            manager.launch(b['build_id'])

    def test_generated_net_provenance(self):
        manager=CaptureAutomation(self.s)
        b=manager.prepare('rc',{'c_farad':100e-9})
        folder=Path(b['directory'])
        b['status']='launch_requested'
        (folder/'build.json').write_text(json.dumps(b))
        native='* unit fixture, NOT simulation evidence\nV_V1 IN_MCP 0 DC 0Vdc AC 1\nR_R1 IN_MCP OUT_MCP 20000 TC=0,0\nC_C1 OUT_MCP 0 1e-7 TC=0,0\n'
        (folder/'test.net').write_text(native)
        checked=manager.verify(b['build_id'])
        self.assertTrue(checked['connectivity_verified'])
        output=manager.simulation_input(b['build_id'])
        self.assertIn(native,Path(output['path']).read_text())
        self.assertIn('.PRINT AC VDB([OUT_MCP]) VP([OUT_MCP])',Path(output['path']).read_text())
        (folder/'test.net').write_text(native.replace('20000','10000'))
        with self.assertRaises(ValueError):
            manager.verify(b['build_id'])

    def test_transient_settling(self):
        for kind,p in (('rc',{'c_farad':1e-6,'frequency_hz':80}),('rectifier',{'frequency_hz':10000})):
            v=TEMPLATES[kind]|p
            line=next(s for s in template(kind,p,self.s).splitlines() if s.startswith('.TRAN'))
            _,step,end,start,maximum=line.split()
            self.assertGreaterEqual(float(start),12*v['r_ohm']*v['c_farad']-1e-12)
            self.assertAlmostEqual((float(end)-float(start))*v['frequency_hz'],3)

    def test_probe_real_export(self):
        path=Path(__file__).parent/'fixtures/probe-ac-export.txt'
        self.assertEqual(len(probe_table(path)['rows']),501)

    def test_probe_bad_input(self):
        path=Path(self.tmp.name)/'export.txt'
        for data in ('Time V(1)\n1 2\n0 3','Time V(1)\n1 nan','Time V(1)\n1 2 3','Time V(1)\n'):
            path.write_text(data)
            with self.subTest(data=data),self.assertRaises(ValueError):
                probe_table(path)

    def test_probe_measurement_label(self):
        path=Path(self.tmp.name)/'export.txt'
        path.write_text('Time V(1)\n0 -1\n1 2')
        r=measure_export(path,'V(1)')
        self.assertEqual(r['peak_to_peak'],3)
        self.assertIn('not cursor',r['basis'])

    def test_batch_bad_conditions(self):
        b=Batches(self.s,Jobs(self.s))
        for cases in ([],[{}]*31,[{'oops':1}]):
            with self.assertRaises(ValueError):
                b.start('rc',cases)
        self.assertFalse((self.s.workspace/'batches').exists())

    def test_reservation_blocks_single_job(self):
        jobs=Jobs(self.s)
        jobs.reservation='batch'
        path=self.s.workspace/'test.cir'
        path.write_text(template('rc',{},self.s))
        with self.assertRaises(ValueError):
            jobs.start(path)

    def test_batch_stops_on_failure(self):
        jobs=Jobs(self.s)
        jobs.start=Mock(return_value={'job_id':'fixture'})
        jobs.status=Mock(return_value={'status':'failed'})
        b=Batches(self.s,jobs)
        data=b.start('rc',[{},{}])
        for _ in range(100):
            out=b.status(data['batch_id'])
            if out['status']!='running': break
            time.sleep(.01)
        self.assertEqual(out['status'],'failed')
        self.assertEqual(out['conditions'][1]['status'],'not_run')
        self.assertIsNone(jobs.reservation)

    def test_batch_cancel_preserves_completed(self):
        jobs=Jobs(self.s)
        jobs.start=Mock(return_value={'job_id':'fixture'})
        jobs.status=Mock(return_value={'status':'running'})
        jobs.cancel=Mock(side_effect=lambda _:setattr(jobs.status,'return_value',{'status':'cancelled'}))
        b=Batches(self.s,jobs)
        data=b.start('rc',[{},{}])
        self.assertTrue(b.cancel(data['batch_id'])['stop_requested'])
        for _ in range(100):
            out=b.status(data['batch_id'])
            if out['status']!='running': break
            time.sleep(.01)
        self.assertEqual(out['status'],'cancelled')
        self.assertTrue(all(e['status'] in ('not_run','cancelled') for e in out['conditions']))


if __name__=='__main__':
    unittest.main(verbosity=2)
