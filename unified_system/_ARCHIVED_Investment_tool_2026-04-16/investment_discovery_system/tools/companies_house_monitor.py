"""
Companies House Monitor
========================
Monitors UK Companies House for signals relevant to listed equity investment.

Data Source: Companies House API (free, requires API key)
API Docs: https://developer.company-information.service.gov.uk/
Rate Limit: 600 requests per 5 minutes
Streaming API: https://stream.companieshouse.gov.uk/ (real-time filings)

Key Signals:
- Director appointments/resignations for listed companies
- PSC (Persons with Significant Control) changes — ownership shifts
- New company formations by known directors
- Charges register — new security interests (financing activity)
- Winding-up petitions and insolvency filings
"""

import requests
import json
import time
from datetime import datetime, timedelta
from base64 import b64encode

# CONFIGURATION
API_KEY = "YOUR_API_KEY_HERE"  # Get free key at https://developer.company-information.service.gov.uk/
BASE_URL = "https://api.company-information.service.gov.uk"

# Encode API key for Basic Auth (Companies House uses API key as username, no password)
AUTH_HEADER = b64encode(f"{API_KEY}:".encode()).decode()


def _headers():
    return {"Authorization": f"Basic {AUTH_HEADER}"}


def search_companies(query, items_per_page=20):
    """Search for companies by name."""
    resp = requests.get(
        f"{BASE_URL}/search/companies",
        params={"q": query, "items_per_page": items_per_page},
        headers=_headers()
    )
    resp.raise_for_status()
    return resp.json()


def get_company_profile(company_number):
    """Get company profile including status, type, registered office."""
    resp = requests.get(
        f"{BASE_URL}/company/{company_number}",
        headers=_headers()
    )
    resp.raise_for_status()
    return resp.json()


def get_officers(company_number, active_only=True):
    """Get current and former officers (directors) of a company."""
    params = {}
    if not active_only:
        params["register_view"] = "true"

    resp = requests.get(
        f"{BASE_URL}/company/{company_number}/officers",
        params=params,
        headers=_headers()
    )
    resp.raise_for_status()
    return resp.json()


def get_pscs(company_number):
    """Get Persons with Significant Control — beneficial owners."""
    resp = requests.get(
        f"{BASE_URL}/company/{company_number}/persons-with-significant-control",
        headers=_headers()
    )
    resp.raise_for_status()
    return resp.json()


def get_charges(company_number):
    """Get charges (security interests) — reveals financing activity."""
    resp = requests.get(
        f"{BASE_URL}/company/{company_number}/charges",
        headers=_headers()
    )
    resp.raise_for_status()
    return resp.json()


def get_filing_history(company_number, items_per_page=25, category=None):
    """
    Get filing history.
    Categories: accounts, annual-return, capital, change-of-name,
    confirmation-statement, incorporation, insolvency, liquidation,
    miscellaneous, mortgage, officers, resolution
    """
    params = {"items_per_page": items_per_page}
    if category:
        params["category"] = category

    resp = requests.get(
        f"{BASE_URL}/company/{company_number}/filing-history",
        params=params,
        headers=_headers()
    )
    resp.raise_for_status()
    return resp.json()


def search_officers(query, items_per_page=20):
    """Search for officers (directors) by name — find all their directorships."""
    resp = requests.get(
        f"{BASE_URL}/search/officers",
        params={"q": query, "items_per_page": items_per_page},
        headers=_headers()
    )
    resp.raise_for_status()
    return resp.json()


def get_officer_appointments(officer_id):
    """Get all appointments for a specific officer — reveals interlocking directorships."""
    resp = requests.get(
        f"{BASE_URL}/officers/{officer_id}/appointments",
        headers=_headers()
    )
    resp.raise_for_status()
    return resp.json()


# --- SIGNAL DETECTION FUNCTIONS ---

def detect_director_networks(director_name):
    """
    Find all companies where a director holds positions.
    Signal: Interlocking directorships, new entity formation.
    """
    officers = search_officers(director_name)

    appointments = []
    for item in officers.get("items", []):
        officer_id = item.get("links", {}).get("self", "").split("/officers/")[1].split("/")[0] if "/officers/" in item.get("links", {}).get("self", "") else None

        if officer_id:
            time.sleep(0.5)  # Rate limiting
            appts = get_officer_appointments(officer_id)
            for appt in appts.get("items", []):
                appointments.append({
                    "officer_name": item.get("title", ""),
                    "company_name": appt.get("appointed_to", {}).get("company_name", ""),
                    "company_number": appt.get("appointed_to", {}).get("company_number", ""),
                    "role": appt.get("officer_role", ""),
                    "appointed_on": appt.get("appointed_on", ""),
                    "resigned_on": appt.get("resigned_on", ""),
                })

    return appointments


