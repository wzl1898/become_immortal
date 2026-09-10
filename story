#!/bin/sh
# Resolve paths independently of the caller's current directory.
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$project_dir/.venv/bin/python" "$project_dir/backend/cli.py" "$@"
