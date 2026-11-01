"""Windows process and file event collectors using ETW and ReadDirectoryChangesW.

Process lifecycle uses ETW Microsoft-Windows-Kernel-Process provider.
File events use ReadDirectoryChangesW for directory monitoring.
No polling is used; permission and queue errors are surfaced in capabilities/health.

ETW process provider: Microsoft-Windows-Kernel-Process ({22FB2CD6-0E7B-422B-A0C7-2FAD1FD0E716})
  Events: Process Start (1), Process End (2), Thread Start (3), Thread End (4), Image Load (5)
  Requires: Admin / SeDebugPrivilege / ETW session

File monitoring: ReadDirectoryChangesW
  Events: File creation, deletion, modification, rename
  Requires: Standard user (for accessible directories)
  Limitations: No process attribution, no open events, single directory only (non-recursive by default)
"""
from __future__ import annotations
import ctypes
import ctypes.wintypes
import errno
import fnmatch
import os
import platform
import struct
import threading
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from nativerelay.collector import Collector
from nativerelay.model import Capability, CollectorStatus, Event, EventType, Process, Resource
from nativerelay.stream import EventStream

if platform.system() == "Windows":
    import ctypes.wintypes
    from ctypes import wintypes

# ETW Constants
WNODE_FLAG_TRACED_GUID = 0x00020000
EVENT_TRACE_REAL_TIME_MODE = 0x00000100
ERROR_SUCCESS = 0
ERROR_ACCESS_DENIED = 5
ERROR_IO_PENDING = 997
ERROR_OPERATION_ABORTED = 995
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF

FILE_NOTIFY_CHANGE_FILE_NAME = 0x00000001
FILE_NOTIFY_CHANGE_DIR_NAME = 0x00000002
FILE_NOTIFY_CHANGE_LAST_WRITE = 0x00000010
FILE_NOTIFY_CHANGE_CREATION = 0x00000040

FILE_ACTION_ADDED = 0x00000001
FILE_ACTION_REMOVED = 0x00000002
FILE_ACTION_MODIFIED = 0x00000003
FILE_ACTION_RENAMED_OLD_NAME = 0x00000004
FILE_ACTION_RENAMED_NEW_NAME = 0x00000005

FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OVERLAPPED = 0x40000000
FILE_LIST_DIRECTORY = 0x0001
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3

KERNEL_PROCESS_PROVIDER_GUID = ctypes.GUID(
    0x22FB2CD6, 0x0E7B, 0x422B,
    (0xA0, 0xC7, 0x2F, 0xAD, 0x1F, 0xD0, 0xE7, 0x16)
)

KERNEL_PROCESS_EVENT_START = 1
KERNEL_PROCESS_EVENT_END = 2
KERNEL_PROCESS_EVENT_THREAD_START = 3
KERNEL_PROCESS_EVENT_THREAD_END = 4
KERNEL_PROCESS_EVENT_IMAGE_LOAD = 5

kernel32 = ctypes.windll.kernel32
advapi32 = ctypes.windll.advapi32

StartTrace = advapi32.StartTraceW
StartTrace.argtypes = [ctypes.POINTER(ctypes.wintypes.ULONG), ctypes.wintypes.LPWSTR, ctypes.POINTER(EVENT_TRACE_PROPERTIES)]
StartTrace.restype = ctypes.wintypes.ULONG

ControlTrace = advapi32.ControlTraceW
ControlTrace.argtypes = [ctypes.wintypes.ULONG, ctypes.wintypes.LPWSTR, ctypes.POINTER(EVENT_TRACE_PROPERTIES), ctypes.wintypes.ULONG]
ControlTrace.restype = ctypes.wintypes.ULONG

EnableTraceEx2 = advapi32.EnableTraceEx2
EnableTraceEx2.argtypes = [
    ctypes.wintypes.ULONG, ctypes.POINTER(ctypes.GUID), ctypes.wintypes.ULONG,
    ctypes.wintypes.UCHAR, ctypes.wintypes.ULONGLONG, ctypes.wintypes.ULONGLONG,
    ctypes.wintypes.ULONG, ctypes.c_void_p,
]
EnableTraceEx2.restype = ctypes.wintypes.ULONG

