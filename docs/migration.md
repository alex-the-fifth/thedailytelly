# Move your personal library into an account

The importer copies a consistent legacy backup into an existing verified account.
Imported libraries remain private runtime data outside the source repository.

1. Obtain a consistent SQLite .backup of the existing Daily Telly database. Keep the
   original application and live database unchanged. Do not import a live WAL file.
2. Start the new platform and verify hello@thedailytelly.com through its email flow.
3. Keep that account's destination library empty.
4. Set the platform environment/data directory, then run:

~~~bash
python scripts/import-library.py --source /path/to/dailytelly-backup.db --email hello@thedailytelly.com
~~~

5. Review dry-run counts. Add --apply to copy the library.
6. Confirm channels, rules, block keywords, video counts, resume positions, and history.
7. Pair TVs anew through /activate. Legacy shared device tokens do not carry over.
8. Keep the old deployment available until you confirm the new account behaves correctly.

The importer checks integrity, reads the source in immutable read-only mode, refuses
a source with a WAL sidecar, and refuses a nonempty/already-imported destination.
It copies channel schedules, video metadata and blocks, watch progress, keywords,
duration rules, timezone, and refresh interval. It omits password hashes, old
devices/tokens, activity/IP/user-agent logs, and Pushover credentials.

Importing content does not bypass email verification or confer special access:
your owner household is an ordinary library account plus an explicitly allowlisted
operator role. Your family content is not promoted to a public catalog.
