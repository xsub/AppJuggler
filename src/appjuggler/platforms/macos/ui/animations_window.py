"""Dedicated dialog to disable macOS window animations & configure 0 ms switching."""

from __future__ import annotations

import threading
from typing import Callable

import Cocoa
import objc

from appjuggler.persistence.binding_store import BindingStore
from appjuggler.platforms.macos.animations import (
    TERMINAL_DISABLE_COMMANDS,
    apply_fast_macos_animations,
    open_reduce_motion_settings,
    read_macos_animation_status,
    restore_default_macos_animations,
)
from appjuggler.platforms.macos.ui import theme

_GUIDE_TEXT = """\
DLACZEGO PRZEŁĄCZANIE W MACOS „PRZESUWA” LUB „ZNIKA” (SLIDE / FADE)?

1. Natywny tryb Full Screen w macOS = osobne wirtualne Biurko (Space):
   • Każde okno przełączone w natywny Full Screen (zielona kropka / Ctrl+Cmd+F) trafia na osobny Space.
   • WindowServer w macOS ma zaszytą na sztywno animację zmiany Space'a:
     – ok. 500–600 ms przesuwania (gdy „Zredukuj ruch” jest wyłączone),
     – ok. 250–300 ms przenikania/znikania (gdy „Zredukuj ruch” jest włączone).
   • Żadna komenda systemowa `defaults write` nie skraca zmiany Space'a do 0 ms (bez wyłączenia SIP).

2. Jak uzyskać PRAWDZIWE 0 ms (zero przesuwania i zero znikania) w AppJugglerze:
   • Krok 1 — Kliknij „Disable macOS Window Animations” powyżej:
     AppJuggler automatycznie wyłączy animacje otwierania/zamykania okien AppKit, animacje zmiany rozmiaru okien, zmieni minimalizację Docka na szybkie „Scale”, przyspieszy Mission Control i wyłączy animacje Findera (zrestartuje Dock i Finder; już otwarte aplikacje wczytają brak animacji okien po ich ponownym uruchomieniu).
   • Krok 2 — Kliknij „Use Instant Maximize (0 ms)” zamiast Native Full Screen:
     AppJuggler rozciąga okna na cały ekran przez Accessibility API (< 1 ms) NA TYM SAMYM BIURKU (Space), bez tworzenia nowych Space'ów. Jeśli masz już aplikacje otwarte w natywnym Full Screen, kliknij „Exit Full Screen on Open Apps”, aby sprowadzić je na wspólne Biurko.
   • Krok 3 — Włącz ręcznie „Zredukuj ruch” (chronione przez macOS TCC):
     Kliknij „Open Display Settings (Zredukuj ruch)…” i włącz przełącznik:
     Ustawienia systemowe → Dostępność → Ekran → Zredukuj ruch
     (System Settings → Accessibility → Display → Reduce motion).
"""


class _AnimationsDialogDelegate(Cocoa.NSObject):
    def initWithCallbacks_(self, callbacks: dict[str, Callable]):
        self = objc.super(_AnimationsDialogDelegate, self).init()
        if self is None:
            return None
        self._cb = callbacks
        return self

    def windowDidBecomeKey_(self, _note) -> None:
        self._cb["on_refresh"]()

    def refreshClicked_(self, _sender) -> None:
        self._cb["on_refresh"]()

    def disableAnimationsClicked_(self, _sender) -> None:
        self._cb["on_disable_animations"]()

    def restoreDefaultsClicked_(self, _sender) -> None:
        self._cb["on_restore_defaults"]()

    def copyCommandsClicked_(self, _sender) -> None:
        self._cb["on_copy_commands"]()

    def enableInstantMaximizeClicked_(self, _sender) -> None:
        self._cb["on_enable_instant_maximize"]()

    def exitOpenFullscreenClicked_(self, _sender) -> None:
        self._cb["on_exit_open_fullscreen"]()

    def openReduceMotionClicked_(self, _sender) -> None:
        self._cb["on_open_reduce_motion"]()

    def closeClicked_(self, _sender) -> None:
        self._cb["on_close"]()

    def finishExitFullscreenOnMain_(self, converted_num) -> None:
        self._cb["on_exit_fullscreen_done"](int(converted_num))


class _SectionBoxView(Cocoa.NSView):
    """Subtle bordered section container for settings groups."""

    def initWithFrame_(self, frame):
        self = objc.super(_SectionBoxView, self).initWithFrame_(frame)
        return self

    def drawRect_(self, _dirty_rect) -> None:
        bounds = Cocoa.NSInsetRect(self.bounds(), 0.5, 0.5)
        bg = Cocoa.NSColor.controlBackgroundColor().colorWithAlphaComponent_(0.55)
        border = Cocoa.NSColor.separatorColor()
        theme.fill_round_rect(bounds, 8.0, bg, border, 1.0)


