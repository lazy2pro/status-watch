# Start here

## What is ready

This folder contains the status monitor, its 15-minute GitHub Actions workflow, and local fixture tests. The tests have passed. The Telegram bot token and chat ID are not included.

## What you need to do

1. On GitHub, create a **new empty public repository** named `status-watch` under `lazy2pro`. Public is required to keep standard GitHub-hosted Actions runs free. Do not add a README, license, or `.gitignore` during creation.
2. Reply with `lazy2pro/status-watch` once it exists. I can then upload these files through the connected GitHub integration; no browser sign-in is needed for that step.
3. In the repository's **Settings → Secrets and variables → Actions**, add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Enter the values directly in GitHub Secrets; do not send them in chat.

After those steps, I can run the workflow and check the first collection results. Healthchecks.io is optional; it requires its own account and is not needed for the service monitor itself.

## Why a new repository

I checked the connected account. Its current repositories are existing, non-empty projects, and none is a suitable empty home for this monitor. I have not modified any of them.

