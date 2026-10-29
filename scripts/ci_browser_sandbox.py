"""Allow only the installed CI Chromium binaries to create sandbox namespaces."""
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path


def main():
    if sys.platform != 'linux' or os.environ.get('GITHUB_ACTIONS') != 'true':
        raise RuntimeError('This setup is only for disposable GitHub Linux runners')
    helper = Path('/opt/google/chrome/chrome-sandbox')
    if helper.is_file() and helper.stat().st_mode & stat.S_ISUID:
        with Path(os.environ['GITHUB_ENV']).open('a') as output:
            output.write('CHROME_DEVEL_SANDBOX=' + str(helper) + '\n')
        print('Using the installed Chrome sandbox helper')
        return
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        chromium = Path(playwright.chromium.executable_path)
    cache = chromium.parents[2].resolve()
    binaries = [chromium, *cache.glob('chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell')]
    if len(binaries) < 2 or not shutil.which('apparmor_parser'):
        raise RuntimeError('Install Chromium and AppArmor before configuring the CI sandbox')
    profile = 'abi <abi/4.0>,\ninclude <tunables/global>\n'
    for index, binary in enumerate(binaries):
        binary = binary.resolve()
        if not binary.is_file() or not binary.is_relative_to(cache) or any(c in str(binary) for c in '\n"'):
            raise ValueError('Unexpected CI browser executable')
        profile += f'profile coverly-ci-chromium-{index} "{binary}" flags=(unconfined) {{\n  userns,\n}}\n'
    # Chromium's documented per-executable AppArmor policy: no global sysctl
    # changes and no --no-sandbox fallback. Only this runner's browser paths.
    target = '/etc/apparmor.d/coverly-ci-chromium'
    subprocess.run(['sudo', 'tee', target], input=profile, text=True, stdout=subprocess.DEVNULL, check=True)
    subprocess.run(['sudo', 'apparmor_parser', '-r', target], check=True)
    print('Enabled sandbox namespaces for', len(binaries), 'specific CI browser executables')


if __name__ == '__main__':
    main()
