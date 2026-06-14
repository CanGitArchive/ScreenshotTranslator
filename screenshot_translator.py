#!/usr/bin/env python3
r"""
ScreenshotTranslator V4.1

Tray-only instant OCR screenshot tool.

V2.2 scope:
- System tray OCR Language menu. First-install default is All-arounder / Auto.
- Ctrl + PrintScreen: region screenshot only
  -> DATA/ScreenshotTranslator/Screenshots/YYYY-MM/

- Shift + PrintScreen: region screenshot + immediate OCR only
  -> DATA/ScreenshotTranslator/OCR Screenshots/YYYY-MM/
  -> DATA/ScreenshotTranslator/OCRText/YYYY-MM/
  -> plain text popup
  -> OCR result is copied to clipboard

- Alt + PrintScreen: OCR + DeepSeek translation
  -> DATA/ScreenshotTranslator/Translation Screenshots/YYYY-MM/
  -> V2.2 OCRs, translates with DeepSeek, shows translation, and copies translation

DeepSeek/API translation is enabled for Alt + PrintScreen. Shift + PrintScreen remains OCR-only.

Install:
    pip install -r requirements.txt

Run:
    python screenshot_translator.py

Recommended .venv run:
    .\.venv\Scripts\python.exe .\screenshot_translator.py

PyInstaller onedir-first note:
    Use onedir first for PaddleOCR/Paddle/PaddleX.
    One-file may be possible later, but onedir is safer.
"""

from __future__ import annotations

import ctypes
import json
import os
import platform
import shutil
import subprocess
import sys
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Tuple

from dotenv import load_dotenv
from PIL import Image
import mss

# ---------------------------------------------------------------------------
# Windows no-console subprocess patch
# ---------------------------------------------------------------------------

def patch_windows_child_process_consoles() -> None:
    """
    Prevent console windows from flashing when third-party OCR libraries spawn
    child processes.

    Important:
    Do NOT replace subprocess.Popen with a normal function. asyncio on Windows
    subclasses subprocess.Popen, so Popen must remain a class.
    """
    if not sys.platform.startswith("win"):
        return

    if getattr(subprocess.Popen, "_screenshot_translator_no_console_patch", False):
        return

    original_popen = subprocess.Popen

    class NoConsolePopen(original_popen):
        _screenshot_translator_no_console_patch = True

        def __init__(self, *args, **kwargs):
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | getattr(
                subprocess, "CREATE_NO_WINDOW", 0
            )

            try:
                startupinfo = kwargs.get("startupinfo")
                if startupinfo is None:
                    startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 0
                kwargs["startupinfo"] = startupinfo
            except Exception:
                pass

            super().__init__(*args, **kwargs)

    subprocess.Popen = NoConsolePopen


patch_windows_child_process_consoles()


# ---------------------------------------------------------------------------
# PaddleOCR import diagnostics
# ---------------------------------------------------------------------------

PADDLEOCR_IMPORT_ERROR: BaseException | None = None
PADDLEOCR_IMPORT_TRACEBACK = ""

try:
    from paddleocr import PaddleOCR
except Exception as exc:
    PaddleOCR = None
    PADDLEOCR_IMPORT_ERROR = exc
    PADDLEOCR_IMPORT_TRACEBACK = traceback.format_exc()


# ---------------------------------------------------------------------------
# PyQt imports
# ---------------------------------------------------------------------------

from PyQt6.QtCore import QAbstractNativeEventFilter, QObject, QPoint, QRect, QThread, QTimer, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QAction, QActionGroup, QColor, QFont, QIcon, QImage, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

try:
    import keyboard  # type: ignore
except Exception:
    keyboard = None


# ---------------------------------------------------------------------------
# Native Windows global hotkeys
# ---------------------------------------------------------------------------

WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

VK_CODES: dict[str, int] = {
    "print screen": 0x2C,
    "snapshot": 0x2C,
    "insert": 0x2D,
    "delete": 0x2E,
    "home": 0x24,
    "end": 0x23,
    "page up": 0x21,
    "page down": 0x22,
    "tab": 0x09,
    "space": 0x20,
    "enter": 0x0D,
    "return": 0x0D,
    "escape": 0x1B,
    "esc": 0x1B,
    "backspace": 0x08,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
}

for _i in range(1, 25):
    VK_CODES[f"f{_i}"] = 0x70 + (_i - 1)

for _ch in "abcdefghijklmnopqrstuvwxyz":
    VK_CODES[_ch] = ord(_ch.upper())

for _ch in "0123456789":
    VK_CODES[_ch] = ord(_ch)


def parse_windows_hotkey(combo: str) -> tuple[int, int]:
    combo = normalize_hotkey_text(combo)
    parts = [p.strip() for p in combo.split("+") if p.strip()]

    modifiers = MOD_NOREPEAT
    key_name = ""

    for part in parts:
        if part in {"ctrl", "control"}:
            modifiers |= MOD_CONTROL
        elif part == "shift":
            modifiers |= MOD_SHIFT
        elif part == "alt":
            modifiers |= MOD_ALT
        elif part in {"win", "windows", "meta", "cmd"}:
            modifiers |= MOD_WIN
        else:
            key_name = part

    if not key_name:
        raise ValueError(f"No non-modifier key found in hotkey: {combo!r}")

    vk = VK_CODES.get(key_name)
    if vk is None:
        raise ValueError(f"Unsupported key for native Windows hotkey: {key_name!r}")

    return modifiers, vk


class NativeHotkeyFilter(QAbstractNativeEventFilter):
    """
    Windows WM_HOTKEY filter.

    This replaces keyboard.add_hotkey() for normal app operation. It avoids the
    low-level suppression hook that caused Ctrl/Shift/Alt to get logically stuck.
    """

    def __init__(self, owner) -> None:
        super().__init__()
        self.owner = owner

    def nativeEventFilter(self, event_type, message):  # noqa: N802
        if not sys.platform.startswith("win"):
            return False, 0

        try:
            msg = ctypes.wintypes.MSG.from_address(int(message))
        except Exception:
            return False, 0

        if msg.message == WM_HOTKEY:
            try:
                hotkey_id = int(msg.wParam)
                self.owner.on_native_hotkey(hotkey_id)
                return True, 0
            except Exception:
                return False, 0

        return False, 0


# ---------------------------------------------------------------------------
# App constants
# ---------------------------------------------------------------------------

APP_NAME = "ScreenshotTranslator"
APP_VERSION = "4.1"
APP_ICON_FILE = "ScreenshotTranslator_Icon.ico"
STARTUP_REG_NAME = "ScreenshotTranslator"

DEFAULT_LANG = "auto"
DEFAULT_MIN_SCORE = 0.0
DEFAULT_DISABLE_DOC_PREPROCESS = True
DEFAULT_COPY_OCR_TEXT_TO_CLIPBOARD = True
DEFAULT_COPY_TRANSLATION_TO_CLIPBOARD = True
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
DEFAULT_DEEPSEEK_TARGET_LANGUAGE = "English"
DEFAULT_DEEPSEEK_TIMEOUT_SECONDS = 60
DEFAULT_OCR_LOCAL_AUTO_SEED = True
DEFAULT_HOTKEY_SCREENSHOT_ONLY = "ctrl+print screen"
DEFAULT_HOTKEY_OCR_ONLY = "shift+print screen"
DEFAULT_HOTKEY_TRANSLATE = "alt+print screen"

_LAST_OCRLOCAL_SEED_REPORT = "not run"

# OCR_LANG values saved to DATA/ScreenshotTranslator/.env.
#
# V3.9 behavior:
# - All-arounder / Auto uses PaddleOCR's default setup, same as the earlier
#   stable versions.
# - English uses PaddleOCR lang="en".
# - Japanese uses PaddleOCR lang="japan".
# - French and Turkish use the English OCR model as a practical fallback.
#
# Reason:
# On the current PaddleOCR/PaddleX install, lang="latin" raises:
#   ValueError: No models are available for the language 'latin'
# Dedicated fr/tr models may trigger remote downloads. Mapping fr/tr to en
# avoids extra model downloads while still handling most Latin text reasonably.
OCR_LANGUAGE_OPTIONS: dict[str, str] = {
    "auto": "All-arounder / Auto",
    "en": "English",
    "japan": "Japanese",
    "fr": "French / English OCR",
    "tr": "Turkish / English OCR",
}


class CaptureTarget(Enum):
    SCREENSHOT_ONLY = "screenshot_only"
    OCR_ONLY = "ocr_only"
    TRANSLATION_OCR = "translation_ocr"


# ---------------------------------------------------------------------------
# Windows / app path helpers
# ---------------------------------------------------------------------------

def set_windows_dpi_awareness() -> None:
    """
    Make region coordinates line up better with Windows display scaling.
    Must run before QApplication is created.
    """
    if platform.system().lower() != "windows":
        return

    try:
        awareness_context = ctypes.c_void_p(-4)  # PER_MONITOR_AWARE_V2
        ctypes.windll.user32.SetProcessDpiAwarenessContext(awareness_context)
        return
    except Exception:
        pass

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass

    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def is_windows() -> bool:
    return platform.system().lower() == "windows"


def get_app_dir() -> Path:
    """
    Folder containing the running .py file or future .exe.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_resource_path(filename: str) -> Path:
    """
    Find a resource file in normal Python and PyInstaller builds.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bundled = Path(meipass) / filename
        if bundled.exists():
            return bundled
    return get_app_dir() / filename


def data_root_dir() -> Path:
    return get_app_dir() / "DATA"


def app_data_dir() -> Path:
    return data_root_dir() / "ScreenshotTranslator"


def screenshots_base_dir() -> Path:
    return app_data_dir() / "Screenshots"


def translation_screenshots_base_dir() -> Path:
    return app_data_dir() / "Translation Screenshots"


def ocr_screenshots_base_dir() -> Path:
    return app_data_dir() / "OCR Screenshots"


def ocr_text_base_dir() -> Path:
    return app_data_dir() / "OCRText"


def translation_output_base_dir() -> Path:
    return app_data_dir() / "TranslationOutput"


def ocr_debug_dir() -> Path:
    return app_data_dir() / "OCRDebug"


def ocr_temp_dir() -> Path:
    return app_data_dir() / "OCRTemp"


def ocr_local_dir() -> Path:
    return app_data_dir() / "OCRLocal"


def ocr_local_official_models_dir() -> Path:
    return ocr_local_dir() / "official_models"


def user_paddlex_official_models_dir() -> Path:
    return Path.home() / ".paddlex" / "official_models"


