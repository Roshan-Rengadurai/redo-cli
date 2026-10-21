# Releasing redo

Everything here is run by a human; nothing pushes automatically.

## 1. Pre-flight

```sh
qa/gate.sh                       # must not get worse than qa/README.md's baseline
```
Bump `version` in `pyproject.toml` and `__version__` in `redo_cli/__init__.py`.

## 2. Build the release files

```sh
python3 packaging/render.py
```
Builds `dist/redo_cli-X.Y.Z.tar.gz` (Homebrew builds from it) and
`dist/redo_cli-X.Y.Z-py3-none-any.whl` (Chocolatey installs it offline), and
renders `dist/homebrew/redo-cli.rb` and `dist/chocolatey/redo-cli/` pointing at
the GitHub release URLs with their sha256s. **Upload exactly these two files** --
rebuilding changes the hashes.

## 3. GitHub release

```sh
git tag vX.Y.Z && git push origin main vX.Y.Z
gh release create vX.Y.Z dist/redo_cli-X.Y.Z.tar.gz dist/redo_cli-X.Y.Z-py3-none-any.whl --title vX.Y.Z --generate-notes
```

## 4. Homebrew (tap repo `Roshan-Rengadurai/homebrew-tap`)

```sh
cp dist/homebrew/redo-cli.rb ../homebrew-tap/Formula/redo-cli.rb
cd ../homebrew-tap && git commit -am "redo-cli X.Y.Z" && git push
```
Users: `brew install roshan-rengadurai/tap/redo-cli`.
Not eligible for homebrew-core under this name: core already has `redo`
(a make alternative) with the same executable, hence `conflicts_with`.

## 5. Chocolatey (on Windows, with your community.chocolatey.org API key)

```powershell
cd dist\chocolatey\redo-cli
choco pack
choco push redo-cli.X.Y.Z.nupkg --source https://push.chocolatey.org/ --api-key <your key>
```
New packages go through human moderation (usually a few days).
Users: `choco install redo-cli`.
