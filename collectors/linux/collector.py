"""Linux process connector and fanotify collectors (Linux only, stdlib only).

Process lifecycle uses NETLINK_CONNECTOR/CN_PROC. File events use a fanotify
mount mark, filtered to one requested directory in userspace. No polling is
used; permission and queue errors are surfaced in capabilities/health.
"""
from __future__ import annotations
import ctypes
import errno
from dataclasses import replace
import fnmatch
import os
import platform
import re
import select
import socket
import struct
import threading
import time
from pathlib import Path
from typing import Optional

from nativerelay.collector import Collector
from nativerelay.model import Capability, CollectorStatus, Event, EventType, Process, Resource
from nativerelay.stream import EventStream

NETLINK_CONNECTOR = 11
CN_IDX_PROC = 1
CN_VAL_PROC = 1
NLMSG_DONE = 3
NLMSG_OVERRUN = 4
PROC_EVENT_FORK = 0x00000001
PROC_EVENT_EXIT = 0x80000000
PROC_CN_MCAST_LISTEN = 1
FAN_CLOEXEC = 0x00000001
FAN_CLASS_NOTIF = 0x00000000
FAN_CLASS_CONTENT = 0x00000004
FAN_CLASS_PRE_CONTENT = 0x00000008
FAN_NONBLOCK = 0x00000002
FAN_MARK_ADD = 0x00000001
FAN_MARK_MOUNT = 0x00000010
FAN_MARK_FILESYSTEM = 0x00000100
FAN_MARK_ONLYDIR = 0x00000020
FAN_MODIFY = 0x00000002
FAN_OPEN = 0x00000020
FAN_CREATE = 0x00000100
FAN_DELETE = 0x00000200
FAN_DELETE_SELF = 0x00000400
FAN_MOVE = 0x00000800
FAN_RENAME = 0x00000800
FAN_MOVED_FROM = 0x00001000
FAN_MOVED_TO = 0x00002000
FAN_Q_OVERFLOW = 0x00004000
FAN_REPORT_PID = 0x00008000
FAN_REPORT_FID = 0x00010000
FAN_REPORT_DFID_NAME = 0x00020000
FANOTIFY_METADATA_VERSION = 3
FANOTIFY_METADATA_LEN = 24
FAN_EVENT_METADATA_FMT = "=IBBHQii"

# fanotify event info types
FAN_EVENT_INFO_TYPE_FID = 1
FAN_EVENT_INFO_TYPE_DFID = 2
FAN_EVENT_INFO_TYPE_DFID_NAME = 3


class _PathFilter:
    """Path filter supporting include/exclude patterns with fnmatch semantics."""

    def __init__(self, scope: Path, include_patterns: Optional[list[str]] = None,
                 exclude_patterns: Optional[list[str]] = None):
        self.scope = scope
        self.include_patterns = include_patterns or []
        self.exclude_patterns = exclude_patterns or []

    def matches(self, path: Path) -> bool:
        """Check if path matches the filter criteria."""
        # Path must be within scope
        try:
            path.relative_to(self.scope)
        except ValueError:
            return False

        # If include patterns specified, path must match at least one
        if self.include_patterns:
            matched = False
            for pattern in self.include_patterns:
                if fnmatch.fnmatch(str(path), pattern) or fnmatch.fnmatch(path.name, pattern):
                    matched = True
                    break
            if not matched:
                return False

        # Path must not match any exclude pattern
        for pattern in self.exclude_patterns:
            if fnmatch.fnmatch(str(path), pattern) or fnmatch.fnmatch(path.name, pattern):
                return False

        return True


