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
import tempfile
import wave
from dataclasses import dataclass

import httpx
import numpy as np


@dataclass(frozen=True)
class LocalVoiceConfig:
    ollama_url: str = "http://127.0.0.1:11434"
    llm_model: str = "llama3.2:3b"
    vision_model: str = "qwen2.5vl:3b"
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
            whisper_compute_type=os.getenv("NAHA_WHISPER_COMPUTE_TYPE", cls.whisper_compute_type),
            piper_model=os.getenv("NAHA_PIPER_MODEL", cls.piper_model),
            piper_data_dir=os.getenv("NAHA_PIPER_DATA_DIR") or None,
        )


def _resample_pcm16(pcm: bytes, source_rate: int, target_rate: int) -> bytes:
    if source_rate == target_rate or not pcm:
        return pcm
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if audio.size == 0:
        return b""
    target_length = max(1, int(round(audio.size * target_rate / source_rate)))
    old_x = np.linspace(0.0, 1.0, num=audio.size, endpoint=True)
    new_x = np.linspace(0.0, 1.0, num=target_length, endpoint=True)
    return np.interp(new_x, old_x, audio).astype(np.int16).tobytes()


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
        audio_pcm = _resample_pcm16(pcm, sample_rate, 16000)
        audio = np.frombuffer(audio_pcm, dtype=np.int16).astype(np.float32) / 32768.0
        model = self._load()
        segments, _ = model.transcribe(audio, beam_size=1, vad_filter=True)
        return " ".join(segment.text.strip() for segment in segments).strip()


class LocalTTS:
    """Local Piper wrapper."""

    def __init__(self, config: LocalVoiceConfig | None = None) -> None:
        self.config = config or LocalVoiceConfig.from_env()

    def synthesize_wav(self, text: str) -> bytes:
        cleaned = re.sub(r"\s+", " ", text).strip()
        if not cleaned:
            return b""
        from piper import PiperVoice

        model_path = self._model_path()
        if not os.path.isfile(model_path):
            raise RuntimeError(
                "Piper voice model not found: "
                + model_path
                + ". Run python -m piper.download_voices "
                + self.config.piper_model
            )

        with tempfile.TemporaryDirectory(prefix="naha-piper-") as tmp:
            output = os.path.join(tmp, "speech.wav")
            voice = PiperVoice.load(model_path)
            with wave.open(output, "wb") as wav_file:
                voice.synthesize_wav(cleaned, wav_file)
            with open(output, "rb") as handle:
                return handle.read()

    def _model_path(self) -> str:
        candidates: list[str] = []
        if self.config.piper_data_dir:
            candidates.extend(
                [
                    os.path.join(self.config.piper_data_dir, self.config.piper_model + ".onnx"),
                    os.path.join(self.config.piper_data_dir, self.config.piper_model),
                ]
            )
        candidates.append(self.config.piper_model)
        return next((path for path in candidates if os.path.isfile(path)), candidates[0])


class OllamaBrain:
    """Small Ollama client for text and image-aware prompts."""

    def __init__(self, config: LocalVoiceConfig | None = None) -> None:
        self.config = config or LocalVoiceConfig.from_env()

    async def chat(self, system: str, user: str, *, image_b64: str | None = None) -> str:
        message: dict[str, object] = {"role": "user", "content": user}
        if image_b64:
            message["images"] = [image_b64]
        payload = {
            "model": self.config.vision_model if image_b64 else self.config.llm_model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                message,
            ],
        }
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(f"{self.config.ollama_url}/api/chat", json=payload)
            response.raise_for_status()
            data = response.json()
        message_out = data.get("message", {})
        return str(message_out.get("content", "")).strip()


def wav_to_pcm16(wav_bytes: bytes, target_rate: int = 8000) -> bytes:
    """Decode Piper WAV and resample mono PCM16 to the Asterisk rate."""
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
    return _resample_pcm16(audio.tobytes(), source_rate, target_rate)


async def transcribe_async(stt: LocalSTT, pcm: bytes, sample_rate: int) -> str:
    return await asyncio.to_thread(stt.transcribe_pcm16, pcm, sample_rate)


async def synthesize_async(tts: LocalTTS, text: str) -> bytes:
    return await asyncio.to_thread(tts.synthesize_wav, text)
