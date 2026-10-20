"""Hermes plugin entry point for the generic Freshdesk v2 connector."""

from __future__ import annotations

import hashlib
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


_STATUS_LABELS = {2: "Open", 3: "Pending", 4: "Resolved", 5: "Closed"}
_PRIORITY_LABELS = {1: "Low", 2: "Medium", 3: "High", 4: "Urgent"}


def _status_label(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, int):
        return f"{value} unrecognized"
    if value in _STATUS_LABELS:
        return f"{_STATUS_LABELS[value]} ({value})"
    return f"{value} unrecognized"


def _priority_label(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, int):
        return f"{value} unrecognized"
    if value in _PRIORITY_LABELS:
        return f"{_PRIORITY_LABELS[value]} ({value})"
    return f"{value} unrecognized"


def _excerpt(value: object, limit: int = 100) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = " ".join(value.split())
    if len(cleaned) > limit:
        return cleaned[: max(0, limit - 3)] + "..."
    return cleaned


def _preview_ticket(ticket_id: object):
    """Return ``{subject, status, priority}`` for a ticket, or ``None``.

    The default implementation reads through ``_client().get_ticket``. Tests may
    monkeypatch this function to avoid the network. Any failure (missing
    credentials, transport error, malformed response) is swallowed so a broken
    preview never blocks the approval prompt.
    """
    try:
        ticket = _client().get_ticket(ticket_id)
    except Exception:
        return None
    if not isinstance(ticket, dict):
        return None
    return {
        "subject": ticket.get("subject"),
        "status": ticket.get("status"),
        "priority": ticket.get("priority"),
    }


def _format_from_to(label: str, current: object, proposed: object, label_fn) -> str:
    current_text = "current value unavailable" if current is None else label_fn(current)
    return f"{label}: {current_text} -> {label_fn(proposed)}"


def _truncate_message(message: str, limit: int = 450) -> str:
    if len(message) <= limit:
        return message
    return message[: max(0, limit - 3)] + "..."


def _approval_relevant_args(tool_name: str, args: dict) -> dict:
    args = args or {}
    if tool_name == "freshdesk_create_ticket":
        src = args.get("fields") if isinstance(args.get("fields"), dict) else {}
    else:
        src = args
    return {
        "tool": tool_name,
        "ticket_id": src.get("ticket_id"),
        "body": src.get("body"),
        "subject": src.get("subject"),
        "description": src.get("description"),
        "email": src.get("email"),
        "requester_id": src.get("requester_id"),
        "status": src.get("status"),
        "priority": src.get("priority"),
        "time_spent": src.get("time_spent"),
        "billable": src.get("billable"),
        "note": src.get("note"),
        "agent_id": src.get("agent_id"),
    }


def _rule_key(tool_name: str, args: dict) -> str:
    payload = _approval_relevant_args(tool_name, args)
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return f"{tool_name}:{digest[:16]}"


def _approval_message_update_ticket(args: dict) -> str:
    ticket_id = args.get("ticket_id")
    current = _preview_ticket(ticket_id)
    subject = _excerpt(current.get("subject")) if current else ""
    header = f"Update Freshdesk ticket {ticket_id}"
    if subject:
        header = f"{header}: {subject}"
    lines = [header]
    if "status" in args:
        cur = current.get("status") if current else None
        lines.append(_format_from_to("status", cur, args["status"], _status_label))
    if "priority" in args:
        cur = current.get("priority") if current else None
        lines.append(_format_from_to("priority", cur, args["priority"], _priority_label))
    return "\n".join(lines)


def _approval_message_add_private_note(args: dict) -> str:
    ticket_id = args.get("ticket_id")
    body = args.get("body")
    current = _preview_ticket(ticket_id)
    subject = _excerpt(current.get("subject")) if current else ""
    header = f"Add a private note to Freshdesk ticket {ticket_id}"
    if subject:
        header = f"{header}: {subject}"
    lines = [header]
    if isinstance(body, str) and body.strip():
        lines.append(f"Note: {_excerpt(body)}")
    return "\n".join(lines)


def _approval_message_reply_to_ticket(args: dict) -> str:
    ticket_id = args.get("ticket_id")
    body = args.get("body")
    current = _preview_ticket(ticket_id)
    subject = _excerpt(current.get("subject")) if current else ""
    header = f"Freshdesk public reply to ticket {ticket_id}"
    if subject:
        header = f"{header}: {subject}"
    lines = [header, "This emails the requester."]
    if isinstance(body, str) and body.strip():
        lines.append(f"Body: {_excerpt(body)}")
    return "\n".join(lines)


def _approval_message_create_ticket(args: dict) -> str:
    fields = args.get("fields") if isinstance(args.get("fields"), dict) else {}
    subject = _excerpt(fields.get("subject"))
    description = _excerpt(fields.get("description"))
    status = fields.get("status")
    priority = fields.get("priority")
    email = fields.get("email")
    requester_id = fields.get("requester_id")
    lines = ["Create a new Freshdesk ticket"]
    if subject:
        lines.append(f"Subject: {subject}")
    if email:
        lines.append(f"Requester (email): {email}")
    if requester_id is not None:
        lines.append(f"Requester (id): {requester_id}")
    if status is not None:
        lines.append(f"Status: {_status_label(status)}")
    if priority is not None:
        lines.append(f"Priority: {_priority_label(priority)}")
    if description:
        lines.append(f"Description: {description}")
    return "\n".join(lines)


def _approval_message_add_time_entry(args: dict) -> str:
    ticket_id = args.get("ticket_id")
    time_spent = args.get("time_spent")
    billable = args.get("billable")
    note = args.get("note")
    agent_id = args.get("agent_id")
    current = _preview_ticket(ticket_id)
    subject = _excerpt(current.get("subject")) if current else ""
    header = f"Freshdesk time entry for ticket {ticket_id}"
    if subject:
        header = f"{header}: {subject}"
    lines = [header]
    if time_spent is not None:
        lines.append(f"Duration: {time_spent}")
    if billable is not None:
        lines.append(f"Billable: {'yes' if billable else 'no'}")
    if isinstance(note, str) and note.strip():
        lines.append(f"Note: {_excerpt(note)}")
    if agent_id is not None:
        lines.append(f"Agent: {agent_id}")
    return "\n".join(lines)


_APPROVAL_MESSAGES = {
    "freshdesk_add_private_note": _approval_message_add_private_note,
    "freshdesk_create_ticket": _approval_message_create_ticket,
    "freshdesk_reply_to_ticket": _approval_message_reply_to_ticket,
    "freshdesk_add_time_entry": _approval_message_add_time_entry,
    "freshdesk_update_ticket": _approval_message_update_ticket,
}


def _approval_hook(tool_name: str, args: dict | None = None, **_: Any):
    builder = _APPROVAL_MESSAGES.get(tool_name)
    if builder is None:
        return None
    args = args or {}
    try:
        message = builder(args)
    except Exception:
        message = f"Approve {tool_name}"
    return {
        "action": "approve",
        "message": _truncate_message(message),
        "rule_key": _rule_key(tool_name, args),
    }


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
_TIME_SPENT = {"type": "string", "pattern": "^[0-9]{2,4}:[0-5][0-9]$"}
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
