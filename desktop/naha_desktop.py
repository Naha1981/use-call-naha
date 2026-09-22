"""NahaLabs Windows desktop voice agent.

Desktop-first mode:
    microphone -> local Whisper -> local vision/reasoning -> safe computer action
    -> verify with a fresh screenshot -> local Piper speech.

Phone calling is intentionally not exposed from this UI yet. Telephony stays
separate until the desktop control loop is proven end-to-end.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import queue
import threading
import tkinter as tk
import wave
from tkinter import ttk

import numpy as np
import sounddevice as sd

from call_use.desktop_control import DesktopActionError, capture_screen_jpeg, execute_desktop_action
from call_use.local_voice import LocalSTT, LocalTTS, LocalVoiceConfig, OllamaBrain
from desktop.naha_runtime import run_first_start

MAX_ACTION_STEPS = 8

SYSTEM_PROMPT = """
You are Naha, a Windows desktop AI assistant.

You receive a live screenshot of the primary monitor on every planning step.
Use it to understand the current UI and choose one safe action at a time.

Return exactly one JSON object and nothing else.

Normal reply:
{"type":"answer","text":"..."}

Computer action:
{"type":"action","action":{"type":"click","x":123,"y":456}}

Allowed action types:
- open_url: {"type":"open_url","url":"https://..."}
- open_app: {"type":"open_app","app":"notepad|calculator|explorer|terminal|browser"}
- type_text: {"type":"type_text","text":"..."}
- click: {"type":"click","x":123,"y":456}
- double_click: {"type":"double_click","x":123,"y":456}
- right_click: {"type":"right_click","x":123,"y":456}
- move: {"type":"move","x":123,"y":456}
- scroll: {"type":"scroll","amount":-3}
- press: {"type":"press","key":"enter"}
- hotkey: {"type":"hotkey","keys":["ctrl","f"]}
- wait: {"type":"wait","seconds":0.5}