OpenTrace = advapi32.OpenTraceW
OpenTrace.argtypes = [ctypes.POINTER(EVENT_TRACE_LOGFILE)]
OpenTrace.restype = ctypes.wintypes.ULONG

ProcessTrace = advapi32.ProcessTrace
ProcessTrace.argtypes = [ctypes.POINTER(ctypes.wintypes.ULONG), ctypes.wintypes.ULONG, ctypes.c_void_p, ctypes.c_void_p]
ProcessTrace.restype = ctypes.wintypes.ULONG

CloseTrace = advapi32.CloseTrace
CloseTrace.argtypes = [ctypes.wintypes.ULONG]
CloseTrace.restype = ctypes.wintypes.ULONG


ReadDirectoryChangesW = ctypes.windll.kernel32.ReadDirectoryChangesW
ReadDirectoryChangesW.argtypes = [
    ctypes.wintypes.HANDLE, ctypes.c_void_p, ctypes.wintypes.DWORD,
    ctypes.wintypes.BOOL, ctypes.wintypes.DWORD, ctypes.POINTER(ctypes.wintypes.DWORD),
    ctypes.wintypes.LPOVERLAPPED, ctypes.wintypes.LPOVERLAPPED_COMPLETION_ROUTINE,
]
ReadDirectoryChangesW.restype = ctypes.wintypes.BOOL

CreateFileW = ctypes.windll.kernel32.CreateFileW
CreateFileW.argtypes = [ctypes.wintypes.LPCWSTR, ctypes.wintypes.DWORD, ctypes.wintypes.DWORD,
                        ctypes.c_void_p, ctypes.wintypes.DWORD, ctypes.wintypes.DWORD, ctypes.wintypes.HANDLE]
CreateFileW.restype = ctypes.wintypes.HANDLE

CloseHandle = ctypes.windll.kernel32.CloseHandle
CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
CloseHandle.restype = ctypes.wintypes.BOOL

GetOverlappedResult = ctypes.windll.kernel32.GetOverlappedResult
GetOverlappedResult.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.LPOVERLAPPED,
                                ctypes.POINTER(ctypes.wintypes.DWORD), ctypes.wintypes.BOOL]
GetOverlappedResult.restype = ctypes.wintypes.BOOL

CreateEventW = ctypes.windll.kernel32.CreateEventW
CreateEventW.argtypes = [ctypes.c_void_p, ctypes.wintypes.BOOL, ctypes.wintypes.BOOL, ctypes.wintypes.LPCWSTR]
CreateEventW.restype = ctypes.wintypes.HANDLE

WaitForSingleObject = ctypes.windll.kernel32.WaitForSingleObject
WaitForSingleObject.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD]
WaitForSingleObject.restype = ctypes.wintypes.DWORD

CancelIoEx = ctypes.windll.kernel32.CancelIoEx
CancelIoEx.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.LPOVERLAPPED]
CancelIoEx.restype = ctypes.wintypes.BOOL

GetCurrentProcessId = ctypes.windll.kernel32.GetCurrentProcessId
GetCurrentProcessId.restype = ctypes.wintypes.DWORD

ERROR_SUCCESS = 0
ERROR_ACCESS_DENIED = 5
ERROR_IO_PENDING = 997
ERROR_OPERATION_ABORTED = 995
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF

FILE_NOTIFY_CHANGE_FILE_NAME = 0x00000001
FILE_NOTIFY_CHANGE_DIR_NAME = 0x00000002
FILE_NOTIFY_CHANGE_LAST_WRITE = 0x00000010
FILE_NOTIFY_CHANGE_CREATION = 0x00000040

FILE_ACTION_ADDED = 0x00000001
FILE_ACTION_REMOVED = 0x00000002
FILE_ACTION_MODIFIED = 0x00000003
FILE_ACTION_RENAMED_OLD_NAME = 0x00000004
FILE_ACTION_RENAMED_NEW_NAME = 0x00000005

FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OVERLAPPED = 0x40000000
FILE_LIST_DIRECTORY = 0x0001
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3

WNODE_FLAG_TRACED_GUID = 0x00020000
EVENT_TRACE_REAL_TIME_MODE = 0x00000100
ERROR_SUCCESS = 0
ERROR_ACCESS_DENIED = 5
ERROR_IO_PENDING = 997
ERROR_OPERATION_ABORTED = 995
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF

KERNEL_PROCESS_PROVIDER_GUID = ctypes.GUID(
    0x22FB2CD6, 0x0E7B, 0x422B,
    (0xA0, 0xC7, 0x2F, 0xAD, 0x1F, 0xD0, 0xE7, 0x16)
)

KERNEL_PROCESS_EVENT_START = 1
KERNEL_PROCESS_EVENT_END = 2
KERNEL_PROCESS_EVENT_THREAD_START = 3
KERNEL_PROCESS_EVENT_THREAD_END = 4
KERNEL_PROCESS_EVENT_IMAGE_LOAD = 5


# ETW Structures
class EVENT_TRACE_HEADER(ctypes.Structure):
    _fields_ = [
        ('Size', ctypes.wintypes.USHORT),
        ('FieldTypeFlags', ctypes.wintypes.USHORT),
        ('Version', ctypes.wintypes.UCHAR),
        ('Type', ctypes.wintypes.UCHAR),
        ('Level', ctypes.wintypes.UCHAR),
        ('Channel', ctypes.wintypes.UCHAR),
        ('ThreadId', ctypes.wintypes.ULONG),
        ('ProcessId', ctypes.wintypes.ULONG),
        ('TimeStamp', ctypes.wintypes.ULONGLONG),
        ('Guid', ctypes.GUID),
        ('ClientContext', ctypes.wintypes.ULONG),
        ('Flags', ctypes.wintypes.ULONG),
    ]

class EVENT_TRACE_PROPERTIES(ctypes.Structure):
    _fields_ = [
        ('Wnode', ctypes.wintypes.WNODE_HEADER),
        ('BufferSize', ctypes.wintypes.ULONG),
        ('MinimumBuffers', ctypes.wintypes.ULONG),
        ('MaximumBuffers', ctypes.wintypes.ULONG),
        ('MaximumFileSize', ctypes.wintypes.ULONG),
        ('LogFileMode', ctypes.wintypes.ULONG),
        ('FlushTimer', ctypes.wintypes.ULONG),
        ('EnableFlags', ctypes.wintypes.ULONG),
        ('AgeLimit', ctypes.wintypes.ULONG),
        ('NumberOfBuffers', ctypes.wintypes.ULONG),
        ('FreeBuffers', ctypes.wintypes.ULONG),
        ('EventsLost', ctypes.wintypes.ULONG),
        ('BuffersWritten', ctypes.wintypes.ULONG),
        ('LogBuffersLost', ctypes.wintypes.ULONG),
        ('RealTimeBuffersLost', ctypes.wintypes.ULONG),
        ('LoggerNameOffset', ctypes.wintypes.ULONG),
        ('LogFileNameOffset', ctypes.wintypes.ULONG),
    ]

class EVENT_TRACE_LOGFILE(ctypes.Structure):
    _fields_ = [
        ('LogFileName', ctypes.wintypes.LPWSTR),
        ('LoggerName', ctypes.wintypes.LPWSTR),
        ('CurrentTime', ctypes.wintypes.ULONGLONG),
        ('BuffersRead', ctypes.wintypes.ULONG),
        ('LogFileMode', ctypes.wintypes.ULONG),
        ('CurrentBufferNumber', ctypes.wintypes.ULONG),
        ('BuffersWritten', ctypes.wintypes.ULONG),
        ('BufferCallback', ctypes.c_void_p),
        ('BufferSize', ctypes.wintypes.ULONG),
        ('Filled', ctypes.wintypes.ULONG),
        ('EventsLost', ctypes.wintypes.ULONG),
        ('LoggerThreadId', ctypes.wintypes.ULONG),
        ('LogFileHeader', ctypes.POINTER(EVENT_TRACE_PROPERTIES)),
        ('ProcessTraceMode', ctypes.wintypes.ULONG),
        ('EventCallback', ctypes.c_void_p),
        ('EventRecordCallback', ctypes.c_void_p),
        ('Context', ctypes.c_void_p),
    ]

