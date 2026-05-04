# Job Monitor

Watches company career pages every 15 minutes with GitHub Actions and sends new-posting alerts to Discord, Slack, or Telegram.

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

## GitHub Setup

From this folder:

```powershell
git init
git add -A
git commit -m "Initial job monitor"
gh repo create job-monitor --private --source=. --push
gh secret set WEBHOOK_URL
```

Paste your Discord webhook URL when `gh secret set WEBHOOK_URL` prompts for it.

Then open the repo on GitHub, go to `Actions`, enable workflows if prompted, and run `Job Monitor` manually once. After that, `.github/workflows/monitor.yml` runs it every 15 minutes.

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
