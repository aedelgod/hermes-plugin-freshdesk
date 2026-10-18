from __future__ import annotations

import base64
import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Mapping


class FreshdeskError(RuntimeError):
    """Freshdesk request or response could not be handled safely."""


class FreshdeskHttpError(FreshdeskError):
    """A Freshdesk HTTP response returned a non-success status code."""

    def __init__(self, status: int, payload: str = "") -> None:
        self.status = status
        super().__init__(f"Freshdesk HTTP status {status}: {payload}")


class FreshdeskTransportError(FreshdeskError):
    """The transport could not reach Freshdesk (timeout, connection reset, ...)."""


class FreshdeskWriteUncertainError(FreshdeskError):
    """A write may have succeeded, but its exact result could not be verified."""

    def __init__(self, operation: str, created_id: int | None, cause: BaseException) -> None:
        self.operation = operation
        self.created_id = created_id
        identity = f" ID {created_id}" if created_id is not None else ""
        super().__init__(
            f"Freshdesk {operation}{identity} may have succeeded but verification failed; "
            "do not retry blindly"
        )


Transport = Callable[
    [str, str, Mapping[str, str], bytes | None, float],
    tuple[int, str, Mapping[str, str], bytes],
]

_ACCOUNT = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_ATTACHMENT_HOST_RE = re.compile(r"^[a-z0-9-]+-attachment\.freshdesk\.com$")
_CDN_S3_HOST_RE = re.compile(r"^cdn\.freshdesk\.com\.s3(\.[a-z0-9-]+)?\.amazonaws\.com$")

# POST response codes that are *certain* failures: Freshdesk answered, the
# request was rejected, and no ticket was created. Anything else (408, 429, 5xx,
# transport failures) is treated as an ambiguous write.
_CERTAIN_FAILURE_STATUS = frozenset({400, 401, 403, 404, 409, 422})
_MAX_SEARCH_PAGES = 10


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_bounded(stream, limit: int) -> bytes:
    body = stream.read(limit + 1)
    if not isinstance(body, bytes) or len(body) > limit:
        raise FreshdeskError("Freshdesk response exceeds the size limit")
    return body


def _attachment_path_is_unsafe(path: str) -> bool:
    """True if an attachment URL path contains a trailing-traversal ``dot``
    segment, a backslash, or an encoded dot segment (``%2e`` / ``%2E`` and
    double-encoded forms) that a transport could normalize across origins.

    ``urllib.parse.urlsplit`` does not normalize ``..`` or decode percent
    sequences, so we decode iteratively to surface single- and double-encoded
    dot segments in one pass.
    """
    if "\\" in path:
        return True
    current = path
    for _ in range(3):
        if any(segment in (".", "..") for segment in current.split("/")):
            return True
        decoded = urllib.parse.unquote(current)
        if decoded == current:
            break
        current = decoded
    return False


