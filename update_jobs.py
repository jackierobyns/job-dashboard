#!/usr/bin/env python3
import json
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

OUT = Path("jobs.json")
NOW = datetime.now(timezone.utc)
CUTOFF = NOW - timedelta(hours=48)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; JacquettaJobDashboard/1.0; +https://github.com/)"
}

# Career areas based on the dashboard/search criteria.
POSITIVE = {
    "operations": 8,
    "operations manager": 12,
    "business operations": 12,
    "program manager": 12,
    "program management": 10,
    "project manager": 10,
    "project management": 8,
    "strategic initiatives": 10,
    "strategy": 5,
    "workforce development": 12,
    "career pathways": 8,
    "organizational effectiveness": 9,
    "process improvement": 9,
    "continuous improvement": 8,
    "lean six sigma": 8,
    "performance management": 8,
    "performance measurement": 8,
    "public health": 10,
    "population health": 10,
    "healthcare administration": 10,
    "healthcare operations": 10,
    "community health": 8,
    "community engagement": 7,
    "public engagement": 7,
    "government relations": 9,
    "external affairs": 9,
    "strategic communications": 8,
    "executive support": 7,
    "chief of staff": 10,
    "stakeholder": 5,
    "grants": 6,
    "grant management": 8,
    "contracts": 5,
    "contract management": 7,
    "vendor management": 6,
    "sharepoint": 6,
    "power bi": 6,
    "dashboard": 4,
    "data analysis": 5,
    "analytics": 4,
    "it operations": 9,
    "service delivery": 7,
    "technical operations": 8,
    "implementation manager": 8,
    "customer success manager": 6,
    "technology operations": 8,
    "it project": 7,
    "it program": 7,
}

# Exclude defense/cleared environments and hard-specialty areas that are poor fits.
EXCLUDE_TERMS = [
    "secret clearance", "top secret", "ts/sci", "ts-sci", "sci clearance",
    "security clearance required", "active clearance", "polygraph",
    "q clearance", "classified program", "classified environment",
    "dod clearance", "department of defense", "defense contractor",
    "clearance required", "ability to obtain a secret",
]

DEFENSE_EMPLOYERS = [
    "northrop grumman", "lockheed martin", "raytheon", "rtx", "booz allen",
    "general dynamics", "leidos", "saic", "bae systems", "l3harris",
    "parsons", "caci", "mantech",
]

HARD_EXCLUDE = [
    "registered nurse", "licensed clinical", "physician", "pharmacist",
    "professional engineer", "software engineer", "electrical engineer",
    "mechanical engineer", "civil engineer", "attorney", "cpa required",
]

LOCATION_HINTS = [
    "baltimore", "maryland", "md", "washington, dc", "washington dc",
    "district of columbia", "remote", "united states",
]


def get(url, **kwargs):
    r = requests.get(url, headers=HEADERS, timeout=30, **kwargs)
    r.raise_for_status()
    return r


