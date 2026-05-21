# Job Monitor

Watches company career pages with GitHub Actions and sends new-posting alerts to Discord, Slack, or Telegram.

## Current Companies

- Fuse - Ashby
- Inertia - Ashby
- Atomic Semi - Ashby
- Etched - Ashby
- Diamond Foundry - Lever
- Lightmatter - Greenhouse
- Marvell - Workday, fully paginated
- Neurable - SmartRecruiters

## Local Test

```powershell
pip install -r requirements.txt
python monitor.py
```

The first successful run seeds `state.json`, so future runs alert only for genuinely new postings.

## Discord Alerts

1. In Discord, open your server.
2. Open the alert channel settings.
3. Go to `Integrations` -> `Webhooks` -> `New Webhook`.
4. Copy the webhook URL exactly as Discord gives it to you.
5. Add it to GitHub as the `WEBHOOK_URL` repository secret.

Do not paste the webhook URL into `config.yaml`; keep it as a GitHub secret.

Alerts include one section:

- `New Internship/Jobs` - new roles matching the internship/early-career keywords in `config.yaml`.

New non-internship postings are still saved to `state.json` so they will not alert repeatedly, but they are not sent to Discord.

To duplicate selected startup alerts into a second Discord channel, create a second webhook for that channel and add it as the `STARTUP_WEBHOOK_URL` repository secret. Companies marked with `startup_channel: true` in `config.yaml` are sent to that channel in addition to the main channel.

## GitHub Setup

From this folder:

```powershell
git init
git add -A
git commit -m "Initial job monitor"
gh repo create job-monitor --private --source=. --push
gh secret set WEBHOOK_URL
gh secret set STARTUP_WEBHOOK_URL
```

Paste your main Discord webhook URL when `gh secret set WEBHOOK_URL` prompts for it. Paste the startup-channel webhook URL for `STARTUP_WEBHOOK_URL`, or skip that secret if you do not want the duplicate startup channel.

Then open the repo on GitHub, go to `Actions`, enable workflows if prompted, and run `Job Monitor` manually once. The workflow also supports GitHub's built-in schedule and external cron triggers.

## Reliable 15-Minute Scheduling

GitHub's built-in `schedule` trigger can be delayed or skipped. For more reliable checks, use cron-job.org to trigger the workflow every 15 minutes.

### 1. Create a GitHub Token

1. Open GitHub `Settings` -> `Developer settings` -> `Personal access tokens` -> `Fine-grained tokens`.
2. Click `Generate new token`.
3. Set repository access to `xlinzel/Job-Monitor`.
4. Under repository permissions, set `Contents` to `Read and write`.
5. Generate the token and copy it.

### 2. Create the cron-job.org Job

1. Create an account at [cron-job.org](https://cron-job.org/).
2. Create a new cronjob.
3. Set the schedule to every 15 minutes.
4. Set the request method to `POST`.
5. Set the URL to:

```text
https://api.github.com/repos/xlinzel/Job-Monitor/dispatches
```

6. Add these headers:

```text
Accept: application/vnd.github+json
Authorization: Bearer YOUR_GITHUB_TOKEN
X-GitHub-Api-Version: 2022-11-28
Content-Type: application/json
User-Agent: cron-job.org
```

7. Set the request body to:

```json
{"event_type":"job-monitor"}
```

8. Save it and run it once manually from cron-job.org.

If it works, GitHub Actions will show a new run with event type `repository_dispatch`.

## Testing Discord

Normal runs only send Discord messages when a new job is found. To test the webhook without waiting for a real posting:

1. Open the repo on GitHub.
2. Go to `Actions` -> `Job Monitor`.
3. Click `Run workflow`.
4. Set `test_alert` to `true`.
5. Click `Run workflow`.

If the webhook secrets are set correctly, Discord should receive a test message in the configured channel or channels. If a secret is missing or invalid, the workflow logs show the webhook error.

## Adding Companies

Edit `config.yaml`. Supported ATS values are:

- `greenhouse`
- `lever`
- `ashby`
- `workday`
- `smartrecruiters`
- `html`

For Workday, use `tenant/site`, for example:

```yaml
- name: Marvell
  ats: workday
  slug: marvell/MarvellCareers
```

## Notes

- `state.json` is intentionally committed so GitHub Actions remembers which jobs it has already seen.
- The workflow commits updated `state.json` after each run.
- GitHub scheduled jobs can have a few minutes of delay, even with a 15-minute cron.
- For more reliable timing, use cron-job.org with the `repository_dispatch` trigger.
