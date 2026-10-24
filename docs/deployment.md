# Production deployment

Deploy to a host with persistent storage and HTTPS. Keep real credentials, runtime
libraries, and host-specific migration reports outside the source repository.

## Configuration

Use server/.env.example as the production environment. Set DAILYTELLY_ENV=production,
DAILYTELLY_PUBLIC_URL=https://thedailytelly.com, and
DAILYTELLY_OWNER_EMAILS=hello@thedailytelly.com. SMTP requires host, TLS port (587
STARTTLS or 465 implicit TLS), sender, and provider credentials as appropriate.
Never paste credentials into issues/chat or commit environment files.

For Resend SMTP, first verify the sending domain in the Resend dashboard. Use
smtp.resend.com, port 465 with implicit TLS, username resend, and a sending-only
API key as DAILYTELLY_SMTP_PASSWORD. Set DAILYTELLY_MAIL_FROM to an address on the
verified domain. Keep open/click tracking disabled in the domain configuration;
application sign-in messages contain plain text only, with no HTML or pixels.
Keep mail mode disabled until the sender is accepted and delivery is tested.

The public /contact page has a form and a direct email address. Contact form
messages go only to DAILYTELLY_CONTACT_EMAIL (hello@thedailytelly.com by default),
with the visitor's address in Reply-To. No automatic reply goes to an unverified
visitor. Messages are plain text and are not stored in the application database.
The form uses a honeypot and temporary keyed rate-limit digests: three attempts
per peer/hour and per reply address/day. Global budgets default to ten/hour and
thirty/day; set DAILYTELLY_CONTACT_HOURLY_BUDGET and DAILYTELLY_CONTACT_DAILY_BUDGET as
needed within your delivery-provider allowance.

### Closed registration before SMTP is ready

Set DAILYTELLY_MAIL_MODE=disabled to run securely without an email provider. Public
registration and email sign-in are closed; no address can be claimed through a
public endpoint. HTTPS and the owner allowlist remain mandatory.

With the same production environment loaded, run:

~~~bash
python scripts/owner-access.py --email hello@thedailytelly.com --hours 24
~~~

The operator explicitly approves the configured owner account and receives a
private link. The link expires, is stored only as a hash, requires an explicit
confirmation POST, and works once. Issuing another invalidates earlier links.
Do not log or share the link. It is a sign-in credential. Browser sessions last up to
30 days and extend during use; they expire after 30 days without activity.
This is server-admin approval, not evidence of email deliverability.

When SMTP is configured, switch to smtp mode; owner-link redemption is then
disabled and the standard browser-bound email verification flow applies.

Set YOUTUBE_API_KEY and restrict it to the required API/server. Set Pushover user
and application tokens for operator load alerts. CPU/memory/disk defaults are
85/85/90 percent. Three consecutive minute samples trigger an alert, then a
30-minute successful-alert cooldown. Delivery failures are retried on later samples.

## Container or systemd

The Dockerfile runs as an unprivileged user, one Uvicorn worker, no access logs.
The compose example binds only loopback and uses a persistent volume at /data.
Put nginx/Caddy with a valid certificate in front of it. Enable HTTPS redirects.
No authentication flow should cross remote plain HTTP.
The compose example gives its bridge a fixed gateway and trusts only that gateway
and localhost for forwarded client addresses. nginx must replace forwarding headers.
The compose CPU/memory monitoring variables match its one-core/1 GB limits; keep
them aligned when changing capacity. Without these variables the panel reports
host CPU/memory. Disk always reports the data volume filesystem utilization.
If this subnet conflicts with your host, change both the subnet/gateway and trusted
address together. Never use a wildcard proxy trust setting on an exposed app port.

Alternatively run Uvicorn directly with the same environment and persistent data
directory. Do not run multiple workers or share one data volume across replicas.
For the systemd example, change DAILYTELLY_DATA from /data to /opt/dailytelly/data
and create that directory owned by the service's telly user before starting it.
The supplied reverse-proxy and systemd examples use /opt/dailytelly; review paths
and permissions for your host. This repository does not modify your existing VPS.

