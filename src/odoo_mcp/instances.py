"""Configuration for Odoo instances.

Instances are discovered from the environment at start-up: every
``ODOO_<NAME>_URL`` defines an instance ``<name>`` (lower-cased), e.g.
``ODOO_PROD_URL`` / ``ODOO_DEV_URL`` / ``ODOO_ACME_URL``. Credentials come from
environment variables, typically populated by a secret-manager (Phase, Vault,
AWS SSM, etc.) or a `.env` file (gitignored).

An instance is treated as *production* (gated behind ``odoo:prod`` over HTTP)
when it is named ``prod`` or when ``ODOO_<NAME>_PRODUCTION`` is truthy.
"""

import os
from dataclasses import dataclass
from typing import Literal

DEFAULT_INSTANCES = ("prod", "dev")
_TRUE = ("1", "true", "yes", "on")


def available_instances() -> tuple[str, ...]:
    """Instance names configured in the environment, ``prod``/``dev`` first."""
    found = {k[len("ODOO_"):-len("_URL")].lower() for k, v in os.environ.items()
             if k.startswith("ODOO_") and k.endswith("_URL") and v and k.count("_") == 2}
    ordered = [n for n in DEFAULT_INSTANCES if n in found] + sorted(found - set(DEFAULT_INSTANCES))
    return tuple(ordered) or DEFAULT_INSTANCES


# Built once at import: the MCP tool schemas expose it as an enum, so the server
# must be started with the environment already populated (``phase run … -- odoo-mcp``).
Instance = Literal[available_instances()]  # type: ignore[valid-type]


def default_instance() -> str | None:
    """The only configured instance, when there is exactly one; else None.

    Tools accept ``instance=None`` and fall back to this, so a single-instance
    deployment (one association, one company database) does not make every
    caller repeat a name that cannot be anything else.
    """
    names = available_instances()
    return names[0] if len(names) == 1 else None


def resolve_instance(instance: str | None) -> str:
    if instance:
        return instance
    only = default_instance()
    if only:
        return only
    raise ValueError(
        f"Several instances are configured ({', '.join(available_instances())}); pass `instance`."
    )


def is_production(instance: str) -> bool:
    name = str(instance).strip().lower()
    return name == "prod" or os.environ.get(f"ODOO_{name.upper()}_PRODUCTION", "").strip().lower() in _TRUE


@dataclass(frozen=True)
class OdooConfig:
    url: str
    db: str
    user: str
    password: str
    #: Run every call as the OAuth caller through the `odoo_mcp_gateway` addon
    #: (``ODOO_<NAME>_IMPERSONATE=1``). The service account then only needs
    #: group_system for the gateway; Odoo's ACLs apply to the human.
    impersonate: bool = False


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
    values["impersonate"] = os.environ.get(prefix + "IMPERSONATE", "").strip().lower() in _TRUE
    return OdooConfig(**values)
