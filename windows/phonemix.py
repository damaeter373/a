#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PhoneMix - Receptor para Windows
================================

Recibe el audio de tu teléfono Android (por Wi-Fi, USB o Bluetooth/anclaje) y lo
reproduce por la MISMA salida de audio que usas para jugar, de modo que con unos
solos audífonos conectados a la PC escuchas los dos sonidos a la vez.

Controles principales
  * Teléfono : volumen del audio que llega del teléfono (0-150 %).
  * PC       : volumen de los juegos/apps de Windows (se ajusta por aplicación).
  * General  : volumen maestro de Windows.

Protocolo (muy simple, TCP):
  cabecera : b"PMX1" | sample_rate (uint32 BE) | canales (uint8) | len_nombre (uint8) | nombre UTF-8
  después  : PCM de 16 bits little-endian, entrelazado, sin fin.
Descubrimiento por UDP (puerto 48001): el teléfono envía b"PMX?" y esta app responde b"PMX!nombre:puerto".
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time

import numpy as np

try:
    import sounddevice as sd
    SD_ERROR = ""
except Exception as _exc:  # PortAudio no disponible / paquete sin instalar
    sd = None
    SD_ERROR = str(_exc)

APP_NAME = "PhoneMix"
TCP_PORT = 48000
DISCOVERY_PORT = 48001
MAGIC = b"PMX1"
FRAME_BYTES = 4  # 16 bits x 2 canales

DEFAULTS = {
    "port": TCP_PORT,
    "phone_volume": 100,
    "phone_muted": False,
    "pc_volume": 100,
    "pc_muted": False,
    "pc_control": False,
    "buffer_ms": 60,
    "output_device": "",
    "exclude_apps": "",
}


# --------------------------------------------------------------------------- #
# Ajustes
# --------------------------------------------------------------------------- #
def settings_path() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, APP_NAME)
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        pass
    return os.path.join(folder, "settings.json")