class FILE_NOTIFY_INFORMATION(ctypes.Structure):
    _fields_ = [
        ('NextEntryOffset', ctypes.wintypes.DWORD),
        ('Action', ctypes.wintypes.DWORD),
        ('FileNameLength', ctypes.wintypes.DWORD),
        ('FileName', ctypes.wintypes.WCHAR * 1),
    ]

class PROCESS_TRACE_EVENT(ctypes.Structure):
    _fields_ = [
        ('ProcessId', ctypes.wintypes.ULONG),
        ('ParentId', ctypes.wintypes.ULONG),
        ('SessionId', ctypes.wintypes.ULONG),
        ('ExitStatus', ctypes.wintypes.ULONG),
        ('ImageFileName', ctypes.wintypes.WCHAR * 260),
        ('CommandLine', ctypes.wintypes.WCHAR * 1024),
    ]

kernel32 = ctypes.windll.kernel32
advapi32 = ctypes.windll.advapi32

StartTrace = advapi32.StartTraceW
StartTrace.argtypes = [ctypes.POINTER(ctypes.wintypes.ULONG), ctypes.wintypes.LPWSTR, ctypes.POINTER(EVENT_TRACE_PROPERTIES)]
StartTrace.restype = ctypes.wintypes.ULONG

ControlTrace = advapi32.ControlTraceW
ControlTrace.argtypes = [ctypes.wintypes.ULONG, ctypes.wintypes.LPWSTR, ctypes.POINTER(EVENT_TRACE_PROPERTIES), ctypes.wintypes.ULONG]
ControlTrace.restype = ctypes.wintypes.ULONG

EnableTraceEx2 = advapi32.EnableTraceEx2
EnableTraceEx2.argtypes = [
    ctypes.wintypes.ULONG, ctypes.POINTER(ctypes.GUID), ctypes.wintypes.ULONG,
    ctypes.wintypes.UCHAR, ctypes.wintypes.ULONGLONG, ctypes.wintypes.ULONGLONG,
    ctypes.wintypes.ULONG, ctypes.c_void_p,
]
EnableTraceEx2.restype = ctypes.wintypes.ULONG

OpenTrace = advapi32.OpenTraceW
OpenTrace.argtypes = [ctypes.POINTER(EVENT_TRACE_LOGFILE)]
OpenTrace.restype = ctypes.wintypes.ULONG

ProcessTrace = advapi32.ProcessTrace
ProcessTrace.argtypes = [ctypes.POINTER(ctypes.wintypes.ULONG), ctypes.wintypes.ULONG, ctypes.c_void_p, ctypes.c_void_p]
ProcessTrace.restype = ctypes.wintypes.ULONG

CloseTrace = advapi32.CloseTrace
CloseTrace.argtypes = [ctypes.wintypes.ULONG]
CloseTrace.restype = ctypes.wintypes.ULONG
ReadDirectoryChangesW = ctypes.windll.kernel32.ReadDirectoryChangesW
ReadDirectoryChangesW.argtypes = [
    ctypes.wintypes.HANDLE, ctypes.c_void_p, ctypes.wintypes.DWORD,
    ctypes.wintypes.BOOL, ctypes.wintypes.DWORD, ctypes.POINTER(ctypes.wintypes.DWORD),
    ctypes.wintypes.LPOVERLAPPED, ctypes.wintypes.LPOVERLAPPED_COMPLETION_ROUTINE,
]
ReadDirectoryChangesW.restype = ctypes.wintypes.BOOL

