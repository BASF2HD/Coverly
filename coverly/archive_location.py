"""Local folder selection and non-destructive portable archive copying."""
import os
import string
import tempfile
from pathlib import Path
from .backup import make_backup,restore_backup


def browse(path=None):
    current=Path(path).expanduser().resolve() if path else Path.home()
    if not current.is_dir():raise ValueError('Choose an existing folder')
    entries=[]
    try:
        for child in current.iterdir():
            try:
                if not child.name.startswith('.') and child.is_dir():entries.append({'name':child.name,'path':str(child.resolve())})
            except OSError:continue
    except PermissionError:raise ValueError('This folder is not accessible to Coverly') from None
    places=[Path.home(),Path.home()/'Documents',Path.home()/'Downloads']
    if os.name=='nt':places += [Path(f'{letter}:/') for letter in string.ascii_uppercase if Path(f'{letter}:/').is_dir()]
    else:places += [Path('/'),Path('/Volumes'),Path('/mnt'),Path('/media')]
    return {'path':str(current),'parent':str(current.parent),'directories':sorted(entries,key=lambda c:c['name'].casefold())[:500],
            'places':[{'name':p.name or str(p),'path':str(p)} for p in places if p.is_dir()]}


def copy_archive(source,destination):
    source=Path(source).resolve();destination=Path(destination).expanduser().resolve()
    if source==destination or source in destination.parents or destination in source.parents:
        raise ValueError('Choose a separate folder outside the current archive')
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError('Copy destination must be an empty folder')
    with tempfile.TemporaryDirectory(prefix='coverly-relocate-') as temp:
        backup=Path(temp)/'archive.zip'
        make_backup(source,backup)
        restore_backup(backup,destination)
    return {'path':str(destination),'message':'Archive copied. The original is retained. Save settings and restart Coverly to use this copy; paused jobs stay paused.'}
