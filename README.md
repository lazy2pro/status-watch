# Public Service Status Watch

An unauthenticated, public-feed monitor for 10 major service providers. It polls every 15 minutes in GitHub Actions and sends new incidents, material updates, recoveries, and collection failures to Telegram. Incident state lives in `state.json` in the repository. No company account or administrator access is used.

## Coverage and source limits

| Service | Official public source configured | Coverage caveat |
|---|---|---|
| Microsoft 365 | `https://status.cloud.microsoft/api/feed/mac` (RSS) | Public service-health notice page is a fallback channel, chiefly when admin-center health is unavailable. It does not expose tenant-specific Service Health; Microsoft Graph data needs tenant/admin authorization. |
| Azure | `https://azure.status.microsoft/en-us/status/feed/` | RSS reports broad public-impact events only, not all subscription/resource-specific advisories, maintenance, or outages. |
| Google Workspace | `https://www.google.com/appsstatus/dashboard/incidents.json` | Official JSON History feed. It includes historical incidents as well as resolved notices; current open items are determined from latest update status and `end`. |
| Google Cloud | `https://status.cloud.google.com/incidents.json` | Public incident history is available without credentials; project-personalized Service Health is not included. |
| AWS | `https://status.aws.amazon.com/rss/all.rss` | Public RSS; AWS says RSS format may change and recommends EventBridge for programmatic account-specific events. |
| Cloudflare | `https://www.cloudflarestatus.com/api/v2/summary.json` | Public Statuspage JSON API, no auth. |
| GitHub | `https://www.githubstatus.com/api/v2/summary.json` | Public Statuspage JSON API, no auth. |
| OpenAI | `https://status.openai.com/api/v2/summary.json` | Public Statuspage JSON API, no auth. |
| Slack | `https://slack-status.com/api/v2.0.0/current` | Slack documents unauthenticated current/history endpoints. |
| Zoom | `https://status.zoom.us/api/v2/summary.json` | Statuspage-style URL responds through Zoom's current status host; verify the redirect/API payload in GitHub Actions before relying on it. |

The polling code intentionally treats feed/parse errors as collection failures rather than interpreting them as recovery. In an initial run it establishes a baseline without alerting on all still-open historical incidents. Subsequent runs alert on new incidents, updates, and disappearance from a successfully polled source (recovery). State commits only after Telegram messages are delivered. This avoids losing notices if Telegram is temporarily unavailable.

## One-time setup

1. Put this directory's contents in a public GitHub repository. Standard GitHub-hosted Actions runners are free for public repositories. The workflow has to be on the repository's default branch. Code and committed status history will be public.
2. Add repository Actions secrets `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Never put these values in files, commits, or workflow text.
3. (Optional) Create a free Healthchecks.io check for the workflow, set its period to 15 minutes plus grace time, then store its ping URL in Actions secret `HC_PING_URL`. Healthchecks.io's free Hobbyist tier supports 20 checks. Its notifications are independent of Actions, but the check itself must be configured in the Healthchecks account.
4. Run `workflow_dispatch` once. Inspect logs for `OK` for all ten sources and inspect the committed state file. Verify a Telegram test separately; the monitor deliberately does not send a startup test message.
5. Check at least one real incident/recovery test using saved fixture data before enabling regular monitoring.

## Free limits

At 15 minutes, expect 2,880 workflow runs per 30-day month. GitHub currently provides standard GitHub-hosted Actions runners free for public repositories. Private Free repositories have included compute usage of $12/month, shared with other eligible compute products, after which usage can be billed when a payment method is on file or blocked if none is on file. To keep Actions usage at $0, use this project in a public repository and standard Linux runner; do not use larger runners. Public repositories expose code and committed status history. A 15-minute schedule can be delayed by GitHub Actions load; it is not a hard real-time guarantee.

## GitHub Actions independent watch

Healthchecks.io should expect a heartbeat within 15 minutes and alert after an additional grace interval (e.g. 10 minutes). This detects missing workflow heartbeats, including a disabled/stuck Actions workflow, subject to Healthchecks.io delivery/integration configuration. It does not independently monitor GitHub's platform status; a GitHub outage can itself prevent Actions from starting. A genuinely independent signal for GitHub platform health requires another always-on scheduler/provider, a hosted status subscription, or a person checking status. No such account/service was connected automatically.

## Local smoke run

Python 3.10+ standard library only. A live monitor run needs Telegram secrets and network access. For local parser/state tests run:

```sh
python -m unittest discover -s tests -v
```