CreateFileW = ctypes.windll.kernel32.CreateFileW
CreateFileW.argtypes = [ctypes.wintypes.LPCWSTR, ctypes.wintypes.DWORD, ctypes.wintypes.DWORD,
                        ctypes.c_void_p, ctypes.wintypes.DWORD, ctypes.wintypes.DWORD, ctypes.wintypes.HANDLE]
CreateFileW.restype = ctypes.wintypes.HANDLE

CloseHandle = ctypes.windll.kernel32.CloseHandle
CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
CloseHandle.restype = ctypes.wintypes.BOOL

GetOverlappedResult = ctypes.windll.kernel32.GetOverlappedResult
GetOverlappedResult.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.LPOVERLAPPED,
                                ctypes.POINTER(ctypes.wintypes.DWORD), ctypes.wintypes.BOOL]
GetOverlappedResult.restype = ctypes.wintypes.BOOL

CreateEventW = ctypes.windll.kernel32.CreateEventW
CreateEventW.argtypes = [ctypes.c_void_p, ctypes.wintypes.BOOL, ctypes.wintypes.BOOL, ctypes.wintypes.LPCWSTR]
CreateEventW.restype = ctypes.wintypes.HANDLE

WaitForSingleObject = ctypes.windll.kernel32.WaitForSingleObject
WaitForSingleObject.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD]
WaitForSingleObject.restype = ctypes.wintypes.DWORD

CancelIoEx = ctypes.windll.kernel32.CancelIoEx
CancelIoEx.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.LPOVERLAPPED]
CancelIoEx.restype = ctypes.wintypes.BOOL

GetCurrentProcessId = ctypes.windll.kernel32.GetCurrentProcessId
GetCurrentProcessId.restype = ctypes.wintypes.DWORD

ERROR_SUCCESS = 0
ERROR_ACCESS_DENIED = 5
ERROR_IO_PENDING = 997
ERROR_OPERATION_ABORTED = 995
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF

FILE_NOTIFY_CHANGE_FILE_NAME = 0x00000001
FILE_NOTIFY_CHANGE_DIR_NAME = 0x00000002
FILE_NOTIFY_CHANGE_LAST_WRITE = 0x00000010
FILE_NOTIFY_CHANGE_CREATION = 0x00000040

FILE_ACTION_ADDED = 0x00000001
FILE_ACTION_REMOVED = 0x00000002
FILE_ACTION_MODIFIED = 0x00000003
FILE_ACTION_RENAMED_OLD_NAME = 0x00000004
FILE_ACTION_RENAMED_NEW_NAME = 0x00000005

FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OVERLAPPED = 0x40000000
FILE_LIST_DIRECTORY = 0x0001
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3

WNODE_FLAG_TRACED_GUID = 0x00020000
EVENT_TRACE_REAL_TIME_MODE = 0x00000100
ERROR_SUCCESS = 0
ERROR_ACCESS_DENIED = 5
ERROR_IO_PENDING = 997
ERROR_OPERATION_ABORTED = 995
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF

KERNEL_PROCESS_PROVIDER_GUID = ctypes.GUID(
    0x22FB2CD6, 0x0E7B, 0x422B,
    (0xA0, 0xC7, 0x2F, 0xAD, 0x1F, 0xD0, 0xE7, 0x16)
)

KERNEL_PROCESS_EVENT_START = 1
KERNEL_PROCESS_EVENT_END = 2
KERNEL_PROCESS_EVENT_THREAD_START = 3
KERNEL_PROCESS_EVENT_THREAD_END = 4
KERNEL_PROCESS_EVENT_IMAGE_LOAD = 5


# Path filter
class _PathFilter:
    " \Path filter supporting include/exclude patterns with fnmatch semantics.\\n
