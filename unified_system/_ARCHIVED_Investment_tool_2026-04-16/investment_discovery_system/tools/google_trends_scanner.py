"""
Google Trends Signal Scanner
==============================
Uses Google Trends to detect unusual search interest spikes for
listed companies and their products/services.

Data Source: Google Trends (trends.google.com)
Library: pytrends (unofficial Google Trends API)
Cost: Free
Rate Limit: ~1 request per 2 seconds recommended

Signal Logic:
- Sudden spike in search interest (>200% of 90-day average) → potential catalyst
- Sustained search interest increase → growing consumer/investor attention
- Geographic concentration of search interest → market-specific catalyst
- Related queries → reveals what's driving the interest (product launch, scandal, M&A rumor)
"""

from pytrends.request import TrendReq
import time
from datetime import datetime, timedelta

# Initialize pytrends
pytrends = TrendReq(hl='en-US', tz=0)


def get_interest_over_time(keywords, timeframe='today 3-m', geo=''):
    """
    Get Google Trends interest over time for keywords.

    Args:
        keywords: List of up to 5 keywords
        timeframe: 'today 3-m' (3 months), 'today 12-m' (1 year), etc.
        geo: Country code ('ES' for Spain, 'GB' for UK, '' for global)

    Returns:
        DataFrame with interest over time
    """
    pytrends.build_payload(keywords, timeframe=timeframe, geo=geo)
    data = pytrends.interest_over_time()
    return data


def detect_spikes(keyword, geo='', threshold_multiplier=2.0):
    """
    Detect if a keyword has spiked above its 90-day average.

    Args:
        keyword: Search term
        geo: Country code
        threshold_multiplier: How many times above average to flag (default 2x)

    Returns:
        Dict with spike detection results
    """
    try:
        data = get_interest_over_time([keyword], timeframe='today 3-m', geo=geo)

        if data.empty:
            return {"keyword": keyword, "spike_detected": False, "reason": "No data"}

        values = data[keyword].values
        avg = values[:-7].mean()  # Average excluding last week
        recent = values[-7:].mean()  # Last week average

        spike_detected = recent > (avg * threshold_multiplier) and avg > 5  # Minimum baseline threshold

        return {
            "keyword": keyword,
            "geo": geo or "Global",
            "spike_detected": spike_detected,
            "recent_avg": round(recent, 1),
            "baseline_avg": round(avg, 1),
            "multiplier": round(recent / avg, 2) if avg > 0 else 0,
            "peak_value": int(values.max()),
            "peak_date": str(data[keyword].idxmax().date()) if not data.empty else None,
        }

    except Exception as e:
        return {"keyword": keyword, "spike_detected": False, "error": str(e)}


def get_related_queries(keyword, geo=''):
    """
    Get related queries — reveals what's driving search interest.

    Returns:
        Dict with rising and top related queries
    """
    pytrends.build_payload([keyword], timeframe='today 3-m', geo=geo)
    related = pytrends.related_queries()

    result = {"keyword": keyword, "rising": [], "top": []}

    if keyword in related:
        rising = related[keyword].get("rising")
        top = related[keyword].get("top")

        if rising is not None and not rising.empty:
            result["rising"] = rising.to_dict("records")
        if top is not None and not top.empty:
            result["top"] = top.to_dict("records")

    return result


def scan_watchlist(watchlist, geo=''):
    """
    Scan a watchlist of company names/tickers for search interest spikes.

    Args:
        watchlist: Dict of {display_name: search_term}
        geo: Country code

    Returns:
        List of spike alerts
    """
    alerts = []

    for name, search_term in watchlist.items():
        result = detect_spikes(search_term, geo=geo)
        result["company"] = name

        if result.get("spike_detected"):
            # Get related queries to understand the catalyst
            time.sleep(2)
            related = get_related_queries(search_term, geo=geo)
            result["related_rising"] = related.get("rising", [])[:5]
            alerts.append(result)

        time.sleep(2)  # Rate limiting

    return alerts


# --- EXAMPLE WATCHLISTS ---

# Spanish equities watchlist
SPANISH_WATCHLIST = {
    "Telefonica": "Telefonica",
    "Banco Santander": "Banco Santander",
    "BBVA": "BBVA",
    "Iberdrola": "Iberdrola",
    "Inditex": "Inditex",
    "Repsol": "Repsol",
    "CaixaBank": "CaixaBank",
    "Ferrovial": "Ferrovial",
    "Amadeus IT": "Amadeus IT",
    "Cellnex": "Cellnex",
    "Grifols": "Grifols",
    "Solaria": "Solaria",
    "Fluidra": "Fluidra",
    "Puig": "Puig brands",
}

# UK equities watchlist
UK_WATCHLIST = {
    "Vodafone": "Vodafone",
    "Rolls-Royce": "Rolls Royce",
    "easyJet": "easyJet",
    "Aston Martin": "Aston Martin",
    "THG": "THG company",
    "boohoo": "boohoo",
    "ASOS": "ASOS",
    "Deliveroo": "Deliveroo",
    "Darktrace": "Darktrace",
}


# --- MAIN EXECUTION ---
if __name__ == "__main__":
    print("=" * 60)
    print("Google Trends Signal Scanner")
    print("=" * 60)

    # Note: Requires pytrends installation: pip install pytrends

    # Scan Spanish equities (Spain geo)
    print("\n--- SPANISH EQUITIES SPIKE SCAN ---")
    spanish_alerts = scan_watchlist(SPANISH_WATCHLIST, geo='ES')
    if spanish_alerts:
        for alert in spanish_alerts:
            print(f"  SPIKE: {alert['company']}")
            print(f"    Recent avg: {alert['recent_avg']} | Baseline: {alert['baseline_avg']} | Multiplier: {alert['multiplier']}x")
            if alert.get("related_rising"):
                print(f"    Related queries: {[q['query'] for q in alert['related_rising'][:3]]}")
    else:
        print("  No spikes detected.")

    # Scan UK equities (UK geo)
    print("\n--- UK EQUITIES SPIKE SCAN ---")
    uk_alerts = scan_watchlist(UK_WATCHLIST, geo='GB')
    if uk_alerts:
        for alert in uk_alerts:
            print(f"  SPIKE: {alert['company']}")
            print(f"    Recent avg: {alert['recent_avg']} | Baseline: {alert['baseline_avg']} | Multiplier: {alert['multiplier']}x")
    else:
        print("  No spikes detected.")
