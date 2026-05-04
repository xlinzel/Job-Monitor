#!/usr/bin/env python3
"""
Job Posting Monitor
Polls company career pages across multiple ATS platforms,
detects new postings, filters by keywords, and sends alerts.
"""

import json
import hashlib
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
import yaml

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG_PATH = os.environ.get("CONFIG_PATH", "config.yaml")
STATE_PATH = os.environ.get("STATE_PATH", "state.json")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("job-monitor")

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class Job:
    id: str
    title: str
    url: str
    location: str = ""
    team: str = ""
    company: str = ""
    discovered_at: str = ""

    def matches_filters(self, include: list[str], exclude: list[str]) -> bool:
        """Check if job matches keyword filters (case-insensitive)."""
        text = f"{self.title} {self.location} {self.team}".lower()
        if include and not any(kw.lower() in text for kw in include):
            return False
        if exclude and any(kw.lower() in text for kw in exclude):
            return False
        return True


# ---------------------------------------------------------------------------
# ATS Fetchers
# ---------------------------------------------------------------------------

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "JobMonitorBot/1.0"})
REQUEST_TIMEOUT = 30
DEFAULT_INTERNSHIP_KEYWORDS = [
    "intern",
    "internship",
    "co-op",
    "co op",
    "coop",
    "co-operative",
    "cooperative",
]


