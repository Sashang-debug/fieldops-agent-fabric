"""Identity principals and scope constants for tool authorization."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

SCOPE_CRM_READ = "crm:read"
SCOPE_TICKET_READ = "ticket:read"
SCOPE_TICKET_WRITE = "ticket:write"

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_PRINCIPALS_PATH = _REPO_ROOT / "data" / "principals.json"


@dataclass(frozen=True)
class Principal:
    id: str
    display_name: str
    scopes: frozenset[str]


class UnknownPrincipalError(KeyError):
    """Raised when a principal id is not present in the registry."""


def _parse_principal(raw: dict) -> Principal:
    return Principal(
        id=raw["id"],
        display_name=raw["display_name"],
        scopes=frozenset(raw["scopes"]),
    )


@lru_cache
def load_principals(path: str | None = None) -> dict[str, Principal]:
    principals_path = Path(path) if path else _DEFAULT_PRINCIPALS_PATH
    raw_list = json.loads(principals_path.read_text(encoding="utf-8"))
    return {entry["id"]: _parse_principal(entry) for entry in raw_list}


def get_principal(principal_id: str, path: str | None = None) -> Principal:
    registry = load_principals(path)
    try:
        return registry[principal_id]
    except KeyError as exc:
        raise UnknownPrincipalError(principal_id) from exc
