import hashlib
import json
import tomllib
import pytest
from scripts.build_site import build
from scripts.build_desktop import version, windows_version_file
from scripts.publish_release import verify_artifacts
from coverly import __version__


def test_package_version_and_page_downloads_agree(tmp_path):
    assert version()==__version__
    page=(build(tmp_path)/'index.html').read_text('utf-8')
    assert '{{VERSION}}' not in page
    for suffix in ['macos-arm64.dmg','macos-x64.dmg','windows-x64-Setup.exe','windows-x64-Portable.zip']:
        assert f'Coverly-{version()}-{suffix}' in page


def test_windows_version_resource_is_valid_python_expression(tmp_path):
    target=tmp_path/'version.txt'
    windows_version_file(target,'1.0.1')
    compile(target.read_text('utf-8'),str(target),'eval')
    assert "filevers=(1, 0, 1, 0)" in target.read_text('utf-8')


def test_release_requires_all_native_packages_and_matching_checksums(tmp_path):
    for label,extensions in [('macos-arm64',['.dmg']),('macos-x64',['.dmg']),('windows-x64',['-Portable.zip','-Setup.exe'])]:
        artifacts=[]
        for extension in extensions:
            name=f'Coverly-1.0.1-{label}'+extension
            raw=('test '+name).encode();(tmp_path/name).write_bytes(raw)
            artifacts.append({'file':name,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()})
            (tmp_path/(name+'.sha256')).write_text(hashlib.sha256(raw).hexdigest()+'  '+name+'\n')
        (tmp_path/f'Coverly-1.0.1-{label}-build.json').write_text(json.dumps({'version':'1.0.1','platform':label,'commit':'expected','artifacts':artifacts}))
    assert len(verify_artifacts(tmp_path,'1.0.1','expected'))==11
    with pytest.raises(ValueError,match='provenance'):verify_artifacts(tmp_path,'1.0.1','wrong')
    (tmp_path/'Coverly-1.0.1-windows-x64-Setup.exe').write_bytes(b'bad data')
    with pytest.raises(ValueError,match='checksum'):verify_artifacts(tmp_path,'1.0.1','expected')
