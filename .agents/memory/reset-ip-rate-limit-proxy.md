---
name: Password-reset trigger must go through CF Worker proxy
description: Why forgot-password "can't receive email" is usually IP throttling, and which JetBrains calls must be proxied vs direct.
---

# Forgot-password rate limiting and proxy routing

JetBrains throttles `GET /forgot-password` + `POST /request-password-reset` by
**source IP** (and additionally per-account). From Replit every request shares one
egress IP, so a burst of reset triggers gets silently throttled: the POST still
returns HTTP 200 but **no reset email is sent**. Symptom looks like "根本接不到邮件".

**Rule:** route the rate-limited trigger calls through the same CF Worker proxy that
registration uses (rotating egress IPs), not a plain direct session.

**Why:** registration already worked because it goes through the Worker; the reset
path was the only flow hitting JetBrains from the bare Replit IP.

**How to apply:**
- The trigger session (GET forgot-password, POST request-password-reset, OTP submit)
  must use a proxied session when CF proxy is enabled; the *completion* step
  (`complete_reset_via_link`) stays on the direct session because it needs the final
  redirected URL and is not rate-limited.
- A 200 from the POST does NOT mean an email was sent. Confirm by polling the inbox.

## SimpleWorkerSession form-data gotcha
The transparent `?__target=` SimpleWorkerSession originally encoded dict `data` with
`str(data).encode()` (Python repr) → JetBrains returned **HTTP 400** on the form POST.
Registration never hit this because it only sends `json=`. Any form POST (e.g.
forgot-password `data={"username": ...}`) must be URL-encoded
(`application/x-www-form-urlencoded`). Cookies (`_st-JBA`, `JSESSIONID-JBA`) DO flow
correctly GET→POST through the transparent worker, so CSRF `_st` validates fine.

## Domain deliverability pattern (account-specific, not a code bug)
Multi-level subdomain YYDS mailboxes (e.g. `x@sub.domain.tld`, 3+ labels) consistently
do NOT receive the JetBrains reset email even when the proxied trigger returns 200 —
including freshly-created, never-triggered ones. Plain 2-label domains
(e.g. `x@lyux39qw.cn`) receive it in ~5s. When a reset times out, check the domain
depth before assuming a code/throttle problem.
