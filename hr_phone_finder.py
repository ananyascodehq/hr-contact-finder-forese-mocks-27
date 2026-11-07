#!/usr/bin/env python3
"""Find publicly listed HR / talent acquisition phone numbers for a list of companies.

Searches the web through the Serper.dev API, reads the public pages it finds, and
keeps an Indian phone number only when the text right around it mentions both the
company and an HR role.

Usage:
    python hr_phone_finder.py companies.txt --city Chennai --out hr_contacts.csv
"""
import argparse
import csv
import io
import json
import logging
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib import robotparser
from urllib.parse import urlparse

import phonenumbers
import requests
from bs4 import BeautifulSoup
from phonenumbers import PhoneNumberFormat, PhoneNumberType
from pypdf import PdfReader

SEARCH_URL = "https://google.serper.dev/search"
USER_AGENT = "SVCE-MockPlacement-ContactFinder/1.0"
TIMEOUT = 15
MAX_PAGE_BYTES = 3_000_000
PAGES_PER_QUERY = 5
CONTEXT_CHARS = 150  # how far either side of a number to look for company / HR words
MAX_PDF_PAGES = 30
SEARCH_ATTEMPTS = 3  # tries per search when the network or the API has a brief problem
RETRY_DELAY = 2  # seconds, doubled after each failed try
MAX_FAILED_SEARCHES_IN_A_ROW = 6  # then stop, rather than failing through every company
OLD_AFTER_DAYS = 548  # about 18 months; older pages are read last and flagged

# Each phrase costs one search credit per company. Edit these to tune what gets found.
SEARCH_PHRASES = [
    '"{company}" (HR OR recruiter OR "talent acquisition") contact number "+91" {city}',
    '"{company}" {city} walk-in drive HR contact number',
    '"{company}" ("campus hiring" OR "campus recruitment") HR contact number {city}',
    '"{company}" ("HR head" OR "HR manager" OR "head of talent acquisition") phone {city}',
    '"{company}" HR contact number {city} filetype:pdf',
]

logging.getLogger("pypdf").setLevel(logging.ERROR)  # malformed PDFs are common and harmless

# Sites we never read:
#  - login-walled or terms-restricted social and job sites
#  - contact-data resellers and caller-ID lookups, whose numbers people did not publish
BLOCKED_DOMAINS = {
    "linkedin.com", "facebook.com", "instagram.com", "x.com", "twitter.com",
    "naukri.com", "indeed.com", "glassdoor.com", "glassdoor.co.in",
    "apollo.io", "zoominfo.com", "lusha.com", "rocketreach.co", "signalhire.com",
    "contactout.com", "truecaller.com", "getcontact.com",
}

HR_WORDS = re.compile(
    r"\b(hr|h\.r\.|human resources?|talent acquisition|recruiters?|recruitment|"
    r"recruiting|hiring manager|campus hiring|ta (lead|manager|partner|specialist))\b",
    re.I,
)
AGENCY_WORDS = re.compile(
    r"\b(consultants?|consultancy|staffing|manpower|placement services?|outsourcing)\b",
    re.I,
)
ALLOWED_LINE_TYPES = {
    PhoneNumberType.MOBILE: "mobile",
    PhoneNumberType.FIXED_LINE: "landline",
    PhoneNumberType.FIXED_LINE_OR_MOBILE: "unknown",
}
CSV_COLUMNS = ["company", "hr_name", "phone", "line_type", "page_date", "sources_found", "check_first", "source_url"]

