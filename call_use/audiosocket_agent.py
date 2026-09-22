"""Self-hosted Asterisk AudioSocket conversational agent.

This is intentionally small and provider-free:
  Asterisk -> 8 kHz PCM -> VAD -> faster-whisper -> Ollama -> Piper -> PCM -> Asterisk

The process supports both inbound calls and outbound calls because Asterisk
uses the same AudioSocket dialplan for both directions.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import struct
import uuid
from dataclasses import dataclass

import numpy as np

from call_use.local_voice import LocalSTT, LocalTTS, OllamaBrain, synthesize_async, transcribe_async, wav_to_pcm16k

LOGGER = logging.getLogger("naha.audiosocket")

AUDIO_TYPE = 0x10
UUID_TYPE = 0x01
DTMF_TYPE = 0x03
HANGUP_TYPE = 0x00
ERROR_TYPE = 0xFF
FRAME_BYTES = 320  # 20 ms of 8 kHz signed 16-bit mono PCM


@dataclass
class Session:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    call_uuid: str = ""
    speaking: bool = False
    stop_playback: asyncio.Event | None = None


class AudioSocketServer:
    """Minimal AsyncIO AudioSocket server for local speech agents."""

    def __init__(self, host: str = "127.0.0.1", port: int = 9092) -> None:
        self.host = host
        self.port = port
        self.stt = LocalSTT()
        self.tts = LocalTTS()
        self.brain = OllamaBrain()

    async def run(self) -> None:
        server = await asyncio.start_server(self.handle, self.host, self.port)
        sockets = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
        LOGGER.info("AudioSocket agent listening on %s", sockets)
        async with server:
            await server.serve_forever()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        session = Session(reader=reader, writer=writer)
        LOGGER.info("Call connected from %s", peer)
        try:
            await self._session(session)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("AudioSocket session failed")
        finally:
            writer.close()
            with contextlib_suppress():
                await writer.wait_closed()

    async def _session(self, session: Session) -> None:
        first_type, first_payload = await self._read_packet(session.reader)
        if first_type == UUID_TYPE:
            session.call_uuid = str(uuid.UUID(bytes=first_payload))
        else:
            session.call_uuid = uuid.uuid4().hex

        await self._speak(session, "Hi, this is Naha. How can I help you today?")

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
                LOGGER.info("DTMF %s on %s", payload.decode("ascii", errors="ignore"), session.call_uuid)
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
                        await _cancel_task(playback_task)
                    session.speaking = False
                    transcript = await transcribe_async(self.stt, utterance, 8000)
                    if not transcript:
                        continue
                    LOGGER.info("Caller %s: %s", session.call_uuid, transcript)
                    response = await self._reply(transcript)
                    if response:
                        playback_task = asyncio.create_task(self._speak(session, response))

        if playback_task and not playback_task.done():
            await _cancel_task(playback_task)

    async def _reply(self, transcript: str) -> str:
        system = os.getenv(
            "NAHA_PHONE_SYSTEM_PROMPT",
            "You are Naha, a concise South African phone assistant. "
            "Speak naturally, keep replies under 80 words, never invent facts, "
            "and ask one clear question at a time.",
        )
        return await self.brain.chat(system, transcript)

    async def _speak(self, session: Session, text: str) -> None:
        wav = await synthesize_async(self.tts, text)
        pcm = wav_to_pcm16k(wav, 8000)
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

    async def _write_packet(self, writer: asyncio.StreamWriter, packet_type: int, payload: bytes) -> None:
        writer.write(struct.pack(">BH", packet_type, len(payload)) + payload)
        await writer.drain()

    async def _read_packet(self, reader: asyncio.StreamReader) -> tuple[int, bytes]:
        header = await reader.readexactly(3)
        packet_type, length = struct.unpack(">BH", header)
        payload = await reader.readexactly(length)
        return packet_type, payload


class contextlib_suppress:
    """Tiny local context manager to avoid an extra runtime dependency."""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return True


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


async def main() -> None:
    parser = argparse.ArgumentParser(description="NahaLabs local Asterisk AudioSocket agent")
    parser.add_argument("--host", default=os.getenv("NAHA_AUDIOSOCKET_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("NAHA_AUDIOSOCKET_PORT", "9092")))
    args = parser.parse_args()
    await AudioSocketServer(args.host, args.port).run()


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("NAHA_LOG_LEVEL", "INFO"))
    asyncio.run(main())