def clean(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def parse_money(text):
    """Return a conservative lower annual salary, or None."""
    if not text:
        return None
    t = text.lower().replace(",", "")
    nums = [float(x) for x in re.findall(r"\$?\s*(\d+(?:\.\d+)?)", t)]
    if not nums:
        return None

    # Hourly stated compensation -> annualize at 2080 hours.
    if "hour" in t or "/hr" in t or "hourly" in t:
        plausible = [n for n in nums if 15 <= n <= 300]
        return round(min(plausible) * 2080) if plausible else None

    # Annual salaries. Ignore 4-digit years and tiny values.
    plausible = []
    for n in nums:
        if 10000 <= n <= 1000000:
            plausible.append(n)
        elif 40 <= n <= 500 and ("k" in t):
            plausible.append(n * 1000)
    return round(min(plausible)) if plausible else None


def parse_date(text):
    if not text:
        return None
    text = clean(text)

    # ISO date
    m = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
        except ValueError:
            pass

    # Common US date formats.
    for pat in [
        r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+20\d{2}",
        r"\b\d{1,2}/\d{1,2}/20\d{2}\b",
    ]:
        m = re.search(pat, text, re.I)
        if m:
            for fmt in ("%B %d, %Y", "%m/%d/%Y"):
                try:
                    return datetime.strptime(m.group(0), fmt).replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
    return None


def is_fresh(dt):
    return bool(dt and dt >= CUTOFF and dt <= NOW + timedelta(hours=12))


def disallowed(title, company, description):
    blob = f"{title} {company} {description}".lower()
    if any(x in blob for x in EXCLUDE_TERMS):
        return True
    if any(x in company.lower() for x in DEFENSE_EMPLOYERS):
        return True
    if any(x in blob for x in HARD_EXCLUDE):
        return True
    return False


def score_job(title, description, location):
    blob = f"{title} {description}".lower()
    score = 45
    for phrase, weight in POSITIVE.items():
        if phrase in blob:
            score += weight
    # Geographic preference.
    loc = (location or "").lower()
    if any(h in loc for h in ["baltimore", "maryland", ", md", "remote"]):
        score += 8
    elif "washington" in loc or "district of columbia" in loc:
        score += 5

    # Penalize clear entry-level/support-only roles.
    if any(x in blob for x in ["help desk technician", "desktop support technician", "entry level", "junior help desk"]):
        score -= 20

    return max(0, min(99, score))


def track_for(title, desc):
    blob = f"{title} {desc}".lower()
    if any(x in blob for x in ["workforce", "career pathway", "apprentice"]):
        return "Workforce / Strategic Programs"
    if any(x in blob for x in ["public health", "population health", "health promotion", "healthcare"]):
        return "Healthcare / Public Health"
    if any(x in blob for x in ["external affairs", "government relations", "communications", "public engagement"]):
        return "Communications / External Affairs"
    if any(x in blob for x in ["it operations", "technical operations", "service delivery", "technology operations", "it project", "it program"]):
        return "Technology / IT Operations"
    if any(x in blob for x in ["program manager", "project manager", "project management"]):
        return "Program / Project"
    return "Operations Leadership"


def snippets(title, company, desc, fit):
    why = []
    blob = f"{title} {desc}".lower()
    mapping = [
        ("operations", "Strong overlap with your senior operations leadership and process-improvement background."),
        ("program", "Relevant to your 10+ years of program management, planning, reporting, and cross-functional coordination."),
        ("project", "Matches your project-management, scheduling, stakeholder, and executive-reporting experience."),
        ("workforce", "Directly relevant to your workforce-development program experience in Baltimore."),
        ("health", "Connects well with your healthcare, population-health, and community-health background."),
        ("government relations", "Aligns with your government-relations and public-facing stakeholder experience."),
        ("communications", "Leverages your communications education and executive/public communications experience."),
        ("sharepoint", "Uses your established SharePoint administration and collaboration experience."),
        ("power bi", "Uses your Power BI/dashboard development and performance-reporting skills."),
        ("grant", "Matches your grant administration, review, compliance, and reporting experience."),
    ]
    for term, sentence in mapping:
        if term in blob and sentence not in why:
            why.append(sentence)
        if len(why) >= 3:
            break
    if not why:
        why = ["The role contains transferable operations, stakeholder, and organizational-management responsibilities."]

    gaps = []
    if fit < 75:
        gaps.append("This is a stretch match; review the mandatory qualifications carefully before applying.")
    if "pmp" in blob:
        gaps.append("PMP is mentioned; your current résumé does not list PMP certification.")
    if "sap" in blob:
        gaps.append("SAP is mentioned and is not currently listed on your résumé.")
    if not gaps:
        gaps.append("No obvious clearance or defense requirement was detected; still review mandatory qualifications before applying.")

    summary = (
        "Senior operations and program management leader with 10+ years of experience across "
        "federal civilian operations, healthcare, workforce development, community engagement, "
        "contracts, dashboards, performance improvement, and executive support. Skilled in "
        "cross-functional project execution, stakeholder management, data-driven reporting, "
        "SharePoint, Power BI, Excel, SOP development, and continuous improvement."
    )
    cover = (
        f"I am interested in the {title} opportunity with {company} because it aligns with my "
        "background in operations, program and project management, stakeholder coordination, "
        "performance improvement, and executive-level support. My experience spans federal "
        "civilian operations, healthcare, workforce development, community initiatives, and "
        "data-driven organizational improvement."
    )
    return why, gaps, summary, cover


def md_jobs():
    """Scrape public State of Maryland JobAps recruitments, then verify opening date on detail pages."""
    base = "https://www.jobapscloud.com/MD/"
    try:
        html = get(base + "?KeywordFullText=0").text
    except Exception as e:
        print("Maryland source failed:", e, file=sys.stderr)
        return []

    soup = BeautifulSoup(html, "html.parser")
    found = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if "bulpreview.asp" not in href.lower():
            continue
        url = urljoin(base, href)
        if url in seen:
            continue
        seen.add(url)

        title = clean(a.get_text(" ", strip=True))
        if not title:
            continue

        try:
            detail_html = get(url).text
            detail_soup = BeautifulSoup(detail_html, "html.parser")
            detail = clean(detail_soup.get_text(" ", strip=True))
        except Exception:
            continue

        # Opened / posting date. If we cannot verify it, skip.
        opened = None
        for label in ["Date Opened", "Recruitment Start Date", "Posting Date", "Open Date"]:
            m = re.search(label + r"\s*:?\s*([^|]{0,60})", detail, re.I)
            if m:
                opened = parse_date(m.group(1))
                if opened:
                    break
        if not is_fresh(opened):
            continue

        # Salary.
        salary_text = ""
        m = re.search(r"Salary\s*:?\s*(.{0,160}?)(?:Employment Type|Job Type|Filing Deadline|Introduction|GRADE|Main Purpose)", detail, re.I)
        if m:
            salary_text = clean(m.group(1))
        else:
            sm = re.search(r"\$[\d,]+(?:\.\d+)?\s*(?:-|–|to)\s*\$?[\d,]+(?:\.\d+)?(?:\s*/\s*(?:year|hour))?", detail, re.I)
            if sm:
                salary_text = clean(sm.group(0))
        salary_min = parse_money(salary_text)
        if not salary_min or salary_min < 75000:
            continue

        # Agency and location.
        agency = "State of Maryland"
        am = re.search(r"Agency\s*:?\s*(.{2,100}?)(?:Salary|Work Location|Main Purpose|GRADE)", detail, re.I)
        if am:
            agency = clean(am.group(1))
        location = "Maryland"
        lm = re.search(r"Work Location\s*:?\s*(.{2,100}?)(?:Agency|Salary|Main Purpose|GRADE)", detail, re.I)
        if lm:
            location = clean(lm.group(1))

        if disallowed(title, agency, detail):
            continue

        fit = score_job(title, detail, location)
        if fit < 65:
            continue

        req = ""
        rm = re.search(r"\b(\d{2}-\d{6}-\d{4})\b", detail)
        if rm:
            req = rm.group(1)

        why, gaps, summary, cover = snippets(title, agency, detail, fit)
        keywords = [k for k in POSITIVE if k in f"{title} {detail}".lower()][:10]

        found.append({
            "id": f"md-{req or re.sub(r'[^a-z0-9]+','-',title.lower()).strip('-')}",
            "title": title,
            "company": agency,
            "location": location,
            "work_mode": "See posting",
            "posted": opened.date().isoformat(),
            "verified_fresh": True,
            "salary": salary_text or f"${salary_min:,}+",
            "salary_min": salary_min,
            "track": track_for(title, detail),
            "fit": fit,
            "source": "State of Maryland official careers",
            "url": url,
            "req": req,
            "why": why,
            "gaps": gaps,
            "keywords": keywords,
            "summary": summary,
            "cover": cover,
            "status": "New"
        })
        time.sleep(0.15)

    return found


def remotive_jobs():
    """Use Remotive's public remote jobs API. Only keep jobs with explicit salary text."""
    url = "https://remotive.com/api/remote-jobs"
    try:
        data = get(url).json()
    except Exception as e:
        print("Remotive source failed:", e, file=sys.stderr)
        return []

    found = []
    for j in data.get("jobs", []):
        title = clean(j.get("title"))
        company = clean(j.get("company_name"))
        location = clean(j.get("candidate_required_location") or "Remote")
        desc_html = j.get("description") or ""
        desc = clean(BeautifulSoup(desc_html, "html.parser").get_text(" ", strip=True))
        pub = parse_date(j.get("publication_date", "")[:10])
        if not is_fresh(pub):
            continue

        salary_text = clean(j.get("salary") or "")
        salary_min = parse_money(salary_text)
        if not salary_min or salary_min < 75000:
            continue
        if disallowed(title, company, desc):
            continue

        fit = score_job(title, desc, location)
        if fit < 65:
            continue

        why, gaps, summary, cover = snippets(title, company, desc, fit)
        keywords = [k for k in POSITIVE if k in f"{title} {desc}".lower()][:10]

        found.append({
            "id": f"remotive-{j.get('id')}",
            "title": title,
            "company": company,
            "location": location,
            "work_mode": "Remote",
            "posted": pub.date().isoformat(),
            "verified_fresh": True,
            "salary": salary_text,
            "salary_min": salary_min,
            "track": track_for(title, desc),
            "fit": fit,
            "source": "Remotive public job feed",
            "url": j.get("url") or "",
            "req": "",
            "why": why,
            "gaps": gaps,
            "keywords": keywords,
            "summary": summary,
            "cover": cover,
            "status": "New"
        })
    return found


def remoteok_jobs():
    """Use Remote OK's public JSON feed when explicit salary and date are available."""
    url = "https://remoteok.com/api"
    try:
        data = get(url).json()
    except Exception as e:
        print("RemoteOK source failed:", e, file=sys.stderr)
        return []

    found = []
    if not isinstance(data, list):
        return found

    for j in data:
        if not isinstance(j, dict) or not j.get("position"):
            continue
        title = clean(j.get("position"))
        company = clean(j.get("company"))
        location = clean(j.get("location") or "Remote")
        desc = clean(BeautifulSoup(j.get("description") or "", "html.parser").get_text(" ", strip=True))

        pub = None
        if j.get("date"):
            pub = parse_date(str(j.get("date"))[:10])
        elif j.get("epoch"):
            try:
                pub = datetime.fromtimestamp(int(j["epoch"]), tz=timezone.utc)
            except Exception:
                pass
        if not is_fresh(pub):
            continue

        min_salary = j.get("salary_min")
        max_salary = j.get("salary_max")
        try:
            salary_min = int(float(min_salary)) if min_salary not in (None, "", 0, "0") else None
        except Exception:
            salary_min = None
        if not salary_min or salary_min < 75000:
            continue

        salary_text = f"${salary_min:,}"
        try:
            if max_salary:
                salary_text += f"–${int(float(max_salary)):,}"
        except Exception:
            pass

        if disallowed(title, company, desc):
            continue
        fit = score_job(title, desc, location)
        if fit < 65:
            continue

        why, gaps, summary, cover = snippets(title, company, desc, fit)
        keywords = [k for k in POSITIVE if k in f"{title} {desc}".lower()][:10]
        job_url = j.get("url") or j.get("apply_url") or ""
        if job_url and job_url.startswith("/"):
            job_url = "https://remoteok.com" + job_url

        found.append({
            "id": f"remoteok-{j.get('id') or re.sub(r'[^a-z0-9]+','-',company.lower()+'-'+title.lower()).strip('-')}",
            "title": title,
            "company": company,
            "location": location,
            "work_mode": "Remote",
            "posted": pub.date().isoformat(),
            "verified_fresh": True,
            "salary": salary_text,
            "salary_min": salary_min,
            "track": track_for(title, desc),
            "fit": fit,
            "source": "Remote OK public job feed",
            "url": job_url,
            "req": "",
            "why": why,
            "gaps": gaps,
            "keywords": keywords,
            "summary": summary,
            "cover": cover,
            "status": "New"
        })
    return found


def main():
    all_jobs = []
    for source in (md_jobs, remotive_jobs, remoteok_jobs):
        try:
            all_jobs.extend(source())
        except Exception as e:
            print(f"{source.__name__} failed:", e, file=sys.stderr)

    # Deduplicate by company/title/location.
    unique = {}
    for j in all_jobs:
        key = (
            re.sub(r"\W+", "", j["company"].lower()),
            re.sub(r"\W+", "", j["title"].lower()),
            re.sub(r"\W+", "", (j.get("location") or "").lower()),
        )
        existing = unique.get(key)
        if not existing or j["fit"] > existing["fit"]:
            unique[key] = j

    jobs = list(unique.values())
    jobs.sort(key=lambda j: (j["fit"], j["salary_min"], j["posted"]), reverse=True)
    jobs = jobs[:10]

    payload = {
        "generated_at": NOW.isoformat(),
        "criteria": {
            "freshness_hours": 48,
            "salary_floor": 75000,
            "defense_and_elevated_clearance_roles": "excluded",
            "max_results": 10
        },
        "jobs": jobs
    }
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(jobs)} qualifying jobs to {OUT}")


if __name__ == "__main__":
    main()
