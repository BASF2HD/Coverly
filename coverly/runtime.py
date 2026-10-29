from __future__ import annotations

import argparse
import contextlib
import logging
import multiprocessing
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from .config import load_settings
from .db import open_db
from .jobs import Worker, recover


@contextlib.contextmanager
def application_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    path = root / ".application.lock"
    with path.open("a+b") as f:
        if path.stat().st_size == 0:
            f.write(b"0")
            f.flush()
        f.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Another Coverly application is already using this archive") from exc
        try:
            yield
        finally:
            f.seek(0)
            if os.name == "nt":
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f, fcntl.LOCK_UN)


def worker_main(stop, kinds=None, quantum=None):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    settings = load_settings()
    engine, sessions = open_db(settings.data)
    worker = Worker(settings, sessions, allowed_kinds=kinds, quantum=quantum)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    try:
        while not stop.is_set():
            if not worker.tick():
                stop.wait(.7)
    finally:
        engine.dispose()


def serve(bind_host="127.0.0.1", bind_port=8000):
    import uvicorn
    from .app import create_app
    uvicorn.run(create_app(), host=bind_host, port=bind_port, log_level="info", timeout_graceful_shutdown=3)


def run(bind_host="127.0.0.1", stop_event=None, bind_port=8000):
    def shutdown_signal(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, shutdown_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, shutdown_signal)
    settings = load_settings()
    with application_lock(settings.data):
        engine, sessions = open_db(settings.data)
        with sessions() as s:
            recover(s)
        ctx = multiprocessing.get_context("spawn")  # Same process model on Apple Silicon, Intel and Windows.
        stop = ctx.Event()
        lanes = ([('discover',)] * settings.discovery_concurrency + [('download',)] * settings.worker_concurrency
                 + [('identify',), ('restore','ocr','classify','organize')])
        workers = [ctx.Process(target=worker_main, args=(stop, kinds, 1)) for kinds in lanes]
        server = ctx.Process(target=serve, args=(bind_host, bind_port))
        for process in [server, *workers]:
            process.start()
        print("\nCoverly is opening at http://localhost:8000\nPress Ctrl+C to stop safely.\n", flush=True)
        try:
            while server.is_alive() and not (stop_event and stop_event.is_set()):
                (settings.data / ".worker-heartbeat").write_text(str(time.time()))
                for index, process in enumerate(workers):
                    if not process.is_alive():
                        with sessions() as s:
                            recover(s, str(process.pid))
                        replacement = ctx.Process(target=worker_main, args=(stop, lanes[index], 1))
                        replacement.start()
                        workers[index] = replacement
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            if server.is_alive():
                if os.name != "nt":
                    os.kill(server.pid, signal.SIGINT)
                else:
                    server.terminate()
            for process in [server, *workers]:
                process.join(timeout=8)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=3)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=3)
            with sessions() as s:
                recover(s)
            (settings.data / ".worker-heartbeat").unlink(missing_ok=True)
            engine.dispose()


def main():
    # Optional local credentials; never copy .env into the artwork archive.
    from .config import ROOT
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env", override=False)
    except ImportError:
        pass
    parser = argparse.ArgumentParser(description="Coverly portable Tamil vinyl archive")
    parser.add_argument("command", nargs="?", choices=["run", "migrate", "backup", "restore", "doctor"], default="run")
    parser.add_argument("path", nargs="?")
    parser.add_argument("--destination")
    args = parser.parse_args()
    settings = load_settings()
    if args.command == "run":
        run()
    elif args.command == "migrate":
        engine, _ = open_db(settings.data)
        engine.dispose()
        print("Database schema is up to date.")
    elif args.command == "doctor":
        from .restoration import hardware
        from .intelligence import ocr_capabilities
        import json
        print(json.dumps({"hardware": hardware(), "ocr": ocr_capabilities(), "storage": str(settings.data)}, indent=2))
    elif args.command == "backup":
        if not args.path:
            parser.error("backup requires an output ZIP path")
        from .backup import make_backup
        with application_lock(settings.data):
            make_backup(settings.data, Path(args.path).resolve())
        print("Backup written.")
    elif args.command == "restore":
        if not args.path or not args.destination:
            parser.error("restore requires a ZIP path and --destination empty-folder")
        from .backup import restore_backup
        restore_backup(Path(args.path).resolve(), Path(args.destination).resolve())
        print("Archive restored. Point config.json storage at the destination, then start Coverly.")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
