# Platform architecture

## Single-node, separate households

A central platform.sqlite3 registry holds adult email accounts, hashed sessions,
hashed email/pairing/device secrets, minimal operator audits, and aggregate metrics.
Each verified account has a generated 128-bit ID and its own database under
data/libraries/<id>.sqlite3. Account IDs are never accepted as filesystem paths
from a client. Request ContextVar scoping is fail-closed and propagates into workers.

Existing filtering/progress logic is reused against the scoped database. The
legacy settings password/token columns remain for schema compatibility but cannot
authenticate platform requests. Old parent-password login and shared-token routes
are removed. Per-household Pushover sending is disabled.

This design deliberately avoids a broad tenant-column retrofit to legacy SQL.
It supports a measured single-host launch. One process/worker owns the scheduler.
Horizontal scaling, shared metadata caches/quotas, account sharding, and a
PostgreSQL migration are future capacity work; do not advertise unlimited scale.

## Authentication

Email sign-in requests require adult/policy acceptance. Tokens expire after 15
minutes, are stored only as hashes, bind to a short-lived HttpOnly browser cookie,
and are consumed only by an explicit POST. Email scanners cannot consume a GET.
A seven-day opaque session authenticates the parent. There is no stored password.

Cookies are Secure in production; foreign Origins are rejected on writes.
Rate-limit keys are keyed digests, not retained plaintext addresses/IPs.
Sign-in mail has per-peer, per-address, and global hourly/daily budgets. Configure
the service budgets before inviting more users; they bound transactional-mail abuse.
Production startup requires HTTPS and an owner email. SMTP mode additionally
requires sender configuration; disabled-mail mode closes public registration and
supports expiring, single-use owner links issued by a server administrator.

## TV pairing

The TV displays a random eight-character human code. A separate 256-bit secret
authorizes polling. A logged-in verified parent must explicitly approve the code.
Polling is bounded and rate-limited; codes expire after 10 minutes and redeem
atomically once. Returned credentials are independently revocable and authorize
only household viewing/progress routes. They cannot access parent/owner routes.

## Schedules and data

Multiple channels per weekday, duration/title filters, video blocking, interleaved
daily results, shared household progress, and history replay rules are preserved.
Server timezone determines the schedule day; client display dates use local time.
Rules are checked at launch and child writes, not continuously inside YouTube.

Sync status is per account. Discovery is capped at ten upload pages per refresh;
channel additions and manual syncs have per-account budgets. A shared atomic daily
YouTube metadata reservation budget defaults to 9,000 units. Search has a separate
90-call budget; days follow Pacific time. Configure both against the actual API
project's allocation and current [quota rules](https://developers.google.com/youtube/v3/determine_quota_cost).
Shared metadata caching and hosting capacity still need planning before wider registration.

## Operations

The owner panel exposes email/status/timestamps and device counts, not family
video titles/history. Suspension revokes sessions and device credentials. Only
aggregate request/error counts and CPU/memory/disk samples are retained.
Pushover alerts contain resource percentages, never household data.
