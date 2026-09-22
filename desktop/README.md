# NahaLabs Desktop Agent

The desktop client is the Windows control surface for the local Naha agent.

Capabilities:
- Hold Ctrl+Shift+Space to talk.
- Transcribe locally with faster-whisper.
- Reason locally through Ollama.
- Speak locally through Piper.
- Capture the screen on request.
- Execute a small allow-list of computer actions.
- Start South African or Lesotho calls through the Asterisk provider.
- Receive inbound-call notifications through the local bridge.

Setup:
1. Run .\scripts\setup-desktop.ps1 in PowerShell.
2. Run .\scripts\start-desktop.ps1.
3. Install Ollama and pull a local model.

Telephony variables:
- ASTERISK_AMI_HOST
- ASTERISK_AMI_PORT
- ASTERISK_AMI_USER
- ASTERISK_AMI_PASSWORD
- ASTERISK_MODEMMANAGER_SIM
- NAHA_CALLER_ID

The SIM remains the mobile carrier billing/account boundary.
