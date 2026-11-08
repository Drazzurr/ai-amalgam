# AI Amalgam — Phase 1: Claude + GPT discussion relay

Give Claude and GPT a goal. They discuss it with each other in one chat and stop to ask you
when they need a decision. It runs on your Claude Pro and ChatGPT Plus subscriptions, with
no API keys and no per-message charges.

## One-time setup (about 15 minutes)

### 1. Install Python
1. Go to https://www.python.org/downloads/ and download the latest Python 3 for Windows.
2. Run the installer and **tick "Add python.exe to PATH"** on the first screen, then click Install Now.

### 2. Install the two model tools
Open **PowerShell** (Start menu, type "PowerShell") and run these one at a time.

Claude Code (Anthropic's official tool):
```
irm https://claude.ai/install.ps1 | iex
```

Codex CLI (OpenAI's official tool):
```
irm https://chatgpt.com/codex/install.ps1 | iex
```

Close PowerShell and open a new one so Windows picks up the new commands.

### 3. Sign each tool in to your subscription
In the new PowerShell window:

1. Type `claude` and press Enter. When it asks how to log in, choose
   **"Claude account with subscription"** and sign in with your Claude Pro account in the browser.
   When you see the Claude prompt, type `/exit` to leave.
2. Type `codex` and press Enter. Choose **"Sign in with ChatGPT"** and sign in with your
   ChatGPT Plus account in the browser. Then press Ctrl+C to leave.

You only do this once; both tools remember the sign-in.

### 4. Create the desktop shortcut (once)
Double-click **Create Desktop Shortcut.bat**. It puts an **AI Amalgam** icon on your Desktop
and in the Start menu. (Tip: right-click it in the Start menu and choose *Pin to taskbar*.)

### 5. Start the app
Double-click the **AI Amalgam** shortcut. It opens in its own window with no console.
- Close the window, or click **Quit AI Amalgam** at the bottom left, and the app shuts down.
- Starting it again while it's already open just opens another window to the same app.
- **Try Demo Mode.bat** opens the app with stand-in models (uses none of your plans' usage).
- **AI Amalgam.bat** does the same as the shortcut, if you'd rather not make one.

The window is a Microsoft Edge "app window" (Edge comes with Windows; Chrome works too).
Your normal browser isn't involved.

## Using it
- **Start a discussion:** type a goal, e.g. "Plan a weekend Python project that teaches me
  SQL. Agree on one plan." Claude speaks first, then GPT, and so on.
- **When they need you:** a yellow box shows the question. Type your answer and send; the
  discussion continues.
- **Ask just one model:** start your message with `@claude` or `@gpt` (one reply, no discussion).
- **Max rounds** (right panel): one round = each model speaks once. Default is 4.
- **Stop** halts the model that's currently thinking.
- **Saved chats** are listed on the left and kept in `amalgam.db` in this folder.

## Working on a Google Doc
- Paste the doc's link into **Project doc** (right panel). Each chat remembers its own link.
- Claude can search and read your Google Drive and Docs (through the connectors on your Claude
  account) and quotes the relevant parts so GPT can follow. GPT can read Drive too if Google Drive
  is connected in your ChatGPT account (check with `/apps` inside `codex`).
- **Allow doc edits** (off by default): Claude may then edit Google Docs. The models agree on a
  change, Claude shows you the exact edit in a "Needs your input" box, and applies it only after
  you approve. Deleting, sharing or moving files is always blocked. Google Docs keeps version
  history (File > Version history) if you ever want to undo.

## Projects
- Click **+** next to **Projects** in the sidebar, type a name and press Enter.
- Chats you start inside a project are grouped under it. Click a project to open a new chat in it.
- The **Project** card (right panel) holds instructions both models follow in every chat of that project,
  and a default doc link for new chats. Deleting a project keeps its chats (they move to **Chats**).

## Managing chats
- Hover a chat in the sidebar: ✎ renames it, 🗑 deletes it.
- The project menu next to the chat title moves the open chat into (or out of) a project.

## Project roles and memory
- **Roles** (Project card): e.g. Claude = "lore keeper and editor", GPT = "critic: plot holes and rules".
  Each model brings its role's perspective instead of just agreeing.
- **Memory**: when you confirm a decision, the models save it as a short line (you'll see
  "Saved to project memory"). Every chat in the project starts from these lines. You can edit them.

## Attaching files
Click the paperclip, or drag files onto the chat (up to 25 MB each). They go with your next message,
and both models can open them: images like maps, PDFs, text notes, character sheets. Files are kept in
`workspace/attachments/` inside the app folder.

## Doc edits log
Every change Claude makes to a Google Doc is listed in the **Doc edits** card (and noted in the chat),
with an **Open doc** link. To undo one, open the doc and use File → Version history.

## Long chats
Once a chat gets long, older messages are folded into a short summary that the models read instead,
so each turn re-sends far less and your plan limits last longer. You still see the whole chat.

## When a plan limit runs out
If one model hits its plan limit, the discussion carries on with the other. The resting model shows
"resting until …" and rejoins automatically after its limit resets.

## Speed and live replies
- Replies appear as they're written, with a line showing what the model is doing
  (searching your Drive, reading a doc, writing...).
- Each model has **Fast / Balanced / Best** under its name (remembered between sessions):
  - Claude: Fast = Haiku, Balanced = your Claude Code default, Best = Opus (uses your limits fastest).
  - GPT: Fast = low reasoning effort, Balanced = your Codex default, Best = high reasoning effort.
  - If a tool doesn't accept the setting, the app quietly falls back to Balanced.

## Plan usage
- **GPT:** 5-hour and weekly limits with % left and reset times, read from Codex's own records on
  your PC (they update after each GPT reply).
- **Claude:** OK / Near limit / Limit reached, and when it resets. Anthropic doesn't give the exact %
  to background tools, so **Details** opens your Claude usage page for that.
- Both show how many replies each model has given in the app (last 5 hours / 7 days).

## Updates
At the bottom left of the sidebar: **v1.0.0 · Check for updates**. The app also checks once each time it
starts. When there's a new version, click **Update available** and it downloads it from
github.com/Drazzurr/ai-amalgam, replaces the program files and restarts. Your chats, projects,
settings (`amalgam.db`) and attachments (`workspace/`) are never touched, and the replaced files are
backed up in `%LOCALAPPDATA%\AIAmalgam\backups`.

## Staying at $0
- The app never uses API keys. It also hides `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and
  `CODEX_API_KEY` from the tools if they exist on your PC, so they can't switch you to
  pay-per-use billing.
- If either tool ever offers to buy extra usage, decline.
- When a plan's usage limit runs out, the app shows the error and pauses. Wait for the
  reset, then type "continue".

## Troubleshooting
| What you see | Fix |
| --- | --- |
| "'claude' not found" or "'codex' not found" in the right panel | Redo step 2, then open a new PowerShell window and check `claude --version` and `codex --version` work. Restart the app. |
| A model "returned an error" mentioning login or auth | Redo step 3 for that tool. |
| A model "returned an error" mentioning a limit | You've hit your plan's limit; wait for it to reset. |
| Nothing opens from the shortcut | Double-click **Troubleshoot (show console).bat** to see the error, or open `amalgam.log` in this folder. |
| "Couldn't find Python" when making the shortcut | Check Python is installed: in PowerShell, `py --version`. |
| The window shows "AI Amalgam has closed" | The app stopped (Quit was pressed or it closed itself). Start it again from the shortcut. |

## How it works (files)
- `app.py` – starts the local server (only reachable from your own PC), opens the app window, and shuts down when it closes.
- `orchestrator.py` – the turn loop: who speaks next, the prompt each model gets, pauses on NEED USER.
- `runners.py` – runs `claude -p` and `codex exec` with your sign-ins; both are talk-only (no file edits).
- `storage.py` – saves chats in `amalgam.db`.
- `ui/index.html` – the chat window.
- `workspace/` – the empty folder the tools run in.
- `amalgam.ico` – the app icon. `amalgam.log` – a log of each run, for troubleshooting.
- `tools/make-shortcut.ps1` – what Create Desktop Shortcut.bat runs.
