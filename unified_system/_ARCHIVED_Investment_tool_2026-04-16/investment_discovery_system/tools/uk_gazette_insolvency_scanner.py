"""
UK Gazette Insolvency Scanner
==============================
Monitors The Gazette (thegazette.co.uk) for insolvency-related notices
affecting companies that may be linked to listed equities.

Data Source: https://www.thegazette.co.uk/
API: The Gazette has an Atom feed and search API
Cost: Free

Key Notices to Monitor:
- Winding-up petitions (filed weeks before formal insolvency)
- Statutory demands (precursor to winding-up)
- Administration appointments
- Liquidator appointments
- Company voluntary arrangements (CVAs)
- Striking-off notices (Section 1000 Companies Act)

Investment Signal Logic:
- Winding-up petition against a subsidiary of a listed company → distress signal
- Statutory demand against a listed company → severe distress signal
- CVA proposal → restructuring opportunity
- Striking-off of a subsidiary → corporate simplification (could be positive or negative)
"""

import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
import re

# CONFIGURATION
GAZETTE_SEARCH_URL = "https://www.thegazette.co.uk/all-notices/content"
GAZETTE_FEED_URL = "https://www.thegazette.co.uk/all-notices/notice"

# Insolvency notice categories in The Gazette
INSOLVENCY_CATEGORIES = {
    "winding_up_petitions": "2903",      # Corporate insolvency — winding up
    "administration": "2901",             # Corporate insolvency — administration
    "voluntary_liquidation": "2902",       # Corporate insolvency — voluntary liquidation
    "company_voluntary_arrangement": "2900",  # CVA
    "striking_off": "2400",               # Striking off
}

# Known subsidiaries of listed companies (expand this mapping)
# Format: "subsidiary_name_fragment": {"parent": "Listed Parent", "ticker": "TICK.L"}
LISTED_COMPANY_SUBSIDIARIES = {
    # This would be populated from Companies House group structure data
    # Examples:
    "vodafone": {"parent": "Vodafone Group", "ticker": "VOD.L"},
    "rolls-royce": {"parent": "Rolls-Royce Holdings", "ticker": "RR.L"},
    "bp": {"parent": "BP plc", "ticker": "BP.L"},
    "shell": {"parent": "Shell plc", "ticker": "SHEL.L"},
    "barclays": {"parent": "Barclays PLC", "ticker": "BARC.L"},
    "tesco": {"parent": "Tesco PLC", "ticker": "TSCO.L"},
    "marks spencer": {"parent": "Marks & Spencer", "ticker": "MKS.L"},
    "bt group": {"parent": "BT Group", "ticker": "BT-A.L"},
    # Add more as needed...
}


def search_gazette_notices(query=None, category_code=None, date_from=None, date_to=None):
    """
    Search The Gazette for notices matching criteria.

    Args:
        query: Text search query (company name, etc.)
        category_code: Gazette notice category code
        date_from: Start date (YYYY-MM-DD)
        date_to: End date (YYYY-MM-DD)

    Returns:
        List of notice dicts
    """
    params = {
        "results-page-size": 50,
    }

    if query:
        params["text"] = query
    if category_code:
        params["categorycode"] = category_code
    if date_from:
        params["start-publish-date"] = date_from
    if date_to:
        params["end-publish-date"] = date_to

    headers = {"Accept": "application/atom+xml"}

    try:
        resp = requests.get(GAZETTE_SEARCH_URL, params=params, headers=headers)
        resp.raise_for_status()

        # Parse Atom XML feed
        root = ET.fromstring(resp.content)
        ns = {"atom": "http://www.w3.org/2005/Atom"}

        notices = []
        for entry in root.findall("atom:entry", ns):
            notice = {
                "title": entry.find("atom:title", ns).text if entry.find("atom:title", ns) is not None else "",
                "published": entry.find("atom:published", ns).text if entry.find("atom:published", ns) is not None else "",
                "link": entry.find("atom:link", ns).get("href", "") if entry.find("atom:link", ns) is not None else "",
                "summary": entry.find("atom:summary", ns).text if entry.find("atom:summary", ns) is not None else "",
            }
            notices.append(notice)

        return notices

    except (requests.exceptions.RequestException, ET.ParseError) as e:
        print(f"Error searching Gazette: {e}")
        return []


def scan_insolvency_notices(days_back=7):
    """Scan for all insolvency-related notices in recent days."""
    date_from = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    date_to = datetime.now().strftime("%Y-%m-%d")

    all_notices = []

    for category_name, category_code in INSOLVENCY_CATEGORIES.items():
        notices = search_gazette_notices(
            category_code=category_code,
            date_from=date_from,
            date_to=date_to,
        )

        for notice in notices:
            notice["category"] = category_name

        all_notices.extend(notices)

    return all_notices


def match_to_listed_companies(notices):
    """
    Cross-reference insolvency notices against known listed company subsidiaries.
    Returns notices that may affect listed equities.
    """
    matched = []

    for notice in notices:
        title_lower = notice.get("title", "").lower()
        summary_lower = notice.get("summary", "").lower()
        text = f"{title_lower} {summary_lower}"

        for fragment, parent_info in LISTED_COMPANY_SUBSIDIARIES.items():
            if fragment in text:
                notice["matched_parent"] = parent_info["parent"]
                notice["matched_ticker"] = parent_info["ticker"]
                notice["match_fragment"] = fragment
                matched.append(notice)
                break

    return matched


def monitor_specific_company(company_name, days_back=30):
    """Monitor Gazette for any notices mentioning a specific company."""
    date_from = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")

    notices = search_gazette_notices(
        query=company_name,
        date_from=date_from,
    )

    return notices


def daily_scan():
    """
    Run daily insolvency scan and cross-reference with listed companies.
    Returns actionable alerts.
    """
    print("Scanning Gazette for insolvency notices...")

    # Get recent insolvency notices
    notices = scan_insolvency_notices(days_back=3)
    print(f"Found {len(notices)} insolvency-related notices")

    # Cross-reference with listed companies
    matched = match_to_listed_companies(notices)

    if matched:
        print(f"\n*** ALERTS: {len(matched)} notices matched to listed companies ***")
        for m in matched:
            print(f"  [{m['category']}] {m['title']}")
            print(f"    → Parent: {m['matched_parent']} ({m['matched_ticker']})")
            print(f"    → Published: {m['published']}")
            print(f"    → Link: {m['link']}")
            print()
    else:
        print("No matches to listed companies found.")

    return matched


# --- MAIN EXECUTION ---
if __name__ == "__main__":
    print("=" * 60)
    print("UK Gazette Insolvency Scanner")
    print("=" * 60)

    # Run daily scan
    alerts = daily_scan()

    # Also scan for winding-up petitions specifically (highest urgency)
    print("\n--- WINDING-UP PETITIONS (Last 7 Days) ---")
    wup = search_gazette_notices(
        category_code=INSOLVENCY_CATEGORIES["winding_up_petitions"],
        date_from=(datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d"),
    )
    for notice in wup[:20]:
        print(f"  {notice['published'][:10]} | {notice['title'][:80]}")

    print(f"\nTotal winding-up petitions found: {len(wup)}")