def model_cache_copy_log_path() -> Path:
    return ocr_debug_dir() / "model_cache_copy_log.txt"


def logs_dir() -> Path:
    return app_data_dir() / "Logs"


def month_folder_name() -> str:
    return datetime.now().strftime("%Y-%m")


def screenshots_dir() -> Path:
    return screenshots_base_dir() / month_folder_name()


def translation_screenshots_dir() -> Path:
    return translation_screenshots_base_dir() / month_folder_name()


def ocr_screenshots_dir() -> Path:
    return ocr_screenshots_base_dir() / month_folder_name()


def ocr_text_dir() -> Path:
    return ocr_text_base_dir() / month_folder_name()


def translation_output_dir() -> Path:
    return translation_output_base_dir() / month_folder_name()


def ensure_app_folders() -> None:
    for path in (
        data_root_dir(),
        app_data_dir(),
        screenshots_base_dir(),
        translation_screenshots_base_dir(),
        ocr_screenshots_base_dir(),
        ocr_text_base_dir(),
        translation_output_base_dir(),
        ocr_debug_dir(),
        ocr_temp_dir(),
        ocr_local_dir(),
        ocr_local_official_models_dir(),
        logs_dir(),
        screenshots_dir(),
        translation_screenshots_dir(),
        ocr_screenshots_dir(),
        ocr_text_dir(),
        translation_output_dir(),
    ):
        path.mkdir(parents=True, exist_ok=True)


def get_env_path() -> Path:
    return app_data_dir() / ".env"


def default_env_text() -> str:
    return (
        'OCR_LANG="auto"\n'
        'OCR_MIN_SCORE="0.0"\n'
        'OCR_DISABLE_DOC_PREPROCESS="true"\n'
        'OCR_COPY_TEXT_TO_CLIPBOARD="true"\n'
        'DEEPSEEK_API_KEY=""\n'
        'DEEPSEEK_BASE_URL="https://api.deepseek.com"\n'
        'DEEPSEEK_MODEL="deepseek-v4-flash"\n'
        'DEEPSEEK_TARGET_LANGUAGE="English"\n'
        'TRANSLATION_COPY_TO_CLIPBOARD="true"\n'
        'DEEPSEEK_TIMEOUT_SECONDS="60"\n'
        'OCR_LOCAL_AUTO_SEED="true"\n'
        'HOTKEY_SCREENSHOT_ONLY="ctrl+print screen"\n'
        'HOTKEY_OCR_ONLY="shift+print screen"\n'
        'HOTKEY_TRANSLATE="alt+print screen"\n'
    )


def ensure_default_env() -> None:
    env_path = get_env_path()
    if not env_path.exists():
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(default_env_text(), encoding="utf-8")


def load_app_env() -> Path | None:
    """
    Preferred env path:
        DATA/ScreenshotTranslator/.env

    If a legacy .env exists beside the script/EXE or directly in DATA, copy it
    into DATA/ScreenshotTranslator/.env once. Existing app .env is never
    overwritten.
    """
    ensure_app_folders()
    app_env = get_env_path()

    if app_env.exists():
        load_dotenv(app_env, override=True)
        return app_env

    legacy_candidates = [
        data_root_dir() / ".env",
        get_app_dir() / ".env",
    ]

    for legacy in legacy_candidates:
        if not legacy.exists():
            continue
        try:
            app_env.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy, app_env)
            load_dotenv(app_env, override=True)
            return app_env
        except Exception:
            try:
                load_dotenv(legacy, override=True)
                return legacy
            except Exception:
                continue

    ensure_default_env()
    load_dotenv(app_env, override=True)
    return app_env


def set_env_value(name: str, value: str) -> None:
    """
    Update one key in DATA/ScreenshotTranslator/.env while preserving the other keys.
    Also updates os.environ for the current running process.
    """
    env_path = get_env_path()
    env_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    prefix = f"{name}="
    replacement = f'{name}="{value}"'
    replaced = False
    new_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith(prefix):
            new_lines.append(replacement)
            replaced = True
        else:
            new_lines.append(line)

    if not replaced:
        if new_lines and new_lines[-1].strip():
            new_lines.append("")
        new_lines.append(replacement)

    env_path.write_text("\n".join(new_lines).rstrip() + "\n", encoding="utf-8")
    os.environ[name] = value


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return float(value.strip())
    except ValueError:
        return default


def get_lang() -> str:
    return os.getenv("OCR_LANG", DEFAULT_LANG).strip() or DEFAULT_LANG


def resolve_paddleocr_lang(lang_code: str | None = None) -> str:
    """
    Convert the user-facing OCR language into the actual PaddleOCR lang code.

    V3.9:
      auto -> ""      meaning do not pass lang=..., use PaddleOCR default
      fr   -> "en"    practical fallback to avoid fr model download
      tr   -> "en"    practical fallback to avoid tr model download
    """
    code = (lang_code or get_lang()).strip() or DEFAULT_LANG
    if code == "auto":
        return ""
    if code in {"fr", "tr"}:
        return "en"
    return code


def get_lang_label(lang_code: str | None = None) -> str:
    code = (lang_code or get_lang()).strip()
    return OCR_LANGUAGE_OPTIONS.get(code, code)


def get_min_score() -> float:
    return env_float("OCR_MIN_SCORE", DEFAULT_MIN_SCORE)


def get_disable_doc_preprocess() -> bool:
    return env_bool("OCR_DISABLE_DOC_PREPROCESS", DEFAULT_DISABLE_DOC_PREPROCESS)


def get_copy_ocr_text_to_clipboard() -> bool:
    return env_bool("OCR_COPY_TEXT_TO_CLIPBOARD", DEFAULT_COPY_OCR_TEXT_TO_CLIPBOARD)


def get_copy_translation_to_clipboard() -> bool:
    return env_bool("TRANSLATION_COPY_TO_CLIPBOARD", DEFAULT_COPY_TRANSLATION_TO_CLIPBOARD)


def get_deepseek_api_key() -> str:
    return os.getenv("DEEPSEEK_API_KEY", "").strip()


def get_deepseek_base_url() -> str:
    return os.getenv("DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL).strip().rstrip("/") or DEFAULT_DEEPSEEK_BASE_URL


def get_deepseek_model() -> str:
    return os.getenv("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL).strip() or DEFAULT_DEEPSEEK_MODEL


def get_deepseek_target_language() -> str:
    return os.getenv("DEEPSEEK_TARGET_LANGUAGE", DEFAULT_DEEPSEEK_TARGET_LANGUAGE).strip() or DEFAULT_DEEPSEEK_TARGET_LANGUAGE


def get_deepseek_timeout_seconds() -> int:
    raw = os.getenv("DEEPSEEK_TIMEOUT_SECONDS", str(DEFAULT_DEEPSEEK_TIMEOUT_SECONDS)).strip()
    try:
        return max(5, int(float(raw)))
    except Exception:
        return DEFAULT_DEEPSEEK_TIMEOUT_SECONDS


def get_ocr_local_auto_seed() -> bool:
    return env_bool("OCR_LOCAL_AUTO_SEED", DEFAULT_OCR_LOCAL_AUTO_SEED)


def normalize_hotkey_text(value: str) -> str:
    """
    Normalize values for the keyboard package.
    """
    text = (value or "").strip().lower().replace("_", " ")
    aliases = {
        "prtsc": "print screen",
        "prt sc": "print screen",
        "printscreen": "print screen",
        "control": "ctrl",
        "cmd": "windows",
        "meta": "windows",
        "win": "windows",
    }
    text = aliases.get(text, text)
    return " + ".join(part.strip() for part in text.split("+") if part.strip())


def get_hotkey_screenshot_only() -> str:
    return normalize_hotkey_text(
        os.getenv("HOTKEY_SCREENSHOT_ONLY", DEFAULT_HOTKEY_SCREENSHOT_ONLY).strip()
        or DEFAULT_HOTKEY_SCREENSHOT_ONLY
    )


def get_hotkey_ocr_only() -> str:
    return normalize_hotkey_text(
        os.getenv("HOTKEY_OCR_ONLY", DEFAULT_HOTKEY_OCR_ONLY).strip()
        or DEFAULT_HOTKEY_OCR_ONLY
    )


def get_hotkey_translate() -> str:
    return normalize_hotkey_text(
        os.getenv("HOTKEY_TRANSLATE", DEFAULT_HOTKEY_TRANSLATE).strip()
        or DEFAULT_HOTKEY_TRANSLATE
    )


def get_hotkey_map() -> dict[str, str]:
    return {
        "screenshot_only": get_hotkey_screenshot_only(),
        "ocr_only": get_hotkey_ocr_only(),
        "translate": get_hotkey_translate(),
    }


def get_ocr_debug_path() -> Path:
    return ocr_debug_dir() / "startup_debug.txt"


def write_startup_debug(loaded_env: Path | None) -> None:
    """
    Debug file for interpreter/env/PaddleOCR issues. Does not print secrets.
    """
    try:
        out = get_ocr_debug_path()
        out.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"Timestamp: {datetime.now().isoformat(timespec='seconds')}",
            f"App: {APP_NAME} v{APP_VERSION}",
            f"Frozen EXE: {bool(getattr(sys, 'frozen', False))}",
            f"sys.executable: {sys.executable}",
            f"App dir: {get_app_dir()}",
            f"DATA root dir: {data_root_dir()}",
            f"App DATA dir: {app_data_dir()}",
            f"App DATA .env: {get_env_path()} | exists={get_env_path().exists()}",
            f"Loaded env: {loaded_env if loaded_env else 'none'}",
            f"OCR_LOCAL_AUTO_SEED: {get_ocr_local_auto_seed()}",
            f"HOTKEY_SCREENSHOT_ONLY: {get_hotkey_screenshot_only()}",
            f"HOTKEY_OCR_ONLY: {get_hotkey_ocr_only()}",
            f"HOTKEY_TRANSLATE: {get_hotkey_translate()}",
            f"OCRLocal official models source: {ocr_local_official_models_dir()} | exists={ocr_local_official_models_dir().exists()}",
            f"User PaddleX official models target: {user_paddlex_official_models_dir()} | exists={user_paddlex_official_models_dir().exists()}",
            f"OCRLocal seed report: {_LAST_OCRLOCAL_SEED_REPORT.splitlines()[0] if _LAST_OCRLOCAL_SEED_REPORT else 'none'}",
            f"OCR_LANG: {get_lang()}",
            f"Resolved PaddleOCR lang: {resolve_paddleocr_lang()}",
            f"OCR_MIN_SCORE: {get_min_score()}",
            f"OCR_DISABLE_DOC_PREPROCESS: {get_disable_doc_preprocess()}",
            f"OCR_COPY_TEXT_TO_CLIPBOARD: {get_copy_ocr_text_to_clipboard()}",
            f"DEEPSEEK_API_KEY present: {bool(get_deepseek_api_key())}",
            f"DEEPSEEK_BASE_URL: {get_deepseek_base_url()}",
            f"DEEPSEEK_MODEL: {get_deepseek_model()}",
            f"DEEPSEEK_TARGET_LANGUAGE: {get_deepseek_target_language()}",
            f"TRANSLATION_COPY_TO_CLIPBOARD: {get_copy_translation_to_clipboard()}",
            f"Icon file: {APP_ICON_FILE}",
            f"Icon bundled/app path exists: {get_resource_path(APP_ICON_FILE).exists()} | {get_resource_path(APP_ICON_FILE)}",
            f"PaddleOCR import available: {PaddleOCR is not None}",
        ]
        if PADDLEOCR_IMPORT_ERROR is not None:
            lines.append("")
            lines.append("PaddleOCR import error:")
            lines.append(f"{type(PADDLEOCR_IMPORT_ERROR).__name__}: {PADDLEOCR_IMPORT_ERROR}")
            lines.append("")
            lines.append("PaddleOCR import traceback:")
            lines.append(PADDLEOCR_IMPORT_TRACEBACK)
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def app_launch_command() -> str:
    """
    Command saved to HKCU Run key for Windows startup.
    """
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'

    python_exe = Path(sys.executable).resolve()
    pythonw_exe = python_exe.with_name("pythonw.exe")
    runner = pythonw_exe if pythonw_exe.exists() else python_exe
    script = Path(__file__).resolve()
    return f'"{runner}" "{script}"'


def is_startup_enabled() -> bool:
    if not is_windows():
        return False

    try:
        import winreg

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, STARTUP_REG_NAME)
        return bool(value)
    except FileNotFoundError:
        return False
    except Exception:
        return False


