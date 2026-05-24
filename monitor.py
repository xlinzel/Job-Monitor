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
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

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

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36 JobMonitorBot/1.0"
    ),
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
}


class ThreadLocalSession:
    """Give each worker thread its own requests session and connection pool."""

    def __init__(self):
        self._local = threading.local()

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(REQUEST_HEADERS)
            adapter = requests.adapters.HTTPAdapter(pool_connections=64, pool_maxsize=64)
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            self._local.session = session
        return session

    def get(self, *args, **kwargs):
        return self._session().get(*args, **kwargs)

    def post(self, *args, **kwargs):
        return self._session().post(*args, **kwargs)


SESSION = ThreadLocalSession()
REQUEST_TIMEOUT = 30
COMPANY_FETCH_WORKERS = max(1, int(os.environ.get("COMPANY_FETCH_WORKERS", "8")))
WORKDAY_PAGE_WORKERS = max(1, int(os.environ.get("WORKDAY_PAGE_WORKERS", "6")))
DISCORD_COMMAND_FETCH_LIMIT = max(100, int(os.environ.get("DISCORD_COMMAND_FETCH_LIMIT", "1000")))
DEFAULT_INTERNSHIP_KEYWORDS = [
    "intern",
    "internship",
    "co-op",
    "co op",
    "coop",
    "co-operative",
    "cooperative",
    "trainee",
    "traineeship",
    "student",
    "university",
    "apprentice",
    "apprenticeship",
    "fall",
    "winter",
    "spring",
    "working student",
]
SECTION_NEW_INTERNSHIP_JOBS = "New Internship/Jobs"
SECTION_INTERNSHIP_REMINDERS = "Internship / co-op / trainee reminders"
SECTION_OTHER_NEW_JOBS = "Other new jobs"


def stable_id(*parts: str) -> str:
    """Build a short stable ID when an ATS does not expose one cleanly."""
    raw = "|".join(p for p in parts if p)
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def clean_text(value: str) -> str:
    """Collapse HTML/text whitespace."""
    return re.sub(r"\s+", " ", value or "").strip()


def job_state_key(job: Job) -> str:
    """Fingerprint a posting so reused ATS IDs can still produce new alerts."""
    return "v2:" + stable_id(
        job.company,
        job.id,
        clean_text(job.title).lower(),
        clean_text(job.location).lower(),
        clean_text(job.url).lower(),
    )


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


def parse_workday_slug(company_slug: str) -> tuple[str, str, str, str, str]:
    """
    Return tenant, wd instance, site, API URL, public URL base.
    Supported formats:
      tenant/site
      tenant.wd3/site
      https://tenant.wd3.myworkdayjobs.com/Site
      https://wd5.myworkdaysite.com/recruiting/tenant/Site
    """
    if company_slug.startswith("http"):
        parsed = urlparse(company_slug)
        host = parsed.netloc
        path_parts = [part for part in parsed.path.strip("/").split("/") if part]

        if host.endswith(".myworkdayjobs.com"):
            tenant_part = host.split(".myworkdayjobs.com", 1)[0]
            if "." in tenant_part:
                tenant, wd_instance = tenant_part.rsplit(".", 1)
            else:
                tenant, wd_instance = tenant_part, "wd1"
            site = path_parts[0] if path_parts else ""
            public_base = f"https://{tenant}.{wd_instance}.myworkdayjobs.com"
        elif host.endswith(".myworkdaysite.com") and len(path_parts) >= 3 and path_parts[0] == "recruiting":
            wd_instance = host.split(".myworkdaysite.com", 1)[0]
            tenant = path_parts[1]
            site = path_parts[2]
            public_base = f"https://{host}/recruiting/{tenant}"
        else:
            raise ValueError(f"Unsupported Workday URL: {company_slug}")
    else:
        tenant_part, site = company_slug.split("/", 1)
        if "." in tenant_part:
            tenant, wd_instance = tenant_part.rsplit(".", 1)
        else:
            tenant = tenant_part
            wd_instance = "wd1"
        public_base = f"https://{tenant}.{wd_instance}.myworkdayjobs.com"

    api_url = f"https://{tenant}.{wd_instance}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    if company_slug.startswith("http") and ".myworkdaysite.com" in urlparse(company_slug).netloc:
        host = urlparse(company_slug).netloc
        api_url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    return tenant, wd_instance, site, api_url, public_base


