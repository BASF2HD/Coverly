"""Optional container entry point; ordinary native installs never use Docker."""
import multiprocessing
import os
from . import runtime as cli


if __name__ == "__main__":
    multiprocessing.freeze_support()
    cli.run(bind_host="0.0.0.0")