# Names are read from how recruiters usually write them next to a number. Only these
# tight patterns count, so a wrong guess is rare and "no name" is the common fallback.
NAME_WORD = r"(?:[A-Z][a-z]+|[A-Z]{2,})"
NAME = (
    r"(?<![\w.])(?:(?i:mr|ms|mrs|dr)\.?\s+)?"
    rf"((?:[A-Z]\.?\s+){{0,2}}{NAME_WORD}(?:\s+[A-Z]\.?(?!\w))?(?:\s+{NAME_WORD}){{0,2}})"
)
SEPARATOR = r"[\s:,\-\u2013\u2014()|/]"
GAP = (
    rf"{SEPARATOR}*(?i:(?:(?:at|on|call|phone|mobile|mob|ph|number|no|contact|whatsapp|tel)\b"
    rf"[\s.:,\-\u2013\u2014()|/]*){{0,2}})$"
)
ROLE = (
    r"(?i:hr\s+(?:manager|executive|lead|head|partner|recruiter)|hr|h\.r\.|human resources?|"
    r"talent acquisition(?:\s+(?:lead|manager|partner|specialist|executive))?|"
    r"recruiter|recruitment\s+(?:lead|manager|executive)|hiring manager)"
)
LABEL = rf"(?:{ROLE}|(?i:contact person|contact|spoc|reach out to|call))"
NAME_PATTERNS = [
    re.compile(rf"{LABEL}{SEPARATOR}+{NAME}{GAP}"),  # HR Manager: Priya Sharma, 98765...
    re.compile(rf"{NAME}{SEPARATOR}*{ROLE}{SEPARATOR}*{GAP}"),  # Priya Sharma (HR) - 98765...
]
NAME_AFTER_NUMBER = re.compile(rf"^\s*(?:\(\s*|[\-\u2013\u2014]\s*){NAME}")  # 98765... (Priya)
NOT_NAME_WORDS = {
    "acquisition", "address", "apply", "bangalore", "bengaluru", "call", "chennai", "college",
    "company", "consultancy", "consultants", "contact", "corp", "date", "department", "desk",
    "drive", "email", "executive", "fresher", "freshers", "group", "head", "helpline", "hiring",
    "human", "hyderabad", "inc", "india", "industries", "interview", "job", "jobs", "lead",
    "limited", "location", "ltd", "manager", "mobile", "mumbai", "note", "number", "office",
    "partner", "phone", "please", "private", "pvt", "recruiter", "recruitment", "regards",
    "resources", "resume", "send", "services", "share", "software", "solutions", "specialist",
    "staffing", "systems", "talent", "team", "tech", "technologies", "thanks", "timing",
    "university", "venue", "walk", "whatsapp",
    "and", "are", "at", "bpo", "by", "career", "careers", "for", "from", "hr", "immediate", "in",
    "it", "join", "labs", "looking", "mnc", "new", "of", "on", "opening", "openings", "our",
    "required", "the", "this", "to", "urgent", "vacancies", "vacancy", "we", "with", "your",
}


class SearchStopped(Exception):
    """The search API refused the request (bad key or no credits left)."""


@dataclass
class Company:
    name: str
    pattern: re.Pattern


@dataclass
class Contact:
    company: str
    phone_key: str  # E.164, used only for de-duplication
    phone: str
    line_type: str
    names: list = field(default_factory=list)
    urls: list = field(default_factory=list)
    possible_agency: bool = False
    page_date: str = ""  # newest page date seen, as YYYY-MM-DD


def parse_company(line):
    """'TCS | Tata Consultancy Services' -> first name shown, every name matched."""
    names = [part.strip() for part in line.split("|") if part.strip()]
    pattern = "|".join(re.escape(name) for name in names)
    return Company(names[0], re.compile(rf"(?<!\w)({pattern})(?!\w)", re.I))


def parse_companies(lines):
    """Parse company lines, ignoring lines with no name and repeats of the same company."""
    companies = {}
    for line in lines:
        if any(part.strip() for part in line.split("|")):
            company = parse_company(line)
            companies.setdefault(company.name.lower(), company)
    return list(companies.values())


def load_lines(path):
    # errors="replace" so a file saved in another encoding can't stop the run.
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        return [s for s in (line.strip() for line in f) if s and not s.startswith("#")]


def is_blocked(url):
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in BLOCKED_DOMAINS)


def e164(raw):
    """Normalise a number from the do-not-call file; None if it can't be parsed."""
    try:
        return phonenumbers.format_number(
            phonenumbers.parse(raw, "IN"), PhoneNumberFormat.E164
        )
    except phonenumbers.NumberParseException:
        return None


def clean_name(candidate, company):
    """Trim words like "Call" off the front; return "" if what's left isn't clearly a name."""
    words = candidate.split()
    while words and words[0].lower() in NOT_NAME_WORDS:
        words.pop(0)
    name = " ".join(words)
    if not name or company.pattern.search(name):
        return ""
    if any(word.lower().strip(".") in NOT_NAME_WORDS for word in words):
        return ""
    capitals = [word for word in words if word.isupper() and len(word.strip(".")) > 1]
    if capitals:
        # A lone "ABC" or a two-letter "KV" is more likely a company or abbreviation.
        if len(words) == 1 or any(len(word) < 3 for word in capitals):
            return ""
        name = " ".join(word.capitalize() if word in capitals else word for word in words)
    return name


