"""Hermes plugin entry point for the generic Freshdesk v2 connector."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

from .client import FreshdeskClient, FreshdeskWriteUncertainError

REQUIRES_ENV = ["FRESHDESK_DOMAIN", "FRESHDESK_API_KEY"]

# Resolve the profile-scoped secret reader lazily. In the plugin runtime the
# ``agent`` package is present and ``get_secret`` honours the active profile
# scope; in an isolated subprocess (e.g. ``hermes plugins validate`` or the
# unit-test runner) ``utils``/``hermes_yaml`` may be unavailable, in which case
# the import fails and the tools are declared unavailable rather than falling
# back to ``os.environ``.
try:  # pragma: no cover - exercised by the runtime, not by bare subprocesses
    from agent.secret_scope import get_secret
except Exception:  # noqa: BLE001 - any import failure disables the tools
    get_secret = None  # type: ignore[assignment]


def _available() -> bool:
    if get_secret is None:
        return False
    return all(
        isinstance(value, str) and value.strip()
        for value in (get_secret(name, "") for name in REQUIRES_ENV)
    )


def _client() -> FreshdeskClient:
    if get_secret is None:
        raise RuntimeError("Hermes secret scope is not available")
    domain = get_secret("FRESHDESK_DOMAIN")
    api_key = get_secret("FRESHDESK_API_KEY")
    if not domain or not api_key:
        raise RuntimeError("Freshdesk credentials are not configured")
    return FreshdeskClient(domain, api_key)


def _result(call: Callable[[], Any]) -> str:
    try:
        return json.dumps({"success": True, "data": call()}, ensure_ascii=False, default=str)
    except FreshdeskWriteUncertainError as exc:
        return json.dumps({
            "success": False,
            "state": "write_may_have_succeeded",
            "created_id": exc.created_id,
            "error": str(exc),
            "error_type": type(exc).__name__,
            "retry_safe": False,
        })
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc), "error_type": type(exc).__name__})


def _approval_hook(tool_name: str, args: dict | None = None, **_: Any):
    if tool_name == "freshdesk_add_private_note":
        ticket = (args or {}).get("ticket_id", "unknown")
        return {
            "action": "approve",
            "message": f"Add a private note to Freshdesk ticket {ticket}",
        }
    if tool_name == "freshdesk_create_ticket":
        return {
            "action": "approve",
            "message": "Create a new Freshdesk ticket",
        }
    if tool_name == "freshdesk_reply_to_ticket":
        ticket = (args or {}).get("ticket_id", "unknown")
        return {
            "action": "approve",
            "message": f"Send a public reply to the requester of Freshdesk ticket {ticket}",
        }
    if tool_name == "freshdesk_add_time_entry":
        ticket = (args or {}).get("ticket_id", "unknown")
        duration = (args or {}).get("time_spent", "unknown")
        billable = (args or {}).get("billable", "unknown")
        return {
            "action": "approve",
            "message": f"Add time entry of {duration} to Freshdesk ticket {ticket} (billable: {billable})",
        }
    if tool_name == "freshdesk_update_ticket":
        ticket = (args or {}).get("ticket_id", "unknown")
        fields = sorted(k for k in (args or {}) if k in ("status", "priority"))
        field_text = ", ".join(fields) if fields else "no fields"
        return {
            "action": "approve",
            "message": f"Update Freshdesk ticket {ticket}: {field_text}",
        }
    return None


def _list_tickets(args: dict, **_: Any) -> str:
    return _result(lambda: _client().list_tickets(
        updated_since=args.get("updated_since", ""),
        include_description=bool(args.get("include_description", False)),
    ))


def _get_ticket(args: dict, **_: Any) -> str:
    return _result(lambda: _client().get_ticket(args.get("ticket_id")))


def _list_conversations(args: dict, **_: Any) -> str:
    return _result(lambda: _client().list_conversations(args.get("ticket_id")))


def _get_contact(args: dict, **_: Any) -> str:
    return _result(lambda: _client().get_contact(args.get("contact_id")))


def _download_attachment(args: dict, **_: Any) -> str:
    def run():
        try:
            from hermes_constants import get_hermes_home

            root = Path(get_hermes_home())
        except ImportError:
            root = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
        path = _client().download_attachment(
            args.get("ticket_id"), args.get("attachment_id"), root / "cache" / "freshdesk"
        )
        return {"path": str(path), "bytes": path.stat().st_size}

    return _result(run)


def _add_private_note(args: dict, **_: Any) -> str:
    def run():
        return _client().add_private_note(args.get("ticket_id"), args.get("body"))

    return _result(run)


def _create_ticket(args: dict, **_: Any) -> str:
    def run():
        fields = args.get("fields")
        if not isinstance(fields, dict):
            raise ValueError("fields must be an object")
        return _client().create_ticket(fields)

    return _result(run)


def _reply_to_ticket(args: dict, **_: Any) -> str:
    def run():
        return _client().reply_to_ticket(args.get("ticket_id"), args.get("body"))
    return _result(run)


def _add_time_entry(args: dict, **_: Any) -> str:
    def run():
        return _client().add_time_entry(
            args.get("ticket_id"),
            args.get("time_spent"),
            args.get("note"),
            args.get("billable"),
            args.get("agent_id"),
        )
    return _result(run)


def _update_ticket(args: dict, **_: Any) -> str:
    def run():
        return _client().update_ticket(
            args.get("ticket_id"),
            status=args.get("status"),
            priority=args.get("priority"),
        )
    return _result(run)


def _search_tickets(args: dict, **_: Any) -> str:
    def run():
        if "query" in args:
            raise ValueError("freshdesk_search_tickets does not accept a raw query argument")
        for rejected in ("subject", "keyword"):
            if rejected in args:
                raise ValueError(f"freshdesk_search_tickets does not accept a {rejected} argument")
        return _client().search_tickets(
            status=args.get("status"),
            priority=args.get("priority"),
            tag=args.get("tag"),
            type=args.get("type"),
            agent_id=args.get("agent_id"),
            group_id=args.get("group_id"),
            created_after=args.get("created_after"),
            created_before=args.get("created_before"),
            updated_after=args.get("updated_after"),
            updated_before=args.get("updated_before"),
        )

    return _result(run)


def _schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


_ID = {"type": "integer", "minimum": 1}
_PRIORITY = {"type": "integer", "minimum": 1, "maximum": 4}
_STATUS = {"type": "integer", "minimum": 2, "maximum": 5}
_TIME_SPENT = {"type": "string", "pattern": "^[0-9]{1,4}:[0-5][0-9]$"}
_TAG_VALUE = {"type": "string", "pattern": "^[A-Za-z0-9 _-]{1,64}$"}
_DATE_VALUE = {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"}

_CREATE_FIELDS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "subject": {"type": "string"},
        "description": {"type": "string"},
        "priority": _PRIORITY,
        "status": _STATUS,
        "email": {"type": "string"},
        "requester_id": _ID,
    },
    "required": ["subject", "description", "priority", "status"],
}

_TOOLS = (
    (
        "freshdesk_list_tickets",
        _schema(
            "freshdesk_list_tickets",
            "List Freshdesk tickets using bounded pagination. "
            "With no updated_since, Freshdesk returns only tickets created in the last 30 days. "
            "Description is omitted unless include_description is true.",
            {"updated_since": {"type": "string"}, "include_description": {"type": "boolean", "default": False}},
            [],
        ),
        _list_tickets,
    ),
    (
        "freshdesk_get_ticket",
        _schema(
            "freshdesk_get_ticket",
            "Get one Freshdesk ticket by ID.",
            {"ticket_id": _ID},
            ["ticket_id"],
        ),
        _get_ticket,
    ),
    (
        "freshdesk_search_tickets",
        _schema(
            "freshdesk_search_tickets",
            "Search Freshdesk tickets by status (2-5), priority (1-4), tag, type, "
            "agent_id, group_id, or created_at/updated_at date ranges. "
            "At least one filter is required. This is a read-only list operation.",
            {
                "status": _STATUS, "priority": _PRIORITY,
                "tag": _TAG_VALUE, "type": _TAG_VALUE,
                "agent_id": _ID, "group_id": _ID,
                "created_after": _DATE_VALUE, "created_before": _DATE_VALUE,
                "updated_after": _DATE_VALUE, "updated_before": _DATE_VALUE,
            },
            [],
        ),
        _search_tickets,
    ),
    (
        "freshdesk_list_conversations",
        _schema(
            "freshdesk_list_conversations",
            "List all bounded conversations for a Freshdesk ticket.",
            {"ticket_id": _ID},
            ["ticket_id"],
        ),
        _list_conversations,
    ),
    (
        "freshdesk_get_contact",
        _schema(
            "freshdesk_get_contact",
            "Get one Freshdesk contact by ID.",
            {"contact_id": _ID},
            ["contact_id"],
        ),
        _get_contact,
    ),
    (
        "freshdesk_download_attachment",
        _schema(
            "freshdesk_download_attachment",
            "Download a trusted Freshdesk attachment for a ticket into the Hermes cache. "
            "The attachment is resolved from the ticket and its conversations; only Freshdesk "
            "and Freshdesk CDN/S3 origins are permitted.",
            {"ticket_id": _ID, "attachment_id": _ID},
            ["ticket_id", "attachment_id"],
        ),
        _download_attachment,
    ),
    (
        "freshdesk_add_private_note",
        _schema(
            "freshdesk_add_private_note",
            "Add a private note and verify it by reading the ticket conversations. Hermes requires human approval before execution.",
            {"ticket_id": _ID, "body": {"type": "string"}},
            ["ticket_id", "body"],
        ),
        _add_private_note,
    ),
    (
        "freshdesk_create_ticket",
        _schema(
            "freshdesk_create_ticket",
            "Create a Freshdesk ticket and verify it by reading it back. Hermes requires human approval before execution.",
            {"fields": _CREATE_FIELDS},
            ["fields"],
        ),
        _create_ticket,
    ),
    (
        "freshdesk_reply_to_ticket",
        _schema(
            "freshdesk_reply_to_ticket",
            "Send a public reply to the requester of a Freshdesk ticket. Hermes requires human approval before execution.",
            {"ticket_id": _ID, "body": {"type": "string"}},
            ["ticket_id", "body"],
        ),
        _reply_to_ticket,
    ),
    (
        "freshdesk_add_time_entry",
        _schema(
            "freshdesk_add_time_entry",
            "Add a finished time entry to a Freshdesk ticket. Hermes requires human approval before execution.",
            {
                "ticket_id": _ID,
                "time_spent": _TIME_SPENT,
                "note": {"type": "string"},
                "billable": {"type": "boolean"},
                "agent_id": _ID,
            },
            ["ticket_id", "time_spent", "note", "billable"],
        ),
        _add_time_entry,
    ),
    (
        "freshdesk_update_ticket",
        _schema(
            "freshdesk_update_ticket",
            "Update a Freshdesk ticket's status (2-5) and/or priority (1-4). Hermes requires human approval before execution.",
            {"ticket_id": _ID, "status": _STATUS, "priority": _PRIORITY},
            ["ticket_id"],
        ),
        _update_ticket,
    ),
)


def register(ctx: Any) -> None:
    ctx.register_hook("pre_tool_call", _approval_hook)
    for name, schema, handler in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="freshdesk",
            schema=schema,
            handler=handler,
            check_fn=_available,
            requires_env=REQUIRES_ENV,
            description=schema["description"],
            emoji="🎫",
        )


__all__ = ["FreshdeskClient", "register"]