def fetch_greenhouse(company_slug: str, company_name: str) -> list[Job]:
    """Fetch jobs from Greenhouse JSON API."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{company_slug}/jobs"
    # The ?content=true param is optional; omit to keep payload smaller
    resp = SESSION.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    jobs = []
    for item in data.get("jobs", []):
        job = Job(
            id=str(item["id"]),
            title=item.get("title", ""),
            url=item.get("absolute_url", f"https://boards.greenhouse.io/{company_slug}/jobs/{item['id']}"),
            location=item.get("location", {}).get("name", ""),
            team=(item.get("departments", [{}])[0].get("name", "") if item.get("departments") else ""),
            company=company_name,
        )
        jobs.append(job)
    return jobs


def fetch_lever(company_slug: str, company_name: str) -> list[Job]:
    """Fetch jobs from Lever public API."""
    url = f"https://api.lever.co/v0/postings/{company_slug}?mode=json"
    resp = SESSION.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    jobs = []
    for item in data:
        cats = item.get("categories", {})
        job = Job(
            id=item.get("id", ""),
            title=item.get("text", ""),
            url=item.get("hostedUrl", ""),
            location=cats.get("location", ""),
            team=cats.get("team", ""),
            company=company_name,
        )
        jobs.append(job)
    return jobs


def fetch_ashby(company_slug: str, company_name: str) -> list[Job]:
    """Fetch jobs from Ashby public REST API."""
    url = f"https://api.ashbyhq.com/posting-api/job-board/{company_slug}"
    resp = SESSION.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    jobs = []
    for item in data.get("jobs", []):
        if not item.get("isListed", True):
            continue
        job = Job(
            id=item.get("id", item.get("jobUrl", "")),
            title=item.get("title", ""),
            url=item.get("jobUrl", f"https://jobs.ashbyhq.com/{company_slug}"),
            location=item.get("location", ""),
            team=item.get("team", item.get("department", "")),
            company=company_name,
        )
        jobs.append(job)
    return jobs


def fetch_workday(company_slug: str, company_name: str) -> list[Job]:
    """
    Fetch jobs from Workday with pagination.
    company_slug format: 'tenant/site' e.g. 'mycompany/mycompany'
    Optionally: 'tenant.wd1/site' to specify the Workday instance (wd1, wd5, etc.)
    Default instance is wd1 if not specified.
    """
    tenant_part, site = company_slug.split("/", 1)
    if "." in tenant_part:
        tenant, wd_instance = tenant_part.rsplit(".", 1)
    else:
        tenant = tenant_part
        wd_instance = "wd1"
    url = f"https://{tenant}.{wd_instance}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"

    jobs = []
    seen_ids = set()
    offset = 0
    page_size = 20
    max_pages = 50  # safety cap: 50 * 20 = 1000 jobs max
    expected_total: Optional[int] = None

    for _ in range(max_pages):
        payload = {"appliedFacets": {}, "limit": page_size, "offset": offset, "searchText": ""}
        resp = SESSION.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        postings = data.get("jobPostings", [])
        if not postings:
            break

        page_total = data.get("total")
        if isinstance(page_total, int) and page_total > 0:
            expected_total = page_total

        for item in postings:
            ext_path = item.get("externalPath", "")
            job_id = ext_path or item.get("bulletFields", [""])[0]
            if job_id in seen_ids:
                continue
            seen_ids.add(job_id)
            job = Job(
                id=job_id,
                title=item.get("title", ""),
                url=f"https://{tenant}.{wd_instance}.myworkdayjobs.com/en-US/{site}{ext_path}",
                location=item.get("locationsText", ""),
                company=company_name,
            )
            jobs.append(job)

        offset += page_size
        if expected_total and len(jobs) >= expected_total:
            break
        if len(postings) < page_size:
            break
        time.sleep(0.3)  # be polite

    return jobs


def fetch_smartrecruiters(company_slug: str, company_name: str) -> list[Job]:
    """Fetch jobs from SmartRecruiters public API."""
    jobs = []
    offset = 0
    limit = 100

    while True:
        url = f"https://api.smartrecruiters.com/v1/companies/{company_slug}/postings"
        params = {"offset": offset, "limit": limit}
        resp = SESSION.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        content = data.get("content", [])
        if not content:
            break

        for item in content:
            loc = item.get("location", {})
            location_parts = [loc.get("city", ""), loc.get("region", ""), loc.get("country", "")]
            location_str = ", ".join(p for p in location_parts if p)
            dept = item.get("department", {}).get("label", "")

            job = Job(
                id=item.get("id", item.get("uuid", "")),
                title=item.get("name", ""),
                url=item.get("applyUrl", f"https://jobs.smartrecruiters.com/{company_slug}/{item.get('id','')}"),
                location=location_str,
                team=dept,
                company=company_name,
            )
            jobs.append(job)

        total = data.get("totalFound", 0)
        offset += limit
        if offset >= total:
            break
        time.sleep(0.3)

    return jobs


def fetch_generic_html(career_url: str, company_name: str) -> list[Job]:
    """
    Fallback: fetch an HTML page and extract links that look like job postings.
    This is intentionally simple - you'll likely want to customize the
    CSS selectors or regex per company. See config.yaml for options.
    """
    from bs4 import BeautifulSoup

    resp = SESSION.get(career_url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # Heuristic: find links whose href contains common job-page patterns
    job_patterns = re.compile(
        r"/(jobs?|careers?|positions?|openings?|apply|requisition)/",
        re.IGNORECASE,
    )
    jobs = []
    seen_urls = set()
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if not job_patterns.search(href):
            continue
        # Normalise relative URLs
        if href.startswith("/"):
            from urllib.parse import urlparse
            parsed = urlparse(career_url)
            href = f"{parsed.scheme}://{parsed.netloc}{href}"
        if href in seen_urls:
            continue
        seen_urls.add(href)

        title = a_tag.get_text(strip=True) or href
        job_id = hashlib.md5(href.encode()).hexdigest()[:12]
        jobs.append(Job(id=job_id, title=title, url=href, company=company_name))

    return jobs


# Map ATS names to fetcher functions
FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workday": fetch_workday,
    "smartrecruiters": fetch_smartrecruiters,
    "html": fetch_generic_html,
}


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_state(path: str) -> dict:
    p = Path(path)
    if p.exists():
        return json.loads(p.read_text())
    return {}


def save_state(path: str, state: dict):
    Path(path).write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------


def is_internship_job(job: Job, keywords: list[str]) -> bool:
    """Return true for internship/co-op style postings."""
    text = f"{job.title} {job.location} {job.team}".lower()
    for keyword in keywords:
        keyword = keyword.strip().lower()
        if not keyword:
            continue
        if keyword in {"co-op", "co op", "coop"}:
            if re.search(r"\bco[-\s]?op\b", text):
                return True
            continue
        if re.search(rf"\b{re.escape(keyword)}s?\b", text):
            return True
    return False


def clean_markdown_text(value: str) -> str:
    """Keep alert lines readable if an ATS returns newlines or empty fields."""
    return re.sub(r"\s+", " ", value or "").strip()


def format_job_line(job: Job, link_style: str = "markdown") -> str:
    title = clean_markdown_text(job.title) or "Untitled role"
    company = clean_markdown_text(job.company) or "Unknown company"
    location = clean_markdown_text(job.location)
    team = clean_markdown_text(job.team)

    if link_style == "slack":
        title_part = f"<{job.url}|{title}>" if job.url else title
    else:
        title_part = f"[{title}]({job.url})" if job.url else title

    details = [company]
    if location:
        details.append(location)
    if team:
        details.append(team)
    return f"- {title_part} - {' | '.join(details)}"


def chunk_sectioned_alerts(
    sections: list[tuple[str, list[Job]]],
    link_style: str = "markdown",
    max_length: int = 1900,
    header_format: str = "**{title}**",
) -> list[str]:
    """Build one or more sectioned alert messages within platform limits."""
    messages: list[str] = []
    current = ""

    for section_title, jobs in sections:
        header = header_format.format(title=section_title) + "\n"
        lines = [format_job_line(job, link_style) for job in sorted(jobs, key=lambda j: (j.company, j.title))]
        if not lines:
            lines = ["- None right now"]

        section_started = False
        for line in lines:
            if not section_started:
                addition = ("\n" if current else "") + header + line + "\n"
                section_started = True
            else:
                addition = line + "\n"

            if current and len(current) + len(addition) > max_length:
                messages.append(current.rstrip())
                continued = header_format.format(title=f"{section_title} (continued)")
                current = f"{continued}\n{line}\n"
            else:
                current += addition

    if current:
        messages.append(current.rstrip())
    return messages


def send_webhook(webhook_url: str, new_jobs: list[Job], internship_jobs: list[Job]) -> bool:
    """Send a notification via Slack or Discord webhook."""
    if not new_jobs:
        return True

    webhook_url = webhook_url.strip()
    if webhook_url.rstrip("/").endswith("/slack") and "discord" in webhook_url:
        webhook_url = webhook_url.rstrip("/")[:-len("/slack")]

    is_discord = "discord.com" in webhook_url or "discordapp.com" in webhook_url
    link_style = "markdown" if is_discord else "slack"
    messages = chunk_sectioned_alerts(
        [
            ("New job postings", new_jobs),
            ("Internship / co-op reminders", internship_jobs),
        ],
        link_style=link_style,
        max_length=1900 if is_discord else 2800,
        header_format="**{title}**" if is_discord else "*{title}*",
    )

    delivered_all = True
    for message in messages:
        payload = {"content": message} if is_discord else {"text": message}

        try:
            r = SESSION.post(webhook_url, json=payload, timeout=10)
            r.raise_for_status()
            time.sleep(0.5)  # respect rate limits
        except Exception as e:
            delivered_all = False
            log.error("Webhook delivery failed: %s", e)
    return delivered_all


def send_telegram(bot_token: str, chat_id: str, new_jobs: list[Job], internship_jobs: list[Job]) -> bool:
    """Send notifications via Telegram bot."""
    messages = chunk_sectioned_alerts(
        [
            ("New job postings", new_jobs),
            ("Internship / co-op reminders", internship_jobs),
        ],
        link_style="markdown",
        max_length=3800,
        header_format="*{title}*",
    )

    delivered_all = True
    for text in messages:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        try:
            r = SESSION.post(url, json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            }, timeout=10)
            r.raise_for_status()
            time.sleep(0.3)
        except Exception as e:
            delivered_all = False
            log.error("Telegram delivery failed: %s", e)
    return delivered_all


def is_truthy_env(value: str) -> bool:
    """Parse GitHub Actions input/environment booleans."""
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def send_test_alert(webhook_url: str, telegram_token: str, telegram_chat: str, now: str) -> bool:
    """Send a harmless alert to verify notification wiring."""
    test_new_jobs = [
        Job(
            id="test-new-job",
            title="Test alert from Job Monitor",
            url="https://github.com/xlinzel/Job-Monitor/actions",
            location="Notification test",
            team="Monitor",
            company="Job Monitor",
            discovered_at=now,
        )
    ]
    test_internship_jobs = [
        Job(
            id="test-internship-reminder",
            title="Example Internship / Co-op Reminder",
            url="https://github.com/xlinzel/Job-Monitor",
            location="Notification test",
            team="Reminder",
            company="Job Monitor",
            discovered_at=now,
        )
    ]

    delivered = True
    if webhook_url:
        delivered = send_webhook(webhook_url, test_new_jobs, test_internship_jobs) and delivered
    if telegram_token and telegram_chat:
        delivered = send_telegram(telegram_token, telegram_chat, test_new_jobs, test_internship_jobs) and delivered
    if not webhook_url and not telegram_token:
        delivered = False
        log.warning("No notification channel configured - printing test alert to stdout")
        for message in chunk_sectioned_alerts([
            ("New job postings", test_new_jobs),
            ("Internship / co-op reminders", test_internship_jobs),
        ]):
            print(message)
    return delivered


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run():
    # Load config
    config_path = Path(CONFIG_PATH)
    if not config_path.exists():
        log.error("Config file not found: %s", CONFIG_PATH)
        sys.exit(1)

    config = yaml.safe_load(config_path.read_text())
    companies = config.get("companies", [])
    global_include = config.get("filters", {}).get("include_keywords", [])
    global_exclude = config.get("filters", {}).get("exclude_keywords", [])
    reminder_config = config.get("reminders", {})
    internship_keywords = reminder_config.get("internship_keywords", DEFAULT_INTERNSHIP_KEYWORDS)
    notifications = config.get("notifications", {})

    webhook_url = os.environ.get("WEBHOOK_URL", notifications.get("webhook_url", ""))
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", notifications.get("telegram_bot_token", ""))
    telegram_chat = os.environ.get("TELEGRAM_CHAT_ID", notifications.get("telegram_chat_id", ""))
    test_alert = is_truthy_env(os.environ.get("TEST_ALERT", "false"))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if test_alert:
        log.info("TEST_ALERT=true - sending test alert and skipping job fetch/state update.")
        delivered = send_test_alert(webhook_url, telegram_token, telegram_chat, now)
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a") as f:
                f.write(f"## Job Monitor Test Alert - {now}\n\n")
                f.write("Test alert delivered.\n" if delivered else "Test alert failed. Check workflow logs and secrets.\n")
        if not delivered:
            sys.exit(1)
        return

    # Load previous state
    configured_company_names = {company["name"] for company in companies}
    state = {
        company_name: job_ids
        for company_name, job_ids in load_state(STATE_PATH).items()
        if company_name in configured_company_names
    }
    all_new_jobs: list[Job] = []
    all_current_jobs: list[Job] = []

    for company in companies:
        name = company["name"]
        ats = company["ats"]
        slug = company.get("slug", "")
        url = company.get("url", "")
        include = company.get("include_keywords", global_include)
        exclude = company.get("exclude_keywords", global_exclude)

        log.info("Checking %s (%s)...", name, ats)

        fetcher = FETCHERS.get(ats)
        if not fetcher:
            log.warning("Unknown ATS type '%s' for %s - skipping", ats, name)
            continue

        try:
            if ats == "html":
                current_jobs = fetcher(url, name)
            else:
                current_jobs = fetcher(slug, name)
        except Exception as e:
            log.error("Failed to fetch %s: %s", name, e)
            continue
        all_current_jobs.extend(current_jobs)

        # Diff against known IDs
        known_ids = set(state.get(name, []))
        current_ids = {j.id for j in current_jobs}

        new_jobs = []
        for job in current_jobs:
            if job.id not in known_ids and job.matches_filters(include, exclude):
                job.discovered_at = now
                new_jobs.append(job)

        if new_jobs:
            log.info("  -> %d new posting(s) for %s", len(new_jobs), name)
            all_new_jobs.extend(new_jobs)
        else:
            log.info("  -> No new postings for %s", name)

        # Update state with current IDs
        state[name] = sorted(current_ids)

    # Persist state
    save_state(STATE_PATH, state)

    # Send notifications
    if all_new_jobs:
        internship_jobs = [
            job
            for job in all_current_jobs
            if is_internship_job(job, internship_keywords)
        ]
        log.info("Sending alerts for %d new posting(s)...", len(all_new_jobs))
        log.info("Including %d internship/co-op reminder posting(s).", len(internship_jobs))
        if webhook_url:
            send_webhook(webhook_url, all_new_jobs, internship_jobs)
        if telegram_token and telegram_chat:
            send_telegram(telegram_token, telegram_chat, all_new_jobs, internship_jobs)
        if not webhook_url and not telegram_token:
            log.warning("No notification channel configured - printing to stdout")
            for message in chunk_sectioned_alerts([
                ("New job postings", all_new_jobs),
                ("Internship / co-op reminders", internship_jobs),
            ]):
                print(message)
    else:
        log.info("No new postings found this run.")

    # Write summary for GitHub Actions
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write(f"## Job Monitor - {now}\n\n")
            if all_new_jobs:
                f.write(f"**{len(all_new_jobs)} new posting(s) found:**\n\n")
                for j in all_new_jobs:
                    f.write(f"- [{j.title}]({j.url}) @ {j.company} ({j.location})\n")
                internship_jobs = [
                    job
                    for job in all_current_jobs
                    if is_internship_job(job, internship_keywords)
                ]
                f.write(f"\n**{len(internship_jobs)} internship/co-op reminder posting(s):**\n\n")
                for j in internship_jobs:
                    f.write(f"- [{j.title}]({j.url}) @ {j.company} ({j.location})\n")
            else:
                f.write("No new postings.\n")


if __name__ == "__main__":
    run()