def fetch_workday(company_slug: str, company_name: str) -> list[Job]:
    """
    Fetch jobs from Workday with pagination.
    company_slug format: 'tenant/site' e.g. 'mycompany/mycompany'
    Optionally: 'tenant.wd1/site' to specify the Workday instance (wd1, wd5, etc.)
    Default instance is wd1 if not specified.
    """
    tenant, wd_instance, site, url, public_base = parse_workday_slug(company_slug)
    referer = company_slug if company_slug.startswith("http") else f"{public_base}/en-US/{site}"
    headers = {
        **REQUEST_HEADERS,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": public_base,
        "Referer": referer,
    }

    jobs = []
    seen_ids = set()
    page_size = 20
    max_pages = 50  # safety cap: 50 * 20 = 1000 jobs max

    def fetch_page(page_offset: int) -> tuple[int, dict]:
        payload = {"appliedFacets": {}, "limit": page_size, "offset": page_offset, "searchText": ""}
        last_error = None
        for attempt in range(3):
            try:
                resp = SESSION.post(url, json=payload, headers=headers, timeout=max(REQUEST_TIMEOUT, 45))
                resp.raise_for_status()
                return page_offset, resp.json()
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(1 + attempt)
        raise last_error

    def add_postings(postings: list[dict]):
        for item in postings:
            ext_path = item.get("externalPath", "")
            job_id = ext_path or item.get("bulletFields", [""])[0]
            if job_id in seen_ids:
                continue
            seen_ids.add(job_id)
            if ".myworkdaysite.com/" in public_base:
                job_url = urljoin(f"{public_base}/{site}/", ext_path.lstrip("/"))
            else:
                job_url = urljoin(f"{public_base}/en-US/{site}/", ext_path.lstrip("/"))
            job = Job(
                id=job_id,
                title=item.get("title", ""),
                url=job_url,
                location=item.get("locationsText", ""),
                company=company_name,
            )
            jobs.append(job)

    _, first_page = fetch_page(0)
    first_postings = first_page.get("jobPostings", [])
    if not first_postings:
        return jobs

    add_postings(first_postings)
    expected_total = first_page.get("total")

    if isinstance(expected_total, int) and expected_total > page_size:
        max_offset = min(expected_total, max_pages * page_size)
        remaining_offsets = list(range(page_size, max_offset, page_size))
        with ThreadPoolExecutor(max_workers=min(WORKDAY_PAGE_WORKERS, len(remaining_offsets))) as executor:
            futures = [executor.submit(fetch_page, page_offset) for page_offset in remaining_offsets]
            for future in as_completed(futures):
                _, data = future.result()
                add_postings(data.get("jobPostings", []))
        return jobs

    offset = page_size
    for _ in range(1, max_pages):
        _, data = fetch_page(offset)
        postings = data.get("jobPostings", [])
        if not postings:
            break
        add_postings(postings)
        offset += page_size
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


