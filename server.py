#!/usr/bin/env python3
"""
Mando PC - servidor para el ordenador.

Sirve la interfaz web al móvil y ejecuta en el PC las acciones que llegan
por WebSocket (mover ratón, clics, scroll, atajos, escribir texto...).
También informa de la ventana activa y envía capturas para la vista de pantalla.

Uso:
    python server.py            -> modo normal
    python server.py --simular  -> no toca ratón ni teclado, solo muestra lo que haría
"""
import asyncio
import io
import json
import logging
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from aiohttp import web, WSMsgType

VERSION = "1.1"
FROZEN = getattr(sys, "frozen", False)               # True dentro del .exe
# RES: archivos de la app (página, icono). DATA: configuración y registro.
RES = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
if FROZEN:
    DATA = Path(os.environ.get("APPDATA") or Path.home()) / "MandoPC"
else:
    DATA = Path(__file__).resolve().parent
DATA.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = DATA / "config.json"
WHEEL_DELTA = 120  # unidades de Windows por "muesca" de rueda

log = logging.getLogger("mando")


def detect_lang():
    """Idioma de los avisos del PC: español si Windows (o el sistema) está en español; si no, inglés."""
    forced = os.environ.get("MANDO_LANG", "").lower()
    if forced in ("es", "en"):
        return forced
    try:
        if sys.platform == "win32":
            import ctypes
            return "es" if ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == 0x0A else "en"
        import locale
        loc = os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or (locale.getlocale()[0] or "")
        return "es" if loc.lower().startswith("es") else "en"
    except Exception:
        return "en"


LANG = detect_lang()
EN = {
    "Mando PC · Conectar móvil": "Mando PC · Connect phone",
    "Escanea con la cámara del móvil": "Scan with your phone's camera",
    "El móvil y el PC tienen que estar en la misma red": "Your phone and PC need to be on the same network",
    "Si no carga, prueba con la IP {0}": "If it doesn't load, try the IP {0}",
    " o ": " or ",
    "¡Copiado!": "Copied!", "Copiar enlace": "Copy link", "Cerrar": "Close",
    "Para que tu móvil pueda conectarse, Windows te pedirá permiso una sola vez.\n\nPulsa «Sí» en la siguiente ventana.":
        "So your phone can connect, Windows will ask for permission once.\n\nClick \"Yes\" in the next window.",
    "¿Quieres que Mando PC se inicie solo al encender el PC?\n\nPuedes cambiarlo cuando quieras desde su icono, junto al reloj.":
        "Start Mando PC automatically when your PC turns on?\n\nYou can change this any time from its icon next to the clock.",
    "Conectar móvil…": "Connect phone…", "Iniciar con Windows": "Start with Windows",
    "Permitir la conexión en el firewall": "Allow the connection in the firewall",
    "Abrir carpeta de datos": "Open data folder", "Salir": "Quit",
    "Mando PC ya está en marcha.\n\nBusca su icono junto al reloj (puede estar en la flechita ^).":
        "Mando PC is already running.\n\nLook for its icon next to the clock (it may be under the ^ arrow).",
    "\n  Mando PC {0} en marcha": "\n  Mando PC {0} is running", "  (MODO SIMULACIÓN)": "  (SIMULATION MODE)",
    "  Abre esto en el móvil (misma Wi-Fi):\n": "  Open this on your phone (same Wi-Fi):\n",
    "  Si no carga, prueba cambiando la IP por: {0}\n": "  If it doesn't load, try this IP instead: {0}\n",
    "  Ctrl+C para cerrar.\n": "  Press Ctrl+C to stop.\n",
    "\n  El puerto {0} está ocupado: seguramente Mando PC ya está en marcha en segundo plano. No hace falta abrirlo otra vez.\n":
        "\n  Port {0} is busy: Mando PC is probably already running in the background. No need to open it again.\n",
    "  También está junto al reloj: clic en su icono para ver el QR. Ctrl+C o cerrar esta ventana lo detiene.\n":
        "  It's also next to the clock: click its icon to see the QR. Ctrl+C or closing this window stops it.\n",
}


def T(s, *a):
    """Texto en el idioma del PC (las cadenas se escriben en español)."""
    r = EN.get(s, s) if LANG == "en" else s
    return r.format(*a) if a else r

DEFAULT_CONFIG = {
    "port": 8765,
    "token": "",
    "apps": [
        {"name": "Navegador", "cmd": "start \"\" https://www.google.com"},
        {"name": "Explorador", "cmd": "explorer"},
        {"name": "Bloc de notas", "cmd": "notepad"},
        {"name": "Calculadora", "cmd": "calc"},
        {"name": "Admin. tareas", "cmd": "taskmgr"},
        {"name": "Spotify", "cmd": "start \"\" spotify:"},
    ],
    "shortcuts": [
        {"name": "Captura", "keys": ["win", "shift", "s"]},
        {"name": "Emojis", "keys": ["win", "."]},
        {"name": "Ajustes de Windows", "keys": ["win", "i"]},
    ],
    # Sección TV: webs que se abren en el navegador por defecto ("browser" = abrir el navegador)
    "tv": [
        {"name": "Navegador", "kind": "browser"},
        {"name": "YouTube", "url": "https://www.youtube.com"},
        {"name": "Netflix", "url": "https://www.netflix.com"},
        {"name": "Prime Video", "url": "https://www.primevideo.com"},
        {"name": "Disney+", "url": "https://www.disneyplus.com"},
        {"name": "Movistar Plus+", "url": "https://ver.movistarplus.es"} if LANG == "es"
        else {"name": "Max", "url": "https://play.max.com"},
        {"name": "Twitch", "url": "https://www.twitch.tv"},
        {"name": "Spotify", "url": "https://open.spotify.com"},
    ],
}

MAX_TV = 40


def clean_tv(items):
    """Valida una lista TV enviada por el móvil: solo webs http(s) y el navegador."""
    out = []
    if not isinstance(items, list):
        raise ValueError("lista TV no válida")
    for it in items[:MAX_TV]:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name", "")).strip()[:40]
        if it.get("kind") == "browser":
            out.append({"name": name or "Navegador", "kind": "browser"})
            continue
        url = str(it.get("url", "")).strip()[:500]
        if not re.match(r"^https?://[^\s/$.?#][^\s]*$", url, re.I):
            continue
        out.append({"name": name or url, "url": url})
    return out

