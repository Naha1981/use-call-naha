# NahaLabs local SIM telephony

Supported host: Ubuntu 24.04 or 26.04.

Architecture:
Windows desktop -> Asterisk/ModemManager -> USB voice-capable cellular modem -> South African SIM -> mobile network.
 Asterisk forwards the live call to Naha over AudioSocket.

The open-source ModemManager channel driver is installed from its Ubuntu release package.

Required hardware:
- Voice-capable cellular USB modem.
- A SIM provisioned for voice calls.
- USB audio voice support exposed by the modem.

Useful checks:
- mmcli -L
- mmcli -m 0 | grep device
- mmcli -m 0 --sim 0
- asterisk -rx 'modemmanager list available'
- ss -ltnp | grep 9092
- systemctl status naha-audiosocket

Do not expose Asterisk AMI or the desktop bridge to the public internet. The first deployment keeps AMI on localhost.
