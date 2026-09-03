# python setup_jira_importer_dependencies.py

#!/usr/bin/env python3
"""Install and verify dependencies for the Excel-to-Jira importer."""

from __future__ import annotations

import importlib
import subprocess
import sys


MINIMUM_PYTHON = (3, 10)
PIP_PACKAGES = ("openpyxl>=3.1,<4",)


def run(command: list[str], description: str) -> None:
    print(f"\n{description}...")
    subprocess.check_call(command)


def main() -> int:
    print("Excel-to-Jira importer dependency setup")
    print(f"Python executable: {sys.executable}")
    print(f"Python version: {sys.version.split()[0]}")

    if sys.version_info < MINIMUM_PYTHON:
        print(
            "\nERROR: Python 3.10 or newer is required. "
            "Install it from https://www.python.org/downloads/ and rerun this file."
        )
        return 1

    try:
        import pip  # noqa: F401
    except ImportError:
        run(
            [sys.executable, "-m", "ensurepip", "--upgrade"],
            "Installing pip",
        )

    run(
        [sys.executable, "-m", "pip", "install", "--upgrade", *PIP_PACKAGES],
        "Installing/updating required Python packages",
    )

    problems: list[str] = []

    try:
        openpyxl = importlib.import_module("openpyxl")
        print(f"\nOK: openpyxl {openpyxl.__version__}")
    except Exception as exc:
        problems.append(f"openpyxl could not be imported: {exc}")

    try:
        tkinter = importlib.import_module("tkinter")
        print(f"OK: tkinter (Tk {tkinter.TkVersion})")
    except Exception as exc:
        problems.append(
            "tkinter is unavailable. On Windows, reinstall/modify Python from "
            "python.org and include 'tcl/tk and IDLE'. "
            f"Technical detail: {exc}"
        )

    if problems:
        print("\nSETUP INCOMPLETE:")
        for problem in problems:
            print(f"- {problem}")
        return 1

    print("\nSUCCESS: All dependencies required by the Jira importer are available.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


