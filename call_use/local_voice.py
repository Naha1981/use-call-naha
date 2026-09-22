"""Local speech + language + speech stack.

The default path is:
  microphone/audio -> faster-whisper -> Ollama -> Piper

Everything is local once the model weights are installed. Each adapter is
loaded lazily so the core call-use package remains lightweight.
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import subprocess
import tempfile
import wave
from dataclasses import dataclass

import httpx
import numpy as np


@dataclass(frozen=True)
class LocalVoiceConfig:
    ollama_url: str = "http://127.0.0.1:11434"
    llm_model: str = "llama3.2:3b"
    vision_model: str = "llama3.2:3b"
    whisper_model: str = "base"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    piper_model: str = "en_US-lessac-medium"
    piper_data_dir: str | None = None

    @classmethod
    def from_env(cls) -> "LocalVoiceConfig":
        return cls(
            ollama_url=os.getenv("NAHA_OLLAMA_URL", cls.ollama_url),
            llm_model=os.getenv("NAHA_LLM_MODEL", cls.llm_model),
            vision_model=os.getenv("NAHA_VISION_MODEL", cls.vision_model),
            whisper_model=os.getenv("NAHA_WHISPER_MODEL", cls.whisper_model),
            whisper_device=os.getenv("NAHA_WHISPER_DEVICE", cls.whisper_device),
            whisper_compute_type=os.getenv(
                "NAHA_WHISPER_COMPUTE_TYPE", cls.whisper_compute_type
            ),
            piper_model=os.getenv("NAHA_PIPER_MODEL", cls.piper_model),
            piper_data_dir=os.getenv("NAHA_PIPER_DATA_DIR") or None,
        )


class LocalSTT:
    """Lazy faster-whisper recognizer."""

    def __init__(self, config: LocalVoiceConfig | None = None) -> None:
        self.config = config or LocalVoiceConfig.from_env()
        self._model = None

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                self.config.whisper_model,
                device=self.config.whisper_device,
                compute_type=self.config.whisper_compute_type,
            )
        return self._model

    def transcribe_pcm16(self, pcm: bytes, sample_rate: int) -> str:
        if not pcm:
            return ""
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        model = self._load()
        segments, _ = model.transcribe(audio, beam_size=1, vad_filter=True)
        return " ".join(segment.text.strip() for segment in segments).strip()


class LocalTTS:
    """Local Piper wrapper. CLI invocation keeps the dependency boundary simple."""

    def __init__(self, config: LocalVoiceConfig | None = None) -> None:
        self.config = config or LocalVoiceConfig.from_env()

    def synthesize_wav(self, text: str) -> bytes:
        cleaned = re.sub(r"\s+", " ", text).strip()
        if not cleaned:
            return b""
        with tempfile.TemporaryDirectory(prefix="naha-piper-") as tmp:
            output = os.path.join(tmp, "speech.wav")
            command = [
                "piper",
                "--model",
                self.config.piper_model,
                "--output_file",
                output,
                "--",
                cleaned,
            ]
            if self.config.piper_data_dir:
                command[1:1] = ["--data-dir", self.config.piper_data_dir]
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    "Piper failed. Install piper-tts and a compatible voice model. "
                    f"{completed.stderr.strip()}"
                )
            with open(output, "rb") as handle:
                return handle.read()


class OllamaBrain:
    """Small Ollama client for text and optional image-aware prompts."""

    def __init__(self, config: LocalVoiceConfig | None = None) -> None:
        self.config = config or LocalVoiceConfig.from_env()

    async def chat(self, system: str, user: str, *, image_b64: str | None = None) -> str:
        content: str | list[dict[str, object]] = user
        if image_b64:
            content = [
                {"type": "text", "text": user},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
            ]
        payload = {
            "model": self.config.vision_model if image_b64 else self.config.llm_model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
        }
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(f"{self.config.ollama_url}/api/chat", json=payload)
            response.raise_for_status()
            data = response.json()
        message = data.get("message", {})
        return str(message.get("content", "")).strip()


def wav_to_pcm16k(wav_bytes: bytes, target_rate: int = 8000) -> bytes:
    """Decode a Piper WAV and resample mono PCM16 to the Asterisk rate."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as source:
        channels = source.getnchannels()
        sample_width = source.getsampwidth()
        source_rate = source.getframerate()
        frames = source.readframes(source.getnframes())
    if sample_width != 2:
        raise ValueError(f"Expected 16-bit PCM WAV, got sample width {sample_width}")
    audio = np.frombuffer(frames, dtype=np.int16)
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1).astype(np.int16)
    if source_rate == target_rate:
        return audio.tobytes()
    target_length = max(1, int(round(len(audio) * target_rate / source_rate)))
    old_x = np.linspace(0.0, 1.0, num=len(audio), endpoint=True)
    new_x = np.linspace(0.0, 1.0, num=target_length, endpoint=True)
    resampled = np.interp(new_x, old_x, audio.astype(np.float32)).astype(np.int16)
    return resampled.tobytes()


async def transcribe_async(stt: LocalSTT, pcm: bytes, sample_rate: int) -> str:
    return await asyncio.to_thread(stt.transcribe_pcm16, pcm, sample_rate)


async def synthesize_async(tts: LocalTTS, text: str) -> bytes:
    return await asyncio.to_thread(tts.synthesize_wav, text)
