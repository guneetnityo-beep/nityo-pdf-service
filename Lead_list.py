"""
LinkedIn Role Scraper -> Waterfall Enrichment -> Pipedrive Push
=================================================================
Flow:
  1. Fetch Pipedrive orgs matching target SM name.
  2. Scrape LinkedIn (Playwright) for target roles at those orgs.
  3. Dedup against Pipedrive (skip existing persons).
  4. Waterfall enrich survivors (Apollo -> Hunter -> PDL -> ...).
  5. Push new persons to Pipedrive, linked to org.

Fill CONFIG before running. Run find_sm_field_key() once to get
the SM custom field key from Pipedrive.
"""

import re
import time
import requests
import gspread
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright

# ============================================================
# CONFIG
# ============================================================
CONFIG = {
    "PIPEDRIVE_API_KEY": "fd718fa41bf89000d4ef9cefd5962251f31caf1a",
    "PIPEDRIVE_DOMAIN": "ptnityoinfotech.pipedrive.com",  # full domain
    "SM_FIELD_KEY": "owner_id",
    "SM_TARGET_NAME": "Guneet",  # exact owner name as shown in Pipedrive
    "ROLE_KEYWORDS": [
        "HR Manager",
        "Talent Acquisition",
        "Recruitment",
        "People Operations",
        "IT Head",
        "CTO"
    ],
    "LINKEDIN_SESSION_COOKIE": "AQEDASvT_7oBdTLkAAABnd3dIB8AAAGfOm4dX1YAJp0zUe5JNvaPKmKCfFj_225Sg0BREVO7KwyC3yh6ktShHWx7Eg3buYf9aK8GglhPEqRS9AHUJ8s0S_FIkfr6v2EYdjd3ZQ7SFn0bDqJT3s6TONAd",
    "MAX_PROFILES_PER_COMPANY": 20,
    "APOLLO_API_KEY": "2HP9CULCk5V5j5miq2VF5w",
    "HUNTER_API_KEY": "46e7d46e4df9cb578adf0b9ea7c47e07bf2ece97",
    "PDL_API_KEY": "6eb8eeb6f535bc3ed940c1163e1ca0b49b7ebe529ed20827dd39966db914ba91",
    "SNOV_CLIENT_ID": "a253518cbe602f87dec162dd722bf1a2",
    "SNOV_CLIENT_SECRET": "b21ceb9cdf7ab70e6a63bcc471a77033",
    "GETPROSPECT_API_KEY": "e29085d4-a2e0-4d00-9ef7-1d062aed1347",
    "ENRICH_SO_API_KEY": "sk_live_gDLCQfCSXnPYMjDCwVcPsswFaveLfDAWzseCevdyYEZvNHcecevyGUhkXculhVNI",
    "GOOGLE_SERVICE_ACCOUNT_JSON": "service_account.json",
    "SHEET_ID": "1YBiaa8VNUqYVRR3njsMkfHzADgUXAwism0_bjvsC_4Q",
    "SHEET_TAB": "Scraped Leads",
    "DRY_RUN": True,  # True = skip pushing to Pipedrive, just print what would be pushed
    "EXTRA_COMPANIES": [],  # company names to prospect that aren't in Pipedrive yet, e.g. ["Acme Corp"]
    "MAX_ORGS": None,  # cap orgs scraped per run for testing; set to None to process all
    "TARGET_INDUSTRIES": [],  # e.g. ["Financial Services"] -- discover new companies via LinkedIn company search
    "TARGET_COUNTRIES": [],  # e.g. ["Indonesia"] -- paired with TARGET_INDUSTRIES for discovery
    "MAX_DISCOVERED_COMPANIES": 20,  # cap on newly discovered companies per run
    "DISCOVERY_SHEET_TAB": "New Companies",  # cumulative log for TARGET_INDUSTRIES/TARGET_COUNTRIES finds, kept separate from SHEET_TAB
}

CUSTOMER_LABEL_ID = 10  # Pipedrive org "Label" field option id for "Customer"
PD_BASE = f"https://{CONFIG['PIPEDRIVE_DOMAIN']}/api/v1"
PD_WEB_BASE = f"https://{CONFIG['PIPEDRIVE_DOMAIN']}"
SHEET_HEADERS = [
    "Name", "Title", "Company", "LinkedIn URL", "Org Link",
    "Pipedrive Status", "Email", "Email Source", "Pushed to Pipedrive",
]