# Nombres de tecla que acepta el protocolo -> atributo de pynput.keyboard.Key
PYNPUT_KEYS = {
    "ctrl": "ctrl", "alt": "alt", "shift": "shift", "win": "cmd",
    "tab": "tab", "enter": "enter", "esc": "esc", "backspace": "backspace",
    "delete": "delete", "space": "space",
    "up": "up", "down": "down", "left": "left", "right": "right",
    "home": "home", "end": "end", "pgup": "page_up", "pgdn": "page_down",
    "printscreen": "print_screen",
    "vol_up": "media_volume_up", "vol_down": "media_volume_down",
    "mute": "media_volume_mute", "play_pause": "media_play_pause",
    "next": "media_next", "prev": "media_previous",
    **{f"f{i}": f"f{i}" for i in range(1, 13)},
}


def valid_key(name):
    return isinstance(name, str) and (
        name in PYNPUT_KEYS or (len(name) == 1 and name.isprintable())
    )


# ---------------------------------------------------------------- config

def load_config():
    exists = CONFIG_PATH.exists()
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if exists else {}
    changed = not exists
    for k, v in DEFAULT_CONFIG.items():
        if k not in cfg:
            cfg[k] = json.loads(json.dumps(v))
            changed = True
    if not cfg.get("token"):
        cfg["token"] = secrets.token_urlsafe(9)
        changed = True
    if changed:
        save_config(cfg)
    return cfg


def save_config(cfg):
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)


# ---------------------------------------------------------------- ventanas (Windows)