def detect_new_charges(company_number, days_back=30):
    """
    Detect recently created charges (security interests).
    Signal: New financing, potential distress if secured lending replaces unsecured.
    """
    charges = get_charges(company_number)
    cutoff = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")

    new_charges = []
    for charge in charges.get("items", []):
        created = charge.get("created_on", "")
        if created >= cutoff:
            new_charges.append({
                "created_on": created,
                "charge_code": charge.get("charge_code", ""),
                "classification": charge.get("classification", {}).get("description", ""),
                "persons_entitled": [p.get("name", "") for p in charge.get("persons_entitled", [])],
                "particulars": charge.get("particulars", {}).get("description", ""),
                "status": charge.get("status", ""),
            })

    return new_charges


def detect_insolvency_filings(company_number):
    """
    Check for insolvency-related filings.
    Signal: Winding-up petitions, administration, liquidation.
    """
    filings = get_filing_history(company_number, category="insolvency")

    insolvency_events = []
    for item in filings.get("items", []):
        insolvency_events.append({
            "date": item.get("date", ""),
            "category": item.get("category", ""),
            "description": item.get("description", ""),
            "type": item.get("type", ""),
        })

    return insolvency_events


def detect_psc_changes(company_number, days_back=90):
    """
    Detect changes in Persons with Significant Control.
    Signal: Ownership shifts, new beneficial owners, stake increases.
    """
    pscs = get_pscs(company_number)
    cutoff = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")

    recent_changes = []
    for psc in pscs.get("items", []):
        notified = psc.get("notified_on", "")
        if notified >= cutoff:
            recent_changes.append({
                "name": psc.get("name", ""),
                "notified_on": notified,
                "nature_of_control": psc.get("natures_of_control", []),
                "kind": psc.get("kind", ""),
                "nationality": psc.get("nationality", ""),
                "country_of_residence": psc.get("country_of_residence", ""),
            })

    return recent_changes


# --- WATCHLIST MONITORING ---

# Example watchlist of UK-listed companies to monitor
WATCHLIST = {
    "Vodafone Group": "01833679",
    "Rolls-Royce Holdings": "07524813",
    "easyJet": "03959649",
    "Aston Martin Lagonda": "11488166",
    "Deliveroo": "11040982",
    # Add more company numbers as needed
}


def run_watchlist_scan():
    """Run signal detection across the watchlist."""
    results = {}

    for name, company_number in WATCHLIST.items():
        print(f"Scanning {name} ({company_number})...")

        signals = {
            "company": name,
            "company_number": company_number,
            "new_charges": detect_new_charges(company_number, days_back=30),
            "insolvency": detect_insolvency_filings(company_number),
            "psc_changes": detect_psc_changes(company_number, days_back=90),
        }

        # Flag if any signals found
        has_signals = (
            len(signals["new_charges"]) > 0 or
            len(signals["insolvency"]) > 0 or
            len(signals["psc_changes"]) > 0
        )

        if has_signals:
            results[name] = signals

        time.sleep(0.5)  # Rate limiting

    return results


# --- MAIN EXECUTION ---
if __name__ == "__main__":
    print("=" * 60)
    print("Companies House Monitor — UK Equity Signal Scanner")
    print("=" * 60)

    # NOTE: Replace API_KEY above with your actual key before running
    # Get a free key at: https://developer.company-information.service.gov.uk/

    print("\nThis script requires a free Companies House API key.")
    print("Get one at: https://developer.company-information.service.gov.uk/")
    print("\nFunctions available:")
    print("  - search_companies(query): Find companies by name")
    print("  - detect_director_networks(name): Map a director's company network")
    print("  - detect_new_charges(company_number): Find new financing/security interests")
    print("  - detect_insolvency_filings(company_number): Check for insolvency signals")
    print("  - detect_psc_changes(company_number): Detect ownership changes")
    print("  - run_watchlist_scan(): Scan all watchlist companies for signals")