class LinuxCollector(Collector):
    name = "linux-kernel"

    def __init__(self, scope: str, *, include_command: bool = False,
                 include_paths: Optional[list[str]] = None,
                 exclude_paths: Optional[list[str]] = None):
        if platform.system() != "Linux":
            raise OSError("LinuxCollector is available only on Linux")
        self.scope = Path(scope).resolve(strict=True)
        if not self.scope.is_dir():
            raise ValueError("scope must be an existing directory")
        self.include_command = include_command
        # Path filtering for file events
        self._path_filter = _PathFilter(self.scope, include_paths, exclude_paths)
        self._stream = None
        self._sockets = []
        self._threads = []
        self._stop = threading.Event()
        self._publish_lock = threading.Lock()
        self._sequence = 0
        self.health = {"process": "not_started", "filesystem": "not_started", "dropped": 0, "errors": []}
        self._caps = self._initial_capabilities()
        self._fanotify_advanced = False

    def _initial_capabilities(self):
        return tuple(Capability(t, CollectorStatus.SUPPORTED if t in (
            EventType.PROCESS_STARTED, EventType.PROCESS_EXITED,
            EventType.FILE_OPENED, EventType.FILE_MODIFIED,
            EventType.FILE_CREATED, EventType.FILE_DELETED, EventType.FILE_RENAMED
        ) else CollectorStatus.UNSUPPORTED,
                 "collector has not started" if t in (
            EventType.PROCESS_STARTED, EventType.PROCESS_EXITED,
            EventType.FILE_OPENED, EventType.FILE_MODIFIED,
            EventType.FILE_CREATED, EventType.FILE_DELETED, EventType.FILE_RENAMED
        ) else
                 "This collector does not infer namespace changes from file content notifications",
                 "host process table" if t.value.startswith("process") else str(self.scope),
                 "kernel PID/TGID" if t.value.startswith("process") else "fanotify event PID") for t in EventType)

    @property
    def capabilities(self): return self._caps

    @property
    def status(self):
        losses = self._stream.losses if self._stream is not None else getattr(self, "_last_stream_losses", {})
        generation = self._stream.loss_generation if self._stream is not None else getattr(self, "_last_loss_generation", 0)
        return {**self.health, "errors": list(self.health["errors"]), "losses": losses, "loss_generation": generation}

    def start(self, stream: EventStream):
        if self._stream is not None: raise RuntimeError("collector already started")
        self._stop = threading.Event()
        self._publish_lock = threading.Lock()
        self._sequence = 0
        self.health = {"process": "not_started", "filesystem": "not_started", "dropped": 0, "errors": []}
        self._caps = self._initial_capabilities()
        self._stream = stream
        self._last_stream_losses = {}
        self._start_process()
        self._start_filesystem()

    def stop(self):
        if self._stream is None: return
        self._stop.set()
        for sock in self._sockets:
            try:
                if isinstance(sock, int): os.close(sock)
                else: sock.close()
            except OSError: pass
        for thread in self._threads: thread.join(timeout=2)
        self._last_stream_losses = self._stream.losses
        self._last_loss_generation = self._stream.loss_generation
        self._stream.close()
        self._sockets.clear(); self._threads.clear(); self._stream = None

    def _failure(self, subsystem, exc):
        status = CollectorStatus.PERMISSION_DENIED if isinstance(exc, PermissionError) or getattr(exc, "errno", None) in (errno.EPERM, errno.EACCES) else CollectorStatus.UNAVAILABLE
        self.health[subsystem] = status.value
        self.health["errors"].append(f"{subsystem}: {exc}")
        affected = (EventType.PROCESS_STARTED, EventType.PROCESS_EXITED) if subsystem == "process" else (
            EventType.FILE_OPENED, EventType.FILE_MODIFIED, EventType.FILE_CREATED, EventType.FILE_DELETED, EventType.FILE_RENAMED
        )
        self._caps = tuple(Capability(c.event_type, status if c.event_type in affected else c.status,
                         str(exc) if c.event_type in affected else c.reason, c.scope, c.attribution) for c in self._caps)

    def _start_process(self):
        try:
            sock = socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM, NETLINK_CONNECTOR)
            sock.bind((os.getpid(), CN_IDX_PROC))
            payload = struct.pack("=IIIIHHI", CN_IDX_PROC, CN_VAL_PROC, 0, 0, 4, 0, PROC_CN_MCAST_LISTEN)
            header = struct.pack("=IHHII", 16 + len(payload), NLMSG_DONE, 0, 1, os.getpid())
            sock.send(header + payload)
            self._sockets.append(sock)
            # CN_PROC may silently ignore subscribers outside the initial PID/user namespaces.
            # Require its connector acknowledgement before claiming process coverage.
            deadline = time.monotonic() + 2.0
            while self.health["process"] != "available":
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("CN_PROC sent no subscription acknowledgement; process events may be disabled or this PID namespace may not be the kernel's initial namespace")
                sock.settimeout(remaining)
                packet = sock.recv(65536)
                try: self._parse_proc_packet(packet)
                except (ValueError, struct.error, OSError) as exc:
                    self.health["errors"].append(f"malformed process event during startup: {exc}")
                    self._record_native_loss("cn_proc_malformed_packet", count=None)
                if self.health["process"] in (CollectorStatus.PERMISSION_DENIED.value, CollectorStatus.UNAVAILABLE.value): break
            if self.health["process"] != "available":
                sock.close()
                self._sockets.remove(sock)
                return
            sock.settimeout(.5)
            thread = threading.Thread(target=self._process_loop, args=(sock,), daemon=True, name="nativerelay-proc")
            thread.start(); self._threads.append(thread)
        except OSError as exc:
            self._failure("process", exc)

    def _process_loop(self, sock):
        while not self._stop.is_set():
            try: packet = sock.recv(65536)
            except socket.timeout: continue
            except OSError:
                if not self._stop.is_set(): self._failure("process", OSError("process connector socket closed"))
                break
            try: self._parse_proc_packet(packet)
            except (ValueError, struct.error, OSError) as exc:
                self.health["errors"].append(f"malformed process event: {exc}")
                self._record_native_loss("cn_proc_malformed_packet", count=None)

    def _parse_proc_packet(self, packet):
        # Netlink header (16), connector header (20), then proc_event data.
        offset = 0
        while offset < len(packet):
            if len(packet) - offset < 16: raise ValueError("truncated netlink header")
            nl_len, nl_type, _, _, _ = struct.unpack_from("=IHHII", packet, offset)
            if nl_len < 16 or offset + nl_len > len(packet): raise ValueError("invalid netlink message length")
            if nl_type == NLMSG_OVERRUN:
                self._record_native_loss("cn_proc_netlink_overrun", count=None)
                offset += (nl_len + 3) & ~3
                continue
            if len(packet) - offset < 36 or nl_len < 36: raise ValueError("truncated netlink connector message")
            cn = offset + 16
            idx, val = struct.unpack_from("=II", packet, cn)
            payload_len = struct.unpack_from("=H", packet, cn + 16)[0]
            if payload_len > nl_len - 36: raise ValueError("connector payload exceeds netlink message")
            if idx == CN_IDX_PROC and val == CN_VAL_PROC and payload_len >= 16:
                body = cn + 20
                what = struct.unpack_from("=I", packet, body)[0]
                data = body + 16
                if what == 0 and payload_len >= 20:
                    error = struct.unpack_from("=i", packet, data)[0]
                    if error:
                        code = abs(error)
                        self._failure("process", OSError(code, os.strerror(code)))
                    else:
                        self.health["process"] = "available"
                        self._set_status((EventType.PROCESS_STARTED, EventType.PROCESS_EXITED), CollectorStatus.AVAILABLE, "Linux NETLINK_CONNECTOR/CN_PROC notifications acknowledged")
                elif what == PROC_EVENT_FORK and payload_len >= 32:
                    _, parent_tgid, child_pid, child_tgid = struct.unpack_from("=IIII", packet, data)
                    if child_pid == child_tgid:
                        self._emit_process(EventType.PROCESS_STARTED, child_tgid, parent_tgid)
                elif what == PROC_EVENT_EXIT and payload_len >= 32:
                    pid, tgid, code, sig = struct.unpack_from("=IIII", packet, data)
                    if pid == tgid:
                        self._emit_process(EventType.PROCESS_EXITED, tgid, None,
                                           {"exitStatus": code, "exitSignal": sig, "leaderThreadExit": True})
            offset += nl_len
    def _process_info(self, pid, parent_pid):
        proc = Path("/proc") / str(pid)
        try:
            stat = (proc / "stat").read_text()
            # comm may contain spaces and ')' characters; fields after final ')' are stable.
            tail = stat[stat.rfind(")") + 2:].split()
            parent_pid = int(tail[1]) if parent_pid is None and len(tail) > 1 else parent_pid
        except (OSError, ValueError, IndexError): pass
        try: executable = os.readlink(proc / "exe")
        except OSError: executable = None
        command = None
        if self.include_command:
            try: command = tuple((proc / "cmdline").read_bytes().decode(errors="replace").rstrip("\0").split("\0"))
            except OSError: pass
        return Process(pid=pid, parent_pid=parent_pid, executable=executable, command=command)

    def _emit_process(self, typ, pid, ppid, metadata=None):
        self._publish(Event(typ, "linux", self.name, self._process_info(pid, ppid), metadata=metadata or {}))

    def _start_filesystem(self):
        """Initialize fanotify with fallback for filesystems without file-handle support."""
        libc = ctypes.CDLL(None, use_errno=True)
        init = libc.fanotify_init
        init.argtypes = [ctypes.c_uint, ctypes.c_uint]
        init.restype = ctypes.c_int
        mark = libc.fanotify_mark
        mark.argtypes = [ctypes.c_int, ctypes.c_uint, ctypes.c_uint64, ctypes.c_int, ctypes.c_char_p]
        mark.restype = ctypes.c_int

        open_flags = os.O_RDONLY | os.O_LARGEFILE if hasattr(os, "O_LARGEFILE") else os.O_RDONLY

        # Try advanced mode first: file-handle reporting for create/delete/rename
        # Requires FAN_CLASS_CONTENT (not NOTIF) for FAN_REPORT_FID/DFID_NAME
        advanced_fd = init(
            FAN_CLOEXEC | FAN_CLASS_CONTENT | FAN_NONBLOCK | FAN_REPORT_PID | FAN_REPORT_FID | FAN_REPORT_DFID_NAME,
            open_flags
        )
        if advanced_fd >= 0:
            mask = FAN_OPEN | FAN_MODIFY | FAN_CREATE | FAN_DELETE | FAN_MOVED_FROM | FAN_MOVED_TO
            if mark(advanced_fd, FAN_MARK_ADD | FAN_MARK_FILESYSTEM, mask, -100, os.fsencode(str(self.scope))) >= 0:
                self._fanotify_fd = advanced_fd
                self._sockets.append(advanced_fd)
                self._fanotify_advanced = True
                self.health["filesystem"] = "available"
                self._set_status(
                    (EventType.FILE_OPENED, EventType.FILE_MODIFIED, EventType.FILE_CREATED, EventType.FILE_DELETED, EventType.FILE_RENAMED),
                    CollectorStatus.AVAILABLE,
                    f"fanotify filesystem notifications filtered to {self.scope}"
                )
                thread = threading.Thread(target=self._fanotify_loop, daemon=True, name="nativerelay-fanotify")
                thread.start()
                self._threads.append(thread)
                return
            # Mark failed, close and try fallback
            os.close(advanced_fd)

        # Fallback 1: basic mode with FAN_REPORT_FID (required for FAN_MARK_MOUNT)
        # Uses FAN_CLASS_CONTENT for FAN_REPORT_FID, with/without FAN_REPORT_PID
        fid_flag_combinations = [
            (FAN_CLOEXEC | FAN_CLASS_CONTENT | FAN_NONBLOCK | FAN_REPORT_PID | FAN_REPORT_FID, "with FAN_REPORT_PID+FID"),
            (FAN_CLOEXEC | FAN_CLASS_CONTENT | FAN_NONBLOCK | FAN_REPORT_FID, "with FAN_REPORT_FID only"),
        ]

        fid_fd = -1
        fid_mode_desc = ""
        for flags, desc in fid_flag_combinations:
            fd = init(flags, open_flags)
            if fd >= 0:
                fid_fd = fd
                fid_mode_desc = desc
                break
            errno_val = ctypes.get_errno()
            self.health["errors"].append(f"fanotify_init {desc} failed: {os.strerror(errno_val)}")

        if fid_fd >= 0:
            mask = FAN_OPEN | FAN_MODIFY
            mountpoint = self._mountpoint(self.scope)
            if mark(fid_fd, FAN_MARK_ADD | FAN_MARK_MOUNT, mask, -100, os.fsencode(mountpoint)) >= 0:
                self._fanotify_fd = fid_fd
                self._sockets.append(fid_fd)
                self._fanotify_advanced = False
                self.health["filesystem"] = "available"
                self._set_status(
                    (EventType.FILE_OPENED, EventType.FILE_MODIFIED),
                    CollectorStatus.AVAILABLE,
                    f"fanotify mount notifications filtered to {self.scope} (create/delete/rename unsupported on this filesystem)"
                )
                self._set_status(
                    (EventType.FILE_CREATED, EventType.FILE_DELETED, EventType.FILE_RENAMED),
                    CollectorStatus.UNAVAILABLE,
                    "filesystem does not support file-handle reporting (FAN_REPORT_DFID_NAME)"
                )
                thread = threading.Thread(target=self._fanotify_loop, daemon=True, name="nativerelay-fanotify")
                thread.start()
                self._threads.append(thread)
                return
            # Mark failed, close and try next fallback
            os.close(fid_fd)

        # Fallback 2: basic mode without FAN_REPORT_FID (no mount/filesystem marking)
        # Uses FAN_CLASS_NOTIF with FAN_MARK_ONLYDIR on the specific directory
        # This only watches the exact directory, not recursive
        basic_flag_combinations = [
            (FAN_CLOEXEC | FAN_CLASS_NOTIF | FAN_NONBLOCK | FAN_REPORT_PID, "with FAN_REPORT_PID (dir-only)"),
            (FAN_CLOEXEC | FAN_CLASS_NOTIF | FAN_NONBLOCK, "minimal (dir-only, no FAN_REPORT_PID)"),
        ]

        basic_fd = -1
        basic_mode_desc = ""
        for flags, desc in basic_flag_combinations:
            fd = init(flags, open_flags)
            if fd >= 0:
                basic_fd = fd
                basic_mode_desc = desc
                break
            errno_val = ctypes.get_errno()
            self.health["errors"].append(f"fanotify_init {desc} failed: {os.strerror(errno_val)}")

        if basic_fd < 0:
            self._failure("filesystem", OSError(ctypes.get_errno(), "fanotify_init failed: all flag combinations failed"))
            return

        mask = FAN_OPEN | FAN_MODIFY
        # Mark the specific directory only (not recursive, no mount marking)
        if mark(basic_fd, FAN_MARK_ADD | FAN_MARK_ONLYDIR, mask, -100, os.fsencode(str(self.scope))) < 0:
            os.close(basic_fd)
            self._failure("filesystem", OSError(ctypes.get_errno(), "fanotify_mark failed (directory-only)"))
            return

        self._fanotify_fd = basic_fd
        self._sockets.append(basic_fd)
        self._fanotify_advanced = False
        self.health["filesystem"] = "available"
        self._set_status(
            (EventType.FILE_OPENED, EventType.FILE_MODIFIED),
            CollectorStatus.AVAILABLE,
            f"fanotify directory-only notifications for {self.scope} (non-recursive, create/delete/rename unsupported)"
        )
        self._set_status(
            (EventType.FILE_CREATED, EventType.FILE_DELETED, EventType.FILE_RENAMED),
            CollectorStatus.UNAVAILABLE,
            "directory-only mode does not support create/delete/rename events"
        )
        thread = threading.Thread(target=self._fanotify_loop, daemon=True, name="nativerelay-fanotify")
        thread.start()
        self._threads.append(thread)

    @staticmethod
    def _raise_errno():
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))

    @staticmethod
    def _mountpoint(path):
        best = Path("/")
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            fields = line.split()
            if len(fields) > 4:
                decoded = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), fields[4])
                try: mount = Path(decoded).resolve()
                except OSError: continue
                if path == mount or mount in path.parents:
                    if len(str(mount)) > len(str(best)): best = mount
        return best

    def _fanotify_loop(self):
        poller = select.poll()
        poller.register(self._fanotify_fd, select.POLLIN | select.POLLERR | select.POLLHUP)
        while not self._stop.is_set():
            ready = poller.poll(500)
            if not ready: continue
            try: data = os.read(self._fanotify_fd, 65536)
            except BlockingIOError: continue
            except OSError:
                if not self._stop.is_set(): self._failure("filesystem", OSError("fanotify descriptor closed"))
                return
            self._parse_fanotify_data(data)

    def _parse_fanotify_data(self, data):
        offset = 0
        while offset + FANOTIFY_METADATA_LEN <= len(data):
            try:
                length, version, _, metadata_len, mask, event_fd, pid = struct.unpack_from(FAN_EVENT_METADATA_FMT, data, offset)
                if length < FANOTIFY_METADATA_LEN or offset + length > len(data): raise ValueError("invalid fanotify event length")
                if version != FANOTIFY_METADATA_VERSION or metadata_len < FANOTIFY_METADATA_LEN or metadata_len > length: raise ValueError("unsupported fanotify metadata version")
                if mask & FAN_Q_OVERFLOW:
                    self._record_native_loss("fanotify_queue_overflow", count=None)
                elif self._fanotify_advanced:
                    # Advanced mode: file-handle events have event_fd == -1 (FAN_NOFD)
                    if event_fd >= 0:
                        self._handle_file_event(mask, event_fd, pid)
                    else:
                        self._handle_file_handle_event(mask, pid, data, offset, length, metadata_len)
                else:
                    # Basic mode: only FAN_OPEN and FAN_MODIFY with event_fd >= 0
                    if event_fd >= 0 and (mask & (FAN_OPEN | FAN_MODIFY)):
                        self._handle_file_event(mask, event_fd, pid)
                    elif event_fd < 0:
                        # Unexpected file-handle event in basic mode; report loss
                        self._record_native_loss("fanotify_unexpected_handle_event", count=None)
                offset += length
            except (ValueError, struct.error) as exc:
                self.health["errors"].append(f"malformed fanotify event: {exc}")
                self._record_native_loss("fanotify_malformed_event", count=None)
                break
        if offset < len(data) and len(data) - offset < FANOTIFY_METADATA_LEN:
            self.health["errors"].append("truncated fanotify metadata")
            self._record_native_loss("fanotify_truncated_metadata", count=None)

    def _parse_fan_event_info(self, data, offset, metadata_len):
        """Parse extended fanotify event info (FID, DFID, DFID_NAME)."""
        fid = None
        dfid = None
        dfid_name = None
        info_offset = offset + FANOTIFY_METADATA_LEN
        while info_offset < offset + metadata_len:
            if info_offset + 4 > offset + metadata_len:
                break
            info_type, info_len = struct.unpack_from("=BH", data, info_offset)
            info_offset += 4
            if info_offset + info_len > offset + metadata_len:
                break
            info_data = data[info_offset:info_offset + info_len]
            info_offset += info_len
            if info_type == FAN_EVENT_INFO_TYPE_FID:
                fid = info_data
            elif info_type == FAN_EVENT_INFO_TYPE_DFID:
                dfid = info_data
            elif info_type == FAN_EVENT_INFO_TYPE_DFID_NAME:
                # Structure: file_handle + filename (null-terminated)
                if len(info_data) > 0:
                    name_end = info_data.rfind(b'\0')
                    if name_end >= 0:
                        dfid = info_data[:name_end]
                        dfid_name = info_data[name_end + 1:].decode(errors='replace')
                    else:
                        dfid = info_data
                        dfid_name = info_data.decode(errors='replace')
        return fid, dfid, dfid_name

    def _open_by_handle(self, mount_fd, handle_bytes):
        """Open a file by handle using open_by_handle_at."""
        libc = ctypes.CDLL(None, use_errno=True)
        open_by_handle = libc.open_by_handle_at
        open_by_handle.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        open_by_handle.restype = ctypes.c_int
        handle = ctypes.create_string_buffer(handle_bytes)
        return open_by_handle(mount_fd, handle, os.O_RDONLY | os.O_PATH)

    def _handle_file_handle_event(self, mask, pid, data, offset, length, metadata_len):
        """Handle file handle-based events (create/delete/rename) using open_by_handle_at."""
        fid, dfid, dfid_name = self._parse_fan_event_info(data, offset, metadata_len)
        try:
            mount_fd = os.open(self._mountpoint(self.scope), os.O_RDONLY | os.O_DIRECTORY)
            try:
                # For rename events, we get two events: FAN_MOVED_FROM and FAN_MOVED_TO
                # FAN_MOVED_FROM gives source DFID + name, FAN_MOVED_TO gives dest DFID + name
                # For create/delete: DFID + name gives the target
                if dfid and dfid_name:
                    fd = self._open_by_handle(mount_fd, dfid + dfid_name.encode())
                elif fid:
                    fd = self._open_by_handle(mount_fd, fid)
                else:
                    return
                if fd >= 0:
                    try:
                        path = Path(os.readlink(f"/proc/self/fd/{fd}")).resolve(strict=False)
                        if self._path_filter.matches(path):
                            self._publish_file_event(mask, pid, path)
                    finally:
                        os.close(fd)
            except (OSError, ValueError) as exc:
                self.health["errors"].append(f"file handle event resolution: {exc}")
                self._record_native_loss("fanotify_path_resolution", count=1)
            finally:
                os.close(mount_fd)
        except (OSError, ValueError) as exc:
            self.health["errors"].append(f"file handle event setup: {exc}")
            self._record_native_loss("fanotify_path_resolution", count=1)
    def _publish_file_event(self, mask, pid, path):
        """Publish a file event based on the mask."""
        if mask & FAN_OPEN:
            self._publish(Event(EventType.FILE_OPENED, "linux", self.name,
                                self._process_info(pid, None), Resource("file", str(path))))
        if mask & FAN_MODIFY:
            self._publish(Event(EventType.FILE_MODIFIED, "linux", self.name,
                                self._process_info(pid, None), Resource("file", str(path))))
        if mask & FAN_CREATE:
            self._publish(Event(EventType.FILE_CREATED, "linux", self.name,
                                self._process_info(pid, None), Resource("file", str(path))))
        if mask & FAN_DELETE:
            self._publish(Event(EventType.FILE_DELETED, "linux", self.name,
                                self._process_info(pid, None), Resource("file", str(path))))
        if mask & (FAN_MOVED_FROM | FAN_MOVED_TO | FAN_RENAME):
            # For rename, we emit a single event with both paths in metadata
            # The actual correlation of from/to is done by the consumer
            metadata = {}
            if mask & FAN_MOVED_FROM:
                metadata["rename_from"] = True
            if mask & FAN_MOVED_TO:
                metadata["rename_to"] = True
            self._publish(Event(EventType.FILE_RENAMED, "linux", self.name,
                                self._process_info(pid, None), Resource("file", str(path)), metadata=metadata))

    def _handle_file_event(self, mask, fd, pid):
        try:
            path = Path(os.readlink(f"/proc/self/fd/{fd}")).resolve(strict=False)
            # Apply path filtering
            if not self._path_filter.matches(path):
                return
            self._publish_file_event(mask, pid, path)
        except (OSError, ValueError) as exc:
            self.health["errors"].append(f"file event resolution: {exc}")
            lost = int(bool(mask & FAN_OPEN)) + int(bool(mask & FAN_MODIFY))
            if lost:
                self._record_native_loss("fanotify_path_resolution", count=lost)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def _record_native_loss(self, reason, *, count):
        """Record detected native loss and reserve a collector sequence marker."""
        with self._publish_lock:
            sequence = self._sequence
            self._sequence += 1
            if self._stream is not None:
                self._stream.report_loss(self.name, reason, count=count, sequence=sequence)

    def _publish(self, event):
        with self._publish_lock:
            sequenced = replace(event, sequence=self._sequence)
            self._sequence += 1
            if self._stream is not None and not self._stream.publish(sequenced):
                self.health["dropped"] += 1

    def _set_status(self, event_types, status, reason):
        self._caps = tuple(Capability(c.event_type, status if c.event_type in event_types else c.status,
                         reason if c.event_type in event_types else c.reason, c.scope, c.attribution) for c in self._caps)
