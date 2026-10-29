"""File-backed handoff between a worker's browser and the local web UI."""
import json
import re
import time
from .storage import atomic_write


def directory(settings, job_id):
    if not re.fullmatch(r'[a-f0-9]{32}', job_id):
        raise ValueError('Invalid browser session')
    return settings.data / '.browser' / 'sessions' / job_id


def state(settings, job_id):
    try:
        value = json.loads((directory(settings, job_id) / 'state.json').read_text('utf-8'))
        if time.time() - value.get('updated', 0) > 15:
            value['waiting'] = False
        return value
    except (FileNotFoundError, ValueError):
        return {'waiting': False}


def publish(settings, job_id, value):
    atomic_write(directory(settings, job_id) / 'state.json', json.dumps({**value, 'updated': time.time()}).encode())
