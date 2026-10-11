# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jang cuber
"""Read literal numeric Probe TXT exports. No screenshots, curves or samples synthesized."""
import math
import re
from core import read_text, digest, statistics


def probe_table(path):
    lines=read_text(path).splitlines()
    header=next((i for i,s in enumerate(lines) if s.strip()),None)
    if header is None:
        raise ValueError('Empty export.')
    columns=lines[header].strip().split()
    if (len(columns)<2 or columns[0].casefold() not in ('time','frequency','freq') or
            len(set(columns)) != len(columns) or
            not all(re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*\([^\s]+\)',c) for c in columns[1:])):
        raise ValueError('Expected numeric Probe export header Time/Frequency and V/I trace columns.')
    rows, row_lines=[],[]
    for line_no,line in enumerate(lines[header+1:],header+2):
        if not line.strip():
            continue
        fields=line.split()
        if len(fields)!=len(columns):
            raise ValueError(f'Unexpected column count at line {line_no}; multiple exports must remain separate.')
        try:
            row=[float(v.replace('D','E').replace('d','e')) for v in fields]
        except ValueError as exc:
            raise ValueError(f'Non-numeric export at line {line_no}.') from exc
        if not all(math.isfinite(v) for v in row) or (rows and row[0] < rows[-1][0]):
            raise ValueError(f'Non-finite or decreasing X at line {line_no}. Do not combine sweeps.')
        rows.append(row)
        row_lines.append(line_no)
    if not rows:
        raise ValueError('No exported samples.')
    return {'columns':columns,'rows':rows,'row_lines':row_lines}


def measure_export(path,column,start=None,stop=None):
    t=probe_table(path)
    result=statistics(t,column,start,stop)
    result['basis']='Literal Probe exported samples; not cursor measurements. Export alone does not prove successful simulation.'
    return {'path':str(path),'sha256':digest(path.read_bytes()),**result}