def find_name(before, after, company):
    """Find the HR person's name right beside a number, or "" when it isn't clear."""
    for pattern in NAME_PATTERNS:
        match = pattern.search(before)
        if match:
            name = clean_name(match.group(1), company)
            if name:
                return name
    match = NAME_AFTER_NUMBER.search(after)
    return clean_name(match.group(1), company) if match else ""


def extract_numbers(text, title, company):
    """Return [(e164, display, line_type, possible_agency, hr_name)] for HR numbers in text."""
    text = re.sub(r"\s+", " ", text)
    company_in_title = bool(company.pattern.search(title or ""))
    found = []
    for match in phonenumbers.PhoneNumberMatcher(text, "IN"):
        line_type = ALLOWED_LINE_TYPES.get(phonenumbers.number_type(match.number))
        if line_type is None:  # toll-free, premium-rate, etc. are not a person's line
            continue
        nearby = text[max(0, match.start - CONTEXT_CHARS): match.end + CONTEXT_CHARS]
        if not HR_WORDS.search(nearby):
            continue
        if not (company.pattern.search(nearby) or company_in_title):
            continue
        found.append((
            phonenumbers.format_number(match.number, PhoneNumberFormat.E164),
            # National format ("098765 43210") stays text in Excel and Google Sheets.
            phonenumbers.format_number(match.number, PhoneNumberFormat.NATIONAL),
            line_type,
            bool(AGENCY_WORDS.search(nearby)),
            find_name(text[max(0, match.start - 80): match.start], text[match.end: match.end + 40], company),
        ))
    return found


def parse_date_text(raw, today=None):
    """Read '3 weeks ago', 'Mar 3, 2025' or an ISO date; None if unclear or in the future."""
    today = today or date.today()
    if not isinstance(raw, str) or not raw.strip():
        return None
    raw = raw.strip()
    parsed = None
    relative = re.match(r"(\d+)\s+(hour|day|week|month|year)s?\s+ago", raw, re.I)
    if relative:
        days = {"hour": 0, "day": 1, "week": 7, "month": 30, "year": 365}[relative[2].lower()]
        try:
            parsed = today - timedelta(days=int(relative[1]) * days)
        except OverflowError:
            return None
    else:
        for fmt in ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y"):
            try:
                parsed = datetime.strptime(raw, fmt).date()
                break
            except ValueError:
                pass
        if parsed is None:
            try:
                parsed = date.fromisoformat(raw[:10])
            except ValueError:
                return None
    return parsed if parsed <= today else None


def is_old(iso_date, today=None):
    today = today or date.today()
    try:
        return (today - date.fromisoformat(iso_date)).days > OLD_AFTER_DAYS
    except ValueError:
        return False


def prefer_recent(results, today=None):
    """Order results: recent pages, then undated, then old; otherwise keep search order."""
    def rank(result):
        parsed = parse_date_text(result.get("date"), today)
        if parsed is None:
            return 1
        return 2 if is_old(parsed.isoformat(), today) else 0
    return sorted(results, key=rank)


def html_published_date(soup, today=None):
    for attrs in (
        {"property": "article:published_time"},
        {"name": "article:published_time"},
        {"itemprop": "datePublished"},
        {"name": "date"},
        {"name": "publish-date"},
    ):
        tag = soup.find("meta", attrs=attrs)
        parsed = parse_date_text(tag.get("content") if tag else "", today)
        if parsed:
            return parsed.isoformat()
    match = re.search(r'"datePublished"\s*:\s*"([^"]+)"', str(soup))
    parsed = parse_date_text(match[1], today) if match else None
    return parsed.isoformat() if parsed else None


def read_pdf(body, today=None):
    """Return (title, text, date) from PDF bytes, or None if it can't be read."""
    try:
        pdf = PdfReader(io.BytesIO(body))
        text = " ".join((page.extract_text() or "") for page in pdf.pages[:MAX_PDF_PAGES])
        info = pdf.metadata or {}
        title = str(info.get("/Title") or "")
        created = re.match(r"D:(\d{4})(\d{2})(\d{2})", str(info.get("/CreationDate") or ""))
    except Exception:  # broken or encrypted PDFs raise many different errors
        return None
    parsed = parse_date_text("-".join(created.groups()), today) if created else None
    return title, text, parsed.isoformat() if parsed else None


