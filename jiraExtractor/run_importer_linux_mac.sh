#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 -m pip install -r requirements.txt
python3 validate_package.py "${1:-}"
python3 jira_destination_importer.py "$@"
