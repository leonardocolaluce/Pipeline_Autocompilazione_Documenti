import os
import re
from pathlib import Path


WINDOWS_DRIVE_RE = re.compile(r"^(?P<drive>[A-Za-z]):[\\/](?P<rest>.*)$")


def coerce_path(value: str | Path, current_os: str | None = None) -> Path:
    if isinstance(value, Path):
        return value

    text = str(value).strip()
    os_name = current_os or os.name
    match = WINDOWS_DRIVE_RE.match(text)
    if match:
        if os_name == "nt":
            return Path(text)
        drive = match.group("drive").lower()
        rest = match.group("rest").replace("\\", "/").lstrip("/")
        return Path("/mnt") / drive / rest
    return Path(text)
