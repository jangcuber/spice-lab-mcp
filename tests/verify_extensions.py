# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jang cuber
"""Official MCP SDK / real PSpice verification. No GUI inputs and no fabricated data."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import asyncio
import json
import math
from pathlib import Path
import sys
import time
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from core import Settings, digest, measured_table

ROOT=Path(__file__).resolve().parent.parent


async def main():
    folder=Settings().new_dir('extension-verification','live')
    report={'created_unix':time.time(),'batches':[],'probe_exports':[],'gui_tested_by_this_runner':False}
    async with stdio_client(StdioServerParameters(command=sys.executable,args=[str(ROOT/'server.py')],cwd=str(ROOT))) as (r,w):
        async with ClientSession(r,w) as session:
            await session.initialize()
            report['tools']=[t.name for t in (await session.list_tools()).tools]
            async def call(name,args=None):
                r=await session.call_tool(name,args or {})
                if r.isError: raise AssertionError(r.content)
                return r.structuredContent or json.loads(r.content[0].text)
            cases={
                'res_equivalence':[{'r_ohm':1000},{'r_ohm':3000}],
                'ind_equivalence':[{'l_henry':.01},{'l_henry':.02,'series_ohm':.01}],
                'cap_equivalence':[{'c_farad':100e-12},{'c_farad':220e-12}],
                'rc_ac':[{'c_farad':c} for c in (220e-12,100e-9,1e-6)],
                'rc':[{'c_farad':c,'frequency_hz':f} for c,freqs in ((220e-12,(3600,36000,360000)),(100e-9,(8,80,800)),(1e-6,(.8,8,80))) for f in freqs],
                'rectifier':[{'c_farad':c,'frequency_hz':f} for f in (100,1000,10000) for c in (0,220e-12,100e-9,1e-6)],
                'clipper':[{'amplitude_v':a} for a in (.3,1,5)]+[{'frequency_hz':100},{'frequency_hz':10000}],
            }
            for kind,conditions in cases.items():
                started=await call('start_parameter_batch',{'kind':kind,'conditions':conditions})
                deadline=time.monotonic()+180
                while time.monotonic()<deadline:
                    result=await call('parameter_batch_status',{'batch_id':started['batch_id']})
                    if result['status']!='running': break
                    await asyncio.sleep(.25)
                assert result['status']=='completed',result
                for entry in result['conditions']:
                    path=entry['result']['output']
                    if kind.endswith('_equivalence'):
                        entry['magnitude_comparison']=await call('compare_columns',{'path':path,'column_a':'VDB(2)','column_b':'VDB(5)'})
                        entry['phase_comparison']=await call('compare_columns',{'path':path,'column_a':'VP(2)','column_b':'VP(5)'})
                        assert entry['magnitude_comparison']['max_absolute_difference']<=.002,entry
                        assert entry['phase_comparison']['max_absolute_difference']<=.011,entry
                    else:
                        entry['sample_measurement']=await call('measure_samples',{'path':path,'column':'VDB(2)' if kind=='rc_ac' else 'V(2)'})
                        assert entry['sample_measurement']['sample_count']>=300,entry
                        p=entry['parameters']
                        if kind=='rc_ac':
                            rows=measured_table(Path(path),0)['rows']
                            db_error=max(abs(row[1]+10*math.log10(1+(2*math.pi*row[0]*p['r_ohm']*p['c_farad'])**2)) for row in rows)
                            phase_error=max(abs(row[2]+math.degrees(math.atan(2*math.pi*row[0]*p['r_ohm']*p['c_farad']))) for row in rows)
                            entry['theory_comparison']={'max_db_error':db_error,'max_phase_error_deg':phase_error,
                                                       'basis':'Theory evaluated at rounded printed frequency; thresholds allow .PRINT rounding.'}
                            assert db_error<.055 and phase_error<.025,entry
                        elif kind=='rc':
                            theory=p['amplitude_v']/math.sqrt(1+(2*math.pi*p['frequency_hz']*p['r_ohm']*p['c_farad'])**2)
                            stats=entry['sample_measurement']
                            entry['theory_comparison']={'predicted_peak_v':theory,'positive_peak_error_v':abs(stats['maximum']-theory),
                                                       'negative_peak_error_v':abs(stats['minimum']+theory)}
                            assert max(abs(stats['maximum']-theory),abs(stats['minimum']+theory))<.0002,entry
                        elif kind=='rectifier':
                            stats=entry['sample_measurement']
                            assert 4.1<stats['maximum']<4.5 and stats['minimum']>-.01,entry
                        elif kind=='clipper':
                            stats=entry['sample_measurement']
                            assert stats['maximum']<min(p['amplitude_v'],.75) and stats['minimum']>max(-p['amplitude_v'],-.75),entry
                            assert abs(stats['maximum']+stats['minimum'])<.001,entry
                if kind=='rectifier':
                    for group in (result['conditions'][0:4],result['conditions'][4:8],result['conditions'][8:12]):
                        assert group[-1]['sample_measurement']['peak_to_peak']<group[-2]['sample_measurement']['peak_to_peak'],group
                report['batches'].append(result)
                (folder/'evidence.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
                print(f'{kind}: {len(conditions)} real simulations completed',flush=True)
            for file,column in (('probe-ac-export.txt','db(v(n00105))'),('probe-transient-export.txt','V(N00119)')):
                path=ROOT/'tests'/'fixtures'/file
                read=await call('read_probe_export',{'path':str(path),'limit':2})
                stats=await call('measure_probe_export',{'path':str(path),'column':column})
                report['probe_exports'].append({'read':read,'measurement':stats})
            cancelled=await call('start_parameter_batch',{'kind':'rc','conditions':[{}]*30})
            report['cancellation_request']=await call('cancel_parameter_batch',{'batch_id':cancelled['batch_id']})
            for _ in range(100):
                cancelled=await call('parameter_batch_status',{'batch_id':cancelled['batch_id']})
                if cancelled['status']!='running':break
                await asyncio.sleep(.1)
            assert cancelled['status']=='cancelled',cancelled
            assert any(c['status']=='not_run' for c in cancelled['conditions']),cancelled
            report['cancellation_result']=cancelled
    report['source_hashes']={name:digest((ROOT/name).read_bytes()) for name in ('core.py','server.py','batch.py','capture_automation.py','probe_data.py')}
    report['completed_unix']=time.time()
    (folder/'evidence.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(f'Evidence: {folder}',flush=True)


if __name__=='__main__':
    asyncio.run(main())
