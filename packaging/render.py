#!/usr/bin/env python3
"""Render the Homebrew formula for a release.

    python3 packaging/render.py            # URL points at the GitHub release asset
    python3 packaging/render.py --local    # file:// URL, for testing before release

Builds dist/redo_cli-<version>.tar.gz and the wheel (the exact files uploaded
to the GitHub release, so the sha256 is known before anything is pushed) and
writes dist/homebrew/redo-cli.rb.
"""
import hashlib
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKG = ROOT / "packaging"
DIST = ROOT / "dist"
REPO = "Roshan-Rengadurai/redo-cli"


def version() -> str:
    return re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)


def build(ver: str):
    """sdist for Homebrew (it builds from source); the wheel is for pip/pipx
    users installing from the release. Both get uploaded as-is."""
    sdist = DIST / ("redo_cli-%s.tar.gz" % ver)
    wheel = DIST / ("redo_cli-%s-py3-none-any.whl" % ver)
    for f in (sdist, wheel):
        f.unlink(missing_ok=True)
    if shutil.which("uv"):
        subprocess.run(["uv", "build", "-q", "-o", str(DIST)], cwd=ROOT, check=True)
    else:
        subprocess.run([sys.executable, "-m", "build", "-o", str(DIST)], cwd=ROOT, check=True)
    return sdist, wheel


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    ver = version()
    sdist, wheel = build(ver)
    url = (sdist.resolve().as_uri() if "--local" in sys.argv else
           "https://github.com/%s/releases/download/v%s/%s" % (REPO, ver, sdist.name))
    text = (PKG / "homebrew" / "redo-cli.rb.in").read_text()
    text = text.replace("@URL@", url).replace("@SHA256@", sha256(sdist))
    left = re.findall(r"@[A-Z0-9]+@", text)
    if left:
        sys.exit("unfilled placeholders: %s" % left)
    out = DIST / "homebrew" / "redo-cli.rb"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    for f in (sdist, wheel):
        print("%-8s %s\n         sha256 %s" % (f.suffix.lstrip("."), f, sha256(f)))
    print("wrote    %s" % out)


if __name__ == "__main__":
    main()
