# Local setup

Follow the root README. The default development URL is http://127.0.0.1:18400.
Set DAILYTELLY_OWNER_EMAILS=hello@thedailytelly.com to exercise the owner UI locally.
The owner role requires both a verified email and an exact allowlist match.

Development mail is a local JSON file under the ignored data/dev-mail directory.
It is never returned by a public endpoint and cannot be enabled in production.
Use the same browser for requesting and opening its link. Expired/used links need
a fresh request. The site collects no passwords or child identity.

Runtime variables are listed in server/.env.example. Production examples are not
automatically loaded by local commands. Configure your own YouTube API key for
channel resolution; no real videos can be synced without it.

Pages: /, /login, /verify, /app, /activate, /kid/, /owner, /privacy, /conduct.
The owner address is hello@thedailytelly.com; do not move development account data
into production as a substitute for real mailbox verification.