def set_startup_enabled(enabled: bool) -> None:
    if not is_windows():
        raise RuntimeError("Windows startup toggle is only supported on Windows.")

    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, STARTUP_REG_NAME, 0, winreg.REG_SZ, app_launch_command())
        else:
            try:
                winreg.DeleteValue(key, STARTUP_REG_NAME)
            except FileNotFoundError:
                pass


def timestamp_name() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]


def ensure_unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    parent = path.parent
    stem = path.stem
    suffix = path.suffix
    counter = 2
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _copy_file_if_needed(src: Path, dst: Path) -> str:
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            try:
                if dst.stat().st_size == src.stat().st_size:
                    return "skipped"
            except Exception:
                pass
            shutil.copy2(src, dst)
            return "repaired"
        shutil.copy2(src, dst)
        return "copied"
    except Exception as exc:
        return f"error:{type(exc).__name__}: {exc}"


def copy_model_cache_tree(source: Path, target: Path, mode_label: str) -> str:
    started = datetime.now().isoformat(timespec="seconds")
    source = Path(source)
    target = Path(target)
    copied = repaired = skipped = created_dirs = 0
    errors: list[str] = []
    if not source.exists() or not source.is_dir():
        report = (
            "===== MODEL CACHE COPY REPORT =====\n"
            f"Timestamp: {started}\n"
            f"App: {APP_NAME} v{APP_VERSION}\n"
            f"Mode: {mode_label}\n"
            f"Source: {source}\n"
            f"Target: {target}\n"
            "Message: Source folder does not exist or is not a directory. Nothing copied.\n"
        )
        write_text(model_cache_copy_log_path(), report)
        return report
    try:
        target.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        report = (
            "===== MODEL CACHE COPY REPORT =====\n"
            f"Timestamp: {started}\n"
            f"App: {APP_NAME} v{APP_VERSION}\n"
            f"Mode: {mode_label}\n"
            f"Source: {source}\n"
            f"Target: {target}\n"
            f"Message: Could not create target folder: {type(exc).__name__}: {exc}\n"
        )
        write_text(model_cache_copy_log_path(), report)
        return report
    for item in source.rglob("*"):
        try:
            rel = item.relative_to(source)
        except Exception:
            continue
        dst = target / rel
        if item.is_dir():
            if not dst.exists():
                try:
                    dst.mkdir(parents=True, exist_ok=True)
                    created_dirs += 1
                except Exception as exc:
                    errors.append(f"DIR {rel}: {type(exc).__name__}: {exc}")
            continue
        if not item.is_file():
            continue
        outcome = _copy_file_if_needed(item, dst)
        if outcome == "copied": copied += 1
        elif outcome == "repaired": repaired += 1
        elif outcome == "skipped": skipped += 1
        elif outcome.startswith("error:"): errors.append(f"FILE {rel}: {outcome[6:]}")
    report_lines = [
        "===== MODEL CACHE COPY REPORT =====",
        f"Timestamp: {started}",
        f"App: {APP_NAME} v{APP_VERSION}",
        f"Mode: {mode_label}",
        f"Source: {source}",
        f"Target: {target}",
        f"Message: {mode_label} complete. Copied={copied}, repaired={repaired}, skipped={skipped}.",
        f"Copied files: {copied}",
        f"Repaired/overwritten files: {repaired}",
        f"Skipped files: {skipped}",
        f"Created dirs: {created_dirs}",
        f"Errors: {len(errors)}",
    ]
    if errors:
        report_lines.append("")
        report_lines.append("Errors:")
        report_lines.extend(errors[:200])
        if len(errors) > 200:
            report_lines.append(f"... {len(errors) - 200} more errors omitted")
    report = "\n".join(report_lines) + "\n"
    write_text(model_cache_copy_log_path(), report)
    return report


def seed_user_paddlex_cache_from_ocrlocal() -> str:
    return copy_model_cache_tree(ocr_local_official_models_dir(), user_paddlex_official_models_dir(), "Import OCRLocal models to user PaddleX cache")


def export_user_paddlex_cache_to_ocrlocal() -> str:
    return copy_model_cache_tree(user_paddlex_official_models_dir(), ocr_local_official_models_dir(), "Export user PaddleX cache to OCRLocal")


def maybe_seed_user_paddlex_cache_from_ocrlocal() -> str:
    global _LAST_OCRLOCAL_SEED_REPORT
    if not get_ocr_local_auto_seed():
        _LAST_OCRLOCAL_SEED_REPORT = "OCRLocal auto-seed disabled by OCR_LOCAL_AUTO_SEED=false."
        return _LAST_OCRLOCAL_SEED_REPORT
    source = ocr_local_official_models_dir()
    try:
        has_any = source.exists() and any(source.iterdir())
    except Exception:
        has_any = False
    if not has_any:
        _LAST_OCRLOCAL_SEED_REPORT = f"OCRLocal source empty or missing: {source}"
        return _LAST_OCRLOCAL_SEED_REPORT
    _LAST_OCRLOCAL_SEED_REPORT = seed_user_paddlex_cache_from_ocrlocal()
    return _LAST_OCRLOCAL_SEED_REPORT


def make_tray_icon() -> QIcon:
    """
    Built-in fallback icon if ScreenshotTranslator_Icon.ico is missing.
    """
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    painter.setBrush(QColor(28, 32, 38))
    painter.setPen(QPen(QColor(230, 230, 230), 3))
    painter.drawRoundedRect(6, 6, 52, 52, 12, 12)

    # Crop frame.
    painter.setPen(QPen(QColor(245, 245, 245), 4))
    painter.drawLine(17, 20, 17, 13)
    painter.drawLine(17, 13, 25, 13)
    painter.drawLine(39, 13, 47, 13)
    painter.drawLine(47, 13, 47, 20)
    painter.drawLine(17, 44, 17, 51)
    painter.drawLine(17, 51, 25, 51)
    painter.drawLine(39, 51, 47, 51)
    painter.drawLine(47, 51, 47, 44)

    # Speech bubble / OCR text hint.
    painter.setBrush(QColor(30, 170, 190))
    painter.setPen(QPen(QColor(210, 255, 255), 2))
    painter.drawRoundedRect(18, 25, 28, 17, 5, 5)
    painter.setPen(QPen(QColor(255, 255, 255), 2))
    painter.drawLine(24, 31, 40, 31)
    painter.drawLine(24, 37, 35, 37)

    painter.end()
    return QIcon(pixmap)


def load_app_icon() -> QIcon:
    icon_path = get_resource_path(APP_ICON_FILE)
    if icon_path.exists():
        icon = QIcon(str(icon_path))
        if not icon.isNull():
            return icon
    return make_tray_icon()


# ---------------------------------------------------------------------------
# Screen capture helpers
# ---------------------------------------------------------------------------

def capture_virtual_screen() -> Tuple[Image.Image, dict]:
    """
    Capture the full virtual desktop using mss.
    """
    with mss.MSS() as sct:
        monitor = dict(sct.monitors[0])
        shot = sct.grab(monitor)

    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    return img, monitor


def pil_image_to_qpixmap(image: Image.Image) -> QPixmap:
    """
    Convert a PIL RGB image to QPixmap for the frozen selection overlay.

    We use .copy() on the QImage so the pixmap owns its data independently.
    """
    if image.mode != "RGB":
        image = image.convert("RGB")
    width, height = image.size
    data = image.tobytes("raw", "RGB")
    qimage = QImage(data, width, height, width * 3, QImage.Format.Format_RGB888).copy()
    return QPixmap.fromImage(qimage)


