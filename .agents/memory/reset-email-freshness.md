---
name: Password-reset email freshness detection
description: Why JetBrains 改密 timed out despite reset mail arriving, and the robust way to detect a fresh inbound mail
---

# Detecting the "fresh" reset email in poll_email_for_reset (ghostreg/reset_password.py)

**Rule:** To decide which inbound mail belongs to *this* request, snapshot the set of
existing message IDs **before** triggering the action, then accept only IDs that appear
afterward (`known_ids` → seed `seen_ids`). Do NOT gate freshness on parsing the YYDS
message-ID timestamp vs local `time.time()`.

**Why:** The old code compared `_mail_id_to_timestamp(id)` (parsed from the YYDS server
ID, Beijing time) against `since = time.time()` (Replit server clock) with only a 5s
tolerance. Any clock skew between the two servers pushed a freshly-arrived reset mail's
parsed timestamp *before* `since`, so it was discarded as "old" → `等待重置邮件超时` even
though the mail was sitting in the box. Cross-server timestamp comparison is inherently
fragile; ID-set diffing is clock-independent.

**How to apply:** Keep the timestamp check only as a loose backstop (drop mail older than
~10 min). The real "new vs old" decision is the pre-request ID snapshot.

## Other durable facts about this 改密 flow / YYDS
- YYDS `/me/inboxes` `messageCount` is **stale for deactivated inboxes** (often shows 0)
  even when `GET /v1/messages?address=` actually returns stored mail. Trust the messages
  endpoint, not the count.
- Deactivated YYDS inboxes **cannot** be recreated (`409 address_already_in_use`) or
  reactivated/claimed (`409 inbox_is_no_longer_active`) — but they **still receive and
  store new inbound mail**, readable via API key + `?address=`. So 改密 works on them.
- YYDS appears to keep only the latest ~2-3 messages per inbox.
- JetBrains rate-limits `forgot-password`: rapid bulk requests stop producing new reset
  mails, so later accounts in a big batch legitimately time out. Recovery should use
  small batches with longer gaps, not 100 back-to-back.
