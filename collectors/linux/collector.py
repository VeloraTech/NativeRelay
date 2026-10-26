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
FAN_NONBLOCK = 0x00000002
FAN_MARK_ADD = 0x00000001
FAN_MARK_MOUNT = 0x00000010
FAN_MODIFY = 0x00000002
FAN_OPEN = 0x00000020
FAN_Q_OVERFLOW = 0x00004000
FANOTIFY_METADATA_VERSION = 3
FANOTIFY_METADATA_LEN = 24
FAN_EVENT_METADATA_FMT = "=IBBHQii"


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

    def _initial_capabilities(self):
        return tuple(Capability(t, CollectorStatus.SUPPORTED if t in (EventType.PROCESS_STARTED, EventType.PROCESS_EXITED, EventType.FILE_OPENED, EventType.FILE_MODIFIED) else CollectorStatus.UNSUPPORTED,
                 "collector has not started" if t in (EventType.PROCESS_STARTED, EventType.PROCESS_EXITED, EventType.FILE_OPENED, EventType.FILE_MODIFIED) else
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
        affected = (EventType.PROCESS_STARTED, EventType.PROCESS_EXITED) if subsystem == "process" else (EventType.FILE_OPENED, EventType.FILE_MODIFIED)
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
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            init = libc.fanotify_init
            init.argtypes = [ctypes.c_uint, ctypes.c_uint]
            init.restype = ctypes.c_int
            fd = init(FAN_CLOEXEC | FAN_CLASS_NOTIF | FAN_NONBLOCK, os.O_RDONLY | os.O_LARGEFILE if hasattr(os, "O_LARGEFILE") else os.O_RDONLY)
            if fd < 0: self._raise_errno()
            self._fanotify_fd = fd
            mark = libc.fanotify_mark
            mark.argtypes = [ctypes.c_int, ctypes.c_uint, ctypes.c_uint64, ctypes.c_int, ctypes.c_char_p]
            mark.restype = ctypes.c_int
            mountpoint = self._mountpoint(self.scope)
            if mark(fd, FAN_MARK_ADD | FAN_MARK_MOUNT, FAN_OPEN | FAN_MODIFY, -100, os.fsencode(mountpoint)) < 0:
                os.close(fd); self._raise_errno()
            self._sockets.append(fd)
            self.health["filesystem"] = "available"
            self._set_status((EventType.FILE_OPENED, EventType.FILE_MODIFIED), CollectorStatus.AVAILABLE, f"fanotify mount notifications filtered to {self.scope}")
            thread = threading.Thread(target=self._fanotify_loop, daemon=True, name="nativerelay-fanotify")
            thread.start(); self._threads.append(thread)
        except OSError as exc:
            self._failure("filesystem", exc)

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
                elif event_fd >= 0:
                    self._handle_file_event(mask, event_fd, pid)
                offset += length
            except (ValueError, struct.error) as exc:
                self.health["errors"].append(f"malformed fanotify event: {exc}")
                self._record_native_loss("fanotify_malformed_event", count=None)
                break
        if offset < len(data) and len(data) - offset < FANOTIFY_METADATA_LEN:
            self.health["errors"].append("truncated fanotify metadata")
            self._record_native_loss("fanotify_truncated_metadata", count=None)
    def _handle_file_event(self, mask, fd, pid):
        try:
            path = Path(os.readlink(f"/proc/self/fd/{fd}")).resolve(strict=False)
            # Apply path filtering
            if not self._path_filter.matches(path):
                return
            if mask & FAN_OPEN:
                self._publish(Event(EventType.FILE_OPENED, "linux", self.name,
                                    self._process_info(pid, None), Resource("file", str(path))))
            if mask & FAN_MODIFY:
                self._publish(Event(EventType.FILE_MODIFIED, "linux", self.name,
                                    self._process_info(pid, None), Resource("file", str(path))))
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
