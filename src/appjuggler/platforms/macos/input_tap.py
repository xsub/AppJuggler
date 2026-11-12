"""macOS CGEventTap + IOHIDManager physical Caps-hold adapter on a dedicated CFRunLoop."""

from __future__ import annotations

import ctypes
from ctypes import CFUNCTYPE, c_int32, c_long, c_uint32, c_void_p
import logging
import threading
import time
from typing import Callable

import Cocoa
import objc
import Quartz

from appjuggler.contracts import InputAdapter
from appjuggler.domain.events import HardwareKeyEvent, InputAction
from appjuggler.platforms.macos.keymap import (
    FLAG_ALPHA_SHIFT,
    VK_CAPS_LOCK,
    KeyEventDecoder,
)
from appjuggler.platforms.macos.permissions import is_screen_locked

logger = logging.getLogger(__name__)

# HID Usage constants for physical Caps Lock switch (USB HID Usage Tables §10)
HID_USAGE_PAGE_KEYBOARD = 0x07
HID_USAGE_CAPS_LOCK = 0x39

_IOHIDValueCallback = CFUNCTYPE(None, c_void_p, c_int32, c_void_p, c_void_p)


class _LayoutChangeObserver(Cocoa.NSObject):
    """Main-thread observer refreshing the keycode->letter map when input source changes."""

    def initWithCallback_(self, callback: Callable[[], None]):
        self = objc.super(_LayoutChangeObserver, self).init()
        if self is None:
            return None
        self._callback = callback
        return self

    def inputSourceChanged_(self, _notification) -> None:
        if self._callback is not None:
            self._callback()