## Privacy and continuity

### Optional inbound support forwarding

The signed endpoint `POST /webhooks/resend/inbound` accepts only Resend
`email.received` events. Set `DAILYTELLY_RESEND_WEBHOOK_SECRET` from that webhook,
`DAILYTELLY_RESEND_RECEIVING_KEY` to a Resend key with receiving access, and
`DAILYTELLY_INBOUND_FORWARD_TO` to your private support inbox. Enable and verify
receiving in Resend. A sending-only API key cannot retrieve received messages.
Keep these values outside source control. The default accepted recipient is
`hello@thedailytelly.com`; override with `DAILYTELLY_INBOUND_ADDRESS` if needed.

Forwarding preserves the message body, attachments up to 10 MB total, and the
original reply address. A fixed verified sender is used for delivery. Signature
and timestamp checks reject forged events; successful message IDs are hashed
for 90-day duplicate protection. Resend idempotency keys cover concurrent retries.
No message content or sender addresses are stored by the application. Failed
events return non-2xx so Resend can retry; review its webhook logs for persistent
failures or messages over the attachment limit. Forwarded messages carry an
auto-submitted marker to reduce reply loops. Tracking is never added.

The contact form can deliver directly to the same private inbox by setting
`DAILYTELLY_CONTACT_EMAIL`, while the public page continues to show the support
address. This avoids an unnecessary second send through inbound forwarding.

Disable reverse-proxy/CDN analytics and access logs. Restrict error logs and rotate
them within seven days. Maintain encrypted consistent backups of the registry,
libraries, signing key, and a deletion ledger. Expire backups within seven days.
When restoring, apply deletion records before reopening the service. Do not
silently resurrect a deleted account from a backup.

Monitor filesystem capacity, YouTube quota, SMTP bounce/abuse, and resource alerts.
Set reasonable registration/admission limits before inviting the public. Provider
logs and data processing need disclosure in the final live privacy notice.

Complete platform-policy/self-designation, security review, live SMTP verification,
real TV tests, and the migration checklist before opening registration at scale.

## Starter schedules and welcome check-ins

Sign in with a verified email in `DAILYTELLY_OWNER_EMAILS` and visit `/owner`.
The starter editor selects channels already in your own library. Publish after
changing selections or when you want to refresh cached public video metadata.
Only safe-to-share catalog fields are copied: no watch progress, activity,
credentials, private settings, or blocklists. Unavailable, blocked, and
keyword-excluded source videos are omitted. New libraries start with 1–30 minute
videos and America/New_York as an editable schedule timezone. The starter template
is private runtime data, not a bundled database. Existing accounts are untouched.

New registrations can opt out of their one welcome check-in. Otherwise it is due
at the first 8 p.m. America/New_York at least 24 hours after email confirmation.
The five-minute scheduler runs only during that Eastern evening hour and handles
daylight saving through `zoneinfo`. This can mean roughly 24–48 hours, rather than
an exact 24-hour delay. An outage past the evening window defers unsent work until
the next evening. Only active, verified, opted-in accounts are eligible.

Set `DAILYTELLY_RESEND_SENDING_KEY`, or use Resend SMTP credentials already present
in the environment. Welcome delivery uses the Resend send API with a stable
idempotency key, a frozen retry payload, and at most six attempts. It stops retrying
after 23 hours from the first attempt to stay within provider idempotency retention.
Delivery snapshots are cleared after sending, cancellation, or expiry; the owner
metrics endpoint exposes aggregate reminder states. Sign-in and support email
continue to use SMTP with branded HTML and a plain-text alternative. Disable
Resend open/click tracking for the sending domain.

Use Account → Your account to cancel a pending check-in, or its signed preference
link. GET requests never change preferences. Deletion cascades pending jobs; a
suspended account is skipped. Sign-in email remains available independently.