class _WindowsProcessCollector:
    """ETW-based process lifecycle collector."""
    def start(self, stream: EventStream) -> None:
        if self.running:
            return
        self.running = True
        self.stream = stream
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
    
    def stop(self) -> None:
        self.running = False
        if self.logger_handle:
            props = EVENT_TRACE_PROPERTIES()
            props.Wnode.BufferSize = ctypes.sizeof(EVENT_TRACE_PROPERTIES)
            ControlTrace(self.logger_handle, self.session_name, ctypes.byref(props), 2)
            self.logger_handle = 0
        if self.trace_handle:
            CloseTrace(self.trace_handle)
            self.trace_handle = 0
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5.0)
    
    def _run(self) -> None:
        props = EVENT_TRACE_PROPERTIES()
        props.Wnode.BufferSize = ctypes.sizeof(EVENT_TRACE_PROPERTIES)
        props.Wnode.Flags = WNODE_FLAG_TRACED_GUID
        props.Wnode.Guid = KERNEL_PROCESS_PROVIDER_GUID
        props.BufferSize = 64
        props.MinimumBuffers = 10
        props.MaximumBuffers = 100
        props.LogFileMode = EVENT_TRACE_REAL_TIME_MODE
        props.EnableFlags = 0
        props.FlushTimer = 1
        
        logger_name = self.session_name
        logger_name_w = ctypes.create_unicode_buffer(logger_name)
        
        status = StartTrace(ctypes.byref(ctypes.wintypes.ULONG()), logger_name_w, ctypes.byref(props))
        if status != ERROR_SUCCESS:
            self._emit_error(f'StartTrace failed: {status}')
            return
        self.logger_handle = ctypes.wintypes.ULONG(props.Wnode.HistoricalContext)
        
        status = EnableTraceEx2(
            self.logger_handle,
            ctypes.byref(KERNEL_PROCESS_PROVIDER_GUID),
            1,
            0,
            0xFFFFFFFFFFFFFFFF,
            0,
            0,
            None
        )
        if status != ERROR_SUCCESS:
            self._emit_error(f'EnableTraceEx2 failed: {status}')
            return
        
        logfile = EVENT_TRACE_LOGFILE()
        logfile.LoggerName = logger_name_w
        logfile.ProcessTraceMode = EVENT_TRACE_REAL_TIME_MODE
        logfile.EventCallback = self._event_callback
        
        self.trace_handle = OpenTrace(ctypes.byref(logfile))
        if self.trace_handle == 0xFFFFFFFF:
            self._emit_error('OpenTrace failed')
            return
        
        handles = (ctypes.wintypes.ULONG * 1)(self.trace_handle)
        while self.running:
            status = ProcessTrace(handles, 1, None, None)
            if status != ERROR_SUCCESS:
                if status != WAIT_TIMEOUT:
                    self._emit_error(f'ProcessTrace failed: {status}')
                break
    
    def _event_callback(self, event_record) -> None:
        header = ctypes.cast(event_record, ctypes.POINTER(EVENT_TRACE_HEADER)).contents
        if header.Guid != KERNEL_PROCESS_PROVIDER_GUID:
            return
        
        event_type = header.Type
        pid = header.ProcessId
        
        data_ptr = ctypes.c_void_p(ctypes.addressof(header) + header.Size)
        
        if event_type == KERNEL_PROCESS_EVENT_START:
            self._handle_process_start(data_ptr, header.TimeStamp)
        elif event_type == KERNEL_PROCESS_EVENT_END:
            self._handle_process_end(data_ptr, header.TimeStamp)
    
    def _handle_process_start(self, data_ptr, timestamp) -> None:
        event = ctypes.cast(data_ptr, ctypes.POINTER(PROCESS_TRACE_EVENT)).contents
        pid = event.ProcessId
        ppid = event.ParentId
        image_name = event.ImageFileName.rstrip('\x00')
        cmdline = event.CommandLine.rstrip('\x00') if self.capture_arguments else ''
        
        process = Process(pid=pid, ppid=ppid, executable=image_name, arguments=cmdline)
        resource = Resource(path=image_name, resource_type='process')
        
        evt = Event(
            type=EventType.PROCESS_STARTED,
            timestamp=datetime.fromtimestamp(timestamp / 1e7 - 11644473600, tz=timezone.utc),
            process=process,
            resource=resource,
            metadata={'session_id': event.SessionId}
        )
        self.stream.write(evt)
    
    def _handle_process_end(self, data_ptr, timestamp) -> None:
        event = ctypes.cast(data_ptr, ctypes.POINTER(PROCESS_TRACE_EVENT)).contents
        pid = event.ProcessId
        exit_status = event.ExitStatus
        
        process = Process(pid=pid, ppid=0, executable='')
        resource = Resource(path='', resource_type='process')
        
        evt = Event(
            type=EventType.PROCESS_EXITED,
            timestamp=datetime.fromtimestamp(timestamp / 1e7 - 11644473600, tz=timezone.utc),
            process=process,
            resource=resource,
            metadata={'exit_status': exit_status}
        )
        self.stream.write(evt)
    
    def _emit_error(self, msg: str) -> None:
        self.stream.write(Event(
            type=EventType.NATIVERELAY_STATUS,
            timestamp=datetime.now(timezone.utc),
            process=Process(pid=0, ppid=0, executable=''),
            resource=Resource(path='', resource_type='system'),
            metadata={'level': 'error', 'message': msg}
        ))
    
    def get_status(self) -> dict:
        return {
            'running': self.running,
            'loss_count': self.loss_count,
            'loss_generation': self.loss_generation,
            'events_dropped': self.events_dropped,
        }


