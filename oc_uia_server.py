"""Persistent JSON front-end for oc_uia.py so commands do not pay process start.

One request per line, one response per line. Requests:
  {"id": 1, "command": "inspect|start|send|cancel", "hwnd": null,
   "signature": null, "focus": false}
Requests are serialised: UI Automation is not reentrant and two overlapping
queries can block a provider. The caller owns the timeout and must kill this
process when a request stops answering.
"""
import json
import sys

from oc_uia import run


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        request_id = None
        try:
            request = json.loads(line)
            request_id = request.get('id')
            command = request.get('command')
            if command == 'quit':
                send({'id': request_id, 'ok': True, 'result': 'stopped'})
                return 0
            if command not in ('inspect', 'start', 'send', 'cancel'):
                raise ValueError('unknown_command')
            send(dict({'id': request_id, 'command': command},
                      **run(command, request.get('hwnd'),
                            signature=request.get('signature'),
                            focus=bool(request.get('focus')))))
        except Exception as exc:
            send({'id': request_id, 'state': 'unknown', 'ok': False,
                  'error': type(exc).__name__ + ': ' + str(exc)[:200]})


def send(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + '\n')
    sys.stdout.flush()


if __name__ == '__main__':
    sys.exit(main())
