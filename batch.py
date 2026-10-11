# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jang cuber
"""Bounded sequential real-engine runs with explicit condition provenance."""
import json
import os
import re
import threading
import time
from core import parameters, template, validate_netlist


class Batches:
    def __init__(self, settings, jobs):
        self.settings, self.jobs = settings, jobs
        self.active = {}

    def _write(self, folder, data):
        with self.jobs.lock:
            tmp = folder/'batch.pending'
            tmp.write_text(json.dumps(data,indent=2),encoding='utf-8')
            os.replace(tmp, folder/'batch.json')

    def start(self, kind, conditions):
        if not isinstance(conditions,list) or not 1 <= len(conditions) <= 30:
            raise ValueError('Provide 1..30 parameter dictionaries (one isolated run per condition).')
        values = [parameters(kind,p) for p in conditions]
        circuits = [template(kind,p,self.settings) for p in values]
        for text in circuits:
            validate_netlist(text,self.settings)
        with self.jobs.lock:
            if self.jobs.active or self.jobs.reservation or self.jobs.starting or self.jobs.closing:
                raise ValueError('Engine is busy. Wait for the current job/batch.')
            folder = self.settings.new_dir('batches','sweep')
            data = {'batch_id':folder.name,'kind':kind,'status':'running','created_unix':time.time(),
                    'conditions':[{'index':i,'parameters':p,'status':'pending'} for i,p in enumerate(values)]}
            for i,text in enumerate(circuits):
                (folder/f'condition-{i:02d}.cir').write_text(text,encoding='ascii')
            event = threading.Event()
            self.active[folder.name] = event
            self.jobs.reservation = folder.name
            self._write(folder,data)
            threading.Thread(target=self._run,args=(folder,data,event),daemon=True).start()
            return json.loads(json.dumps(data))

    def _run(self,folder,data,event):
        try:
            for entry in data['conditions']:
                if event.is_set() or self.jobs.closing:
                    data['status']='cancelled'
                    break
                job = self.jobs.start(folder/f'condition-{entry["index"]:02d}.cir', owner=folder.name)
                entry.update(status='running',job_id=job['job_id'])
                self._write(folder,data)
                while True:
                    if event.is_set():
                        self.jobs.cancel(job['job_id'])
                    result = self.jobs.status(job['job_id'])
                    if result['status'] not in ('starting','running'):
                        break
                    time.sleep(.1)
                entry.update(status=result['status'],result=result)
                self._write(folder,data)
                if result['status'] != 'completed':
                    data['status']='cancelled' if event.is_set() else 'failed'
                    break
            else:
                data['status']='completed'
        except Exception as exc:
            data.update(status='failed',error=str(exc))
        finally:
            for entry in data['conditions']:
                if entry['status']=='pending':
                    entry['status']='not_run'
            data['finished_unix']=time.time()
            with self.jobs.lock:
                self._write(folder,data)
                self.active.pop(folder.name,None)
                if self.jobs.reservation==folder.name:
                    self.jobs.reservation=None

    def status(self,batch_id):
        if not re.fullmatch(r'\d{8}-\d{6}-sweep-[a-f0-9]{10}',batch_id):
            raise ValueError('Invalid batch_id.')
        folder = (self.settings.workspace/'batches'/batch_id).resolve(strict=True)
        if not folder.is_relative_to(self.settings.workspace):
            raise ValueError('Invalid batch path.')
        with self.jobs.lock:
            data=json.loads((folder/'batch.json').read_text(encoding='utf-8'))
            if data['status']=='running' and batch_id not in self.active:
                data['status']='interrupted_or_untracked'
        return data

    def cancel(self,batch_id):
        with self.jobs.lock:
            data=self.status(batch_id)
            event=self.active.get(batch_id)
            if event:
                event.set()
            return {'batch_id':batch_id,'stop_requested':bool(event),'status':data['status']}
