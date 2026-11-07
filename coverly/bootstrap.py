"""Minimal setup before frozen multiprocessing dispatch; no GUI imports."""
import os
import sys


def prepare_streams():
    # Windows windowed bootloaders set these to None in every spawned child.
    # Uvicorn and other libraries require normal stream methods even without
    # a console. Backend children append to their coordinator's diagnostic log.
    if sys.stdout is not None and sys.stderr is not None:
        return
    stream = open(os.environ.get('COVERLY_BACKEND_LOG') or os.devnull,
                  'a', buffering=1, encoding='utf-8')
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