def pipedrive_org_url(org_id):
    return f"{PD_WEB_BASE}/organization/{org_id}" if org_id else ""


def pipedrive_person_url(person_id):
    return f"{PD_WEB_BASE}/person/{person_id}"


# ============================================================
# GOOGLE SHEETS
# ============================================================
def get_sheet(tab_name):
    creds = Credentials.from_service_account_file(
        CONFIG["GOOGLE_SERVICE_ACCOUNT_JSON"],
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    client = gspread.authorize(creds)
    sh = client.open_by_key(CONFIG["SHEET_ID"])
    try:
        ws = sh.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(tab_name, rows=1000, cols=len(SHEET_HEADERS))
        ws.append_row(SHEET_HEADERS)
    return ws


def get_known_linkedin_urls(ws):
    return set(ws.col_values(4)[1:])  # column D, skip header row


def write_scraped_to_sheet(ws, people):
    rows = [
        [
            p["name"], p["title"], p["company"], p["linkedin_url"],
            pipedrive_org_url(p.get("org_id")), "", "", "", "",
        ]
        for p in people
    ]
    if rows:
        ws.append_rows(rows)
    print(f"Wrote {len(rows)} rows to sheet")


def update_sheet_status(ws, person, status, email="", source="", pushed_link=""):
    cell = ws.find(person["linkedin_url"])
    if not cell:
        return
    row = cell.row
    ws.update(range_name=f"F{row}:I{row}", values=[[status, email, source, pushed_link]])


# ============================================================
# STEP 0 — Find SM custom field key (run once, manually)
# ============================================================
def find_sm_field_key():
    """Lists organization custom fields. Find the SM field, copy its 'key'."""
    resp = requests.get(
        f"{PD_BASE}/organizationFields",
        params={"api_token": CONFIG["PIPEDRIVE_API_KEY"]},
    )
    fields = resp.json().get("data", [])
    for f in fields:
        print(f"{f['name']!r:40} key={f['key']}")


# ============================================================
# STEP 1 — Pull orgs for target SM
# ============================================================
def get_target_orgs():
    orgs = []
    start = 0
    while True:
        resp = requests.get(
            f"{PD_BASE}/organizations",
            params={
                "api_token": CONFIG["PIPEDRIVE_API_KEY"],
                "start": start,
                "limit": 100,
            },
        )
        data = resp.json()
        batch = data.get("data") or []
        if not batch:
            break
        for org in batch:
            if org.get("label") == CUSTOMER_LABEL_ID:
                continue  # already a customer, not a prospect to scrape
            owner = org.get(CONFIG["SM_FIELD_KEY"])
            owner_name = owner.get("name") if isinstance(owner, dict) else owner
            if owner_name == CONFIG["SM_TARGET_NAME"]:
                org["source"] = "pipedrive"
                orgs.append(org)
        if not data.get("additional_data", {}).get("pagination", {}).get(
            "more_items_in_collection"
        ):
            break
        start += 100
    print(f"Found {len(orgs)} orgs for SM={CONFIG['SM_TARGET_NAME']} (customers excluded)")

    for name in CONFIG["EXTRA_COMPANIES"]:
        orgs.append({"id": None, "name": name, "source": "extra"})
    if CONFIG["EXTRA_COMPANIES"]:
        print(f"Added {len(CONFIG['EXTRA_COMPANIES'])} extra companies not in Pipedrive")

    return orgs


# ============================================================
# STEP 2 — Scrape LinkedIn per company
# ============================================================
RESULT_CARD_RE = re.compile(r"^(.*?)\s*•\s*(1st|2nd|3rd\+)$")


def scrape_company_people(page, company_name):
    results = []
    query = " OR ".join(f'"{kw}"' for kw in CONFIG["ROLE_KEYWORDS"])
    search_url = (
        "https://www.linkedin.com/search/results/people/"
        f"?keywords={query}&company={company_name}"
    )
    page.goto(search_url)
    page.wait_for_timeout(3000)

    # LinkedIn's result-card class names are auto-generated hashes and change
    # often, so we anchor on the one stable thing: links to /in/<profile>.
    cards = page.evaluate(
        """() => {
            const anchors = Array.from(document.querySelectorAll('a[href*="/in/"]'));
            const seen = new Set();
            const out = [];
            for (const a of anchors) {
                const href = a.href.split('?')[0];
                const text = (a.innerText || '').trim();
                if (!text || seen.has(href)) continue;
                seen.add(href);
                out.push({ href, text });
            }
            return out;
        }"""
    )

    company_lower = company_name.lower()
    for card in cards:
        segments = [s.strip() for s in card["text"].split("\n\n") if s.strip()]
        if not segments:
            continue
        match = RESULT_CARD_RE.match(segments[0])
        if not match:
            continue  # not a real result card (e.g. a "mutual connections" mention)
        if company_lower not in card["text"].lower():
            continue  # keyword search hit wasn't actually scoped to this company
        results.append(
            {
                "name": match.group(1).strip(),
                "title": segments[1] if len(segments) > 1 else "",
                "company": company_name,
                "linkedin_url": card["href"],
            }
        )
        if len(results) >= CONFIG["MAX_PROFILES_PER_COMPANY"]:
            break
    return results


# ============================================================
# STEP 1b — Discover new companies via LinkedIn company search
# ============================================================
def resolve_country_geo_id(page, country_name):
    """Drives the real 'Locations' filter UI once per country and reads the
    geo id LinkedIn assigns it back out of the resulting URL, so subsequent
    searches for that country can skip the UI dance and build the URL directly."""
    page.get_by_text("Locations", exact=True).first.click()
    page.wait_for_timeout(800)
    page.get_by_placeholder("Add a location").fill(country_name)
    page.wait_for_timeout(1200)
    suggestion = page.get_by_text(country_name, exact=True).first
    if not suggestion.is_visible():
        return None
    suggestion.click()
    page.wait_for_timeout(500)
    page.get_by_text("Show results", exact=True).first.click()
    page.wait_for_timeout(2500)
    match = re.search(r"companyHqGeo=%5B%22(\d+)%22%5D", page.url)
    return match.group(1) if match else None


def discover_companies(page, known_names_lower):
    """Searches LinkedIn's company search per (industry, country) pair to find
    prospect companies not already in Pipedrive or EXTRA_COMPANIES. Client-side
    scoping only -- LinkedIn's own geo filter isn't perfectly precise, so some
    results outside the target country can still slip through."""
    discovered = []
    seen_lower = set(known_names_lower)
    geo_cache = {}

    for country in CONFIG["TARGET_COUNTRIES"]:
        for industry in CONFIG["TARGET_INDUSTRIES"]:
            if len(discovered) >= CONFIG["MAX_DISCOVERED_COMPANIES"]:
                return discovered

            if country not in geo_cache:
                page.goto(f"https://www.linkedin.com/search/results/companies/?keywords={industry}")
                page.wait_for_timeout(2500)
                geo_cache[country] = resolve_country_geo_id(page, country)
                time.sleep(4)

            geo_id = geo_cache[country]
            url = f"https://www.linkedin.com/search/results/companies/?keywords={industry}"
            if geo_id:
                url += f"&companyHqGeo=%5B%22{geo_id}%22%5D"
            page.goto(url)
            page.wait_for_timeout(3000)

            cards = page.evaluate(
                """() => {
                    const anchors = Array.from(document.querySelectorAll('a[href*="/company/"]'));
                    const seen = new Set();
                    const out = [];
                    for (const a of anchors) {
                        const href = a.href.split('?')[0];
                        const text = (a.innerText || '').trim();
                        if (!text || seen.has(href)) continue;
                        seen.add(href);
                        out.push({ href, text });
                    }
                    return out;
                }"""
            )
            for card in cards:
                segments = [s.strip() for s in card["text"].split("\n\n") if s.strip()]
                if len(segments) < 3:
                    continue  # too little info to be a real listing (e.g. "Page by X")
                name = segments[0].strip()
                if name.lower() in seen_lower:
                    continue
                seen_lower.add(name.lower())
                discovered.append({"id": None, "name": name, "source": "discovered"})
                if len(discovered) >= CONFIG["MAX_DISCOVERED_COMPANIES"]:
                    break
            print(f"  [discover] {industry} / {country}: +{len(discovered)} total so far")
            time.sleep(4)

    return discovered


def run_scraper(orgs, discover=False):
    all_people = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        context.add_cookies(
            [
                {
                    "name": "li_at",
                    "value": CONFIG["LINKEDIN_SESSION_COOKIE"],
                    "domain": ".linkedin.com",
                    "path": "/",
                }
            ]
        )
        page = context.new_page()

        if discover:
            known_names_lower = {org["name"].lower() for org in orgs}
            discovered = discover_companies(page, known_names_lower)
            print(f"Discovered {len(discovered)} new companies via LinkedIn company search")
            orgs = orgs + discovered

        for i, org in enumerate(orgs, start=1):
            print(f"[{i}/{len(orgs)}] Scraping {org['name']}...")
            people = scrape_company_people(page, org["name"])
            for person in people:
                person["org_id"] = org["id"]
                person["org_source"] = org.get("source", "pipedrive")
            all_people.extend(people)
            print(f"  -> found {len(people)} (running total: {len(all_people)})")
            time.sleep(4)  # throttle, avoid rate limit
        browser.close()
    print(f"Scraped {len(all_people)} profiles")
    return all_people


# ============================================================
# STEP 3 — Dedup against Pipedrive
# ============================================================
def is_existing_in_pipedrive(name, company):
    resp = requests.get(
        f"{PD_BASE}/itemSearch",
        params={
            "api_token": CONFIG["PIPEDRIVE_API_KEY"],
            "term": name,
            "item_types": "person",
            "fields": "name",
        },
    )
    items = resp.json().get("data", {}).get("items", [])
    for item in items:
        p = item.get("item", {})
        orgs = p.get("organization")
        org_name = orgs.get("name") if orgs else None
        if org_name and org_name.lower() == company.lower():
            return True
    return False


def dedup_people(people, ws):
    survivors = []
    for person in people:
        exists = is_existing_in_pipedrive(person["name"], person["company"])
        status = "Existing" if exists else "New"
        update_sheet_status(ws, person, status)
        if not exists:
            survivors.append(person)
    print(f"{len(survivors)}/{len(people)} are new (not in Pipedrive)")
    return survivors


# ============================================================
# STEP 4 — Waterfall enrichment
# ============================================================
def enrich_apollo(person):
    if not CONFIG["APOLLO_API_KEY"]:
        return None
    resp = requests.post(
        "https://api.apollo.io/v1/people/match",
        headers={"X-Api-Key": CONFIG["APOLLO_API_KEY"]},
        json={
            "name": person["name"],
            "organization_name": person["company"],
            "linkedin_url": person["linkedin_url"],
        },
    )
    data = resp.json().get("person") or {}
    return data.get("email")


def enrich_hunter(person):
    if not CONFIG["HUNTER_API_KEY"]:
        return None
    domain_guess = person["company"].lower().replace(" ", "") + ".com"
    resp = requests.get(
        "https://api.hunter.io/v2/email-finder",
        params={
            "domain": domain_guess,
            "full_name": person["name"],
            "api_key": CONFIG["HUNTER_API_KEY"],
        },
    )
    return resp.json().get("data", {}).get("email")


def enrich_pdl(person):
    if not CONFIG["PDL_API_KEY"]:
        return None
    resp = requests.get(
        "https://api.peopledatalabs.com/v5/person/enrich",
        params={
            "api_key": CONFIG["PDL_API_KEY"],
            "profile": person["linkedin_url"],
        },
    )
    data = resp.json().get("data", {})
    emails = data.get("emails")
    if not isinstance(emails, list) or not emails:
        return None  # PDL sometimes returns `true` (masked) instead of a list on lower-tier plans
    return emails[0]["address"]


def enrich_snov(person):
    if not (CONFIG["SNOV_CLIENT_ID"] and CONFIG["SNOV_CLIENT_SECRET"]):
        return None
    token_resp = requests.post(
        "https://api.snov.io/v1/oauth/access_token",
        data={
            "grant_type": "client_credentials",
            "client_id": CONFIG["SNOV_CLIENT_ID"],
            "client_secret": CONFIG["SNOV_CLIENT_SECRET"],
        },
    )
    token = token_resp.json().get("access_token")
    if not token:
        return None

    name_parts = person["name"].split()
    first_name = name_parts[0]
    last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""
    domain_guess = person["company"].lower().replace(" ", "") + ".com"

    resp = requests.post(
        "https://api.snov.io/v1/get-emails-from-names",
        data={
            "access_token": token,
            "firstName": first_name,
            "lastName": last_name,
            "domain": domain_guess,
        },
    )
    emails = resp.json().get("data", {}).get("emails") or []
    if not emails:
        return None
    return emails[0].get("email") or emails[0].get("address")


WATERFALL = [enrich_apollo, enrich_hunter, enrich_pdl, enrich_snov]


def enrich_people(people, ws):
    for person in people:
        for source in WATERFALL:
            email = source(person)
            if email:
                person["email"] = email
                person["email_source"] = source.__name__
                break
        else:
            person["email"] = None
            person["email_source"] = None
        update_sheet_status(
            ws, person, "New",
            email=person["email"] or "", source=person["email_source"] or "",
        )
    found = sum(1 for p in people if p["email"])
    print(f"Enriched {found}/{len(people)} emails")
    return people


# ============================================================
# STEP 5 — Push to Pipedrive
# ============================================================
def push_to_pipedrive(person):
    payload = {
        "api_token": CONFIG["PIPEDRIVE_API_KEY"],
        "name": person["name"],
    }
    if person.get("org_id") is not None:
        payload["org_id"] = person["org_id"]
    if person.get("email"):
        payload["email"] = [{"value": person["email"], "primary": True}]

    resp = requests.post(f"{PD_BASE}/persons", params=payload)
    ok = resp.status_code == 201
    person_link = pipedrive_person_url(resp.json()["data"]["id"]) if ok else ""
    print(f"{'OK  ' if ok else 'FAIL'} push: {person['name']}")
    return person_link


def push_all(people, ws):
    for person in people:
        person_link = push_to_pipedrive(person)
        update_sheet_status(
            ws, person, "New",
            email=person.get("email") or "",
            source=person.get("email_source") or "",
            pushed_link=person_link,
        )
        person["pushed"] = person_link


def print_dry_run_report(people, label=""):
    prefix = f"[{label}] " if label else ""
    print(f"\n{prefix}DRY RUN — would push {len(people)} person(s) to Pipedrive:")
    for p in people:
        email = p.get("email") or "(no email found)"
        source = f" [{p['email_source']}]" if p.get("email_source") else ""
        org_link = pipedrive_org_url(p.get("org_id")) or "(no Pipedrive org)"
        print(f"  - {p['name']} | {p['title']} | {p['company']} | {email}{source} | {org_link}")
    print(f"{prefix}Set CONFIG['DRY_RUN'] = False to actually push these to Pipedrive.")


def scrape_and_dedup(people, ws, label=""):
    """Writes freshly scraped people to a sheet and marks New/Existing.
    Does NOT enrich or push -- that's a separate action (see 'Enrich only')."""
    known_urls = get_known_linkedin_urls(ws)
    before = len(people)
    people = [p for p in people if p["linkedin_url"] not in known_urls]
    skipped = before - len(people)
    prefix = f"[{label}] " if label else ""
    if skipped:
        print(f"{prefix}Skipping {skipped} profile(s) already logged in the sheet from a previous run")
    if not people:
        print(f"{prefix}Nothing new scraped")
        return

    write_scraped_to_sheet(ws, people)
    dedup_people(people, ws)
    print(f"{prefix}Scrape + dedup complete. Use 'Enrich only' next to find emails.")


# ============================================================
# BACKFILL — finish dedup/enrichment for rows already in a sheet
# (no LinkedIn scraping; use after an interrupted run)
# ============================================================
def parse_org_id_from_link(link):
    match = re.search(r"/organization/(\d+)", link or "")
    return int(match.group(1)) if match else None


def read_sheet_rows(ws):
    rows = ws.get_all_values()[1:]  # skip header
    people = []
    for row in rows:
        row = row + [""] * (9 - len(row))  # pad in case of short rows
        name, title, company, linkedin_url, org_link, status, email, source, pushed = row[:9]
        if not linkedin_url:
            continue
        people.append(
            {
                "name": name, "title": title, "company": company,
                "linkedin_url": linkedin_url, "org_id": parse_org_id_from_link(org_link),
                "status": status, "email": email, "email_source": source, "pushed": pushed,
            }
        )
    return people


def backfill_existing(ws):
    people = read_sheet_rows(ws)
    needs_dedup = [p for p in people if not p["status"]]
    needs_enrich_only = [p for p in people if p["status"] == "New" and not p["email"]]
    print(f"Backfill: {len(needs_dedup)} row(s) need dedup, {len(needs_enrich_only)} row(s) need enrichment only")

    new_from_dedup = dedup_people(needs_dedup, ws) if needs_dedup else []
    to_enrich = new_from_dedup + needs_enrich_only
    if not to_enrich:
        print("Nothing to enrich.")
        return

    enriched = enrich_people(to_enrich, ws)

    if CONFIG["DRY_RUN"]:
        print_dry_run_report(enriched)
    else:
        to_push = [p for p in enriched if not p.get("pushed")]
        push_all(to_push, ws)


# ============================================================
# ACTIONS
# ============================================================
def action_scrape_existing():
    """Scrape LinkedIn for orgs already in Pipedrive (+ EXTRA_COMPANIES).
    Writes to the main sheet and marks New/Existing. No enrichment, no push."""
    orgs = get_target_orgs()
    if not orgs:
        print("No orgs found for this SM. Check SM_TARGET_NAME / SM_FIELD_KEY.")
        return

    if CONFIG["MAX_ORGS"] is not None:
        orgs = orgs[: CONFIG["MAX_ORGS"]]
        print(f"MAX_ORGS set — limiting this run to {len(orgs)} org(s)")

    people = run_scraper(orgs, discover=False)
    scrape_and_dedup(people, get_sheet(CONFIG["SHEET_TAB"]), label="main")


def action_scrape_new():
    """Discover new companies via LinkedIn company search (TARGET_INDUSTRIES x
    TARGET_COUNTRIES) and scrape them. Writes to the discovery sheet only."""
    if not (CONFIG["TARGET_INDUSTRIES"] and CONFIG["TARGET_COUNTRIES"]):
        print("Set CONFIG['TARGET_INDUSTRIES'] and CONFIG['TARGET_COUNTRIES'] first.")
        return

    people = run_scraper([], discover=True)
    scrape_and_dedup(people, get_sheet(CONFIG["DISCOVERY_SHEET_TAB"]), label="discovery")


def action_enrich_only():
    """Runs the email waterfall on rows already marked 'New' with no email yet,
    in both sheets. No scraping, no dedup re-check."""
    for tab in (CONFIG["SHEET_TAB"], CONFIG["DISCOVERY_SHEET_TAB"]):
        ws = get_sheet(tab)
        people = read_sheet_rows(ws)
        needs_enrich = [p for p in people if p["status"] == "New" and not p["email"]]
        print(f"[{tab}] {len(needs_enrich)} row(s) need enrichment")
        if not needs_enrich:
            continue

        enriched = enrich_people(needs_enrich, ws)
        if CONFIG["DRY_RUN"]:
            print_dry_run_report(enriched, tab)
        else:
            to_push = [p for p in enriched if not p.get("pushed")]
            push_all(to_push, ws)


def action_backfill():
    """Full recovery pass: finishes dedup AND enrichment for any incomplete
    rows in both sheets. Use this after an interrupted run."""
    for tab in (CONFIG["SHEET_TAB"], CONFIG["DISCOVERY_SHEET_TAB"]):
        print(f"=== Backfilling '{tab}' ===")
        backfill_existing(get_sheet(tab))


MENU_ACTIONS = {
    "1": ("Scrape existing Pipedrive companies (scrape + dedup only)", action_scrape_existing),
    "2": ("Scrape new companies (LinkedIn discovery, scrape + dedup only)", action_scrape_new),
    "3": ("Enrich only (find emails for rows already marked New)", action_enrich_only),
    "4": ("Full backfill (dedup + enrich for any incomplete rows)", action_backfill),
}


def main():
    while True:
        print("\nLEAD SCRAPER — choose an action:")
        for key, (description, _) in MENU_ACTIONS.items():
            print(f"  {key}) {description}")
        print("  0) Exit")

        choice = input("Choose an option: ").strip()
        if choice == "0":
            break
        if choice not in MENU_ACTIONS:
            print("Invalid choice, try again.")
            continue

        _, action = MENU_ACTIONS[choice]
        action()


if __name__ == "__main__":
    # Run this first, once, to get SM_FIELD_KEY:
    # find_sm_field_key()
    main()