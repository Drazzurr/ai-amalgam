#!/usr/bin/env bash
# amalgam            start AI Amalgam (if it isn't running) and show how to open it
# amalgam stop       stop it
# amalgam restart    stop it and start it again
# amalgam login      sign Claude and Codex in to your plans
# amalgam repair     reinstall anything that's missing or broken (keeps your chats)
# amalgam log        show the last lines of the log
APP="$HOME/ai-amalgam"
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$PATH"
export DISABLE_AUTOUPDATER=1          # model tools change only when you choose to update them
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN OPENAI_API_KEY CODEX_API_KEY   # stay on your subscriptions
PORT=8765
running() { curl -s -o /dev/null -m 2 "http://127.0.0.1:$PORT/manifest.webmanifest"; }
svc() { [ -f /etc/systemd/system/ai-amalgam.service ] && [ -d /run/systemd/system ]; }
code() { python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/AIAmalgam/phone.json')))['code'])" 2>/dev/null; }
case "${1:-}" in
  stop)
    svc && sudo -n systemctl stop ai-amalgam 2>/dev/null
    pkill -f "app.py --phone"
    running && echo "It's still closing; give it a few seconds." || echo "AI Amalgam stopped."; exit 0 ;;
  restart)
    svc && sudo -n systemctl stop ai-amalgam 2>/dev/null
    pkill -f "app.py --phone"
    for i in $(seq 1 60); do running || break; sleep 1; done
    exec "$0" ;;
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
  started=""
  if svc; then      # Linux runs it as a service (it also starts by itself when the Terminal boots)
    sudo -n systemctl reset-failed ai-amalgam 2>/dev/null
    sudo -n systemctl start ai-amalgam 2>/dev/null && started=1
  fi
  if [ -z "$started" ]; then
    setsid nohup python3 app.py --phone >/dev/null 2>&1 < /dev/null &
  fi
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
    printf '\n  \033[1;32m✓ AI Amalgam is running.\033[0m Switch back to it (swipe up and pick AI Amalgam).\n\n'
  fi
else
  echo "AI Amalgam didn't start. Type: amalgam log"
fi
