"""NahaLabs Windows desktop voice agent."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import queue
import re
import threading
import tkinter as tk
import wave
from tkinter import ttk

import numpy as np
import sounddevice as sd

from call_use.asterisk import AsteriskTelephonyProvider
from call_use.desktop_bridge import start_desktop_bridge
from call_use.desktop_control import DesktopActionError, capture_screen_jpeg, execute_desktop_action
from call_use.local_voice import LocalSTT, LocalTTS, LocalVoiceConfig, OllamaBrain
from call_use.telephony import DialRequest

E164_RE = re.compile(r"\+\d{8,15}")

SYSTEM_PROMPT = """
You are Naha, a South African desktop AI assistant.
Return exactly one JSON object and nothing else.
For normal questions: {"type":"answer","text":"..."}
For a safe desktop action: {"type":"action","action":{...}}
Allowed action types are open_url, open_app, type_text, click, move, hotkey.
Allowed apps are notepad, calculator, explorer, terminal.
Allowed hotkeys are ctrl+a, ctrl+c, ctrl+v, ctrl+x, ctrl+f, ctrl+z, ctrl+y, alt+tab, win+d.
Never invent click coordinates. Use the attached screen image when coordinates are needed.
Never claim an action happened until the action tool reports success.
"""

class NahaDesktop:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("NahaLabs — Naha Agent")
        self.root.geometry("620x500")
        self.root.attributes("-topmost", True)
        self.config = LocalVoiceConfig.from_env()
        self.stt = LocalSTT(self.config)
        self.tts = LocalTTS(self.config)
        self.brain = OllamaBrain(self.config)
        self.provider = AsteriskTelephonyProvider()
        self.recording = False
        self.record_chunks: list[np.ndarray] = []
        self.stream: sd.InputStream | None = None
        self.pressed: set[object] = set()
        self.messages: queue.Queue[str] = queue.Queue()
        self._build_ui()
        start_desktop_bridge()
        self._start_hotkey()
        self._poll_messages()
        self._poll_events()

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.root, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="NAHA", font=("Segoe UI", 26, "bold")).pack(anchor="w")
        ttk.Label(frame, text="Ctrl + Shift + Space  •  talk hands-free").pack(anchor="w")
        self.status = ttk.Label(frame, text="Ready")
        self.status.pack(anchor="w", pady=(4, 8))
        self.log = tk.Text(frame, height=20, width=78, state="disabled")
        self.log.pack(fill="both", expand=True, pady=8)
        controls = ttk.Frame(frame)
        controls.pack(fill="x")
        ttk.Button(controls, text="Listen", command=self._manual_listen).pack(side="left")
        ttk.Button(controls, text="See screen", command=self._see_screen).pack(side="left", padx=6)
        ttk.Button(controls, text="Test bridge", command=self._test_bridge).pack(side="left")
        ttk.Button(controls, text="Quit", command=self.root.destroy).pack(side="right")

    def _log(self, message: str) -> None:
        self.messages.put(message)

    def _poll_messages(self) -> None:
        while True:
            try:
                message = self.messages.get_nowait()
            except queue.Empty:
                break
            self.log.configure(state="normal")
            self.log.insert("end", message + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        self.root.after(100, self._poll_messages)

    def _poll_events(self) -> None:
        try:
            import httpx
            response = httpx.get("http://127.0.0.1:8766/health", timeout=0.3)
            if response.is_success:
                events = response.json().get("events", [])
                if events:
                    latest = events[-1]
                    marker = latest.get("timestamp", "")
                    if marker != getattr(self, "_last_event_marker", ""):
                        self._last_event_marker = marker
                        if latest.get("direction") == "inbound":
                            self._log("Incoming call: " + str(latest.get("from_number", "unknown")))
                            self._speak("You have an incoming phone call.")
        except Exception:
            pass
        self.root.after(1000, self._poll_events)

    def _start_hotkey(self) -> None:
        try:
            from pynput import keyboard
            self.listener = keyboard.Listener(on_press=self._hotkey_press, on_release=self._hotkey_release)
            self.listener.daemon = True
            self.listener.start()
        except Exception as exc:
            self._log("Global hotkey unavailable: " + str(exc))

    def _hotkey_press(self, key) -> None:
        from pynput import keyboard
        self.pressed.add(key)
        required = {keyboard.Key.ctrl_l, keyboard.Key.shift, keyboard.Key.space}
        if required.issubset(self.pressed) and not self.recording:
            self._begin_recording()

    def _hotkey_release(self, key) -> None:
        from pynput import keyboard
        was_recording = self.recording
        self.pressed.discard(key)
        required = {keyboard.Key.ctrl_l, keyboard.Key.shift, keyboard.Key.space}
        if was_recording and not required.issubset(self.pressed):
            self._end_recording()

    def _begin_recording(self) -> None:
        self.recording = True
        self.record_chunks = []
        self.status.configure(text="Listening…")
        self.stream = sd.InputStream(samplerate=16000, channels=1, dtype="int16", callback=self._audio_callback)
        self.stream.start()

    def _audio_callback(self, indata, _frames, _time, _status) -> None:
        if self.recording:
            self.record_chunks.append(indata.copy())

    def _end_recording(self) -> None:
        if not self.recording:
            return
        self.recording = False
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        audio = np.concatenate(self.record_chunks, axis=0) if self.record_chunks else np.empty((0, 1), dtype=np.int16)
        if len(audio) < 1600:
            self.status.configure(text="Ready")
            return
        threading.Thread(target=self._process_audio, args=(audio[:, 0].copy(),), daemon=True).start()

    def _manual_listen(self) -> None:
        self._begin_recording()
        self.root.after(5000, self._end_recording)

    def _process_audio(self, audio: np.ndarray) -> None:
        self._set_status("Thinking…")
        try:
            transcript = self.stt.transcribe_pcm16(audio.tobytes(), 16000)
        except Exception as exc:
            self._log("STT failed: " + str(exc))
            self._set_status("Ready")
            return
        if not transcript:
            self._set_status("Ready")
            return
        self._log("You: " + transcript)
        phone_match = E164_RE.search(transcript)
        if transcript.lower().startswith("call ") and phone_match:
            number = phone_match.group(0)
            instructions = transcript.replace(number, "").strip() or "Have a friendly conversation."
            self._place_call(number, instructions)
            return
        see_screen = any(word in transcript.lower() for word in ("screen", "look at", "what is open", "what is on"))
        image_b64 = None
        if see_screen:
            try:
                image_b64 = base64.b64encode(capture_screen_jpeg()).decode("ascii")
            except DesktopActionError as exc:
                self._log(str(exc))
        try:
            raw = asyncio.run(self.brain.chat(SYSTEM_PROMPT, transcript, image_b64=image_b64))
            self._handle_brain_response(raw)
        except Exception as exc:
            self._log("AI failed: " + str(exc))
        finally:
            self._set_status("Ready")

    def _handle_brain_response(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"type": "answer", "text": raw}
        if payload.get("type") == "action":
            try:
                result = execute_desktop_action(payload.get("action", {}))
                self._log(result.message)
                self._speak(result.message)
            except DesktopActionError as exc:
                self._log("Blocked: " + str(exc))
                self._speak(str(exc))
            return
        text = str(payload.get("text", ""))
        if text:
            self._log("Naha: " + text)
            self._speak(text)

    def _place_call(self, number: str, instructions: str) -> None:
        self._log("Calling " + number + "…")
        request = DialRequest(
            to_number=number,
            from_number=os.getenv("NAHA_CALLER_ID", ""),
            country="ZA" if number.startswith("+27") else "LS",
            call_id="desktop",
            instructions=instructions,
        )
        try:
            result = asyncio.run(self.provider.dial(request))
            self._log("Call started: " + result.provider_call_id)
            self._speak("The call is dialing now.")
        except Exception as exc:
            self._log("Call failed: " + str(exc))
            self._speak("I could not start the call.")

    def _see_screen(self) -> None:
        threading.Thread(target=self._screen_thread, daemon=True).start()

    def _screen_thread(self) -> None:
        self._set_status("Looking…")
        try:
            image_b64 = base64.b64encode(capture_screen_jpeg()).decode("ascii")
            answer = asyncio.run(self.brain.chat("Describe only useful visible information from this Windows screen. Keep the answer under 60 words.", "What is on my screen?", image_b64=image_b64))
            self._log("Screen: " + answer)
            self._speak(answer)
        except Exception as exc:
            self._log("Screen inspection failed: " + str(exc))
        finally:
            self._set_status("Ready")

    def _test_bridge(self) -> None:
        self._log("Desktop bridge: 127.0.0.1:8766")

    def _set_status(self, value: str) -> None:
        self.root.after(0, lambda: self.status.configure(text=value))

    def _speak(self, text: str) -> None:
        threading.Thread(target=self._speak_thread, args=(text,), daemon=True).start()

    def _speak_thread(self, text: str) -> None:
        try:
            wav_bytes = self.tts.synthesize_wav(text)
            with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
                rate = wf.getframerate()
                channels = wf.getnchannels()
                frames = wf.readframes(wf.getnframes())
            audio = np.frombuffer(frames, dtype=np.int16)
            if channels > 1:
                audio = audio.reshape(-1, channels)
            sd.play(audio, samplerate=rate, blocking=True)
        except Exception as exc:
            self._log("TTS failed: " + str(exc))


def main() -> None:
    root = tk.Tk()
    NahaDesktop(root)
    root.mainloop()


if __name__ == "__main__":
    main()