Rules:
- Never invent coordinates. Derive coordinates from the attached screenshot.
- Prefer the smallest number of actions needed.
- Never delete files, close windows with force, expose secrets, or bypass security prompts.
- Do not claim an action succeeded until the execution result says it succeeded.
- After an action succeeds, the next step receives a fresh screenshot so you can verify it.
- When the user's request is finished, return an "answer" describing the result in one or two sentences.
- Keep normal spoken answers concise and natural.
"""


class NahaDesktop:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("NahaLabs — Naha Agent")
        self.root.geometry("680x560")
        self.config = LocalVoiceConfig.from_env()
        self.stt = LocalSTT(self.config)
        self.tts = LocalTTS(self.config)
        self.brain = OllamaBrain(self.config)
        self.recording = False
        self.record_chunks: list[np.ndarray] = []
        self.stream: sd.InputStream | None = None
        self.messages: queue.Queue[str] = queue.Queue()
        self.speech_lock = threading.Lock()
        self.processing_lock = threading.Lock()
        self.listener = None
        self._build_ui()
        self._start_hotkey()
        self._poll_messages()

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.root, padding=14)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="NAHA", font=("Segoe UI", 26, "bold")).pack(anchor="w")
        ttk.Label(
            frame,
            text="Hold Ctrl + Shift + Space to talk • Naha can see and operate your screen",
        ).pack(anchor="w")

        self.status = ttk.Label(frame, text="Ready")
        self.status.pack(anchor="w", pady=(5, 8))

        task_row = ttk.Frame(frame)
        task_row.pack(fill="x", pady=(0, 8))
        self.task_entry = ttk.Entry(task_row)
        self.task_entry.pack(side="left", fill="x", expand=True)
        self.task_entry.bind("<Return>", lambda _event: self._run_typed_task())
        ttk.Button(task_row, text="Run task", command=self._run_typed_task).pack(
            side="left", padx=(6, 0)
        )

        self.log = tk.Text(frame, height=20, width=88, state="disabled")
        self.log.pack(fill="both", expand=True, pady=8)

        controls = ttk.Frame(frame)
        controls.pack(fill="x")
        ttk.Button(controls, text="Listen 5s", command=self._manual_listen).pack(side="left")
        ttk.Button(controls, text="See screen", command=self._see_screen).pack(
            side="left", padx=6
        )
        ttk.Button(controls, text="Stop speech", command=self._stop_speech).pack(side="left")
        ttk.Button(controls, text="Quit", command=self._shutdown).pack(side="right")

        self.root.protocol("WM_DELETE_WINDOW", self._shutdown)

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
        self.root.after(80, self._poll_messages)

    def _start_hotkey(self) -> None:
        try:
            from pynput import keyboard

            hotkey = keyboard.HotKey(
                keyboard.HotKey.parse("<ctrl>+<shift>+<space>"),
                lambda: self.root.after(0, self._begin_recording),
            )
            self._hotkey = hotkey
            self.listener = keyboard.Listener(
                on_press=lambda key: hotkey.press(self.listener.canonical(key)),
                on_release=lambda key: hotkey.release(self.listener.canonical(key)),
            )
            self.listener.daemon = True
            self.listener.start()
            self._log("Global voice control ready.")
        except Exception as exc:
            self._log("Global hotkey unavailable: " + str(exc))

    def _begin_recording(self) -> None:
        if self.recording or self.processing_lock.locked():
            return
        self.recording = True
        self.record_chunks = []
        self.status.configure(text="Listening…")
        try:
            self.stream = sd.InputStream(
                samplerate=16000,
                channels=1,
                dtype="int16",
                callback=self._audio_callback,
            )
            self.stream.start()
        except Exception as exc:
            self.recording = False
            self._set_status("Ready")
            self._log("Microphone failed: " + str(exc))

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

        audio = (
            np.concatenate(self.record_chunks, axis=0)
            if self.record_chunks
            else np.empty((0, 1), dtype=np.int16)
        )
        if len(audio) < 1600:
            self._set_status("Ready")
            return

        threading.Thread(
            target=self._process_audio,
            args=(audio[:, 0].copy(),),
            daemon=True,
        ).start()

    def _manual_listen(self) -> None:
        self._begin_recording()
        self.root.after(5000, self._end_recording)

    def _process_audio(self, audio: np.ndarray) -> None:
        with self.processing_lock:
            self._set_status("Listening → thinking…")
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
            self._run_desktop_task(transcript)

    def _run_typed_task(self) -> None:
        text = self.task_entry.get().strip()
        if not text:
            return
        self.task_entry.delete(0, "end")
        threading.Thread(
            target=self._run_desktop_task_thread,
            args=(text,),
            daemon=True,
        ).start()

    def _run_desktop_task_thread(self, task: str) -> None:
        with self.processing_lock:
            self._run_desktop_task(task)

    def _run_desktop_task(self, task: str) -> None:
        self._set_status("Looking at your screen…")
        history: list[str] = []

        for step in range(1, MAX_ACTION_STEPS + 1):
            try:
                image_b64 = base64.b64encode(capture_screen_jpeg()).decode("ascii")
            except DesktopActionError as exc:
                self._log(str(exc))
                self._speak(str(exc))
                self._set_status("Ready")
                return

            context = self._planner_context(task, history, step)
            try:
                raw = asyncio.run(
                    self.brain.chat(SYSTEM_PROMPT, context, image_b64=image_b64)
                )
            except Exception as exc:
                self._log("AI failed: " + str(exc))
                self._speak("I could not reach the local AI model.")
                self._set_status("Ready")
                return

            payload = self._parse_planner_json(raw)
            if payload is None:
                self._log("Planner returned invalid JSON; retrying.")
                history.append("Planner error: response was not valid JSON.")
                continue

            if payload.get("type") == "answer":
                text = str(payload.get("text", "")).strip()
                if text:
                    self._log("Naha: " + text)
                    self._speak(text)
                self._set_status("Ready")
                return

            if payload.get("type") != "action":
                history.append("Planner error: unsupported response type.")
                continue

            action = payload.get("action")
            try:
                result = execute_desktop_action(action)
            except DesktopActionError as exc:
                message = "Action blocked/failed: " + str(exc)
                self._log(message)
                history.append(message)
                self._set_status("Recovering…")
                continue

            self._log(f"Step {step}: {result.message}")
            history.append("Action succeeded: " + result.message)
            self._set_status(f"Working… step {step}/{MAX_ACTION_STEPS}")

        final = "I reached the safe action limit before confirming the task was complete."
        self._log("Naha: " + final)
        self._speak(final)
        self._set_status("Ready")

    @staticmethod
    def _planner_context(task: str, history: list[str], step: int) -> str:
        history_text = "\n".join(f"- {item}" for item in history[-10:]) or "- none"
        return (
            f"User task: {task}\n"
            f"Current planning step: {step}/{MAX_ACTION_STEPS}\n"
            "Action history:\n"
            f"{history_text}\n\n"
            "Use the attached current screenshot. If the task is complete, return an answer. "
            "Otherwise return exactly one next action."
        )

    @staticmethod
    def _parse_planner_json(raw: str) -> dict[str, object] | None:
        raw = raw.strip()
        candidates = [raw]
        if "{" in raw and "}" in raw:
            candidates.append(raw[raw.find("{") : raw.rfind("}") + 1])

        for candidate in candidates:
            try:
                value = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
        return None

    def _see_screen(self) -> None:
        threading.Thread(target=self._screen_thread, daemon=True).start()

    def _screen_thread(self) -> None:
        if self.processing_lock.locked():
            return
        with self.processing_lock:
            self._set_status("Looking…")
            try:
                image_b64 = base64.b64encode(capture_screen_jpeg()).decode("ascii")
                answer = asyncio.run(
                    self.brain.chat(
                        "Describe only useful visible information from this Windows screen. "
                        "Keep the answer under 60 words.",
                        "What is on my screen?",
                        image_b64=image_b64,
                    )
                )
                self._log("Screen: " + answer)
                self._speak(answer)
            except Exception as exc:
                self._log("Screen inspection failed: " + str(exc))
            finally:
                self._set_status("Ready")

    def _stop_speech(self) -> None:
        try:
            sd.stop()
            self._log("Speech stopped.")
        except Exception as exc:
            self._log("Could not stop speech: " + str(exc))

    def _speak(self, text: str) -> None:
        if not text:
            return
        threading.Thread(target=self._speak_thread, args=(text,), daemon=True).start()

    def _speak_thread(self, text: str) -> None:
        with self.speech_lock:
            try:
                wav_bytes = self.tts.synthesize_wav(text)
                with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
                    rate = wf.getframerate()
                    channels = wf.getnchannels()
                    frames = wf.readframes(wf.getnframes())

                audio = np.frombuffer(frames, dtype=np.int16)
                if channels > 1:
                    audio = audio.reshape(-1, channels)
                sd.stop()
                sd.play(audio, samplerate=rate, blocking=True)
            except Exception as exc:
                self._log("TTS failed: " + str(exc))

    def _set_status(self, value: str) -> None:
        self.root.after(0, lambda: self.status.configure(text=value))

    def _shutdown(self) -> None:
        try:
            self.recording = False
            if self.stream:
                self.stream.stop()
                self.stream.close()
        except Exception:
            pass

        if self.listener:
            self.listener.stop()

        try:
            sd.stop()
        except Exception:
            pass

        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    root.withdraw()
    if not run_first_start(root):
        root.destroy()
        return
    root.deiconify()
    NahaDesktop(root)
    root.mainloop()


if __name__ == "__main__":
    main()