class WinApi:
    """Ventana en primer plano y acciones sobre ella. Solo en Windows."""

    SW_MINIMIZE, SW_MAXIMIZE, SW_RESTORE = 6, 3, 9
    WM_CLOSE = 0x0010
    DESKTOP_CLASSES = {"Progman", "WorkerW"}
    IGNORE_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd"}

    def __init__(self):
        import ctypes
        from ctypes import wintypes as w
        self.ct = ctypes
        u, k = ctypes.windll.user32, ctypes.windll.kernel32
        u.GetForegroundWindow.restype = w.HWND
        u.GetWindowTextLengthW.argtypes = [w.HWND]
        u.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
        u.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
        u.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
        u.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
        u.IsZoomed.argtypes = [w.HWND]
        u.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
        u.GetSystemMetrics.argtypes = [ctypes.c_int]
        k.OpenProcess.restype = w.HANDLE
        k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        k.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
        k.CloseHandle.argtypes = [w.HANDLE]
        self.u, self.k, self.w = u, k, w
        self.paths = {}       # exe -> ruta completa, para sacar su icono
        self.icons = {}       # ruta -> PNG (o None si no tiene)

    def _class(self, hwnd):
        buf = self.ct.create_unicode_buffer(256)
        self.u.GetClassNameW(hwnd, buf, 256)
        return buf.value

    def foreground(self):
        """-> (hwnd, {"title", "exe"}) o (None, None) si no interesa."""
        hwnd = self.u.GetForegroundWindow()
        if not hwnd:
            return None, None
        cls = self._class(hwnd)
        if cls in self.IGNORE_CLASSES:
            return None, None
        if cls in self.DESKTOP_CLASSES:
            return hwnd, {"title": "Escritorio", "exe": "desktop"}
        n = self.u.GetWindowTextLengthW(hwnd)
        buf = self.ct.create_unicode_buffer(n + 1)
        self.u.GetWindowTextW(hwnd, buf, n + 1)
        pid = self.w.DWORD()
        self.u.GetWindowThreadProcessId(hwnd, self.ct.byref(pid))
        exe = ""
        h = self.k.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            size = self.w.DWORD(1024)
            pbuf = self.ct.create_unicode_buffer(1024)
            if self.k.QueryFullProcessImageNameW(h, 0, pbuf, self.ct.byref(size)):
                exe = os.path.basename(pbuf.value)
                self.paths[exe.lower()] = pbuf.value
            self.k.CloseHandle(h)
        return hwnd, {"title": buf.value, "exe": exe.lower()}

    # ------------------------------------------------ lista de ventanas y cursor de texto

    SWITCHER_CLASSES = {"MultitaskingViewFrame", "XamlExplorerHostIslandWindow", "ForegroundStaging",
                        "TaskSwitcherWnd", "Windows.UI.Core.CoreWindow"}

    def _exe_of(self, hwnd):
        pid = self.w.DWORD()
        self.u.GetWindowThreadProcessId(hwnd, self.ct.byref(pid))
        h = self.k.OpenProcess(0x1000, False, pid.value)
        if not h:
            return ""
        try:
            size = self.w.DWORD(1024)
            pbuf = self.ct.create_unicode_buffer(1024)
            if self.k.QueryFullProcessImageNameW(h, 0, pbuf, self.ct.byref(size)):
                exe = os.path.basename(pbuf.value).lower()
                self.paths[exe] = pbuf.value
                return exe
        finally:
            self.k.CloseHandle(h)
        return ""

    def alt_tab_windows(self, limit=15):
        """Ventanas que salen en Alt+Tab, en el mismo orden (de más a menos reciente)."""
        ct, w = self.ct, self.w
        u = self.u
        u.IsWindowVisible.argtypes = [w.HWND]
        u.GetWindow.argtypes = [w.HWND, w.UINT]
        u.GetWindow.restype = w.HWND
        u.GetWindowLongW.argtypes = [w.HWND, ct.c_int]
        dwm = ct.windll.dwmapi
        out = []

        @ct.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        def cb(hwnd, _):
            try:
                if not u.IsWindowVisible(hwnd) or u.GetWindow(hwnd, 4):          # GW_OWNER
                    return True
                if u.GetWindowLongW(hwnd, -20) & 0x80:                           # WS_EX_TOOLWINDOW
                    return True
                n = u.GetWindowTextLengthW(hwnd)
                if n == 0:
                    return True
                cloaked = ct.c_int(0)
                dwm.DwmGetWindowAttribute(hwnd, 14, ct.byref(cloaked), 4)        # DWMWA_CLOAKED
                if cloaked.value:
                    return True
                cls = self._class(hwnd)
                if cls in self.DESKTOP_CLASSES or cls in self.IGNORE_CLASSES or cls in self.SWITCHER_CLASSES:
                    return True
                buf = ct.create_unicode_buffer(n + 1)
                u.GetWindowTextW(hwnd, buf, n + 1)
                out.append({"title": buf.value, "exe": self._exe_of(hwnd)})
            except Exception:
                pass
            return len(out) < limit

        u.EnumWindows(cb, 0)
        return out

    def caret_active(self):
        """True si la ventana activa tiene un cursor de texto (se está escribiendo en un cuadro)."""
        ct, w = self.ct, self.w

        class GTI(ct.Structure):
            _fields_ = [("cbSize", w.DWORD), ("flags", w.DWORD), ("hwndActive", w.HWND), ("hwndFocus", w.HWND),
                        ("hwndCapture", w.HWND), ("hwndMenuOwner", w.HWND), ("hwndMoveSize", w.HWND),
                        ("hwndCaret", w.HWND), ("rcCaret", w.RECT)]
        g = GTI()
        g.cbSize = ct.sizeof(GTI)
        if not self.u.GetGUIThreadInfo(0, ct.byref(g)):
            return False
        return bool(g.hwndCaret) or bool(g.flags & 1)                             # GUI_CARETBLINKING

    # ------------------------------------------------ iconos

    def default_browser(self):
        """Ruta del navegador por defecto (desde el registro), o None."""
        try:
            import winreg
            key = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                progid = winreg.QueryValueEx(k, "ProgId")[0]
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid + r"\shell\open\command") as k:
                cmd = winreg.QueryValueEx(k, None)[0]
            m = re.match(r'\s*"([^"]+)"|\s*(\S+)', cmd)
            path = m.group(1) or m.group(2)
            if path and os.path.exists(path):
                self.paths[os.path.basename(path).lower()] = path
                return path
        except Exception as e:
            log.debug("navegador por defecto: %s", e)
        return None

    def icon_png(self, path, size=96):
        """Icono de un ejecutable en PNG con transparencia, o None."""
        if path in self.icons:
            return self.icons[path]
        png = None
        try:
            png = self._extract(path, size)
        except Exception as e:
            log.debug("icono de %s: %s", path, e)
        self.icons[path] = png
        return png

    def _extract(self, path, size):
        ct, w = self.ct, self.w
        u, g = ct.windll.user32, ct.windll.gdi32

        class ICONINFO(ct.Structure):
            _fields_ = [("fIcon", w.BOOL), ("xHotspot", w.DWORD), ("yHotspot", w.DWORD),
                        ("hbmMask", w.HBITMAP), ("hbmColor", w.HBITMAP)]

        class BIH(ct.Structure):
            _fields_ = [("biSize", w.DWORD), ("biWidth", w.LONG), ("biHeight", w.LONG), ("biPlanes", w.WORD),
                        ("biBitCount", w.WORD), ("biCompression", w.DWORD), ("biSizeImage", w.DWORD),
                        ("biXPelsPerMeter", w.LONG), ("biYPelsPerMeter", w.LONG),
                        ("biClrUsed", w.DWORD), ("biClrImportant", w.DWORD)]

        u.PrivateExtractIconsW.argtypes = [w.LPCWSTR, ct.c_int, ct.c_int, ct.c_int,
                                           ct.POINTER(w.HICON), ct.POINTER(w.UINT), w.UINT, w.UINT]
        u.PrivateExtractIconsW.restype = w.UINT
        u.GetIconInfo.argtypes = [w.HICON, ct.POINTER(ICONINFO)]
        u.DestroyIcon.argtypes = [w.HICON]
        u.GetDC.restype = w.HDC
        u.GetDC.argtypes = [w.HWND]
        u.ReleaseDC.argtypes = [w.HWND, w.HDC]
        g.GetDIBits.argtypes = [w.HDC, w.HBITMAP, w.UINT, w.UINT, ct.c_void_p, ct.POINTER(BIH), w.UINT]
        g.DeleteObject.argtypes = [w.HGDIOBJ]

        hicon = w.HICON()
        got = u.PrivateExtractIconsW(path, 0, size, size, ct.byref(hicon), None, 1, 0)
        if got in (0, 0xFFFFFFFF) or not hicon:
            return None
        ii = ICONINFO()
        data = None
        try:
            if not u.GetIconInfo(hicon, ct.byref(ii)) or not ii.hbmColor:
                return None
            bih = BIH()
            bih.biSize, bih.biWidth, bih.biHeight = ct.sizeof(BIH), size, -size
            bih.biPlanes, bih.biBitCount, bih.biCompression = 1, 32, 0
            buf = ct.create_string_buffer(size * size * 4)
            hdc = u.GetDC(None)
            try:
                if not g.GetDIBits(hdc, ii.hbmColor, 0, size, buf, ct.byref(bih), 0):
                    return None
                data = bytearray(buf.raw)
                if not any(data[3::4]):                    # icono antiguo sin alfa: usar la máscara
                    mbuf = ct.create_string_buffer(size * size * 4)
                    g.GetDIBits(hdc, ii.hbmMask, 0, size, mbuf, ct.byref(bih), 0)
                    mask = mbuf.raw
                    data[3::4] = bytes(0 if mask[i] else 255 for i in range(0, len(mask), 4))
            finally:
                u.ReleaseDC(None, hdc)
        finally:
            if ii.hbmColor:
                g.DeleteObject(ii.hbmColor)
            if ii.hbmMask:
                g.DeleteObject(ii.hbmMask)
            u.DestroyIcon(hicon)
        from PIL import Image
        im = Image.frombuffer("RGBA", (size, size), bytes(data), "raw", "BGRA", 0, 1)
        out = io.BytesIO()
        im.save(out, "PNG")
        return out.getvalue()

    def window_action(self, action):
        hwnd, info = self.foreground()
        if not hwnd or info["exe"] == "desktop":
            return
        if action == "min":
            self.u.ShowWindow(hwnd, self.SW_MINIMIZE)
        elif action == "max":
            self.u.ShowWindow(hwnd, self.SW_RESTORE if self.u.IsZoomed(hwnd) else self.SW_MAXIMIZE)
        elif action == "close":
            self.u.PostMessageW(hwnd, self.WM_CLOSE, 0, 0)

    def screen_size(self):
        return self.u.GetSystemMetrics(0), self.u.GetSystemMetrics(1)


# ---------------------------------------------------------------- captura de pantalla