class MacOSInputAdapter(InputAdapter):
    """Low-latency CGEventTap + IOHIDManager physical Caps Lock hold listener.

    - `CGEventTap` handles double-Caps Lock (`AlphaShift` toggles) and modal key swallowing.
    - `IOHIDManager` observes raw physical key contact (`1` down / `0` up) on Caps Lock
      (`UsagePage=0x07, Usage=0x39`), firing `on_caps_hold` when physically held continuously
      for `hold_assign_ms` (default 2000 ms) without blocking normal single/double taps.
    """

    def __init__(self, hold_assign_ms: int = 2000) -> None:
        initial_caps = self._read_current_caps_lock()
        self._decoder = KeyEventDecoder(initial_caps_on=initial_caps)
        self._decoder.refresh_from_system_layout()

        self._lock = threading.Lock()
        self._running = False
        self._tap = None
        self._runloop_source = None
        self._tap_runloop = None
        self._thread: threading.Thread | None = None
        self._callback_ref = None
        self._hid_callback_ref = None

        self._on_event: Callable[[HardwareKeyEvent], InputAction] | None = None
        self._on_interrupt: Callable[[str], None] | None = None
        self._on_caps_hold: Callable[[], None] | None = None
        self._layout_observer: _LayoutChangeObserver | None = None

        # Physical Caps Lock 2-second hold state
        self._hold_lock = threading.Lock()
        self._hold_assign_sec: float = max(0.5, min(5.0, hold_assign_ms / 1000.0))
        self._caps_hold_seq: int = 0
        self._caps_hold_timer: threading.Timer | None = None
        self._caps_physically_down: bool = False

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running and self._tap is not None

    @property
    def layout_description(self) -> str:
        return self._decoder.layout_description

    def set_hold_assign_ms(self, hold_ms: int) -> None:
        with self._hold_lock:
            self._hold_assign_sec = max(0.5, min(5.0, hold_ms / 1000.0))

    def resync_modifier_state(self) -> None:
        self._cancel_caps_hold_timer()
        self._decoder.sync_caps_state(self._read_current_caps_lock())

    def refresh_keyboard_layout_on_main_thread(self) -> None:
        self._decoder.refresh_from_system_layout()

    def start(
        self,
        on_event: Callable[[HardwareKeyEvent], InputAction],
        on_interrupt: Callable[[str], None],
        on_caps_hold: Callable[[], None] | None = None,
    ) -> bool:
        with self._lock:
            if self._running:
                return True
            if is_screen_locked():
                return False

            self._on_event = on_event
            self._on_interrupt = on_interrupt
            self._on_caps_hold = on_caps_hold
            self.resync_modifier_state()

            ready_event = threading.Event()
            created_ok = [False]

            def _run_tap_loop() -> None:
                mask = (
                    Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
                    | Quartz.CGEventMaskBit(Quartz.kCGEventKeyUp)
                    | Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged)
                )
                self._callback_ref = self._tap_callback
                tap = Quartz.CGEventTapCreate(
                    Quartz.kCGSessionEventTap,
                    Quartz.kCGHeadInsertEventTap,
                    Quartz.kCGEventTapOptionDefault,
                    mask,
                    self._callback_ref,
                    None,
                )
                if tap is None:
                    ready_event.set()
                    return

                source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
                rl = Quartz.CFRunLoopGetCurrent()
                Quartz.CFRunLoopAddSource(rl, source, Quartz.kCFRunLoopCommonModes)
                Quartz.CGEventTapEnable(tap, True)

                if not Quartz.CGEventTapIsEnabled(tap):
                    Quartz.CFMachPortInvalidate(tap)
                    ready_event.set()
                    return

                hid_cleanup = self._attach_iohid_caps_monitor()

                with self._lock:
                    self._tap = tap
                    self._runloop_source = source
                    self._tap_runloop = rl
                    self._running = True
                created_ok[0] = True
                ready_event.set()

                try:
                    Quartz.CFRunLoopRun()
                finally:
                    if hid_cleanup is not None:
                        try:
                            hid_cleanup()
                        except Exception:
                            pass
                    with self._lock:
                        self._running = False
                        self._tap = None
                        self._runloop_source = None
                        self._tap_runloop = None

            self._thread = threading.Thread(
                target=_run_tap_loop,
                name="AppJuggler-CGEventTap",
                daemon=True,
            )
            self._thread.start()

        ready_event.wait(timeout=2.0)
        if created_ok[0]:
            self._register_layout_observer()
            logger.info("CGEventTap + IOHID Caps monitor started (layout=%s)", self._decoder.layout_description)
            return True
        return False

    def stop(self) -> None:
        self._cancel_caps_hold_timer()
        self._unregister_layout_observer()
        with self._lock:
            tap = self._tap
            rl = self._tap_runloop
            self._running = False

        if tap is not None:
            try:
                Quartz.CGEventTapEnable(tap, False)
                Quartz.CFMachPortInvalidate(tap)
            except Exception:
                pass
        if rl is not None:
            try:
                Quartz.CFRunLoopStop(rl)
            except Exception:
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None
        logger.info("CGEventTap stopped")

    def _attach_iohid_caps_monitor(self) -> Callable[[], None] | None:
        """Schedule an IOHIDManager keyboard listener on the current thread's CFRunLoop."""
        try:
            iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
            cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

            iokit.IOHIDManagerCreate.argtypes = [c_void_p, c_uint32]
            iokit.IOHIDManagerCreate.restype = c_void_p
            iokit.IOHIDManagerSetDeviceMatchingMultiple.argtypes = [c_void_p, c_void_p]
            iokit.IOHIDManagerRegisterInputValueCallback.argtypes = [
                c_void_p,
                _IOHIDValueCallback,
                c_void_p,
            ]
            iokit.IOHIDManagerScheduleWithRunLoop.argtypes = [c_void_p, c_void_p, c_void_p]
            iokit.IOHIDManagerUnscheduleFromRunLoop.argtypes = [c_void_p, c_void_p, c_void_p]
            iokit.IOHIDManagerOpen.argtypes = [c_void_p, c_uint32]
            iokit.IOHIDManagerOpen.restype = c_int32
            iokit.IOHIDManagerClose.argtypes = [c_void_p, c_uint32]

            iokit.IOHIDValueGetElement.argtypes = [c_void_p]
            iokit.IOHIDValueGetElement.restype = c_void_p
            iokit.IOHIDValueGetIntegerValue.argtypes = [c_void_p]
            iokit.IOHIDValueGetIntegerValue.restype = c_long
            iokit.IOHIDElementGetUsagePage.argtypes = [c_void_p]
            iokit.IOHIDElementGetUsagePage.restype = c_uint32
            iokit.IOHIDElementGetUsage.argtypes = [c_void_p]
            iokit.IOHIDElementGetUsage.restype = c_uint32

            cf.CFRunLoopGetCurrent.restype = c_void_p
            cf.CFRelease.argtypes = [c_void_p]
            common_modes = c_void_p.in_dll(cf, "kCFRunLoopCommonModes")

            def _hid_cb(_ctx, _result, _sender, value) -> None:
                if not value:
                    return
                el = iokit.IOHIDValueGetElement(value)
                if not el:
                    return
                if (
                    iokit.IOHIDElementGetUsagePage(el) == HID_USAGE_PAGE_KEYBOARD
                    and iokit.IOHIDElementGetUsage(el) == HID_USAGE_CAPS_LOCK
                ):
                    is_down = iokit.IOHIDValueGetIntegerValue(value) != 0
                    self._on_physical_caps_key_change(is_down)

            self._hid_callback_ref = _IOHIDValueCallback(_hid_cb)
            mgr = iokit.IOHIDManagerCreate(None, 0)
            if not mgr:
                return None

            # Match both standard Keyboards (1, 6) and Keypads (1, 7)
            criteria = Cocoa.NSArray.arrayWithArray_(
                [
                    {"DeviceUsagePage": 1, "DeviceUsage": 6},
                    {"DeviceUsagePage": 1, "DeviceUsage": 7},
                ]
            )
            iokit.IOHIDManagerSetDeviceMatchingMultiple(mgr, objc.pyobjc_id(criteria))
            iokit.IOHIDManagerRegisterInputValueCallback(mgr, self._hid_callback_ref, None)

            raw_rl = cf.CFRunLoopGetCurrent()
            iokit.IOHIDManagerScheduleWithRunLoop(mgr, raw_rl, common_modes)
            open_ret = iokit.IOHIDManagerOpen(mgr, 0)
            if open_ret != 0:
                logger.warning("IOHIDManagerOpen returned %d; Caps 2s hold fallback via Tab in HUD remains available", open_ret)
                iokit.IOHIDManagerUnscheduleFromRunLoop(mgr, raw_rl, common_modes)
                cf.CFRelease(mgr)
                return None

            def _cleanup() -> None:
                iokit.IOHIDManagerUnscheduleFromRunLoop(mgr, raw_rl, common_modes)
                iokit.IOHIDManagerClose(mgr, 0)
                cf.CFRelease(mgr)

            return _cleanup
        except Exception:
            logger.exception("Failed to attach IOHIDManager Caps Lock hold monitor")
            return None

    def _on_physical_caps_key_change(self, is_down: bool) -> None:
        with self._hold_lock:
            if is_down:
                if self._caps_physically_down:
                    return
                self._caps_physically_down = True
                if self._caps_hold_timer is not None:
                    self._caps_hold_timer.cancel()
                self._caps_hold_seq += 1
                seq = self._caps_hold_seq
                timer = threading.Timer(
                    self._hold_assign_sec,
                    self._fire_caps_hold_if_still_down,
                    args=(seq,),
                )
                timer.daemon = True
                self._caps_hold_timer = timer
                timer.start()
            else:
                self._caps_physically_down = False
                self._caps_hold_seq += 1
                if self._caps_hold_timer is not None:
                    self._caps_hold_timer.cancel()
                    self._caps_hold_timer = None

    def _cancel_caps_hold_timer(self) -> None:
        with self._hold_lock:
            self._caps_physically_down = False
            self._caps_hold_seq += 1
            if self._caps_hold_timer is not None:
                self._caps_hold_timer.cancel()
                self._caps_hold_timer = None

    def _fire_caps_hold_if_still_down(self, expected_seq: int) -> None:
        with self._hold_lock:
            if not self._caps_physically_down or self._caps_hold_seq != expected_seq:
                return
            self._caps_hold_timer = None
            cb = self._on_caps_hold
        if cb is not None:
            try:
                cb()
            except Exception:
                logger.exception("Error in Caps Lock 2-second hold callback")

    def _tap_callback(self, _proxy, event_type: int, event, _refcon):
        if event_type in (
            Quartz.kCGEventTapDisabledByTimeout,
            Quartz.kCGEventTapDisabledByUserInput,
        ):
            reason = (
                "tap_disabled_timeout"
                if event_type == Quartz.kCGEventTapDisabledByTimeout
                else "tap_disabled_user"
            )
            logger.warning("CGEventTap disabled (%s); attempting immediate recovery", reason)
            self._cancel_caps_hold_timer()
            if self._on_interrupt is not None:
                try:
                    self._on_interrupt(reason)
                except Exception:
                    pass
            self.resync_modifier_state()
            tap = self._tap
            if tap is not None:
                Quartz.CGEventTapEnable(tap, True)
                if not Quartz.CGEventTapIsEnabled(tap):
                    logger.error("CGEventTap could not be re-enabled (permissions revoked)")
                    if self._on_interrupt is not None:
                        try:
                            self._on_interrupt("permissions_revoked")
                        except Exception:
                            pass
            return event

        on_event = self._on_event
        if on_event is None or event is None:
            return event

        try:
            now_ns = time.monotonic_ns()
            keycode = int(
                Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
            )
            flags = int(Quartz.CGEventGetFlags(event))

            # Any non-Caps key or modifier press cancels a pending 2-second Caps hold!
            if keycode != VK_CAPS_LOCK and event_type in (
                Quartz.kCGEventKeyDown,
                Quartz.kCGEventFlagsChanged,
            ):
                self._cancel_caps_hold_timer()

            if event_type == Quartz.kCGEventFlagsChanged:
                hw_event = self._decoder.decode_flags_changed(
                    keycode=keycode,
                    flags=flags,
                    timestamp_ns=now_ns,
                )
            elif event_type in (Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp):
                is_down = event_type == Quartz.kCGEventKeyDown
                is_repeat = bool(
                    Quartz.CGEventGetIntegerValueField(
                        event, Quartz.kCGKeyboardEventAutorepeat
                    )
                )
                hw_event = self._decoder.decode_key_event(
                    keycode=keycode,
                    flags=flags,
                    is_key_down=is_down,
                    is_autorepeat=is_repeat,
                    timestamp_ns=now_ns,
                )
            else:
                return event

            action = on_event(hw_event)
            if action is InputAction.SWALLOW:
                return None
            return event
        except Exception:
            logger.exception("Unhandled error inside CGEventTap callback; passing event through")
            return event

    @staticmethod
    def _read_current_caps_lock() -> bool:
        flags = int(
            Quartz.CGEventSourceFlagsState(Quartz.kCGEventSourceStateCombinedSessionState)
        )
        return bool(flags & FLAG_ALPHA_SHIFT)

    def _register_layout_observer(self) -> None:
        if self._layout_observer is not None:
            return
        try:
            obs = _LayoutChangeObserver.alloc().initWithCallback_(
                self.refresh_keyboard_layout_on_main_thread
            )
            dnc = Cocoa.NSDistributedNotificationCenter.defaultCenter()
            dnc.addObserver_selector_name_object_(
                obs,
                "inputSourceChanged:",
                "com.apple.Carbon.TISNotifySelectedKeyboardInputSourceChanged",
                None,
            )
            self._layout_observer = obs
        except Exception:
            logger.debug("Could not register input-source distributed notification observer")

    def _unregister_layout_observer(self) -> None:
        if self._layout_observer is None:
            return
        try:
            dnc = Cocoa.NSDistributedNotificationCenter.defaultCenter()
            dnc.removeObserver_(self._layout_observer)
        except Exception:
            pass
        self._layout_observer = None