class _WindowsFileCollector:
    " \ReadDirectoryChangesW-based file event collector.\\n    def start(self, stream: EventStream) -> None:
        if self.running:
            return
        self.running = True
        self.stream = stream
        
        flags = FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OVERLAPPED
        share = FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
        
        self.dir_handle = CreateFileW(
            str(self.scope),
            FILE_LIST_DIRECTORY,
            share,
            None,
            OPEN_EXISTING,
            flags,
            None
        )
        
        if self.dir_handle == ctypes.wintypes.HANDLE(-1).value:
            error = ctypes.get_last_error()
            self._emit_error(f'CreateFileW failed: {error}')
            return
        
        self.event = CreateEventW(None, True, False, None)
        if not self.event:
            self._emit_error('CreateEventW failed')
            return
        
        self.overlapped = ctypes.wintypes.OVERLAPPED()
        self.overlapped.hEvent = self.event
        
        self.buffer = ctypes.create_string_buffer(self.buffer_size)
        
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
    
    def stop(self) -> None:
        self.running = False
        if self.dir_handle:
            CancelIoEx(self.dir_handle, ctypes.byref(self.overlapped))
            CloseHandle(self.dir_handle)
            self.dir_handle = None
        if self.event:
            CloseHandle(self.event)
            self.event = None
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5.0)
    
    def _run(self) -> None:
        notify_filter = (FILE_NOTIFY_CHANGE_FILE_NAME |
                        FILE_NOTIFY_CHANGE_DIR_NAME |
                        FILE_NOTIFY_CHANGE_LAST_WRITE |
                        FILE_NOTIFY_CHANGE_CREATION)
        
        while self.running:
            bytes_returned = ctypes.wintypes.DWORD()
            success = ReadDirectoryChangesW(
                self.dir_handle,
                self.buffer,
                self.buffer_size,
                self.recursive,
                notify_filter,
                ctypes.byref(bytes_returned),
                ctypes.byref(self.overlapped),
                None
            )
            
            if not success:
                error = ctypes.get_last_error()
                if error == ERROR_IO_PENDING:
                    wait_result = WaitForSingleObject(self.event, 1000)
                    if wait_result == WAIT_TIMEOUT:
                        continue
                    elif wait_result == WAIT_FAILED:
                        self._emit_error('WaitForSingleObject failed')
                        break
                    
                    success = GetOverlappedResult(self.dir_handle, ctypes.byref(self.overlapped),
                                                ctypes.byref(bytes_returned), True)
                    if not success:
                        error = ctypes.get_last_error()
                        if error == ERROR_OPERATION_ABORTED:
                            break
                        self._emit_error(f'GetOverlappedResult failed: {error}')
                        continue
                elif error == ERROR_OPERATION_ABORTED:
                    break
                else:
                    self._emit_error(f'ReadDirectoryChangesW failed: {error}')
                    continue
            
            self._process_notifications(bytes_returned.value)
            
            ctypes.windll.kernel32.ResetEvent(self.event)
    
    def _process_notifications(self, bytes_returned: int) -> None:
        offset = 0
        while offset < bytes_returned:
            notify = ctypes.cast(
                ctypes.addressof(self.buffer) + offset,
                ctypes.POINTER(FILE_NOTIFY_INFORMATION)
            ).contents
            
            file_name = ctypes.wstring_at(
                ctypes.addressof(notify.FileName),
                notify.FileNameLength // 2
            )
            full_path = self.scope / file_name
            
            if self.path_filter.matches(full_path):
                event_type = self._map_action(notify.Action)
                if event_type:
                    resource = Resource(path=str(full_path), resource_type='file')
                    process = Process(pid=GetCurrentProcessId(), ppid=0, executable='')
                    
                    evt = Event(
                        type=event_type,
                        timestamp=datetime.now(timezone.utc),
                        process=process,
                        resource=resource,
                        metadata={}
                    )
                    self.stream.write(evt)
            
            if notify.NextEntryOffset == 0:
                break
            offset += notify.NextEntryOffset
    
    def _map_action(self, action: int) -> Optional[EventType]:
        mapping = {
            FILE_ACTION_ADDED: EventType.FILE_CREATED,
            FILE_ACTION_REMOVED: EventType.FILE_DELETED,
            FILE_ACTION_MODIFIED: EventType.FILE_MODIFIED,
            FILE_ACTION_RENAMED_OLD_NAME: EventType.FILE_RENAMED,
            FILE_ACTION_RENAMED_NEW_NAME: EventType.FILE_RENAMED,
        }
        return mapping.get(action)
    
    def _emit_error(self, msg: str) -> None:
        self.stream.write(Event(
            type=EventType.NATIVERELAY_STATUS,
            timestamp=datetime.now(timezone.utc),
            process=Process(pid=0, ppid=0, executable=''),
            resource=Resource(path='', resource_type='system'),
            metadata={'level': 'error', 'message': msg}
        ))
    
    def get_status(self) -> dict:
        return {
            'running': self.running,
            'loss_count': self.loss_count,
            'loss_generation': self.loss_generation,
            'events_dropped': self.events_dropped,
        }


