# usage: tc NAME 'SETUP (runs in $D)' 'FAILING CMD' ['HISTORY lines (\n sep), also run in hook session before CMD']
REDO_BIN=${REDO_BIN:+$(dirname "$REDO_BIN")}
REDO_BIN=${REDO_BIN:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/.venv/bin}
QA=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
export NO_COLOR=1 SHELL=/bin/zsh GIT_AUTHOR_NAME=qa GIT_AUTHOR_EMAIL=qa@x GIT_COMMITTER_NAME=qa GIT_COMMITTER_EMAIL=qa@x GIT_CONFIG_NOSYSTEM=1 HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_ANALYTICS=1
export PATH=$REDO_BIN:$PATH
MODES=${MODES:-run hook}
mkenv(){ H=$(mktemp -d "$QA/work/h.XXXX"); D=$H/w; mkdir -p $D; export HOME=$H REDO_HOME=$H/.redo HISTFILE=$H/hist; printf '[init]\n\tdefaultBranch = main\n' > $H/.gitconfig; (cd $D && eval "$1") >/dev/null 2>&1; }
tc(){ local name=$1 setup=$2 cmd=$3 hist=$4
  [ -n "$ONLY" ] && [[ $name != $ONLY* ]] && return
  echo "################ $name :: $cmd"
  for m in $MODES; do
    ( mkenv "$setup"
      if [ $m = run ]; then
        [ -n "$hist" ] && printf '%b\n' "$hist" > $HISTFILE
        cd $D; echo "--- run: stderr of cmd:"; redo run "$cmd" > $H/out 2>&1; head -6 $H/out | sed 's/^/    | /'
        echo "--- run: redo --no-push"; redo --no-push 2>&1 | sed 's/^/    /'
      else
        redo init >/dev/null 2>&1
        { echo 'source ~/.zshrc'; echo "PS1=''; PROMPT_EOL_MARK=''"; echo "cd $D"; [ -n "$hist" ] && printf '%b\n' "$hist"; echo "$cmd"; echo 'redo --no-push'; echo exit; } > $H/script
        echo "--- hook: redo --no-push"; zsh -f -i < $H/script 2>&1 | tr -d '\r' | sed -n '/You ran:/,$p;/No failed/p;/[Nn]othing/p' | grep -v '^ *$' | sed 's/^/    /'
      fi )
  done; }
