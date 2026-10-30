"""Runtime identity of the running engine.

Reports which ops-engine version is running (as ``importlib.metadata`` reports
it at runtime) and the SHA-256 of the exact config bytes the config loader last
read. A value that cannot be determined is the named value ``"unknown"``; this
module never raises.
"""

from dataclasses import dataclass
from importlib import metadata

from .config_loader import last_config_sha256

UNKNOWN = "unknown"
_DISTRIBUTION = "ops-engine"


@dataclass(frozen=True)
class RuntimeIdentity:
    version: str
    config_sha256: str


def _version() -> str:
    try:
        value = metadata.version(_DISTRIBUTION)
    except Exception:
        return UNKNOWN
    return value if isinstance(value, str) and value else UNKNOWN


def _config_sha256() -> str:
    try:
        value = last_config_sha256()
    except Exception:
        return UNKNOWN
    return value if isinstance(value, str) and value else UNKNOWN


def runtime_identity() -> RuntimeIdentity:
    """Return the running version and the hash of the config bytes last read.

    Each field falls back to ``"unknown"`` independently; this never raises.
    """
    return RuntimeIdentity(version=_version(), config_sha256=_config_sha256())
