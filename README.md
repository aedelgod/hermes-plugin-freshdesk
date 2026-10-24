# Freshdesk connector for Hermes Agent

**Author:** GFORGOV®. Copyright (c) 2026 GFORGOV®. Released under the MIT License.

A dependency-free directory plugin that exposes Freshdesk API v2 tools to Hermes
Agent. It performs bounded pagination, bounded downloads, strict origin checks,
and exact readback verification of every write. The connector is generic: it
carries no tenant configuration, credentials, or ticket data.

## Included tools

- `freshdesk_list_tickets` — bounded ticket pagination with `updated_since` and an `include_description` option. Returns `truncated` rather than discarding pages when the page cap is reached.
- `freshdesk_get_ticket` — fetch one ticket by ID.
- `freshdesk_search_tickets` — read-only search by `status` (2-5), `priority` (1-4), `tag`, `type`, `agent_id`, `group_id`, or `created_at`/`updated_at` date ranges. At least one filter is required; the query is built and URL-encoded by the client.
- `freshdesk_list_conversations` — fetch a bounded complete conversation list.
- `freshdesk_get_contact` — fetch one contact by ID.
- `freshdesk_download_attachment` — resolve an attachment by ticket and attachment ID, then download it from the Freshdesk CDN/S3 origin to the Hermes cache. No caller-controlled URL.
- `freshdesk_add_private_note` — write a plain-text private note (HTML-escaped), then verify it through conversations.
- `freshdesk_create_ticket` — create a basic ticket from plain text. The description is HTML-escaped before it is sent, then verified against `description_text`.
- `freshdesk_reply_to_ticket` — send a public reply to the requester of a ticket. The approval Reason includes the reply text.
- `freshdesk_add_time_entry` — add a finished (non-timer) time entry. Callers may pass `H:MM` through `HHHH:MM`; the client stores the form Freshdesk keeps (`1:30` and `000:01` are sent as `01:30` and `00:01`). Zero is rejected, including padded zeros.
- `freshdesk_update_ticket` — update a ticket's `status` (2-5) and/or `priority` (1-4).

The five write tools (`freshdesk_add_private_note`, `freshdesk_create_ticket`, `freshdesk_reply_to_ticket`, `freshdesk_add_time_entry`, and `freshdesk_update_ticket`) register a `pre_tool_call` hook that escalates execution to Hermes' trusted human-approval gate. The language model cannot satisfy this gate with an ordinary tool argument, and unattended contexts without an approval bridge fail closed. The approval Reason reads back the change that will be written: the reply or note text, the description, the canonical duration, and the status or priority from-to line. A newline in a field is collapsed, so it cannot hide that change. Hermes still owns the human gate, and the headline remains the tool name.

## Configuration

Provide credentials and account identity as profile secrets named exactly as the
plugin declares:

```
FRESHDESK_DOMAIN=your-account
FRESHDESK_API_KEY=your-api-key
```

`FRESHDESK_DOMAIN` accepts either `your-account` or `https://your-account.freshdesk.com`. Custom support CNAMEs are rejected because credentials must only be sent to a native Freshdesk HTTPS origin. The plugin reads these through Hermes' profile secret scope, so they are never read directly from the process environment by plugin code.

## Install as a user plugin

Copy this directory into the active Hermes profile:

```bash
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
mkdir -p "$HERMES_HOME/plugins"
cp -R . "$HERMES_HOME/plugins/freshdesk"
```

Restart the relevant Hermes process so plugin discovery runs again.

## Tests

The suite uses an injected fake HTTP transport and never contacts a live server:

```bash
python3 -m unittest discover -s tests -v
```

## Security properties

- Native `https://<account>.freshdesk.com` account origins only.
- Basic-auth credentials are never placed in URLs or tool results.
- Redirects are rejected.
- Response bodies and attachment downloads are bounded while being read.
- Attachment downloads are resolved only from a ticket's own attachment objects (the ticket, then its conversations); no caller-supplied URL is ever fetched.
- Attachment URLs are accepted only from Freshdesk's same-origin attachment path (`/helpdesk/attachments/`), `attachment.freshdesk.com`, hosts matching `[a-z0-9-]+-attachment.freshdesk.com`, `s3.amazonaws.com/cdn.freshdesk.com/`, and `cdn.freshdesk.com.s3*.amazonaws.com`. Dummy signatures on any other S3 bucket are rejected.
- The Freshdesk Authorization header is sent only to the same-origin attachment path; it is never sent to the Freshdesk CDN or to S3.
- Attachment filenames are reduced to their basename, prefixed with the attachment id, created mode `0600`, exclusively and without following symlinks, inside a directory opened component-by-component without symlink traversal.
- Write tools pass through Hermes' human-approval gate and are read back before success is returned.
- No automatic retries of POST requests. Ambiguous writes return `state=write_may_have_succeeded` with `created_id` when known and `retry_safe=false`; definite rejections (HTTP 400/401/403/404/409/422) return a plain failure.

## Deliberate field limits

- `freshdesk_download_attachment` takes only `ticket_id` and `attachment_id`; the URL is always derived from Freshdesk.
- `freshdesk_create_ticket` accepts only `subject`, `description`, `priority`, `status`, and exactly one of `email` or `requester_id`.
- `freshdesk_reply_to_ticket` accepts only `ticket_id` and `body`.
- `freshdesk_add_time_entry` accepts only `ticket_id`, `time_spent`, `note`, `billable`, and `agent_id`.
- `freshdesk_update_ticket` accepts only `ticket_id`, `status`, and `priority`.
- `freshdesk_search_tickets` accepts only `status`, `priority`, `tag`, `type`, `agent_id`, `group_id`, `created_after`, `created_before`, `updated_after`, and `updated_before`; it never accepts a raw query string. Subject, description, and keyword search are intentionally unsupported and rejected if added to the call.
