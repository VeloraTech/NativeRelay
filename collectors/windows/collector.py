"""Windows 10/11 process and scoped filesystem telemetry collector."""
from __future__ import annotations

import ctypes
import base64
import fnmatch
import json
import os
import platform
import struct
import subprocess
import threading
import time
from ctypes import wintypes
from pathlib import Path

from nativerelay.collector import Collector
from nativerelay.model import Capability, CollectorStatus, Event, EventType, Process, Resource
from nativerelay.stream import EventStream

ERROR_ACCESS_DENIED = 5
ERROR_OPERATION_ABORTED = 995
ERROR_IO_PENDING = 997
ERROR_NOTIFY_ENUM_DIR = 1022
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
WAIT_FAILED = 0xFFFFFFFF

FILE_LIST_DIRECTORY = 0x0001
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OVERLAPPED = 0x40000000

FILE_NOTIFY_CHANGE_FILE_NAME = 0x00000001
FILE_NOTIFY_CHANGE_DIR_NAME = 0x00000002
FILE_NOTIFY_CHANGE_LAST_WRITE = 0x00000010
FILE_NOTIFY_CHANGE_CREATION = 0x00000040

FILE_ACTION_ADDED = 0x00000001
FILE_ACTION_REMOVED = 0x00000002
FILE_ACTION_MODIFIED = 0x00000003
FILE_ACTION_RENAMED_OLD_NAME = 0x00000004
FILE_ACTION_RENAMED_NEW_NAME = 0x00000005

TH32CS_SNAPPROCESS = 0x00000002
ERROR_NO_MORE_FILES = 18


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class _OVERLAPPED_UNION(ctypes.Union):
    _fields_ = [
        ("offsets", wintypes.DWORD * 2),
        ("pointer", ctypes.c_void_p),
    ]


class _OVERLAPPED(ctypes.Structure):
    _anonymous_ = ("union",)
    _fields_ = [
        ("internal", ctypes.c_size_t),
        ("internal_high", ctypes.c_size_t),
        ("union", _OVERLAPPED_UNION),
        ("event", wintypes.HANDLE),
    ]


