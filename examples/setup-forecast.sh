#!/usr/bin/env bash
# Create the Precog demo project as its own git repo (default: ~/precog-demo/forecast).
#
# The project keeps its dependencies in its own uv venv (.venv, Python 3.11+),
# like most real Python projects. The shell's default `python3` and `pytest`
# cannot run its tests, so an agent's first natural command fails and it has to
# find the fix. Precog learns that fix once; later sessions get it right first.
set -euo pipefail
src="$(cd "$(dirname "$0")/forecast" && pwd)"
dest="${1:-$HOME/precog-demo/forecast}"
rm -rf "$dest"
mkdir -p "$(dirname "$dest")"
cp -R "$src" "$dest"
cd "$dest"
uv venv --quiet --python 3.11 .venv
uv pip install --quiet --python .venv/bin/python -e '.[dev]'
printf '.venv/\n__pycache__/\n*.egg-info/\n.pytest_cache/\n' > .gitignore
git init -q && git add -A && git -c user.name=demo -c user.email=demo@example.invalid commit -qm "forecast"
echo "demo project ready: $dest"
