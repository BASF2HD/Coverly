"""Run on each target OS/architecture; never bundle an artwork archive."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import platform
import plistlib
import shutil
import ssl
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def version():
    return tomllib.loads((ROOT/'pyproject.toml').read_text('utf-8'))['project']['version']


def windows_version_file(path, app_version):
    numbers=tuple(int(n) for n in app_version.split('.'))+(0,)
    path.write_text("VSVersionInfo(ffi=FixedFileInfo(filevers="+repr(numbers)+", prodvers="+repr(numbers)+", mask=0x3f, flags=0, OS=0x40004, fileType=1, subtype=0, date=(0,0)), kids=[StringFileInfo([StringTable('040904b0', [StringStruct('CompanyName', 'Coverly'), StringStruct('FileDescription', 'Coverly artwork archive'), StringStruct('FileVersion', "+repr(app_version)+"), StringStruct('ProductVersion', "+repr(app_version)+"), StringStruct('ProductName', 'Coverly'), StringStruct('OriginalFilename', 'Coverly.exe')])]), VarFileInfo([VarStruct('Translation', [1033,1200])])])",encoding='utf-8')


def windows_setup(output, app_version):
    compiler=shutil.which('ISCC.exe')
    if not compiler:
        candidate=Path(os.environ.get('ProgramFiles(x86)',r'C:\Program Files (x86)'))/'Inno Setup 6'/'ISCC.exe'
        if candidate.is_file():compiler=str(candidate)
    if not compiler:
        raise RuntimeError('Install Inno Setup 6 to build Setup, or use --portable-only for only the portable ZIP.')
    subprocess.run([compiler,'/DAppVersion='+app_version,'/DAppSource='+str(output/'Coverly'),
                    '/DAppOutput='+str(ROOT/'dist'),str(ROOT/'desktop'/'windows-setup.iss')],check=True)
    return ROOT/'dist'/f'Coverly-{app_version}-windows-x64-Setup.exe'


def checksums(artifacts, label, app_version):
    records=[]
    for artifact in artifacts:
        sha=hashlib.sha256(artifact.read_bytes()).hexdigest()
        artifact.with_name(artifact.name+'.sha256').write_text(sha+'  '+artifact.name+'\n',encoding='utf-8')
        records.append({'file':artifact.name,'bytes':artifact.stat().st_size,'sha256':sha})
    commit=subprocess.run(['git','rev-parse','HEAD'],cwd=ROOT,capture_output=True,text=True)
    report={'version':app_version,'platform':label,'commit':commit.stdout.strip() if commit.returncode==0 else None,'artifacts':records}
    (ROOT/'dist'/f'Coverly-{app_version}-{label}-build.json').write_text(json.dumps(report,indent=2),encoding='utf-8')


def build():
    parser = argparse.ArgumentParser()
    parser.add_argument('--skip-dmg', action='store_true')
    parser.add_argument('--portable-only',action='store_true')
    parser.add_argument('--expected-platform',choices=['macos-arm64','macos-x64','windows-x64'])
    args = parser.parse_args()
    system = platform.system()
    if system not in {'Darwin', 'Windows'}:
        parser.error('Build the macOS app on macOS, or the Windows EXE on Windows.')
    arch = platform.machine().lower()
    arch = 'arm64' if arch in {'arm64','aarch64'} else 'x64' if arch in {'amd64','x86_64'} else arch
    if arch not in {'arm64','x64'}:
        parser.error('Use a native 64-bit Python installation.')
    label = ('macos' if system == 'Darwin' else 'windows') + '-' + arch
    if system=='Windows' and arch!='x64':parser.error('Windows packages currently require a native x64 Python runtime.')
    if args.expected_platform and args.expected_platform!=label:
        parser.error(f'Expected {args.expected_platform}, but this Python runtime is {label}. Use a native interpreter.')
    app_version=version()
    output = ROOT / 'dist' / label
    work = ROOT / 'build' / label
    work.mkdir(parents=True, exist_ok=True)
    from PIL import Image, ImageDraw
    image = Image.new('RGBA', (1024,1024))
    d = ImageDraw.Draw(image)
    d.rounded_rectangle((16,16,1008,1008), radius=280, fill='#d7ef7b')
    d.ellipse((180,180,844,844), outline='#192012', width=64)
    d.ellipse((410,410,614,614), fill='#192012')
    d.arc((308,308,716,716), 180,270, fill='#192012', width=48)
    d.arc((308,308,716,716), 0,90, fill='#192012', width=48)
    icon = work / ('Coverly.icns' if system == 'Darwin' else 'Coverly.ico')
    image.save(icon)
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed', '--onedir',
               '--name','Coverly', '--distpath',str(output), '--workpath',str(work / 'pyinstaller'), '--specpath',str(work),
               '--icon',str(icon), '--paths',str(ROOT),
               '--add-data',str(ROOT / 'coverly' / 'static') + ':coverly/static',
               '--add-data',str(ROOT / 'coverly' / 'migrations') + ':coverly/migrations',
               '--add-data',str(ROOT / 'browser-bridge') + ':browser-bridge',
               '--collect-submodules','uvicorn', '--collect-submodules','sqlalchemy.dialects.sqlite',
               '--collect-data','indic_transliteration', '--hidden-import','webview',
               '--exclude-module','pytest', '--exclude-module','torch', '--exclude-module','torchvision',
               '--exclude-module','spandrel', '--exclude-module','cv2']
    # Some managed Python distributions ship an additional CA policy file
    # beside ssl.py. Keep that runtime policy intact in the frozen build.
    ssl_policy = Path(ssl.__file__).with_name('codex-rejected-cas.pem')
    if ssl_policy.is_file():
        command += ['--add-data', str(ssl_policy) + ':.']
    if system == 'Darwin':
        command += ['--target-architecture', 'arm64' if arch == 'arm64' else 'x86_64',
                    '--osx-bundle-identifier','com.coverly.archive']
    else:
        resource=work/'version.txt';windows_version_file(resource,app_version)
        command+=['--version-file',str(resource)]
    subprocess.run([*command, str(ROOT / 'desktop' / 'entry.py')], cwd=ROOT, check=True)
    if system=='Darwin':
        bundle=output/'Coverly.app'
        metadata=bundle/'Contents'/'Info.plist'
        with metadata.open('rb') as stream:info=plistlib.load(stream)
        info.update(CFBundleShortVersionString=app_version,CFBundleVersion=app_version)
        with metadata.open('wb') as stream:plistlib.dump(info,stream)
        subprocess.run(['codesign','--force','--sign','-',str(bundle)],check=True)
    executable = output / ('Coverly.app/Contents/MacOS/Coverly' if system == 'Darwin' else 'Coverly/Coverly.exe')
    report = output / 'self-test.json'
    subprocess.run([str(executable), '--self-test-report', str(report)], cwd=ROOT, check=True, timeout=120)
    subprocess.run([str(executable), '--backend-self-test-report', str(output / 'backend-self-test.json')], cwd=ROOT, check=True, timeout=180)
    result = json.loads(report.read_text('utf-8'))
    if not result.get('ok'):
        raise RuntimeError('Frozen application self-test failed')
    artifact = ROOT / 'dist' / (f'Coverly-{app_version}-' + label)
    artifacts=[]
    if system == 'Darwin' and not args.skip_dmg:
        stage = work / 'dmg'
        if stage.exists():
            shutil.rmtree(stage)  # Generated staging only; never the archive.
        stage.mkdir(exist_ok=True)
        shutil.copytree(output / 'Coverly.app', stage / 'Coverly.app', dirs_exist_ok=True, symlinks=True)
        applications = stage / 'Applications'
        if not applications.exists():
            applications.symlink_to('/Applications', target_is_directory=True)
        subprocess.run(['hdiutil','create','-volname','Coverly','-srcfolder',str(stage),'-ov','-format','UDZO',str(artifact) + '.dmg'], check=True)
        artifacts.append(Path(str(artifact)+'.dmg'))
    else:
        source = output / ('Coverly.app' if system == 'Darwin' else 'Coverly')
        # ditto preserves macOS framework symlinks and executable permissions.
        if system == 'Darwin':
            subprocess.run(['ditto','-c','-k','--sequesterRsrc','--keepParent',str(source),str(artifact) + '.zip'], check=True)
        else:
            artifact=Path(str(artifact)+'-Portable')
            with zipfile.ZipFile(str(artifact) + '.zip','w',zipfile.ZIP_DEFLATED) as archive:
                for path in source.rglob('*'):
                    if path.is_file():
                        archive.write(path,path.relative_to(output))
        artifacts.append(Path(str(artifact)+'.zip'))
    if system=='Windows' and not args.portable_only:artifacts.append(windows_setup(output,app_version))
    checksums(artifacts,label,app_version)
    print(json.dumps(result, indent=2))
    print('Desktop output: ' + str(output))


if __name__ == '__main__':
    build()
