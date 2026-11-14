"""Provider-neutral release identity; never contains a stream URL or credentials."""

import re
from collections.abc import Mapping
from typing import Any


def release_key(stream: Mapping[str, Any]) -> dict:
    result = {}
    value = str(stream.get("infoHash") or "")
    if re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", value):
        result["info_hash"] = value.lower()
        if type(stream.get("fileIdx")) is int and stream["fileIdx"] >= 0:
            result["file_index"] = stream["fileIdx"]
    filename = (stream.get("behaviorHints") or {}).get("filename")
    if (
        isinstance(filename, str)
        and filename
        and len(filename) <= 512
        and "://" not in filename
        and not any(ord(c) < 32 for c in filename)
    ):
        result["filename"] = filename.replace("\\", "/").rsplit("/", 1)[-1]
    return result


def same_release(stream: Mapping[str, Any], expected: dict) -> bool:
    candidate = release_key(stream)
    if expected.get("info_hash") and candidate.get("info_hash") == expected["info_hash"]:
        if "file_index" in expected:
            return candidate.get("file_index") == expected["file_index"]
        return bool(expected.get("filename") and candidate.get("filename") == expected["filename"])
    return bool(expected.get("filename") and candidate.get("filename") == expected["filename"])
