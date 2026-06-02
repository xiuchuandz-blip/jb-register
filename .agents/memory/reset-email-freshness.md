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

## JetBrains 风控:登录验证码邮件会污染改密轮询
- JetBrains 风控分上升后,除"Reset your ... password"重置邮件外,还会单独发
  "Confirmation code to log in to your JetBrains Account"登录验证码邮件。
- 改密轮询若对每封新邮件先找链接、再找 6 位码并"首个命中即返回",登录验证码邮件
  可能排在重置邮件前被误当成重置 OTP → 改密用错码失败。
- 解法:OTP 提取前按 subject 排除登录类邮件(含 "log in"/"login"/"sign in" 且不含
  "reset")。重置链接最可靠(只有重置邮件含 /reset-password/<token>),应始终优先。

## 改密批次必须有真正的取消
- 早期"取消"按钮只关弹窗,后台批次(_reset_running)直到全部跑完才释放 → 用户卡死、
  无法发起新批次。正确做法:取消标志 + 端点,循环逐账号检查(未开始的标记已取消跳过),
  并把 cancel_cb 透传进 poll,使正在进行的轮询能即时中断(抛 ResetCancelled)。
