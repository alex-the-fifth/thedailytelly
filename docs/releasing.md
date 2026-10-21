# Release checklist

- Run core logic, platform security/migration tests, and complete browser flows.
- Build/lint Android, test phone/TV playback and lifecycle/remote behavior.
- Verify live SMTP deliverability and production secure-cookie/origin behavior.
- Configure domain/TLS, sender records, owner allowlist, Pushover, backup and deletion handling.
- Perform a dry-run and reviewed import of the real library backup.
- Review child-directed platform policies, quotas, abuse limits, and capacity.
- Publish the source location and link it in source.html before calling the service public.
- Inspect the exact Git tree for secrets/runtime data and source provenance.
- Deploy one instance first, observe load, and open registration gradually.

The public source repository is https://github.com/alex-the-fifth/daily-telly.
Keep runtime data, credentials, private host configuration, and signing keys out of Git.
