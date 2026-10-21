# Privacy implementation

The user-facing policy is served at /privacy and /conduct from
server/static/policies.html. It explains minimal adult-email accounts, essential
cookies, per-household data, TV credentials, third-party YouTube/SMTP/Pushover
boundaries, export/deletion, and retention. It is a deployment policy that the
operator must actually enforce, not a claim of legal certification.

No analytics/ads/fingerprinting SDK is included. Household configuration activity
drops IP, user agent, detail, and child playback/app-open events. Watch progress is
retained because resume/history across devices is a requested account feature.
Network addresses are used only through expiring keyed abuse-limit digests.

Retention jobs run every minute: 24-hour server metrics and unverified accounts,
30-day operator audit, and expired tokens/rate limits. Household configuration
logs are pruned on activity and by hourly maintenance, including suspended accounts.
Backups/error logs require operator-enforced seven-day expiry and deletion replay.

The standard YouTube player and remote thumbnails can process data and display
ads; privacy-enhanced embedding is not a promise of zero tracking. Do not advertise
unqualified ad-free/anonymous playback.

Starter schedules copy only public metadata selected by the operator. Welcome
check-ins are optional, one-time, and cancelable; they use an idempotent send and
clear temporary retry snapshots after completion/cancellation or after 23 hours.
The privacy policy discloses email processors and the public GitHub community.
Sign-in and support email have HTML and plain-text alternatives, without remote
image resources or application-added tracking. Provider tracking must be disabled.
