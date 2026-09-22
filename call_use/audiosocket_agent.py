"""Self-hosted Asterisk AudioSocket conversational agent."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import struct
import uuid
from dataclasses import dataclass

import numpy as np

from call_use.local_voice import (
    LocalSTT,
    LocalTTS,
    OllamaBrain,
    synthesize_async,
    transcribe_async,
    wav_to_pcm16,
)

LOGGER = logging.getLogger("naha.audiosocket")
AUDIO_TYPE = 0x10
UUID_TYPE = 0x01
DTMF_TYPE = 0x03
HANGUP_TYPE = 0x00
ERROR_TYPE = 0xFF
FRAME_BYTES = 320


@dataclass
class Session:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    call_uuid: str = ""
    speaking: bool = False
    stop_playback: asyncio.Event | None = None


class AudioSocketServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 9092) -> None:
        self.host = host
        self.port = port
        self.stt = LocalSTT()
        self.tts = LocalTTS()
        self.brain = OllamaBrain()
        self.context_dir = os.getenv("NAHA_CALL_CONTEXT_DIR", "/var/lib/naha/calls")

    async def run(self) -> None:
        server = await asyncio.start_server(self.handle, self.host, self.port)
        LOGGER.info("AudioSocket agent listening on %s", self.host + ":" + str(self.port))
        async with server:
            await server.serve_forever()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        session = Session(reader=reader, writer=writer)
        try:
            await self._session(session)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("AudioSocket session failed")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    def _load_context(self, call_uuid: str) -> dict[str, str]:
        path = os.path.join(self.context_dir, call_uuid + ".json")
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            return {str(k): str(v) for k, v in data.items()}
        except (OSError, ValueError):
            return {}

    async def _session(self, session: Session) -> None:
        first_type, first_payload = await self._read_packet(session.reader)
        if first_type == UUID_TYPE and len(first_payload) == 16:
            session.call_uuid = str(uuid.UUID(bytes=first_payload))
        else:
            session.call_uuid = uuid.uuid4().hex

        context = self._load_context(session.call_uuid)
        instructions = context.get("instructions", "").strip()
        greeting = "Hi, this is Naha. How can I help you today?"
        if instructions:
            greeting = "Hi, this is Naha. I am calling about a request. How can I help?"
        await self._speak(session, greeting)

        speech = bytearray()
        in_speech = False
        silence_frames = 0
        vad = _make_vad()
        playback_task: asyncio.Task | None = None

        while True:
            packet_type, payload = await self._read_packet(session.reader)
            if packet_type == HANGUP_TYPE:
                break
            if packet_type == ERROR_TYPE:
                LOGGER.warning("Asterisk AudioSocket error for %s: %r", session.call_uuid, payload)
                break
            if packet_type == DTMF_TYPE:
                continue
            if packet_type != AUDIO_TYPE:
                continue

            for frame in _split_frames(payload):
                is_speech = vad.is_speech(frame, 8000) if vad else _energy_speech(frame)
                if is_speech:
                    if session.speaking and session.stop_playback is not None:
                        session.stop_playback.set()
                    session.speaking = False
                    speech.extend(frame)
                    in_speech = True
                    silence_frames = 0
                elif in_speech:
                    speech.extend(frame)
                    silence_frames += 1

                if in_speech and silence_frames >= 35 and len(speech) >= 6400:
                    utterance = bytes(speech)
                    speech.clear()
                    in_speech = False
                    silence_frames = 0
                    if playback_task and not playback_task.done():
                        playback_task.cancel()
                        try:
                            await playback_task
                        except asyncio.CancelledError:
                            pass
                    session.speaking = False
                    transcript = await transcribe_async(self.stt, utterance, 8000)
                    if not transcript:
                        continue
                    response = await self._reply(transcript, instructions)
                    if response:
                        playback_task = asyncio.create_task(self._speak(session, response))

        if playback_task and not playback_task.done():
            playback_task.cancel()

    async def _reply(self, transcript: str, instructions: str) -> str:
        base = os.getenv(
            "NAHA_PHONE_SYSTEM_PROMPT",
            "You are Naha, a concise South African phone assistant. Speak naturally, "
            "keep replies under 80 words, never invent facts, "
            "and ask one clear question at a time.",
        )
        if instructions:
            base += "\nOutbound task instructions:\n" + instructions
        return await self.brain.chat(base, transcript)

    async def _speak(self, session: Session, text: str) -> None:
        wav = await synthesize_async(self.tts, text)
        pcm = wav_to_pcm16(wav, 8000)
        stop = asyncio.Event()
        session.stop_playback = stop
        session.speaking = True
        try:
            for offset in range(0, len(pcm), FRAME_BYTES):
                if stop.is_set():
                    break
                frame = pcm[offset : offset + FRAME_BYTES]
                if len(frame) < FRAME_BYTES:
                    frame += b"\x00" * (FRAME_BYTES - len(frame))
                await self._write_packet(session.writer, AUDIO_TYPE, frame)
                await asyncio.sleep(0.02)
        finally:
            session.speaking = False
            if session.stop_playback is stop:
                session.stop_playback = None

    async def _write_packet(
        self,
        writer: asyncio.StreamWriter,
        packet_type: int,
        payload: bytes,
    ) -> None:
        writer.write(struct.pack(">BH", packet_type, len(payload)) + payload)
        await writer.drain()

    async def _read_packet(self, reader: asyncio.StreamReader) -> tuple[int, bytes]:
        header = await reader.readexactly(3)
        packet_type, length = struct.unpack(">BH", header)
        payload = await reader.readexactly(length)
        return packet_type, payload


def _split_frames(payload: bytes):
    for start in range(0, len(payload), FRAME_BYTES):
        frame = payload[start : start + FRAME_BYTES]
        if len(frame) == FRAME_BYTES:
            yield frame


def _energy_speech(frame: bytes) -> bool:
    samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(np.square(samples)))) > 500.0


def _make_vad():
    try:
        import webrtcvad
        return webrtcvad.Vad(2)
    except ImportError:
        LOGGER.warning("webrtcvad not installed; using energy-based VAD")
        return None


async def async_main() -> None:
    parser = argparse.ArgumentParser(description="NahaLabs local Asterisk AudioSocket agent")
    parser.add_argument("--host", default=os.getenv("NAHA_AUDIOSOCKET_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("NAHA_AUDIOSOCKET_PORT", "9092")))
    args = parser.parse_args()
    await AudioSocketServer(args.host, args.port).run()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("NAHA_LOG_LEVEL", "INFO"))
    main()