class WindowsCollector(Collector):
    " \Windows collector using ETW for process events and ReadDirectoryChangesW for file events.\\n    def _can_access_etw(self) -> bool:
        try:
            kernel32 = ctypes.windll.kernel32
            advapi32 = ctypes.windll.advapi32
            props = EVENT_TRACE_PROPERTIES()
            props.Wnode.BufferSize = ctypes.sizeof(EVENT_TRACE_PROPERTIES)
            props.Wnode.Flags = WNODE_FLAG_TRACED_GUID
            props.Wnode.Guid = KERNEL_PROCESS_PROVIDER_GUID
            props.BufferSize = 64
            props.MinimumBuffers = 10
            props.MaximumBuffers = 100
            props.LogFileMode = EVENT_TRACE_REAL_TIME_MODE
            
            session_name = f'NativeRelay-Test-{uuid.uuid4().hex[:8]}'
            logger_name = ctypes.create_unicode_buffer(session_name)
            
            status = StartTrace(ctypes.byref(ctypes.wintypes.ULONG()), logger_name, ctypes.byref(props))
            if status == ERROR_SUCCESS:
                ControlTrace(ctypes.wintypes.ULONG(props.Wnode.HistoricalContext), session_name,
                           ctypes.byref(props), 2)
                return True
            elif status == ERROR_ACCESS_DENIED:
                return False
        except Exception:
            pass
        return False
    
    @property
    def status(self) -> dict:
        return {
            'process': self.process_collector.get_status(),
            'file': self.file_collector.get_status(),
            'running': self.running,
            'errors': [],
        }
    
    @property
    def etw_mode(self) -> str:
        return 'kernel-process' if self._can_access_etw() else 'unavailable'

__all__ = ['WindowsCollector']
