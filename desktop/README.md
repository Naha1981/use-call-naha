# NahaLabs Desktop Agent

Naha's Windows desktop-first mode is intentionally separate from telephony.

## What works

- Hold **Ctrl + Shift + Space** to talk.
- Local speech-to-text with faster-whisper.
- Local reasoning with Ollama.
- Naha receives a **fresh screenshot on every planning step**.
- Naha can execute small, allow-listed mouse/keyboard actions.
- After each action, Naha sees the screen again and can continue until the task is verified.
- Local speech with Piper.
- Type a task into the window when you want to test without the microphone.
- Phone calling is **not exposed by the desktop UI yet**. Telephony remains a separate later gate.

## Setup

From a PowerShell window opened at the repository root:

```powershell
.\scripts\setup-desktop.ps1
.\scripts\start-desktop.ps1
```

The setup script creates `.venv`, installs the desktop dependencies, downloads the Piper voice, installs Ollama when `winget` is available, and pulls the local text and vision models.

## First desktop tests

Say or type these one at a time:

```text
Look at my screen and tell me what is open.
Open Notepad.
Type: Naha is controlling this computer.
Open the browser.
Go to https://www.nahalabs.co.za
```

The agent is designed to inspect the screen before choosing coordinates. It will not claim success until a desktop action reports success.

## Safety boundary

The computer-control layer is allow-listed. It can click, double-click, right-click, move, scroll, type, press a small set of navigation keys, open a small set of applications, and open HTTP(S) URLs. Destructive hotkeys and force-close shortcuts are blocked.

`pyautogui` fail-safe mode is enabled. Moving the mouse to the top-left corner can trigger PyAutoGUI's emergency stop.

## Troubleshooting

Check:

```powershell
ollama list
.\.venv\Scripts\python.exe -m py_compile desktop\naha_desktop.py
```

For microphone or speaker problems, use Windows Sound settings to confirm the intended input/output devices are selected.
