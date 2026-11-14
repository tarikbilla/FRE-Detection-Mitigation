"""Guards against committing secrets again."""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# literal user:password@ in a URI; {...} template placeholders and <...> docs are not secrets
CRED_URI = re.compile(r"mongodb(\+srv)?://[^:/\s<>{}]+:[^@\s<>{}]+@")


def tracked_files():
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [ROOT / p for p in out.splitlines()]


def test_no_connection_string_with_password_is_tracked():
    # tests/ intentionally contains obviously fake URIs to exercise redaction
    offenders = []
    for f in tracked_files():
        if f.relative_to(ROOT).parts[0] == "tests" or not f.is_file() or f.stat().st_size > 2_000_000:
            continue
        for m in CRED_URI.finditer(f.read_text(errors="ignore")):
            if "<password>" not in m.group(0) and "****" not in m.group(0):
                offenders.append(str(f.relative_to(ROOT)))
    assert not offenders, f"credential-bearing MongoDB URI in tracked files: {offenders}"


def test_no_env_or_credential_files_tracked():
    names = [f.name for f in tracked_files()]
    assert not [n for n in names if n == ".env" or (n.endswith(".env") and n != ".env.example")]
    assert not [n for n in names if "credential" in n.lower()]


def test_gitignore_blocks_secrets():
    gi = (ROOT / ".gitignore").read_text().splitlines()
    assert ".env" in gi and "*.env" in gi and "*credentials*" in gi
