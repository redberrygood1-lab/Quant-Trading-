"""Day 4 environment bootstrap. Standard library only — runs on whatever
Python the member already has, on Mac or Windows.

Creates an isolated virtual environment at ~/.ptq-academy/venv and installs
the two packages the backtest needs. Prints the path to the venv's Python
on the last line so the caller can run the backtest with it.

Price bars do NOT come from a pip package any more. They come from
shared/ptq_data.py, which is standard library only and talks to whichever
free data key the member set up on day 1. Nothing to install for that.

Fails honestly: if Python is too old, if the venv cannot be created, or if
the install fails, it says exactly what happened in plain words and exits
non-zero. It never carries on with a broken environment.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

PACKAGES = ["pandas>=2.0,<4", "numpy>=1.24,<3", "Pillow>=10.1,<13"]
HOME = Path.home() / ".ptq-academy"
VENV = HOME / "venv"


def venv_python() -> Path:
    if os.name == "nt":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def fail(message: str) -> None:
    print("")
    print("SETUP FAILED")
    print(message)
    sys.exit(1)


def main() -> None:
    global HOME, VENV
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--academy-home", type=Path, default=HOME)
    args = parser.parse_args()
    HOME = args.academy_home.expanduser().resolve()
    VENV = HOME / "venv"
    if sys.version_info < (3, 10):
        fail(
            f"This needs Python 3.10 or newer. The Python running this is "
            f"{sys.version.split()[0]}.\n"
            "Install a current Python from python.org, then run this again."
        )

    py = venv_python()
    if not py.exists():
        print(f"Creating a private Python environment at {VENV}")
        HOME.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                [sys.executable, "-m", "venv", str(VENV)],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            fail(
                "Could not create the environment.\n"
                f"Python said: {(exc.stderr or exc.stdout or '').strip()[:600]}"
            )
        except FileNotFoundError:
            fail("Could not find a working Python to build the environment with.")

    if not py.exists():
        fail(f"The environment was created but {py} is not there. Nothing was run.")

    # Check what is already installed before spending time on pip.
    probe = subprocess.run(
        [str(py), "-c", "import pandas, numpy, PIL"],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        print("Installing pandas, numpy and Pillow in the private academy environment.")
        install = subprocess.run(
            [str(py), "-m", "pip", "install", "--quiet", "--upgrade", *PACKAGES],
            capture_output=True,
            text=True,
        )
        if install.returncode != 0:
            fail(
                "Could not install the packages.\n"
                f"pip said: {(install.stderr or install.stdout).strip()[-900:]}\n\n"
                "The usual cause is no internet connection or a company network "
                "blocking pip. Try again on a normal connection."
            )
        verify = subprocess.run(
            [str(py), "-c", "import pandas, numpy, PIL"],
            capture_output=True,
            text=True,
        )
        if verify.returncode != 0:
            fail(
                "The packages installed but will not import.\n"
                f"Python said: {verify.stderr.strip()[-600:]}"
            )

    print("Environment ready.")
    print(str(py))


if __name__ == "__main__":
    main()
