"""macOS ApplicationRegistry backed by NSWorkspace."""

from __future__ import annotations

import logging
import os
from pathlib import Path
import threading
import time
from typing import Callable

import Cocoa
import objc

from appjuggler import APP_BUNDLE_ID
from appjuggler.contracts import ApplicationRegistry, LaunchResult, WindowRegistry
from appjuggler.domain.models import AppRecord

logger = logging.getLogger(__name__)


class _WorkspaceObserver(Cocoa.NSObject):
    """Objective-C notification receiver for NSWorkspace app lifecycle events."""

    def initWithCallbacks_(
        self,
        callbacks: tuple[
            Callable[[str | None, int], None],
            Callable[[str | None, int], None],
            Callable[[str | None, int], None],
        ],
    ):
        self = objc.super(_WorkspaceObserver, self).init()
        if self is None:
            return None
        self._on_launch, self._on_terminate, self._on_activate = callbacks
        return self

    def appDidLaunch_(self, notification) -> None:
        bid, pid = self._extract(notification)
        self._on_launch(bid, pid)

    def appDidTerminate_(self, notification) -> None:
        bid, pid = self._extract(notification)
        self._on_terminate(bid, pid)

    def appDidActivate_(self, notification) -> None:
        bid, pid = self._extract(notification)
        self._on_activate(bid, pid)

    @staticmethod
    def _extract(notification) -> tuple[str | None, int]:
        info = notification.userInfo() if notification is not None else None
        app = info.get(Cocoa.NSWorkspaceApplicationKey) if info is not None else None
        if app is None:
            return None, 0
        raw_bid = app.bundleIdentifier()
        bid = str(raw_bid) if raw_bid else None
        pid = int(app.processIdentifier())
        return bid, pid


