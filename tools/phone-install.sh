#!/usr/bin/env bash
# AI Amalgam for the phone (Google Pixel "Linux Terminal" app, Debian).
# Install or repair everything with one line in the Terminal app:
#   bash <(curl -fsSL https://raw.githubusercontent.com/Drazzurr/ai-amalgam/main/tools/phone-install.sh)
# Safe to run again any time ("amalgam repair" does exactly that). Your chats are never touched.
set -u

APP="$HOME/ai-amalgam"
BIN="$HOME/.local/bin"
NPM_PREFIX="$HOME/.npm-global"
REPO="https://github.com/Drazzurr/ai-amalgam"
# Versions known to work with Amalgam (the same ones as on the PC). Newer ones are only
# used after you choose "Update model tools" in the app.
CLAUDE_VERSION="2.1.319"
CODEX_VERSION="0.167.0"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '    \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31mStopped: %s\033[0m\n' "$*"; printf 'Run the same line again to retry. Nothing was lost.\n'; exit 1; }

export PATH="$BIN:$NPM_PREFIX/bin:$PATH"
mkdir -p "$BIN"

say "1/6  System tools (Python, Git, Node)"
need=""
for p in python3 git curl node npm; do command -v "$p" >/dev/null 2>&1 || need="$need $p"; done
if [ -n "$need" ]; then
  sudo apt-get update -qq || die "Couldn't reach Debian's servers. Check Wi-Fi or mobile data."
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 git curl ca-certificates nodejs npm >/dev/null \
    || die "Installing system tools failed."
fi
ok "python $(python3 -c 'import sys;print(sys.version.split()[0])'), git $(git --version | awk '{print $3}'), node $(node --version)"

say "2/6  AI Amalgam"
if [ -d "$APP/.git" ]; then
  git -C "$APP" fetch -q --depth 1 origin main && git -C "$APP" reset -q --hard origin/main \
    || warn "Couldn't download the newest version; keeping the one you have."
elif [ -f "$APP/app.py" ]; then
  ok "already here (updates come through the app)"
else
  git clone -q --depth 1 "$REPO" "$APP" || die "Couldn't download AI Amalgam from GitHub."
fi
ok "version $(cat "$APP/VERSION" 2>/dev/null)"

say "3/6  Claude Code (Anthropic's official tool)"
if command -v claude >/dev/null 2>&1; then
  ok "installed: $(claude --version 2>/dev/null | head -1)"
else
  curl -fsSL https://claude.ai/install.sh | bash -s "$CLAUDE_VERSION" >/dev/null 2>&1 \
    || curl -fsSL https://claude.ai/install.sh | bash >/dev/null 2>&1 \
    || die "Installing Claude Code failed."
  command -v claude >/dev/null 2>&1 || die "Claude Code installed but can't be found."
  ok "installed: $(claude --version 2>/dev/null | head -1)"
fi

say "4/6  Codex (OpenAI's official tool)"
if command -v codex >/dev/null 2>&1; then
  ok "installed: $(codex --version 2>/dev/null | head -1)"
else
  npm config set prefix "$NPM_PREFIX" >/dev/null
  npm install -g --silent "@openai/codex@$CODEX_VERSION" >/dev/null 2>&1 \
    || npm install -g --silent @openai/codex >/dev/null 2>&1 \
    || die "Installing Codex failed."
  command -v codex >/dev/null 2>&1 || die "Codex installed but can't be found."
  ok "installed: $(codex --version 2>/dev/null | head -1)"
fi

