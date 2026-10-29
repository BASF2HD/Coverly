"""Publish only complete, checksum-verified native builds from this workflow."""
import hashlib
import json
import os
import subprocess
import tomllib
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def verify_artifacts(root, version, commit):
    expected={f'Coverly-{version}-macos-arm64.dmg',f'Coverly-{version}-macos-x64.dmg',
              f'Coverly-{version}-windows-x64-Portable.zip',f'Coverly-{version}-windows-x64-Setup.exe'}
    found=set()
    verified=[]
    for label in ['macos-arm64','macos-x64','windows-x64']:
        report=json.loads((root/f'Coverly-{version}-{label}-build.json').read_text('utf-8'))
        if report['version']!=version or report['platform']!=label or report['commit']!=commit:
            raise ValueError('Build provenance does not match this release: '+label)
        for artifact in report['artifacts']:
            name=artifact['file']
            if Path(name).name!=name or name not in expected:raise ValueError('Unexpected release artifact')
            path=root/name
            if hashlib.sha256(path.read_bytes()).hexdigest()!=artifact['sha256'] or path.stat().st_size!=artifact['bytes']:
                raise ValueError('Artifact checksum failed: '+name)
            sidecar=path.with_name(path.name+'.sha256')
            if sidecar.read_text('utf-8').split()[0]!=artifact['sha256']:raise ValueError('Checksum sidecar failed: '+name)
            found.add(name)
            verified.extend([path,sidecar])
        verified.append(root/f'Coverly-{version}-{label}-build.json')
    if found!=expected:raise ValueError('All four verified native packages are required before publishing')
    return sorted(verified)


def main():
    if os.environ.get('GITHUB_ACTIONS')!='true':raise RuntimeError('Publish from the verified GitHub Actions release job')
    version=tomllib.loads((ROOT/'pyproject.toml').read_text('utf-8'))['project']['version']
    repo=os.environ['GITHUB_REPOSITORY'];commit=os.environ['GITHUB_SHA'];tag='v'+version
    artifacts=verify_artifacts(ROOT/'dist',version,commit)
    existing=subprocess.run(['gh','release','view',tag,'--repo',repo],capture_output=True,text=True)
    if existing.returncode==0:
        print('Release already exists; existing public assets were not replaced. Bump the version for a new release.')
        return
    subprocess.run(['gh','release','create',tag,*map(str,artifacts),'--repo',repo,'--target',commit,
                    '--title','Coverly '+version,'--notes-file',str(ROOT/'docs'/'RELEASE_NOTES.md'),'--latest'],check=True)


if __name__=='__main__':main()