def load_settings() -> dict:
    data = dict(DEFAULTS)
    try:
        with open(settings_path(), "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            data.update({k: v for k, v in loaded.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return data


def save_settings(data: dict) -> None:
    try:
        with open(settings_path(), "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Búfer de audio (jitter buffer)
# --------------------------------------------------------------------------- #
class AudioBuffer:
    """Cola de bytes PCM con precarga y control de latencia."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._playing = False
        self.rate = 48000
        self.target_ms = 60
        self.underruns = 0
        self.drops = 0

    def configure(self, rate: int) -> None:
        with self._lock:
            self.rate = int(rate)
            self._buf.clear()
            self._playing = False

    def _target_bytes(self) -> int:
        return max(1, int(self.rate * self.target_ms / 1000)) * FRAME_BYTES

    def fill_ms(self) -> float:
        return len(self._buf) / FRAME_BYTES / max(1, self.rate) * 1000.0

    def push(self, data: bytes) -> None:
        with self._lock:
            self._buf += data
            target = self._target_bytes()
            if len(self._buf) > target * 3:
                # El reloj del teléfono y el de la PC nunca coinciden del todo:
                # si se acumula demasiado, se descarta lo más viejo para no
                # ir sumando retraso.
                drop = len(self._buf) - int(target * 1.5)
                drop -= drop % FRAME_BYTES
                if drop > 0:
                    del self._buf[:drop]
                    self.drops += 1

    def pull(self, nbytes: int):
        """Devuelve exactamente nbytes o None (silencio / recargando)."""
        with self._lock:
            if not self._playing:
                if len(self._buf) >= max(self._target_bytes(), nbytes):
                    self._playing = True
                else:
                    return None
            if len(self._buf) < nbytes:
                self._playing = False
                self.underruns += 1
                return None
            data = bytes(self._buf[:nbytes])
            del self._buf[:nbytes]
            return data


# --------------------------------------------------------------------------- #
# Dispositivos de salida
# --------------------------------------------------------------------------- #
def _wasapi_index():
    if sd is None:
        return None
    try:
        for i, api in enumerate(sd.query_hostapis()):
            if "WASAPI" in api["name"]:
                return i
    except Exception:
        pass
    return None


def list_output_devices():
    """Devuelve [(indice, nombre)] de salidas (WASAPI si existe) y el índice predeterminado."""
    if sd is None:
        return [], None
    wasapi = _wasapi_index()
    devices = []
    try:
        for i, dev in enumerate(sd.query_devices()):
            if dev["max_output_channels"] > 0 and (wasapi is None or dev["hostapi"] == wasapi):
                devices.append((i, dev["name"]))
        if wasapi is not None:
            default = sd.query_hostapis(wasapi)["default_output_device"]
        else:
            default = sd.default.device[1]
    except Exception:
        return [], None
    return devices, (default if default is not None and default >= 0 else None)


def resolve_device(name: str):
    devices, default = list_output_devices()
    if name:
        for idx, dev_name in devices:
            if dev_name == name:
                return idx
    return default


# --------------------------------------------------------------------------- #
# Motor de reproducción
# --------------------------------------------------------------------------- #
class AudioEngine:
    def __init__(self) -> None:
        self.buffer = AudioBuffer()
        self.stream = None
        self.device_name = ""
        self.gain = 1.0
        self.muted = False
        self.level = 0.0
        self._applied = 0.0
        self._lock = threading.RLock()

    def start(self, rate: int) -> None:
        with self._lock:
            self.stop()
            if sd is None:
                raise RuntimeError("sounddevice no disponible: " + SD_ERROR)
            self.buffer.configure(rate)
            self._applied = 0.0
            device = resolve_device(self.device_name)
            extra = None
            if device is not None and _wasapi_index() is not None:
                try:
                    if sd.query_devices(device)["hostapi"] == _wasapi_index():
                        extra = sd.WasapiSettings(auto_convert=True)
                except Exception:
                    extra = None
            self.stream = sd.OutputStream(
                samplerate=rate,
                channels=2,
                dtype="float32",
                device=device,
                latency="low",
                callback=self._callback,
                extra_settings=extra,
            )
            self.stream.start()

    def stop(self) -> None:
        with self._lock:
            stream, self.stream = self.stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        self.level = 0.0

    def restart(self) -> None:
        if self.stream is not None:
            self.start(self.buffer.rate)

    def push(self, data: bytes) -> None:
        self.buffer.push(data)

    def _callback(self, outdata, frames, time_info, status) -> None:
        target = 0.0 if self.muted else float(self.gain)
        data = self.buffer.pull(frames * FRAME_BYTES)
        if data is None:
            outdata.fill(0)
            self._applied = target
            self.level *= 0.8
            return
        pcm = np.frombuffer(data, dtype=np.int16).reshape(-1, 2).astype(np.float32)
        pcm *= 1.0 / 32768.0
        if abs(target - self._applied) > 1e-4:
            # Rampa suave para evitar "clics" al mover el control de volumen.
            pcm *= np.linspace(self._applied, target, pcm.shape[0], dtype=np.float32)[:, None]
            self._applied = target
        else:
            pcm *= target
        np.clip(pcm, -1.0, 1.0, out=pcm)
        outdata[:] = pcm
        peak = float(np.max(np.abs(pcm))) if pcm.size else 0.0
        self.level = max(peak, self.level * 0.85)


# --------------------------------------------------------------------------- #
# Volumen de Windows (maestro + por aplicación) con pycaw
# --------------------------------------------------------------------------- #
class PCVolume(threading.Thread):
    """
    Hilo propio (COM requiere inicializarse por hilo). Mientras `enabled` sea True fija
    el volumen de TODAS las apps de Windows (excepto PhoneMix y las excluidas) al valor
    `scale`. Al desactivar o cerrar, restaura los volúmenes originales.
    """

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.available = False
        self.error = ""
        self.enabled = False
        self.scale = 1.0
        self.muted = False
        self.exclude: set[str] = set()
        self.master_now = None
        self._master_request = None
        self._reset_request = False
        self._halt = threading.Event()
        self._originals: dict = {}
        self._endpoint = None

    # -- API pública (se llama desde la interfaz) ------------------------- #
    def set_master(self, value: float) -> None:
        self._master_request = max(0.0, min(1.0, value))

    def reset(self) -> None:
        self.enabled = False
        self._reset_request = True

    def stop(self) -> None:
        self._halt.set()

    # -- Hilo ---------------------------------------------------------------- #
    def run(self) -> None:
        try:
            import comtypes
            from pycaw.pycaw import AudioUtilities
        except Exception as exc:
            self.error = f"pycaw no disponible: {exc}"
            return
        comtypes.CoInitialize()
        self.available = True
        try:
            while not self._halt.is_set():
                try:
                    self._tick(AudioUtilities)
                    self.error = ""
                except Exception as exc:
                    self.error = str(exc)
                    self._endpoint = None
                self._halt.wait(0.4)
        finally:
            try:
                self._restore(AudioUtilities)
            except Exception:
                pass
            comtypes.CoUninitialize()

    @staticmethod
    def _get_endpoint():
        from ctypes import POINTER, cast
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        speakers = AudioUtilities.GetSpeakers()
        endpoint = getattr(speakers, "EndpointVolume", None)  # pycaw reciente
        if endpoint is not None:
            return endpoint
        iface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        return cast(iface, POINTER(IAudioEndpointVolume))

    @staticmethod
    def _sid(session) -> str:
        return str(getattr(session, "InstanceIdentifier", None) or getattr(session, "Identifier", ""))

    def _tick(self, AU) -> None:
        if self._endpoint is None:
            self._endpoint = self._get_endpoint()
        if self._master_request is not None:
            value, self._master_request = self._master_request, None
            self._endpoint.SetMasterVolumeLevelScalar(value, None)
        self.master_now = float(self._endpoint.GetMasterVolumeLevelScalar())

        if self._reset_request:
            self._reset_request = False
            self._restore(AU)

        if not self.enabled:
            if self._originals:
                self._restore(AU)
            return

        target = 0.0 if self.muted else self.scale
        my_pid = os.getpid()
        for session in AU.GetAllSessions():
            try:
                pid = session.ProcessId
                if pid in (0, my_pid):
                    continue
                name = ""
                try:
                    if session.Process is not None:
                        name = session.Process.name().lower()
                except Exception:
                    name = ""
                if name.removesuffix(".exe") in self.exclude:
                    continue
                vol = session.SimpleAudioVolume
                key = (pid, self._sid(session))
                current = vol.GetMasterVolume()
                if key not in self._originals:
                    self._originals[key] = current
                if abs(current - target) > 0.004:
                    vol.SetMasterVolume(target, None)
            except Exception:
                continue

    def _restore(self, AU) -> None:
        if not self._originals:
            return
        for session in AU.GetAllSessions():
            try:
                key = (session.ProcessId, self._sid(session))
                if key in self._originals:
                    session.SimpleAudioVolume.SetMasterVolume(self._originals[key], None)
            except Exception:
                continue
        self._originals.clear()


# --------------------------------------------------------------------------- #
# Red: servidor TCP + descubrimiento UDP
# --------------------------------------------------------------------------- #
class State:
    def __init__(self) -> None:
        self.connected = False
        self.phone_name = ""
        self.phone_ip = ""
        self.error = ""


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("conexión cerrada")
        buf += chunk
    return bytes(buf)


class Receiver(threading.Thread):
    def __init__(self, port: int, engine, state: State) -> None:
        super().__init__(daemon=True)
        self.port = port
        self.engine = engine
        self.state = state
        self._halt = threading.Event()

    def stop(self) -> None:
        self._halt.set()

    def run(self) -> None:
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if os.name != "nt":
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", self.port))
            srv.listen(2)
            srv.settimeout(0.5)
        except OSError as exc:
            self.state.error = f"No pude abrir el puerto {self.port}: {exc}"
            return
        try:
            while not self._halt.is_set():
                try:
                    conn, addr = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                self._serve(conn, addr)
        finally:
            srv.close()

    def _serve(self, conn: socket.socket, addr) -> None:
        started = False
        try:
            conn.settimeout(10)
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            head = recv_exact(conn, 10)
            if head[:4] != MAGIC:
                return
            rate, channels, name_len = struct.unpack(">IBB", head[4:])
            name = recv_exact(conn, name_len).decode("utf-8", "replace") if name_len else "Teléfono"
            if not (8000 <= rate <= 192000) or channels not in (1, 2):
                return
            self.engine.start(rate)
            started = True
            self.state.error = ""
            self.state.phone_name = name
            self.state.phone_ip = addr[0]
            self.state.connected = True

            in_frame = 2 * channels
            leftover = b""
            while not self._halt.is_set():
                data = conn.recv(8192)
                if not data:
                    break
                data = leftover + data
                usable = len(data) - (len(data) % in_frame)
                leftover = data[usable:]
                if usable == 0:
                    continue
                chunk = data[:usable]
                if channels == 1:
                    chunk = np.repeat(np.frombuffer(chunk, dtype=np.int16), 2).tobytes()
                self.engine.push(chunk)
        except (OSError, ConnectionError, struct.error):
            pass
        except Exception as exc:  # p. ej. no se pudo abrir el dispositivo de audio
            self.state.error = f"Error de audio: {exc}"
        finally:
            try:
                conn.close()
            except OSError:
                pass
            if started:
                self.engine.stop()
            self.state.connected = False


class DiscoveryResponder(threading.Thread):
    def __init__(self, tcp_port: int) -> None:
        super().__init__(daemon=True)
        self.tcp_port = tcp_port
        self.error = ""
        self._halt = threading.Event()

    def stop(self) -> None:
        self._halt.set()

    def run(self) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", DISCOVERY_PORT))
            sock.settimeout(0.5)
        except OSError as exc:
            self.error = str(exc)
            return
        reply = b"PMX!" + f"{socket.gethostname()}:{self.tcp_port}".encode("utf-8", "replace")
        try:
            while not self._halt.is_set():
                try:
                    data, addr = sock.recvfrom(256)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if data.startswith(b"PMX?"):
                    try:
                        sock.sendto(reply, addr)
                    except OSError:
                        pass
        finally:
            sock.close()


def local_ips() -> list[str]:
    ips: list[str] = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))  # UDP: no envía nada, solo elige la interfaz
        ips.append(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith(("127.", "169.254.")) and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return ips


def find_adb():
    path = shutil.which("adb")
    if path:
        return path
    candidates = []
    for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = os.environ.get(env)
        if root:
            candidates.append(os.path.join(root, "platform-tools", "adb.exe"))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(os.path.join(local, "Android", "Sdk", "platform-tools", "adb.exe"))
    here = os.path.dirname(os.path.abspath(sys.argv[0] if sys.argv and sys.argv[0] else __file__))
    candidates += [os.path.join(here, "platform-tools", "adb.exe"), os.path.join(here, "adb.exe")]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


# --------------------------------------------------------------------------- #
# Interfaz
# --------------------------------------------------------------------------- #
BG = "#15171c"
PANEL = "#1e2128"
FG = "#e8eaf0"
MUTED = "#8b91a1"
ACCENT = "#4f8cff"
GOOD = "#3ecf8e"
WARN = "#f5b94a"
BAD = "#ff6b6b"


class App:
    def __init__(self, root) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk, self.ttk = tk, ttk
        self.root = root
        self.settings = load_settings()
        self.state = State()
        self.engine = AudioEngine()
        self.pc = PCVolume()
        self.usb_msg = ""
        self._programmatic = False
        self._drag_master = False
        self._save_job = None

        # Estado inicial a partir de los ajustes guardados
        self.engine.gain = self.settings["phone_volume"] / 100.0
        self.engine.muted = bool(self.settings["phone_muted"])
        self.engine.device_name = self.settings["output_device"]
        self.engine.buffer.target_ms = int(self.settings["buffer_ms"])
        self.pc.scale = self.settings["pc_volume"] / 100.0
        self.pc.muted = bool(self.settings["pc_muted"])
        self.pc.enabled = bool(self.settings["pc_control"])
        self._set_exclude(self.settings["exclude_apps"])

        self._build_ui()

        self.pc.start()
        self.receiver = Receiver(int(self.settings["port"]), self.engine, self.state)
        self.receiver.start()
        self.discovery = DiscoveryResponder(int(self.settings["port"]))
        self.discovery.start()

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.tick()

    # -- construcción ------------------------------------------------------ #
    def _build_ui(self) -> None:
        tk, ttk = self.tk, self.ttk
        root = self.root
        root.title(APP_NAME)
        root.configure(bg=BG)
        root.minsize(560, 560)

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", background=BG, foreground=FG, fieldbackground=PANEL)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Card.TLabel", background=PANEL, foreground=FG)
        style.configure("Muted.TLabel", background=PANEL, foreground=MUTED)
        style.configure("Title.TLabel", background=BG, foreground=FG, font=("Segoe UI", 18, "bold"))
        style.configure("Status.TLabel", background=BG, font=("Segoe UI", 11, "bold"))
        style.configure("TCheckbutton", background=PANEL, foreground=FG)
        style.map("TCheckbutton", background=[("active", PANEL)])
        style.configure("TButton", background=ACCENT, foreground="white", padding=6, borderwidth=0)
        style.map("TButton", background=[("active", "#6ba0ff")])
        style.configure("Horizontal.TScale", background=PANEL, troughcolor="#2b2f39")
        style.configure("Horizontal.TProgressbar", troughcolor="#2b2f39", background=GOOD, borderwidth=0)
        style.configure("TCombobox", fieldbackground=PANEL, background=PANEL, foreground=FG)

        outer = ttk.Frame(root, padding=16)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="PhoneMix", style="Title.TLabel").pack(anchor="w")
        self.status_lbl = ttk.Label(outer, text="", style="Status.TLabel")
        self.status_lbl.pack(anchor="w", pady=(2, 10))

        # --- Mezclador ---
        mixer = ttk.Frame(outer, style="Card.TFrame", padding=14)
        mixer.pack(fill="x")
        mixer.columnconfigure(1, weight=1)

        self.phone_var = tk.DoubleVar(value=self.settings["phone_volume"])
        self.pc_var = tk.DoubleVar(value=self.settings["pc_volume"])
        self.master_var = tk.DoubleVar(value=100)
        self.phone_mute = tk.BooleanVar(value=bool(self.settings["phone_muted"]))
        self.pc_mute = tk.BooleanVar(value=bool(self.settings["pc_muted"]))

        self.phone_val = self._row(mixer, 0, "Teléfono", self.phone_var, 150, self.on_phone, self.phone_mute, self.on_phone_mute)
        self.meter = ttk.Progressbar(mixer, maximum=100, mode="determinate")
        self.meter.grid(row=1, column=1, sticky="ew", pady=(0, 10))
        self.pc_val = self._row(mixer, 2, "PC (juegos y apps)", self.pc_var, 100, self.on_pc, self.pc_mute, self.on_pc_mute)
        self.master_val = self._row(mixer, 4, "General (Windows)", self.master_var, 100, self.on_master, None, None)
        self.master_scale.bind("<ButtonPress-1>", lambda e: setattr(self, "_drag_master", True))
        self.master_scale.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag_master", False))

        self.phone_val.config(text=f"{int(self.phone_var.get())}%")
        self.pc_val.config(text=f"{int(self.pc_var.get())}%")
        self.master_val.config(text="--")

        btns = ttk.Frame(mixer, style="Card.TFrame")
        btns.grid(row=6, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Button(btns, text="Soltar volumen de la PC", command=self.on_reset_pc).pack(side="left")
        self.pc_note = ttk.Label(btns, text="", style="Muted.TLabel")
        self.pc_note.pack(side="left", padx=10)

        # --- Conexión ---
        conn = ttk.Frame(outer, style="Card.TFrame", padding=14)
        conn.pack(fill="x", pady=(12, 0))
        ttk.Label(conn, text="Conexión", style="Card.TLabel", font=("Segoe UI", 11, "bold")).grid(row=0, column=0, sticky="w")
        ips = local_ips()
        ip_text = "   ".join(ips) if ips else "(sin red)"
        ttk.Label(conn, text=f"IP de esta PC: {ip_text}    Puerto: {self.settings['port']}", style="Card.TLabel").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Label(conn, text="Wi-Fi: usa la IP o pulsa «Buscar PC» en el teléfono.  Bluetooth: usa la IP de la red de anclaje.", style="Muted.TLabel").grid(row=2, column=0, sticky="w", pady=(2, 6))
        row = ttk.Frame(conn, style="Card.TFrame")
        row.grid(row=3, column=0, sticky="w")
        ttk.Button(row, text="Conectar por USB (adb)", command=self.on_usb).pack(side="left")
        self.usb_lbl = ttk.Label(row, text="", style="Muted.TLabel")
        self.usb_lbl.pack(side="left", padx=10)

        # --- Opciones ---
        opts = ttk.Frame(outer, style="Card.TFrame", padding=14)
        opts.pack(fill="x", pady=(12, 0))
        opts.columnconfigure(1, weight=1)
        ttk.Label(opts, text="Salida de audio", style="Card.TLabel").grid(row=0, column=0, sticky="w", pady=4)
        self.device_var = tk.StringVar()
        self.device_box = ttk.Combobox(opts, textvariable=self.device_var, state="readonly")
        self.device_box.grid(row=0, column=1, columnspan=2, sticky="ew", padx=(12, 0))
        self.device_box.bind("<<ComboboxSelected>>", self.on_device)
        self._fill_devices()

        ttk.Label(opts, text="Latencia / estabilidad", style="Card.TLabel").grid(row=1, column=0, sticky="w", pady=4)
        self.buffer_var = tk.DoubleVar(value=self.settings["buffer_ms"])
        ttk.Scale(opts, from_=20, to=300, orient="horizontal", variable=self.buffer_var, command=self.on_buffer).grid(row=1, column=1, sticky="ew", padx=(12, 0))
        self.buffer_val = ttk.Label(opts, text=f"{int(self.buffer_var.get())} ms", style="Card.TLabel", width=7, anchor="e")
        self.buffer_val.grid(row=1, column=2)

        ttk.Label(opts, text="No tocar estas apps", style="Card.TLabel").grid(row=2, column=0, sticky="w", pady=4)
        self.exclude_var = tk.StringVar(value=self.settings["exclude_apps"])
        entry = ttk.Entry(opts, textvariable=self.exclude_var)
        entry.grid(row=2, column=1, columnspan=2, sticky="ew", padx=(12, 0))
        self.exclude_var.trace_add("write", lambda *a: self.on_exclude())
        ttk.Label(opts, text="Ej.: discord, spotify  (nombres de proceso, separados por comas)", style="Muted.TLabel").grid(row=3, column=1, columnspan=2, sticky="w", padx=(12, 0))

        self.info_lbl = ttk.Label(outer, text="", foreground=MUTED)
        self.info_lbl.pack(anchor="w", pady=(10, 0))

    def _row(self, parent, r, text, var, maximum, on_slider, mute_var, on_mute):
        ttk = self.ttk
        ttk.Label(parent, text=text, style="Card.TLabel").grid(row=r, column=0, sticky="w", padx=(0, 14))
        scale = ttk.Scale(parent, from_=0, to=maximum, orient="horizontal", variable=var, command=on_slider)
        scale.grid(row=r, column=1, sticky="ew", pady=6)
        value = ttk.Label(parent, style="Card.TLabel", width=5, anchor="e")
        value.grid(row=r, column=2)
        if mute_var is not None:
            ttk.Checkbutton(parent, text="Silenciar", variable=mute_var, command=on_mute).grid(row=r, column=3, padx=(10, 0))
        if text.startswith("General"):
            self.master_scale = scale
        return value

    def _fill_devices(self) -> None:
        devices, default = list_output_devices()
        default_name = next((n for i, n in devices if i == default), "")
        self._device_names = [n for _, n in devices]
        label = "Predeterminado de Windows" + (f" ({default_name})" if default_name else "")
        self.device_box["values"] = [label] + self._device_names
        saved = self.settings["output_device"]
        self.device_var.set(saved if saved in self._device_names else label)

    # -- eventos ------------------------------------------------------------- #
    @staticmethod
    def _pct(value) -> int:
        return int(round(float(value)))

    def on_phone(self, value) -> None:
        v = self._pct(value)
        self.engine.gain = v / 100.0
        self.phone_val.config(text=f"{v}%")
        self.settings["phone_volume"] = v
        self.schedule_save()

    def on_phone_mute(self) -> None:
        self.engine.muted = self.phone_mute.get()
        self.settings["phone_muted"] = self.engine.muted
        self.schedule_save()

    def _enable_pc_control(self) -> None:
        self.pc.enabled = True
        self.settings["pc_control"] = True

    def on_pc(self, value) -> None:
        if self._programmatic:
            return
        v = self._pct(value)
        self.pc.scale = v / 100.0
        self._enable_pc_control()
        self.pc_val.config(text=f"{v}%")
        self.settings["pc_volume"] = v
        self.schedule_save()

    def on_pc_mute(self) -> None:
        self.pc.muted = self.pc_mute.get()
        self._enable_pc_control()
        self.settings["pc_muted"] = self.pc.muted
        self.schedule_save()

    def on_master(self, value) -> None:
        if self._programmatic:
            return
        v = self._pct(value)
        self.master_val.config(text=f"{v}%")
        self.pc.set_master(v / 100.0)

    def on_reset_pc(self) -> None:
        self.pc.reset()
        self.settings["pc_control"] = False
        self._programmatic = True
        self.pc_var.set(100)
        self.pc_mute.set(False)
        self._programmatic = False
        self.pc.muted = False
        self.pc.scale = 1.0
        self.pc_val.config(text="100%")
        self.schedule_save()

    def on_buffer(self, value) -> None:
        v = self._pct(value)
        self.engine.buffer.target_ms = v
        self.buffer_val.config(text=f"{v} ms")
        self.settings["buffer_ms"] = v
        self.schedule_save()

    def on_device(self, _event=None) -> None:
        name = self.device_var.get()
        self.engine.device_name = name if name in self._device_names else ""
        self.settings["output_device"] = self.engine.device_name
        self.schedule_save()
        try:
            self.engine.restart()
        except Exception as exc:
            self.state.error = f"No pude abrir esa salida: {exc}"

    def _set_exclude(self, text: str) -> None:
        names = set()
        for part in text.split(","):
            part = part.strip().lower()
            if part:
                names.add(part.removesuffix(".exe"))
        self.pc.exclude = names

    def on_exclude(self) -> None:
        text = self.exclude_var.get()
        self._set_exclude(text)
        self.settings["exclude_apps"] = text
        self.schedule_save()

    def on_usb(self) -> None:
        adb = find_adb()
        if not adb:
            self.usb_msg = "No encontré adb. Instala «platform-tools» de Android y agrégalo al PATH."
            return
        self.usb_msg = "Conectando…"
        port = int(self.settings["port"])
        threading.Thread(target=self._run_adb, args=(adb, port), daemon=True).start()

    def _run_adb(self, adb: str, port: int) -> None:
        try:
            flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
            res = subprocess.run(
                [adb, "reverse", f"tcp:{port}", f"tcp:{port}"],
                capture_output=True, text=True, timeout=25, creationflags=flags,
            )
            if res.returncode == 0:
                self.usb_msg = "USB listo: en el teléfono usa 127.0.0.1"
            else:
                self.usb_msg = (res.stderr or res.stdout or "adb falló").strip().splitlines()[-1]
        except Exception as exc:
            self.usb_msg = f"adb falló: {exc}"

    # -- ciclo --------------------------------------------------------------- #
    def tick(self) -> None:
        st = self.state
        if st.error:
            self.status_lbl.config(text="● " + st.error, foreground=BAD)
        elif st.connected:
            self.status_lbl.config(text=f"● Conectado: {st.phone_name} ({st.phone_ip})", foreground=GOOD)
        else:
            self.status_lbl.config(text="● Esperando al teléfono…", foreground=WARN)

        self.meter["value"] = min(100.0, self.engine.level * 100.0)

        if self.pc.master_now is not None and not self._drag_master:
            v = int(round(self.pc.master_now * 100))
            if abs(self.master_var.get() - v) >= 1 or self.master_val.cget("text") == "--":
                self._programmatic = True
                self.master_var.set(v)
                self._programmatic = False
                self.master_val.config(text=f"{v}%")

        if self.pc.error and not self.pc.available:
            self.pc_note.config(text="Falta pycaw: no puedo controlar el volumen de Windows.")
        elif self.pc.enabled:
            self.pc_note.config(text="Controlando el volumen de tus apps.")
        else:
            self.pc_note.config(text="Mueve el control «PC» para empezar a controlarlo.")

        if sd is None:
            self.info_lbl.config(text="Falta sounddevice/PortAudio: " + SD_ERROR, foreground=BAD)
        else:
            b = self.engine.buffer
            self.info_lbl.config(
                text=f"Búfer: {b.fill_ms():.0f} ms   ·   cortes: {b.underruns}   ·   recortes: {b.drops}",
                foreground=MUTED,
            )
        self.usb_lbl.config(text=self.usb_msg)
        self.root.after(100, self.tick)

    def schedule_save(self) -> None:
        if self._save_job is not None:
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(600, self.save_now)

    def save_now(self) -> None:
        self._save_job = None
        save_settings(self.settings)

    def on_close(self) -> None:
        save_settings(self.settings)
        self.receiver.stop()
        self.discovery.stop()
        self.engine.stop()
        self.pc.stop()
        self.pc.join(timeout=2.0)  # restaura los volúmenes originales de las apps
        self.root.destroy()


def main() -> None:
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    import tkinter as tk
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
