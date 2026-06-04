# Changelog

Engineering log for Screenshot Translator. The app version is tracked in-app
(`APP_VERSION`) and in git history rather than in filenames. Dates approximate.

## v4.1 — current

- DeepSeek translation runs on its own worker thread; OCR text is preserved and
  copied to the clipboard even when the API call fails, so a network/key error never
  costs you the recognized text.
- Tray tooltip and menu reflect the live hotkey bindings and current OCR language.
- Per-month output folders (`YYYY-MM/`) under `DATA/ScreenshotTranslator/` for
  screenshots, OCR text, and translation output.

## v3.8 — DPI / scaling correctness (the bug that drove the rewrite)

The selection overlay originally subtracted monitor offsets from the selection rect.
On laptops and multi-monitor setups with display scaling, Qt's widget coordinates and
mss's screenshot pixels are **not** 1:1, so the offset math produced crops shifted away
from what the user dragged over. Fixed by having the overlay store *widget-local*
coordinates and convert them to frozen-image pixel coordinates in one place
(`local_rect_to_image_rect`), scaling by `image_size / widget_size`. Monitor-offset
subtraction was removed entirely — reintroducing it is the easiest way to bring the bug
back.

## v3.x — native hotkeys, model cache, language handling

- **Native Win32 hotkeys.** Replaced the `keyboard` package's global hook (used in
  early versions) with `RegisterHotKey` + a `WM_HOTKEY` native event filter. The
  low-level hook could leave modifier keys (`Ctrl`/`Shift`/`Alt`) logically stuck after
  a capture; the native API doesn't. The `keyboard` package is now used only inside the
  hotkey-editor dialog to *record* a new shortcut — and the app unregisters its own
  hotkeys while that dialog is open so the recorder isn't fighting the live bindings.
- **Local model seeding.** Bundled PaddleOCR models are copied into the user's PaddleX
  cache on startup (size-compared, skip-if-present), with import/export tools in the
  debug menu and a copy-report log. Weights are never committed — they seed/download at
  runtime.
- **Language fallbacks.** `auto` uses PaddleOCR's default; `en`/`japan` use their
  models; `fr`/`tr` map to the English model on purpose — the dedicated `latin` model
  raised `No models are available for the language 'latin'` on this PaddleX build and
  the per-language models triggered extra remote downloads. Mapping to `en` handles
  most Latin-script text without that cost.

## v2.5 — tray-menu screenshots without a pre-capture delay

Earlier versions closed the tray menu *before* grabbing the screen, which both lost the
menu from the capture and added a visible delay. Now the full virtual desktop is grabbed
*first* (while transient UI like the open tray menu is still on screen), then the real
menu is closed so it can't steal the first mouse click from the selection overlay. The
overlay paints the frozen grab underneath itself, so menus and tooltips can be captured.

## v2.x — OCR pipeline and threading

- **Persistent OCR worker.** PaddleOCR is initialized once in a `QThread` worker and
  reused across captures instead of constructing a new engine per image; changing the
  OCR language tears down and rebuilds that worker.
- **Tolerant result parsing.** `extract_text_lines_from_result` walks PaddleOCR's
  varying result shapes (dicts with `rec_texts`/`rec_scores`, nested list/tuple
  structures, generic text keys) and applies a configurable min-score filter.

## Cross-cutting / platform

- **No flashing consoles.** On Windows, `subprocess.Popen` is subclassed
  (`CREATE_NO_WINDOW` + hidden `STARTUPINFO`) so PaddleOCR's child processes don't pop
  console windows. The subclass preserves `Popen` as a class because asyncio subclasses
  it.
- **Diagnosable import failures.** The PaddleOCR import is wrapped; failures are written
  to a startup debug file (no secrets) instead of crashing the tray app.
- **Aggressive, deliberate exit.** The tray Exit stops worker threads and then calls
  `os._exit(0)` as a safety net — lingering Paddle worker threads could otherwise keep
  the process alive in Task Manager and block rebuilds.

## Origin

Merged from two earlier personal tools: a region-screenshot grabber and a PaddleOCR
image-to-text extractor. This app is the daily-driver successor to both.