class Store:
    """Everything found so far and which companies are done, kept in one JSON file."""

    def __init__(self, path):
        self.path = Path(path)
        self.contacts = {}
        self.searched = {}  # lowercased company name -> date searched
        self.warning = ""  # a problem with the saved file, for showing to the user
        self.locked = False  # the file exists but couldn't be read, so don't overwrite it

    def load(self):
        try:
            if not self.path.exists():
                return self
            data = json.loads(self.path.read_text(encoding="utf-8"))
            contacts = {}
            for fields in data["contacts"]:
                contact = Contact(**fields)
                if not (contact.company and contact.phone_key and contact.phone and contact.urls):
                    continue  # one bad entry shouldn't cost the rest of the list
                if not parse_date_text(contact.page_date):
                    contact.page_date = ""
                contacts[(contact.company, contact.phone_key)] = contact
            if not isinstance(data["searched"], dict):
                raise TypeError("searched should be a dictionary")
            self.contacts, self.searched = contacts, data["searched"]
        except OSError as error:
            self.locked = True
            self.warning = f"Couldn't open your saved results ({error}). Close anything using {self.path.name}, then restart the finder."
        except (ValueError, KeyError, TypeError, AttributeError):
            # Keep the damaged file for rescue rather than overwriting it later.
            backup = self.path.with_name(self.path.name + ".damaged")
            try:
                self.path.replace(backup)
                self.warning = f"Your saved results were damaged, so this is a fresh list. The old file is kept as {backup.name}."
            except OSError:
                self.locked = True
                self.warning = f"Your saved results in {self.path.name} are damaged and couldn't be moved aside."
        return self

    def save(self):
        if self.locked:
            raise OSError(f"{self.path.name} couldn't be opened earlier, so it wasn't overwritten")
        data = {"contacts": [asdict(c) for c in self.contacts.values()], "searched": self.searched}
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(data, indent=1), encoding="utf-8")
        for attempt in range(3):
            try:
                temporary.replace(self.path)  # a crash mid-write can't corrupt the real file
                return
            except PermissionError:  # antivirus or cloud sync can hold the file for a moment
                if attempt == 2:
                    raise
                time.sleep(0.2)

    def was_searched(self, name):
        return name.lower() in self.searched

    def searched_on(self, name):
        return self.searched.get(name.lower())

    def mark_searched(self, name, today=None):
        self.searched[name.lower()] = (today or date.today()).isoformat()

    def remove_numbers(self, numbers):
        """Permanently drop numbers now on the do-not-call list."""
        for key in [k for k in self.contacts if k[1] in numbers]:
            del self.contacts[key]


def search(session, api_key, query, retry_delay=RETRY_DELAY):
    for attempt in range(SEARCH_ATTEMPTS):
        last_try = attempt == SEARCH_ATTEMPTS - 1
        try:
            response = session.post(
                SEARCH_URL,
                headers={"X-API-KEY": api_key},
                json={"q": query, "gl": "in", "num": 10},
                timeout=TIMEOUT,
            )
        except (requests.ConnectionError, requests.Timeout):
            if last_try:
                raise
        else:
            if response.status_code < 500 or last_try:
                break
        time.sleep(retry_delay * 2 ** attempt)

    if response.status_code in (401, 402, 403, 429):
        raise SearchStopped(f"search API returned {response.status_code}: {response.text[:200]}")
    if response.status_code == 400 and "credit" in response.text.lower():
        raise SearchStopped(f"search API says it's out of credits: {response.text[:200]}")
    response.raise_for_status()
    data = response.json()
    results = data.get("organic") if isinstance(data, dict) else None
    return [r for r in results if isinstance(r, dict)] if isinstance(results, list) else []


def describe_search_error(error):
    """Short wording for the activity log instead of a long technical message."""
    if isinstance(error, requests.Timeout):
        return "the search timed out"
    if isinstance(error, requests.ConnectionError):
        return "couldn't connect to the search service"
    if isinstance(error, requests.HTTPError) and error.response is not None:
        return f"the search service returned an error ({error.response.status_code})"
    return "the search service sent back something unexpected"


