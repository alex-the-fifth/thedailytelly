# Daily Telly backend

FastAPI with a central identity/operational SQLite registry and one scoped SQLite
library per verified household. No frontend bundler. Configuration is documented
in .env.example and [setup](../docs/setup.md).

Run from the repository root with an isolated Python environment. Production uses
HTTPS and SMTP, disables request access logs, and runs one worker. Never mount
the data directory in the public static tree.

The account context is request-local and fail-closed. Do not pass account IDs from
request payloads into database paths. TV bearer tokens cannot access parent or owner
routes. Tests in tests/test_platform.py exercise these boundaries.
