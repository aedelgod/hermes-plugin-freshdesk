import base64
import json
import os
import stat
import tempfile
import unittest
import urllib.parse
from pathlib import Path

import freshdesk_connector
from freshdesk_connector import (
    _search_tickets, _available,
)
from freshdesk_connector.client import (
    FreshdeskClient, FreshdeskError, FreshdeskWriteUncertainError, _read_bounded,
)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        # ``_default_transport`` passes a 6th ``limit`` argument; the real client
        # only does so when bounded, but accept/kwargs gracefully so the same
        # fake works for both call shapes.
        self.calls.append((method, url, headers, body, timeout))
        if not self.responses:
            raise AssertionError("unexpected request")
        status, final_url, response_headers, payload = self.responses.pop(0)
        return status, final_url or url, response_headers or {}, payload


def response(status, payload, final_url=None, headers=None):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return status, final_url, headers or {}, body


def _attachment(aid, url, name="doc.pdf", size=4):
    return {"id": aid, "name": name, "attachment_url": url, "size": size}


def _ticket(ticket_id, attachments=None, **extra):
    ticket = {"id": ticket_id, "subject": "T", "attachments": attachments or []}
    ticket.update(extra)
    return ticket


class FreshdeskClientTests(unittest.TestCase):
    def test_bounded_reader_stops_before_unbounded_memory_use(self):
        class Stream:
            def __init__(self):
                self.calls = []

            def read(self, size):
                self.calls.append(size)
                return b"x" * size

        stream = Stream()
        with self.assertRaisesRegex(FreshdeskError, "size limit"):
            _read_bounded(stream, 10)
        self.assertEqual(stream.calls, [11])

    def test_normalizes_account_domain_and_builds_basic_auth(self):
        transport = FakeTransport([response(200, {"id": 42})])
        client = FreshdeskClient("acme", "secret-key", transport=transport)
        self.assertEqual(client.get_ticket(42), {"id": 42})
        _, url, headers, _, _ = transport.calls[0]
        self.assertEqual(url, "https://acme.freshdesk.com/api/v2/tickets/42")
        expected = base64.b64encode(b"secret-key:X").decode("ascii")
        self.assertEqual(headers["Authorization"], f"Basic {expected}")

    def test_rejects_non_native_or_credentialed_origins(self):
        invalid = [
            "https://evil.test", "http://acme.freshdesk.com",
            "https://acme.freshdesk.com:8443", "https://acme.freshdesk.com/path",
            "acme.freshdesk.com.evil.test",
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                FreshdeskClient(value, "key")

    def test_lists_all_ticket_pages_and_reports_not_truncated(self):
        first = [{"id": 1}, {"id": 2}]
        second = [{"id": 3}]
        transport = FakeTransport([response(200, first), response(200, second)])
        client = FreshdeskClient("acme", "key", transport=transport, per_page=2, max_pages=3)
        result = client.list_tickets(updated_since="2026-01-01T00:00:00Z")
        self.assertEqual([row["id"] for row in result["tickets"]], [1, 2, 3])
        self.assertFalse(result["truncated"])
        self.assertNotIn("status", transport.calls[0][1])
        self.assertIn("updated_since=2026-01-01T00%3A00%3A00Z", transport.calls[0][1])
        self.assertIn("page=2", transport.calls[1][1])

    def test_list_tickets_truncates_instead_of_raising_at_page_cap(self):
        transport = FakeTransport([
            response(200, [{"id": 1}]),
            response(200, [{"id": 2}]),
            response(200, [{"id": 3}]),
        ])
        client = FreshdeskClient("acme", "key", transport=transport, per_page=1, max_pages=3)
        result = client.list_tickets()
        self.assertEqual([row["id"] for row in result["tickets"]], [1, 2, 3])
        self.assertTrue(result["truncated"])

    def test_list_tickets_omits_description_by_default_and_sends_include(self):
        transport = FakeTransport([response(200, [{"id": 1}])])
        client = FreshdeskClient("acme", "key", transport=transport)
        client.list_tickets()
        self.assertNotIn("include=", transport.calls[0][1])
        transport = FakeTransport([response(200, [{"id": 1}])])
        FreshdeskClient("acme", "key", transport=transport).list_tickets(include_description=True)
        self.assertIn("include=description", transport.calls[0][1])

    def test_search_builds_quoted_encoded_query_from_status_and_priority(self):
        transport = FakeTransport([response(200, {"results": [{"id": 5}], "total": 1})])
        client = FreshdeskClient("acme", "key", transport=transport)
        result = client.search_tickets(status=2, priority=3)
        self.assertEqual(result["results"], [{"id": 5}])
        self.assertFalse(result["truncated"])
        _, url, _, _, _ = transport.calls[0]
        parsed = urllib.parse.urlsplit(url)
        self.assertEqual(parsed.path, "/api/v2/search/tickets")
        query = urllib.parse.parse_qs(parsed.query)["query"][0]
        self.assertEqual(query, '"status:2 AND priority:3"')

    def test_search_requires_at_least_one_filter(self):
        client = FreshdeskClient("acme", "key")
        with self.assertRaisesRegex(ValueError, "at least one"):
            client.search_tickets()

    def test_search_rejects_out_of_range_filters(self):
        client = FreshdeskClient("acme", "key")
        with self.assertRaises(ValueError):
            client.search_tickets(status=6)
        with self.assertRaises(ValueError):
            client.search_tickets(priority=0)

    def test_search_truncates_when_page_cap_is_exhausted(self):
        page = [{"id": 1}]
        transport = FakeTransport([response(200, {"results": page, "total": 200}) for _ in range(10)])
        client = FreshdeskClient("acme", "key", transport=transport, per_page=1)
        result = client.search_tickets(status=2)
        self.assertEqual(len(result["results"]), 10)
        self.assertTrue(result["truncated"])

    def test_private_note_is_explicit_and_verified_by_readback(self):
        created = {"id": 99, "ticket_id": 7, "private": True, "body_text": "Handled"}
        conversations = [created]
        transport = FakeTransport([response(201, created), response(200, conversations)])
        client = FreshdeskClient("acme", "key", transport=transport)
        result = client.add_private_note(7, "Handled")
        sent = json.loads(transport.calls[0][3].decode())
        self.assertEqual(sent, {"body": "<div>Handled</div>", "private": True})
        self.assertEqual(result["id"], 99)
        self.assertEqual(transport.calls[1][0], "GET")

    def test_private_note_readback_body_must_match(self):
        created = {"id": 99, "ticket_id": 7, "private": True, "body_text": "Handled"}
        changed = {**created, "body_text": "Changed"}
        transport = FakeTransport([response(201, created), response(200, [changed])])
        client = FreshdeskClient("acme", "key", transport=transport)
        with self.assertRaises(FreshdeskWriteUncertainError) as caught:
            client.add_private_note(7, "Handled")
        self.assertEqual(caught.exception.created_id, 99)

    def test_private_note_is_plain_text_and_escapes_html(self):
        created = {"id": 99, "ticket_id": 7, "private": True, "body_text": "<b>safe</b>"}
        transport = FakeTransport([response(201, created), response(200, [created])])
        client = FreshdeskClient("acme", "key", transport=transport)
        client.add_private_note(7, "<b>safe</b>")
        sent = json.loads(transport.calls[0][3].decode())
        self.assertEqual(sent["body"], "<div>&lt;b&gt;safe&lt;/b&gt;</div>")

    def test_private_note_post_400_is_a_definite_failure(self):
        transport = FakeTransport([response(400, {"message": "bad request"})])
        client = FreshdeskClient("acme", "key", transport=transport)
        with self.assertRaises(FreshdeskError) as caught:
            client.add_private_note(7, "Handled")
        self.assertNotIsInstance(caught.exception, FreshdeskWriteUncertainError)

    def test_create_ticket_is_verified_by_get_with_requester_email_and_text_description(self):
        created = {"id": 12, "subject": "Streetlight"}
        readback = {
            "id": 12, "subject": "Streetlight", "priority": 1, "status": 2,
            "description": "<div>Out</div>", "description_text": "Out",
            "requester": {"email": "Primary-Contact"},
        }
        transport = FakeTransport([response(201, created), response(200, readback)])
        client = FreshdeskClient("acme", "key", transport=transport)
        result = client.create_ticket({
            "subject": "Streetlight", "email": "primary-contact",
            "description": "Out", "priority": 1, "status": 2,
        })
        self.assertEqual(result, readback)
        self.assertEqual(transport.calls[1][1], "https://acme.freshdesk.com/api/v2/tickets/12?include=requester")

    def test_create_ticket_readback_is_case_insensitive_on_email_and_normalizes_newlines(self):
        created = {"id": 12, "subject": "Streetlight"}
        readback = {
            "id": 12, "subject": "Streetlight", "priority": 1, "status": 2,
            "description": "<div>Out\r\nMore</div>", "description_text": "Out\r\nMore",
            "requester": {"email": "Primary-Contact"},
        }
        transport = FakeTransport([response(201, created), response(200, readback)])
        client = FreshdeskClient("acme", "key", transport=transport)
        result = client.create_ticket({
            "subject": "Streetlight", "email": "primary-contact",
            "description": "Out\nMore", "priority": 1, "status": 2,
        })
        self.assertEqual(result["id"], 12)

    def test_create_ticket_readback_fields_must_match(self):
        created = {"id": 12, "subject": "Streetlight"}
        changed = {
            "id": 12, "subject": "Different", "description": "<div>Out</div>",
            "description_text": "Out", "priority": 1, "status": 2,
            "requester": {"email": "primary-contact"},
        }
        transport = FakeTransport([response(201, created), response(200, changed)])
        client = FreshdeskClient("acme", "key", transport=transport)
        with self.assertRaises(FreshdeskWriteUncertainError) as caught:
            client.create_ticket({
                "subject": "Streetlight", "email": "primary-contact",
                "description": "Out", "priority": 1, "status": 2,
            })
        self.assertEqual(caught.exception.created_id, 12)

    def test_create_ticket_rejects_unverified_extra_fields(self):
        client = FreshdeskClient("acme", "key")
        with self.assertRaisesRegex(ValueError, "unsupported fields"):
            client.create_ticket({
                "subject": "Streetlight", "email": "primary-contact", "description": "Out",
                "priority": 1, "status": 2, "group_id": 99,
            })

    def test_create_ticket_requires_exactly_one_requester_identity(self):
        client = FreshdeskClient("acme", "key")
        base = {"subject": "Streetlight", "description": "Out", "priority": 1, "status": 2}
        with self.assertRaisesRegex(ValueError, "exactly one"):
            client.create_ticket(base)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            client.create_ticket({**base, "email": "primary-contact", "requester_id": 9})

    def test_create_ticket_post_400_is_a_definite_failure(self):
        transport = FakeTransport([response(400, {"message": "bad request"})])
        client = FreshdeskClient("acme", "key", transport=transport)
        with self.assertRaises(FreshdeskError) as caught:
            client.create_ticket({
                "subject": "Streetlight", "email": "primary-contact",
                "description": "Out", "priority": 1, "status": 2,
            })
        self.assertNotIsInstance(caught.exception, FreshdeskWriteUncertainError)

    def test_create_ticket_post_422_is_a_definite_failure(self):
        transport = FakeTransport([response(422, {"message": "unprocessable"})])
        client = FreshdeskClient("acme", "key", transport=transport)
        with self.assertRaises(FreshdeskError) as caught:
            client.create_ticket({
                "subject": "Streetlight", "email": "primary-contact",
                "description": "Out", "priority": 1, "status": 2,
            })
        self.assertNotIsInstance(caught.exception, FreshdeskWriteUncertainError)

    def test_create_ticket_post_500_is_an_ambiguous_write(self):
        transport = FakeTransport([response(500, {"message": "boom"})])
        client = FreshdeskClient("acme", "key", transport=transport)
        with self.assertRaises(FreshdeskWriteUncertainError) as caught:
            client.create_ticket({
                "subject": "Streetlight", "email": "primary-contact",
                "description": "Out", "priority": 1, "status": 2,
            })
        self.assertIsNone(caught.exception.created_id)

    def test_download_uses_attachment_url_from_ticket_and_writes_mode_0600_without_auth(self):
        url = "https://s3.amazonaws.com/cdn.freshdesk.com/report.pdf?X-Amz-Signature=abc"
        ticket = _ticket(5, [_attachment(7, url, "report.pdf")])
        transport = FakeTransport([response(200, ticket), response(200, b"DATA")])
        client = FreshdeskClient("acme", "key", transport=transport, max_attachment_bytes=1024)
        with tempfile.TemporaryDirectory() as tmp:
            old = os.umask(0)
            try:
                path = client.download_attachment(5, 7, Path(tmp))
            finally:
                os.umask(old)
            self.assertEqual(path.name, "report.pdf")
            self.assertEqual(path.read_bytes(), b"DATA")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(len(transport.calls), 2)
        self.assertIn("Authorization", transport.calls[0][2])
        self.assertNotIn("Authorization", transport.calls[1][2])
        self.assertEqual(transport.calls[1][1], url)

    def test_download_rejects_same_origin_non_attachment_path_without_calling_transport_for_it(self):
        ticket = _ticket(5, [_attachment(7, "https://acme.freshdesk.com/api/v2/agents", "x.pdf")])
        transport = FakeTransport([response(200, ticket)])
        client = FreshdeskClient("acme", "key", transport=transport)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                client.download_attachment(5, 7, Path(tmp))
        self.assertFalse(any("/api/v2/agents" in call[1] for call in transport.calls))

    def test_download_rejects_traversal_path_without_calling_transport(self):
        url = "https://acme.freshdesk.com/helpdesk/attachments/../../api/v2/agents"
        ticket = _ticket(5, [_attachment(7, url, "x.pdf")])
        transport = FakeTransport([response(200, ticket)])
        client = FreshdeskClient("acme", "key", transport=transport)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                client.download_attachment(5, 7, Path(tmp))
        self.assertFalse(any("/api/v2/agents" in call[1] for call in transport.calls))
        self.assertFalse(any("helpdesk/attachments" in call[1] for call in transport.calls))

    def test_download_rejects_signed_s3_on_other_bucket(self):
        ticket = _ticket(5, [_attachment(7, "https://s3.amazonaws.com/other-bucket/file?X-Amz-Signature=x", "y.pdf")])
        transport = FakeTransport([response(200, ticket)])
        client = FreshdeskClient("acme", "key", transport=transport)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                client.download_attachment(5, 7, Path(tmp))
        self.assertFalse(any("other-bucket" in call[1] for call in transport.calls))

    def test_download_rejects_untrusted_attachment_url(self):
        ticket = _ticket(5, [_attachment(7, "https://evil.test/file", "x.pdf")])
        transport = FakeTransport([response(200, ticket)])
        client = FreshdeskClient("acme", "key", transport=transport)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                client.download_attachment(5, 7, Path(tmp))
        self.assertFalse(any("evil.test" in call[1] for call in transport.calls))

    def test_download_rejects_oversize_body(self):
        url = "https://acme.freshdesk.com/helpdesk/attachments/x"
        ticket = _ticket(5, [_attachment(7, url, "x.pdf")])
        transport = FakeTransport([response(200, ticket), response(200, b"12345")])
        client = FreshdeskClient("acme", "key", transport=transport, max_attachment_bytes=4)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(FreshdeskError, "size limit"):
                client.download_attachment(5, 7, Path(tmp))

    def test_download_rejects_unsigned_s3_and_existing_destination(self):
        ticket = _ticket(5, [_attachment(7, "https://s3.amazonaws.com/bucket/x?download=1", "x.pdf")])
        transport = FakeTransport([response(200, ticket)])
        client = FreshdeskClient("acme", "key", transport=transport)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                client.download_attachment(5, 7, Path(tmp))
            existing = Path(tmp) / "x.pdf"
            existing.write_bytes(b"keep")
            guarded = FreshdeskClient("acme", "key", transport=FakeTransport([
                response(200, _ticket(5, [_attachment(7, "https://acme.freshdesk.com/helpdesk/attachments/x", "x.pdf")])),
                response(200, b"replace"),
            ]))
            with self.assertRaisesRegex(FreshdeskError, "already exists"):
                guarded.download_attachment(5, 7, Path(tmp))
            self.assertEqual(existing.read_bytes(), b"keep")

    def test_download_rejects_missing_attachment(self):
        transport = FakeTransport([response(200, _ticket(5)), response(200, [])])
        client = FreshdeskClient("acme", "key", transport=transport)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(FreshdeskError, "not found"):
                client.download_attachment(5, 7, Path(tmp))

    def test_download_resolves_attachment_from_conversations(self):
        url = "https://s3.amazonaws.com/cdn.freshdesk.com/c.pdf?X-Amz-Signature=abc"
        ticket = _ticket(5)  # no top-level attachments
        conversation = {"id": 1, "attachments": [_attachment(7, url, "c.pdf")]}
        transport = FakeTransport([
            response(200, ticket),
            response(200, [conversation]),
            response(200, b"DATA"),
        ])
        client = FreshdeskClient("acme", "key", transport=transport, max_attachment_bytes=1024)
        with tempfile.TemporaryDirectory() as tmp:
            path = client.download_attachment(5, 7, Path(tmp))
            self.assertEqual(path.read_bytes(), b"DATA")
        self.assertNotIn("Authorization", transport.calls[2][2])

    @unittest.skipIf(not hasattr(os, "symlink"), "symlinks unavailable")
    def test_download_does_not_follow_destination_symlink(self):
        url = "https://acme.freshdesk.com/helpdesk/attachments/x"
        transport = FakeTransport([
            response(200, _ticket(5, [_attachment(1, url, "x.pdf")])),
            response(200, b"replace"),
        ])
        client = FreshdeskClient("acme", "key", transport=transport)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.write_bytes(b"keep")
            os.symlink(target, Path(tmp) / "x.pdf")
            with self.assertRaises(FreshdeskError):
                client.download_attachment(5, 1, Path(tmp))
            self.assertEqual(target.read_bytes(), b"keep")

    @unittest.skipIf(not hasattr(os, "symlink"), "symlinks unavailable")
    def test_download_rejects_symlinked_destination_directory(self):
        url = "https://acme.freshdesk.com/helpdesk/attachments/x"
        transport = FakeTransport([
            response(200, _ticket(5, [_attachment(1, url, "x.pdf")])),
            response(200, b"replace"),
        ])
        client = FreshdeskClient("acme", "key", transport=transport)
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "outside"
            outside.mkdir()
            linked = Path(tmp) / "linked"
            os.symlink(outside, linked)
            with self.assertRaises(FreshdeskError):
                client.download_attachment(5, 1, linked)
            self.assertFalse((outside / "x.pdf").exists())

    def test_reply_posts_escaped_html_and_verifies_readback(self):
        created = {"id": 50, "body_text": "Hello world"}
        conversation = {"id": 50, "private": False, "incoming": False, "body_text": "Hello world"}
        transport = FakeTransport([response(201, created), response(200, [conversation])])
        client = FreshdeskClient("acme", "key", transport=transport)
        result = client.reply_to_ticket(7, "Hello world")
        sent = json.loads(transport.calls[0][3].decode())
        self.assertEqual(sent, {"body": "<div>Hello world</div>"})
        self.assertEqual(transport.calls[0][0], "POST")
        self.assertEqual(transport.calls[0][1], "https://acme.freshdesk.com/api/v2/tickets/7/reply")
        self.assertEqual(result["id"], 50)
        self.assertEqual(transport.calls[1][0], "GET")

    def test_reply_escapes_html_in_body(self):
        created = {"id": 50, "body_text": "<b>safe</b>"}
        conversation = {"id": 50, "private": False, "incoming": False, "body_text": "<b>safe</b>"}
        transport = FakeTransport([response(201, created), response(200, [conversation])])
        client = FreshdeskClient("acme", "key", transport=transport)
        client.reply_to_ticket(7, "<b>safe</b>")
        sent = json.loads(transport.calls[0][3].decode())
        self.assertEqual(sent["body"], "<div>&lt;b&gt;safe&lt;/b&gt;</div>")

    def test_reply_converts_newlines_to_br(self):
        created = {"id": 50, "body_text": "Line1\nLine2"}
        conversation = {"id": 50, "private": False, "incoming": False, "body_text": "Line1\nLine2"}
        transport = FakeTransport([response(201, created), response(200, [conversation])])
        client = FreshdeskClient("acme", "key", transport=transport)
        client.reply_to_ticket(7, "Line1\nLine2")
        sent = json.loads(transport.calls[0][3].decode())
        self.assertEqual(sent["body"], "<div>Line1<br>Line2</div>")

    def test_reply_readback_requires_private_false(self):
        created = {"id": 50, "body_text": "Hello world"}
        conversation = {"id": 50, "private": True, "incoming": False, "body_text": "Hello world"}
        transport = FakeTransport([response(201, created), response(200, [conversation])])
        client = FreshdeskClient("acme", "key", transport=transport)
        with self.assertRaises(FreshdeskWriteUncertainError) as caught:
            client.reply_to_ticket(7, "Hello world")
        self.assertEqual(caught.exception.created_id, 50)

    def test_reply_post_400_is_a_definite_failure(self):
        transport = FakeTransport([response(400, {"message": "bad request"})])
        client = FreshdeskClient("acme", "key", transport=transport)
        with self.assertRaises(FreshdeskError) as caught:
            client.reply_to_ticket(7, "Hello")
        self.assertNotIsInstance(caught.exception, FreshdeskWriteUncertainError)

    def test_time_entry_sends_timer_running_false_and_billable(self):
        created = {"id": 30, "ticket_id": 7, "time_spent": "01:30", "note": "Did work", "billable": True, "timer_running": False}
        transport = FakeTransport([response(201, created), response(200, [created])])
        client = FreshdeskClient("acme", "key", transport=transport)
        result = client.add_time_entry(7, "01:30", "Did work", True)
        sent = json.loads(transport.calls[0][3].decode())
        self.assertEqual(sent["timer_running"], False)
        self.assertEqual(sent["billable"], True)
        self.assertNotIn("start_time", sent)
        self.assertEqual(result["id"], 30)

    def test_time_entry_sends_caller_billable_false(self):
        created = {"id": 30, "ticket_id": 7, "time_spent": "02:00", "note": "Work", "billable": False, "timer_running": False}
        transport = FakeTransport([response(201, created), response(200, [created])])
        client = FreshdeskClient("acme", "key", transport=transport)
        client.add_time_entry(7, "02:00", "Work", False)
        sent = json.loads(transport.calls[0][3].decode())
        self.assertEqual(sent["billable"], False)
        self.assertEqual(sent["timer_running"], False)
        self.assertNotIn("start_time", sent)

    def test_time_entry_sends_agent_id_when_provided(self):
        created = {"id": 30, "ticket_id": 7, "time_spent": "01:00", "note": "Work", "billable": True, "timer_running": False}
        transport = FakeTransport([response(201, created), response(200, [created])])
        client = FreshdeskClient("acme", "key", transport=transport)
        client.add_time_entry(7, "01:00", "Work", True, agent_id=5)
        sent = json.loads(transport.calls[0][3].decode())
        self.assertEqual(sent["agent_id"], 5)
        self.assertNotIn("start_time", sent)

    def test_time_entry_omits_agent_id_when_not_provided(self):
        created = {"id": 30, "ticket_id": 7, "time_spent": "01:00", "note": "Work", "billable": True, "timer_running": False}
        transport = FakeTransport([response(201, created), response(200, [created])])
        client = FreshdeskClient("acme", "key", transport=transport)
        client.add_time_entry(7, "01:00", "Work", True)
        sent = json.loads(transport.calls[0][3].decode())
        self.assertNotIn("agent_id", sent)
        self.assertNotIn("start_time", sent)

    def test_time_entry_missing_or_zero_time_spent_does_not_call_transport(self):
        transport = FakeTransport([])
        client = FreshdeskClient("acme", "key", transport=transport)
        with self.assertRaises(ValueError):
            client.add_time_entry(7, None, "note", True)
        self.assertEqual(len(transport.calls), 0)
        with self.assertRaises(ValueError):
            client.add_time_entry(7, "0:01", "note", True)
        self.assertEqual(len(transport.calls), 0)
        with self.assertRaises(ValueError):
            client.add_time_entry(7, "0:00", "note", True)
        self.assertEqual(len(transport.calls), 0)
        with self.assertRaises(ValueError):
            client.add_time_entry(7, "00:00", "note", True)
        self.assertEqual(len(transport.calls), 0)

    def test_time_entry_invalid_time_spent_does_not_call_transport(self):
        transport = FakeTransport([])
        client = FreshdeskClient("acme", "key", transport=transport)
        for bad in ("1:60", "100000:00", "1:30", "1:0", "abc"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    client.add_time_entry(7, bad, "note", True)
        self.assertEqual(len(transport.calls), 0)

    def test_time_entry_post_400_is_a_definite_failure(self):
        transport = FakeTransport([response(400, {"message": "bad request"})])
        client = FreshdeskClient("acme", "key", transport=transport)
        with self.assertRaises(FreshdeskError) as caught:
            client.add_time_entry(7, "01:30", "note", True)
        self.assertNotIsInstance(caught.exception, FreshdeskWriteUncertainError)

    def test_time_entry_readback_mismatch_is_ambiguous(self):
        created = {"id": 30, "time_spent": "01:30", "note": "Did work", "billable": True, "timer_running": False}
        readback = {"id": 30, "ticket_id": 7, "time_spent": "2:00", "note": "Did work", "billable": True, "timer_running": False}
        transport = FakeTransport([response(201, created), response(200, [readback])])
        client = FreshdeskClient("acme", "key", transport=transport)
        with self.assertRaises(FreshdeskWriteUncertainError) as caught:
            client.add_time_entry(7, "01:30", "Did work", True)
        self.assertEqual(caught.exception.created_id, 30)

    def test_time_entry_readback_uses_ticket_list_not_singular_get(self):
        created = {"id": 30, "ticket_id": 7, "time_spent": "00:01", "note": "Did work", "billable": True, "timer_running": False}
        transport = FakeTransport([response(201, created), response(200, [created])])
        client = FreshdeskClient("acme", "key", transport=transport)
        result = client.add_time_entry(7, "00:01", "Did work", True)
        sent = json.loads(transport.calls[0][3].decode())
        self.assertEqual(sent["time_spent"], "00:01")
        self.assertEqual(sent["billable"], True)
        self.assertEqual(sent["timer_running"], False)
        self.assertNotIn("start_time", sent)
        self.assertEqual(transport.calls[1][0], "GET")
        parsed = urllib.parse.urlsplit(transport.calls[1][1])
        self.assertEqual(parsed.path, "/api/v2/tickets/7/time_entries")
        self.assertFalse(any("/api/v2/time_entries/" in call[1] for call in transport.calls))
        self.assertEqual(result, created)

    def test_time_entry_missing_from_readback_list_is_ambiguous(self):
        created = {"id": 30, "ticket_id": 7, "time_spent": "00:01", "note": "Did work", "billable": True, "timer_running": False}
        other = {"id": 99, "ticket_id": 7, "time_spent": "00:05", "note": "Other", "billable": False, "timer_running": False}
        transport = FakeTransport([response(201, created), response(200, [other])])
        client = FreshdeskClient("acme", "key", transport=transport)
        with self.assertRaises(FreshdeskWriteUncertainError) as caught:
            client.add_time_entry(7, "00:01", "Did work", True)
        self.assertEqual(caught.exception.created_id, 30)
        posts = [call for call in transport.calls if call[0] == "POST"]
        self.assertEqual(len(posts), 1)

    def test_update_ticket_sends_only_status_and_priority_then_verifies_get(self):
        created = {"id": 1, "status": 2, "priority": 3}
        readback = {"id": 1, "status": 2, "priority": 3, "subject": "test"}
        transport = FakeTransport([response(200, created), response(200, readback)])
        client = FreshdeskClient("acme", "key", transport=transport)
        result = client.update_ticket(1, status=2, priority=3)
        sent = json.loads(transport.calls[0][3].decode())
        self.assertEqual(sent, {"status": 2, "priority": 3})
        self.assertEqual(transport.calls[0][0], "PUT")
        self.assertEqual(transport.calls[1][0], "GET")
        self.assertEqual(transport.calls[1][1], "https://acme.freshdesk.com/api/v2/tickets/1")
        self.assertEqual(result, readback)

    def test_update_ticket_with_only_status(self):
        created = {"id": 1, "status": 2}
        readback = {"id": 1, "status": 2, "priority": 3}
        transport = FakeTransport([response(200, created), response(200, readback)])
        client = FreshdeskClient("acme", "key", transport=transport)
        result = client.update_ticket(1, status=2)
        sent = json.loads(transport.calls[0][3].decode())
        self.assertEqual(sent, {"status": 2})
        self.assertNotIn("priority", sent)
        self.assertEqual(result["status"], 2)

    def test_update_ticket_with_only_priority(self):
        created = {"id": 1, "priority": 3}
        readback = {"id": 1, "status": 2, "priority": 3}
        transport = FakeTransport([response(200, created), response(200, readback)])
        client = FreshdeskClient("acme", "key", transport=transport)
        result = client.update_ticket(1, priority=3)
        sent = json.loads(transport.calls[0][3].decode())
        self.assertEqual(sent, {"priority": 3})
        self.assertNotIn("status", sent)
        self.assertEqual(result["priority"], 3)

    def test_update_rejects_extra_body_field_without_transport_call(self):
        transport = FakeTransport([])
        client = FreshdeskClient("acme", "key", transport=transport)
        with self.assertRaises(TypeError):
            client.update_ticket(1, status=2, subject="whatever")
        self.assertEqual(len(transport.calls), 0)

    def test_update_requires_at_least_one_of_status_or_priority(self):
        transport = FakeTransport([])
        client = FreshdeskClient("acme", "key", transport=transport)
        with self.assertRaisesRegex(ValueError, "at least one"):
            client.update_ticket(1)
        self.assertEqual(len(transport.calls), 0)

    def test_update_readback_mismatch_is_ambiguous(self):
        created = {"id": 1, "status": 2, "priority": 3}
        readback = {"id": 1, "status": 3, "priority": 3}
        transport = FakeTransport([response(200, created), response(200, readback)])
        client = FreshdeskClient("acme", "key", transport=transport)
        with self.assertRaises(FreshdeskWriteUncertainError) as caught:
            client.update_ticket(1, status=2)
        self.assertEqual(caught.exception.created_id, 1)

    def test_update_put_400_is_a_definite_failure(self):
        transport = FakeTransport([response(400, {"message": "bad request"})])
        client = FreshdeskClient("acme", "key", transport=transport)
        with self.assertRaises(FreshdeskError) as caught:
            client.update_ticket(1, status=2)
        self.assertNotIsInstance(caught.exception, FreshdeskWriteUncertainError)

    def test_search_builds_query_with_tag_type_agent_group_and_date_range(self):
        transport = FakeTransport([response(200, {"results": [], "total": 0})])
        client = FreshdeskClient("acme", "key", transport=transport)
        client.search_tickets(
            tag="Alpha", type="Question", agent_id=9, group_id=4,
            created_after="2026-10-01", created_before="2026-10-20",
        )
        _, url, _, _, _ = transport.calls[0]
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parsed.query)["query"][0]
        self.assertIn("tag:'Alpha'", query)
        self.assertIn("type:'Question'", query)
        self.assertIn("agent_id:9", query)
        self.assertIn("group_id:4", query)
        self.assertIn("created_at:>'2026-10-01'", query)
        self.assertIn("created_at:<'2026-10-20'", query)

    def test_search_rejects_tag_with_quote(self):
        client = FreshdeskClient("acme", "key")
        with self.assertRaisesRegex(ValueError, "invalid characters"):
            client.search_tickets(tag="Alpha'")

    def test_search_rejects_invalid_date(self):
        client = FreshdeskClient("acme", "key")
        with self.assertRaisesRegex(ValueError, "real calendar date"):
            client.search_tickets(created_after="2026-02-30")
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            client.search_tickets(created_after="2026/10/01")

    def test_search_rejects_date_range_where_after_is_not_before(self):
        client = FreshdeskClient("acme", "key")
        with self.assertRaisesRegex(ValueError, "must be before"):
            client.search_tickets(created_after="2026-10-20", created_before="2026-10-01")


class PluginRegistrationTests(unittest.TestCase):
    def test_registers_expected_tool_surface(self):
        class Context:
            def __init__(self):
                self.tools = []
                self.hooks = []

            def register_tool(self, name, *, schema, handler, check_fn=None, requires_env=None,
                              description=None, emoji=None, **kwargs):
                self.tools.append({"name": name, "schema": schema, "handler": handler,
                                   "check_fn": check_fn, "requires_env": requires_env})

            def register_hook(self, name, callback):
                self.hooks.append((name, callback))

        ctx = Context()
        freshdesk_connector.register(ctx)
        names = {item["name"] for item in ctx.tools}
        self.assertEqual(names, {
            "freshdesk_list_tickets", "freshdesk_get_ticket",
            "freshdesk_search_tickets", "freshdesk_list_conversations",
            "freshdesk_get_contact", "freshdesk_download_attachment",
            "freshdesk_add_private_note", "freshdesk_create_ticket",
            "freshdesk_reply_to_ticket", "freshdesk_add_time_entry",
            "freshdesk_update_ticket",
        })
        for item in ctx.tools:
            self.assertEqual(item["check_fn"], freshdesk_connector._available)
            self.assertEqual(item["requires_env"], ["FRESHDESK_DOMAIN", "FRESHDESK_API_KEY"])
        self.assertEqual(len(ctx.hooks), 1)
        name, hook = ctx.hooks[0]
        self.assertEqual(name, "pre_tool_call")
        self.assertIsNone(hook(tool_name="freshdesk_get_ticket", args={}))
        directive = hook(tool_name="freshdesk_add_private_note", args={"ticket_id": 7})
        self.assertEqual(directive["action"], "approve")
        self.assertIn("private note", directive["message"])
        directive = hook(tool_name="freshdesk_create_ticket", args={})
        self.assertEqual(directive["action"], "approve")
        directive = hook(tool_name="freshdesk_reply_to_ticket", args={"ticket_id": 7, "body": "Hello"})
        self.assertEqual(directive["action"], "approve")
        self.assertIn("public reply", directive["message"])
        directive = hook(tool_name="freshdesk_add_time_entry", args={"ticket_id": 7, "time_spent": "1:30", "billable": True})
        self.assertEqual(directive["action"], "approve")
        self.assertIn("time entry", directive["message"])
        self.assertIn("1:30", directive["message"])
        directive = hook(tool_name="freshdesk_update_ticket", args={"ticket_id": 7, "status": 2, "priority": 3})
        self.assertEqual(directive["action"], "approve")
        self.assertIn("7", directive["message"])

    def test_list_tickets_schema_has_no_status(self):
        class Capture:
            def __init__(self):
                self.schemas = {}

            def register_tool(self, name, **kwargs):
                self.schemas[name] = kwargs["schema"]

            def register_hook(self, name, callback):
                pass

        cap = Capture()
        freshdesk_connector.register(cap)

        list_params = cap.schemas["freshdesk_list_tickets"]["parameters"]
        self.assertEqual(list_params["additionalProperties"], False)
        self.assertNotIn("status", list_params["properties"])
        self.assertIn("include_description", list_params["properties"])

        search_params = cap.schemas["freshdesk_search_tickets"]["parameters"]
        self.assertEqual(search_params["additionalProperties"], False)
        self.assertEqual(set(search_params["properties"]), {
            "status", "priority", "tag", "type", "agent_id", "group_id",
            "created_after", "created_before", "updated_after", "updated_before",
        })
        self.assertNotIn("query", search_params["properties"])
        self.assertNotIn("subject", search_params["properties"])
        self.assertNotIn("keyword", search_params["properties"])

        reply_params = cap.schemas["freshdesk_reply_to_ticket"]["parameters"]
        self.assertEqual(reply_params["additionalProperties"], False)
        self.assertEqual(set(reply_params["properties"]), {"ticket_id", "body"})

        time_params = cap.schemas["freshdesk_add_time_entry"]["parameters"]
        self.assertEqual(time_params["additionalProperties"], False)
        self.assertEqual(set(time_params["properties"]), {"ticket_id", "time_spent", "note", "billable", "agent_id"})

        update_params = cap.schemas["freshdesk_update_ticket"]["parameters"]
        self.assertEqual(update_params["additionalProperties"], False)
        self.assertEqual(set(update_params["properties"]), {"ticket_id", "status", "priority"})

        create_params = cap.schemas["freshdesk_create_ticket"]["parameters"]
        fields = create_params["properties"]["fields"]
        self.assertEqual(fields["additionalProperties"], False)
        self.assertEqual(set(fields["properties"]),
                         {"subject", "description", "priority", "status", "email", "requester_id"})


class SecretScopeTests(unittest.TestCase):
    def _restore(self, original):
        freshdesk_connector.get_secret = original

    def test_available_false_when_secret_scope_unavailable(self):
        original = freshdesk_connector.get_secret
        try:
            freshdesk_connector.get_secret = None
            os.environ["FRESHDESK_DOMAIN"] = "your-account"
            os.environ["FRESHDESK_API_KEY"] = "your-api-key"
            self.assertFalse(_available())
        finally:
            self._restore(original)
            os.environ.pop("FRESHDESK_DOMAIN", None)
            os.environ.pop("FRESHDESK_API_KEY", None)

    def test_available_false_when_get_secret_returns_empty_even_with_environ(self):
        original = freshdesk_connector.get_secret
        try:
            freshdesk_connector.get_secret = lambda name, default=None: ""
            os.environ["FRESHDESK_DOMAIN"] = "your-account"
            os.environ["FRESHDESK_API_KEY"] = "your-api-key"
            self.assertFalse(_available())
        finally:
            self._restore(original)
            os.environ.pop("FRESHDESK_DOMAIN", None)
            os.environ.pop("FRESHDESK_API_KEY", None)

    def test_available_true_when_get_secret_returns_values(self):
        original = freshdesk_connector.get_secret
        try:
            values = {"FRESHDESK_DOMAIN": "your-account", "FRESHDESK_API_KEY": "your-api-key"}
            freshdesk_connector.get_secret = lambda name, default=None: values.get(name)
            self.assertTrue(_available())
        finally:
            self._restore(original)


class HandlerResultTests(unittest.TestCase):
    def test_search_tickets_handler_rejects_raw_query_argument(self):
        # The raw-query guard runs before any credentials are read, so this needs
        # no get_secret patching: a rejected query never reaches the client.
        result = _search_tickets({"query": "status:2"})
        data = json.loads(result)
        self.assertFalse(data["success"])
        self.assertIn("raw query", data["error"])

    def test_search_tickets_handler_rejects_subject_and_keyword(self):
        for field in ("subject", "keyword"):
            with self.subTest(field=field):
                result = _search_tickets({field: "printer"})
                data = json.loads(result)
                self.assertFalse(data["success"])
                self.assertIn("does not accept", data["error"])


if __name__ == "__main__":
    unittest.main()