class MacOSApplicationRegistry(ApplicationRegistry):
    """Discovers running & installed macOS apps, caches icons, and launches closed apps."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._observer: _WorkspaceObserver | None = None
        self._lifecycle_listeners: list[Callable[[str, str | None, int], None]] = []
        self._icon_cache: dict[str, object] = {}
        self._launch_signal = threading.Condition()

    def start_observing(
        self, on_lifecycle: Callable[[str, str | None, int], None] | None = None
    ) -> None:
        with self._lock:
            if on_lifecycle is not None and on_lifecycle not in self._lifecycle_listeners:
                self._lifecycle_listeners.append(on_lifecycle)
            if self._observer is not None:
                return

            obs = _WorkspaceObserver.alloc().initWithCallbacks_(
                (
                    lambda bid, pid: self._emit("launch", bid, pid),
                    lambda bid, pid: self._emit("terminate", bid, pid),
                    lambda bid, pid: self._emit("activate", bid, pid),
                )
            )
            nc = Cocoa.NSWorkspace.sharedWorkspace().notificationCenter()
            nc.addObserver_selector_name_object_(
                obs,
                "appDidLaunch:",
                Cocoa.NSWorkspaceDidLaunchApplicationNotification,
                None,
            )
            nc.addObserver_selector_name_object_(
                obs,
                "appDidTerminate:",
                Cocoa.NSWorkspaceDidTerminateApplicationNotification,
                None,
            )
            nc.addObserver_selector_name_object_(
                obs,
                "appDidActivate:",
                Cocoa.NSWorkspaceDidActivateApplicationNotification,
                None,
            )
            self._observer = obs

    def stop_observing(self) -> None:
        with self._lock:
            obs = self._observer
            self._observer = None
        if obs is not None:
            try:
                Cocoa.NSWorkspace.sharedWorkspace().notificationCenter().removeObserver_(obs)
            except Exception:
                pass

    def _emit(self, kind: str, bundle_id: str | None, pid: int) -> None:
        with self._launch_signal:
            self._launch_signal.notify_all()
        with self._lock:
            listeners = list(self._lifecycle_listeners)
        for cb in listeners:
            try:
                cb(kind, bundle_id, pid)
            except Exception:
                logger.exception("Error in application lifecycle listener")

    def running_apps(self) -> tuple[AppRecord, ...]:
        ws = Cocoa.NSWorkspace.sharedWorkspace()
        grouped: dict[str, dict] = {}
        missing_bundle_records: list[AppRecord] = []
        own_pid = os.getpid()

        for app in ws.runningApplications():
            if app.isTerminated():
                continue
            pid = int(app.processIdentifier())
            if pid == own_pid:
                continue
            if app.activationPolicy() != Cocoa.NSApplicationActivationPolicyRegular:
                continue

            raw_bid = app.bundleIdentifier()
            bid = str(raw_bid) if raw_bid else None
            if bid == APP_BUNDLE_ID:
                continue

            name = str(app.localizedName() or "Unknown App")
            b_url = app.bundleURL()
            b_path = str(b_url.path()) if b_url is not None and b_url.path() else None
            hidden = bool(app.isHidden())

            if bid is None:
                # Edge case (§2.2): regular running process without a bundle identifier.
                missing_bundle_records.append(
                    AppRecord(
                        bundle_id=None,
                        display_name=name,
                        is_running=True,
                        pids=(pid,),
                        bundle_path=b_path,
                        candidate_paths=(b_path,) if b_path else (),
                        is_hidden=hidden,
                    )
                )
                continue

            entry = grouped.get(bid)
            if entry is None:
                candidates = self.resolve_bundle_urls(bid, hint_path=b_path)
                grouped[bid] = {
                    "display_name": name,
                    "pids": [pid],
                    "bundle_path": b_path or (candidates[0] if candidates else None),
                    "candidate_paths": candidates,
                    "is_hidden": hidden,
                }
            else:
                entry["pids"].append(pid)
                entry["is_hidden"] = entry["is_hidden"] and hidden

        records = [
            AppRecord(
                bundle_id=bid,
                display_name=info["display_name"],
                is_running=True,
                pids=tuple(info["pids"]),
                bundle_path=info["bundle_path"],
                candidate_paths=info["candidate_paths"],
                is_hidden=info["is_hidden"],
            )
            for bid, info in grouped.items()
        ]
        records.extend(missing_bundle_records)
        records.sort(key=lambda r: r.display_name.casefold())
        return tuple(records)

    def running_bundle_ids(self) -> frozenset[str]:
        ws = Cocoa.NSWorkspace.sharedWorkspace()
        own_pid = os.getpid()
        result: set[str] = set()
        for app in ws.runningApplications():
            if app.isTerminated() or int(app.processIdentifier()) == own_pid:
                continue
            if app.activationPolicy() != Cocoa.NSApplicationActivationPolicyRegular:
                continue
            bid = app.bundleIdentifier()
            if bid and str(bid) != APP_BUNDLE_ID:
                result.add(str(bid))
        return frozenset(result)

    def is_bundle_running(self, bundle_id: str) -> bool:
        return bundle_id in self.running_bundle_ids()

    def resolve_bundle_urls(
        self, bundle_id: str, hint_path: str | None = None
    ) -> tuple[str, ...]:
        """Return all installed .app bundle paths registered for `bundle_id`."""
        ws = Cocoa.NSWorkspace.sharedWorkspace()
        paths: list[str] = []

        if hint_path and Path(hint_path).exists():
            paths.append(str(Path(hint_path).resolve()))

        try:
            urls = ws.URLsForApplicationsWithBundleIdentifier_(bundle_id) or ()
            for u in urls:
                p = str(u.path()) if u is not None and u.path() else ""
                if p and Path(p).exists():
                    resolved = str(Path(p).resolve())
                    if resolved not in paths:
                        paths.append(resolved)
        except Exception:
            url = ws.URLForApplicationWithBundleIdentifier_(bundle_id)
            if url is not None and url.path():
                resolved = str(Path(str(url.path())).resolve())
                if resolved not in paths and Path(resolved).exists():
                    paths.append(resolved)

        return tuple(paths)

    def installed_apps(self) -> tuple[AppRecord, ...]:
        """Scan standard macOS Application directories and merge with running apps."""
        running_by_bid = {
            r.bundle_id: r for r in self.running_apps() if r.bundle_id is not None
        }
        search_dirs = [
            Path("/Applications"),
            Path("/System/Applications"),
            Path("/System/Applications/Utilities"),
            Path.home() / "Applications",
        ]

        discovered: dict[str, list[tuple[str, str]]] = {}
        for root in search_dirs:
            if not root.is_dir():
                continue
            try:
                entries = list(root.iterdir())
            except OSError:
                continue
            for item in entries:
                if item.suffix == ".app" and item.is_dir():
                    self._collect_bundle(item, discovered)
                elif item.is_dir() and not item.name.startswith("."):
                    # One level deep (e.g. /Applications/Utilities)
                    try:
                        for sub in item.iterdir():
                            if sub.suffix == ".app" and sub.is_dir():
                                self._collect_bundle(sub, discovered)
                    except OSError:
                        continue

        records: list[AppRecord] = []
        all_bids = set(discovered.keys()) | set(running_by_bid.keys())
        for bid in all_bids:
            if bid == APP_BUNDLE_ID:
                continue
            run_rec = running_by_bid.get(bid)
            found_entries = discovered.get(bid, [])
            cand_paths: list[str] = [p for _, p in found_entries]
            if run_rec:
                for p in run_rec.candidate_paths:
                    if p not in cand_paths:
                        cand_paths.append(p)

            name = (
                run_rec.display_name
                if run_rec is not None
                else (found_entries[0][0] if found_entries else bid)
            )
            primary_path = (
                run_rec.bundle_path
                if (run_rec and run_rec.bundle_path)
                else (cand_paths[0] if cand_paths else None)
            )
            records.append(
                AppRecord(
                    bundle_id=bid,
                    display_name=name,
                    is_running=run_rec is not None,
                    pids=run_rec.pids if run_rec else (),
                    bundle_path=primary_path,
                    candidate_paths=tuple(cand_paths),
                    is_hidden=run_rec.is_hidden if run_rec else False,
                )
            )

        records.sort(key=lambda r: r.display_name.casefold())
        return tuple(records)

    @staticmethod
    def _collect_bundle(
        app_path: Path, out: dict[str, list[tuple[str, str]]]
    ) -> None:
        bundle = Cocoa.NSBundle.bundleWithPath_(str(app_path))
        if bundle is None:
            return
        raw_bid = bundle.bundleIdentifier()
        if not raw_bid:
            return
        bid = str(raw_bid)
        info = bundle.localizedInfoDictionary() or bundle.infoDictionary() or {}
        name = str(
            info.get("CFBundleDisplayName")
            or info.get("CFBundleName")
            or app_path.stem
        )
        resolved = str(app_path.resolve())
        existing_paths = [p for _, p in out.get(bid, [])]
        if resolved not in existing_paths:
            out.setdefault(bid, []).append((name, resolved))

    def app_icon_for_bundle(
        self, bundle_id: str, hint_path: str | None = None
    ) -> object | None:
        with self._lock:
            cached = self._icon_cache.get(bundle_id)
            if cached is not None:
                return cached

        paths = self.resolve_bundle_urls(bundle_id, hint_path=hint_path)
        if not paths:
            return None
        icon = Cocoa.NSWorkspace.sharedWorkspace().iconForFile_(paths[0])
        if icon is not None:
            with self._lock:
                self._icon_cache[bundle_id] = icon
        return icon

    def launch_and_wait_for_ready(
        self,
        bundle_id: str,
        hint_path: str | None,
        window_registry: WindowRegistry,
        is_cancelled: Callable[[], bool],
        timeout_sec: float = 8.0,
    ) -> LaunchResult:
        """Launch a closed app and wait event-driven for process & window readiness (§2.5)."""
        paths = self.resolve_bundle_urls(bundle_id, hint_path=hint_path)
        if not paths:
            return LaunchResult(
                success=False,
                missing_bundle=True,
                error=f"Application bundle not found for '{bundle_id}'. Repair or remove in Edit Assignments.",
            )

        target_path = paths[0]
        url = Cocoa.NSURL.fileURLWithPath_(target_path)
        ws = Cocoa.NSWorkspace.sharedWorkspace()
        config = Cocoa.NSWorkspaceOpenConfiguration.configuration()
        config.setActivates_(True)

        launch_done = threading.Event()
        launch_err_holder: list[str | None] = [None]

        def _completion(_app, err) -> None:
            if err is not None:
                launch_err_holder[0] = str(err.localizedDescription())
            launch_done.set()
            with self._launch_signal:
                self._launch_signal.notify_all()

        ws.openApplicationAtURL_configuration_completionHandler_(
            url, config, _completion
        )

        deadline = time.monotonic() + timeout_sec
        process_seen_at: float | None = None

        while time.monotonic() < deadline:
            if is_cancelled():
                return LaunchResult(success=False, error="Cancelled")

            if launch_done.is_set() and launch_err_holder[0] is not None:
                return LaunchResult(
                    success=False,
                    error=f"Failed to launch '{bundle_id}': {launch_err_holder[0]}",
                    bundle_path_used=target_path,
                )

            if self.is_bundle_running(bundle_id):
                now = time.monotonic()
                if process_seen_at is None:
                    process_seen_at = now

                finished = self._is_finished_launching(bundle_id)
                wins = window_registry.discover_windows(bundle_id, timeout_sec=0.35)
                if len(wins) > 0 and (finished or (now - process_seen_at) >= 0.20):
                    return LaunchResult(success=True, bundle_path_used=target_path)

                # Desktop apps (especially Electron/Chromium/SwiftUI) often create their
                # initial main window 0.4s-1.8s AFTER `isFinishedLaunching` becomes True.
                # Wait up to 2.0s (event-driven and immediately cancellable via Esc/Caps×2)
                # before concluding the app launched with 0 windows.
                if launch_done.is_set() and finished:
                    if (now - process_seen_at) >= 2.0:
                        return LaunchResult(success=True, bundle_path_used=target_path)

            remaining = max(0.02, min(0.08, deadline - time.monotonic()))
            with self._launch_signal:
                self._launch_signal.wait(timeout=remaining)

        if self.is_bundle_running(bundle_id):
            return LaunchResult(success=True, bundle_path_used=target_path)

        return LaunchResult(
            success=False,
            error=f"Timed out waiting for '{bundle_id}' to launch",
            bundle_path_used=target_path,
        )

    @staticmethod
    def _is_finished_launching(bundle_id: str) -> bool:
        ws = Cocoa.NSWorkspace.sharedWorkspace()
        for app in ws.runningApplications():
            if not app.isTerminated() and str(app.bundleIdentifier() or "") == bundle_id:
                return bool(app.isFinishedLaunching())
        return False
