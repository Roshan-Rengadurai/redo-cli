#!/bin/bash
# usage: fixture.sh DIR  -> builds DIR/repo (+ DIR/remote.git)
set -e
D=$1; mkdir -p $D; cd $D
git init -q --bare remote.git
mkdir repo; cd repo
git init -q -b main
git config user.email qa@example.test; git config user.name QA
printf '# Demo\n\nA demo project.\n' > README.md
mkdir src
printf 'from utils import add\n\ndef main():\n    print(add(1, 2))\n\nif __name__ == "__main__":\n    main()\n' > src/app.py
printf 'def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n' > src/utils.py
cat > package.json <<'J'
{"name":"demo","version":"1.0.0","private":true,"scripts":{"dev":"echo dev","test":"echo test","lint":"echo lint","build":"echo build"}}
J
printf 'build:\n\t@echo building\ntest:\n\t@echo testing\nclean:\n\t@echo cleaning\n.PHONY: build test clean\n' > Makefile
printf 'API_URL=http://localhost\n' > .env
git add -A; git commit -qm "initial"
printf '\nMore text.\n' >> README.md; git commit -qam "docs: more"
git branch develop; git branch feature/auth
git checkout -q feature/auth; printf '\ndef auth():\n    return True\n' >> src/utils.py; git commit -qam "feat: auth"; git checkout -q main
printf 'x' >> README.md; git commit -qam "tweak"
git remote add origin ../remote.git; git push -q origin main develop feature/auth 2>/dev/null
git branch -q -u origin/main
printf "# local edit\n" >> src/app.py
