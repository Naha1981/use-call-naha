"""First-run Windows runtime bootstrap for the Naha desktop app.

The bootstrap is GUI-driven: no PowerShell, Git, or Python commands are needed
on the user's machine. Large model/cache files are stored under a dedicated
Naha data directory (D:\NahaAI when D: exists, otherwise LocalAppData).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from tkinter import END, LEFT, BOTH, X, Button, Label, Text, Toplevel, ttk
import tkinter as tk

OLLAMA_DOWNLOAD_URL = "https://ollama.com/download/OllamaSetup.exe"
OLLAMA_API = "http://127.0.0.1:11434"
OLLAMA_MODELS = ("llama3.2:3b", "qwen2.5vl:3b")
PIPER_MODEL = "en_US-lessac-medium"


@dataclass(frozen=True)
class RuntimePaths:
    root: Path
    ollama_install: Path
    ollama_models: Path
    piper: Path
    hf_cache: Path


def choose_data_dir() -> Path:
    explicit = os.getenv("NAHA_DATA_DIR")
    if explicit:
        return Path(explicit).expanduser()

    for drive in ("D:", "E:", "F:", "G:"):
        candidate = Path(drive + "\")
        if candidate.exists():
            return candidate / "NahaAI"

    local_appdata = os.getenv("LOCALAPPDATA")
    if local_appdata:
        return Path(local_appdata) / "NahaAI"

    return Path.home() / "NahaAI"


def build_paths() -> RuntimePaths:
    root = choose_data_dir()
    return RuntimePaths(
        root=root,
        ollama_install=root / "Ollama",
        ollama_models=root / "OllamaModels",
        piper=root / "PiperVoices",
        hf_cache=root / "ModelCache",
    )


def _set_user_environment(name: str, value: str) -> None:
    os.environ[name] = value
    if os.name != "nt":
        return
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            "Environment",
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_EXPAND_SZ, value)
    except OSError:
        # The running process still receives the variable even when Windows
        # policy prevents persisting it for future processes.
        pass


def find_ollama(paths: RuntimePaths) -> Path | None:
    candidates = [
        shutil.which("ollama.exe"),
        paths.ollama_install / "ollama.exe",
        Path(os.getenv("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe",
        Path("C:/Program Files/Ollama/ollama.exe"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def _api_ready() -> bool:
    try:
        import urllib.request

        with urllib.request.urlopen(f"{OLLAMA_API}/api/tags", timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


def _start_ollama(ollama: Path, paths: RuntimePaths) -> subprocess.Popen:
    env = os.environ.copy()
    env["OLLAMA_MODELS"] = str(paths.ollama_models)
    env["OLLAMA_HOST"] = "127.0.0.1:11434"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(
        [str(ollama), "serve"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )


def _install_ollama(paths: RuntimePaths, log) -> Path:
    paths.root.mkdir(parents=True, exist_ok=True)
    installer = Path(tempfile.gettempdir()) / "Naha-OllamaSetup.exe"
    log("Downloading the official Ollama Windows installer…")
    urllib.request.urlretrieve(OLLAMA_DOWNLOAD_URL, installer)
    log("Launching Ollama installer…")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        [str(installer), f'/DIR="{paths.ollama_install}"'],
        creationflags=creationflags,
    )
    proc.wait()
    try:
        installer.unlink()
    except OSError:
        pass

    deadline = time.time() + 30
    while time.time() < deadline:
        found = find_ollama(paths)
        if found:
            return found
        time.sleep(0.5)
    raise RuntimeError("Ollama was not installed. Close this setup window and try again.")


def _pull_model(ollama: Path, model: str, paths: RuntimePaths, log) -> None:
    log(f"Preparing local model: {model}")
    env = os.environ.copy()
    env["OLLAMA_MODELS"] = str(paths.ollama_models)
    env["OLLAMA_HOST"] = "127.0.0.1:11434"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        [str(ollama), "pull", model],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        clean = line.strip()
        if clean:
            log(clean)
    code = proc.wait()
    if code != 0:
        raise RuntimeError(f"Could not download model {model}.")


def _model_present(ollama: Path, model: str, paths: RuntimePaths) -> bool:
    env = os.environ.copy()
    env["OLLAMA_MODELS"] = str(paths.ollama_models)
    env["OLLAMA_HOST"] = "127.0.0.1:11434"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(
        [str(ollama), "list"],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )
    if proc.returncode != 0:
        return False
    return model in proc.stdout


def _ensure_piper(paths: RuntimePaths, log) -> None:
    paths.piper.mkdir(parents=True, exist_ok=True)
    model_file = paths.piper / f"{PIPER_MODEL}.onnx"
    config_file = paths.piper / f"{PIPER_MODEL}.onnx.json"
    if model_file.exists() and config_file.exists():
        return

    log("Downloading the local Naha voice…")
    from piper.download_voices import download_voice

    download_voice(PIPER_MODEL, paths.piper)


def prepare(log) -> RuntimePaths:
    paths = build_paths()
    for folder in (paths.root, paths.ollama_models, paths.piper, paths.hf_cache):
        folder.mkdir(parents=True, exist_ok=True)

    # Keep Whisper/Hugging Face caches off the main drive whenever possible.
    _set_user_environment("NAHA_DATA_DIR", str(paths.root))
    _set_user_environment("OLLAMA_MODELS", str(paths.ollama_models))
    _set_user_environment("NAHA_PIPER_DATA_DIR", str(paths.piper))
    _set_user_environment("HF_HOME", str(paths.hf_cache))
    _set_user_environment("HUGGINGFACE_HUB_CACHE", str(paths.hf_cache / "hub"))
    _set_user_environment("TRANSFORMERS_CACHE", str(paths.hf_cache / "transformers"))

    ollama = find_ollama(paths)
    if ollama is None:
        ollama = _install_ollama(paths, log)

    if not _api_ready():
        log("Starting local Ollama service…")
        _start_ollama(ollama, paths)
        deadline = time.time() + 30
        while time.time() < deadline and not _api_ready():
            time.sleep(0.5)

    if not _api_ready():
        raise RuntimeError("Ollama did not start on localhost:11434.")

    for model in OLLAMA_MODELS:
        if not _model_present(ollama, model, paths):
            _pull_model(ollama, model, paths, log)

    _ensure_piper(paths, log)
    log(f"Local AI data: {paths.root}")
    return paths


def run_first_start(root: tk.Tk) -> bool:
    window = Toplevel(root)
    window.title("Naha — First-time setup")
    window.geometry("680x430")
    window.resizable(False, False)
    window.grab_set()

    Label(
        window,
        text="Naha local setup",
        font=("Segoe UI", 20, "bold"),
    ).pack(anchor="w", padx=18, pady=(18, 4))

    Label(
        window,
        text="Naha will install/prepare its local AI components automatically. "
        "Large model files are stored outside the Windows app folder.",
        wraplength=640,
        justify="left",
    ).pack(anchor="w", padx=18, pady=(0, 12))

    log_box = Text(window, height=16, width=82, state="disabled")
    log_box.pack(fill=BOTH, expand=True, padx=18, pady=(0, 10))

    progress = ttk.Progressbar(window, mode="indeterminate")
    progress.pack(fill=X, padx=18, pady=(0, 8))
    progress.start(10)

    result = {"ok": False}

    def log(message: str) -> None:
        def apply() -> None:
            log_box.configure(state="normal")
            log_box.insert(END, message + "\n")
            log_box.see(END)
            log_box.configure(state="disabled")

        window.after(0, apply)

    def worker() -> None:
        try:
            paths = prepare(log)
            os.environ["NAHA_DATA_DIR"] = str(paths.root)
            os.environ["OLLAMA_MODELS"] = str(paths.ollama_models)
            os.environ["NAHA_PIPER_DATA_DIR"] = str(paths.piper)
            os.environ["HF_HOME"] = str(paths.hf_cache)
            os.environ["HUGGINGFACE_HUB_CACHE"] = str(paths.hf_cache / "hub")
            os.environ["TRANSFORMERS_CACHE"] = str(paths.hf_cache / "transformers")
            result["ok"] = True
            log("Naha is ready.")
        except Exception as exc:
            log("Setup failed: " + str(exc))

        def finish() -> None:
            progress.stop()
            window.grab_release()
            window.destroy()

        window.after(0, finish)

    threading.Thread(target=worker, daemon=True).start()
    root.wait_window(window)
    return result["ok"]
