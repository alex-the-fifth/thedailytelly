# Troubleshooting

| Symptom | Check |
| --- | --- |
| No email in development | Read the ignored data/dev-mail JSON. There is no public mailbox endpoint. |
| No production email | SMTP host/TLS/sender/credentials and provider deliverability. |
| Link invalid | It expires, is single use, and requires the browser that requested it. Request again there. |
| Owner panel forbidden | Verify the exact email in DAILYTELLY_OWNER_EMAILS. |
| TV code invalid | Check it is on your own TV, still within 10 minutes, and not already approved/redeemed. |
| TV no longer authorized | It was unlinked, suspended, or paired to an unavailable account. Request a new code. |
| Empty library | Assign channels for today's server timezone; configure YouTube API key and inspect sync. |
| Code 403 during replay | The video is outside today's schedule/filter rules. |
| Load alerts absent | Configure Pushover environment secrets; wait for three sustained high samples. |
| Android build blocked here | Run the wrapper in a normal developer terminal; Java writability preflight fails in the sandbox. |

Never include live links, cookies, tokens, SMTP credentials, or child activity in reports.
