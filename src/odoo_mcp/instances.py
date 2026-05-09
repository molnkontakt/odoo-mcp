"""Configuration for Odoo instances (prod + dev).

Credentials come from environment variables, typically populated by a
secret-manager (Phase, Vault, AWS SSM, etc.) or a `.env` file (gitignored).
"""

import os
from dataclasses import dataclass
from typing import Literal

Instance = Literal["prod", "dev"]


@dataclass(frozen=True)
class OdooConfig:
    url: str
    db: str
    user: str
    password: str


def get_config(instance: Instance) -> OdooConfig:
    """Resolve credentials for the requested instance.

    Raises ValueError if any env var is missing — fail fast at first call
    rather than getting cryptic XML-RPC errors later.
    """
    prefix = f"ODOO_{instance.upper()}_"
    keys = ["URL", "DB", "USER", "PASSWORD"]
    values = {}
    for k in keys:
        env_key = prefix + k
        v = os.environ.get(env_key)
        if not v:
            raise ValueError(
                f"Missing env var {env_key} for instance '{instance}'. "
                f"Set ODOO_{instance.upper()}_{{URL,DB,USER,PASSWORD}} before starting."
            )
        values[k.lower()] = v
    return OdooConfig(**values)
