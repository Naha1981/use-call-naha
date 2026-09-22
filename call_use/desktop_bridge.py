"""Loopback desktop API and telephony event bridge."""

from __future__ import annotations

import asyncio
import hmac
import os
import threading
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from call_use.desktop_control import DesktopActionError, execute_desktop_action, screenshot_base64
from call_use.local_voice import LocalVoiceConfig, OllamaBrain


class CommandRequest(BaseModel):
    action: dict[str, Any] = Field(default_factory=dict)


class AskRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    see_screen: bool = False


class EventRequest(BaseModel):
    direction: str = "unknown"
    from_number: str | None = None
    to_number: str | None = None
    provider_call_id: str | None = None
    message: str | None = None


class DesktopState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.events: list[dict[str, Any]] = []

    def add_event(self, event: dict[str, Any]) -> None:
        with self._lock:
            item = {"timestamp": datetime.now(timezone.utc).isoformat(), **event}
            self.events.append(item)
            self.events = self.events[-100:]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"events": list(self.events)}


_state = DesktopState()


def _check_event_secret(x_event_secret: str | None) -> None:
    expected = os.getenv("NAHA_EVENT_SECRET", "")
    if expected and not hmac.compare_digest(x_event_secret or "", expected):
        raise HTTPException(401, "Invalid event secret")


def create_desktop_app() -> FastAPI:
    app = FastAPI(title="Naha Desktop Local Bridge")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, **_state.snapshot()}

    @app.get("/screen")
    async def screen() -> dict[str, str]:
        try:
            return {"mime": "image/jpeg", "data": screenshot_base64()}
        except DesktopActionError as exc:
            raise HTTPException(500, str(exc)) from exc

    @app.post("/command")
    async def command(request: CommandRequest) -> dict[str, Any]:
        try:
            result = execute_desktop_action(request.action)
        except DesktopActionError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": result.ok, "message": result.message, "data": result.data}

    @app.post("/ask")
    async def ask(request: AskRequest) -> dict[str, str]:
        image = None
        if request.see_screen:
            try:
                image = screenshot_base64()
            except DesktopActionError as exc:
                raise HTTPException(500, str(exc)) from exc
        brain = OllamaBrain(LocalVoiceConfig.from_env())
        system = (
            "You are Naha, a Windows desktop AI assistant. Answer briefly and factually. "
            "When an action is requested, describe the safe next action; "
            "never invent screen details."
        )
        answer = await brain.chat(system, request.prompt, image_b64=image)
        return {"answer": answer}

    @app.post("/events/incoming")
    async def incoming(
        request: EventRequest,
        x_event_secret: str | None = Header(default=None),
    ) -> dict[str, bool]:
        _check_event_secret(x_event_secret)
        _state.add_event({
            "direction": "inbound",
            "from_number": request.from_number,
            "to_number": request.to_number,
            "message": request.message or "Incoming call",
        })
        return {"ok": True}

    @app.post("/events/outgoing")
    async def outgoing(
        request: EventRequest,
        x_event_secret: str | None = Header(default=None),
    ) -> dict[str, bool]:
        _check_event_secret(x_event_secret)
        _state.add_event({
            "direction": "outbound",
            "provider_call_id": request.provider_call_id,
            "message": request.message or "Outgoing call",
        })
        return {"ok": True}

    return app


def run_desktop_bridge(host: str | None = None, port: int | None = None) -> None:
    import uvicorn

    bind_host = host or os.getenv("NAHA_DESKTOP_HOST", "127.0.0.1")
    bind_port = port or int(os.getenv("NAHA_DESKTOP_PORT", "8766"))
    uvicorn.run(create_desktop_app(), host=bind_host, port=bind_port, log_level="warning")


def start_desktop_bridge(host: str | None = None, port: int | None = None) -> threading.Thread:
    thread = threading.Thread(
        target=run_desktop_bridge,
        kwargs={"host": host, "port": port},
        daemon=True,
        name="naha-desktop-bridge",
    )
    thread.start()
    return thread


async def wait_for_bridge(host: str = "127.0.0.1", port: int = 8766) -> None:
    import httpx

    async with httpx.AsyncClient(timeout=2) as client:
        for _ in range(30):
            try:
                response = await client.get(f"http://{host}:{port}/health")
                if response.is_success:
                    return
            except httpx.HTTPError:
                await asyncio.sleep(0.25)
        raise RuntimeError("Naha desktop bridge did not start")