say "5/6  The 'amalgam' command and auto-start"
cat > "$BIN/amalgam" <<'LAUNCH'
#!/usr/bin/env bash
# amalgam            start AI Amalgam (if it isn't running) and show how to open it
# amalgam stop       stop it
# amalgam login      sign Claude and Codex in to your plans
# amalgam repair     reinstall anything that's missing or broken (keeps your chats)
# amalgam log        show the last lines of the log
APP="$HOME/ai-amalgam"
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$PATH"
export DISABLE_AUTOUPDATER=1          # model tools change only when you choose to update them
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN OPENAI_API_KEY CODEX_API_KEY   # stay on your subscriptions
PORT=8765
running() { curl -s -o /dev/null -m 2 "http://127.0.0.1:$PORT/manifest.webmanifest"; }
code() { python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/AIAmalgam/phone.json')))['code'])" 2>/dev/null; }
case "${1:-}" in
  stop)
    pkill -f "app.py --phone" && echo "AI Amalgam stopped." || echo "It wasn't running."; exit 0 ;;
  log)
    tail -n 40 "$APP/amalgam.log"; exit 0 ;;
  repair)
    exec bash <(curl -fsSL https://raw.githubusercontent.com/Drazzurr/ai-amalgam/main/tools/phone-install.sh) ;;
  login)
    if [ ! -s "$HOME/.claude/.credentials.json" ]; then
      echo; echo "== Claude: choose 'Claude account with subscription'. Open the link it shows in Chrome,"
      echo "   sign in, copy the code, paste it here. When you see Claude's prompt, type /exit"
      claude
    else echo "Claude: already signed in."; fi
    if [ ! -s "$HOME/.codex/auth.json" ]; then
      echo; echo "== Codex: open the link it shows in Chrome, sign in to ChatGPT, and type the code."
      codex login --device-auth || codex login
    else echo "Codex: already signed in."; fi
    exit 0 ;;
esac
quiet=""; [ "${1:-}" = "--quiet" ] && quiet=1
if ! running; then
  cd "$APP" || { echo "AI Amalgam isn't installed. Run: amalgam repair"; exit 1; }
  setsid nohup python3 app.py --phone >/dev/null 2>&1 < /dev/null &
  for i in $(seq 1 30); do running && break; sleep 0.5; done
fi
if running; then
  if [ -z "$quiet" ] || [ ! -f "$HOME/AIAmalgam/.paired" ]; then
    echo
    echo "  AI Amalgam is running.  Open Chrome at:  localhost:$PORT"
    echo "  Pairing code (first time only):  $(code)"
    echo "  If the Terminal asks to allow port $PORT, tap Allow."
    echo
  else
    echo "AI Amalgam is running (localhost:$PORT)."
  fi
else
  echo "AI Amalgam didn't start. Type: amalgam log"
fi
LAUNCH
chmod +x "$BIN/amalgam"
if ! grep -q "AI Amalgam auto-start" "$HOME/.bashrc" 2>/dev/null; then
  cat >> "$HOME/.bashrc" <<'RC'

# AI Amalgam auto-start (opening the Terminal app starts it)
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$PATH"
case $- in *i*) command -v amalgam >/dev/null && amalgam --quiet ;; esac
RC
fi
ok "type 'amalgam' any time to see the link and code"

say "6/6  Checking everything"
python3 - "$APP" <<'PY' || die "AI Amalgam's files look damaged. Run 'amalgam repair'."
import sys, ast, os
for f in ("app.py", "orchestrator.py", "runners.py", "storage.py", "sync.py"):
    ast.parse(open(os.path.join(sys.argv[1], f), encoding="utf-8").read())
PY
ok "app files"
claude --version >/dev/null 2>&1 && ok "Claude Code runs" || warn "Claude Code doesn't run. Try: amalgam repair"
codex --version >/dev/null 2>&1 && ok "Codex runs" || warn "Codex doesn't run. Try: amalgam repair"

if [ ! -s "$HOME/.claude/.credentials.json" ] || [ ! -s "$HOME/.codex/auth.json" ]; then
  say "Sign in to your plans (once)"
  "$BIN/amalgam" login
fi

"$BIN/amalgam"
printf '\033[1mLast steps:\033[0m open Chrome → localhost:8765 → type the code → menu ⋮ → "Add to Home screen".\n'
printf 'Then connect Sync under Info → Sync with your GitHub key.\n\n'
