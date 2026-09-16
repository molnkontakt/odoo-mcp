import logging

from odoo import _, api, models
from odoo.exceptions import AccessDenied, AccessError
from odoo.service.model import call_kw

_logger = logging.getLogger(__name__)


class McpGateway(models.AbstractModel):
    """Run an ORM call as another (active) user.

    The MCP server authenticates the human with OAuth and holds one Odoo service account. Through this
    endpoint it executes ``model.method(*args, **kwargs)`` with the environment switched to the user
    whose login matches the token's e-mail, so normal ACLs and record rules apply: a portal member sees
    their own invoices, an accountant sees the books. ``sudo()`` is never used on the way in. Only the
    group *MCP gateway / Får byta användare* may call it; the service account needs nothing else.
    """
    _name = "mcp.gateway"
    _description = "MCP gateway (act as user)"

    @api.model
    def execute_as(self, login, model, method, args=None, kwargs=None):
        if not self.env.user.has_group("odoo_mcp_gateway.group_mcp_gateway"):
            raise AccessDenied(_("mcp.gateway.execute_as is reserved for the MCP service account"))
        if not login or not isinstance(login, str):
            raise AccessDenied(_("mcp.gateway: no caller identity"))
        if not model or model not in self.env or method.startswith("_"):
            raise AccessError(_("mcp.gateway: invalid model or method"))
        user = self.env["res.users"].sudo().search([("login", "=", login), ("active", "=", True)], limit=1)
        if not user:
            user = self.env["res.users"].sudo().search([("oauth_uid", "=", login), ("active", "=", True)], limit=1) \
                if "oauth_uid" in self.env["res.users"]._fields else user
        if not user:
            raise AccessDenied(_("mcp.gateway: no active Odoo user for %s", login))
        kwargs = dict(kwargs or {})
        context = dict(user.context_get())
        context.update(kwargs.get("context") or {})
        context.setdefault("allowed_company_ids", user.company_ids.ids)
        kwargs["context"] = context
        env = self.env(user=user.id, context=context)
        _logger.debug("mcp.gateway: %s (service %s) → %s.%s", login, self.env.user.login, model, method)
        return call_kw(env[model], method, list(args or []), kwargs)
