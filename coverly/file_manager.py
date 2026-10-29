"""Reveal an existing archive item without changing its contents."""
import platform
import subprocess
from pathlib import Path


def reveal(path: Path):
    if not path.exists():
        raise FileNotFoundError('This artwork or folder is no longer on disk. Refresh files and try again.')
    system = platform.system()
    if system == 'Darwin':
        command = ['open', '-R', str(path)] if path.is_file() else ['open', str(path)]
    elif system == 'Windows':
        command = ['explorer.exe', '/select,' + str(path)] if path.is_file() else ['explorer.exe', str(path)]
    else:
        command = ['xdg-open', str(path.parent if path.is_file() else path)]
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
