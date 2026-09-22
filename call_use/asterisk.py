"""Asterisk AMI telephony provider for local/SIM-based calling.

This adapter deliberately controls Asterisk rather than a paid PSTN API. The
SIM, modem and carrier remain outside Python; Asterisk owns the call leg and
routes media to the local AudioSocket agent.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from dataclasses import dataclass
from typing import Mapping

from call_use.telephony import DialRequest, DialResult, TelephonyProvider


class AsteriskAMIError(RuntimeError):
    """Raised when Asterisk Manager Interface rejects an operation."""


@dataclass(frozen=True)
class AsteriskConfig:
    host: str = "127.0.0.1"
    port: int = 5038
    username: str = "naha"
    password: str = ""
    modem_sim_identifier: str = ""
    outbound_context: str = "naha-outbound"
    timeout_ms: int = 30000

    @classmethod
    def from_env(cls) -> "AsteriskConfig":
        return cls(
            host=os.getenv("ASTERISK_AMI_HOST", "127.0.0.1"),
            port=int(os.getenv("ASTERISK_AMI_PORT", "5038")),
            username=os.getenv("ASTERISK_AMI_USER", "naha"),
            password=os.getenv("ASTERISK_AMI_PASSWORD", ""),
            modem_sim_identifier=os.getenv("ASTERISK_MODEMMANAGER_SIM", ""),
            outbound_context=os.getenv("ASTERISK_OUTBOUND_CONTEXT", "naha-outbound"),
            timeout_ms=int(os.getenv("ASTERISK_ORIGINATE_TIMEOUT_MS", "30000")),
        )


class AsteriskAMI:
    """Minimal dependency-free AMI client for originate and hangup."""

    def __init__(self, config: AsteriskConfig | None = None) -> None:
        self.config = config or AsteriskConfig.from_env()

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if not self.config.password:
            raise AsteriskAMIError("ASTERISK_AMI_PASSWORD is not configured")
        try:
            reader, writer = await asyncio.open_connection(self.config.host, self.config.port)
            await self._read_message(reader)
            await self._send(
                writer,
                {
                    "Action": "Login",
                    "ActionID": secrets.token_hex(8),
                    "Username": self.config.username,
                    "Secret": self.config.password,
                    "Events": "off",
                },
            )
            response = await asyncio.wait_for(self._read_message(reader), 5)
            if response.get("Response") != "Success":
                await self._close(writer)
                raise AsteriskAMIError(response.get("Message", "AMI login failed"))
            return reader, writer
        except OSError as exc:
            raise AsteriskAMIError(
                f"Could not connect to Asterisk AMI at {self.config.host}:{self.config.port}: {exc}"
            ) from exc

    async def _close(self, writer: asyncio.StreamWriter) -> None:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    async def _send(self, writer: asyncio.StreamWriter, fields: Mapping[str, str]) -> None:
        payload = "".join(f"{key}: {value}\r\n" for key, value in fields.items()) + "\r\n"
        writer.write(payload.encode("utf-8"))
        await writer.drain()

    async def _read_message(self, reader: asyncio.StreamReader) -> dict[str, str]:
        values: dict[str, str] = {}
        while True:
            line = await asyncio.wait_for(reader.readline(), 5)
            if not line:
                raise AsteriskAMIError("Asterisk closed the AMI connection")
            text = line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not text:
                return values
            if ": " in text:
                key, value = text.split(": ", 1)
                values[key] = value

    async def originate(
        self,
        *,
        channel: str,
        context: str,
        extension: str,
        caller_id: str | None,
        timeout_ms: int | None = None,
    ) -> str:
        reader, writer = await self._open()
        action_id = "naha-" + secrets.token_hex(8)
        try:
            fields = {
                "Action": "Originate",
                "ActionID": action_id,
                "Channel": channel,
                "Context": context,
                "Exten": extension,
                "Priority": "1",
                "Timeout": str(timeout_ms or self.config.timeout_ms),
                "Async": "true",
                "Variable": f"NAHA_CALL_ID={action_id}",
            }
            if caller_id:
                fields["CallerID"] = caller_id
            await self._send(writer, fields)
            response = await asyncio.wait_for(self._read_message(reader), 5)
            if response.get("Response") != "Success":
                raise AsteriskAMIError(response.get("Message", "Originate rejected"))
            return action_id
        finally:
            await self._close(writer)

    async def hangup(self, channel: str) -> None:
        reader, writer = await self._open()
        try:
            await self._send(
                writer,
                {
                    "Action": "Hangup",
                    "ActionID": "naha-" + secrets.token_hex(8),
                    "Channel": channel,
                },
            )
            response = await asyncio.wait_for(self._read_message(reader), 5)
            if response.get("Response") != "Success":
                raise AsteriskAMIError(response.get("Message", "Hangup rejected"))
        finally:
            await self._close(writer)


class AsteriskTelephonyProvider(TelephonyProvider):
    """TelephonyProvider implementation backed by Asterisk + a cellular SIM."""

    def __init__(self, config: AsteriskConfig | None = None) -> None:
        self.config = config or AsteriskConfig.from_env()
        self.ami = AsteriskAMI(self.config)
        self._active: dict[str, str] = {}
        self._status: dict[str, str] = {}

    def _channel_for(self, number: str) -> str:
        if not self.config.modem_sim_identifier:
            raise AsteriskAMIError(
                "ASTERISK_MODEMMANAGER_SIM is not configured; run the modem setup first"
            )
        return f"ModemManager/{self.config.modem_sim_identifier}/{number}"

    async def dial(self, request: DialRequest) -> DialResult:
        channel = self._channel_for(request.to_number)
        provider_call_id = await self.ami.originate(
            channel=channel,
            context=self.config.outbound_context,
            extension="s",
            caller_id=request.from_number or None,
        )
        self._active[provider_call_id] = channel
        self._status[provider_call_id] = "dialing"
        return DialResult(provider_call_id=provider_call_id, status="dialing")

    async def hangup(self, provider_call_id: str) -> None:
        channel = self._active.get(provider_call_id)
        if not channel:
            raise AsteriskAMIError(f"Unknown Asterisk call {provider_call_id}")
        await self.ami.hangup(channel)
        self._status[provider_call_id] = "hangup"
        self._active.pop(provider_call_id, None)

    async def get_status(self, provider_call_id: str) -> str:
        return self._status.get(provider_call_id, "unknown")
