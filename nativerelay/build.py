"""Tiny offline PEP 517 wheel builder; runtime remains standard-library only."""
from pathlib import Path
import base64
import csv
import hashlib
import io
from zipfile import ZIP_DEFLATED, ZipFile

NAME = "nativerelay"
VERSION = "0.3.0"

def get_requires_for_build_wheel(config_settings=None):
    return []

def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    output = Path(wheel_directory)
    output.mkdir(parents=True, exist_ok=True)
    filename = f"{NAME}-{VERSION}-py3-none-any.whl"
    dist = f"{NAME}-{VERSION}.dist-info"
    files = []
    for package in (Path("nativerelay"), Path("collectors")):
        for path in package.rglob("*.py"):
            if "__pycache__" not in path.parts:
                files.append(path)
    record = []
    with ZipFile(output / filename, "w", ZIP_DEFLATED) as wheel:
        for path in files:
            content = path.read_bytes()
            wheel.writestr(path.as_posix(), content)
            record.append(_record_row(path.as_posix(), content))
        metadata = f"{dist}/METADATA"
        content = f"Metadata-Version: 2.1\nName: nativerelay\nVersion: {VERSION}\nRequires-Python: >=3.11\nSummary: Local-first normalized native OS telemetry\n\n".encode()
        wheel.writestr(metadata, content); record.append(_record_row(metadata, content))
        wheel_file = f"{dist}/WHEEL"
        content = b"Wheel-Version: 1.0\nGenerator: nativerelay.build\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        wheel.writestr(wheel_file, content); record.append(_record_row(wheel_file, content))
        entrypoints = f"{dist}/entry_points.txt"
        content = b"[console_scripts]\nnativerelay = nativerelay.cli:main\n"
        wheel.writestr(entrypoints, content); record.append(_record_row(entrypoints, content))
        record_path = f"{dist}/RECORD"
        record.append((record_path, "", ""))
        stream = io.StringIO(newline="")
        csv.writer(stream, lineterminator="\n").writerows(record)
        wheel.writestr(record_path, stream.getvalue())
    return filename

def _record_row(path, content):
    digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode("ascii")
    return (path, f"sha256={digest}", str(len(content)))