class CaptureOverlay(QWidget):
    region_selected = pyqtSignal(QRect)
    cancelled = pyqtSignal()

    def __init__(
        self,
        virtual_monitor: dict,
        frozen_image: Optional[Image.Image] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)

        self.virtual_monitor = virtual_monitor
        self.background_pixmap = pil_image_to_qpixmap(frozen_image) if frozen_image is not None else QPixmap()
        if frozen_image is not None:
            self.image_width, self.image_height = frozen_image.size
        else:
            self.image_width = int(virtual_monitor["width"])
            self.image_height = int(virtual_monitor["height"])

        # Store local widget coordinates, not global screen coordinates.
        # This is important for Windows DPI/display-scaling differences.
        self.start_point: Optional[QPoint] = None
        self.current_point: Optional[QPoint] = None
        self.selecting = False

        self.setWindowTitle("Select screenshot region")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.setGeometry(
            int(virtual_monitor["left"]),
            int(virtual_monitor["top"]),
            int(virtual_monitor["width"]),
            int(virtual_monitor["height"]),
        )

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # Do not grab immediately inside showEvent on Windows: the native
        # QWidgetWindow may still be considered invisible for a moment.
        # A 0 ms timer means "next Qt event-loop tick", not a human-visible
        # pre-capture delay.
        QTimer.singleShot(0, self._force_focus_and_grab)

    def _force_focus_and_grab(self) -> None:
        try:
            if not self.isVisible():
                return
            self.raise_()
            self.activateWindow()
            self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
            self.setCursor(Qt.CursorShape.CrossCursor)
            self.grabMouse(Qt.CursorShape.CrossCursor)
            self.grabKeyboard()
        except Exception:
            pass

    def _release_grabs(self) -> None:
        try:
            self.releaseMouse()
        except Exception:
            pass
        try:
            self.releaseKeyboard()
        except Exception:
            pass

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Show the frozen desktop capture underneath the selector. This means
        # screenshots can include transient UI like the system tray menu, even
        # if Windows closes the real menu after the hotkey press.
        if not self.background_pixmap.isNull():
            painter.drawPixmap(self.rect(), self.background_pixmap)
        else:
            painter.fillRect(self.rect(), QColor(0, 0, 0, 1))

        painter.fillRect(self.rect(), QColor(0, 0, 0, 95))

        painter.setPen(QColor(255, 255, 255, 235))
        font = QFont()
        font.setPointSize(14)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(24, 42, "Drag to capture region   |   Esc / Right-click to cancel")

        if self.start_point is not None and self.current_point is not None:
            selected = QRect(self.start_point, self.current_point).normalized()

            painter.fillRect(selected, QColor(255, 255, 255, 35))
            painter.setPen(QPen(QColor(255, 255, 255), 2))
            painter.drawRect(selected)

            painter.setPen(QColor(255, 255, 255, 245))
            font2 = QFont()
            font2.setPointSize(10)
            font2.setBold(True)
            painter.setFont(font2)
            painter.drawText(
                selected.left() + 8,
                max(18, selected.top() - 8),
                f"{selected.width()} × {selected.height()}",
            )

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.RightButton:
            self.cancel()
            return

        if event.button() == Qt.MouseButton.LeftButton:
            self.selecting = True
            self.start_point = event.position().toPoint()
            self.current_point = self.start_point
            self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self.selecting:
            self.current_point = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or not self.selecting:
            return

        self.selecting = False
        self.current_point = event.position().toPoint()

        if self.start_point is None or self.current_point is None:
            self.cancel()
            return

        local_rect = QRect(self.start_point, self.current_point).normalized()
        if local_rect.width() < 8 or local_rect.height() < 8:
            self.cancel()
            return

        image_rect = self.local_rect_to_image_rect(local_rect)
        if image_rect.width() < 1 or image_rect.height() < 1:
            self.cancel()
            return

        self._release_grabs()
        self.region_selected.emit(image_rect)
        self.close()

    def local_rect_to_image_rect(self, rect: QRect) -> QRect:
        """
        Convert the user's selection on the scaled overlay into pixel
        coordinates inside the frozen full-screen screenshot.

        This fixes wrong-crop bugs on laptops with display scaling where Qt's
        widget coordinates and MSS's screenshot pixels are not 1:1.
        """
        widget_w = max(1, self.width())
        widget_h = max(1, self.height())

        sx = self.image_width / widget_w
        sy = self.image_height / widget_h

        left = int(round(rect.left() * sx))
        top = int(round(rect.top() * sy))
        right = int(round((rect.right() + 1) * sx))
        bottom = int(round((rect.bottom() + 1) * sy))

        left = max(0, min(left, self.image_width))
        top = max(0, min(top, self.image_height))
        right = max(0, min(right, self.image_width))
        bottom = max(0, min(bottom, self.image_height))

        return QRect(left, top, max(0, right - left), max(0, bottom - top))

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.cancel()
        else:
            super().keyPressEvent(event)

    def cancel(self) -> None:
        self._release_grabs()
        self.cancelled.emit()
        self.close()


# ---------------------------------------------------------------------------
# OCR result extraction copied/adapted from the working ImageToText app
# ---------------------------------------------------------------------------

def to_plain_data(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, list):
        return [to_plain_data(x) for x in obj]
    if isinstance(obj, tuple):
        return [to_plain_data(x) for x in obj]
    if isinstance(obj, dict):
        return {k: to_plain_data(v) for k, v in obj.items()}

    model_dump = getattr(obj, "model_dump", None)
    if callable(model_dump):
        try:
            return model_dump()
        except Exception:
            pass

    json_method = getattr(obj, "json", None)
    if callable(json_method):
        try:
            dumped = json_method()
            if isinstance(dumped, str):
                return json.loads(dumped)
            return dumped
        except Exception:
            pass

    data = getattr(obj, "__dict__", None)
    if isinstance(data, dict):
        return {k: to_plain_data(v) for k, v in data.items() if not k.startswith("_")}

    try:
        return json.loads(str(obj))
    except Exception:
        return str(obj)


def flatten_scores(raw_scores: Any) -> list[float]:
    if raw_scores is None:
        return []
    if isinstance(raw_scores, (int, float)):
        return [float(raw_scores)]
    if isinstance(raw_scores, list):
        out: list[float] = []
        for item in raw_scores:
            out.extend(flatten_scores(item))
        return out
    return []


def extract_from_nested_structures(node: Any, min_score: float) -> list[str]:
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, list):
            if len(value) >= 2 and isinstance(value[-1], (list, tuple)):
                tail = value[-1]
                if len(tail) >= 1 and isinstance(tail[0], str):
                    text_s = tail[0].strip()
                    score = None
                    if len(tail) >= 2:
                        try:
                            score = float(tail[1])
                        except Exception:
                            score = None
                    if text_s and (score is None or score >= min_score):
                        found.append(text_s)
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            txt = value.get("text")
            score = value.get("score")
            if isinstance(txt, str) and txt.strip():
                try:
                    score_f = float(score) if score is not None else None
                except Exception:
                    score_f = None
                if score_f is None or score_f >= min_score:
                    found.append(txt.strip())
            for item in value.values():
                walk(item)

    walk(node)

    deduped: list[str] = []
    seen: set[str] = set()
    for item in found:
        if item not in seen:
            deduped.append(item)
            seen.add(item)
    return deduped


def extract_text_lines_from_result(raw_result: Any, min_score: float) -> list[str]:
    plain = to_plain_data(raw_result)

    candidate_dicts: list[dict[str, Any]] = []
    if isinstance(plain, dict):
        candidate_dicts.append(plain)
        inner = plain.get("res")
        if isinstance(inner, dict):
            candidate_dicts.append(inner)

    for data in candidate_dicts:
        rec_texts = data.get("rec_texts")
        rec_scores = flatten_scores(data.get("rec_scores"))
        if isinstance(rec_texts, list):
            lines: list[str] = []
            for i, text in enumerate(rec_texts):
                text_s = str(text).strip()
                score = rec_scores[i] if i < len(rec_scores) else None
                if not text_s:
                    continue
                if score is not None and score < min_score:
                    continue
                lines.append(text_s)
            if lines:
                return lines

    lines = extract_from_nested_structures(plain, min_score=min_score)
    if lines:
        return lines

    generic_lines: list[str] = []
    for data in candidate_dicts:
        for key in ("texts", "text", "ocr_text", "content"):
            value = data.get(key)
            if isinstance(value, list):
                generic_lines.extend(str(x).strip() for x in value if str(x).strip())
            elif isinstance(value, str) and value.strip():
                generic_lines.append(value.strip())
    return generic_lines


# ---------------------------------------------------------------------------
# Persistent OCR worker
# ---------------------------------------------------------------------------

class OCRWorker(QObject):
    initializing = pyqtSignal()
    ready = pyqtSignal()
    log = pyqtSignal(str)
    result = pyqtSignal(str, str, str)  # image_path, ocr_text, txt_path
    failed = pyqtSignal(str, str)       # image_path, message

    def __init__(self, lang: str, min_score: float, disable_doc_preprocess: bool) -> None:
        super().__init__()
        self.lang = lang
        self.min_score = min_score
        self.disable_doc_preprocess = disable_doc_preprocess
        self.ocr: Any = None

    def make_ocr_engine(self) -> Any:
        if PaddleOCR is None:
            details = (
                "PaddleOCR could not be imported in this runtime.\n\n"
                "Check:\n"
                f"{get_ocr_debug_path()}\n\n"
            )
            if PADDLEOCR_IMPORT_ERROR is not None:
                details += (
                    "Original import error:\n"
                    f"{type(PADDLEOCR_IMPORT_ERROR).__name__}: {PADDLEOCR_IMPORT_ERROR}\n"
                )
            raise RuntimeError(details)

        kwargs: dict[str, Any] = {}
        resolved_lang = resolve_paddleocr_lang(self.lang)
        if resolved_lang:
            kwargs["lang"] = resolved_lang

        if self.disable_doc_preprocess:
            kwargs.update(
                {
                    "use_doc_orientation_classify": False,
                    "use_doc_unwarping": False,
                    "use_textline_orientation": False,
                }
            )
        return PaddleOCR(**kwargs)

    def ensure_ocr_ready(self) -> None:
        if self.ocr is not None:
            return
        self.initializing.emit()
        self.log.emit(f"Initializing PaddleOCR once. OCR language={get_lang_label(self.lang)} ({self.lang}) -> PaddleOCR lang={resolve_paddleocr_lang(self.lang)}")
        self.ocr = self.make_ocr_engine()
        self.ready.emit()
        self.log.emit("PaddleOCR is ready.")

    @pyqtSlot(str, str)
    def process_image(self, image_path_str: str, label: str) -> None:
        image_path = Path(image_path_str)
        try:
            self.ensure_ocr_ready()

            self.log.emit(f"OCR started: {image_path.name}")
            prediction_iter = self.ocr.predict(str(image_path))
            prediction_list = list(prediction_iter)

            lines: list[str] = []
            for result in prediction_list:
                lines.extend(extract_text_lines_from_result(result, min_score=self.min_score))

            final_text = "\n".join(line for line in lines if line.strip()).strip()
            if not final_text:
                final_text = "[NO TEXT FOUND]"

            txt_path = ensure_unique_path(ocr_text_dir() / f"{image_path.stem}.txt")
            write_text(txt_path, final_text + "\n")

            self.log.emit(f"OCR done: {image_path.name}")
            self.result.emit(str(image_path), final_text, str(txt_path))

        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            self.log.emit(f"OCR failed: {image_path.name} -> {message}")
            self.failed.emit(str(image_path), message)


# ---------------------------------------------------------------------------
# OCR result popup
# ---------------------------------------------------------------------------

