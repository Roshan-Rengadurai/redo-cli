#!/usr/bin/env python3
"""Render the Homebrew formula and Chocolatey package for a release.

    python3 packaging/render.py            # URLs point at the GitHub release asset
    python3 packaging/render.py --local    # file:// URLs, for testing before release

Builds dist/redo_cli-<version>.tar.gz (the exact file uploaded to the GitHub
release, so its sha256 is known before anything is pushed) and writes:
    dist/homebrew/redo-cli.rb
    dist/chocolatey/redo-cli/...   (run `choco pack` in that folder on Windows)
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
    """sdist for Homebrew (it builds from source), wheel for Chocolatey (pip
    installs it offline). Both get uploaded to the GitHub release as-is."""
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


def url_for(path: pathlib.Path, ver: str, local: bool) -> str:
    if local:
        return path.resolve().as_uri()
    return "https://github.com/%s/releases/download/v%s/%s" % (REPO, ver, path.name)


def render(src: pathlib.Path, dst: pathlib.Path, **values) -> None:
    text = src.read_text()
    for k, v in values.items():
        text = text.replace("@%s@" % k.upper(), v)
    left = re.findall(r"@[A-Z0-9]+@", text)
    if left:
        sys.exit("unfilled placeholders in %s: %s" % (src.name, left))
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text)


def main() -> None:
    local = "--local" in sys.argv
    ver = version()
    sdist, wheel = build(ver)

    render(PKG / "homebrew" / "redo-cli.rb.in", DIST / "homebrew" / "redo-cli.rb",
           url=url_for(sdist, ver, local), sha256=sha256(sdist))

    choco = DIST / "chocolatey" / "redo-cli"
    shutil.rmtree(choco, ignore_errors=True)
    render(PKG / "chocolatey" / "redo-cli.nuspec.in", choco / "redo-cli.nuspec", version=ver)
    render(PKG / "chocolatey" / "tools" / "chocolateyinstall.ps1.in", choco / "tools" / "chocolateyinstall.ps1",
           version=ver, wheel_url=url_for(wheel, ver, local), wheel_sha256=sha256(wheel))
    shutil.copy(PKG / "chocolatey" / "tools" / "chocolateyuninstall.ps1", choco / "tools")
    shutil.copy(ROOT / "LICENSE", choco / "tools" / "LICENSE.txt")

    for f in (sdist, wheel):
        print("%-8s %s\n         sha256 %s" % (f.suffix.lstrip("."), f, sha256(f)))
    print("wrote    %s\n         %s" % (DIST / "homebrew" / "redo-cli.rb", choco))


if __name__ == "__main__":
    main()