def _default_transport(method, url, headers, body, timeout, limit):
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urllib.request.build_opener(_RejectRedirects()).open(request, timeout=timeout) as response:
            raw_length = response.headers.get("Content-Length")
            if raw_length is not None:
                try:
                    if int(raw_length) > limit:
                        raise FreshdeskError("Freshdesk response exceeds the size limit")
                except ValueError:
                    raise FreshdeskError("Freshdesk Content-Length is invalid") from None
            return (
                response.status, response.geturl(), dict(response.headers.items()),
                _read_bounded(response, limit),
            )
    except urllib.error.HTTPError as exc:
        payload = exc.read(2048)
        raise FreshdeskHttpError(exc.code, payload.decode("utf-8", "replace")[:500]) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FreshdeskTransportError(f"Freshdesk transport failure: {type(exc).__name__}") from None


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _origin(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Freshdesk domain is required")
    raw = value.strip().lower()
    if _ACCOUNT.fullmatch(raw):
        return f"https://{raw}.freshdesk.com"
    parsed = urllib.parse.urlsplit(raw)
    host = parsed.hostname or ""
    account = host.removesuffix(".freshdesk.com")
    if (
        parsed.scheme != "https"
        or not host.endswith(".freshdesk.com")
        or not _ACCOUNT.fullmatch(account)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Freshdesk domain must be an account name or native HTTPS Freshdesk origin")
    return f"https://{host}"


def _open_directory_nofollow(path: Path) -> int:
    """Open/create an absolute directory tree without traversing symlinks on POSIX."""
    path = Path(path).expanduser().absolute()
    if os.name == "nt" or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        path.mkdir(parents=True, exist_ok=True)
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current /= part
            if current.is_symlink():
                raise FreshdeskError("attachment directory contains a symlink")
        return os.open(path, os.O_RDONLY)

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                os.mkdir(part, 0o700, dir_fd=descriptor)
                child = os.open(part, flags, dir_fd=descriptor)
            except OSError as exc:
                raise FreshdeskError(
                    f"attachment directory is unsafe: {type(exc).__name__}"
                ) from None
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


class FreshdeskClient:
    """Dependency-free, bounded client for the Freshdesk v2 API."""

    def __init__(
        self,
        domain: str,
        api_key: str,
        *,
        transport: Transport | None = None,
        timeout: float = 20.0,
        per_page: int = 100,
        max_pages: int = 10,
        max_response_bytes: int = 4 * 1024 * 1024,
        max_attachment_bytes: int = 25 * 1024 * 1024,
    ) -> None:
        self.origin = _origin(domain)
        self._account_host = urllib.parse.urlsplit(self.origin).hostname or ""
        if not isinstance(api_key, str) or not api_key.strip() or "\n" in api_key or "\r" in api_key:
            raise ValueError("Freshdesk API key is invalid")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("timeout must be positive")
        if type(per_page) is not int or not 1 <= per_page <= 100:
            raise ValueError("per_page must be between 1 and 100")
        if type(max_pages) is not int or not 1 <= max_pages <= 300:
            raise ValueError("max_pages must be between 1 and 300")
        if type(max_response_bytes) is not int or max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        if type(max_attachment_bytes) is not int or max_attachment_bytes <= 0:
            raise ValueError("max_attachment_bytes must be positive")
        credential = base64.b64encode(f"{api_key.strip()}:X".encode()).decode("ascii")
        self._headers = {
            "Authorization": f"Basic {credential}",
            "Accept": "application/json",
            "User-Agent": "Hermes-Freshdesk-Connector/0.1.1",
        }
        self._transport = transport or _default_transport
        self._bounded_transport = transport is None
        self._timeout = float(timeout)
        self._per_page = per_page
        self._max_pages = max_pages
        self._max_response_bytes = max_response_bytes
        self._max_attachment_bytes = max_attachment_bytes

    def _url(self, path: str, query: Mapping[str, object] | None = None) -> str:
        if not path.startswith("/api/v2/") or "?" in path or "#" in path:
            raise ValueError("Freshdesk API path is invalid")
        url = self.origin + path
        if query:
            clean = {key: value for key, value in query.items() if value is not None and value != ""}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        return url

    def _request(
        self, method: str, url: str, *, payload=None, authenticated: bool = True, limit=None
    ) -> bytes:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port is not None or parsed.fragment:
            raise ValueError("request URL is invalid")
        data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        headers = dict(self._headers) if authenticated else {"Accept": "application/octet-stream", "User-Agent": self._headers["User-Agent"]}
        if data is not None:
            headers["Content-Type"] = "application/json"
        size_limit = self._max_response_bytes if limit is None else limit
        if self._bounded_transport:
            status, final_url, _, body = self._transport(
                method, url, headers, data, self._timeout, size_limit
            )
        else:
            status, final_url, _, body = self._transport(method, url, headers, data, self._timeout)
        if final_url != url:
            raise FreshdeskError("redirects are not accepted")
        expected = 201 if method == "POST" else 200
        if status != expected:
            raise FreshdeskHttpError(status, "")
        if not isinstance(body, bytes):
            raise FreshdeskError("Freshdesk returned a non-byte response")
        if len(body) > size_limit:
            raise FreshdeskError("Freshdesk response exceeds the size limit")
        return body

    def _json(self, method: str, path: str, *, query=None, payload=None):
        body = self._request(method, self._url(path, query), payload=payload)
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise FreshdeskError("Freshdesk returned malformed JSON") from exc
        return value

    def _paged_truncated(self, path: str, query: Mapping[str, object] | None = None) -> tuple[list[dict], bool]:
        rows: list[dict] = []
        base = dict(query or {})
        for page in range(1, self._max_pages + 1):
            value = self._json("GET", path, query={**base, "page": page, "per_page": self._per_page})
            if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
                raise FreshdeskError("Freshdesk returned an invalid list response")
            rows.extend(value)
            if len(value) < self._per_page:
                return rows, False
        return rows, True

    def _paged(self, path: str, query: Mapping[str, object] | None = None) -> list[dict]:
        rows, truncated = self._paged_truncated(path, query)
        if truncated:
            raise FreshdeskError("Freshdesk pagination limit was reached")
        return rows

    def list_tickets(self, *, updated_since: str = "", include_description: bool = False) -> dict:
        query: dict[str, object] = {"updated_since": updated_since}
        if include_description:
            query["include"] = "description"
        rows, truncated = self._paged_truncated("/api/v2/tickets", query)
        return {"tickets": rows, "truncated": truncated}

    def get_ticket(self, ticket_id: int, *, include: str | None = None) -> dict:
        ticket_id = _positive_int(ticket_id, "ticket_id")
        value = self._json("GET", f"/api/v2/tickets/{ticket_id}", query={"include": include} if include else None)
        if not isinstance(value, dict) or value.get("id") != ticket_id:
            raise FreshdeskError("Freshdesk ticket readback is invalid")
        return value

    def list_conversations(self, ticket_id: int) -> list[dict]:
        ticket_id = _positive_int(ticket_id, "ticket_id")
        return self._paged(f"/api/v2/tickets/{ticket_id}/conversations")

    def get_contact(self, contact_id: int) -> dict:
        contact_id = _positive_int(contact_id, "contact_id")
        value = self._json("GET", f"/api/v2/contacts/{contact_id}")
        if not isinstance(value, dict) or value.get("id") != contact_id:
            raise FreshdeskError("Freshdesk contact readback is invalid")
        return value

    @staticmethod
    def _pick_attachment(obj: object, attachment_id: int) -> Mapping[str, object] | None:
        if not isinstance(obj, dict):
            return None
        for item in obj.get("attachments") or []:
            if isinstance(item, dict) and item.get("id") == attachment_id:
                return item
        return None

    def _find_attachment(self, ticket_id: int, attachment_id: int) -> Mapping[str, object]:
        ticket = self.get_ticket(ticket_id)
        attachment = self._pick_attachment(ticket, attachment_id)
        if attachment is None:
            for conversation in self.list_conversations(ticket_id):
                attachment = self._pick_attachment(conversation, attachment_id)
                if attachment is not None:
                    break
        if attachment is None:
            raise FreshdeskError(f"attachment {attachment_id} not found on ticket {ticket_id}")
        return attachment

    def _classify_attachment_url(self, url: str) -> str:
        """Return the trust tier of an attachment URL: ``freshdesk`` (same-origin,
        credentials sent), ``cdn``, or ``s3`` (credentials never sent)."""
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.fragment
        ):
            raise ValueError("attachment URL is outside the permitted Freshdesk/S3 origins")
        host = (parsed.hostname or "").lower()
        path = parsed.path or ""
        if _attachment_path_is_unsafe(path):
            raise ValueError("attachment URL path must not contain dot segments or backslashes")
        if host == self._account_host and path.startswith("/helpdesk/attachments/"):
            return "freshdesk"
        if host == "attachment.freshdesk.com" or _ATTACHMENT_HOST_RE.fullmatch(host):
            return "cdn"
        if host == "s3.amazonaws.com" and path.startswith("/cdn.freshdesk.com/"):
            return "s3"
        if _CDN_S3_HOST_RE.fullmatch(host):
            return "s3"
        raise ValueError("attachment URL is outside the permitted Freshdesk/S3 origins")

    def download_attachment(self, ticket_id: int, attachment_id: int, directory: Path) -> Path:
        ticket_id = _positive_int(ticket_id, "ticket_id")
        attachment_id = _positive_int(attachment_id, "attachment_id")
        attachment = self._find_attachment(ticket_id, attachment_id)
        url = attachment.get("attachment_url")
        name = attachment.get("name")
        if not isinstance(url, str) or not isinstance(name, str) or not name.strip():
            raise FreshdeskError("attachment URL and name are required")
        safe_name = Path(name).name
        if safe_name in ("", ".", ".."):
            raise ValueError("attachment filename is invalid")
        kind = self._classify_attachment_url(url)
        body = self._request(
            "GET", url, authenticated=(kind == "freshdesk"), limit=self._max_attachment_bytes
        )
        directory = Path(directory).expanduser().absolute()
        destination = directory / safe_name
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        directory_fd = _open_directory_nofollow(directory)
        try:
            descriptor = os.open(safe_name, flags, 0o600, dir_fd=directory_fd)
        except FileExistsError:
            os.close(directory_fd)
            raise FreshdeskError("attachment destination already exists") from None
        except OSError as exc:
            os.close(directory_fd)
            raise FreshdeskError(f"attachment destination could not be created: {type(exc).__name__}") from None
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(body)
        except Exception:
            try:
                os.unlink(safe_name, dir_fd=directory_fd)
            except OSError:
                pass
            raise
        finally:
            os.close(directory_fd)
        return destination

    def add_private_note(self, ticket_id: int, body: str) -> dict:
        ticket_id = _positive_int(ticket_id, "ticket_id")
        if not isinstance(body, str) or not body.strip():
            raise ValueError("body is required")
        clean_body = body.strip()
        html_body = f"<div>{html.escape(clean_body).replace(chr(10), '<br>')}</div>"
        try:
            created = self._json(
                "POST", f"/api/v2/tickets/{ticket_id}/notes",
                payload={"body": html_body, "private": True},
            )
        except FreshdeskHttpError as exc:
            if exc.status in _CERTAIN_FAILURE_STATUS:
                raise FreshdeskError(f"private-note write rejected by Freshdesk: HTTP {exc.status}") from exc
            raise FreshdeskWriteUncertainError("private-note write", None, exc) from exc
        except FreshdeskTransportError as exc:
            raise FreshdeskWriteUncertainError("private-note write", None, exc) from exc
        if not isinstance(created, dict) or type(created.get("id")) is not int:
            raise FreshdeskWriteUncertainError(
                "private-note write", None, FreshdeskError("note response is invalid")
            )
        note_id = created["id"]
        try:
            matches = [item for item in self.list_conversations(ticket_id) if item.get("id") == note_id]
            if (
                len(matches) != 1
                or matches[0].get("private") is not True
                or str(matches[0].get("body_text", "")).replace("\r\n", "\n").strip()
                != clean_body.replace("\r\n", "\n")
            ):
                raise FreshdeskError("private note readback did not match")
            return matches[0]
        except Exception as exc:
            raise FreshdeskWriteUncertainError("private-note write", note_id, exc) from exc

    def _verify_create_readback(self, sent: Mapping[str, object], readback: Mapping[str, object]) -> None:
        for key in ("subject", "priority", "status", "requester_id"):
            if key in sent and readback.get(key) != sent[key]:
                raise FreshdeskError(f"ticket readback field {key} did not match")
        if "email" in sent:
            requester = readback.get("requester") if isinstance(readback.get("requester"), dict) else {}
            rb_email = requester.get("email")
            if not isinstance(rb_email, str) or rb_email.lower() != str(sent["email"]).lower():
                raise FreshdeskError("ticket readback email did not match requester.email")
        if "description" in sent:
            desc_text = readback.get("description_text", "")
            if not isinstance(desc_text, str):
                raise FreshdeskError("ticket readback is missing description_text")
            norm = lambda s: s.replace("\r\n", "\n").replace("\r", "\n")
            if norm(desc_text) != norm(sent["description"]):
                raise FreshdeskError("ticket readback description did not match description_text")

    def create_ticket(self, fields: Mapping[str, object]) -> dict:
        if not isinstance(fields, Mapping):
            raise ValueError("fields must be an object")
        required = ("subject", "description", "priority", "status")
        if any(key not in fields for key in required):
            raise ValueError("ticket requires subject, description, priority, and status")
        has_email = bool(fields.get("email"))
        has_requester_id = bool(fields.get("requester_id"))
        if has_email == has_requester_id:
            raise ValueError("ticket requires exactly one of email or requester_id")
        allowed = {*required, "email", "requester_id"}
        unsupported = sorted(set(fields) - allowed)
        if unsupported:
            raise ValueError(f"unsupported fields: {', '.join(unsupported)}")
        sent = dict(fields)
        try:
            created = self._json("POST", "/api/v2/tickets", payload=sent)
        except FreshdeskHttpError as exc:
            if exc.status in _CERTAIN_FAILURE_STATUS:
                raise FreshdeskError(f"create-ticket rejected by Freshdesk: HTTP {exc.status}") from exc
            raise FreshdeskWriteUncertainError("ticket creation", None, exc) from exc
        except FreshdeskTransportError as exc:
            raise FreshdeskWriteUncertainError("ticket creation", None, exc) from exc
        if not isinstance(created, dict) or type(created.get("id")) is not int:
            raise FreshdeskWriteUncertainError(
                "ticket creation", None, FreshdeskError("create-ticket response is invalid")
            )
        ticket_id = created["id"]
        try:
            readback = self.get_ticket(ticket_id, include="requester")
            self._verify_create_readback(sent, readback)
            return readback
        except Exception as exc:
            raise FreshdeskWriteUncertainError("ticket creation", ticket_id, exc) from exc

    def search_tickets(self, *, status: int | None = None, priority: int | None = None) -> dict:
        if status is not None:
            _positive_int(status, "status")
            if not 2 <= status <= 5:
                raise ValueError("status must be between 2 and 5")
        if priority is not None:
            _positive_int(priority, "priority")
            if not 1 <= priority <= 4:
                raise ValueError("priority must be between 1 and 4")
        clauses = []
        if status is not None:
            clauses.append(f"status:{status}")
        if priority is not None:
            clauses.append(f"priority:{priority}")
        if not clauses:
            raise ValueError("search requires at least one of status or priority")
        query = f'"{" AND ".join(clauses)}"'
        results, truncated = self._search_paged(query)
        return {"results": results, "truncated": truncated}

    def _search_paged(self, query: str) -> tuple[list, bool]:
        results: list = []
        total: int | None = None
        for page in range(1, _MAX_SEARCH_PAGES + 1):
            value = self._json("GET", "/api/v2/search/tickets", query={"query": query, "page": page})
            if not isinstance(value, dict):
                raise FreshdeskError("Freshdesk returned an invalid search response")
            page_results = value.get("results")
            if not isinstance(page_results, list):
                raise FreshdeskError("Freshdesk returned an invalid search response")
            results.extend(page_results)
            raw_total = value.get("total")
            if isinstance(raw_total, int):
                total = raw_total
            if total is not None and len(results) >= total:
                return results, False
            if len(page_results) == 0:
                return results, False
        return results, True