class OCRResultPopup(QWidget):
    """
    Minimal always-on-top OCR result window.

    V1.1 intentionally shows only the resulting text. The native title bar
    still gives you the normal top-right X close button.
    """

    def __init__(self, app_icon: QIcon) -> None:
        super().__init__()

        self.setWindowTitle("")
        self.setWindowIcon(app_icon)
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowCloseButtonHint
        )

        self.current_text = ""
        self.current_image_path: Optional[Path] = None
        self.current_txt_path: Optional[Path] = None

        self.resize(620, 360)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(0)

        self.text_box = QPlainTextEdit()
        self.text_box.setReadOnly(True)
        self.text_box.setPlaceholderText("OCR text will appear here.")
        self.text_box.setStyleSheet(
            "QPlainTextEdit { background: #151515; color: #f2f2f2; border: 1px solid #444; "
            "font-size: 16px; padding: 10px; }"
        )
        root.addWidget(self.text_box)

    def show_result(self, image_path: Path, text: str, txt_path: Path) -> None:
        self.current_text = text
        self.current_image_path = image_path
        self.current_txt_path = txt_path
        self.text_box.setPlainText(text)

        self.position_bottom_right()
        self.show()
        self.raise_()
        self.activateWindow()

    def show_error(self, image_path: Path, message: str) -> None:
        self.current_text = message
        self.current_image_path = image_path
        self.current_txt_path = None
        self.text_box.setPlainText(message)

        self.position_bottom_right()
        self.show()
        self.raise_()
        self.activateWindow()

    def position_bottom_right(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geometry = screen.availableGeometry()
        margin = 24
        x = geometry.right() - self.width() - margin
        y = geometry.bottom() - self.height() - margin
        self.move(max(geometry.left(), x), max(geometry.top(), y))



# ---------------------------------------------------------------------------
# DeepSeek translation worker
# ---------------------------------------------------------------------------

class DeepSeekTranslatorWorker(QObject):
    log = pyqtSignal(str)
    result = pyqtSignal(str, str, str, str)  # image_path, ocr_text, translation_text, txt_path
    failed = pyqtSignal(str, str, str)       # image_path, ocr_text, message

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        target_language: str,
        timeout_seconds: int,
    ) -> None:
        super().__init__()
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.target_language = target_language
        self.timeout_seconds = timeout_seconds

    def call_deepseek(self, ocr_text: str) -> str:
        api_key = self.api_key.strip()
        if not api_key or api_key.upper() in {"APIKEY", "YOUR_API_KEY", "DEEPSEEK_API_KEY"}:
            raise RuntimeError(
                "DEEPSEEK_API_KEY is missing or still a placeholder in DATA/ScreenshotTranslator/.env"
            )

        url = f"{self.base_url}/chat/completions"
        system_prompt = (
            "You are a precise translation engine for OCR text extracted from screenshots. "
            "The OCR may contain broken line breaks, minor recognition errors, missing punctuation, "
            "or vertical-text artifacts. Detect the source language automatically. Translate naturally "
            f"into {self.target_language}. Preserve names, tone, dialogue feeling, and meaning. "
            "Do not explain. Output only the translation. If the OCR text already appears to be in the "
            "target language, clean it lightly and output it."
        )
        user_prompt = (
            f"Target language: {self.target_language}\n\n"
            "OCR text from screenshot:\n"
            "```\n"
            f"{ocr_text.strip()}\n"
            "```"
        )

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "stream": False,
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"DeepSeek HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"DeepSeek connection error: {exc}") from exc

        try:
            parsed = json.loads(body)
            return str(parsed["choices"][0]["message"]["content"]).strip()
        except Exception as exc:
            raise RuntimeError(f"Could not parse DeepSeek response: {body[:1000]}") from exc

    @pyqtSlot(str, str, str)
    def process_translation(self, image_path_str: str, ocr_text: str, ocr_txt_path_str: str) -> None:
        image_path = Path(image_path_str)
        try:
            clean_ocr = ocr_text.strip()
            if not clean_ocr or clean_ocr == "[NO TEXT FOUND]":
                translation = "[NO TEXT FOUND]"
            else:
                self.log.emit(f"DeepSeek translation started: {image_path.name}")
                translation = self.call_deepseek(clean_ocr)
                if not translation:
                    translation = "[EMPTY TRANSLATION]"

            out_path = ensure_unique_path(translation_output_dir() / f"{image_path.stem}_translation.txt")
            write_text(out_path, translation + "\n")

            self.log.emit(f"DeepSeek translation done: {image_path.name}")
            self.result.emit(str(image_path), ocr_text, translation, str(out_path))

        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            self.log.emit(f"DeepSeek translation failed: {image_path.name} -> {message}")
            self.failed.emit(str(image_path), ocr_text, message)


# ---------------------------------------------------------------------------
# Small non-intrusive status popup
# ---------------------------------------------------------------------------