_PROCESS_WATCHER_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$includeCommand = __INCLUDE_COMMAND__
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Register-WmiEvent -Class Win32_ProcessStartTrace -SourceIdentifier NativeRelay.Start | Out-Null
Register-WmiEvent -Class Win32_ProcessStopTrace -SourceIdentifier NativeRelay.Stop | Out-Null
[Console]::Out.WriteLine('{"kind":"ready"}')
try {
    while ($true) {
        $event = Wait-Event -Timeout 1
        if ($null -eq $event) { continue }
        $native = $event.SourceEventArgs.NewEvent
        $kind = if ($event.SourceIdentifier -eq 'NativeRelay.Start') { 'start' } else { 'stop' }
        $record = @{
            kind = $kind
            pid = [int]$native.ProcessID
            parent_pid = [int]$native.ParentProcessID
            process_name = [string]$native.ProcessName
            session_id = [int]$native.SessionID
            exit_status = [uint32]$native.ExitStatus
        }
        if ($includeCommand -and $kind -eq 'start') {
            $process = Get-CimInstance -ClassName Win32_Process `
                -Filter "ProcessId = $($record.pid)" -ErrorAction SilentlyContinue
            if ($process) { $record.command_line = [string]$process.CommandLine }
        }
        [Console]::Out.WriteLine((ConvertTo-Json -InputObject $record -Compress))
        Remove-Event -EventIdentifier $event.EventIdentifier
    }
}
finally {
    Unregister-Event -SourceIdentifier NativeRelay.Start -ErrorAction SilentlyContinue
    Unregister-Event -SourceIdentifier NativeRelay.Stop -ErrorAction SilentlyContinue
}
"""


class _PathFilter:
    def __init__(self, scope: Path, include_patterns=None, exclude_patterns=None):
        self.scope = os.path.normcase(os.path.abspath(os.fspath(scope)))
        self.include_patterns = tuple(include_patterns or ())
        self.exclude_patterns = tuple(exclude_patterns or ())

    def matches(self, path: Path) -> bool:
        candidate = os.path.normcase(os.path.abspath(os.fspath(path)))
        try:
            if os.path.commonpath((self.scope, candidate)) != self.scope:
                return False
        except ValueError:
            return False

        full_path = os.fspath(path)
        name = os.path.basename(full_path)
        if self.include_patterns and not any(
            fnmatch.fnmatch(full_path, pattern) or fnmatch.fnmatch(name, pattern)
            for pattern in self.include_patterns
        ):
            return False
        return not any(
            fnmatch.fnmatch(full_path, pattern) or fnmatch.fnmatch(name, pattern)
            for pattern in self.exclude_patterns
        )


class WindowsCollector(Collector):
    """Best-effort Windows process and directory activity collector.

    Process lifecycle is delivered by Windows WMI trace notifications. File
    activity uses ReadDirectoryChangesW and therefore has no actor PID and no
    file-open event. PID 0 represents that unknown actor in the shared model.
    """

    name = "windows-native"
    platform = "windows"

    def __init__(self, scope: str, *, include_command: bool = False,
                 include_paths: list[str] | None = None,
                 exclude_paths: list[str] | None = None,
                 recursive: bool = True):
        if platform.system() != "Windows":
            raise OSError("WindowsCollector is available only on Windows")
        self.scope = Path(scope).resolve(strict=True)
        if not self.scope.is_dir():
            raise ValueError("scope must be an existing directory")
        self.include_command = include_command
        self.include_paths = include_paths or []
        self.exclude_paths = exclude_paths or []
        self.recursive = recursive
        self._path_filter = _PathFilter(self.scope, self.include_paths, self.exclude_paths)
        self._stream: EventStream | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._sequence = 0
        self._process = None
        self._process_startup_ready = None
        self._process_startup_aborted = False
        self._process_mode = None
        self._process_snapshot = {}
        self._toolhelp = None
        self._threads: list[threading.Thread] = []
        self._directory_handle = None
        self._directory_event = None
        self._active_overlapped = None
        self._last_losses = {}
        self._last_loss_generation = 0
        self.health = {"process": "not_started", "filesystem": "not_started",
                       "dropped": 0, "errors": []}
        self._caps = self._initial_capabilities()

    def _initial_capabilities(self):
        supported = {
            EventType.PROCESS_STARTED, EventType.PROCESS_EXITED,
            EventType.FILE_CREATED, EventType.FILE_MODIFIED,
            EventType.FILE_DELETED, EventType.FILE_RENAMED,
        }
        reason = "collector has not started"
        return tuple(
            Capability(
                event_type,
                CollectorStatus.SUPPORTED if event_type in supported else CollectorStatus.UNSUPPORTED,
                reason if event_type in supported else
                "ReadDirectoryChangesW does not report file-open events",
                str(self.scope) if event_type.value.startswith("file") else "host process events",
                "WMI process trace" if event_type.value.startswith("process") else
                ("unknown" if event_type != EventType.FILE_OPENED else "not provided"),
            )
            for event_type in EventType
        )

    @property
    def capabilities(self):
        with self._lock:
            return self._caps

    @property
    def status(self):
        stream = self._stream
        return {
            **self.health,
            "errors": list(self.health["errors"]),
            "losses": stream.losses if stream is not None else self._last_losses,
            "loss_generation": stream.loss_generation if stream is not None else self._last_loss_generation,
            "recursive": self.recursive,
            "process_mode": self._process_mode,
        }

    def start(self, stream: EventStream) -> None:
        if self._stream is not None:
            raise RuntimeError("collector already started")
        self._stop = threading.Event()
        self._stream = stream
        self._last_losses = {}
        self._last_loss_generation = 0
        self._sequence = 0
        self._process_startup_aborted = False
        self._process_mode = None
        self.health = {"process": "not_started", "filesystem": "not_started",
                       "dropped": 0, "errors": []}
        self._caps = self._initial_capabilities()
        self._start_process_source()
        self._start_filesystem_source()

    def stop(self) -> None:
        if self._stream is None:
            return
        self._stop.set()
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
        self._cancel_directory_io()
        for thread in self._threads:
            thread.join(timeout=5)
        if self._process is not None and self._process.poll() is None:
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=2)
        if self._process is not None:
            for pipe in (self._process.stdout, self._process.stderr):
                if pipe is not None:
                    pipe.close()
        self._last_losses = self._stream.losses
        self._last_loss_generation = self._stream.loss_generation
        self._stream.close()
        self._stream = None
        self._close_directory_handles()
        self._threads.clear()

    def _set_status(self, event_types, status, reason, *, attribution=None):
        event_types = set(event_types)
        with self._lock:
            self._caps = tuple(
                Capability(cap.event_type, status, reason, cap.scope,
                           attribution or cap.attribution)
                if cap.event_type in event_types else cap
                for cap in self._caps
            )

    def _failure(self, source, exc):
        status = (CollectorStatus.PERMISSION_DENIED
                  if getattr(exc, "winerror", None) == ERROR_ACCESS_DENIED
                  else CollectorStatus.UNAVAILABLE)
        self.health[source] = status.value
        self.health["errors"].append(f"{source}: {exc}")
        affected = (
            (EventType.PROCESS_STARTED, EventType.PROCESS_EXITED)
            if source == "process" else
            (EventType.FILE_CREATED, EventType.FILE_MODIFIED,
             EventType.FILE_DELETED, EventType.FILE_RENAMED)
        )
        self._set_status(affected, status, str(exc))

    def _start_process_source(self):
        executable = os.environ.get("NATIVERELAY_POWERSHELL", "powershell.exe")
        script = _PROCESS_WATCHER_SCRIPT.replace(
            "__INCLUDE_COMMAND__", "$true" if self.include_command else "$false"
        )
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        try:
            self._process = subprocess.Popen(
                [executable, "-NoLogo", "-NoProfile", "-NonInteractive",
                 "-EncodedCommand", encoded],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            self._failure("process", exc)
            self._start_snapshot_process_source()
            return

        ready = threading.Event()
        self._process_startup_ready = ready
        reader = threading.Thread(target=self._read_process_events, args=(ready,),
                                  daemon=True, name="nativerelay-wmi-process")
        errors = threading.Thread(target=self._read_process_errors, args=(ready,),
                                  daemon=True, name="nativerelay-wmi-errors")
        self._threads.extend((reader, errors))
        reader.start()
        errors.start()
        deadline = time.monotonic() + 3
        while not ready.wait(timeout=0.1):
            if self._process.poll() is not None or time.monotonic() >= deadline:
                break
        if (not ready.is_set()
                or self.health["process"] in (
                    CollectorStatus.PERMISSION_DENIED.value,
                    CollectorStatus.UNAVAILABLE.value,
                )):
            message = "WMI process-trace subscription did not become ready; using Toolhelp fallback"
            self._process_startup_aborted = True
            if self.health["process"] == "not_started":
                self._failure("process", RuntimeError(message))
            elif self.health["process"] != CollectorStatus.PERMISSION_DENIED.value:
                self.health["errors"].append(message)
            self._stop_process()
            self._start_snapshot_process_source()
            return
        self.health["process"] = CollectorStatus.AVAILABLE.value
        self._process_mode = "wmi-trace"
        self._set_status(
            (EventType.PROCESS_STARTED, EventType.PROCESS_EXITED),
            CollectorStatus.AVAILABLE,
            "Windows WMI process start/stop trace subscription is active",
        )

    def _read_process_events(self, ready):
        assert self._process is not None and self._process.stdout is not None
        for line in self._process.stdout:
            try:
                record = json.loads(line)
                if record.get("kind") == "ready":
                    ready.set()
                else:
                    self._publish_process_record(record)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                self.health["errors"].append(f"malformed WMI process record: {exc}")
                self._record_loss("wmi_malformed_record", None)
        if (not self._stop.is_set() and not self._process_startup_aborted
                and self.health["process"] != CollectorStatus.PERMISSION_DENIED.value):
            code = self._process.poll()
            self._failure("process", RuntimeError(
                f"Windows WMI process-trace subscription ended (exit code {code})"
            ))
            ready.set()
        elif not ready.is_set():
            ready.set()

    def _read_process_errors(self, ready):
        if self._process is None or self._process.stderr is None:
            return
        for line in self._process.stderr:
            message = line.strip()
            if message and not self._stop.is_set():
                self.health["errors"].append(f"WMI: {message}")
                if "access denied" in message.lower():
                    self._failure("process", ctypes.WinError(ERROR_ACCESS_DENIED))
                    ready.set()

    def _stop_process(self):
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=2)

    def _start_snapshot_process_source(self):
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
            kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
            kernel32.Process32FirstW.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W),
            ]
            kernel32.Process32FirstW.restype = wintypes.BOOL
            kernel32.Process32NextW.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W),
            ]
            kernel32.Process32NextW.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            self._toolhelp = kernel32
            self._process_snapshot = self._take_process_snapshot()
        except (OSError, AttributeError) as exc:
            self._failure("process", exc)
            return

        self._process_mode = "toolhelp-poll"
        self.health["process"] = CollectorStatus.DEGRADED.value
        reason = "WMI trace unavailable; Toolhelp snapshots are polled every 200 ms and can miss short-lived processes"
        if self.include_command:
            self.health["errors"].append(
                "process command lines are unavailable in Toolhelp fallback mode"
            )
        self._set_status(
            (EventType.PROCESS_STARTED, EventType.PROCESS_EXITED),
            CollectorStatus.DEGRADED,
            reason,
            attribution="Toolhelp process snapshot polling",
        )
        thread = threading.Thread(target=self._poll_processes, daemon=True,
                                  name="nativerelay-toolhelp-process")
        self._threads.append(thread)
        thread.start()

    def _take_process_snapshot(self):
        snapshot_handle = self._toolhelp.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot_handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        processes = {}
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            if not self._toolhelp.Process32FirstW(snapshot_handle, ctypes.byref(entry)):
                error = ctypes.get_last_error()
                if error == ERROR_NO_MORE_FILES:
                    return processes
                raise ctypes.WinError(error)
            while True:
                pid = int(entry.th32ProcessID)
                if pid > 0:
                    processes[pid] = (
                        int(entry.th32ParentProcessID),
                        entry.szExeFile,
                    )
                entry.dwSize = ctypes.sizeof(entry)
                if not self._toolhelp.Process32NextW(snapshot_handle, ctypes.byref(entry)):
                    error = ctypes.get_last_error()
                    if error != ERROR_NO_MORE_FILES:
                        raise ctypes.WinError(error)
                    break
        finally:
            self._toolhelp.CloseHandle(snapshot_handle)
        return processes

    def _poll_processes(self):
        while not self._stop.wait(0.2):
            try:
                current = self._take_process_snapshot()
            except OSError as exc:
                self._failure("process", exc)
                return
            previous = self._process_snapshot
            for pid in previous.keys() - current.keys():
                parent_pid, process_name = previous[pid]
                self._publish_snapshot_event(
                    EventType.PROCESS_EXITED, pid, parent_pid, process_name
                )
            for pid, (parent_pid, process_name) in current.items():
                old = previous.get(pid)
                if old == (parent_pid, process_name):
                    continue
                if old is not None:
                    self._publish_snapshot_event(
                        EventType.PROCESS_EXITED, pid, old[0], old[1]
                    )
                self._publish_snapshot_event(
                    EventType.PROCESS_STARTED, pid, parent_pid, process_name
                )
            self._process_snapshot = current

    def _publish_snapshot_event(self, event_type, pid, parent_pid, process_name):
        self._publish(Event(
            event_type, self.platform, self.name,
            Process(pid=pid, parent_pid=parent_pid, executable=process_name or None),
            Resource("process", process_name) if process_name else None,
            metadata={"observedBy": "toolhelp_snapshot"},
        ))

    def _publish_process_record(self, record):
        event_kind = record["kind"]
        event_type = (EventType.PROCESS_STARTED if event_kind == "start"
                      else EventType.PROCESS_EXITED)
        pid = int(record["pid"])
        parent_pid = int(record["parent_pid"])
        process_name = str(record.get("process_name") or "")
        metadata = {}
        if "session_id" in record:
            metadata["sessionId"] = int(record["session_id"])
        if event_kind == "stop" and "exit_status" in record:
            metadata["exitStatus"] = int(record["exit_status"])
        command = None
        if event_kind == "start" and record.get("command_line"):
            command = self._split_command_line(record["command_line"])
        self._publish(Event(
            event_type, self.platform, self.name,
            Process(pid=pid, parent_pid=parent_pid,
                    executable=process_name or None, command=command),
            Resource("process", process_name) if process_name else None,
            metadata=metadata,
        ))

    def _start_filesystem_source(self):
        if os.name != "nt":
            self._failure("filesystem", OSError("Windows directory notifications require Windows"))
            return
        try:
            self._initialize_win32()
            handle = self._kernel32.CreateFileW(
                str(self.scope), FILE_LIST_DIRECTORY,
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                None, OPEN_EXISTING,
                FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OVERLAPPED,
                None,
            )
            invalid_handle = ctypes.c_void_p(-1).value
            if handle == invalid_handle:
                raise ctypes.WinError(ctypes.get_last_error())
            event_handle = self._kernel32.CreateEventW(None, True, False, None)
            if not event_handle:
                error = ctypes.get_last_error()
                self._kernel32.CloseHandle(handle)
                raise ctypes.WinError(error)
            self._directory_handle = handle
            self._directory_event = event_handle
        except (OSError, AttributeError) as exc:
            self._failure("filesystem", exc)
            return

        ready = threading.Event()
        thread = threading.Thread(target=self._directory_loop, args=(ready,), daemon=True,
                                  name="nativerelay-directory-watch")
        self._threads.append(thread)
        thread.start()
        if not ready.wait(timeout=5):
            self._failure(
                "filesystem",
                TimeoutError("ReadDirectoryChangesW did not arm before the startup deadline"),
            )
            return
        if self.health["filesystem"] == "not_started":
            self.health["filesystem"] = CollectorStatus.AVAILABLE.value
            self._set_status(
                (EventType.FILE_CREATED, EventType.FILE_MODIFIED,
                 EventType.FILE_DELETED, EventType.FILE_RENAMED),
                CollectorStatus.AVAILABLE,
                f"ReadDirectoryChangesW notifications for {self.scope}",
            )

    def _initialize_win32(self):
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        self._kernel32.CreateFileW.restype = wintypes.HANDLE
        self._kernel32.CreateEventW.argtypes = [
            wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR,
        ]
        self._kernel32.CreateEventW.restype = wintypes.HANDLE
        self._kernel32.ReadDirectoryChangesW.argtypes = [
            wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.BOOL,
            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(_OVERLAPPED), wintypes.LPVOID,
        ]
        self._kernel32.ReadDirectoryChangesW.restype = wintypes.BOOL
        self._kernel32.GetOverlappedResult.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(_OVERLAPPED),
            ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
        ]
        self._kernel32.GetOverlappedResult.restype = wintypes.BOOL
        self._kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self._kernel32.ResetEvent.argtypes = [wintypes.HANDLE]
        self._kernel32.ResetEvent.restype = wintypes.BOOL
        self._kernel32.CancelIoEx.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(_OVERLAPPED),
        ]
        self._kernel32.CancelIoEx.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL

    def _directory_loop(self, ready=None):
        buffer = ctypes.create_string_buffer(64 * 1024)
        overlapped = _OVERLAPPED()
        overlapped.event = self._directory_event
        notify_filter = (
            FILE_NOTIFY_CHANGE_FILE_NAME | FILE_NOTIFY_CHANGE_DIR_NAME
            | FILE_NOTIFY_CHANGE_LAST_WRITE | FILE_NOTIFY_CHANGE_CREATION
        )
        while not self._stop.is_set():
            if not self._kernel32.ResetEvent(self._directory_event):
                if not self._stop.is_set():
                    self._failure("filesystem", ctypes.WinError(ctypes.get_last_error()))
                if ready is not None:
                    ready.set()
                return
            bytes_returned = wintypes.DWORD()
            self._active_overlapped = overlapped
            success = self._kernel32.ReadDirectoryChangesW(
                self._directory_handle, buffer, len(buffer), self.recursive,
                notify_filter, ctypes.byref(bytes_returned),
                ctypes.byref(overlapped), None,
            )
            if not success:
                error = ctypes.get_last_error()
                if error == ERROR_NOTIFY_ENUM_DIR:
                    self._active_overlapped = None
                    self._record_loss("directory_notification_overflow", None)
                    if ready is not None:
                        ready.set()
                    continue
                if error != ERROR_IO_PENDING:
                    if not self._stop.is_set():
                        self._failure("filesystem", ctypes.WinError(error))
                    if ready is not None:
                        ready.set()
                    return
            if ready is not None:
                ready.set()

            while True:
                wait_result = self._kernel32.WaitForSingleObject(self._directory_event, 250)
                if wait_result != WAIT_TIMEOUT:
                    break
                if self._stop.is_set():
                    self._cancel_directory_io()
                    continue
            if wait_result == WAIT_FAILED:
                if not self._stop.is_set():
                    self._failure("filesystem", ctypes.WinError(ctypes.get_last_error()))
                return
            if wait_result != WAIT_OBJECT_0:
                self._failure("filesystem", RuntimeError(
                    f"unexpected directory notification wait result: {wait_result}"
                ))
                return

            if not self._kernel32.GetOverlappedResult(
                self._directory_handle, ctypes.byref(overlapped),
                ctypes.byref(bytes_returned), False,
            ):
                error = ctypes.get_last_error()
                if error == ERROR_OPERATION_ABORTED and self._stop.is_set():
                    return
                if error == ERROR_NOTIFY_ENUM_DIR:
                    self._record_loss("directory_notification_overflow", None)
                    self._active_overlapped = None
                    continue
                if error == ERROR_OPERATION_ABORTED and self._stop.is_set():
                    self._active_overlapped = None
                    return
                if not self._stop.is_set():
                    self._failure("filesystem", ctypes.WinError(error))
                return
            self._active_overlapped = None
            if bytes_returned.value == 0:
                self._record_loss("directory_notification_overflow", None)
                continue
            self._parse_notifications(buffer.raw[:bytes_returned.value])

    def _parse_notifications(self, data: bytes):
        offset = 0
        while offset < len(data):
            if len(data) - offset < 12:
                self._record_loss("directory_malformed_record", None)
                self.health["errors"].append("truncated directory notification record")
                return
            next_offset, action, name_length = struct.unpack_from("<III", data, offset)
            name_start = offset + 12
            name_end = name_start + name_length
            if (name_length == 0 or name_length % 2 or name_end > len(data)
                    or (next_offset and (next_offset < 12 or offset + next_offset > len(data)))):
                self._record_loss("directory_malformed_record", None)
                self.health["errors"].append("invalid directory notification record")
                return
            name = data[name_start:name_end].decode("utf-16le", errors="replace")
            path = Path(os.path.abspath(os.path.join(self.scope, name)))
            if self._path_filter.matches(path):
                mapped = {
                    FILE_ACTION_ADDED: (EventType.FILE_CREATED, {}),
                    FILE_ACTION_REMOVED: (EventType.FILE_DELETED, {}),
                    FILE_ACTION_MODIFIED: (EventType.FILE_MODIFIED, {}),
                    FILE_ACTION_RENAMED_OLD_NAME: (EventType.FILE_RENAMED, {"rename_from": True}),
                    FILE_ACTION_RENAMED_NEW_NAME: (EventType.FILE_RENAMED, {"rename_to": True}),
                }.get(action)
                if mapped is not None:
                    event_type, metadata = mapped
                    self._publish(Event(
                        event_type, self.platform, self.name, Process(0),
                        Resource("file", str(path)), metadata=metadata,
                    ))
            if next_offset == 0:
                return
            offset += next_offset

    def _publish(self, event):
        with self._lock:
            self._sequence += 1
            event = Event(
                event.type, event.platform, event.collector, event.process,
                resource=event.resource, timestamp=event.timestamp,
                metadata=event.metadata, evidence=event.evidence,
                id=event.id, sequence=self._sequence,
            )
            stream = self._stream
        if stream is not None and not stream.publish(event):
            self.health["dropped"] = stream.dropped

    def _record_loss(self, reason, count):
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
            stream = self._stream
        if stream is not None:
            stream.report_loss(self.name, reason, count=count, sequence=sequence)

    def _cancel_directory_io(self):
        if self._directory_handle and self._directory_event:
            overlapped = getattr(self, "_active_overlapped", None)
            if overlapped is not None:
                self._kernel32.CancelIoEx(
                    self._directory_handle, ctypes.byref(overlapped)
                )

    def _close_directory_handles(self):
        if getattr(self, "_directory_handle", None):
            self._kernel32.CloseHandle(self._directory_handle)
            self._directory_handle = None
        if getattr(self, "_directory_event", None):
            self._kernel32.CloseHandle(self._directory_event)
            self._directory_event = None

    @staticmethod
    def _split_command_line(command_line):
        if os.name != "nt":
            return (command_line,)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        shell32.CommandLineToArgvW.argtypes = [
            wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int),
        ]
        shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
        kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        kernel32.LocalFree.restype = wintypes.HLOCAL
        argc = ctypes.c_int()
        argv = shell32.CommandLineToArgvW(command_line, ctypes.byref(argc))
        if not argv:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return tuple(argv[index] for index in range(argc.value))
        finally:
            kernel32.LocalFree(argv)
