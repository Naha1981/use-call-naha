#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo 'Run this script with sudo or as root.' >&2
  exit 1
fi

source /etc/os-release
if [[ ${ID} != ubuntu || ( ${VERSION_ID} != 24.04 && ${VERSION_ID} != 26.04 ) ]]; then
  echo 'Naha telephony bootstrap currently targets Ubuntu 24.04 or 26.04.' >&2
  exit 1
fi

NAHA_REPO_URL=${NAHA_REPO_URL:-https://github.com/Naha1981/use-call-naha.git}
NAHA_REPO_REF=${NAHA_REPO_REF:-nahalabs/naha-desktop-sa-agent}
NAHA_ROOT=${NAHA_ROOT:-/opt/naha-call}
NAHA_LLM_MODEL=${NAHA_LLM_MODEL:-llama3.2:3b}
NAHA_PIPER_MODEL=${NAHA_PIPER_MODEL:-en_US-lessac-medium}
NAHA_DESKTOP_EVENT_URL=${NAHA_DESKTOP_EVENT_URL:-http://127.0.0.1:8766}

apt-get update
apt-get install -y curl ca-certificates git python3 python3-venv python3-pip jq openssl alsa-utils modemmanager

ARCH=$(dpkg --print-architecture)
RELEASE_JSON=$(curl -fsSL https://api.github.com/repos/koreapyj/asterisk-chan-modemmanager/releases/latest)
DRIVER_URL=$(printf '%s' "$RELEASE_JSON" | python3 -c '
import json
import sys

version, arch = sys.argv[1:3]
data = json.load(sys.stdin)
assets = [a for a in data.get("assets", []) if a.get("name", "").endswith(".deb")]
preferred = []
for asset in assets:
    name = asset["name"].lower()
    score = 0
    if arch in name or (arch == "amd64" and "x86_64" in name):
        score += 10
    if version in name:
        score += 20
    if version == "24.04" and "noble" in name:
        score += 15
    if version == "26.04" and ("resolute" in name or "26.04" in name):
        score += 15
    preferred.append((score, asset["browser_download_url"], asset["name"]))
preferred.sort(reverse=True)
if not preferred or preferred[0][0] == 0:
    print("Could not identify a matching Ubuntu/AArch64/Amd64 driver package.", file=sys.stderr)
    for _, _, name in preferred:
        print(name, file=sys.stderr)
    raise SystemExit(2)
print(preferred[0][1])
' "$VERSION_ID" "$ARCH"
)

curl -fsSL "$DRIVER_URL" -o /tmp/asterisk-chan-modemmanager.deb
apt-get install -y /tmp/asterisk-chan-modemmanager.deb

usermod -aG audio,dialout asterisk || true

if ! getent group naha >/dev/null; then
  groupadd --system naha
fi
if ! id naha >/dev/null 2>&1; then
  useradd --system --gid naha --home-dir "$NAHA_ROOT" --shell /usr/sbin/nologin naha
fi
usermod -aG naha asterisk || true
mkdir -p "$NAHA_ROOT" /var/lib/naha/calls /var/lib/naha/piper
chown -R naha:naha "$NAHA_ROOT" /var/lib/naha
chgrp naha /var/lib/naha/calls
chmod 2770 /var/lib/naha/calls

if [[ ! -d "$NAHA_ROOT/.git" ]]; then
  git clone --branch "$NAHA_REPO_REF" --depth 1 "$NAHA_REPO_URL" "$NAHA_ROOT"
else
  git -C "$NAHA_ROOT" fetch --depth 1 origin "$NAHA_REPO_REF"
  git -C "$NAHA_ROOT" checkout -q "$NAHA_REPO_REF"
  git -C "$NAHA_ROOT" reset --hard -q "origin/$NAHA_REPO_REF"
fi
chown -R naha:naha "$NAHA_ROOT"

python3 -m venv "$NAHA_ROOT/.venv"
"$NAHA_ROOT/.venv/bin/python" -m pip install --upgrade pip
"$NAHA_ROOT/.venv/bin/python" -m pip install -r "$NAHA_ROOT/deploy/telephony/requirements.txt"
NAHA_PIPER_DATA_DIR=/var/lib/naha/piper
"$NAHA_ROOT/.venv/bin/python" -m piper.download_voices "$NAHA_PIPER_MODEL" --data-dir "$NAHA_PIPER_DATA_DIR"

if ! command -v ollama >/dev/null 2>&1; then
  curl -fsSL https://ollama.com/install.sh | sh
fi
systemctl enable --now ollama.service
ollama pull "$NAHA_LLM_MODEL"

install -m 0755 "$NAHA_ROOT/deploy/telephony/naha-notify-event" /usr/local/bin/naha-notify-event
install -m 0755 "$NAHA_ROOT/deploy/telephony/naha-call-context" /usr/local/bin/naha-call-context
install -m 0644 "$NAHA_ROOT/deploy/telephony/naha-audiosocket.service" /etc/systemd/system/naha-audiosocket.service

cat > /etc/default/naha-audiosocket <<EOF
NAHA_AUDIOSOCKET_HOST=127.0.0.1
NAHA_AUDIOSOCKET_PORT=9092
NAHA_CALL_CONTEXT_DIR=/var/lib/naha/calls
NAHA_OLLAMA_URL=http://127.0.0.1:11434
NAHA_LLM_MODEL=$NAHA_LLM_MODEL
NAHA_PIPER_MODEL=$NAHA_PIPER_MODEL
NAHA_PIPER_DATA_DIR=$NAHA_PIPER_DATA_DIR
NAHA_LOG_LEVEL=INFO
EOF
chmod 0640 /etc/default/naha-audiosocket
chown root:naha /etc/default/naha-audiosocket

install -d -m 0755 /etc/asterisk
install -m 0640 "$NAHA_ROOT/deploy/asterisk/extensions.conf" /etc/asterisk/extensions_naha.conf

if [[ -f /etc/asterisk/extensions.conf ]] && ! grep -Fq '#include extensions_naha.conf' /etc/asterisk/extensions.conf; then
  printf '\n#include extensions_naha.conf\n' >> /etc/asterisk/extensions.conf
fi

AMI_SECRET=$(openssl rand -hex 24)
EVENT_SECRET=$(openssl rand -hex 32)
cat > /etc/asterisk/manager_naha.conf <<EOF
[naha]
secret = $AMI_SECRET
deny = 0.0.0.0/0.0.0.0
permit = 127.0.0.1/255.255.255.255
read = system,call,command
write = system,call,command
EOF
chmod 0640 /etc/asterisk/manager_naha.conf
chown root:asterisk /etc/asterisk/manager_naha.conf

if [[ -f /etc/asterisk/manager.conf ]] && ! grep -Fq '#include manager_naha.conf' /etc/asterisk/manager.conf; then
  printf '\n#include manager_naha.conf\n' >> /etc/asterisk/manager.conf
fi

MODEM_ID=${NAHA_MODEM_IDENTIFIER:-$(mmcli -m 0 2>/dev/null | awk -F': ' '/device:/{print $2; exit}' || true)}
SIM_ICCID=${NAHA_SIM_ICCID:-$(mmcli -m 0 --sim 0 2>/dev/null | awk -F': ' '/iccid:/{print $2; exit}' || true)}
MANUFACTURER=$(mmcli -m 0 2>/dev/null | awk -F': ' '/manufacturer:/{print $2; exit}' || true)

if [[ -n "$MODEM_ID" && -n "$SIM_ICCID" ]]; then
  INIT_COMMAND=''
  if printf '%s' "$MANUFACTURER" | grep -qi quectel; then
    INIT_COMMAND='init_commands = AT+QPCMV=1,2'
  fi
  cat > /etc/asterisk/modemmanager.conf <<EOF
[modem1]
type = modem
identifier = $MODEM_ID
$INIT_COMMAND

[sim1]
type = sim
identifier = $SIM_ICCID
context = from-mobile
exten = s
EOF
  MODEM_READY=1
else
  cp "$NAHA_ROOT/deploy/asterisk/modemmanager.conf" /etc/asterisk/modemmanager.conf
  MODEM_READY=0
fi
chmod 0640 /etc/asterisk/modemmanager.conf
chown root:asterisk /etc/asterisk/modemmanager.conf

cat > /etc/default/naha-telephony <<EOF
ASTERISK_AMI_HOST=127.0.0.1
ASTERISK_AMI_PORT=5038
ASTERISK_AMI_USER=naha
ASTERISK_AMI_PASSWORD=$AMI_SECRET
ASTERISK_MODEMMANAGER_SIM=$SIM_ICCID
NAHA_DESKTOP_EVENT_URL=$NAHA_DESKTOP_EVENT_URL
NAHA_EVENT_SECRET=$EVENT_SECRET
EOF
chmod 0600 /etc/default/naha-telephony

cat > "$NAHA_ROOT/telephony-client.env" <<EOF
ASTERISK_AMI_HOST=127.0.0.1
ASTERISK_AMI_PORT=5038
ASTERISK_AMI_USER=naha
ASTERISK_AMI_PASSWORD=$AMI_SECRET
ASTERISK_MODEMMANAGER_SIM=$SIM_ICCID
EOF
chown naha:naha "$NAHA_ROOT/telephony-client.env"
chmod 0600 "$NAHA_ROOT/telephony-client.env"

systemctl daemon-reload
systemctl enable --now ModemManager.service
systemctl enable --now naha-audiosocket.service
systemctl enable --now asterisk.service

asterisk -rx 'dialplan reload' >/dev/null 2>&1 || true
asterisk -rx 'manager reload' >/dev/null 2>&1 || true

echo
echo 'Naha telephony bootstrap complete.'
echo "Repo: $NAHA_ROOT ($NAHA_REPO_REF)"
echo "Asterisk AMI client file: $NAHA_ROOT/telephony-client.env"
echo "Modem ready: $MODEM_READY"
echo
echo 'Checks:'
echo '  mmcli -L'
echo '  asterisk -rx "modemmanager list available"'
echo '  systemctl status naha-audiosocket --no-pager'
echo '  ss -ltn | grep 9092'
echo
if [[ $MODEM_READY -eq 0 ]]; then
  echo 'Connect the voice-capable USB modem with the SIM, then rerun this script.'
fi