class StatusToast(QWidget):
    """Small always-on-top status bubble for OCR/translation progress."""

    def __init__(self, app_icon: QIcon) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon)
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self.label = QLabel("Working...")
        self.label.setStyleSheet(
            "QLabel { background: rgba(20, 20, 20, 230); color: #f2f2f2; "
            "border: 1px solid #555; border-radius: 10px; padding: 12px 16px; "
            "font-size: 13px; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.label)

    def show_status(self, text: str) -> None:
        self.label.setText(text)
        self.adjustSize()
        self.position_bottom_right()
        self.show()
        self.raise_()

    def position_bottom_right(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geometry = screen.availableGeometry()
        margin = 24
        x = geometry.right() - self.width() - margin
        y = geometry.bottom() - self.height() - margin
        self.move(max(geometry.left(), x), max(geometry.top(), y))


# ---------------------------------------------------------------------------
# Hotkey settings dialog
# ---------------------------------------------------------------------------

class HotkeyRecorder(QObject):
    captured = pyqtSignal(str)
    failed = pyqtSignal(str)

    @pyqtSlot()
    def run(self) -> None:
        if keyboard is None:
            self.failed.emit("The 'keyboard' package is not installed.")
            return

        try:
            # This is the method that worked in the earlier version. It can
            # capture Print Screen reliably, unlike Qt key events.
            combo = keyboard.read_hotkey(suppress=True)
            self.captured.emit(normalize_hotkey_text(combo))
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class HotkeySettingsDialog(QDialog):
    """
    Hotkey editor using the keyboard package's read_hotkey() recorder.

    Important:
    The main app unhooks its own global hotkeys before opening this dialog.
    Otherwise recording Ctrl/Shift/Alt + PrintScreen can trigger the screenshot
    action instead of being recorded.
    """

    ACTIONS = [
        ("screenshot_only", "Screenshot only", DEFAULT_HOTKEY_SCREENSHOT_ONLY),
        ("ocr_only", "OCR only", DEFAULT_HOTKEY_OCR_ONLY),
        ("translate", "OCR + translate", DEFAULT_HOTKEY_TRANSLATE),
    ]

    def __init__(self, current_hotkeys: dict[str, str], app_icon: QIcon, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("ScreenshotTranslator Hotkeys")
        self.setWindowIcon(app_icon)
        self.resize(700, 285)

        self.recording_thread: QThread | None = None
        self.recording_worker: HotkeyRecorder | None = None
        self.recording_key: str | None = None
        self.recording_previous_text: str = ""
        self.edits: dict[str, QLineEdit] = {}
        self.record_buttons: dict[str, QPushButton] = {}

        root = QVBoxLayout(self)
        instructions = QLabel(
            "Set the shortcuts used by the tray app. Click Record, press the shortcut, then Save. "
            "Examples: print screen, ctrl+print screen, shift+print screen, alt+print screen."
        )
        instructions.setWordWrap(True)
        root.addWidget(instructions)

        grid = QGridLayout()
        grid.addWidget(QLabel("Action"), 0, 0)
        grid.addWidget(QLabel("Hotkey"), 0, 1)
        grid.addWidget(QLabel("Record"), 0, 2)
        grid.addWidget(QLabel("Default"), 0, 3)

        for row, (key, label, default) in enumerate(self.ACTIONS, start=1):
            grid.addWidget(QLabel(label), row, 0)
            edit = QLineEdit(current_hotkeys.get(key, default))
            edit.setPlaceholderText(default)
            self.edits[key] = edit
            grid.addWidget(edit, row, 1)

            record_btn = QPushButton("Record")
            record_btn.clicked.connect(lambda checked=False, k=key: self.start_recording(k))
            self.record_buttons[key] = record_btn
            grid.addWidget(record_btn, row, 2)

            reset_btn = QPushButton("Reset")
            reset_btn.clicked.connect(lambda checked=False, k=key, d=default: self.edits[k].setText(d))
            grid.addWidget(reset_btn, row, 3)

        root.addLayout(grid)

        self.status_label = QLabel("Record mode: inactive.")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: #666;")
        root.addWidget(self.status_label)

        note = QLabel(
            "Tip: single Print Screen works, but it replaces Windows' normal Print Screen behavior while this app is running. "
            "If recording gets stuck, close this window and reopen Hotkeys."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #666;")
        root.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.validate_and_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def start_recording(self, key: str) -> None:
        if keyboard is None:
            QMessageBox.warning(self, "Cannot record", "The 'keyboard' package is not installed.")
            return

        if self.recording_thread is not None:
            QMessageBox.information(self, "Already recording", "Press the desired shortcut first, then try again.")
            return

        self.recording_key = key
        self.recording_previous_text = self.edits[key].text()
        self.edits[key].setText("Press shortcut now...")
        self.status_label.setText("Recording... press the shortcut now.")
        self.status_label.setStyleSheet("color: #0a6; font-weight: bold;")

        for btn in self.record_buttons.values():
            btn.setEnabled(False)

        self.recording_thread = QThread(self)
        self.recording_worker = HotkeyRecorder()
        self.recording_worker.moveToThread(self.recording_thread)

        self.recording_thread.started.connect(self.recording_worker.run)
        self.recording_worker.captured.connect(self.on_hotkey_captured)
        self.recording_worker.failed.connect(self.on_hotkey_record_failed)
        self.recording_worker.captured.connect(self.recording_thread.quit)
        self.recording_worker.failed.connect(self.recording_thread.quit)
        self.recording_thread.finished.connect(self.cleanup_recording_worker)
        self.recording_thread.start()

    def on_hotkey_captured(self, combo: str) -> None:
        if self.recording_key:
            if combo:
                self.edits[self.recording_key].setText(combo)
                self.status_label.setText(f"Recorded: {combo}")
                self.status_label.setStyleSheet("color: #0a6;")
            else:
                self.edits[self.recording_key].setText(self.recording_previous_text)

    def on_hotkey_record_failed(self, message: str) -> None:
        QMessageBox.warning(self, "Hotkey recording failed", message)
        if self.recording_key:
            self.edits[self.recording_key].setText(self.recording_previous_text)

    def cleanup_recording_worker(self) -> None:
        if self.recording_worker is not None:
            self.recording_worker.deleteLater()
        if self.recording_thread is not None:
            self.recording_thread.deleteLater()

        self.recording_worker = None
        self.recording_thread = None
        self.recording_key = None
        self.recording_previous_text = ""

        for btn in self.record_buttons.values():
            btn.setEnabled(True)

        if "Recorded:" not in self.status_label.text():
            self.status_label.setText("Record mode: inactive.")
            self.status_label.setStyleSheet("color: #666;")

    def closeEvent(self, event) -> None:  # noqa: N802
        # If read_hotkey is actively waiting, closing the dialog cannot always
        # cancel the low-level keyboard hook immediately. The main app re-hooks
        # its shortcuts after the dialog closes.
        super().closeEvent(event)

    def get_hotkeys(self) -> dict[str, str]:
        return {key: normalize_hotkey_text(edit.text()) for key, edit in self.edits.items()}

    def validate_and_accept(self) -> None:
        if self.recording_thread is not None:
            QMessageBox.information(self, "Still recording", "Press the shortcut first, then save.")
            return

        values = self.get_hotkeys()
        for action, combo in values.items():
            if not combo or combo == "press shortcut now...":
                QMessageBox.warning(self, "Missing hotkey", "Every action needs a hotkey.")
                return

        normalized = list(values.values())
        if len(set(normalized)) != len(normalized):
            QMessageBox.warning(self, "Duplicate hotkeys", "Each action needs a different hotkey.")
            return

        self.accept()



# ---------------------------------------------------------------------------
# Main tray app
# ---------------------------------------------------------------------------

class TrayScreenshotTranslator(QObject):
    capture_requested = pyqtSignal(str)
    ocr_requested = pyqtSignal(str, str)
    translation_requested = pyqtSignal(str, str, str)

    def __init__(self) -> None:
        super().__init__()

        ensure_app_folders()
        self.loaded_env_path = load_app_env()
        maybe_seed_user_paddlex_cache_from_ocrlocal()
        write_startup_debug(self.loaded_env_path)

        self.pending_full_image: Optional[Image.Image] = None
        self.pending_monitor: Optional[dict] = None
        self.overlay: Optional[CaptureOverlay] = None
        self.target: Optional[CaptureTarget] = None
        self.capture_in_progress = False
        self.last_ocr_text = ""
        self.last_ocr_txt_path: Optional[Path] = None
        self.last_translation_text = ""
        self.last_translation_txt_path: Optional[Path] = None
        self.pending_translation_images: set[str] = set()
        self.hotkey_registered_descriptions: list[str] = []
        self.native_hotkey_filter = NativeHotkeyFilter(self)
        self.native_hotkey_ids: dict[int, CaptureTarget] = {}
        self._native_hotkey_filter_installed = False

        self.app_icon = load_app_icon()
        self.popup = OCRResultPopup(self.app_icon)
        self.status_toast = StatusToast(self.app_icon)

        self.tray = QSystemTrayIcon()
        self.tray.setIcon(self.app_icon)
        self.tray.setToolTip(
            f"{APP_NAME} v{APP_VERSION}\n"
            f"{get_hotkey_screenshot_only()} -> screenshot only\n"
            f"{get_hotkey_ocr_only()} -> OCR only\n"
            f"{get_hotkey_translate()} -> OCR + DeepSeek translation\n"
            f"OCR language: {get_lang_label()}"
        )

        self.menu = QMenu()

        self.capture_screenshot_action = QAction("Capture Screenshot Only  (Ctrl + PrintScreen)")
        self.capture_screenshot_action.triggered.connect(
            lambda: self.start_capture(CaptureTarget.SCREENSHOT_ONLY)
        )

        self.capture_ocr_only_action = QAction("Capture + OCR Only  (Shift + PrintScreen)")
        self.capture_ocr_only_action.triggered.connect(
            lambda: self.start_capture(CaptureTarget.OCR_ONLY)
        )

        self.capture_ocr_action = QAction("Capture + Translate  (Alt + PrintScreen)")
        self.capture_ocr_action.triggered.connect(
            lambda: self.start_capture(CaptureTarget.TRANSLATION_OCR)
        )

        self.ocr_language_menu = QMenu("OCR Language")
        self.ocr_language_group = QActionGroup(self)
        self.ocr_language_group.setExclusive(True)
        self.ocr_language_actions: dict[str, QAction] = {}

        current_lang = get_lang()
        for lang_code, label in OCR_LANGUAGE_OPTIONS.items():
            action = QAction(label)
            action.setCheckable(True)
            action.setChecked(lang_code == current_lang)
            action.triggered.connect(lambda checked=False, code=lang_code: self.set_ocr_language(code))
            self.ocr_language_group.addAction(action)
            self.ocr_language_menu.addAction(action)
            self.ocr_language_actions[lang_code] = action

        if current_lang not in self.ocr_language_actions:
            custom_action = QAction(f"Custom from .env: {current_lang}")
            custom_action.setCheckable(True)
            custom_action.setChecked(True)
            custom_action.setEnabled(False)
            self.ocr_language_group.addAction(custom_action)
            self.ocr_language_menu.addAction(custom_action)

        self.show_last_ocr_action = QAction("Show Last OCR Result")
        self.show_last_ocr_action.setEnabled(False)
        self.show_last_ocr_action.triggered.connect(self.show_last_ocr)

        self.copy_last_ocr_action = QAction("Copy Last OCR Text")
        self.copy_last_ocr_action.setEnabled(False)
        self.copy_last_ocr_action.triggered.connect(self.copy_last_ocr)

        self.show_last_translation_action = QAction("Show Last Translation Result")
        self.show_last_translation_action.setEnabled(False)
        self.show_last_translation_action.triggered.connect(self.show_last_translation)

        self.copy_last_translation_action = QAction("Copy Last Translation")
        self.copy_last_translation_action.setEnabled(False)
        self.copy_last_translation_action.triggered.connect(self.copy_last_translation)

        self.hotkey_settings_action = QAction("Hotkeys...")
        self.hotkey_settings_action.triggered.connect(self.open_hotkey_settings)

        self.startup_action = QAction("Run at Windows Startup")
        self.startup_action.setCheckable(True)
        self.startup_action.setChecked(is_startup_enabled())
        self.startup_action.toggled.connect(self.toggle_startup)

        if not is_windows():
            self.startup_action.setEnabled(False)
            self.startup_action.setText("Run at Windows Startup  (Windows only)")

        self.open_app_data_action = QAction("Open ScreenshotTranslator DATA Folder")
        self.open_app_data_action.triggered.connect(lambda: open_folder(app_data_dir()))

        self.open_screenshots_action = QAction("Open Screenshots Folder")
        self.open_screenshots_action.triggered.connect(lambda: open_folder(screenshots_base_dir()))

        self.open_translation_screenshots_action = QAction("Open Translation Screenshots Folder")
        self.open_translation_screenshots_action.triggered.connect(
            lambda: open_folder(translation_screenshots_base_dir())
        )

        self.open_ocr_screenshots_action = QAction("Open OCR Screenshots Folder")
        self.open_ocr_screenshots_action.triggered.connect(
            lambda: open_folder(ocr_screenshots_base_dir())
        )

        self.open_ocr_text_action = QAction("Open OCRText Folder")
        self.open_ocr_text_action.triggered.connect(lambda: open_folder(ocr_text_base_dir()))

        self.open_translation_output_action = QAction("Open TranslationOutput Folder")
        self.open_translation_output_action.triggered.connect(lambda: open_folder(translation_output_base_dir()))

        self.open_debug_action = QAction("Open OCRDebug Folder")
        self.open_debug_action.triggered.connect(lambda: open_folder(ocr_debug_dir()))

        self.debug_menu = QMenu("Debug")

        self.export_models_action = QAction("Export local OCR models to OCRLocal")
        self.export_models_action.triggered.connect(self.export_models_to_ocrlocal)

        self.import_models_action = QAction("Import OCRLocal models to PaddleX cache")
        self.import_models_action.triggered.connect(self.import_models_from_ocrlocal)

        self.open_ocrlocal_action = QAction("Open OCRLocal Folder")
        self.open_ocrlocal_action.triggered.connect(lambda: open_folder(ocr_local_official_models_dir()))

        self.open_user_paddlex_action = QAction("Open User PaddleX Cache")
        self.open_user_paddlex_action.triggered.connect(lambda: open_folder(user_paddlex_official_models_dir()))

        self.open_model_cache_log_action = QAction("Open Model Cache Copy Log Folder")
        self.open_model_cache_log_action.triggered.connect(lambda: open_folder(model_cache_copy_log_path().parent))

        self.debug_menu.addAction(self.export_models_action)
        self.debug_menu.addAction(self.import_models_action)
        self.debug_menu.addSeparator()
        self.debug_menu.addAction(self.open_ocrlocal_action)
        self.debug_menu.addAction(self.open_user_paddlex_action)
        self.debug_menu.addAction(self.open_debug_action)
        self.debug_menu.addAction(self.open_model_cache_log_action)

        self.exit_action = QAction("Exit")
        self.exit_action.triggered.connect(self.exit_app)

        # Keep the tray menu intentionally short. Captures are mainly hotkey-driven:
        # Ctrl+PrintScreen, Shift+PrintScreen, Alt+PrintScreen.
        self.menu.addMenu(self.ocr_language_menu)
        self.menu.addAction(self.hotkey_settings_action)
        self.menu.addSeparator()
        self.menu.addAction(self.show_last_ocr_action)
        self.menu.addAction(self.show_last_translation_action)
        self.menu.addSeparator()
        self.menu.addAction(self.startup_action)
        self.menu.addSeparator()
        self.menu.addAction(self.open_screenshots_action)
        self.menu.addMenu(self.debug_menu)
        self.menu.addSeparator()
        self.menu.addAction(self.exit_action)

        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self.on_tray_activated)
        self.tray.show()

        self.setup_ocr_thread()
        self.setup_translation_thread()

        self.capture_requested.connect(self._capture_requested_from_hotkey)
        self.register_hotkeys()

        msg = f"{APP_NAME} v{APP_VERSION} is running."
        if not self.hotkey_registered_descriptions:
            msg += "\nGlobal hotkeys were not registered. Use the tray menu."
        self.show_message("Ready", msg, QSystemTrayIcon.MessageIcon.Information, 2500)

    def export_models_to_ocrlocal(self) -> None:
        report = export_user_paddlex_cache_to_ocrlocal()
        write_startup_debug(self.loaded_env_path)
        self.show_message("OCR models exported", "Copied local PaddleX model cache into OCRLocal.", QSystemTrayIcon.MessageIcon.Information, 5000)
        QMessageBox.information(None, "Export OCR models", report)

    def import_models_from_ocrlocal(self) -> None:
        report = seed_user_paddlex_cache_from_ocrlocal()
        write_startup_debug(self.loaded_env_path)
        self.show_message("OCR models imported", "Copied OCRLocal model cache into user PaddleX cache.", QSystemTrayIcon.MessageIcon.Information, 5000)
        QMessageBox.information(None, "Import OCR models", report)

    def open_hotkey_settings(self) -> None:
        # Temporarily remove this app's native global shortcuts while recording.
        # Otherwise Ctrl/Shift/Alt + PrintScreen can trigger capture instead of
        # being recorded.
        self.unregister_hotkeys()

        dialog = HotkeySettingsDialog(get_hotkey_map(), self.app_icon)
        result = dialog.exec()

        if result == QDialog.DialogCode.Accepted:
            values = dialog.get_hotkeys()
            set_env_value("HOTKEY_SCREENSHOT_ONLY", values["screenshot_only"])
            set_env_value("HOTKEY_OCR_ONLY", values["ocr_only"])
            set_env_value("HOTKEY_TRANSLATE", values["translate"])
            write_startup_debug(self.loaded_env_path)
            self.show_message(
                "Hotkeys saved",
                "Updated screenshot hotkeys are active now.",
                QSystemTrayIcon.MessageIcon.Information,
                2500,
            )

        self.register_hotkeys()
        self.update_tray_tooltip()

    def set_ocr_language(self, lang_code: str) -> None:
        if lang_code == get_lang():
            return

        if lang_code not in OCR_LANGUAGE_OPTIONS:
            self.show_message(
                "OCR language not supported yet",
                f"{lang_code} is not enabled in this build.",
                QSystemTrayIcon.MessageIcon.Warning,
                3000,
            )
            return

        set_env_value("OCR_LANG", lang_code)
        write_startup_debug(self.loaded_env_path)

        for code, action in getattr(self, "ocr_language_actions", {}).items():
            action.blockSignals(True)
            action.setChecked(code == lang_code)
            action.blockSignals(False)

        self.show_message(
            "OCR language changed",
            f"OCR language: {get_lang_label(lang_code)}\nReloading PaddleOCR model...",
            QSystemTrayIcon.MessageIcon.Information,
            3500,
        )
        self.append_log(f"OCR language changed to {lang_code}. Recreating OCR worker.")
        self.restart_ocr_worker()
        self.update_tray_tooltip()

    def update_tray_tooltip(self) -> None:
        self.tray.setToolTip(
            f"{APP_NAME} v{APP_VERSION}\n"
            f"{get_hotkey_screenshot_only()} -> screenshot only\n"
            f"{get_hotkey_ocr_only()} -> OCR only\n"
            f"{get_hotkey_translate()} -> OCR + DeepSeek translation\n"
            f"OCR language: {get_lang_label()}"
        )

    def restart_ocr_worker(self) -> None:
        try:
            self.ocr_requested.disconnect(self.ocr_worker.process_image)
        except Exception:
            pass

        try:
            self.ocr_thread.quit()
            self.ocr_thread.wait(5000)
        except Exception:
            pass

        self.setup_ocr_thread()

    def setup_ocr_thread(self) -> None:
        self.ocr_thread = QThread(self)
        self.ocr_worker = OCRWorker(
            lang=get_lang(),
            min_score=get_min_score(),
            disable_doc_preprocess=get_disable_doc_preprocess(),
        )
        self.ocr_worker.moveToThread(self.ocr_thread)

        self.ocr_requested.connect(self.ocr_worker.process_image)
        self.ocr_worker.initializing.connect(self.on_ocr_initializing)
        self.ocr_worker.ready.connect(self.on_ocr_ready)
        self.ocr_worker.log.connect(self.append_log)
        self.ocr_worker.result.connect(self.on_ocr_result)
        self.ocr_worker.failed.connect(self.on_ocr_failed)

        self.ocr_thread.start()

    def setup_translation_thread(self) -> None:
        self.translation_thread = QThread(self)
        self.translation_worker = DeepSeekTranslatorWorker(
            api_key=get_deepseek_api_key(),
            base_url=get_deepseek_base_url(),
            model=get_deepseek_model(),
            target_language=get_deepseek_target_language(),
            timeout_seconds=get_deepseek_timeout_seconds(),
        )
        self.translation_worker.moveToThread(self.translation_thread)

        self.translation_requested.connect(self.translation_worker.process_translation)
        self.translation_worker.log.connect(self.append_log)
        self.translation_worker.result.connect(self.on_translation_result)
        self.translation_worker.failed.connect(self.on_translation_failed)

        self.translation_thread.start()

    def append_log(self, text: str) -> None:
        try:
            log_path = logs_dir() / f"{datetime.now().strftime('%Y-%m')}_ocr.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("", encoding="utf-8") if not log_path.exists() else None
            with log_path.open("a", encoding="utf-8") as f:
                f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {text}\n")
        except Exception:
            pass

    def show_message(
        self,
        title: str,
        message: str,
        icon: QSystemTrayIcon.MessageIcon = QSystemTrayIcon.MessageIcon.Information,
        timeout_ms: int = 2500,
    ) -> None:
        if self.tray.supportsMessages():
            self.tray.showMessage(title, message, icon, timeout_ms)

    def on_native_hotkey(self, hotkey_id: int) -> None:
        target = self.native_hotkey_ids.get(hotkey_id)
        if target is not None:
            self.capture_requested.emit(target.value)

    def unregister_hotkeys(self) -> None:
        if not sys.platform.startswith("win"):
            return

        try:
            user32 = ctypes.windll.user32
            for hotkey_id in list(self.native_hotkey_ids):
                try:
                    user32.UnregisterHotKey(None, int(hotkey_id))
                except Exception:
                    pass
            self.native_hotkey_ids.clear()
        except Exception:
            pass

    def register_hotkeys(self) -> None:
        self.hotkey_registered_descriptions = []
        self.unregister_hotkeys()

        if not sys.platform.startswith("win"):
            self.show_message(
                "Hotkeys unavailable",
                "Native hotkeys are currently implemented for Windows only.",
                QSystemTrayIcon.MessageIcon.Warning,
                5000,
            )
            return

        app = QApplication.instance()
        if app is not None and not self._native_hotkey_filter_installed:
            app.installNativeEventFilter(self.native_hotkey_filter)
            self._native_hotkey_filter_installed = True

        user32 = ctypes.windll.user32
        user32.RegisterHotKey.argtypes = [
            ctypes.wintypes.HWND,
            ctypes.wintypes.INT,
            ctypes.wintypes.UINT,
            ctypes.wintypes.UINT,
        ]
        user32.RegisterHotKey.restype = ctypes.wintypes.BOOL

        bindings = [
            (1001, CaptureTarget.SCREENSHOT_ONLY, get_hotkey_screenshot_only(), "screenshot only"),
            (1002, CaptureTarget.OCR_ONLY, get_hotkey_ocr_only(), "OCR only"),
            (1003, CaptureTarget.TRANSLATION_OCR, get_hotkey_translate(), "OCR + DeepSeek translation"),
        ]

        for hotkey_id, target, combo, description in bindings:
            combo = normalize_hotkey_text(combo)
            if not combo:
                continue

            try:
                modifiers, vk = parse_windows_hotkey(combo)
            except Exception as exc:
                self.show_message(
                    "Hotkey parse failed",
                    f"{combo}: {type(exc).__name__}: {exc}",
                    QSystemTrayIcon.MessageIcon.Warning,
                    5000,
                )
                continue

            ok = bool(user32.RegisterHotKey(None, int(hotkey_id), int(modifiers), int(vk)))
            if ok:
                self.native_hotkey_ids[int(hotkey_id)] = target
                self.hotkey_registered_descriptions.append(f"{combo} -> {description} [native]")
            else:
                err = ctypes.get_last_error()
                self.show_message(
                    "Hotkey failed",
                    f"Could not register {combo}. It may already be used by another app. Windows error: {err}",
                    QSystemTrayIcon.MessageIcon.Warning,
                    6000,
                )


    def on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # Left-click: OCR-only capture.
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.start_capture(CaptureTarget.OCR_ONLY)

    def _capture_requested_from_hotkey(self, target_value: str) -> None:
        try:
            target = CaptureTarget(target_value)
        except Exception:
            target = CaptureTarget.TRANSLATION_OCR
        self.start_capture(target)

    def close_tray_menus_after_frozen_capture(self) -> None:
        """
        V2.5 behavior:
        - Do NOT close the tray menu before capture.
        - Capture the frozen desktop first, so the tray menu can be included.
        - Then close the real tray menu immediately so it cannot steal mouse
          input from the selection overlay.

        This keeps tray-menu screenshots possible without the annoying
        pre-capture delay from V2.3.
        """
        try:
            self.ocr_language_menu.close()
        except Exception:
            pass
        try:
            self.menu.close()
        except Exception:
            pass

        try:
            while QApplication.activePopupWidget() is not None:
                popup = QApplication.activePopupWidget()
                if popup is None:
                    break
                popup.close()
        except Exception:
            pass

        QApplication.processEvents()

    def start_capture(self, target: CaptureTarget) -> None:
        if self.capture_in_progress:
            return

        # No pre-capture delay. The screenshot is taken immediately.
        self.capture_in_progress = True
        self.target = target
        self._prepare_overlay()

    def _prepare_overlay(self) -> None:
        try:
            # Capture first, while transient UI such as the tray menu is still visible.
            self.pending_full_image, self.pending_monitor = capture_virtual_screen()

            # Then close the real menu so it does not steal the first click.
            # The frozen overlay still shows the captured menu image.
            self.close_tray_menus_after_frozen_capture()

            self.overlay = CaptureOverlay(self.pending_monitor, self.pending_full_image)
            self.overlay.region_selected.connect(self._save_selected_region)
            self.overlay.cancelled.connect(self._capture_cancelled)
            self.overlay.showFullScreen()
            self.overlay.raise_()
            self.overlay.activateWindow()
            QTimer.singleShot(0, self.overlay._force_focus_and_grab)

        except Exception as exc:
            self._finish_capture()
            self.show_error("Capture failed", exc)

    def _target_dir(self) -> Path:
        if self.target == CaptureTarget.SCREENSHOT_ONLY:
            return screenshots_dir()
        if self.target == CaptureTarget.OCR_ONLY:
            return ocr_screenshots_dir()
        return translation_screenshots_dir()

    def _target_suffix(self) -> str:
        if self.target == CaptureTarget.SCREENSHOT_ONLY:
            return "screenshot"
        if self.target == CaptureTarget.OCR_ONLY:
            return "ocr_screenshot"
        return "translation_screenshot"

    def _save_selected_region(self, rect: QRect) -> None:
        try:
            if self.pending_full_image is None or self.pending_monitor is None:
                raise RuntimeError("Internal error: missing full-screen capture.")

            # V3.8: CaptureOverlay emits image-pixel coordinates relative to
            # the frozen full-screen screenshot. Do not subtract monitor offsets.
            left = rect.left()
            top = rect.top()
            right = rect.left() + rect.width()
            bottom = rect.top() + rect.height()

            img_w, img_h = self.pending_full_image.size
            left = max(0, min(left, img_w))
            top = max(0, min(top, img_h))
            right = max(0, min(right, img_w))
            bottom = max(0, min(bottom, img_h))

            if right <= left or bottom <= top:
                raise RuntimeError("Selected region is outside the captured screen area.")

            cropped = self.pending_full_image.crop((left, top, right, bottom))

            out_dir = self._target_dir()
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = ensure_unique_path(out_dir / f"{timestamp_name()}_{self._target_suffix()}.png")

            cropped.save(out_path)

            # Copy selected image immediately.
            pixmap = QPixmap(str(out_path))
            if not pixmap.isNull():
                QApplication.clipboard().setPixmap(pixmap)

            if self.target in (CaptureTarget.OCR_ONLY, CaptureTarget.TRANSLATION_OCR):
                if self.target == CaptureTarget.TRANSLATION_OCR:
                    self.pending_translation_images.add(str(out_path))
                    message_title = "OCR + translation started"
                else:
                    message_title = "OCR started"
                self.status_toast.show_status("Reading text in progress...")
                self.show_message(
                    message_title,
                    f"{out_path.name}\nReading text in progress...",
                    QSystemTrayIcon.MessageIcon.Information,
                    2500,
                )
                self.ocr_requested.emit(str(out_path), out_path.name)
            else:
                self.show_message(
                    "Screenshot saved",
                    str(out_path),
                    QSystemTrayIcon.MessageIcon.Information,
                    2500,
                )

        except Exception as exc:
            self.show_error("Save failed", exc)
        finally:
            self._finish_capture()

    def _capture_cancelled(self) -> None:
        self._finish_capture()

    def _finish_capture(self) -> None:
        self.pending_full_image = None
        self.pending_monitor = None
        self.overlay = None
        self.target = None
        self.capture_in_progress = False

    def on_ocr_initializing(self) -> None:
        self.show_message(
            "PaddleOCR initializing",
            "First OCR may take a bit. Later captures should be faster.",
            QSystemTrayIcon.MessageIcon.Information,
            3500,
        )

    def on_ocr_ready(self) -> None:
        self.show_message(
            "PaddleOCR ready",
            "OCR engine is now loaded.",
            QSystemTrayIcon.MessageIcon.Information,
            2000,
        )

    def on_ocr_result(self, image_path_str: str, text: str, txt_path_str: str) -> None:
        image_path = Path(image_path_str)
        txt_path = Path(txt_path_str)

        self.last_ocr_text = text
        self.last_ocr_txt_path = txt_path
        self.show_last_ocr_action.setEnabled(True)
        self.copy_last_ocr_action.setEnabled(True)

        if image_path_str in self.pending_translation_images:
            self.pending_translation_images.discard(image_path_str)
            self.status_toast.show_status("Translation in progress...")
            self.show_message(
                "Translation in progress",
                f"OCR saved: {txt_path.name}",
                QSystemTrayIcon.MessageIcon.Information,
                2500,
            )
            self.translation_requested.emit(image_path_str, text, txt_path_str)
            return

        self.status_toast.hide()
        if get_copy_ocr_text_to_clipboard():
            QApplication.clipboard().setText(text)

        self.popup.show_result(image_path, text, txt_path)
        self.show_message(
            "OCR complete",
            f"Text saved:\n{txt_path.name}",
            QSystemTrayIcon.MessageIcon.Information,
            2500,
        )

    def on_translation_result(self, image_path_str: str, ocr_text: str, translation: str, txt_path_str: str) -> None:
        image_path = Path(image_path_str)
        txt_path = Path(txt_path_str)

        self.last_translation_text = translation
        self.last_translation_txt_path = txt_path
        self.show_last_translation_action.setEnabled(True)
        self.copy_last_translation_action.setEnabled(True)

        self.status_toast.hide()
        if get_copy_translation_to_clipboard():
            QApplication.clipboard().setText(translation)

        self.popup.show_result(image_path, translation, txt_path)
        self.show_message(
            "Translation complete",
            f"Translation saved:\n{txt_path.name}",
            QSystemTrayIcon.MessageIcon.Information,
            2500,
        )

    def on_translation_failed(self, image_path_str: str, ocr_text: str, message: str) -> None:
        image_path = Path(image_path_str)
        self.status_toast.hide()
        error_text = f"[TRANSLATION ERROR]\n{message}\n\n[OCR TEXT]\n{ocr_text}"
        self.popup.show_error(image_path, error_text)
        # Keep OCR text useful even if API translation fails.
        if get_copy_ocr_text_to_clipboard():
            QApplication.clipboard().setText(ocr_text)
        self.show_message(
            "Translation failed",
            message,
            QSystemTrayIcon.MessageIcon.Critical,
            7000,
        )

    def on_ocr_failed(self, image_path_str: str, message: str) -> None:
        image_path = Path(image_path_str)
        self.status_toast.hide()
        self.popup.show_error(image_path, message)
        self.show_message(
            "OCR failed",
            message,
            QSystemTrayIcon.MessageIcon.Critical,
            6000,
        )

    def show_last_ocr(self) -> None:
        if self.last_ocr_txt_path and self.last_ocr_txt_path.exists():
            text = self.last_ocr_txt_path.read_text(encoding="utf-8")
            self.popup.show_result(
                Path("last OCR image"),
                text,
                self.last_ocr_txt_path,
            )
        elif self.last_ocr_text:
            self.popup.show_error(Path("last OCR image"), self.last_ocr_text)

    def copy_last_ocr(self) -> None:
        if self.last_ocr_text:
            QApplication.clipboard().setText(self.last_ocr_text)
            self.show_message("Copied", "Last OCR text copied to clipboard.")

    def show_last_translation(self) -> None:
        if self.last_translation_txt_path and self.last_translation_txt_path.exists():
            text = self.last_translation_txt_path.read_text(encoding="utf-8")
            self.popup.show_result(
                Path("last translation image"),
                text,
                self.last_translation_txt_path,
            )
        elif self.last_translation_text:
            self.popup.show_error(Path("last translation image"), self.last_translation_text)

    def copy_last_translation(self) -> None:
        if self.last_translation_text:
            QApplication.clipboard().setText(self.last_translation_text)
            self.show_message("Copied", "Last translation copied to clipboard.")

    def toggle_startup(self, enabled: bool) -> None:
        try:
            set_startup_enabled(enabled)
            actual = is_startup_enabled()
            if actual != enabled:
                self.startup_action.blockSignals(True)
                self.startup_action.setChecked(actual)
                self.startup_action.blockSignals(False)
                raise RuntimeError("Startup setting did not stick.")

            self.show_message(
                "Startup enabled" if enabled else "Startup disabled",
                "This app will run when Windows starts." if enabled else "This app will not run when Windows starts.",
                QSystemTrayIcon.MessageIcon.Information,
                2500,
            )

        except Exception as exc:
            self.startup_action.blockSignals(True)
            self.startup_action.setChecked(is_startup_enabled())
            self.startup_action.blockSignals(False)
            self.show_error("Startup toggle failed", exc)

    def show_error(self, title: str, exc: Exception) -> None:
        details = traceback.format_exc()
        self.show_message(title, str(exc), QSystemTrayIcon.MessageIcon.Critical, 6000)
        QMessageBox.critical(None, title, f"{exc}\n\nDetails:\n{details}")

    def _stop_thread_hard(self, thread: QThread | None, name: str) -> None:
        """
        Best-effort thread shutdown for tray Exit.

        PaddleOCR/requests can occasionally leave a worker thread alive. If that
        happens, the tray icon disappears but the process stays in Task Manager.
        For explicit user Exit, prefer a reliable shutdown.
        """
        if thread is None:
            return

        try:
            thread.requestInterruption()
        except Exception:
            pass

        try:
            thread.quit()
        except Exception:
            pass

        try:
            if not thread.wait(1500):
                self.append_log(f"{name} did not stop cleanly; terminating thread.")
                thread.terminate()
                thread.wait(1500)
        except Exception as exc:
            try:
                self.append_log(f"{name} stop error: {type(exc).__name__}: {exc}")
            except Exception:
                pass

    def exit_app(self) -> None:
        self.unregister_hotkeys()

        try:
            self.ocr_requested.disconnect(self.ocr_worker.process_image)
        except Exception:
            pass

        try:
            self.translation_requested.disconnect(self.translation_worker.translate_text)
        except Exception:
            pass

        try:
            self.popup.hide()
            self.status_toast.hide()
            self.tray.hide()
        except Exception:
            pass

        try:
            app = QApplication.instance()
            if app is not None and self._native_hotkey_filter_installed:
                app.removeNativeEventFilter(self.native_hotkey_filter)
                self._native_hotkey_filter_installed = False
        except Exception:
            pass

        self._stop_thread_hard(getattr(self, "ocr_thread", None), "OCR thread")
        self._stop_thread_hard(getattr(self, "translation_thread", None), "Translation thread")

        QApplication.quit()

        # Final safety net for explicit Exit. This prevents hidden worker hooks
        # or native OCR threads from leaving ScreenshotTranslator.exe in Task
        # Manager and blocking rebuild/delete operations.
        QTimer.singleShot(250, lambda: os._exit(0))


def main() -> int:
    set_windows_dpi_awareness()

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(load_app_icon())
    app.setQuitOnLastWindowClosed(False)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(None, APP_NAME, "System tray is not available on this system.")
        return 1

    tray_app = TrayScreenshotTranslator()
    app._tray_app = tray_app  # type: ignore[attr-defined]

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
