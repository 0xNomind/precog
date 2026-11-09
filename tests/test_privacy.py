"""Guard: the repository must never contain a developer's home-directory path.

Lessons, logs and screenshots are made on real machines; this keeps any
absolute home path (and with it a username) out of what gets published.
"""

import os
import re
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME_PATH = re.compile(
    "|".join([
        r"/" + r"Users/(?!me/|you/|<)[A-Za-z0-9._-]+",
        r"/" + r"home/(?!user/|you/|<)[a-z][a-z0-9_-]*/",
        r"[A-Za-z]:\\\\" + r"Users\\\\",
    ])
)
TEXT_EXT = {".py", ".md", ".yaml", ".yml", ".json", ".js", ".sh", ".toml", ".txt", ""}


def _tracked_files():
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        return [f for f in out.splitlines() if f]
    except Exception:
        found = []
        for base, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in {".git", ".venv", "__pycache__", ".pytest_cache"}]
            found += [os.path.relpath(os.path.join(base, f), ROOT) for f in files]
        return found


def test_no_home_directory_paths_in_repo():
    offenders = []
    for rel in _tracked_files():
        if os.path.splitext(rel)[1] not in TEXT_EXT:
            continue
        try:
            with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
                for n, line in enumerate(fh, 1):
                    if HOME_PATH.search(line):
                        offenders.append(f"{rel}:{n}")
        except (OSError, UnicodeDecodeError):
            continue
    assert not offenders, "home-directory paths found: " + ", ".join(offenders)