class Grabber:
    """Captura el monitor principal en JPEG (mss + Pillow)."""

    def __init__(self):
        import mss  # noqa: F401  (comprobar que están instalados)
        from PIL import Image  # noqa: F401

    def grab(self, width, quality):
        import mss
        from PIL import Image
        factory = getattr(mss, "MSS", None) or mss.mss
        with factory() as s:
            shot = s.grab(s.monitors[1])
        im = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        if im.width > width:
            im = im.resize((width, round(im.height * width / im.width)), Image.BILINEAR)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality)
        return buf.getvalue()


# ---------------------------------------------------------------- entrada

class RealInput:
    """Controla ratón y teclado de verdad con pynput (+ ctypes en Windows)."""

    def __init__(self, winapi=None):
        from pynput.keyboard import Controller as KC, Key
        from pynput.mouse import Controller as MC, Button
        self.kb, self.ms = KC(), MC()
        self.buttons = {"l": Button.left, "r": Button.right, "m": Button.middle}
        self.keys = {n: getattr(Key, a) for n, a in PYNPUT_KEYS.items() if hasattr(Key, a)}
        self.winapi = winapi
        self.win = sys.platform == "win32"
        if self.win:
            import ctypes
            self.user32 = ctypes.windll.user32
        self.rem = [0, 0]

    def _k(self, name):
        return self.keys.get(name, name)

    def press(self, name):
        self.kb.press(self._k(name))

    def release(self, name):
        self.kb.release(self._k(name))

    def move(self, dx, dy):
        self.ms.move(dx, dy)

    def moveto(self, fx, fy):
        """Mueve a una posición relativa (0..1) del monitor principal."""
        if self.winapi:
            w, h = self.winapi.screen_size()
        else:
            w, h = 1920, 1080
        self.ms.position = (int(fx * (w - 1)), int(fy * (h - 1)))

    def button(self, b, down):
        (self.ms.press if down else self.ms.release)(self.buttons[b])

    def wheel(self, v, h):
        """v, h en unidades de Windows (120 = una muesca). v>0 sube, h>0 derecha."""
        if self.win:
            # Windows acepta deltas menores de 120: scroll suave
            if v:
                self.user32.mouse_event(0x0800, 0, 0, v, 0)   # MOUSEEVENTF_WHEEL
            if h:
                self.user32.mouse_event(0x1000, 0, 0, h, 0)   # MOUSEEVENTF_HWHEEL
            return
        self.rem[0] += v
        self.rem[1] += h
        nv, nh = int(self.rem[0] / WHEEL_DELTA), int(self.rem[1] / WHEEL_DELTA)
        self.rem[0] -= nv * WHEEL_DELTA
        self.rem[1] -= nh * WHEEL_DELTA
        if nv or nh:
            self.ms.scroll(nh, nv)

    def type(self, s):
        self.kb.type(s)

    def launch(self, cmd):
        flags = 0x08000000 if self.win else 0  # CREATE_NO_WINDOW
        subprocess.Popen(cmd, shell=True, creationflags=flags)

    def open_url(self, url):
        if self.win:
            os.startfile(url)          # navegador por defecto
        else:
            import webbrowser
            webbrowser.open(url)

    def open_browser(self, path):
        if path:
            subprocess.Popen([path])
        else:
            self.open_url("https://www.google.com")

    def power(self, action):
        """Bloquear, suspender, reiniciar o apagar (reiniciar/apagar con 10 s para cancelar)."""
        if not self.win:
            log.info("[energía] %s (solo funciona en Windows)", action)
            return
        import ctypes
        no_window = 0x08000000
        if action == "lock":
            ctypes.windll.user32.LockWorkStation()
        elif action == "sleep":
            ctypes.windll.powrprof.SetSuspendState(False, False, False)
        elif action == "restart":
            subprocess.Popen(["shutdown", "/r", "/t", "10"], creationflags=no_window)
        elif action == "shutdown":
            subprocess.Popen(["shutdown", "/s", "/t", "10"], creationflags=no_window)
        elif action == "cancel":
            subprocess.Popen(["shutdown", "/a"], creationflags=no_window)

    def window(self, action):
        if self.winapi:
            self.winapi.window_action(action)
        else:  # sin WinAPI: atajos equivalentes
            combo = {"min": ["win", "down"], "max": ["win", "up"], "close": ["alt", "f4"]}[action]
            ks = [self._k(k) for k in combo]
            for k in ks:
                self.kb.press(k)
            for k in reversed(ks):
                self.kb.release(k)


