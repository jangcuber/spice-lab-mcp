# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jang cuber
"""Constrained Capture Tcl generation, never arbitrary Tcl evaluation or console typing.

Uses Cadence's installed empty.dsn and documented `capture <script>` CLI.
The generated page is a NEW copy; an existing Capture process blocks launch.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import psutil

from core import Settings, digest, parameters, template, validate_netlist
import capture_spec


def word(value) -> str:
    text = str(value).replace('\\', '/')
    if any(c in text for c in '{}\r\n\x00'):
        raise ValueError('Unsupported character in generated Tcl literal.')
    return '{' + text + '}'


KINDS = ('rc', 'rectifier', 'clipper', 'cap_equivalence', 'res_equivalence', 'ind_equivalence')


def analysis_kind(kind):
    return 'rc_ac' if kind == 'rc' else kind


def analysis_lines(kind, p, settings):
    lines = [s for s in template(analysis_kind(kind), p, settings).splitlines() if s.startswith('.')]
    for i, node in ((1, 'IN_MCP'), (2, 'OUT_MCP'), (5, 'REF_MCP')):
        lines = [re.sub(rf'\b(VDB|VP|V)\({i}\)', rf'\1([{node}])', s) for s in lines]
    return lines


def profile_text(kind, p, settings):
    """Typed input profile, based on installed vendor format and GUI-saved AC/TRAN profiles.

    This is an INPUT, never evidence that Capture or PSpice accepted it.
    """
    return profile_from_lines(analysis_lines(kind, p, settings), settings)


def profile_from_lines(lines, settings):
    vendor = settings.bin.parent/'capture'/'templates'/'pspice'/'empty-pspicefiles'/'schematic1'/'bias.sim'
    text = vendor.read_text(encoding='ascii').replace('bias', 'mcp_analysis')
    text = text.replace('NetlistFile= ""', 'NetlistFile= "mcp_analysis.cir"')
    ac = next((s.split() for s in lines if s.startswith('.AC ')), None)
    if ac:
        body = '+2 0 0\n' + '\n'.join(f'+{i} "{v}"' for i,v in enumerate(ac[2:] + ['', '', '']))
        index, mode = 1, 2
    else:
        tran = next(s.split() for s in lines if s.startswith('.TRAN '))
        body = '+0 0 0 0\n' + '\n'.join(f'+{i} "{v}"' for i,v in enumerate([tran[2], '', tran[3], tran[4], '', '', '']))
        index, mode = 0, 0
    text = re.sub(r'@Settings: \d+ 1', f'@Settings: {mode} 1', text)
    text = text.replace('@Analysis: 3 1', '@Analysis: 3 0')
    text = re.sub(rf'@Analysis: {index} [01]\n.*?@End Analysis',
                  f'@Analysis: {index} 1\n{body}\n@End Analysis', text, flags=re.S)
    return text


def project_text() -> str:
    return '''(ExpressProject "MCP_Test"
  (ProjectVersion "19981106")
  (ProjectType "Analog or A/D Mixed Mode")
  (Folder "Design Resources" (Folder "Library") (NoModify)
    (File "./circuit.dsn" (Type "Schematic Design")))
  (Folder "Outputs")
  (Folder "PSpice Resources" (Folder "Model Libraries") (Folder "Stimulus Files")
    (Folder "Include Files") (Folder "Simulation Profiles"
      (ActiveProfile "./circuit-pspicefiles/schematic1/mcp_analysis.sim")
      (File.PSpice.{09528990-3187-11D2-BC7B-00A0C90CBF91}
        "./circuit-pspicefiles/schematic1/mcp_analysis.sim"
        (DisplayName "SCHEMATIC1-mcp_analysis") (Type "PSpice Profile")))))
'''


def build_script(folder: Path, kind: str, p: dict, settings: Settings) -> str:
    if kind not in KINDS:
        raise ValueError(f'Capture schematic templates: {KINDS}')
    v = parameters(kind, p)
    libs = settings.bin.parent / 'capture' / 'library' / 'pspice'
    lines = [f'Open {word(folder / "circuit.opj")}', 'SelectPMItem {./circuit.dsn}',
             'OPage {SCHEMATIC1} {PAGE1}']

    def part(x, y, library, symbol, ref, props):
        lines.append(f'PlacePart {x:g} {y:g} {word(libs / (library+".olb"))} {word(symbol)} {{}} FALSE')
        lines.append(f'SetProperty {{Part Reference}} {word(ref)}')
        for key, val in props.items():
            lines.append(f'SetProperty {word(key)} {word(format(val, ".12g") if isinstance(val, (float, int)) else val)}')
        lines.extend(['EndPlace', 'UnSelectAll'])

    def wire(a, b, c, d):
        lines.extend([f'PlaceWire {a:g} {b:g} {c:g} {d:g}', 'EndPlace', 'UnSelectAll'])

    if kind == 'rc':
        part(2, 2, 'source', 'VAC', 'V1', {'ACMAG': 1})
        part(3, 1.8, 'analog', 'R', 'R1', {'Value': v['r_ohm']})
        part(4.5, 1.8, 'analog', 'C', 'C1', {'Value': v['c_farad']})
        segments = [(2.1,1.9,2.9,1.9),(3.3,1.9,4.4,1.9),(4.7,1.9,5.3,1.9),
                    (5.3,1.9,5.3,2.8),(2.1,2.3,2.1,2.8),(2.1,2.8,5.3,2.8)]
        ground, aliases = (5.2,2.8), [(2.5,1.9,'IN_MCP'),(3.8,1.9,'OUT_MCP')]
    elif kind == 'rectifier':
        part(2,1.6,'source','VSIN','V1', {'VOFF':0,'VAMPL':v['amplitude_v'],'FREQ':v['frequency_hz']})
        part(3.5,1.1,'eval','D1N4148','D1', {})
        part(5,1.8,'analog','R','R1', {'Value':v['r_ohm']})
        segments = [(2.1,1.5,2.1,1.2),(2.1,1.2,3.4,1.2),(3.7,1.2,4.5,1.2),
                    (4.5,1.2,4.5,1.9),(4.5,1.9,4.9,1.9),(5.3,1.9,6.1,1.9),
                    (6.1,1.9,6.1,2.7),(2.1,1.9,2.1,2.7),(2.1,2.7,6.1,2.7)]
        if v['c_farad']:
            part(5,2.2,'analog','C','C1',{'Value':v['c_farad']})
            segments += [(4.5,1.9,4.5,2.3),(4.5,2.3,4.9,2.3),(5.2,2.3,6.1,2.3)]
        ground, aliases = (6,2.7), [(2.5,1.2,'IN_MCP'),(4,1.2,'OUT_MCP')]
    else:
        # Net-labelled stubs avoid wire crossings. Matching aliases are actual
        # Capture nets, not cosmetic text. Every pin is checked in the native .net.
        aliases, segments = [], []
        def node_name(n):
            return {'1':'IN_MCP', '2':'OUT_MCP', '5':'REF_MCP'}.get(n, 'N'+n+'_MCP')
        def terminal(x, y, n):
            if n == '0':
                lines.extend([f'PlaceGround {x-.1:g} {y:g} {word(libs/"source.olb")} {{0}} {{0}}', 'EndPlace', 'UnSelectAll'])
            else:
                aliases.append((x,y,node_name(n)))
        if kind == 'clipper':
            part(1,1.4,'source','VSIN','V1',{'VOFF':0,'VAMPL':v['amplitude_v'],'FREQ':v['frequency_hz']})
        else:
            part(1,1.4,'source','VAC','V1',{'ACMAG':1})
        wire(1.1,1.3,1.1,1.0)
        terminal(1.1,1.0,'1')
        wire(1.1,1.7,1.1,2.0)
        terminal(1.1,2.0,'0')
        devices = [s.split() for s in template(kind,v,settings).splitlines() if s and s[0] in 'RCLD']
        for i, tokens in enumerate(devices):
            ref,a,b,value = tokens
            symbol = 'D1N4148' if ref[0]=='D' else ref[0]
            x, y = 3.4 + (i%2)*3.3, 1.2 + (i//2)*.65
            numeric = None if ref[0]=='D' else float(value.replace('G','e9'))
            part(x,y,'eval' if ref[0]=='D' else 'analog',symbol,ref,{} if numeric is None else {'Value':numeric})
            # Installed ANALOG L is wider and one grid lower than R/C.
            right = .5 if ref[0]=='L' else (.2 if ref[0] in 'CD' else .3)
            pin_y = y + (.2 if ref[0]=='L' else .1)
            wire(x-.6,pin_y,x-.1,pin_y)
            wire(x+right,pin_y,x+.85,pin_y)
            terminal(x-.6,pin_y,a)
            terminal(x+.85,pin_y,b)
        ground = None
    for segment in segments:
        wire(*segment)
    if ground:
        lines += [f'PlaceGround {ground[0]:g} {ground[1]:g} {word(libs/"source.olb")} {{0}} {{0}}', 'EndPlace', 'UnSelectAll']
    for x,y,name in aliases:
        lines += [f'PlaceNetAlias {x:g} {y:g} {word(name)}','EndPlace','UnSelectAll']
    return wrap_script(folder, lines)


def spec_script(folder: Path, spec: dict, settings: Settings) -> str:
    libs = settings.bin.parent / 'capture' / 'library' / 'pspice'
    lines = [f'Open {word(folder / "circuit.opj")}', 'SelectPMItem {./circuit.dsn}', 'OPage {SCHEMATIC1} {PAGE1}']
    return wrap_script(folder, lines + capture_spec.layout_lines(capture_spec.normalize(spec), libs, word))


def wrap_script(folder: Path, lines: list) -> str:
    lines = lines + ['Menu {File::Save}', 'Menu {PSpice::Create Netlist}']
    # Receipt marks Tcl completion only, not connectivity or simulation success.
    return ('# Generated constrained PSpice MCP script. Do not edit and rerun.\n'
            'proc ::pspice_mcp_build {} {\nset mcpRc [catch {\n' + '\n'.join(lines) + '\n} mcpMessage mcpOptions]\n'
            f'set mcpReceipt [open {word(folder/"receipt.txt")} w]\n'
            'puts $mcpReceipt "tcl_return_code=$mcpRc"\nputs $mcpReceipt $mcpMessage\n'
            'if {$mcpRc} {puts $mcpReceipt [dict get $mcpOptions -errorinfo]}\nclose $mcpReceipt\n}\n'
            '::pspice_mcp_build\n')


def blocking_dialogs(pid):
    """Texts of visible modal dialogs owned by the launched Capture process (read-only)."""
    if not pid:
        return []
    try:
        import win32gui, win32process
    except ImportError:
        return []
    found = []
    def collect(hwnd, _):
        if (win32gui.IsWindowVisible(hwnd) and win32gui.GetClassName(hwnd) == '#32770'
                and win32process.GetWindowThreadProcessId(hwnd)[1] == pid
                and not win32gui.GetWindowText(hwnd).startswith('About ')):
            texts = []
            win32gui.EnumChildWindows(hwnd, lambda c, _: texts.append(win32gui.GetWindowText(c)), None)
            found.append({'title': win32gui.GetWindowText(hwnd), 'text': [t for t in texts if t][:6]})
    win32gui.EnumWindows(collect, None)
    return found


class CaptureAutomation:
    def __init__(self, settings):
        self.settings = settings
        self.lock = threading.Lock()

    def prepare(self, kind, p):
        parameters(kind, p)
        if kind not in KINDS:
            raise ValueError(f'Supported schematic templates: {KINDS}')
        if kind=='rc' and set(p)-{'r_ohm','c_farad'}:
            raise ValueError('Capture RC is AC-only; r_ohm/c_farad are the supported parameters.')
        vendor = self.settings.bin.parent/'capture'/'templates'/'pspice'/'empty.dsn'
        if not vendor.is_file():
            raise ValueError('Installed Cadence empty.dsn template missing.')
        folder = self.settings.new_dir('capture-tcl', kind)
        script = build_script(folder, kind, p, self.settings)
        shutil.copyfile(vendor, folder/'circuit.dsn')
        profile = folder/'circuit-pspicefiles'/'schematic1'/'mcp_analysis.sim'
        profile.parent.mkdir(parents=True)
        profile.write_text(profile_text(kind,p,self.settings),encoding='ascii')
        # Capture 17.2 opens profiles without this directory but GUI Run aborts
        # before producing a .cir/.out. The normal New Profile wizard creates it.
        (profile.parent/'mcp_analysis').mkdir()
        (folder/'circuit.opj').write_text(project_text(), encoding='ascii')
        (folder/'build.tcl').write_text(script, encoding='utf-8')
        # Capture CLI replays individual lines. Source the full script in ONE Tcl
        # evaluation so proc/catch scope and channel variables remain intact.
        (folder/'launch.tcl').write_text(f'source {word(folder/"build.tcl")}\n',encoding='utf-8')
        saved_parameters = parameters('rc_ac' if kind=='rc' else kind,p)
        record = {'build_id': folder.name, 'kind':kind, 'parameters':saved_parameters, 'status':'prepared',
                  'directory':str(folder),'project':str(folder/'circuit.opj'), 'script':str(folder/'build.tcl'),
                  'template_sha256':digest(vendor.read_bytes()), 'script_sha256':digest((folder/'build.tcl').read_bytes()),
                  'profile':str(profile), 'profile_sha256':digest(profile.read_bytes()),
                  'analysis':analysis_lines(kind,p,self.settings),
                  'created_unix':time.time(), 'simulated':False}
        (folder/'build.json').write_text(json.dumps(record, indent=2),encoding='utf-8')
        return record

    def prepare_spec(self, label, spec):
        normalized = capture_spec.normalize(spec)
        if not normalized['analysis']:
            raise ValueError('spec.analysis needs one .AC or .TRAN line (used for the Capture profile and verification run).')
        vendor = self.settings.bin.parent/'capture'/'templates'/'pspice'/'empty.dsn'
        if not vendor.is_file():
            raise ValueError('Installed Cadence empty.dsn template missing.')
        if not re.fullmatch(r'[a-z0-9_]{1,30}', label):
            raise ValueError('label: 1-30 lowercase letters, digits or _.')
        folder = self.settings.new_dir('capture-tcl', 'spec_' + label)
        script = spec_script(folder, normalized, self.settings)
        shutil.copyfile(vendor, folder/'circuit.dsn')
        profile = folder/'circuit-pspicefiles'/'schematic1'/'mcp_analysis.sim'
        profile.parent.mkdir(parents=True)
        profile.write_text(profile_from_lines(normalized['analysis'], self.settings), encoding='ascii')
        (profile.parent/'mcp_analysis').mkdir()
        (folder/'circuit.opj').write_text(project_text(), encoding='ascii')
        (folder/'build.tcl').write_text(script, encoding='utf-8')
        (folder/'launch.tcl').write_text(f'source {word(folder/"build.tcl")}\n', encoding='utf-8')
        record = {'build_id': folder.name, 'kind': 'spec', 'spec': normalized, 'status': 'prepared',
                  'directory': str(folder), 'project': str(folder/'circuit.opj'), 'script': str(folder/'build.tcl'),
                  'template_sha256': digest(vendor.read_bytes()), 'script_sha256': digest((folder/'build.tcl').read_bytes()),
                  'profile': str(profile), 'profile_sha256': digest(profile.read_bytes()),
                  'analysis': normalized['analysis'], 'created_unix': time.time(), 'simulated': False,
                  'declared_nets': capture_spec.expected_nets(normalized)}
        (folder/'build.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
        return record

    def _expected_script(self, folder, record):
        if record['kind'] == 'spec':
            return spec_script(folder, record['spec'], self.settings)
        return build_script(folder, record['kind'], record['parameters'], self.settings)

    def _expected_profile(self, record):
        if record['kind'] == 'spec':
            return profile_from_lines(record['spec']['analysis'], self.settings)
        return profile_text(record['kind'], record['parameters'], self.settings)

    def _folder(self, build_id):
        if not re.fullmatch(r'\d{8}-\d{6}-(?:'+'|'.join(KINDS)+r'|spec_[a-z0-9_]{1,30})-[a-f0-9]{10}', build_id):
            raise ValueError('Invalid build_id.')
        folder = (self.settings.workspace/'capture-tcl'/build_id).resolve(strict=True)
        if not folder.is_relative_to(self.settings.workspace):
            raise ValueError('Invalid build path.')
        return folder

    def launch(self, build_id):
        with self.lock:
            folder = self._folder(build_id)
            record = json.loads((folder/'build.json').read_text(encoding='utf-8'))
            if record['status'] != 'prepared':
                raise ValueError('Build is single-use. Prepare a new one; do not duplicate placed parts.')
            expected = self._expected_script(folder, record)
            if (folder/'build.tcl').read_text(encoding='utf-8') != expected:
                raise ValueError('Generated script changed; arbitrary Tcl is not allowed.')
            if (folder/'launch.tcl').read_text(encoding='utf-8') != f'source {word(folder/"build.tcl")}\n':
                raise ValueError('Generated launcher changed.')
            vendor = self.settings.bin.parent/'capture'/'templates'/'pspice'/'empty.dsn'
            run_dir = folder/'circuit-pspicefiles'/'schematic1'/'mcp_analysis'
            if not run_dir.is_dir() or any(run_dir.iterdir()):
                raise ValueError('Prepared profile run directory changed. Prepare a new build.')
            if ((folder/'circuit.dsn').read_bytes() != vendor.read_bytes() or
                    (folder/'circuit.opj').read_text(encoding='ascii') != project_text() or
                    (folder/'circuit-pspicefiles'/'schematic1'/'mcp_analysis.sim').read_text(encoding='ascii') !=
                    self._expected_profile(record)):
                raise ValueError('Prepared project changed. Prepare a new build.')
            exe = self.settings.apps['capture']
            for proc in psutil.process_iter(['name','exe']):
                if (proc.info['name'] or '').casefold() == 'capture.exe':
                    raise ValueError('Close Capture normally after saving before launching a generated build; existing designs are protected.')
            # Mark before launch: a crash/exception must never enable unsafe automatic replay.
            record.update(status='launch_requested', launched_unix=time.time())
            (folder/'build.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
            # Cadence treats this command-line argument as a Tcl path: forward slashes required.
            proc = subprocess.Popen([str(exe), (folder/'launch.tcl').as_posix()], cwd=folder,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            record.update(pid=proc.pid)
            (folder/'build.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
            return record

    def status(self, build_id):
        folder = self._folder(build_id)
        record = json.loads((folder/'build.json').read_text(encoding='utf-8'))
        receipt = folder/'receipt.txt'
        if receipt.is_file():
            text = receipt.read_text(encoding='utf-8',errors='replace')
            if text:
                record.update(receipt=text, status='tcl_completed_needs_verification' if text.startswith('tcl_return_code=0\n') else 'tcl_failed')
        if record['status']=='launch_requested':
            dialogs = blocking_dialogs(record.get('pid'))
            if dialogs:
                # e.g. "Capture has detected projects that were not properly closed" after a crash:
                # the generated script is paused until a human answers it.
                record['blocking_dialogs'] = dialogs
        if record['status']=='launch_requested' and time.time()-record.get('launched_unix',time.time())>60:
            record.update(status='needs_attention',note='No completed Tcl receipt within 60 seconds. Inspect Capture; never replay automatically.')
        record['netlists'] = [str(p) for p in folder.rglob('*.net') if p.resolve().is_relative_to(folder)]
        record['verification'] = 'Inspect actual schematic and generated .net; Tcl completion alone is NOT simulation or connectivity verification.'
        return record

    def _verified_net(self, build_id):
        folder = self._folder(build_id)
        record = json.loads((folder/'build.json').read_text(encoding='utf-8'))
        if record['status'] != 'launch_requested':
            raise ValueError('Only an actually launched Capture build can be checked.')
        candidates = [p for p in folder.rglob('*.net') if p.resolve().is_relative_to(folder)]
        if len(candidates) != 1:
            raise ValueError('Exactly one actual Capture-generated .net is required; inspect the build.')
        net = candidates[0]
        raw = net.read_bytes()
        text = raw.decode('ascii').replace('\r\n','\n')
        lines = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith('*'):
                continue
            if line.startswith('+') and lines:
                lines[-1] += ' ' + line[1:]
            else:
                lines.append(line)
        actual = {}
        for line in lines:
            tokens = line.upper().split()
            name = tokens.pop(0)
            if name in actual:
                raise ValueError('Duplicate device in generated netlist.')
            actual[name] = tokens
        if record['kind'] == 'spec':
            problems = capture_spec.compare(record['spec'], actual)
            evidence = {'build_id': build_id, 'netlist': str(net), 'netlist_sha256': digest(raw),
                        'devices': actual, 'mismatches': problems,
                        'connectivity_verified': not problems, 'simulated': False,
                        'basis': 'Actual Capture-generated netlist compared with the declared per-pin nets and R/C/L values. '
                                 'Pins without a declared net (explicit wires) are listed for inspection, not judged.'}
            return record, text, evidence
        p = parameters(record['kind'],record['parameters'])
        expected = {'R_R1':['IN_MCP','OUT_MCP',p['r_ohm'],'TC=0,0'],
                    'V_V1':['IN_MCP','0','DC','0VDC','AC',1.]}
        if record['kind'] in ('rc','rectifier') and p['c_farad']:
            expected['C_C1'] = ['OUT_MCP','0',p['c_farad'],'TC=0,0']
        if record['kind'] == 'rectifier':
            expected['R_R1'] = ['OUT_MCP','0',p['r_ohm'],'TC=0,0']
            expected['D_D1'] = ['IN_MCP','OUT_MCP','D1N4148']
            expected['V_V1'] = ['IN_MCP','0','SIN',0.,p['amplitude_v'],p['frequency_hz'],0.,0.,0.]
        elif record['kind'] != 'rc':
            expected = {'V_V1':expected['V_V1']}
            if record['kind']=='clipper':
                expected['V_V1']=['IN_MCP','0','SIN',0.,p['amplitude_v'],p['frequency_hz'],0.,0.,0.]
            node = lambda n: {'0':'0','1':'IN_MCP','2':'OUT_MCP','5':'REF_MCP'}.get(n,'N'+n+'_MCP')
            for line in template(record['kind'],p,self.settings).splitlines():
                if line and line[0] in 'RCLD':
                    name,a,b,value=line.split()
                    target=[node(a),node(b),value if name[0]=='D' else float(value.replace('G','e9'))]
                    if name[0] in 'RC': target.append('TC=0,0')
                    expected[name[0]+'_'+name]=target
        if actual.keys() != expected.keys():
            raise ValueError(f'Generated device set differs: actual={list(actual)}, expected={list(expected)}')
        for name, want in expected.items():
            got = actual[name]
            if len(got) != len(want):
                raise ValueError(f'Unexpected netlist fields on {name}.')
            for observed, target in zip(got,want):
                if isinstance(target,(float,int)):
                    try:
                        valid = math.isclose(float(observed),target,rel_tol=1e-10,abs_tol=0)
                    except ValueError:
                        valid = False
                else:
                    valid = observed == target
                if not valid:
                    raise ValueError(f'Generated connectivity/value differs on {name}: {observed} != {target}')
        evidence = {'build_id':build_id,'netlist':str(net),'netlist_sha256':digest(raw),
                    'devices':actual,'connectivity_verified':True,'simulated':False,
                    'basis':'Actual Capture-generated netlist matches typed template; not a simulation or visual-layout verdict.'}
        return record, text, evidence

    def verify(self, build_id):
        return self._verified_net(build_id)[2]

    def simulation_input(self, build_id):
        record, native, evidence = self._verified_net(build_id)
        if record['kind'] == 'spec':
            if evidence['mismatches']:
                raise ValueError(f'Schematic does not match the spec; fix before simulating: {evidence["mismatches"]}')
            lib = (f'.LIB "{self.settings.library.as_posix()}"\n'
                   if any(p['type'] in ('UA741', 'D1N4148') for p in record['spec']['parts']) else '')
            # Probe-friendly run: every node voltage is kept in the .dat file.
            text = ('* Actual Capture netlist (spec build); NOT historical measurement\n' + lib + native + '\n'
                    + '\n'.join(record['spec']['analysis']) + '\n.PROBE\n.END\n')
            validate_netlist(text, self.settings)
            folder = self.settings.new_dir('capture-runs', 'spec-input')
            path = folder/'circuit.cir'
            path.write_text(text, encoding='ascii')
            evidence.update(path=str(path), input_sha256=digest(path.read_bytes()), analysis=record['spec']['analysis'])
            (folder/'provenance.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
            return evidence
        # Keep the ACTUAL native device lines unchanged. Only append analysis,
        # library and measurement directives to a separate NEW input file.
        kind = analysis_kind(record['kind'])
        p = {k:v for k,v in record['parameters'].items() if kind!='rc_ac' or k in ('r_ohm','c_farad')}
        analysis = analysis_lines(record['kind'],p,self.settings)
        text = '* Actual Capture netlist verification run; NOT historical measurement\n'+native+'\n'+'\n'.join(analysis)+'\n'
        validate_netlist(text,self.settings)
        folder = self.settings.new_dir('capture-runs','verified-input')
        path = folder/'circuit.cir'
        path.write_text(text,encoding='ascii')
        evidence.update(path=str(path),input_sha256=digest(path.read_bytes()),analysis=analysis)
        (folder/'provenance.json').write_text(json.dumps(evidence,indent=2),encoding='utf-8')
        return evidence