def fetch_jazzhr(board_url: str, company_name: str) -> list[Job]:
    """Fetch jobs from JazzHR / ApplyToJob public boards."""
    from bs4 import BeautifulSoup

    resp = SESSION.get(board_url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    jobs = []
    seen_ids = set()
    for a_tag in soup.find_all("a", href=True):
        href = urljoin(board_url, a_tag["href"])
        parsed = urlparse(href)
        path_parts = [part for part in parsed.path.strip("/").split("/") if part]
        if len(path_parts) < 2 or path_parts[0].lower() != "apply":
            continue

        job_id = path_parts[1]
        if not re.match(r"^[A-Za-z0-9]+$", job_id):
            continue
        if job_id in seen_ids:
            continue

        title = clean_text(a_tag.get_text(" ", strip=True))
        if not title or title.lower() in {"apply", "view all", "view all jobs", "skip to content"}:
            continue

        location = ""
        parent = a_tag.find_parent(["li", "div", "tr"])
        if parent:
            lines = [clean_text(line) for line in parent.get_text("\n", strip=True).splitlines()]
            lines = [line for line in lines if line and line != title]
            if lines:
                location = lines[0]

        seen_ids.add(job_id)
        jobs.append(Job(
            id=job_id,
            title=title,
            url=href,
            location=location,
            company=company_name,
        ))
    return jobs


def fetch_bamboohr(company_slug: str, company_name: str) -> list[Job]:
    """Fetch jobs from BambooHR careers/list JSON endpoint."""
    if company_slug.startswith("http"):
        host = urlparse(company_slug).netloc
        subdomain = host.split(".bamboohr.com", 1)[0]
    else:
        subdomain = company_slug

    base = f"https://{subdomain}.bamboohr.com"
    resp = SESSION.get(f"{base}/careers/list", timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    jobs = []
    for item in data.get("result", []):
        loc = item.get("location") or {}
        location = ", ".join(
            part for part in [loc.get("city", ""), loc.get("state", ""), loc.get("country", "")]
            if part
        )
        job_id = str(item.get("id", ""))
        jobs.append(Job(
            id=job_id,
            title=item.get("jobOpeningName", ""),
            url=f"{base}/careers/{job_id}",
            location=location,
            team=item.get("departmentLabel", ""),
            company=company_name,
        ))
    return jobs


def fetch_jibe(base_url: str, company_name: str) -> list[Job]:
    """Fetch jobs from Jibe/iCIMS-hosted careers APIs."""
    jobs = []
    page = 1
    limit = 100
    total = None

    while True:
        resp = SESSION.get(
            urljoin(base_url.rstrip("/") + "/", "api/jobs"),
            params={"page": page, "limit": limit},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("jobs", [])
        if not items:
            break

        for item in items:
            payload = item.get("data", item)
            meta = payload.get("meta_data") or {}
            job_id = str(payload.get("req_id") or payload.get("job_id") or payload.get("id") or item.get("id") or "")
            title = payload.get("title", "")
            raw_url = meta.get("canonical_url") or payload.get("apply_url") or payload.get("url") or ""
            if not raw_url:
                raw_url = f"/jobs/{payload.get('slug', job_id)}"
            location = (
                payload.get("full_location")
                or payload.get("short_location")
                or payload.get("location_name")
                or ", ".join(part for part in [payload.get("city", ""), payload.get("state", ""), payload.get("country", "")] if part)
            )
            categories = payload.get("categories") or []
            if isinstance(categories, list):
                team = ", ".join(str(cat) for cat in categories if cat)
            else:
                team = str(categories)
            jobs.append(Job(
                id=job_id or stable_id(title, raw_url),
                title=title,
                url=urljoin(base_url, raw_url),
                location=location,
                team=team,
                company=company_name,
            ))

        total = total or data.get("totalCount") or data.get("total")
        if total and page * limit >= int(total):
            break
        page += 1
        time.sleep(0.2)

    return jobs


def fetch_eightfold(company_slug: str, company_name: str) -> list[Job]:
    """Fetch jobs from Eightfold PCS search APIs."""
    if "|" not in company_slug:
        raise ValueError("Eightfold slug must be 'careers_url|domain'")
    careers_url, domain = [part.strip() for part in company_slug.split("|", 1)]
    api_url = urljoin(careers_url.rstrip("/") + "/", "../api/pcsx/search")
    page_size = 10
    workers = max(1, int(os.environ.get("EIGHTFOLD_PAGE_WORKERS", "8")))

    def fetch_page(start: int) -> tuple[int, dict]:
        resp = SESSION.get(
            api_url,
            params={"domain": domain, "query": "", "location": "", "start": start},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return start, resp.json()

    _, first = fetch_page(0)
    data = first.get("data", {})
    total = int(data.get("count") or 0)
    pages = {0: data.get("positions", [])}

    starts = list(range(page_size, total, page_size))
    if starts:
        with ThreadPoolExecutor(max_workers=min(workers, len(starts))) as executor:
            futures = [executor.submit(fetch_page, start) for start in starts]
            for future in as_completed(futures):
                start, payload = future.result()
                pages[start] = payload.get("data", {}).get("positions", [])

    jobs = []
    seen_ids = set()
    for start in sorted(pages):
        for item in pages[start]:
            job_id = str(item.get("id") or item.get("atsJobId") or item.get("displayJobId") or "")
            if not job_id or job_id in seen_ids:
                continue
            seen_ids.add(job_id)
            raw_url = item.get("positionUrl") or ""
            job_url = urljoin(careers_url, raw_url)
            if "domain=" not in job_url:
                separator = "&" if "?" in job_url else "?"
                job_url = f"{job_url}{separator}domain={domain}"
            locations = item.get("locations") or []
            jobs.append(Job(
                id=job_id,
                title=item.get("name", ""),
                url=job_url,
                location=", ".join(locations),
                team=item.get("department", ""),
                company=company_name,
            ))
    return jobs


def fetch_oracle_hcm(company_slug: str, company_name: str) -> list[Job]:
    """Fetch jobs from Oracle HCM candidate experience APIs."""
    try:
        public_url, api_host, site_number, language = [part.strip() for part in company_slug.split("|")]
    except ValueError as exc:
        raise ValueError("Oracle HCM slug must be 'public_url|api_host|site_number|language'") from exc

    api_url = urljoin(api_host.rstrip("/") + "/", "hcmRestApi/resources/latest/recruitingCEJobRequisitions")
    jobs = []
    offset = 0
    limit = 100
    total = None

    while True:
        params = {
            "onlyData": "true",
            "expand": "requisitionList.secondaryLocations,requisitionList.requisitionFlexFields",
            "finder": f"findReqs;siteNumber={site_number},limit={limit},offset={offset},sortBy=POSTING_DATES_DESC",
        }
        resp = SESSION.get(
            api_url,
            params=params,
            headers={**REQUEST_HEADERS, "Accept": "application/json", "Ora-Irc-Language": language, "Referer": public_url},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        search = (data.get("items") or [{}])[0]
        postings = search.get("requisitionList", [])
        if total is None:
            total = int(search.get("TotalJobsCount") or 0)
        if not postings:
            break

        for item in postings:
            job_id = str(item.get("Id", ""))
            jobs.append(Job(
                id=job_id,
                title=item.get("Title", ""),
                url=urljoin(public_url.rstrip("/") + "/", f"../job/{job_id}"),
                location=item.get("PrimaryLocation", ""),
                team=item.get("JobFunction") or item.get("JobFamily") or "",
                company=company_name,
            ))

        offset += limit
        if total and offset >= total:
            break
        time.sleep(0.2)

    return jobs


def fetch_successfactors(career_url: str, company_name: str) -> list[Job]:
    """Fetch jobs from SAP SuccessFactors public search pages."""
    from bs4 import BeautifulSoup

    def fetch_page(startrow: int) -> str:
        last_error = None
        for attempt in range(3):
            try:
                resp = SESSION.get(
                    career_url,
                    params={"q": "", "sortColumn": "referencedate", "sortDirection": "desc", "startrow": startrow},
                    headers={**REQUEST_HEADERS, "Connection": "close"},
                    timeout=max(REQUEST_TIMEOUT, 45),
                )
                resp.raise_for_status()
                return resp.text
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(1 + attempt)
        raise last_error

    first_html = fetch_page(0)
    soup = BeautifulSoup(first_html, "html.parser")
    total_match = re.search(r"Showing\s+\d+\s+to\s+\d+\s+of\s+([\d,]+)\s+Jobs", soup.get_text(" ", strip=True))
    total = int(total_match.group(1).replace(",", "")) if total_match else 0
    per_page = int(soup.select_one("[data-per-page]").get("data-per-page", "25")) if soup.select_one("[data-per-page]") else 25

    jobs = []
    seen_ids = set()

    def add_jobs(html: str):
        page_soup = BeautifulSoup(html, "html.parser")
        for tile in page_soup.select("li.job-tile"):
            a_tag = tile.select_one("a.jobTitle-link")
            if not a_tag:
                continue
            href = urljoin(career_url, tile.get("data-url") or a_tag.get("href", ""))
            job_id_match = re.search(r"/(\d+)/?$", href)
            job_id = job_id_match.group(1) if job_id_match else stable_id(href)
            if job_id in seen_ids:
                continue
            seen_ids.add(job_id)
            location_node = tile.select_one('div[id$="-desktop-section-location-value"]')
            team_node = tile.select_one('div[id$="-desktop-section-dept-value"], div[id$="-desktop-section-customfield1-value"]')
            jobs.append(Job(
                id=job_id,
                title=clean_text(a_tag.get_text(" ", strip=True)),
                url=href,
                location=clean_text(location_node.get_text(" ", strip=True)) if location_node else "",
                team=clean_text(team_node.get_text(" ", strip=True)) if team_node else "",
                company=company_name,
            ))

    add_jobs(first_html)
    for startrow in range(per_page, total, per_page):
        add_jobs(fetch_page(startrow))
        time.sleep(0.2)

    return jobs


def fetch_radancy(career_url: str, company_name: str) -> list[Job]:
    """Fetch jobs from Radancy/TalentBrew search pages."""
    from bs4 import BeautifulSoup

    resp = SESSION.get(career_url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    root = soup.select_one("#search-results")
    if not root:
        return []

    attrs = root.attrs
    total_pages = int(attrs.get("data-total-pages") or 1)
    records_per_page = int(attrs.get("data-records-per-page") or 15)
    total_results = int(attrs.get("data-total-results") or 0)
    ajax_url = urljoin(career_url, attrs.get("data-ajax-url") or "/search-jobs/results")

    jobs = []
    seen_ids = set()

    def add_jobs(html: str):
        page_soup = BeautifulSoup(html, "html.parser")
        for a_tag in page_soup.select('a[href*="/job/"]'):
            href = urljoin(career_url, a_tag.get("href", ""))
            job_id = a_tag.get("data-job-id") or stable_id(href)
            if job_id in seen_ids:
                continue
            seen_ids.add(job_id)
            parts = [clean_text(part) for part in a_tag.get_text("\n", strip=True).splitlines()]
            parts = [part for part in parts if part]
            title = parts[0] if parts else clean_text(a_tag.get_text(" ", strip=True))
            location = parts[1] if len(parts) > 1 and "job id" not in parts[1].lower() else ""
            team = ""
            for index, part in enumerate(parts):
                if part.lower().startswith("category") and index + 1 < len(parts):
                    team = parts[index + 1]
                    break
            jobs.append(Job(
                id=job_id,
                title=title,
                url=href,
                location=location,
                team=team,
                company=company_name,
            ))

    add_jobs(resp.text)
    for page in range(2, total_pages + 1):
        params = {
            "ActiveFacetID": 0,
            "CurrentPage": page,
            "RecordsPerPage": records_per_page,
            "TotalPages": total_pages,
            "TotalResults": total_results,
            "Distance": 50,
            "RadiusUnitType": 2,
            "Keywords": "",
            "Location": "",
            "Latitude": "",
            "Longitude": "",
            "ShowRadius": "False",
            "IsPagination": "True",
            "CustomFacetName": "",
            "FacetTerm": "",
            "FacetType": 0,
            "SearchResultsModuleName": attrs.get("data-search-results-module-name") or "Search Results",
            "SortCriteria": attrs.get("data-sort-criteria") or 0,
            "SortDirection": attrs.get("data-sort-direction") or 1,
            "SearchType": attrs.get("data-search-type") or 5,
            "CategoryFacetTerm": "",
            "CategoryFacetType": "",
            "LocationFacetTerm": "",
            "LocationFacetType": "",
            "KeywordType": "",
            "LocationType": "",
            "LocationPath": "",
            "OrganizationIds": "",
            "PostalCode": "",
            "ResultsType": 0,
            "fc": "",
            "fl": "",
            "fcf": "",
            "afc": "",
            "afl": "",
            "afcf": "",
        }
        page_resp = SESSION.get(ajax_url, params=params, timeout=REQUEST_TIMEOUT)
        page_resp.raise_for_status()
        payload = page_resp.json()
        add_jobs(payload.get("results", ""))
        time.sleep(0.1)

    return jobs


def fetch_asml_sitemap(sitemap_url: str, company_name: str) -> list[Job]:
    """Fetch ASML jobs from its public job-posting sitemap."""
    import xml.etree.ElementTree as ET

    resp = SESSION.get(sitemap_url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    jobs = []
    for loc in root.findall(".//{*}loc"):
        job_url = (loc.text or "").strip()
        if not job_url:
            continue
        slug = urlparse(job_url).path.rstrip("/").split("/")[-1]
        title = re.sub(r"-j\d+$", "", slug, flags=re.I).replace("-", " ").strip().title()
        jobs.append(Job(
            id=stable_id(job_url),
            title=title,
            url=job_url,
            company=company_name,
        ))
    return jobs


def fetch_samsungsemi(company_slug: str, company_name: str) -> list[Job]:
    """Fetch Samsung Semiconductor jobs from its public search API."""
    api_url = "https://search.semiconductor.samsung.com/semi/insightfinder"
    boards = [
        ("semius", "careersJob"),
        ("semiemea", "careersJob"),
        ("semissir", "workday"),
    ]
    jobs = []
    seen_urls = set()

    for site, category in boards:
        start = 0
        page = 1
        page_size = 10
        total = None
        while True:
            params = {
                "onlyfilter": "N",
                "filter": "",
                "sort": "Newest",
                "stage": "real",
                "pagetype": "page",
                "site": site,
                "category": category,
                "q": "",
                "startno": start,
                "pageno": page,
                "num": page_size,
            }
            resp = SESSION.get(api_url, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            result_list = resp.json().get("response", {}).get("resultData", {}).get("resultList", [])
            result = result_list[0] if result_list else {}
            if total is None:
                total = int(result.get("resultCount") or 0)
            items = result.get("insightLandingContentList") or result.get("contentList") or []
            if not items:
                break

            for item in items:
                job_url = item.get("careersUrl") or item.get("workdayUrl") or item.get("pageUrl") or item.get("dispUrl") or item.get("docIdUrl") or ""
                if not job_url or job_url in seen_urls:
                    continue
                seen_urls.add(job_url)
                title = item.get("careersTitle") or item.get("workdayTitle") or item.get("title") or ""
                location = item.get("careersLocation") or item.get("workdayLocation") or ""
                jobs.append(Job(
                    id=stable_id(job_url),
                    title=clean_text(title),
                    url=job_url,
                    location=location,
                    team=item.get("careersCtgry") or item.get("workdayCtgry") or "",
                    company=company_name,
                ))

            start += page_size
            page += 1
            if total and start >= total:
                break
            time.sleep(0.1)

    return jobs


def fetch_google_careers(search_url: str, company_name: str) -> list[Job]:
    """Fetch jobs from a Google Careers search-results URL."""
    from bs4 import BeautifulSoup

    resp = SESSION.get(search_url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    jobs = []
    seen_ids = set()
    app_base_url = "https://www.google.com/about/careers/applications/"

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        match = re.search(r"jobs/results/(\d+)-", href)
        if not match:
            continue

        job_id = match.group(1)
        if job_id in seen_ids:
            continue

        aria_label = a_tag.get("aria-label", "")
        title = re.sub(r"^Learn more about\s+", "", aria_label).strip()
        card = a_tag.find_parent("div", class_="sMn82b")
        lines = [clean_text(line) for line in card.get_text("\n", strip=True).splitlines()] if card else []
        lines = [line for line in lines if line]
        if not title and lines:
            title = lines[0]

        location = ""
        team = ""
        if "place" in lines:
            index = lines.index("place")
            if index + 1 < len(lines):
                location = lines[index + 1]
        if "bar_chart" in lines:
            index = lines.index("bar_chart")
            if index + 1 < len(lines):
                team = lines[index + 1]

        seen_ids.add(job_id)
        jobs.append(Job(
            id=job_id,
            title=title,
            url=urljoin(app_base_url, href),
            location=location,
            team=team,
            company=company_name,
        ))

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
    "jazzhr": fetch_jazzhr,
    "bamboohr": fetch_bamboohr,
    "jibe": fetch_jibe,
    "eightfold": fetch_eightfold,
    "oracle_hcm": fetch_oracle_hcm,
    "successfactors": fetch_successfactors,
    "radancy": fetch_radancy,
    "asml_sitemap": fetch_asml_sitemap,
    "samsungsemi": fetch_samsungsemi,
    "google_careers": fetch_google_careers,
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


def get_state_meta(state: dict) -> dict:
    """Return persistent metadata stored alongside company job IDs."""
    meta = state.get("_meta")
    if not isinstance(meta, dict):
        meta = {}
        state["_meta"] = meta
    return meta


def prune_state_for_configured_companies(state: dict, companies: list[dict]) -> dict:
    """Keep configured companies plus internal metadata."""
    configured_company_names = {company["name"] for company in companies}
    pruned = {
        company_name: job_ids
        for company_name, job_ids in state.items()
        if company_name in configured_company_names
    }
    if isinstance(state.get("_meta"), dict):
        pruned["_meta"] = state["_meta"]
    return pruned


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
        if keyword in {"co-operative", "cooperative"}:
            if re.search(r"\bco[-\s]?operative\b", text):
                return True
            continue
        if keyword == "university":
            if re.search(r"\buniversit(?:y|ies)\b", text):
                return True
            continue
        if keyword == "apprentice":
            if re.search(r"\bapprentice(?:ship)?s?\b", text):
                return True
            continue
        if keyword == "trainee":
            if re.search(r"\btrainee(?:ship)?s?\b", text):
                return True
            continue
        if re.search(rf"\b{re.escape(keyword)}s?\b", text):
            return True
    return False


def clean_markdown_text(value: str) -> str:
    """Keep alert lines readable if an ATS returns newlines or empty fields."""
    return re.sub(r"\s+", " ", value or "").strip()


def format_job_line(job: Job, link_style: str = "markdown", number: Optional[int] = None) -> str:
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
    prefix = f"{number}. " if number is not None else "- "
    return f"{prefix}{title_part} - {' | '.join(details)}"


def chunk_sectioned_alerts(
    sections: list[tuple[str, list[Job]]],
    link_style: str = "markdown",
    max_length: int = 1900,
    header_format: str = "**{title}**",
    numbered_sections: Optional[set[str]] = None,
) -> list[str]:
    """Build one or more sectioned alert messages within platform limits."""
    messages: list[str] = []
    current = ""
    numbered_sections = numbered_sections or set()

    for section_title, jobs in sections:
        header = header_format.format(title=section_title) + "\n"
        sorted_jobs = sorted(jobs, key=lambda j: (j.company, j.title))
        if section_title in numbered_sections:
            lines = [
                format_job_line(job, link_style, number=index)
                for index, job in enumerate(sorted_jobs, start=1)
            ]
        else:
            lines = [format_job_line(job, link_style) for job in sorted_jobs]
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


def build_reminder_map(internship_jobs: list[Job]) -> dict:
    """Map visible reminder numbers to persistent job metadata."""
    reminder_map = {}
    sorted_jobs = sorted(internship_jobs, key=lambda j: (j.company, j.title))
    for index, job in enumerate(sorted_jobs, start=1):
        reminder_map[str(index)] = {
            "id": job.id,
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "url": job.url,
        }
    return reminder_map


def send_plain_webhook(webhook_url: str, content: str) -> bool:
    """Send a simple Discord/Slack-compatible text message."""
    webhook_url = webhook_url.strip()
    if not webhook_url:
        return False
    if webhook_url.rstrip("/").endswith("/slack") and "discord" in webhook_url:
        webhook_url = webhook_url.rstrip("/")[:-len("/slack")]

    is_discord = "discord.com" in webhook_url or "discordapp.com" in webhook_url
    payload = {"content": content, "allowed_mentions": {"parse": []}} if is_discord else {"text": content}
    try:
        resp = SESSION.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        log.error("Webhook delivery failed: %s", e)
        return False


def print_alert_message(message: str):
    """Print alerts safely on Windows consoles with non-UTF-8 encodings."""
    try:
        print(message)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        safe_message = message.encode(encoding, errors="replace").decode(encoding, errors="replace")
        print(safe_message)


def fetch_discord_messages(bot_token: str, channel_id: str, after_id: str = "") -> list[dict]:
    """Fetch recent Discord channel messages using a bot token."""
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {bot_token}"}
    seen_messages: dict[str, dict] = {}
    cursor = str(after_id or "")

    while len(seen_messages) < DISCORD_COMMAND_FETCH_LIMIT:
        params = {"limit": min(100, DISCORD_COMMAND_FETCH_LIMIT - len(seen_messages))}
        if cursor:
            params["after"] = cursor
        resp = SESSION.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break

        new_messages = 0
        for message in batch:
            message_id = str(message["id"])
            if message_id not in seen_messages:
                seen_messages[message_id] = message
                new_messages += 1

        sorted_batch = sorted(batch, key=lambda msg: int(msg["id"]))
        highest_id = str(sorted_batch[-1]["id"])
        if highest_id == cursor or new_messages == 0:
            break
        cursor = highest_id
        if len(batch) < 100:
            break

    if len(seen_messages) >= DISCORD_COMMAND_FETCH_LIMIT:
        log.warning(
            "Fetched Discord command message limit (%d). Increase DISCORD_COMMAND_FETCH_LIMIT if commands are missed.",
            DISCORD_COMMAND_FETCH_LIMIT,
        )

    return sorted(seen_messages.values(), key=lambda msg: int(msg["id"]))


def parse_command_numbers(text: str) -> list[int]:
    """Parse command numbers, including ranges such as '3-7'."""
    numbers = []
    range_pattern = r"\b(\d+)\s*-\s*(\d+)\b"

    for match in re.finditer(range_pattern, text):
        start = int(match.group(1))
        end = int(match.group(2))
        step = 1 if start <= end else -1
        numbers.extend(range(start, end + step, step))

    text_without_ranges = re.sub(range_pattern, " ", text)
    numbers.extend(int(n) for n in re.findall(r"\b\d+\b", text_without_ranges))

    deduped = []
    seen = set()
    for number in numbers:
        if number in seen:
            continue
        seen.add(number)
        deduped.append(number)
    return deduped


def parse_discord_command(content: str) -> Optional[tuple[str, list[int], bool]]:
    """
    Parse commands:
      ignore 3 7 12
      ignore 3-7 12
      unignore 3
      ignore all
      ignored
      job help
    """
    match = re.match(r"^\s*(?:!job\s+|job\s+|!)?(ignore|unignore|ignored|help)\b(.*)$", content, re.I)
    if not match:
        return None

    command = match.group(1).lower()
    rest = match.group(2).strip().lower()
    all_requested = bool(re.search(r"\ball\b", rest))
    numbers = parse_command_numbers(rest)
    return command, numbers, all_requested


def describe_mapped_job(mapped_job: dict) -> str:
    title = clean_markdown_text(mapped_job.get("title", "")) or "Untitled role"
    company = clean_markdown_text(mapped_job.get("company", "")) or "Unknown company"
    location = clean_markdown_text(mapped_job.get("location", ""))
    suffix = f" - {location}" if location else ""
    return f"{title} @ {company}{suffix}"


def process_discord_commands(state: dict, bot_token: str, channel_id: str, webhook_url: str) -> bool:
    """Process ignore/unignore commands from Discord and persist them in state metadata."""
    meta = get_state_meta(state)
    status = {
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "status": "unknown",
    }
    meta["last_discord_command_status"] = status

    if not bot_token or not channel_id:
        missing = []
        if not bot_token:
            missing.append("DISCORD_BOT_TOKEN")
        if not channel_id:
            missing.append("DISCORD_CHANNEL_ID")
        status.update({"status": "disabled", "missing": missing})
        log.warning("Discord command processing disabled; missing %s", ", ".join(missing))
        return False

    last_message_id = str(meta.get("last_discord_command_message_id", ""))
    try:
        messages = fetch_discord_messages(bot_token, channel_id, last_message_id)
    except Exception as e:
        status.update({"status": "fetch_failed", "error": str(e)})
        log.error("Failed to fetch Discord messages for commands: %s", e)
        return False

    if not messages:
        status.update({
            "status": "ok",
            "messages_fetched": 0,
            "commands_processed": 0,
            "last_message_id": last_message_id,
        })
        return False

    reminder_map = meta.get("last_reminder_map", {})
    ignored_ids = set(meta.get("ignored_internship_job_ids", []))
    changed = False
    commands_processed = 0
    user_messages_seen = 0

    for message in messages:
        meta["last_discord_command_message_id"] = message["id"]
        author = message.get("author", {})
        if author.get("bot") or message.get("webhook_id"):
            continue
        user_messages_seen += 1

        parsed = parse_discord_command(message.get("content", ""))
        if not parsed:
            continue

        commands_processed += 1
        command, numbers, all_requested = parsed
        if command == "help":
            send_plain_webhook(
                webhook_url,
                "Job Monitor commands:\n"
                "- `ignore 3 7 12` or `ignore 3-7 12` hides numbered internship/co-op reminders.\n"
                "- `unignore 3 7` or `unignore 3-7` shows numbered reminders again.\n"
                "- `ignore all` hides all currently numbered reminders.\n"
                "- `ignored` lists how many reminder jobs are hidden.",
            )
            continue

        if command == "ignored":
            send_plain_webhook(
                webhook_url,
                f"Currently ignoring {len(ignored_ids)} internship/co-op reminder posting(s).",
            )
            continue

        if not reminder_map:
            send_plain_webhook(
                webhook_url,
                "I do not have a numbered reminder list yet. Wait for the next alert, then reply with `ignore 3 7`.",
            )
            continue

        selected_numbers = sorted(int(n) for n in reminder_map.keys()) if all_requested else numbers
        if not selected_numbers:
            send_plain_webhook(webhook_url, f"Usage: `{command} 3 7 12` or `{command} 3-7 12`")
            continue

        selected_jobs = []
        missing_numbers = []
        for number in selected_numbers:
            mapped_job = reminder_map.get(str(number))
            if not mapped_job:
                missing_numbers.append(number)
                continue
            selected_jobs.append((number, mapped_job))

        if command == "ignore":
            for _, mapped_job in selected_jobs:
                ignored_ids.add(mapped_job["id"])
            action_label = "Ignored"
        else:
            for _, mapped_job in selected_jobs:
                ignored_ids.discard(mapped_job["id"])
            action_label = "Unignored"

        changed = True
        log.info(
            "Processed Discord %s command: %d selected reminder(s), %d unknown number(s).",
            command,
            len(selected_jobs),
            len(missing_numbers),
        )
        lines = [f"{action_label} {len(selected_jobs)} reminder posting(s)."]
        for number, mapped_job in selected_jobs[:10]:
            lines.append(f"- {number}. {describe_mapped_job(mapped_job)}")
        if len(selected_jobs) > 10:
            lines.append(f"- ...and {len(selected_jobs) - 10} more")
        if missing_numbers:
            shown_missing = ", ".join(str(n) for n in missing_numbers[:25])
            if len(missing_numbers) > 25:
                shown_missing += f", ...and {len(missing_numbers) - 25} more"
            lines.append(f"Unknown reminder number(s): {shown_missing}")
        send_plain_webhook(webhook_url, "\n".join(lines))

    meta["ignored_internship_job_ids"] = sorted(ignored_ids)
    status.update({
        "status": "ok",
        "messages_fetched": len(messages),
        "user_messages_seen": user_messages_seen,
        "commands_processed": commands_processed,
        "last_message_id": meta.get("last_discord_command_message_id", ""),
    })
    return changed


def send_webhook(
    webhook_url: str,
    new_internship_jobs: list[Job],
    internship_jobs: list[Job],
    other_new_jobs: list[Job],
) -> bool:
    """Send a notification via Slack or Discord webhook."""
    if not new_internship_jobs:
        return True

    webhook_url = webhook_url.strip()
    if webhook_url.rstrip("/").endswith("/slack") and "discord" in webhook_url:
        webhook_url = webhook_url.rstrip("/")[:-len("/slack")]

    is_discord = "discord.com" in webhook_url or "discordapp.com" in webhook_url
    link_style = "markdown" if is_discord else "slack"
    messages = chunk_sectioned_alerts(
        [
            (SECTION_NEW_INTERNSHIP_JOBS, new_internship_jobs),
        ],
        link_style=link_style,
        max_length=1900 if is_discord else 2800,
        header_format="**{title}**" if is_discord else "*{title}*",
    )

    delivered_all = True
    for message in messages:
        payload = {"content": message, "allowed_mentions": {"parse": []}} if is_discord else {"text": message}

        try:
            r = SESSION.post(webhook_url, json=payload, timeout=10)
            r.raise_for_status()
            time.sleep(0.5)  # respect rate limits
        except Exception as e:
            delivered_all = False
            log.error("Webhook delivery failed: %s", e)
    return delivered_all


def send_telegram(
    bot_token: str,
    chat_id: str,
    new_internship_jobs: list[Job],
    internship_jobs: list[Job],
    other_new_jobs: list[Job],
) -> bool:
    """Send notifications via Telegram bot."""
    if not new_internship_jobs:
        return True

    messages = chunk_sectioned_alerts(
        [
            (SECTION_NEW_INTERNSHIP_JOBS, new_internship_jobs),
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


def send_test_alert(webhook_url: str, startup_webhook_url: str, telegram_token: str, telegram_chat: str, now: str) -> bool:
    """Send a harmless alert to verify notification wiring."""
    test_new_jobs = [
        Job(
            id="test-new-job",
            title="Test Internship Alert from Job Monitor",
            url="https://github.com/xlinzel/Job-Monitor/actions",
            location="Notification test",
            team="Monitor",
            company="Job Monitor",
            discovered_at=now,
        )
    ]
    test_other_new_jobs = [
        Job(
            id="test-other-new-job",
            title="Example Other New Job",
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
            title="Example Internship / Co-op / Trainee Reminder",
            url="https://github.com/xlinzel/Job-Monitor",
            location="Notification test",
            team="Reminder",
            company="Job Monitor",
            discovered_at=now,
        )
    ]

    delivered = True
    if webhook_url:
        delivered = send_webhook(webhook_url, test_new_jobs, test_internship_jobs, test_other_new_jobs) and delivered
    if startup_webhook_url:
        delivered = send_webhook(startup_webhook_url, test_new_jobs, [], []) and delivered
    if telegram_token and telegram_chat:
        delivered = send_telegram(
            telegram_token,
            telegram_chat,
            test_new_jobs,
            test_internship_jobs,
            test_other_new_jobs,
        ) and delivered
    if not webhook_url and not startup_webhook_url and not telegram_token:
        delivered = False
        log.warning("No notification channel configured - printing test alert to stdout")
        for message in chunk_sectioned_alerts([
            (SECTION_NEW_INTERNSHIP_JOBS, test_new_jobs),
        ]):
            print_alert_message(message)
    return delivered


def fetch_company_current_jobs(company: dict) -> tuple[list[Job], float]:
    """Fetch current jobs for one configured company."""
    name = company["name"]
    ats = company["ats"]
    slug = company.get("slug", "")
    url = company.get("url", "")

    fetcher = FETCHERS.get(ats)
    if not fetcher:
        raise ValueError(f"Unknown ATS type '{ats}'")

    started = time.time()
    target = url if ats == "html" else slug
    current_jobs = fetcher(target, name)
    return current_jobs, time.time() - started


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
    startup_webhook_url = os.environ.get("STARTUP_WEBHOOK_URL", notifications.get("startup_webhook_url", ""))
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", notifications.get("telegram_bot_token", ""))
    telegram_chat = os.environ.get("TELEGRAM_CHAT_ID", notifications.get("telegram_chat_id", ""))
    test_alert = is_truthy_env(os.environ.get("TEST_ALERT", "false"))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    startup_company_names = {company["name"] for company in companies if company.get("startup_channel")}

    if test_alert:
        log.info("TEST_ALERT=true - sending test alert and skipping job fetch/state update.")
        delivered = send_test_alert(webhook_url, startup_webhook_url, telegram_token, telegram_chat, now)
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a") as f:
                f.write(f"## Job Monitor Test Alert - {now}\n\n")
                f.write("Test alert delivered.\n" if delivered else "Test alert failed. Check workflow logs and secrets.\n")
        if not delivered:
            sys.exit(1)
        return

    # Load previous state
    state = prune_state_for_configured_companies(load_state(STATE_PATH), companies)
    all_new_jobs: list[Job] = []

    fetch_results: dict[int, tuple[list[Job], Optional[Exception], float]] = {}
    fetch_workers = min(COMPANY_FETCH_WORKERS, len(companies)) if companies else 1
    log.info("Fetching %d compan%s with %d worker(s)...", len(companies), "y" if len(companies) == 1 else "ies", fetch_workers)

    with ThreadPoolExecutor(max_workers=fetch_workers) as executor:
        future_to_index = {}
        for index, company in enumerate(companies):
            log.info("Checking %s (%s)...", company["name"], company["ats"])
            future = executor.submit(fetch_company_current_jobs, company)
            future_to_index[future] = index

        for future in as_completed(future_to_index):
            index = future_to_index[future]
            company = companies[index]
            name = company["name"]
            try:
                current_jobs, elapsed = future.result()
                fetch_results[index] = (current_jobs, None, elapsed)
                log.info("  -> Fetched %d posting(s) for %s in %.1fs", len(current_jobs), name, elapsed)
            except Exception as e:
                fetch_results[index] = ([], e, 0.0)
                log.error("Failed to fetch %s: %s", name, e)

    for index, company in enumerate(companies):
        name = company["name"]
        include = company.get("include_keywords", global_include)
        exclude = company.get("exclude_keywords", global_exclude)
        current_jobs, error, _ = fetch_results.get(index, ([], RuntimeError("Fetch did not run"), 0.0))
        if error:
            continue

        # Diff against known postings. Legacy state entries are raw job IDs;
        # after this run, state is rewritten with title/location/url fingerprints.
        known_entries = set(state.get(name, []))
        current_keys = {job_state_key(j) for j in current_jobs}

        new_jobs = []
        for job in current_jobs:
            known_by_fingerprint = job_state_key(job) in known_entries
            known_by_legacy_id = job.id in known_entries
            if not known_by_fingerprint and not known_by_legacy_id and job.matches_filters(include, exclude):
                job.discovered_at = now
                new_jobs.append(job)

        if new_jobs:
            log.info("  -> %d new posting(s) for %s", len(new_jobs), name)
            all_new_jobs.extend(new_jobs)
        else:
            log.info("  -> No new postings for %s", name)

        # Update state with current posting fingerprints.
        state[name] = sorted(current_keys)

    # Persist state
    save_state(STATE_PATH, state)

    # Send notifications
    if all_new_jobs:
        new_internship_jobs = [
            job
            for job in all_new_jobs
            if is_internship_job(job, internship_keywords)
        ]
        other_new_jobs = [
            job
            for job in all_new_jobs
            if not is_internship_job(job, internship_keywords)
        ]
        log.info("Sending alerts for %d new posting(s)...", len(all_new_jobs))
        log.info("  -> %d new internship/early-career posting(s)", len(new_internship_jobs))
        log.info("  -> %d non-internship posting(s) suppressed from alerts", len(other_new_jobs))
        if new_internship_jobs:
            startup_new_internship_jobs = [
                job
                for job in new_internship_jobs
                if job.company in startup_company_names
            ]
            if webhook_url:
                send_webhook(webhook_url, new_internship_jobs, [], [])
            if startup_webhook_url:
                log.info("  -> %d startup-channel internship/early-career posting(s)", len(startup_new_internship_jobs))
                send_webhook(startup_webhook_url, startup_new_internship_jobs, [], [])
            if telegram_token and telegram_chat:
                send_telegram(telegram_token, telegram_chat, new_internship_jobs, [], [])
            if not webhook_url and not startup_webhook_url and not telegram_token:
                log.warning("No notification channel configured - printing to stdout")
                for message in chunk_sectioned_alerts([
                    (SECTION_NEW_INTERNSHIP_JOBS, new_internship_jobs),
                ]):
                    print_alert_message(message)
        else:
            log.info("No new internship/early-career postings found; no alert sent.")
    else:
        log.info("No new postings found this run.")

    # Write summary for GitHub Actions
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write(f"## Job Monitor - {now}\n\n")
            if all_new_jobs:
                new_internship_jobs = [
                    job
                    for job in all_new_jobs
                    if is_internship_job(job, internship_keywords)
                ]
                if new_internship_jobs:
                    f.write(f"**{SECTION_NEW_INTERNSHIP_JOBS} ({len(new_internship_jobs)}):**\n\n")
                    for j in new_internship_jobs:
                        f.write(f"- [{j.title}]({j.url}) @ {j.company} ({j.location})\n")
                else:
                    f.write("No new internship/early-career postings.\n")
            else:
                f.write("No new postings.\n")


if __name__ == "__main__":
    run()