class Halo:
    """Anillo amarillo alrededor del cursor para verlo de lejos. Ventana transparente que no recibe clics."""

    def __init__(self):
        self.on = False
        self.thread = None

    def set(self, on):
        self.on = bool(on)
        if self.on and not (self.thread and self.thread.is_alive()) and IS_WIN:
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
        log.info("Cursor resaltado: %s", "sí" if self.on else "no")

    def _run(self):
        try:
            import ctypes
            import tkinter as tk
            from ctypes import wintypes as w
            u = ctypes.windll.user32
            root = tk.Tk()
            root.overrideredirect(True)
            root.attributes("-topmost", True)
            key = "#FF00FF"                                  # este color se vuelve transparente
            root.configure(bg=key)
            root.attributes("-transparentcolor", key)
            size = max(100, root.winfo_screenheight() // 8)
            c = tk.Canvas(root, width=size, height=size, bg=key, highlightthickness=0)
            c.pack()
            m = 8
            c.create_oval(m - 3, m - 3, size - m + 3, size - m + 3, outline="#111111", width=3)   # borde oscuro: se ve sobre fondos claros
            c.create_oval(m, m, size - m, size - m, outline="#FFD60A", width=max(6, size // 16))
            c.create_oval(m + 9, m + 9, size - m - 9, size - m - 9, outline="#111111", width=2)
            root.update_idletasks()
            hwnd = u.GetParent(root.winfo_id())
            u.GetWindowLongW.argtypes = [w.HWND, ctypes.c_int]
            u.SetWindowLongW.argtypes = [w.HWND, ctypes.c_int, ctypes.c_long]
            ex = u.GetWindowLongW(hwnd, -20)
            u.SetWindowLongW(hwnd, -20, ex | 0x80000 | 0x20 | 0x80 | 0x08000000)   # capas, transparente a clics, sin barra de tareas, sin foco
            pt = w.POINT()

            def tick():
                if not self.on:
                    root.destroy()
                    return
                u.GetCursorPos(ctypes.byref(pt))
                root.geometry(f"{size}x{size}+{pt.x - size // 2}+{pt.y - size // 2}")
                root.attributes("-topmost", True)
                root.after(16, tick)
            tick()
            root.mainloop()
        except Exception as e:
            log.warning("No se pudo resaltar el cursor: %s", e)


class Volume:
    """Volumen del sistema con pycaw. Si no está instalado, la app usa las teclas de volumen."""

    def __init__(self):
        import pycaw.pycaw  # noqa: F401  (comprobar que está)

    def _ev(self):
        import comtypes
        try:
            comtypes.CoInitialize()
        except OSError:
            pass
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        spk = AudioUtilities.GetSpeakers()
        ev = getattr(spk, "EndpointVolume", None)            # pycaw nuevo
        if ev is None:                                       # pycaw antiguo
            from ctypes import cast, POINTER
            from comtypes import CLSCTX_ALL
            ev = cast(spk.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None), POINTER(IAudioEndpointVolume))
        return ev

    def get(self):
        ev = self._ev()
        return round(ev.GetMasterVolumeLevelScalar() * 100), bool(ev.GetMute())

    def set(self, level):
        self._ev().SetMasterVolumeLevelScalar(max(0, min(100, level)) / 100, None)

    def mute(self, on):
        self._ev().SetMute(1 if on else 0, None)


class DryInput:
    """Modo simulación: solo registra lo que haría."""

    def __getattr__(self, name):
        return lambda *a: log.info("[sim] %s %s", name, a)


# ---------------------------------------------------------------- sesión

class Session:
    """Estado de un móvil conectado. Al desconectarse suelta todo lo pulsado."""

    def __init__(self, inp, cfg, winapi=None, volume=None, halo=None):
        self.inp, self.cfg, self.winapi, self.volume, self.halo = inp, cfg, winapi, volume, halo
        self.rv = self.rh = 0.0
        self.held_keys = []
        self.held_btns = set()

    def press(self, k):
        self.inp.press(k)
        if k not in self.held_keys:
            self.held_keys.append(k)

    def release(self, k):
        self.inp.release(k)
        if k in self.held_keys:
            self.held_keys.remove(k)

    def tap(self, k):
        self.press(k)
        self.release(k)

    def combo(self, keys):
        if not isinstance(keys, list) or not 0 < len(keys) <= 5 or not all(map(valid_key, keys)):
            raise ValueError(f"combinación no válida: {keys!r}")
        for k in keys:
            self.press(k)
        for k in reversed(keys):
            self.release(k)

    def scroll(self, y, x):
        self.rv += y * WHEEL_DELTA
        self.rh += x * WHEEL_DELTA
        iv, ih = int(self.rv), int(self.rh)
        self.rv -= iv
        self.rh -= ih
        if iv or ih:
            self.inp.wheel(iv, ih)

    async def handle(self, m):
        t = m.get("t")
        if t == "m":
            dx = max(-800, min(800, int(m.get("x", 0))))
            dy = max(-800, min(800, int(m.get("y", 0))))
            self.inp.move(dx, dy)
        elif t == "abs":  # posición absoluta desde la vista de pantalla
            fx = max(0.0, min(1.0, float(m.get("x", 0))))
            fy = max(0.0, min(1.0, float(m.get("y", 0))))
            self.inp.moveto(fx, fy)
            b = m.get("b")
            if b in ("l", "r"):
                self.inp.button(b, True)
                self.inp.button(b, False)
        elif t == "c":
            b = m.get("b", "l")
            for _ in range(max(1, min(3, int(m.get("n", 1))))):
                self.inp.button(b, True)
                self.inp.button(b, False)
        elif t == "bd":
            b = m.get("b", "l")
            self.inp.button(b, True)
            self.held_btns.add(b)
        elif t == "bu":
            b = m.get("b", "l")
            self.inp.button(b, False)
            self.held_btns.discard(b)
        elif t == "s":
            self.scroll(max(-20.0, min(20.0, float(m.get("y", 0)))),
                        max(-20.0, min(20.0, float(m.get("x", 0)))))
        elif t == "z":
            d = max(-5, min(5, int(m.get("d", 0))))
            if d:
                self.press("ctrl")
                self.inp.wheel(d * WHEEL_DELTA, 0)
                self.release("ctrl")
        elif t == "k":
            self.combo(m.get("k"))
        elif t == "txt":
            s = str(m.get("s", ""))[:2000]
            if s:
                await asyncio.get_running_loop().run_in_executor(None, self.inp.type, s)
            if m.get("enter"):
                self.tap("enter")
        elif t == "at":  # Alt+Tab con el dial
            a = m.get("a")
            if a == "start":
                wins = []
                if self.winapi:                  # la lista, antes de que aparezca el selector de Windows
                    try:
                        wins = self.winapi.alt_tab_windows()
                    except Exception as e:
                        log.debug("lista de ventanas: %s", e)
                self.press("alt")
                self.tap("tab")
                return {"t": "wins", "list": wins}
            elif a == "next" and "alt" in self.held_keys:
                self.tap("tab")
            elif a == "prev" and "alt" in self.held_keys:
                self.press("shift")
                self.tap("tab")
                self.release("shift")
            elif a == "end":
                if "alt" in self.held_keys:
                    self.release("alt")
        elif t == "wnd":
            a = m.get("a")
            if a in ("min", "max", "close"):
                self.inp.window(a)
        elif t == "app":
            apps = self.cfg.get("apps", [])
            i = int(m.get("i", -1))
            if 0 <= i < len(apps):
                log.info("Abriendo %s", apps[i]["name"])
                self.inp.launch(apps[i]["cmd"])
        elif t == "tv":
            items = self.cfg.get("tv", [])
            i = int(m.get("i", -1))
            if 0 <= i < len(items):
                it = items[i]
                log.info("Abriendo %s", it.get("name"))
                if it.get("kind") == "browser":
                    self.inp.open_browser(self.winapi.default_browser() if self.winapi else None)
                elif it.get("url"):
                    self.inp.open_url(it["url"])
        elif t == "tvset":
            self.cfg["tv"] = clean_tv(m.get("list"))
            save_config(self.cfg)
            log.info("Sección TV actualizada (%d elementos)", len(self.cfg["tv"]))
        elif t == "power":
            a = m.get("a")
            if a in ("lock", "sleep", "restart", "shutdown", "cancel"):
                log.info("Energía: %s", a)
                if a == "sleep":
                    await asyncio.sleep(0.8)          # que el móvil reciba la respuesta antes de dormir
                await asyncio.get_running_loop().run_in_executor(None, self.inp.power, a)
        elif t == "halo":
            if self.halo:
                self.halo.set(m.get("on"))
        elif t == "vol":
            if "step" in m:                      # sin volumen real: pasos de 2 % con las teclas
                n = max(-50, min(50, int(m["step"])))
                for _ in range(abs(n)):
                    self.tap("vol_up" if n > 0 else "vol_down")
                return None
            vol = self.volume
            if vol is None:
                if "mute" in m:
                    self.tap("mute")
                return {"t": "vol", "level": None, "mute": None}
            if "set" in m:
                vol.set(float(m["set"]))
            if "mute" in m:
                vol.mute(bool(m["mute"]))
            level, muted = vol.get()
            return {"t": "vol", "level": level, "mute": muted}
        elif t == "ping":
            pass
        else:
            raise ValueError(f"mensaje desconocido: {t!r}")

    def release_all(self):
        for k in list(reversed(self.held_keys)):
            try:
                self.release(k)
            except Exception:
                pass
        for b in list(self.held_btns):
            try:
                self.inp.button(b, False)
            except Exception:
                pass
        self.held_btns.clear()


# ---------------------------------------------------------------- web

def token_ok(request):
    return secrets.compare_digest(request.query.get("k", ""), request.app["cfg"]["token"])


async def index(request):
    return web.FileResponse(RES / "index.html", headers={"Cache-Control": "no-cache"})


async def manifest(request):
    return web.json_response({
        "name": "Mando PC", "short_name": "Mando PC",
        "start_url": "/", "scope": "/", "display": "fullscreen", "orientation": "portrait",
        "background_color": "#050505", "theme_color": "#050505",
        "icons": [{"src": "/icon.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}],
    }, content_type="application/manifest+json")


async def icon(request):
    return web.FileResponse(RES / "icon.png", headers={"Cache-Control": "max-age=86400"})


async def config(request):
    if not token_ok(request):
        raise web.HTTPUnauthorized()
    cfg = request.app["cfg"]
    winapi = request.app["winapi"]
    browser = winapi.default_browser() if winapi else None
    return web.json_response({
        "version": VERSION,
        "host": socket.gethostname(),
        "screen": request.app["grabber"] is not None,
        "icons": winapi is not None,
        "browser": os.path.basename(browser).lower() if browser else "",
        "tv": cfg.get("tv", []),
        "apps": [a["name"] for a in cfg.get("apps", [])],
        "shortcuts": [s for s in cfg.get("shortcuts", []) if all(map(valid_key, s.get("keys", [])))],
    })


async def appicon(request):
    """Icono real de una app: /appicon/chrome.exe o /appicon/browser."""
    if not token_ok(request):
        raise web.HTTPUnauthorized()
    winapi = request.app["winapi"]
    if winapi is None:
        raise web.HTTPNotFound()
    name = request.match_info["name"].lower()
    path = winapi.default_browser() if name == "browser" else winapi.paths.get(name)
    if not path:
        raise web.HTTPNotFound()
    png = await asyncio.get_running_loop().run_in_executor(None, winapi.icon_png, path)
    if not png:
        raise web.HTTPNotFound()
    return web.Response(body=png, content_type="image/png", headers={"Cache-Control": "max-age=3600"})


async def screen(request):
    if not token_ok(request):
        raise web.HTTPUnauthorized()
    grabber = request.app["grabber"]
    if grabber is None:
        raise web.HTTPServiceUnavailable(text="Falta mss o Pillow en el PC")
    width = max(320, min(2560, int(request.query.get("w", 1280))))
    quality = max(20, min(90, int(request.query.get("q", 60))))
    try:
        data = await asyncio.get_running_loop().run_in_executor(None, grabber.grab, width, quality)
    except Exception as e:
        log.warning("No se pudo capturar la pantalla: %s", e)
        raise web.HTTPServiceUnavailable(text="No se pudo capturar la pantalla")
    return web.Response(body=data, content_type="image/jpeg", headers={"Cache-Control": "no-store"})


async def watch_window(ws, winapi):
    """Envía al móvil la ventana activa cada vez que cambia."""
    last = None
    if winapi is None:
        await ws.send_json({"t": "win", "title": "", "exe": ""})
        return
    caret = None
    while not ws.closed:
        try:
            _, info = winapi.foreground()
            if info and info != last:
                last = info
                await ws.send_json({"t": "win", **info})
            c = winapi.caret_active()
            if c != caret:                       # cuadro de texto activo en el PC: el móvil ofrece el teclado
                caret = c
                await ws.send_json({"t": "caret", "on": c})
        except Exception as e:
            log.debug("ventana activa: %s", e)
        await asyncio.sleep(0.35)


async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=10)
    await ws.prepare(request)
    if not token_ok(request):
        log.warning("Conexión rechazada (código incorrecto) desde %s", request.remote)
        await ws.close(code=4001, message=b"token")
        return ws
    log.info("Móvil conectado: %s", request.remote)
    s = Session(request.app["inp"], request.app["cfg"], request.app["winapi"], request.app["volume"], request.app["halo"])
    watcher = asyncio.create_task(watch_window(ws, request.app["winapi"]))
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                reply = await s.handle(json.loads(msg.data))
                if reply:
                    await ws.send_json(reply)
            except Exception as e:
                log.warning("Error con %s: %s", msg.data[:120], e)
    finally:
        watcher.cancel()
        s.release_all()
        log.info("Móvil desconectado: %s", request.remote)
    return ws


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def other_ips(main_ip):
    try:
        ips = {a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    except Exception:
        return []
    return sorted(ip for ip in ips if ip != main_ip and not ip.startswith("127."))


# ---------------------------------------------------------------- escritorio de Windows
# Icono junto al reloj, ventana con el QR, inicio con Windows y permiso del firewall.

APP_NAME = "Mando PC"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
NO_WINDOW = 0x08000000
IS_WIN = sys.platform == "win32"


def message_box(text, yes_no=False):
    """Aviso nativo de Windows. Con yes_no devuelve True si se pulsa Sí."""
    if not IS_WIN:
        log.info("[aviso] %s", text)
        return False
    import ctypes
    flags = (0x04 | 0x20) if yes_no else 0x40           # MB_YESNO|MB_ICONQUESTION  /  MB_ICONINFORMATION
    flags |= 0x10000 | 0x40000                          # MB_SETFOREGROUND | MB_TOPMOST
    return ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, flags) == 6


def _startup_lnk():
    return Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs\Startup\Mando PC.lnk"


def autostart_command():
    if FROZEN:
        return f'"{sys.executable}"'
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    return f'"{pyw if pyw.exists() else exe}" "{Path(__file__).resolve()}"'


def autostart_enabled():
    if not IS_WIN:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return _startup_lnk().exists()


def set_autostart(on):
    if not IS_WIN:
        return
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        if on:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except OSError:
                pass
    try:                                                # el acceso directo antiguo de los .bat
        _startup_lnk().unlink()
    except OSError:
        pass
    log.info("Iniciar con Windows: %s", "sí" if on else "no")


def firewall_ok():
    if not IS_WIN:
        return True
    try:
        r = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={APP_NAME}"],
                           capture_output=True, creationflags=NO_WINDOW, timeout=10)
        return r.returncode == 0
    except Exception:
        return True                                     # ante la duda, no molestar


def request_firewall(port):
    """Pide permiso de administrador una vez para dejar entrar al móvil por el puerto."""
    if not IS_WIN:
        return False
    import ctypes
    params = (f'advfirewall firewall add rule name="{APP_NAME}" dir=in action=allow '
              f'protocol=TCP localport={port} profile=private,public')
    ok = ctypes.windll.shell32.ShellExecuteW(None, "runas", "netsh", params, None, 0) > 32
    log.info("Regla del firewall %s", "solicitada" if ok else "rechazada")
    return ok


_qr_open = threading.Lock()


def show_qr_window(url, others=()):
    """Ventana con el QR para conectar el móvil (en su propio hilo)."""
    if not _qr_open.acquire(blocking=False):
        return                                          # ya está abierta

    def run():
        try:
            import tkinter as tk
            import qrcode
            from PIL import Image, ImageTk
            BG, FG, MUT = "#111111", "#F2F2F2", "#9A9A9A"
            root = tk.Tk()
            root.title(T("Mando PC · Conectar móvil"))
            root.configure(bg=BG, padx=28, pady=24)
            root.resizable(False, False)
            try:
                root.iconphoto(True, ImageTk.PhotoImage(Image.open(RES / "icon.png")))
            except Exception:
                pass
            qr = qrcode.QRCode(box_size=10, border=2)
            qr.add_data(url)
            img = qr.make_image(fill_color="black", back_color="white")
            img = getattr(img, "get_image", lambda: img)().convert("RGB").resize((300, 300), Image.NEAREST)
            photo = ImageTk.PhotoImage(img)
            font = "Segoe UI" if IS_WIN else "TkDefaultFont"
            tk.Label(root, text=T("Escanea con la cámara del móvil"), bg=BG, fg=FG,
                     font=(font, 15, "bold")).pack(pady=(0, 4))
            tk.Label(root, text=T("El móvil y el PC tienen que estar en la misma red"), bg=BG, fg=MUT,
                     font=(font, 10)).pack(pady=(0, 14))
            tk.Label(root, image=photo, bg="white", bd=0).pack()
            e = tk.Entry(root, font=(font, 10), justify="center", relief="flat", bg="#1E1E1E", fg=FG,
                         readonlybackground="#1E1E1E", insertbackground=FG)
            e.insert(0, url)
            e.configure(state="readonly")
            e.pack(fill="x", pady=(16, 4), ipady=6)
            if others:
                tk.Label(root, text=T("Si no carga, prueba con la IP {0}", T(" o ").join(others)), bg=BG, fg=MUT,
                         font=(font, 9)).pack()
            row = tk.Frame(root, bg=BG)
            row.pack(pady=(14, 0))

            def copy():
                root.clipboard_clear()
                root.clipboard_append(url)
                b1.configure(text=T("¡Copiado!"))
            b1 = tk.Button(row, text=T("Copiar enlace"), command=copy, relief="flat", bg="#2A2A2A", fg=FG,
                           activebackground="#3A3A3A", activeforeground=FG, font=(font, 10), padx=16, pady=6)
            b1.pack(side="left", padx=4)
            tk.Button(row, text=T("Cerrar"), command=root.destroy, relief="flat", bg=FG, fg=BG,
                      activebackground="#FFFFFF", font=(font, 10, "bold"), padx=22, pady=6).pack(side="left", padx=4)
            root.attributes("-topmost", True)
            root.after(400, lambda: root.attributes("-topmost", False))
            root.focus_force()
            root.mainloop()
        except Exception as ex:
            log.warning("No se pudo abrir la ventana del QR: %s", ex)
        finally:
            _qr_open.release()

    threading.Thread(target=run, daemon=True).start()


class ServerThread(threading.Thread):
    """El servidor web en segundo plano, para que el hilo principal lleve el icono."""

    def __init__(self, app, port):
        super().__init__(daemon=True)
        self.app, self.port = app, port
        self.ready, self.error, self.loop = threading.Event(), None, None

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        runner = web.AppRunner(self.app, access_log=None)
        try:
            self.loop.run_until_complete(runner.setup())
            self.loop.run_until_complete(web.TCPSite(runner, "0.0.0.0", self.port).start())
        except Exception as e:                          # puerto ocupado u otro fallo al arrancar
            self.error = e
            self.ready.set()
            try:
                self.loop.run_until_complete(runner.cleanup())
            except Exception:
                pass
            return
        self.ready.set()
        self.loop.run_forever()
        self.loop.run_until_complete(runner.cleanup())

    def stop(self):
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)


def first_run_setup(cfg, url, others):
    """La primera vez: permiso del firewall, inicio con Windows y QR. Sin consola ni archivos .bat."""
    port = cfg["port"]
    changed = False
    if IS_WIN and not cfg.get("firewall_asked") and not firewall_ok():
        message_box(T("Para que tu móvil pueda conectarse, Windows te pedirá permiso una sola vez.\n\n"
                      "Pulsa «Sí» en la siguiente ventana."))
        request_firewall(port)
        cfg["firewall_asked"] = changed = True
    if IS_WIN and not cfg.get("autostart_asked") and not autostart_enabled():
        if message_box(T("¿Quieres que Mando PC se inicie solo al encender el PC?\n\n"
                         "Puedes cambiarlo cuando quieras desde su icono, junto al reloj."), yes_no=True):
            set_autostart(True)
        cfg["autostart_asked"] = changed = True
    if not cfg.get("welcomed"):
        show_qr_window(url, others)
        cfg["welcomed"] = changed = True
    if changed:
        save_config(cfg)


def run_tray(cfg, url, others, on_quit):
    import pystray
    from PIL import Image
    fw = {"ok": firewall_ok()}

    def qr(icon, item):
        show_qr_window(url, others)

    def toggle_autostart(icon, item):
        set_autostart(not autostart_enabled())

    def fix_firewall(icon, item):
        request_firewall(cfg["port"])
        time.sleep(3)
        fw["ok"] = firewall_ok()
        icon.update_menu()

    def open_data(icon, item):
        os.startfile(DATA) if IS_WIN else None

    def quit_(icon, item):
        icon.visible = False
        icon.stop()
        on_quit()

    menu = pystray.Menu(
        pystray.MenuItem(f"Mando PC {VERSION}", None, enabled=False),
        pystray.MenuItem(T("Conectar móvil…"), qr, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(T("Iniciar con Windows"), toggle_autostart, checked=lambda item: autostart_enabled()),
        pystray.MenuItem(T("Permitir la conexión en el firewall"), fix_firewall, visible=lambda item: not fw["ok"]),
        pystray.MenuItem(T("Abrir carpeta de datos"), open_data),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(T("Salir"), quit_),
    )
    host = url.split("//", 1)[1].split("/", 1)[0]
    icon = pystray.Icon("MandoPC", Image.open(RES / "icon.png"), f"Mando PC · {host}", menu)
    icon.run(setup=lambda ic: (setattr(ic, "visible", True), first_run_setup(cfg, url, others)))


def main():
    # Sin consola (.exe o arranque automático con pythonw): el registro va a mando.log
    handlers = None
    if FROZEN or sys.stdout is None or sys.stderr is None:
        handlers = [logging.FileHandler(DATA / "mando.log", encoding="utf-8")]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%d/%m %H:%M:%S",
                        handlers=handlers)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if sys.platform == "win32":
        try:  # coordenadas reales en pantallas con escalado
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass

    cfg = load_config()
    simulate = "--simular" in sys.argv

    winapi = None
    if sys.platform == "win32":
        try:
            winapi = WinApi()
        except Exception as e:
            log.warning("Sin información de ventanas: %s", e)

    grabber = None
    try:
        grabber = Grabber()
    except Exception:
        log.warning("Vista de pantalla desactivada: instala mss y Pillow (pip install -r requirements.txt)")

    inp = None
    if not simulate:
        try:
            inp = RealInput(winapi)
        except Exception as e:
            log.warning("No se pudo controlar ratón/teclado (%s). Arrancando en modo simulación.", e)
            simulate = True
    if simulate:
        inp = DryInput()

    app = web.Application()
    volume = None
    if sys.platform == "win32" and not simulate:
        try:
            volume = Volume()
            volume.get()
        except Exception as e:
            volume = None
            log.warning("Volumen exacto no disponible (instala pycaw); se usarán las teclas de volumen: %s", e)
    app["cfg"], app["inp"], app["winapi"], app["grabber"], app["volume"] = cfg, inp, winapi, grabber, volume
    app["halo"] = Halo()
    app.router.add_get("/", index)
    app.router.add_get("/config", config)
    app.router.add_get("/manifest.webmanifest", manifest)
    app.router.add_get("/icon.png", icon)
    app.router.add_get("/appicon/{name}", appicon)
    app.router.add_get("/screen", screen)
    app.router.add_get("/ws", ws_handler)

    ip = local_ip()
    url = f"http://{ip}:{cfg['port']}/?k={cfg['token']}"
    log.info("Mando PC %s en marcha en %s", VERSION, url.split("?")[0])
    print(T("\n  Mando PC {0} en marcha", VERSION) + (T("  (MODO SIMULACIÓN)") if simulate else ""))
    print(T("  Abre esto en el móvil (misma Wi-Fi):\n"))
    print("  " + url + "\n")
    others = other_ips(ip)
    if others:
        print(T("  Si no carga, prueba cambiando la IP por: {0}\n", ", ".join(others)))
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.print_ascii(invert=True)
    except Exception:
        pass
    # Con icono junto al reloj si se puede (pystray); si no, como siempre, en primer plano
    tray = "--sin-icono" not in sys.argv
    if tray:
        try:
            import pystray  # noqa: F401
        except Exception as e:
            log.warning("Sin icono junto al reloj (instala pystray): %s", e)
            tray = False

    if not tray:
        print(T("  Ctrl+C para cerrar.\n"))
        try:
            web.run_app(app, host="0.0.0.0", port=cfg["port"], print=None, access_log=None)
        except OSError as e:
            log.error("No se pudo abrir el puerto %s: ¿ya hay otro Mando PC en marcha? (%s)", cfg["port"], e)
            print(T("\n  El puerto {0} está ocupado: seguramente Mando PC ya está en marcha"
                    " en segundo plano. No hace falta abrirlo otra vez.\n", cfg["port"]))
        return

    print(T("  También está junto al reloj: clic en su icono para ver el QR. Ctrl+C o cerrar esta ventana lo detiene.\n"))
    srv = ServerThread(app, cfg["port"])
    srv.start()
    srv.ready.wait(15)
    if srv.error:
        log.error("Puerto %s ocupado: Mando PC ya está en marcha (%s)", cfg["port"], srv.error)
        message_box(T("Mando PC ya está en marcha.\n\nBusca su icono junto al reloj (puede estar en la flechita ^)."))
        return
    try:
        run_tray(cfg, url, others, srv.stop)
    except Exception as e:                              # sin escritorio disponible: seguir sin icono
        log.warning("No se pudo mostrar el icono (%s); sigue funcionando sin él", e)
        try:
            srv.join()
        except KeyboardInterrupt:
            pass
    srv.stop()
    srv.join(3)
    log.info("Mando PC cerrado")


if __name__ == "__main__":
    main()
