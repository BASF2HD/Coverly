"""Prepare a text-only public snapshot without private development reports or archives."""
import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def manifest(root=ROOT):
    policy = json.loads((root / '.public-source.json').read_text('utf-8'))
    tracked = subprocess.check_output(['git', 'ls-files', '--stage', '-z'], cwd=root).decode('utf-8')
    result = []
    for entry in tracked.split('\0'):
        if not entry:
            continue
        metadata, name = entry.split('\t', 1)
        path = Path(name)
        if path.parts[0] in policy['exclude_roots'] or path.name in policy['exclude_names']:
            continue
        if path.parts[0] == 'docs' and name not in policy['documentation']:
            continue
        mode = metadata.split()[0]
        if mode not in {'100644', '100755'}:
            raise ValueError('Only regular source files can be published: ' + name)
        raw = (root / path).read_bytes()
        value = raw.decode('utf-8')
        if path.suffix == '.html' and name.startswith('tests/fixtures/'):
            if len(raw) > 20000 or re.search(r'nonce|client_secret|payment|googletagmanager|google-analytics', value, re.I):
                raise ValueError('Replace captured fixture payload with a synthetic contract: ' + name)
        sha = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
        result.append({'path': name, 'mode': mode, 'sha': sha, 'bytes': len(raw), 'encoding': 'utf-8'})
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / 'output/desktop-build/public-source-manifest.json')
    args = parser.parse_args()
    records = manifest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(records), encoding='utf-8')
    print(f'Prepared {len(records)} public source files ({sum(r["bytes"] for r in records)} bytes)')
