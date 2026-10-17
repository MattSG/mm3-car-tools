#!/bin/sh
# Open the MM3 car workshop: the repo's build venv if present, otherwise python3.
here=$(cd "$(dirname "$0")" && pwd)
if [ -x "$here/../../venv/bin/python" ]; then
    py="$here/../../venv/bin/python"
else
    py=python3
fi
exec "$py" "$here/launch_editor.py" "$@"