class PageReader:
    """Fetches public HTML pages, following each site's robots.txt."""

    def __init__(self, session, delay=1.0):
        self.session = session
        self.delay = delay
        self.robots = {}

    def allowed(self, url):
        parts = urlparse(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.robots:
            parser = robotparser.RobotFileParser()
            try:
                response = self.session.get(f"{origin}/robots.txt", timeout=TIMEOUT)
                if response.status_code in (401, 403):
                    parser.disallow_all = True
                elif response.status_code >= 400:
                    parser.allow_all = True
                else:
                    parser.parse(response.text.splitlines())
            except requests.RequestException:
                parser.disallow_all = True
            self.robots[origin] = parser
        return self.robots[origin].can_fetch(USER_AGENT, url)

    def read(self, url):
        """Return (title, text, date), or None if the page is off limits or unreadable."""
        if not self.allowed(url):
            return None
        time.sleep(self.delay)
        truncated = False
        try:
            with self.session.get(url, timeout=TIMEOUT, stream=True) as response:
                kind = response.headers.get("content-type", "").lower()
                is_pdf = "pdf" in kind or urlparse(url).path.lower().endswith(".pdf")
                if not response.ok or not (is_pdf or "html" in kind):
                    return None
                body = b""
                for chunk in response.iter_content(65536):
                    body += chunk
                    if len(body) > MAX_PAGE_BYTES:
                        truncated = True
                        break
        except requests.RequestException:
            return None
        if is_pdf:
            return None if truncated else read_pdf(body)  # a cut-off PDF can't be parsed
        soup = BeautifulSoup(body, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        published = html_published_date(soup)
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        return title, soup.get_text(" "), published


def build_queries(company, city):
    return [phrase.format(company=company.name, city=city) for phrase in SEARCH_PHRASES]


def collect(companies, city, search_fn, read_fn, do_not_call=frozenset(), log=print,
            on_event=None, should_stop=None, contacts=None):
    """Run every company through search and extraction; return contacts keyed by (company, phone).

    on_event(kind, **details) reports progress as it happens, and should_stop() is
    checked before each search and page, so a run can be watched and cancelled.
    """
    contacts = {} if contacts is None else contacts
    emit = on_event or (lambda kind, **details: None)
    failed_in_a_row = 0

    def check_stop():
        if should_stop and should_stop():
            raise SearchStopped("stopped by you")

    try:
        for index, company in enumerate(companies, 1):
            log(f"[{index}/{len(companies)}] {company.name}")
            emit("company", index=index, name=company.name)
            seen_urls = set()
            complete = True
            for query in build_queries(company, city):
                check_stop()
                try:
                    results = search_fn(query)
                except (requests.RequestException, ValueError) as error:
                    reason = describe_search_error(error)
                    log(f"  search failed: {reason}")
                    emit("search_failed", query=query, error=reason)
                    complete = False
                    failed_in_a_row += 1
                    if failed_in_a_row >= MAX_FAILED_SEARCHES_IN_A_ROW:
                        raise SearchStopped("searches keep failing, so check your internet connection")
                    continue
                failed_in_a_row = 0
                emit("search", query=query, results=len(results))
                for result in prefer_recent(results)[:PAGES_PER_QUERY]:
                    check_stop()
                    url = result.get("link", "")
                    if not isinstance(url, str) or not url.startswith("http") or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    # One strange page must never end the run, whatever goes wrong with it.
                    try:
                        if is_blocked(url):
                            emit("page", url=url, status="skipped", found=0)
                            continue
                        page = read_fn(url)
                        numbers = extract_numbers(page[1], page[0], company) if page else None
                    except Exception:
                        page = None
                    if page is None:
                        emit("page", url=url, status="unreadable", found=0)
                        continue
                    page_date = page[2] if len(page) > 2 and page[2] else None
                    if not page_date:
                        parsed = parse_date_text(result.get("date"))
                        page_date = parsed.isoformat() if parsed else ""
                    found = set()
                    for key, display, line_type, agency, name in numbers:
                        if key in do_not_call:
                            continue
                        found.add(key)
                        contact = contacts.setdefault(
                            (company.name, key),
                            Contact(company.name, key, display, line_type),
                        )
                        if url not in contact.urls:
                            contact.urls.append(url)
                        contact.possible_agency |= agency
                        if name and name not in contact.names:
                            contact.names.append(name)
                        contact.page_date = max(contact.page_date, page_date)
                    emit("page", url=url, status="read", found=len(found), date=page_date)
            # Only a company whose searches all ran counts as done; see Store.mark_searched.
            emit("company_done", name=company.name, complete=complete)
    except SearchStopped as error:
        log(f"Stopped early, saving what was found so far: {error}")
        emit("stopped", reason=str(error))
    return contacts


def to_rows(contacts):
    """Build CSV rows, best-supported numbers first, with reasons to double-check."""
    companies_per_number = {}
    for contact in contacts.values():
        companies_per_number.setdefault(contact.phone_key, set()).add(contact.company)
    rows = []
    for contact in contacts.values():
        reasons = []
        if len(companies_per_number[contact.phone_key]) > 1:
            reasons.append("same number listed for several companies")
        if contact.possible_agency:
            reasons.append("may be a recruitment agency")
        if len(contact.names) > 1:
            reasons.append("different names found: " + ", ".join(contact.names))
        if len(contact.urls) == 1:
            reasons.append("only one source")
        if contact.page_date and is_old(contact.page_date):
            reasons.append("page is over 18 months old")
        rows.append({
            "company": contact.company,
            "hr_name": contact.names[0] if contact.names else "",
            "phone": contact.phone,
            "line_type": contact.line_type,
            "page_date": contact.page_date,
            "sources_found": len(contact.urls),
            "check_first": "; ".join(reasons),
            "source_url": contact.urls[0],
        })
    # Within each company: newest page first, then most sources.
    rows.sort(key=lambda r: r["sources_found"], reverse=True)
    rows.sort(key=lambda r: r["page_date"], reverse=True)
    rows.sort(key=lambda r: r["company"].lower())
    return rows


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("companies", help="text file with one company per line")
    parser.add_argument("--city", default="Chennai")
    parser.add_argument("--out", default="hr_contacts.csv")
    parser.add_argument("--do-not-call", help="text file of numbers to leave out, one per line")
    parser.add_argument("--store", default="finder_data.json", help="where results are kept between runs")
    parser.add_argument("--search-again", action="store_true", help="also search companies already done")
    args = parser.parse_args()

    api_key = os.environ.get("SERPER_API_KEY")
    if not api_key:
        sys.exit("Set the SERPER_API_KEY environment variable first.")

    try:
        do_not_call = set()
        if args.do_not_call:
            do_not_call = {n for n in map(e164, load_lines(args.do_not_call)) if n}
        companies = parse_companies(load_lines(args.companies))
    except OSError as error:
        sys.exit(f"Couldn't read your file: {error}")
    if not companies:
        sys.exit(f"No companies found in {args.companies}.")

    store = Store(args.store).load()
    if store.warning:
        print(store.warning)
    if store.locked:
        sys.exit(1)
    store.remove_numbers(do_not_call)
    to_search = [c for c in companies if args.search_again or not store.was_searched(c.name)]
    if len(to_search) < len(companies):
        print(f"Skipping {len(companies) - len(to_search)} already searched (use --search-again to redo)")

    def save_progress():
        try:
            store.save()
        except OSError as error:
            print(f"Couldn't save progress: {error}")

    def on_event(kind, **details):
        if kind == "company_done":
            if details["complete"]:
                store.mark_searched(details["name"])
            save_progress()
        elif kind in ("search_failed", "stopped"):
            print("  " + str(details.get("error") or details.get("reason")))

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    reader = PageReader(session)
    try:
        collect(
            to_search,
            args.city,
            lambda query: search(session, api_key, query),
            reader.read,
            do_not_call,
            on_event=on_event,
            contacts=store.contacts,
        )
    except KeyboardInterrupt:
        print("\nStopped. Saving what was found so far.")
    save_progress()

    rows = to_rows(store.contacts)
    try:
        write_csv(args.out, rows)
    except OSError:
        sys.exit(f"Couldn't write {args.out}. Close it if it's open in Excel, then run this again.")
    found = {row["company"] for row in rows}
    missing = [c.name for c in companies if c.name not in found]
    print(f"\n{len(rows)} numbers for {len(found)} companies -> {args.out}")
    if missing:
        print("No number found: " + ", ".join(missing))


if __name__ == "__main__":
    main()
