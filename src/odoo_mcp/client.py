"""XML-RPC client for Odoo with per-instance connection caching.

Wraps xmlrpc.client.ServerProxy to:
- Authenticate once and cache the uid
- Provide a clean execute_kw interface
- Surface meaningful errors

Each instance ("prod" / "dev") gets its own cached client.
"""

from __future__ import annotations

import xmlrpc.client
from functools import lru_cache
from typing import Any

from odoo_mcp.instances import Instance, get_config


class OdooClient:
    def __init__(self, instance: Instance):
        self.instance = instance
        self.config = get_config(instance)
        self._common = xmlrpc.client.ServerProxy(
            f"{self.config.url}/xmlrpc/2/common", allow_none=True
        )
        self._models = xmlrpc.client.ServerProxy(
            f"{self.config.url}/xmlrpc/2/object", allow_none=True
        )
        self._uid: int | None = None

    @property
    def uid(self) -> int:
        if self._uid is None:
            uid = self._common.authenticate(
                self.config.db, self.config.user, self.config.password, {}
            )
            if not uid:
                raise RuntimeError(
                    f"Authentication failed for instance '{self.instance}' "
                    f"as {self.config.user}"
                )
            self._uid = uid
        return self._uid

    def execute_kw(
        self,
        model: str,
        method: str,
        args: list[Any],
        kwargs: dict[str, Any] | None = None,
    ) -> Any:
        """Execute Odoo ORM method via XML-RPC.

        Example:
            client.execute_kw("res.partner", "search_read",
                              [[("name", "ilike", "Acme")]],
                              {"fields": ["id", "name"], "limit": 5})
        """
        return self._models.execute_kw(
            self.config.db,
            self.uid,
            self.config.password,
            model,
            method,
            args,
            kwargs or {},
        )


@lru_cache(maxsize=2)
def get_client(instance: Instance) -> OdooClient:
    """Cached client factory — one per instance."""
    return OdooClient(instance)
