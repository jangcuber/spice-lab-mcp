# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Jang cuber
"""Manual, human/model-observed GUI verification client. Not an automation macro.

Each stdin JSON line requests exactly one tool through the official MCP SDK.
Raw returned screenshots and tool-call metadata are retained for the audit.
"""
import asyncio
import base64
import json
from pathlib import Path
import sys
import argparse
from datetime import datetime

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

BASE = Path(__file__).resolve().parent


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--request-dir', type=Path)
    opts = parser.parse_args()
    if opts.request_dir:
        opts.request_dir.mkdir(parents=True, exist_ok=False)
    audit = BASE / 'runtime' / 'gui-verification' / datetime.now().strftime('%Y%m%d-%H%M%S')
    audit.mkdir(parents=True, exist_ok=False)
    params = StdioServerParameters(command=sys.executable, args=[str(BASE / 'server.py')], cwd=str(BASE))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            result = await session.initialize()
            print(json.dumps({'ready': True, 'audit': str(audit), 'server': result.serverInfo.model_dump()}), flush=True)
            sequence = 0
            while True:
                if opts.request_dir:
                    request_path = opts.request_dir / f'{sequence+1:03d}-request.json'
                    while not request_path.is_file():
                        await asyncio.sleep(.1)
                    line = request_path.read_text(encoding='utf-8')
                else:
                    line = await asyncio.to_thread(sys.stdin.readline)
                if not line:
                    break
                request = json.loads(line)
                if request.get('close'):
                    break
                sequence += 1
                try:
                    result = await session.call_tool(request['name'], request.get('arguments', {}))
                    response = result.model_dump(mode='json', exclude_none=True)
                    record = json.loads(json.dumps(response))
                    for index, block in enumerate(record.get('content', [])):
                        if block['type'] == 'image':
                            path = audit / f'{sequence:03d}-{request["name"]}-{index}.png'
                            path.write_bytes(base64.b64decode(block.pop('data')))
                            block['saved_path'] = str(path)
                    record = {'sequence': sequence, 'request': request, 'response': record}
                    with (audit / 'calls.jsonl').open('a', encoding='utf-8') as log:
                        log.write(json.dumps(record, ensure_ascii=False) + '\n')
                    reply = {'id': request['id'], 'result': response}
                except Exception as exc:
                    reply = {'id': request['id'], 'error': str(exc)}
                if opts.request_dir:
                    target = opts.request_dir / f'{sequence:03d}-response.json'
                    temporary = target.with_suffix('.pending')
                    temporary.write_text(json.dumps(reply), encoding='utf-8')
                    temporary.rename(target)
                else:
                    print(json.dumps(reply), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
