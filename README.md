# Screenshot Translator

A Windows system-tray utility that turns any region of the screen into text — or a
translation — from a single global hotkey. Press the shortcut, drag a box over the
frozen desktop, and the cropped region is run through **local PaddleOCR** and
(optionally) the **DeepSeek** chat API. No main window, no cloud OCR, no upload of
the image itself.

It's a daily-driver replacement for ShareX plus the cloud OCR/translation services I
used to paste into — built because I wanted the whole capture → OCR → translate loop
to happen on one keypress, locally, in well under a second after the model warms up.

## Engineering highlights

- **Native Win32 global hotkeys** via `RegisterHotKey` + a `WM_HOTKEY`
  `QAbstractNativeEventFilter` — deliberately *not* the `keyboard` library's low-level
  hook, which could leave `Ctrl`/`Shift`/`Alt` logically stuck. Three independent
  hotkeys route to three pipelines.
- **Local OCR, off the UI thread.** PaddleOCR runs in a persistent `QThread` worker
  initialized once and reused across captures; the DeepSeek HTTP call lives on its own
  worker too, so the tray UI never blocks.
- **DPI-aware capture.** The selection overlay paints a *frozen* full-desktop grab and
  converts widget-local selection coordinates into image pixels, so crops stay correct
  on scaled / multi-monitor displays.
- **Self-contained model cache.** Bundled PaddleOCR models are seeded into the user's
  PaddleX cache on first run; weights are never committed — they download/seed at
  runtime. See [CHANGELOG.md](CHANGELOG.md) for the deeper engineering log.

## What each hotkey does

| Hotkey (default)     | Pipeline                              | Output                                   |
| -------------------- | ------------------------------------- | ---------------------------------------- |
| `Ctrl + PrintScreen` | Region screenshot only                | PNG saved + copied to clipboard          |
| `Shift + PrintScreen`| Screenshot → OCR                      | Text popup + `.txt` saved + text copied  |
| `Alt + PrintScreen`  | Screenshot → OCR → DeepSeek translate | Translation popup + `.txt` saved + copied|

The tray menu adds an OCR-language picker, an in-app hotkey editor, "show/copy last
result", a run-at-Windows-startup toggle, output-folder shortcuts, and a debug submenu
for managing the local model cache. All hotkeys, languages, and API settings persist to
a `.env` the app manages for you.

## Tech stack

Python 3.12 · PyQt6 (tray, overlay, threading) · PaddleOCR 3.5 (local OCR) ·
mss (capture) · Pillow (crop/encode) · DeepSeek chat API over stdlib `urllib` (no SDK) ·
PyInstaller (`onedir`) for packaging. Windows-only in practice — native hotkeys and the
startup toggle use the Win32 API.

## Run it

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\requirements.txt
.\.venv\Scripts\python.exe .\screenshot_translator.py
```

The app starts in the system tray (right-click for the menu, left-click for a quick
OCR capture). On first launch it writes a config `.env` under `DATA/ScreenshotTranslator/`;
to use the translate hotkey, set `DEEPSEEK_API_KEY` there (or via the tray menu). All
captures, extracted text, translations, logs, and the model cache stay under that
gitignored `DATA/` folder — nothing you capture ever lands in the repo.

## Configuration

Settings live in `DATA/ScreenshotTranslator/.env` (seeded with safe defaults on first
run). Notable keys: `DEEPSEEK_API_KEY`, `DEEPSEEK_MODEL`, `DEEPSEEK_TARGET_LANGUAGE`,
`OCR_LANG` (`auto` / `en` / `japan` / `fr` / `tr`), `OCR_MIN_SCORE`, the three
`HOTKEY_*` bindings, and clipboard-copy toggles. Anything you change through the tray
menu is written straight back to this file.

## License

MIT — see [LICENSE](LICENSE).
