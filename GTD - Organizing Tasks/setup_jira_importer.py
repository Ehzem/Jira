# python setup_jira_importer.py

"""Install and verify the packages required by the Jira Excel importer."""

from __future__ import annotations

import subprocess
import sys


MINIMUM_PYTHON = (3, 10)
REQUIRED_PACKAGES = ("openpyxl",)


def run(command: list[str], description: str) -> None:
    print(f"\n{description}...")
    subprocess.run(command, check=True)


def main() -> int:
    print("Jira importer package setup")
    print(f"Python executable: {sys.executable}")
    print(f"Python version: {sys.version.split()[0]}")

    if sys.version_info < MINIMUM_PYTHON:
        print("\nERROR: Python 3.10 or newer is required.")
        print("Download it from: https://www.python.org/downloads/")
        return 1

    try:
        run(
            [sys.executable, "-m", "pip", "install", "--upgrade", *REQUIRED_PACKAGES],
            "Installing required package: openpyxl",
        )
    except subprocess.CalledProcessError:
        print("\nERROR: Package installation failed.")
        print("Check your internet connection, then run this script again.")
        return 1

    print("\nVerifying packages...")
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        print("ERROR: openpyxl could not be imported after installation.")
        return 1

    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("\nERROR: tkinter is unavailable.")
        if sys.platform == "win32":
            print(
                "Rerun the Python installer, choose Modify, and enable "
                "'tcl/tk and IDLE'."
            )
        else:
            print("Install your operating system's Tk package for Python 3.")
        return 1

    print("\nSUCCESS: All required packages are available.")
    print("You can now run jira_component_epic_task_import.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