class WindowAnimationsDialogController:
    """Interactive helper & explanatory dialog for macOS animations and 0 ms switching."""

    def __init__(
        self,
        store: BindingStore,
        on_config_updated: Callable[[], None],
        count_fullscreen_windows: Callable[[], int] | None = None,
        exit_fullscreen_windows: Callable[[], int] | None = None,
    ) -> None:
        self._store = store
        self._on_config_updated = on_config_updated
        self._count_fullscreen_windows = count_fullscreen_windows or (lambda: 0)
        self._exit_fullscreen_windows = exit_fullscreen_windows or (lambda: 0)

        self._window: Cocoa.NSWindow | None = None
        self._delegate: _AnimationsDialogDelegate | None = None
        self._exiting_fs_busy: bool = False

        self._defaults_status_lbl = None
        self._space_status_lbl = None
        self._reduce_motion_status_lbl = None
        self._exit_fs_btn = None
        self._instant_max_btn = None
        self._feedback_lbl = None

    def show_window(self, parent_window: Cocoa.NSWindow | None = None) -> None:
        if self._window is None:
            self._build_window()
        assert self._window is not None
        if parent_window is not None and not self._window.isVisible():
            p_frame = parent_window.frame()
            w_frame = self._window.frame()
            x = p_frame.origin.x + (p_frame.size.width - w_frame.size.width) * 0.5
            y = p_frame.origin.y + (p_frame.size.height - w_frame.size.height) * 0.5
            self._window.setFrameOrigin_(Cocoa.NSMakePoint(x, y))
        self.refresh_status()
        Cocoa.NSApp.activateIgnoringOtherApps_(True)
        self._window.makeKeyAndOrderFront_(None)

    def is_visible(self) -> bool:
        return bool(self._window is not None and self._window.isVisible())

    def _build_window(self) -> None:
        win_w = 700.0
        win_h = 584.0
        style = (
            Cocoa.NSWindowStyleMaskTitled
            | Cocoa.NSWindowStyleMaskClosable
            | Cocoa.NSWindowStyleMaskMiniaturizable
        )
        win = Cocoa.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            Cocoa.NSMakeRect(220.0, 160.0, win_w, win_h),
            style,
            Cocoa.NSBackingStoreBuffered,
            False,
        )
        win.setTitle_("AppJuggler — Disable Window Animations & Instant Switching")
        win.setReleasedWhenClosed_(False)

        self._delegate = _AnimationsDialogDelegate.alloc().initWithCallbacks_(
            {
                "on_refresh": self.refresh_status,
                "on_disable_animations": self._handle_disable_animations,
                "on_restore_defaults": self._handle_restore_defaults,
                "on_copy_commands": self._handle_copy_commands,
                "on_enable_instant_maximize": self._handle_enable_instant_maximize,
                "on_exit_open_fullscreen": self._handle_exit_open_fullscreen,
                "on_exit_fullscreen_done": self._handle_exit_fullscreen_done,
                "on_open_reduce_motion": self._handle_open_reduce_motion,
                "on_close": lambda: win.close(),
            }
        )
        win.setDelegate_(self._delegate)

        content = win.contentView()

        # Header
        hdr_lbl = Cocoa.NSTextField.labelWithString_(
            "macOS Window Animations & Zero-Delay Switching"
        )
        hdr_lbl.setFont_(theme.font_ui(15.5, bold=True))
        hdr_lbl.setFrame_(Cocoa.NSMakeRect(20.0, win_h - 40.0, win_w - 170.0, 24.0))
        content.addSubview_(hdr_lbl)

        ref_btn = Cocoa.NSButton.buttonWithTitle_target_action_(
            "Refresh Status", self._delegate, "refreshClicked:"
        )
        ref_btn.setFrame_(Cocoa.NSMakeRect(win_w - 140.0, win_h - 42.0, 120.0, 28.0))
        content.addSubview_(ref_btn)

        # --- Card 1: Programmable macOS Window, Dock & Finder Animations ---
        c1_y = win_h - 142.0
        c1 = _SectionBoxView.alloc().initWithFrame_(
            Cocoa.NSMakeRect(18.0, c1_y, win_w - 36.0, 90.0)
        )
        t1 = Cocoa.NSTextField.labelWithString_(
            "1. System Window, Dock & Finder Animations (Direct Control)"
        )
        t1.setFont_(theme.font_ui(13.0, bold=True))
        t1.setFrame_(Cocoa.NSMakeRect(14.0, 62.0, win_w - 64.0, 20.0))
        c1.addSubview_(t1)

        s1 = Cocoa.NSTextField.labelWithString_("")
        s1.setFont_(theme.font_ui(12.0, bold=False))
        s1.setFrame_(Cocoa.NSMakeRect(14.0, 40.0, win_w - 64.0, 19.0))
        c1.addSubview_(s1)
        self._defaults_status_lbl = s1

        b_dis = Cocoa.NSButton.buttonWithTitle_target_action_(
            "Disable macOS Window Animations",
            self._delegate,
            "disableAnimationsClicked:",
        )
        b_dis.setFrame_(Cocoa.NSMakeRect(12.0, 8.0, 246.0, 28.0))
        c1.addSubview_(b_dis)

        b_res = Cocoa.NSButton.buttonWithTitle_target_action_(
            "Restore macOS Defaults", self._delegate, "restoreDefaultsClicked:"
        )
        b_res.setFrame_(Cocoa.NSMakeRect(266.0, 8.0, 184.0, 28.0))
        c1.addSubview_(b_res)

        b_cpy = Cocoa.NSButton.buttonWithTitle_target_action_(
            "Copy Terminal Commands", self._delegate, "copyCommandsClicked:"
        )
        b_cpy.setFrame_(Cocoa.NSMakeRect(458.0, 8.0, 194.0, 28.0))
        c1.addSubview_(b_cpy)
        content.addSubview_(c1)

        # --- Card 2: AppJuggler Same-Space Instant Maximize vs Full Screen Spaces ---
        c2_y = c1_y - 100.0
        c2 = _SectionBoxView.alloc().initWithFrame_(
            Cocoa.NSMakeRect(18.0, c2_y, win_w - 36.0, 90.0)
        )
        t2 = Cocoa.NSTextField.labelWithString_(
            "2. Single-Space Switching (Eliminates macOS Cross-Space Slide & Fade)"
        )
        t2.setFont_(theme.font_ui(13.0, bold=True))
        t2.setFrame_(Cocoa.NSMakeRect(14.0, 62.0, win_w - 64.0, 20.0))
        c2.addSubview_(t2)

        s2 = Cocoa.NSTextField.labelWithString_("")
        s2.setFont_(theme.font_ui(12.0, bold=False))
        s2.setFrame_(Cocoa.NSMakeRect(14.0, 40.0, win_w - 64.0, 19.0))
        c2.addSubview_(s2)
        self._space_status_lbl = s2

        b_max = Cocoa.NSButton.buttonWithTitle_target_action_(
            "Use Instant Maximize (Same Space — 0 ms)",
            self._delegate,
            "enableInstantMaximizeClicked:",
        )
        b_max.setFrame_(Cocoa.NSMakeRect(12.0, 8.0, 294.0, 28.0))
        c2.addSubview_(b_max)
        self._instant_max_btn = b_max

        b_exit_fs = Cocoa.NSButton.buttonWithTitle_target_action_(
            "Exit Full Screen on Open Apps",
            self._delegate,
            "exitOpenFullscreenClicked:",
        )
        b_exit_fs.setFrame_(Cocoa.NSMakeRect(314.0, 8.0, 260.0, 28.0))
        c2.addSubview_(b_exit_fs)
        self._exit_fs_btn = b_exit_fs
        content.addSubview_(c2)

        # --- Card 3: macOS Reduce Motion (TCC System Settings Toggle) ---
        c3_y = c2_y - 96.0
        c3 = _SectionBoxView.alloc().initWithFrame_(
            Cocoa.NSMakeRect(18.0, c3_y, win_w - 36.0, 86.0)
        )
        t3 = Cocoa.NSTextField.labelWithString_(
            "3. macOS 'Reduce Motion / Zredukuj ruch' (Manual TCC Toggle)"
        )
        t3.setFont_(theme.font_ui(13.0, bold=True))
        t3.setFrame_(Cocoa.NSMakeRect(14.0, 58.0, win_w - 64.0, 20.0))
        c3.addSubview_(t3)

        s3 = Cocoa.NSTextField.labelWithString_("")
        s3.setFont_(theme.font_ui(12.0, bold=False))
        s3.setFrame_(Cocoa.NSMakeRect(14.0, 36.0, win_w - 64.0, 19.0))
        c3.addSubview_(s3)
        self._reduce_motion_status_lbl = s3

        b_rm = Cocoa.NSButton.buttonWithTitle_target_action_(
            "Open Display Settings (Zredukuj ruch)…",
            self._delegate,
            "openReduceMotionClicked:",
        )
        b_rm.setFrame_(Cocoa.NSMakeRect(12.0, 6.0, 286.0, 28.0))
        c3.addSubview_(b_rm)

        rm_hint = Cocoa.NSTextField.labelWithString_(
            "System Settings → Accessibility → Display → Reduce motion"
        )
        rm_hint.setFont_(theme.font_ui(11.5, bold=False))
        rm_hint.setTextColor_(Cocoa.NSColor.secondaryLabelColor())
        rm_hint.setFrame_(Cocoa.NSMakeRect(308.0, 11.0, win_w - 360.0, 18.0))
        c3.addSubview_(rm_hint)
        content.addSubview_(c3)

        # --- Detailed How-To Guide (Scrollable NSTextView) ---
        guide_y = 48.0
        guide_h = c3_y - guide_y - 12.0
        scroll = Cocoa.NSScrollView.alloc().initWithFrame_(
            Cocoa.NSMakeRect(18.0, guide_y, win_w - 36.0, guide_h)
        )
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(Cocoa.NSBezelBorder)

        c_size = scroll.contentSize()
        tv = Cocoa.NSTextView.alloc().initWithFrame_(
            Cocoa.NSMakeRect(0.0, 0.0, c_size.width, c_size.height)
        )
        tv.setEditable_(False)
        tv.setSelectable_(True)
        tv.setTextContainerInset_(Cocoa.NSMakeSize(10.0, 8.0))
        tv.setFont_(theme.font_ui(12.0, bold=False))
        tv.setTextColor_(Cocoa.NSColor.labelColor())
        tv.setString_(_GUIDE_TEXT)
        scroll.setDocumentView_(tv)
        content.addSubview_(scroll)

        # --- Footer: Feedback + Close ---
        fb = Cocoa.NSTextField.labelWithString_("")
        fb.setFont_(theme.font_ui(12.0, bold=True))
        fb.setFrame_(Cocoa.NSMakeRect(20.0, 14.0, win_w - 146.0, 22.0))
        content.addSubview_(fb)
        self._feedback_lbl = fb

        close_btn = Cocoa.NSButton.buttonWithTitle_target_action_(
            "Done", self._delegate, "closeClicked:"
        )
        close_btn.setKeyEquivalent_("\r")
        close_btn.setFrame_(Cocoa.NSMakeRect(win_w - 112.0, 11.0, 94.0, 28.0))
        content.addSubview_(close_btn)

        self._window = win

    def refresh_status(self) -> None:
        if self._window is None:
            return
        st = read_macos_animation_status()

        if st.all_programmable_disabled:
            self._defaults_status_lbl.setStringValue_(
                "✅ Disabled: AppKit window open/close, Cocoa resize, Dock 'Scale' minimize, Mission Control & Finder"
            )
            self._defaults_status_lbl.setTextColor_(Cocoa.NSColor.systemGreenColor())
        else:
            missing: list[str] = []
            if not st.appkit_window_optimized:
                missing.append("AppKit Windows")
            if not st.dock_scale_minimize or not st.mission_control_fast:
                missing.append("Dock/Mission Control")
            if not st.finder_animations_disabled:
                missing.append("Finder")
            self._defaults_status_lbl.setStringValue_(
                f"⚠️ Active macOS animations detected ({', '.join(missing)}) — click button below to disable"
            )
            self._defaults_status_lbl.setTextColor_(Cocoa.NSColor.systemOrangeColor())

        fs_count = 0
        try:
            fs_count = int(self._count_fullscreen_windows())
        except Exception:
            fs_count = 0

        if self._store.auto_fullscreen_on_switch:
            self._space_status_lbl.setStringValue_(
                f"⚠️ Native Full Screen mode is ON (forces cross-Space slide/fade!) · {fs_count} open Full Screen window(s)"
            )
            self._space_status_lbl.setTextColor_(Cocoa.NSColor.systemOrangeColor())
        elif self._store.auto_maximize_on_switch:
            if fs_count > 0:
                self._space_status_lbl.setStringValue_(
                    f"⚠️ Instant Maximize is ON, but {fs_count} open window(s) still occupy Full Screen Spaces"
                )
                self._space_status_lbl.setTextColor_(Cocoa.NSColor.systemOrangeColor())
            else:
                self._space_status_lbl.setStringValue_(
                    "✅ Instant Maximize ON (Same Space — 0 ms) · 0 windows in Full Screen Spaces"
                )
                self._space_status_lbl.setTextColor_(Cocoa.NSColor.systemGreenColor())
        else:
            if fs_count > 0:
                self._space_status_lbl.setStringValue_(
                    f"⚠️ Standard focus mode · {fs_count} open window(s) currently in Full Screen Spaces"
                )
                self._space_status_lbl.setTextColor_(Cocoa.NSColor.systemOrangeColor())
            else:
                self._space_status_lbl.setStringValue_(
                    "✅ Same-Space Standard Focus (0 ms) · Optional: enable Instant Maximize below"
                )
                self._space_status_lbl.setTextColor_(Cocoa.NSColor.systemGreenColor())

        if self._exit_fs_btn is not None and not self._exiting_fs_busy:
            self._exit_fs_btn.setTitle_(
                f"Exit Full Screen on Open Apps ({fs_count})"
                if fs_count > 0
                else "Exit Full Screen on Open Apps (0)"
            )
            self._exit_fs_btn.setEnabled_(fs_count > 0)

        if self._instant_max_btn is not None:
            self._instant_max_btn.setEnabled_(not self._store.auto_maximize_on_switch)

        if st.reduce_motion_enabled:
            self._reduce_motion_status_lbl.setStringValue_(
                "✅ Reduce Motion (Zredukuj ruch): Enabled in macOS System Settings"
            )
            self._reduce_motion_status_lbl.setTextColor_(Cocoa.NSColor.systemGreenColor())
        else:
            self._reduce_motion_status_lbl.setStringValue_(
                "⚠️ Reduce Motion (Zredukuj ruch): OFF — open Display Settings below to replace Space slide with fast fade"
            )
            self._reduce_motion_status_lbl.setTextColor_(Cocoa.NSColor.secondaryLabelColor())

    def _set_feedback(self, msg: str, *, is_error: bool = False) -> None:
        if self._feedback_lbl is None:
            return
        self._feedback_lbl.setStringValue_(msg)
        self._feedback_lbl.setTextColor_(
            Cocoa.NSColor.systemRedColor()
            if is_error
            else Cocoa.NSColor.systemGreenColor()
        )

    def _handle_disable_animations(self) -> None:
        ok, msg = apply_fast_macos_animations(restart_dock_finder=True)
        self.refresh_status()
        self._set_feedback(msg, is_error=not ok)
        self._on_config_updated()

    def _handle_restore_defaults(self) -> None:
        ok, msg = restore_default_macos_animations(restart_dock_finder=True)
        self.refresh_status()
        self._set_feedback(msg, is_error=not ok)
        self._on_config_updated()

    def _handle_copy_commands(self) -> None:
        pb = Cocoa.NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(TERMINAL_DISABLE_COMMANDS, Cocoa.NSPasteboardTypeString)
        self._set_feedback("Copied macOS `defaults write` commands to clipboard.")

    def _handle_enable_instant_maximize(self) -> None:
        try:
            self._store.set_auto_maximize_on_switch(True)
        except Exception as exc:
            self._set_feedback(f"Could not update config: {exc}", is_error=True)
            return
        self._on_config_updated()
        self.refresh_status()
        self._set_feedback(
            "Enabled Instant Maximize (Same Space — 0 ms) and disabled Native Full Screen."
        )

    def _handle_exit_open_fullscreen(self) -> None:
        if self._exiting_fs_busy:
            return
        self._exiting_fs_busy = True
        if self._exit_fs_btn is not None:
            self._exit_fs_btn.setEnabled_(False)
            self._exit_fs_btn.setTitle_("Exiting Full Screen…")
        self._set_feedback("Moving open Full Screen apps onto single Desktop Space…")

        def _worker() -> None:
            try:
                converted = int(self._exit_fullscreen_windows())
            except Exception:
                converted = 0
            if self._delegate is not None:
                self._delegate.performSelectorOnMainThread_withObject_waitUntilDone_(
                    "finishExitFullscreenOnMain:",
                    converted,
                    False,
                )

        threading.Thread(
            target=_worker, name="AppJuggler-ExitFS", daemon=True
        ).start()

    def _handle_exit_fullscreen_done(self, converted: int) -> None:
        self._exiting_fs_busy = False
        self.refresh_status()
        self._set_feedback(
            f"Moved {converted} window(s) from Full Screen Spaces onto the Desktop and maximized them."
        )

    def _handle_open_reduce_motion(self) -> None:
        open_reduce_motion_settings()
        self._set_feedback(
            "Opened System Settings → Accessibility → Display. Toggle 'Reduce motion / Zredukuj ruch' ON."
        )
