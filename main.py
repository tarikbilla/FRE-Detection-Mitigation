#!/usr/bin/env python3
"""FRE Detection & Mitigation Research Platform - application launcher.

    python3 main.py                 start the application (collector + live dashboard)
    python3 main.py --foreground    collect here even if the systemd service runs
    python3 main.py db-check        any other fre-platform command (db-check, buffer-status, decode, ...)

No setup needed: it switches to the project's virtual environment and finds the
settings file (/etc/fre-platform/fre.env, else ./.env) automatically.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"

# 1) Re-launch inside the project's virtual environment if we're not already in it.
venv_python = VENV / "bin" / "python"
if venv_python.exists() and Path(sys.prefix).resolve() != VENV.resolve():
    os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:]])

sys.path.insert(0, str(ROOT / "src"))

# 2) Pick up the settings file automatically.
if "FRE_ENV_FILE" not in os.environ:
    for candidate in (Path("/etc/fre-platform/fre.env"), ROOT / ".env"):
        if candidate.is_file() and os.access(candidate, os.R_OK):
            os.environ["FRE_ENV_FILE"] = str(candidate)
            break

from fre_platform.cli import build_parser, main  # noqa: E402

# 3) Default to the "run" command; global options (e.g. --log-level) may come first.
GLOBAL_WITH_VALUE = {"--env-file", "--log-level", "--log-format"}
COMMANDS = set(build_parser()._subparsers._group_actions[0].choices)  # noqa: SLF001

argv = sys.argv[1:]
i = 0
# skip only genuine global options (and their values)
while i < len(argv) and (argv[i] in GLOBAL_WITH_VALUE or argv[i].split("=")[0] in GLOBAL_WITH_VALUE):
    i += 1 if "=" in argv[i] else 2
if i >= len(argv) or (argv[i] not in COMMANDS and argv[i] not in ("-h", "--help", "--version")):
    argv = argv[:i] + ["run"] + argv[i:]

raise SystemExit(main(argv))
