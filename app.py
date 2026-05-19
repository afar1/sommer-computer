#!/usr/bin/env python3
"""
Web interface for Provider Network Checker
"""

from collections import Counter
from datetime import datetime, timezone
from functools import lru_cache
from io import BytesIO
import hashlib
import json
import os
import re
import sys

from flask import Flask, render_template_string, request, jsonify
import requests
from urllib.parse import quote

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

app = Flask(__name__)

# NPI Registry API
NPI_API = "https://npiregistry.cms.hhs.gov/api/"
CMS_MARKETPLACE_API = "https://marketplace.api.healthcare.gov/api/v1"
CMS_MARKETPLACE_API_KEY = os.environ.get(
    "CMS_MARKETPLACE_API_KEY",
    "d687412e7b53146b2631dc01974ad0a4",
)
NETWORK_LOOKUP_TIMEOUT = 8
SOURCE_FRESHNESS_TIMEOUT = 5
SOURCE_FRESHNESS_TTL_SECONDS = 12 * 60 * 60
SOURCE_FRESHNESS_CACHE_PATH = os.environ.get(
    "SOURCE_FRESHNESS_CACHE_PATH",
    os.path.join(os.path.dirname(__file__), "source_freshness.json"),
)
CMS_PUF_PAGE_URL = "https://www.cms.gov/marketplace/resources/data/public-use-files"
CMS_PUF_LABELS = (
    "Plan Attributes PUF",
    "Service Area PUF",
    "Network PUF",
    "Machine-readable URL PUF",
)
CARRIER_OPTIONS = (
    {
        "value": "bcbstx",
        "label": "Blue Cross and Blue Shield of Texas",
        "source_carrier": "BCBS",
    },
    {
        "value": "uhc",
        "label": "UnitedHealthcare",
        "source_carrier": "UHC",
        "default": True,
    },
    {
        "value": "oscar",
        "label": "Oscar",
        "source_carrier": "Oscar",
        "default": False,
    },
)
DEFAULT_CARRIERS = {
    option["value"]
    for option in CARRIER_OPTIONS
    if option.get("default", True)
}
CARRIER_VALUES = {option["value"] for option in CARRIER_OPTIONS}
SOURCE_CARRIERS_BY_VALUE = {
    option["value"]: option["source_carrier"]
    for option in CARRIER_OPTIONS
}
# BCBSTX Provider Finder URL templates
BCBSTX_SEARCH_URLS = {
    "blue_advantage_hmo": {
        "name": "Blue Advantage HMO",
        "marketplace_issuer": "Blue Cross and Blue Shield of Texas",
        "marketplace_plan_name_contains": ["blue advantage", "hmo"],
        "marketplace_network_url_contains": "tx-blueadvantage-retail",
        "url": "https://my.providerfinderonline.com/search/name/{query}?ci=tx-blueadvantage-retail&network_id=1000128&geo_location={lat},{lon}&locale=en&corp_code=TX&radius={radius}",
    },
    "my_blue_health": {
        "name": "My Blue Health",
        "marketplace_issuer": "Blue Cross and Blue Shield of Texas",
        "marketplace_plan_name_contains": ["myblue health"],
        "marketplace_network_url_contains": "tx-myblue-health",
        "url": "https://my.providerfinderonline.com/search/name/{query}?ci=tx-myblue-health&network_id=240000127&geo_location={lat},{lon}&locale=en&corp_code=TX&radius={radius}",
    },
}

# UHC Provider Finder URLs (Rally/werally.com)
UHC_SEARCH_URLS = {
    "tx_individual_exchange": {
        "name": "UHC TX Individual Exchange",
        "marketplace_issuer": "UnitedHealthcare",
        "marketplace_network_url_contains": "xtxdocfindg2026",
        "network_code": "908",
        "configuration": "uhc",
        "plan_year": 2026,
        "url": "https://connect.werally.com/guest/eyJwbGFuTmFtZSI6IlRYIEluZGl2aWR1YWwgRXhjaGFuZ2UgQmVuZWZpdCBQbGFuIiwiZGVsc3lzIjoiOTA4IiwiY292ZXJhZ2VUeXBlIjoibWVkaWNhbCIsInBhcnRuZXJJZCI6InVoYyIsImxhbmd1YWdlIjoiZW4iLCJzaG93Q29zdHMiOnRydWUsImZpcHNDb2RlIjoiNDgifQMQFY_1U6GK3dWzJO0xysxZD0H-Ei_AJ0Wm_n0zlgcUI?planYear=2026",
    },
    "tx_kelsey_seybold": {
        "name": "UHC TX Kelsey-Seybold",
        "marketplace_issuer": "UnitedHealthcare",
        "marketplace_network_url_contains": "xtxdocfindks2026",
        "network_code": "933",
        "configuration": "uhc",
        "plan_year": 2026,
        "url": "https://connect.werally.com/guest/eyJwbGFuTmFtZSI6IlRYIEtlbHNleS1TZXlib2xkIEluZGl2aWR1YWwgRXhjaGFuZ2UgQmVuZWZpdCBQbGFuIiwiZGVsc3lzIjoiOTMzIiwiY292ZXJhZ2VUeXBlIjoibWVkaWNhbCIsInBhcnRuZXJJZCI6InVoYyIsImxhbmd1YWdlIjoiZW4iLCJzaG93Q29zdHMiOnRydWUsImZpcHNDb2RlIjoiNDgifQXmvT5Dh8azZlM00ptmfLp0BrlP0yAn-7TQUdErUzIbs?planYear=2026",
    },
    "tx_sanitas_anchor": {
        "name": "UHC TX Sanitas Anchor",
        "marketplace_issuer": "UnitedHealthcare",
        "marketplace_network_url_contains": "xtxdocfindsn2026",
        "network_code": "946",
        "configuration": "uhc.exchange",
        "plan_year": 2026,
        "url": "https://connect.werally.com/guest/eyJwbGFuTmFtZSI6IlRYIFNhbml0YXMgQW5jaG9yIEluZGl2aWR1YWwgRXhjaGFuZ2UgQmVuZWZpdCIsImRlbHN5cyI6Ijk0NiIsImNvdmVyYWdlVHlwZSI6Im1lZGljYWwiLCJwYXJ0bmVySWQiOiJ1aGMuZXhjaGFuZ2UiLCJsYW5ndWFnZSI6ImVuIiwic2hvd0Nvc3RzIjp0cnVlLCJmaXBzQ29kZSI6IjQ4In0B_6Q323L9wHzkAf22cjU43lZYKLoyfNzTYw8nrMBL04?planYear=2026",
    },
}

# Texas locations
TEXAS_LOCATIONS = {
    "dallas": (32.7767, -96.7970),
    "fort worth": (32.7555, -97.3308),
    "houston": (29.7604, -95.3698),
    "austin": (30.2672, -97.7431),
    "san antonio": (29.4241, -98.4936),
    "plano": (33.0198, -96.6989),
    "arlington": (32.7357, -97.1081),
    "irving": (32.8140, -96.9489),
    "frisco": (33.1507, -96.8236),
    "mckinney": (33.1972, -96.6397),
    "denton": (33.2148, -97.1331),
    "richardson": (32.9483, -96.7299),
}

TEXAS_MARKETPLACE_PLACES = {
    "dallas": {"zipcode": "75201", "countyfips": "48113", "state": "TX"},
    "fort worth": {"zipcode": "76102", "countyfips": "48439", "state": "TX"},
    "houston": {"zipcode": "77030", "countyfips": "48201", "state": "TX"},
    "austin": {"zipcode": "78701", "countyfips": "48453", "state": "TX"},
    "san antonio": {"zipcode": "78205", "countyfips": "48029", "state": "TX"},
    "plano": {"zipcode": "75024", "countyfips": "48085", "state": "TX"},
    "arlington": {"zipcode": "76010", "countyfips": "48439", "state": "TX"},
    "irving": {"zipcode": "75039", "countyfips": "48113", "state": "TX"},
    "frisco": {"zipcode": "75034", "countyfips": "48085", "state": "TX"},
    "mckinney": {"zipcode": "75070", "countyfips": "48085", "state": "TX"},
    "denton": {"zipcode": "76201", "countyfips": "48121", "state": "TX"},
    "richardson": {"zipcode": "75080", "countyfips": "48113", "state": "TX"},
}

SOURCE_CHECK_DATE = "2026-05-13"
PROVIDER_DIRECTORY = "Provider directory"
FORMULARY = "Formulary"
HTTP_200_HTML = "HTTP 200 text/html"
HTTP_200_PDF = "HTTP 200 application/pdf"
CMS_PRIMARY_ROLE = "Carrier cross-check; CMS Marketplace remains primary when plan IDs match."
FORMULARY_FALLBACK_ROLE = "Formulary evidence and fallback when CMS RxCUI output is suspect."
UNMAPPED_CARRIER_ROLE = "Carrier source to map next; CMS Marketplace structured matching not yet configured."
BEHAVIORAL_COLUMN_NOTE = "Need to decide whether behavioral networks get separate columns or directory-only links."
FORMULARY_DRIFT_NOTE = "2027 formulary URL may change."


def source_plain_english(name, kind, role):
    if kind == FORMULARY:
        return f"This is the carrier formulary for {name}. Use it to manually confirm drug coverage when CMS RxCUI data looks unclear."
    if role == CMS_PRIMARY_ROLE:
        return f"This is the carrier directory for {name}. Use it as a manual cross-check when CMS says a provider is likely in network."
    if "Behavioral" in role:
        return f"This is the behavioral-health directory for {name}. It is not part of the automated CMS lookup yet."
    return f"This is the carrier directory for {name}. Use it for manual confirmation until this carrier is mapped into structured lookup."


def carrier_source(name, kind, identifier, url, validation, role, note, year="2026"):
    return {
        "name": name,
        "kind": kind,
        "year": year,
        "identifier": identifier,
        "url": url,
        "checked_on": SOURCE_CHECK_DATE,
        "validation": validation,
        "description": source_plain_english(name, kind, role),
        "role": role,
        "note": note,
    }


def provider_source(name, identifier, url, role, note):
    return carrier_source(name, PROVIDER_DIRECTORY, identifier, url, HTTP_200_HTML, role, note)


def formulary_source(name, identifier, url, validation=HTTP_200_PDF, note=FORMULARY_DRIFT_NOTE):
    return carrier_source(name, FORMULARY, identifier, url, validation, FORMULARY_FALLBACK_ROLE, note)


CARRIER_SOURCE_GROUPS = [
    {
        "carrier": "BCBS",
        "sources": [
            provider_source(
                "Blue Advantage",
                "network_id=1000128; ci=tx-blueadvantage-retail",
                "https://my.providerfinderonline.com/?ci=tx-blueadvantage-retail&corp_code=TX&network_id=1000128&geo_location=32.753521,-97.331527&locale=en",
                CMS_PRIMARY_ROLE,
                "Directory links depend on coordinates; app-generated links should keep passing city coordinates.",
            ),
            provider_source(
                "MyBlue Health",
                "network_id=240000127; ci=tx-myblue-health",
                "https://my.providerfinderonline.com/?ci=tx-myblue-health&corp_code=TX&network_id=240000127&geo_location=32.753521,-97.331527&locale=en",
                CMS_PRIMARY_ROLE,
                "Directory links depend on coordinates; app-generated links should keep passing city coordinates.",
            ),
            formulary_source(
                "4-Tier",
                "2026_TX_4T_HIM.pdf",
                "https://www.myprime.com/content/dam/prime/memberportal/WebDocs/2026/Formularies/HIM/2026_TX_4T_HIM.pdf",
            ),
            formulary_source(
                "6-Tier",
                "2026_TX_6T_HIM.pdf",
                "https://www.myprime.com/content/dam/prime/memberportal/WebDocs/2026/Formularies/HIM/2026_TX_6T_HIM.pdf",
            ),
        ],
        "open_questions": [
            "Confirm 2027 BCBS formulary URLs when released.",
            "Keep confirming coordinate-dependent provider links for each supported city.",
        ],
    },
    {
        "carrier": "UHC",
        "sources": [
            provider_source(
                "Individual Exchange HMO",
                "IFP medical plan-selection landing",
                "https://findcare.guest.uhc.com/guest-plan-selection/plan-selection?planSelectionLob=IFP&coverageType=M&chipValue=All",
                "Carrier cross-check; CMS Marketplace network_url fragments drive automated status.",
                "Landing page may require user plan/location choices before a specific provider search.",
            ),
            provider_source(
                "Individual Exchange Behavioral Health",
                "behavioralProvider/root",
                "https://connect.werally.com/behavioralProvider/root",
                "Behavioral-health carrier cross-check; not yet an automated CMS status column.",
                BEHAVIORAL_COLUMN_NOTE,
            ),
            provider_source(
                "Sanitas",
                "San Antonio only; IFP medical plan-selection landing",
                "https://findcare.guest.uhc.com/guest-plan-selection/plan-selection?planSelectionLob=IFP&coverageType=M&chipValue=All",
                "Carrier cross-check for San Antonio only; CMS Marketplace not-offered logic stays location-specific.",
                "Only expected in San Antonio markets.",
            ),
            provider_source(
                "Sanitas Behavioral Health",
                "San Antonio only; behavioralProvider/root",
                "https://connect.werally.com/behavioralProvider/root",
                "Behavioral-health carrier cross-check for San Antonio only.",
                BEHAVIORAL_COLUMN_NOTE,
            ),
            provider_source(
                "Kelsey-Seybold",
                "Houston only; IFP medical plan-selection landing",
                "https://findcare.guest.uhc.com/guest-plan-selection/plan-selection?planSelectionLob=IFP&coverageType=M&chipValue=All",
                "Carrier cross-check for Houston only; CMS Marketplace not-offered logic stays location-specific.",
                "Only expected in Houston markets.",
            ),
            provider_source(
                "Kelsey-Seybold Behavioral Health",
                "Houston only; behavioralProvider/root",
                "https://connect.werally.com/behavioralProvider/root",
                "Behavioral-health carrier cross-check for Houston only.",
                BEHAVIORAL_COLUMN_NOTE,
            ),
            formulary_source(
                "Individual Exchange HMO",
                "GPX526TX",
                "https://welcome.optumrx.com/rxexternal/external-prescription-drug-list?type=ClientFormulary&var=GPX526TX&infoid=GPX526TX&page=insert&par=",
                validation=HTTP_200_HTML,
                note="2027 formulary URL or var code may change.",
            ),
        ],
        "open_questions": [
            "Decide whether UHC behavioral directories should become separate lookup columns.",
            "Confirm 2027 OptumRx formulary code when released.",
        ],
    },
    {
        "carrier": "Oscar",
        "sources": [
            provider_source(
                "Individual Texas EPO",
                "networkId=064",
                "https://www.hioscar.com/search/?networkId=064&year=2026",
                UNMAPPED_CARRIER_ROLE,
                "Need to determine whether Oscar network IDs can be linked to CMS plan/network data.",
            ),
            provider_source(
                "Individual Texas HMO",
                "networkId=059",
                "https://www.hioscar.com/search/?networkId=059&year=2026",
                UNMAPPED_CARRIER_ROLE,
                "Need to determine whether Oscar network IDs can be linked to CMS plan/network data.",
            ),
            formulary_source(
                "4-Tier",
                "Oscar_4T_TX_STND_Member_Doc__May_2026__as_of_04232026.pdf",
                "https://assets.ctfassets.net/plyq12u1bv8a/5W9SoIT8xdqyUOubCEkXPu/15537604594f76ddfdbb7afb2c6d7fdf/Oscar_4T_TX_STND_Member_Doc__May_2026__as_of_04232026.pdf",
            ),
            formulary_source(
                "6-Tier",
                "Oscar_6T_TX_STND_Member_Doc__May_2026__as_of_04232026.pdf",
                "https://assets.ctfassets.net/plyq12u1bv8a/1kKyM41GUUlVBx4F9I2EPC/caf94c63da03c324f32b5270cca8ab87/Oscar_6T_TX_STND_Member_Doc__May_2026__as_of_04232026.pdf",
            ),
        ],
        "open_questions": [
            "Map Oscar networkId values to CMS issuer/plan/network identifiers before automating status.",
            "Confirm 2027 Oscar formulary URLs when released.",
        ],
    },
    {
        "carrier": "Imperial",
        "sources": [
            provider_source(
                "Exchange HMO",
                "texas/hmo-exchange",
                "https://exchange.imperialhealthplan.com/texas/hmo-exchange/online-provider-directory/",
                UNMAPPED_CARRIER_ROLE,
                "Need to determine whether this can be queried or only opened for manual confirmation.",
            ),
        ],
        "open_questions": [
            "Identify CMS issuer/plan/network mapping for Imperial Exchange HMO.",
            "Determine whether the directory supports query URLs or only manual search.",
            "Determine which non-CMS carrier links can be queried directly versus opened for manual confirmation.",
        ],
    },
    {
        "carrier": "Community Health Choice",
        "sources": [
            provider_source(
                "Premier Gold",
                "Houston only; find-a-provider",
                "https://memberaccount.communityhealthchoice.org/s/find-a-provider?language=en_US",
                UNMAPPED_CARRIER_ROLE,
                "Directory requires provider/facility type and area before search.",
            ),
            formulary_source(
                "Premier Gold",
                "formulary-premier-2026.pdf",
                "https://www.communityhealthchoice.org/wp-content/uploads/2025/05/formulary-premier-2026.pdf",
            ),
        ],
        "open_questions": [
            "Handle the required provider/facility type and area input before automating Community searches.",
            "Confirm 2027 Premier Gold formulary URL when released.",
        ],
    },
]


def utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_utc_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def flatten_carrier_sources(source_groups=None):
    flattened = []
    for group in source_groups or CARRIER_SOURCE_GROUPS:
        carrier = group["carrier"]
        for source in group.get("sources", []):
            source_id = "::".join([
                carrier,
                source.get("kind", ""),
                source.get("name", ""),
                source.get("identifier", ""),
            ])
            flattened.append({
                **source,
                "id": source_id,
                "carrier": carrier,
            })
    return flattened


def load_source_freshness_cache():
    try:
        with open(SOURCE_FRESHNESS_CACHE_PATH, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def save_source_freshness_cache(cache):
    directory = os.path.dirname(SOURCE_FRESHNESS_CACHE_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(SOURCE_FRESHNESS_CACHE_PATH, "w", encoding="utf-8") as handle:
        json.dump(cache, handle, indent=2, sort_keys=True)


def extract_cms_puf_updates(html):
    updates = {}
    for label in CMS_PUF_LABELS:
        match = re.search(
            rf"{re.escape(label)}.*?Updated\s+([A-Za-z]+\s+\d{{1,2}},\s+\d{{4}})",
            html,
            re.IGNORECASE | re.DOTALL,
        )
        if match:
            updates[label] = match.group(1)
    return updates


def response_header(response, name):
    return getattr(response, "headers", {}).get(name, "")


def response_ok(response):
    return 200 <= getattr(response, "status_code", 0) < 400


def response_metadata(response, method, content_hash=""):
    return {
        "method": method,
        "status_code": getattr(response, "status_code", 0),
        "ok": response_ok(response),
        "url": getattr(response, "url", ""),
        "etag": response_header(response, "ETag"),
        "last_modified": response_header(response, "Last-Modified"),
        "content_length": response_header(response, "Content-Length"),
        "content_type": response_header(response, "Content-Type"),
        "content_hash": content_hash,
    }


def metadata_changed(previous, current):
    for key in ("etag", "last_modified", "content_length", "content_hash"):
        old_value = previous.get(key)
        new_value = current.get(key)
        if old_value and new_value and old_value != new_value:
            return True
    return False


def check_url_metadata(url):
    try:
        response = requests.head(
            url,
            allow_redirects=True,
            timeout=SOURCE_FRESHNESS_TIMEOUT,
        )
        if response_ok(response):
            metadata = response_metadata(response, "HEAD")
            if metadata["etag"] or metadata["last_modified"] or metadata["content_length"]:
                return metadata
    except requests.RequestException:
        pass

    try:
        response = requests.get(
            url,
            allow_redirects=True,
            stream=True,
            timeout=SOURCE_FRESHNESS_TIMEOUT,
        )
        hasher = hashlib.sha256()
        bytes_read = 0
        for chunk in response.iter_content(chunk_size=8192):
            if not chunk:
                continue
            remaining = 128 * 1024 - bytes_read
            if remaining <= 0:
                break
            sample = chunk[:remaining]
            hasher.update(sample)
            bytes_read += len(sample)
        return response_metadata(response, "GET", content_hash=hasher.hexdigest())
    except requests.RequestException as error:
        return {
            "method": "GET",
            "status_code": 0,
            "ok": False,
            "url": url,
            "etag": "",
            "last_modified": "",
            "content_length": "",
            "content_type": "",
            "content_hash": "",
            "error": str(error),
        }


def source_status_from_metadata(previous, metadata):
    if not metadata.get("ok"):
        return "missing"
    if metadata_changed(previous, metadata):
        return "changed"
    if not (metadata.get("etag") or metadata.get("last_modified") or metadata.get("content_length") or metadata.get("content_hash")):
        return "suspect"
    return "ok"


def check_cms_puf_page(previous_cache):
    previous_puf = previous_cache.get("puf", {})
    try:
        response = requests.get(CMS_PUF_PAGE_URL, timeout=SOURCE_FRESHNESS_TIMEOUT)
        response.raise_for_status()
        html = response.text
        page_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()
        updates = extract_cms_puf_updates(html)
        puf = {
            "url": CMS_PUF_PAGE_URL,
            "status": "ok",
            "checked_at": utc_now_iso(),
            "content_hash": page_hash,
            "updates": updates,
        }
        if previous_puf.get("content_hash") and previous_puf["content_hash"] != page_hash:
            puf["status"] = "changed"
        return puf
    except (requests.RequestException, ValueError) as error:
        return {
            "url": CMS_PUF_PAGE_URL,
            "status": "suspect",
            "checked_at": utc_now_iso(),
            "content_hash": previous_puf.get("content_hash", ""),
            "updates": previous_puf.get("updates", {}),
            "error": str(error),
        }


def check_source_freshness(previous_cache=None):
    previous_cache = previous_cache if previous_cache is not None else load_source_freshness_cache()
    previous_sources = {
        source.get("id"): source
        for source in previous_cache.get("sources", [])
    }
    checked_at = utc_now_iso()
    source_entries = []

    for source in flatten_carrier_sources():
        metadata = check_url_metadata(source["url"])
        previous = previous_sources.get(source["id"], {})
        status = source_status_from_metadata(previous, metadata)
        source_entries.append({
            **source,
            **metadata,
            "status": status,
            "checked_at": checked_at,
        })

    cache = {
        "last_checked": checked_at,
        "ttl_seconds": SOURCE_FRESHNESS_TTL_SECONDS,
        "puf": check_cms_puf_page(previous_cache),
        "sources": source_entries,
    }
    save_source_freshness_cache(cache)
    return cache


def source_freshness_age_seconds(cache):
    checked_at = parse_utc_iso(cache.get("last_checked"))
    if not checked_at:
        return None
    return max(0, int((datetime.now(timezone.utc) - checked_at).total_seconds()))


def source_freshness_summary(cache=None):
    cache = cache if cache is not None else load_source_freshness_cache()
    if not cache:
        return {
            "status": "never_checked",
            "last_checked": "",
            "age_seconds": None,
            "ttl_seconds": SOURCE_FRESHNESS_TTL_SECONDS,
            "in_progress": False,
            "counts": {"total": len(flatten_carrier_sources()), "ok": 0, "changed": 0, "suspect": 0, "missing": 0},
            "puf": {},
            "sources": [],
        }

    counts = Counter(source.get("status", "suspect") for source in cache.get("sources", []))
    age_seconds = source_freshness_age_seconds(cache)
    if counts.get("changed") or cache.get("puf", {}).get("status") == "changed":
        status = "changed"
    elif counts.get("suspect") or counts.get("missing") or cache.get("puf", {}).get("status") == "suspect":
        status = "suspect"
    else:
        status = "fresh"

    return {
        "status": status,
        "last_checked": cache.get("last_checked", ""),
        "age_seconds": age_seconds,
        "ttl_seconds": cache.get("ttl_seconds", SOURCE_FRESHNESS_TTL_SECONDS),
        "in_progress": False,
        "counts": {
            "total": len(cache.get("sources", [])),
            "ok": counts.get("ok", 0),
            "changed": counts.get("changed", 0),
            "suspect": counts.get("suspect", 0),
            "missing": counts.get("missing", 0),
        },
        "puf": cache.get("puf", {}),
        "sources": cache.get("sources", []),
    }


HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Provider Network Checker</title>
    <style>
        * {
            box-sizing: border-box;
        }
        :root {
            --ink: #20242a;
            --ink-muted: #59616d;
            --ink-soft: #858c96;
            --line: #e6e9ee;
            --line-soft: #f0f2f5;
            --bg: #f8f8f7;
            --panel: #ffffff;
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            max-width: 2200px;
            margin: 0 auto;
            padding: 24px;
            background: var(--bg);
            color: var(--ink);
        }
        h1 {
            color: var(--ink);
            font-size: 25px;
            letter-spacing: -0.02em;
            margin: 0;
        }
        .case-header {
            align-items: flex-end;
            border-bottom: 1px solid var(--line);
            display: flex;
            gap: 18px;
            justify-content: space-between;
            margin-bottom: 18px;
            padding-bottom: 14px;
        }
        .eyebrow {
            color: var(--ink-soft);
            font-size: 10px;
            font-weight: 800;
            letter-spacing: 0.08em;
            margin-bottom: 5px;
            text-transform: uppercase;
        }
        .subtitle {
            color: var(--ink-muted);
            font-size: 12px;
            margin: 5px 0 0;
        }
        .header-actions {
            align-items: center;
            color: var(--ink-soft);
            display: flex;
            flex-wrap: wrap;
            font-size: 12px;
            gap: 12px;
            justify-content: flex-end;
        }
        .sample-toggle {
            background: transparent;
            border: 0;
            color: var(--ink-muted);
            font-size: 12px;
            font-weight: 650;
            padding: 0;
            text-decoration: underline;
            text-underline-offset: 3px;
            width: auto;
        }
        .sample-toggle:hover {
            background: transparent;
            color: var(--ink);
        }
        .search-form {
            background: var(--line);
            border: 1px solid var(--line);
            border-radius: 10px;
            display: grid;
            gap: 1px;
            grid-template-columns: minmax(0, 1.35fr) minmax(240px, 0.8fr) minmax(220px, 0.65fr);
            margin-bottom: 22px;
            overflow: hidden;
            padding: 0;
        }
        .intake-block {
            background: var(--panel);
            padding: 14px;
        }
        .intake-block-title {
            color: var(--ink-soft);
            font-size: 10px;
            font-weight: 800;
            letter-spacing: 0.07em;
            margin-bottom: 10px;
            text-transform: uppercase;
        }
        .form-group {
            margin-bottom: 12px;
        }
        .form-group:last-child {
            margin-bottom: 0;
        }
        label {
            display: block;
            margin-bottom: 6px;
            font-weight: 600;
            color: var(--ink-muted);
            font-size: 12px;
        }
        input, select, textarea {
            width: 100%;
            padding: 10px 11px;
            border: 1px solid var(--line);
            border-radius: 6px;
            font-size: 14px;
            transition: border-color 0.2s;
            background: #fbfbfb;
            color: var(--ink);
            font-family: inherit;
        }
        textarea {
            min-height: 42px;
            resize: vertical;
        }
        input:focus, select:focus, textarea:focus {
            outline: none;
            border-color: #111827;
            background: white;
        }
        .form-row {
            display: flex;
            gap: 10px;
        }
        .form-row .form-group {
            flex: 1;
        }
        button {
            background: #20242a;
            color: white;
            padding: 10px 14px;
            border: none;
            border-radius: 6px;
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
            width: 100%;
            transition: background 0.2s;
        }
        button:hover {
            background: #111827;
        }
        button:disabled {
            background: #9ca3af;
            cursor: not-allowed;
        }
        .run-copy {
            color: var(--ink-muted);
            font-size: 12px;
            line-height: 1.5;
            margin: 0 0 12px;
        }
        .demo-scenarios {
            background: var(--panel);
            border: 1px solid var(--line);
            border-radius: 8px;
            display: none;
            margin: -8px 0 22px;
            padding: 12px 14px;
        }
        .demo-scenarios.is-open {
            display: block;
        }
        .demo-title {
            color: var(--ink-muted);
            font-size: 12px;
            font-weight: 600;
            margin-bottom: 10px;
        }
        .demo-actions {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
        }
        .demo-random,
        .demo-link {
            align-items: center;
            border-radius: 999px;
            display: inline-flex;
            font-size: 13px;
            font-weight: 600;
            min-height: 36px;
            padding: 8px 12px;
            text-decoration: none;
            width: auto;
        }
        .demo-random {
            background: var(--ink);
            color: white;
        }
        .demo-random:hover {
            background: #374151;
        }
        .demo-link {
            background: #f3f4f6;
            color: var(--ink);
        }
        .demo-link:hover {
            background: #e5e7eb;
        }
        .results {
            background: white;
            border-radius: 12px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            overflow: hidden;
        }
        .result-card {
            padding: 20px;
            border-bottom: 1px solid #e0e0e0;
        }
        .result-card:last-child {
            border-bottom: none;
        }
        .doctor-name {
            font-size: 18px;
            font-weight: 600;
            color: #1f2937;
            margin-bottom: 15px;
        }
        .npi-results {
            margin-bottom: 20px;
        }
        .npi-item {
            background: #f8fafc;
            padding: 15px;
            border-radius: 8px;
            margin-bottom: 10px;
            border-left: 4px solid #10b981;
        }
        .npi-item.not-found {
            border-left-color: #ef4444;
            background: #fef2f2;
        }
        .npi-name {
            font-weight: 600;
            color: #1f2937;
        }
        .npi-detail {
            color: #6b7280;
            font-size: 14px;
            margin-top: 5px;
        }
        .network-section {
            margin-bottom: 12px;
        }
        .network-section:last-child {
            margin-bottom: 0;
        }
        .network-label {
            font-size: 12px;
            font-weight: 600;
            color: #6b7280;
            margin-bottom: 6px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .network-links {
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
        }
        .network-link {
            display: inline-block;
            padding: 10px 16px;
            background: #eff6ff;
            color: #2563eb;
            text-decoration: none;
            border-radius: 6px;
            font-size: 14px;
            font-weight: 500;
            transition: background 0.2s;
        }
        .network-link:hover {
            background: #dbeafe;
        }
        .network-link.uhc {
            background: #fef3c7;
            color: #92400e;
        }
        .network-link.uhc:hover {
            background: #fde68a;
        }
        .picker-list {
            align-items: flex-start;
            display: flex;
            flex-direction: column;
            gap: 8px;
        }
        .selected-rx-list {
            display: flex;
            flex-direction: column;
            gap: 7px;
            width: 100%;
        }
        .selected-rx-empty {
            border: 1px dashed var(--line);
            border-radius: 6px;
            color: var(--ink-soft);
            font-size: 12px;
            padding: 10px 11px;
        }
        .selected-rx-chip {
            align-items: flex-start;
            background: #f8fafc;
            border: 1px solid var(--line);
            border-radius: 6px;
            display: flex;
            gap: 8px;
            justify-content: space-between;
            padding: 9px 10px;
        }
        .selected-rx-name {
            color: var(--ink);
            font-size: 13px;
            font-weight: 750;
            line-height: 1.25;
        }
        .selected-rx-meta {
            color: var(--ink-muted);
            font-size: 11px;
            margin-top: 2px;
        }
        .selected-rx-remove {
            background: transparent;
            color: var(--ink-soft);
            flex: 0 0 auto;
            font-size: 18px;
            line-height: 1;
            padding: 0 2px;
            width: auto;
        }
        .selected-rx-remove:hover {
            background: transparent;
            color: var(--ink);
        }
        .secondary-action {
            background: #f3f6f8;
            border: 1px solid var(--line);
            color: var(--ink);
            width: auto;
        }
        .secondary-action:hover {
            background: #e8edf2;
        }
        .modal-backdrop {
            align-items: center;
            background: rgba(32, 36, 42, 0.45);
            bottom: 0;
            display: flex;
            justify-content: center;
            left: 0;
            padding: 24px;
            position: fixed;
            right: 0;
            top: 0;
            z-index: 20;
        }
        .modal-backdrop[hidden] {
            display: none;
        }
        .prescription-modal {
            background: white;
            border-radius: 8px;
            box-shadow: 0 22px 70px rgba(0, 0, 0, 0.25);
            max-height: min(760px, calc(100vh - 48px));
            max-width: 760px;
            overflow: hidden;
            width: min(760px, 100%);
        }
        .modal-header,
        .modal-footer {
            align-items: center;
            display: flex;
            justify-content: space-between;
            padding: 18px 22px;
        }
        .modal-header {
            border-bottom: 1px solid var(--line);
        }
        .modal-footer {
            background: #f8fafc;
            border-top: 1px solid var(--line);
        }
        .modal-title {
            color: #2b6cb0;
            font-size: 24px;
            font-weight: 750;
        }
        .modal-close {
            background: transparent;
            color: var(--ink-muted);
            font-size: 28px;
            line-height: 1;
            padding: 0 2px;
            width: auto;
        }
        .modal-close:hover {
            background: transparent;
            color: var(--ink);
        }
        .modal-body {
            max-height: calc(100vh - 220px);
            overflow-y: auto;
            padding: 18px 22px 20px;
        }
        .modal-copy {
            color: var(--ink-muted);
            font-size: 14px;
            line-height: 1.45;
            margin: 0 0 14px;
        }
        .drug-search-wrap {
            align-items: center;
            border: 1px solid var(--line);
            border-radius: 6px;
            display: flex;
            margin-bottom: 14px;
            overflow: hidden;
        }
        .drug-search-wrap:focus-within {
            border-color: #2b6cb0;
            box-shadow: 0 0 0 2px rgba(43, 108, 176, 0.12);
        }
        .drug-search-wrap input {
            border: 0;
            border-radius: 0;
            background: white;
        }
        .drug-search-wrap input:focus {
            border: 0;
            box-shadow: none;
        }
        .drug-clear {
            background: transparent;
            color: #2b6cb0;
            font-size: 13px;
            padding: 0 12px;
            width: auto;
        }
        .drug-clear:hover {
            background: transparent;
            color: #1d4ed8;
        }
        .drug-option-list {
            border: 1px solid var(--line);
            border-radius: 6px;
            max-height: 330px;
            overflow-y: auto;
        }
        .drug-option {
            align-items: center;
            border-bottom: 1px solid var(--line);
            display: flex;
            gap: 14px;
            justify-content: space-between;
            padding: 13px 16px;
        }
        .drug-option:last-child {
            border-bottom: 0;
        }
        .drug-option:hover {
            background: #f8fafc;
        }
        .drug-option-name {
            color: var(--ink);
            font-size: 14px;
            font-weight: 750;
            line-height: 1.25;
        }
        .drug-option-meta {
            color: var(--ink-muted);
            font-size: 12px;
            margin-top: 4px;
        }
        .drug-formulary-chips {
            display: flex;
            flex-wrap: wrap;
            gap: 5px;
            margin-top: 8px;
        }
        .drug-formulary-chip {
            align-items: center;
            background: #ecfdf5;
            border: 1px solid #a7f3d0;
            border-radius: 4px;
            color: #047857;
            display: inline-flex;
            font-size: 10px;
            font-weight: 800;
            gap: 4px;
            line-height: 1.2;
            padding: 3px 5px;
        }
        .drug-option-add {
            background: white;
            border: 1px solid var(--line);
            color: #2b6cb0;
            flex: 0 0 auto;
            width: auto;
        }
        .drug-option-add:hover {
            background: #eff6ff;
        }
        .modal-selected {
            display: flex;
            flex-direction: column;
            gap: 10px;
            margin-top: 14px;
        }
        .modal-selected-title {
            color: var(--ink-soft);
            font-size: 11px;
            font-weight: 800;
            letter-spacing: 0.07em;
            text-transform: uppercase;
        }
        .provider-search-wrap {
            align-items: center;
            border: 1px solid var(--line);
            border-radius: 6px;
            display: flex;
            margin-bottom: 14px;
            overflow: hidden;
        }
        .provider-search-wrap:focus-within {
            border-color: #2b6cb0;
            box-shadow: 0 0 0 2px rgba(43, 108, 176, 0.12);
        }
        .provider-search-wrap input {
            border: 0;
            border-radius: 0;
            background: white;
        }
        .carrier-options {
            display: grid;
            gap: 10px;
        }
        .carrier-option {
            align-items: flex-start;
            color: var(--ink-muted);
            display: flex;
            font-size: 0.98rem;
            gap: 10px;
            line-height: 1.35;
            margin-bottom: 0;
        }
        .carrier-option input[type="checkbox"] {
            accent-color: #2b6cb0;
            flex: 0 0 auto;
            height: 16px;
            margin-top: 2px;
            padding: 0;
            width: 16px;
        }
        .network-matrix {
            width: 100%;
            border-collapse: collapse;
            min-width: 1160px;
            table-layout: fixed;
        }
        .network-matrix-wrap {
            background: white;
            border: 1px solid var(--line);
            border-radius: 10px;
            margin-bottom: 24px;
            overflow: hidden;
        }
        .network-matrix-scroll {
            overflow-x: auto;
        }
        .network-matrix-title {
            align-items: baseline;
            color: var(--ink-soft);
            display: flex;
            font-size: 10px;
            font-weight: 800;
            gap: 12px;
            letter-spacing: 0.07em;
            padding: 16px 20px 10px;
            text-transform: uppercase;
        }
        .network-matrix-title::after {
            background: var(--line);
            content: "";
            flex: 1;
            height: 1px;
        }
        .matrix-title-copy {
            color: var(--ink-muted);
            font-size: 12px;
            font-weight: 500;
            letter-spacing: 0;
            text-transform: none;
        }
        .matrix-actions {
            color: var(--ink-soft);
            display: inline-flex;
            font-size: 12px;
            font-weight: 650;
            gap: 14px;
            letter-spacing: 0;
            margin-left: auto;
            text-transform: none;
        }
        .network-matrix-note {
            padding: 0 20px 10px;
            color: var(--ink-muted);
            font-size: 12px;
        }
        .network-matrix th,
        .network-matrix td {
            border-top: 1px solid #e5e7eb;
            padding: 9px 8px;
            text-align: center;
            vertical-align: middle;
        }
        .network-matrix th {
            background: #f3f6f8;
            color: var(--ink);
            font-size: 13px;
            font-weight: 600;
            line-height: 1.2;
        }
        .network-matrix th:first-child,
        .network-matrix td:first-child {
            min-width: 260px;
            text-align: left;
            width: 260px;
        }
        .network-matrix th:not(:first-child),
        .network-matrix td:not(:first-child) {
            border-left: 1px solid #eef2f7;
            min-width: 150px;
        }
        .network-matrix th.row-total-header,
        .network-matrix td.row-total-cell {
            min-width: 110px;
            width: 110px;
        }
        .network-matrix tbody tr.lookup-row:hover td:not(:first-child):not(.row-total-cell) {
            background: #fbfcfd;
        }
        .network-matrix [data-column-index]:not([data-column-index="0"]).column-hover {
            background: #f0f9ff;
        }
        .network-matrix th[data-column-index].column-hover {
            background: #e0f2fe;
        }
        .network-matrix td.network-cell:hover {
            background: #fbfcfd;
        }
        .lookup-section-row td {
            background: #f3f6f8;
            padding: 10px 20px;
        }
        .lookup-section-row .section-count {
            color: var(--ink-soft);
            float: right;
            font-size: 12px;
            font-weight: 600;
            letter-spacing: 0;
            text-transform: none;
        }
        .lookup-section-title {
            color: var(--ink);
            display: inline;
            font-size: 13px;
            font-weight: 800;
            letter-spacing: 0.06em;
            margin-bottom: 2px;
            text-transform: uppercase;
        }
        .lookup-section-copy {
            color: var(--ink-muted);
            display: inline;
            font-size: 13px;
            line-height: 1.35;
            margin-left: 8px;
        }
        .lookup-item-cell {
            position: relative;
        }
        .lookup-item-content {
            padding: 1px 24px 1px 0;
            position: relative;
        }
        .lookup-item-heading-row {
            align-items: flex-start;
            display: flex;
            gap: 8px;
        }
        .lookup-item-heading {
            color: var(--ink);
            font-size: 14px;
            font-weight: 750;
            line-height: 1.25;
            min-width: 0;
        }
        .lookup-item-meta {
            color: #6b7280;
            font-size: 11px;
            line-height: 1.35;
            margin-top: 5px;
            text-transform: none;
        }
        .network-provider {
            color: #1f2937;
            font-weight: 600;
        }
        .provider-tag {
            display: inline-block;
            padding: 2px 7px;
            border-radius: 999px;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
        }
        .provider-tag.item-tag {
            flex: 0 0 auto;
            margin-left: auto;
        }
        .provider-tag.doctor {
            background: #dbeafe;
            color: #1d4ed8;
        }
        .provider-tag.facility {
            background: #ede9fe;
            color: #6d28d9;
        }
        .provider-tag.rx {
            background: #ccfbf1;
            color: #0f766e;
        }
        .provider-tag.not-found {
            background: #fee2e2;
            color: #991b1b;
        }
        .network-status {
            align-items: center;
            display: inline-flex;
            gap: 4px;
            justify-content: center;
            min-width: 0;
            padding: 4px 8px;
            border-radius: 999px;
            font-size: 11px;
            font-weight: 700;
            line-height: 1;
            white-space: nowrap;
        }
        .carrier-header {
            text-align: left;
        }
        .carrier-name {
            color: var(--ink);
            font-size: 13px;
            font-weight: 800;
            line-height: 1.2;
            margin-bottom: 7px;
        }
        .carrier-meta,
        .carrier-counts {
            color: var(--ink-soft);
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            font-size: 11px;
            font-weight: 600;
            line-height: 1.45;
        }
        .carrier-covered {
            color: #166534;
            font-size: 17px;
            font-weight: 800;
        }
        .carrier-caution {
            color: #92400e;
        }
        .carrier-blocking {
            color: #991b1b;
        }
        .plan-arrow {
            color: var(--ink-soft);
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            font-size: 12px;
            font-weight: 800;
            text-transform: uppercase;
        }
        .network-cell {
            background: #fbfcfd;
            padding: 4px;
        }
        .matrix-status-card {
            border: 1px solid var(--line);
            border-radius: 5px;
            cursor: pointer;
            display: flex;
            flex-direction: column;
            gap: 18px;
            justify-content: space-between;
            min-height: 58px;
            padding: 7px 8px;
            text-align: left;
        }
        .network-cell:focus-visible .matrix-status-card {
            outline: 2px solid #2563eb;
            outline-offset: 2px;
        }
        .matrix-status-card.formulary-split {
            gap: 8px;
        }
        .matrix-status-card.in {
            background: #e7f8eb;
            border-color: #9bd8aa;
            color: #064e3b;
        }
        .matrix-status-card.generic-covered {
            background: #ddf7ee;
            border-color: #8bd3bd;
            color: #065f46;
        }
        .matrix-status-card.partial-coverage,
        .matrix-status-card.partial-match,
        .matrix-status-card.data-not-provided,
        .matrix-status-card.lookup-error,
        .matrix-status-card.suspect {
            background: #fff3d6;
            border-color: #e7c46b;
            color: #78350f;
        }
        .matrix-status-card.review-exact-drug,
        .matrix-status-card.other-form-covered {
            background: #e7f8eb;
            border-color: #9bd8aa;
            color: #065f46;
        }
        .matrix-status-card.related-product-covered {
            background: #d9f2fb;
            border-color: #8fcde6;
            color: #075985;
        }
        .matrix-status-card.out,
        .matrix-status-card.not-found,
        .matrix-status-card.no-record {
            background: #fde8e8;
            border-color: #f5a3a3;
            color: #991b1b;
        }
        .matrix-status-card.not-offered,
        .matrix-status-card.not-configured,
        .matrix-status-card.no-result {
            background: #f3f6f8;
            border-color: #d8dee5;
            color: #7b8490;
        }
        .matrix-status-top {
            align-items: center;
            display: flex;
            gap: 5px;
        }
        .matrix-status-icon {
            align-items: center;
            border-radius: 4px;
            color: white;
            display: inline-flex;
            font-size: 10px;
            font-weight: 900;
            height: 16px;
            justify-content: center;
            line-height: 1;
            width: 16px;
        }
        .matrix-status-card.in .matrix-status-icon,
        .matrix-status-card.generic-covered .matrix-status-icon,
        .matrix-status-icon.in,
        .matrix-status-icon.generic-covered {
            background: #15936a;
        }
        .matrix-status-card.partial-coverage .matrix-status-icon,
        .matrix-status-card.partial-match .matrix-status-icon,
        .matrix-status-card.data-not-provided .matrix-status-icon,
        .matrix-status-card.lookup-error .matrix-status-icon,
        .matrix-status-card.suspect .matrix-status-icon,
        .matrix-status-icon.partial-coverage,
        .matrix-status-icon.partial-match,
        .matrix-status-icon.data-not-provided,
        .matrix-status-icon.lookup-error,
        .matrix-status-icon.suspect {
            background: #c98a18;
        }
        .matrix-status-card.review-exact-drug .matrix-status-icon,
        .matrix-status-card.other-form-covered .matrix-status-icon,
        .matrix-status-icon.review-exact-drug,
        .matrix-status-icon.other-form-covered {
            background: #15936a;
        }
        .matrix-status-card.related-product-covered .matrix-status-icon,
        .matrix-status-icon.related-product-covered {
            background: #159ac7;
        }
        .matrix-status-card.out .matrix-status-icon,
        .matrix-status-card.not-found .matrix-status-icon,
        .matrix-status-card.no-record .matrix-status-icon,
        .matrix-status-icon.out,
        .matrix-status-icon.not-found,
        .matrix-status-icon.no-record {
            background: #cc3d3d;
        }
        .matrix-status-card.not-offered .matrix-status-icon,
        .matrix-status-card.not-configured .matrix-status-icon,
        .matrix-status-card.no-result .matrix-status-icon,
        .matrix-status-icon.not-offered,
        .matrix-status-icon.not-configured,
        .matrix-status-icon.no-result {
            background: #aab3bd;
        }
        .matrix-status-label {
            font-size: 12px;
            font-weight: 800;
            line-height: 1.15;
            white-space: normal;
        }
        .matrix-source {
            border: 1px solid currentColor;
            border-radius: 4px;
            font-size: 9px;
            font-weight: 800;
            margin-left: auto;
            opacity: 0.45;
            padding: 0 3px;
        }
        .matrix-status-detail {
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            font-size: 11px;
            font-weight: 800;
            opacity: 0.82;
        }
        .formulary-tier-grid {
            display: grid;
            gap: 6px;
            grid-template-columns: repeat(2, minmax(0, 1fr));
        }
        .formulary-tier-column {
            border-left: 1px solid rgba(6, 78, 59, 0.18);
            min-width: 0;
            padding-left: 6px;
        }
        .formulary-tier-name {
            color: currentColor;
            font-size: 10px;
            font-weight: 900;
            line-height: 1.1;
            opacity: 0.68;
            text-transform: uppercase;
        }
        .formulary-tier-value {
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            font-size: 11px;
            font-weight: 850;
            line-height: 1.2;
            margin-top: 3px;
        }
        .status-popover {
            background: white;
            border: 1px solid #cbd5e1;
            border-radius: 8px;
            box-shadow: 0 18px 48px rgba(15, 23, 42, 0.22);
            color: var(--ink);
            max-width: min(360px, calc(100vw - 32px));
            padding: 12px 14px;
            position: fixed;
            text-align: left;
            z-index: 50;
        }
        .status-popover-title {
            align-items: center;
            display: flex;
            gap: 7px;
            font-size: 13px;
            font-weight: 850;
            line-height: 1.2;
            margin-bottom: 10px;
        }
        .status-popover-close {
            background: transparent;
            color: var(--ink-soft);
            font-size: 18px;
            line-height: 1;
            margin-left: auto;
            padding: 0 2px;
            width: auto;
        }
        .status-popover-close:hover {
            background: transparent;
            color: var(--ink);
        }
        .status-popover-row {
            border-top: 1px solid #eef2f7;
            padding: 8px 0;
        }
        .status-popover-label {
            color: var(--ink-soft);
            font-size: 10px;
            font-weight: 850;
            letter-spacing: 0.06em;
            line-height: 1.2;
            text-transform: uppercase;
        }
        .status-popover-value {
            color: var(--ink);
            font-size: 12px;
            line-height: 1.4;
            margin-top: 3px;
        }
        .status-popover-value.mono {
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            font-weight: 750;
        }
        .row-total {
            color: var(--ink-soft);
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            font-size: 12px;
            font-weight: 700;
            line-height: 1.2;
        }
        .row-total-covered {
            color: #166534;
            font-size: 20px;
            font-weight: 900;
        }
        .row-total-caution {
            color: #92400e;
        }
        .matrix-legend {
            align-items: center;
            background: #f3f6f8;
            border-top: 1px solid var(--line);
            color: var(--ink-muted);
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            padding: 12px 20px;
        }
        .matrix-legend-title {
            color: var(--ink-soft);
            font-size: 11px;
            font-weight: 800;
            letter-spacing: 0.07em;
            text-transform: uppercase;
        }
        .legend-status {
            align-items: center;
            border-radius: 5px;
            cursor: help;
            display: inline-flex;
            font-size: 12px;
            gap: 5px;
            padding: 2px 4px;
            position: relative;
        }
        .legend-status:hover,
        .legend-status:focus {
            background: white;
            outline: 1px solid var(--line);
        }
        .network-status-icon {
            flex: 0 0 auto;
            font-size: 14px;
            line-height: 1;
        }
        .network-status.in {
            background: #dcfce7;
            color: #15803d;
        }
        .network-status.out,
        .network-status.not-found,
        .network-status.no-record,
        .network-status.suspect {
            background: #fee2e2;
            color: #b91c1c;
        }
        .network-status.generic-covered {
            background: #ccfbf1;
            color: #0f766e;
        }
        .network-status.review-exact-drug,
        .network-status.other-form-covered,
        .network-status.related-product-covered {
            background: #fef3c7;
            color: #92400e;
        }
        .network-status.likely-in,
        .network-status.likely-covered {
            background: #dcfce7;
            color: #166534;
        }
        .network-status.partial-coverage {
            background: #fef3c7;
            color: #92400e;
        }
        .network-status.data-not-provided {
            background: #fef3c7;
            color: #92400e;
        }
        .network-status.lookup-error {
            background: #fef3c7;
            color: #92400e;
        }
        .network-status.not-offered,
        .network-status.not-configured,
        .network-status.no-result {
            background: #e0f2fe;
            color: #0369a1;
        }
        .network-status.unknown {
            background: #f3f4f6;
            color: #6b7280;
        }
        .network-status-detail {
            color: #6b7280;
            font-size: 10px;
            line-height: 1.25;
            margin-top: 4px;
        }
        .network-cell,
        .info-tip {
            position: relative;
        }
        .info-tip {
            align-items: center;
            background: #eef2ff;
            border: 1px solid #c7d2fe;
            border-radius: 999px;
            color: #4338ca;
            cursor: help;
            display: inline-flex;
            flex: 0 0 auto;
            font-size: 10px;
            font-weight: 800;
            height: 15px;
            justify-content: center;
            line-height: 1;
            text-transform: none;
            width: 15px;
        }
        .info-tip.item-info {
            position: absolute;
            right: 0;
            top: 0;
        }
        [data-tooltip]:hover::after,
        [data-tooltip]:focus::after {
            background: #111827;
            border-radius: 6px;
            bottom: calc(100% + 8px);
            color: white;
            content: attr(data-tooltip);
            font-size: 11px;
            font-weight: 500;
            left: 50%;
            line-height: 1.35;
            max-width: 320px;
            min-width: 220px;
            padding: 8px 10px;
            pointer-events: none;
            position: absolute;
            text-align: left;
            text-transform: none;
            transform: translateX(-50%);
            white-space: normal;
            z-index: 20;
        }
        .info-tip[data-tooltip]:hover::after,
        .info-tip[data-tooltip]:focus::after {
            bottom: calc(100% + 6px);
            left: 0;
            transform: none;
        }
        .source-freshness {
            align-items: center;
            background: white;
            border: 1px solid var(--line);
            border-radius: 10px;
            display: flex;
            gap: 16px;
            justify-content: space-between;
            margin-bottom: 24px;
            padding: 14px 18px;
        }
        .source-freshness-title {
            color: #1f2937;
            font-size: 13px;
            font-weight: 700;
            margin-bottom: 3px;
        }
        .source-freshness-copy {
            color: #6b7280;
            font-size: 12px;
            line-height: 1.35;
        }
        .provider-details {
            background: white;
            border: 1px solid var(--line);
            border-radius: 10px;
            margin-bottom: 30px;
            overflow: hidden;
        }
        .provider-details summary {
            color: #1f2937;
            cursor: pointer;
            font-size: 16px;
            font-weight: 700;
            list-style-position: inside;
            padding: 16px 20px;
        }
        .provider-details .results {
            border-radius: 0;
            box-shadow: none;
            margin-bottom: 0;
        }
        .source-details {
            background: white;
            border: 1px solid var(--line);
            border-radius: 10px;
            margin-bottom: 30px;
            overflow: hidden;
        }
        .source-details summary {
            color: #1f2937;
            cursor: pointer;
            font-size: 16px;
            font-weight: 700;
            list-style-position: inside;
            padding: 16px 20px;
        }
        .source-group {
            border-top: 1px solid #e5e7eb;
            padding: 16px 20px;
        }
        .source-carrier {
            color: #1f2937;
            font-size: 15px;
            font-weight: 700;
            margin-bottom: 10px;
        }
        .source-row {
            border-top: 1px solid #f3f4f6;
            padding: 10px 0;
        }
        .source-row:first-of-type {
            border-top: 0;
        }
        .source-link {
            color: #2563eb;
            font-weight: 600;
            text-decoration: none;
        }
        .source-link:hover {
            text-decoration: underline;
        }
        .source-meta,
        .source-description,
        .source-note,
        .source-question {
            color: #6b7280;
            font-size: 12px;
            line-height: 1.4;
            margin-top: 4px;
        }
        .source-description {
            color: #374151;
            font-weight: 600;
        }
        .source-question {
            color: #92400e;
        }
        .loading {
            text-align: center;
            padding: 40px;
            color: #6b7280;
        }
        .spinner {
            border: 3px solid #e0e0e0;
            border-top: 3px solid #2563eb;
            border-radius: 50%;
            width: 30px;
            height: 30px;
            animation: spin 1s linear infinite;
            margin: 0 auto 15px;
        }
        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
        .hint {
            font-size: 13px;
            color: #6b7280;
            margin-top: 5px;
        }
        .badge {
            display: inline-block;
            padding: 3px 8px;
            border-radius: 4px;
            font-size: 12px;
            font-weight: 500;
            margin-left: 8px;
        }
        .badge-success {
            background: #d1fae5;
            color: #065f46;
        }
        .badge-error {
            background: #fee2e2;
            color: #991b1b;
        }
        .footer {
            text-align: center;
            margin-top: 48px;
            padding: 24px;
            color: #6b7280;
            font-size: 14px;
        }
        .footer a {
            background: linear-gradient(135deg, #8b5cf6, #ec4899);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            font-weight: 600;
            text-decoration: none;
        }
        .footer a:hover {
            opacity: 0.9;
        }
        @media (max-width: 540px) {
            body {
                padding: 12px;
            }
            .case-header,
            .header-actions {
                align-items: flex-start;
                flex-direction: column;
            }
            .search-form {
                grid-template-columns: 1fr;
            }
            .form-row {
                flex-direction: column;
                gap: 0;
            }
            .result-card {
                padding: 16px;
            }
            .network-link {
                padding: 12px 16px;
                min-height: 44px;
                display: inline-flex;
                align-items: center;
            }
            .network-matrix th:first-child,
            .network-matrix td:first-child {
                min-width: 220px;
                width: 220px;
            }
            .source-freshness {
                align-items: flex-start;
                flex-direction: column;
            }
        }
    </style>
</head>
<body>
    <header class="case-header">
        <div>
            <div class="eyebrow">Case · Provider Network Checker</div>
            <h1>New client coverage check</h1>
            <p class="subtitle">Answer first, evidence underneath, carrier portals for final confirmation.</p>
        </div>
        <div class="header-actions">
            <span>Megan · broker view</span>
            <button type="button" class="sample-toggle" id="sampleToggle">Sample cases</button>
        </div>
    </header>

    <form id="searchForm" class="search-form">
        <section class="intake-block">
            <div class="intake-block-title">Client doctors, facilities & prescriptions</div>
            <div class="form-group">
                <label for="doctors">Doctor Name(s)</label>
                <textarea id="doctors" name="doctors" rows="2"
                          placeholder="e.g., Dr. John Doe, Maria Garcia"></textarea>
                <p class="hint">Separate names with commas. These search individual NPI records.</p>
            </div>

            <div class="form-group">
                <label for="facilities">Facility Name(s)</label>
                <input type="hidden" id="facilities" name="facilities">
                <input type="hidden" id="facilitySelections" name="facility_selections">
                <div class="picker-list">
                    <div id="selectedFacilities" class="selected-rx-list"></div>
                    <button type="button" id="openFacilityModal" class="secondary-action">Add facility</button>
                </div>
                <p class="hint">Search organization NPI records and choose the exact facility when the address matters.</p>
            </div>

            <div class="form-group">
                <label for="prescriptions">Prescription Name(s)</label>
                <input type="hidden" id="prescriptions" name="prescriptions">
                <input type="hidden" id="prescriptionSelections" name="prescription_selections">
                <div class="picker-list">
                    <div id="selectedPrescriptions" class="selected-rx-list"></div>
                    <button type="button" id="openPrescriptionModal" class="secondary-action">Add prescription</button>
                </div>
                <p class="hint">Choose a generic name or a specific brand, strength, and form. Exact RxCUI selection reduces broad-name ambiguity.</p>
            </div>
        </section>

        <section class="intake-block">
            <div class="intake-block-title">Service area</div>
            <div class="form-row">
                <div class="form-group">
                    <label for="location">Location</label>
                    <select id="location" name="location">
                        <option value="dallas">Dallas</option>
                        <option value="fort worth">Fort Worth</option>
                        <option value="houston">Houston</option>
                        <option value="austin">Austin</option>
                        <option value="san antonio">San Antonio</option>
                        <option value="plano">Plano</option>
                        <option value="arlington">Arlington</option>
                        <option value="irving">Irving</option>
                        <option value="frisco">Frisco</option>
                        <option value="mckinney">McKinney</option>
                        <option value="denton">Denton</option>
                        <option value="richardson">Richardson</option>
                    </select>
                </div>

                <div class="form-group">
                    <label for="radius">Radius (miles)</label>
                    <select id="radius" name="radius">
                        <option value="10">10 miles</option>
                        <option value="25" selected>25 miles</option>
                        <option value="50">50 miles</option>
                        <option value="100">100 miles</option>
                    </select>
                </div>
            </div>

            <div class="form-group">
                <label for="city">Filter by City (optional)</label>
                <input type="text" id="city" name="city"
                       placeholder="e.g., Dallas (narrows NPI results)">
            </div>

            <div class="form-group">
                <label>Carriers</label>
                <input type="hidden" name="carrier_filter_submitted" value="true">
                <div class="carrier-options">
                    <label class="carrier-option">
                        <input type="checkbox" name="carriers" value="bcbstx" checked>
                        <span>Blue Cross and Blue Shield of Texas</span>
                    </label>
                    <label class="carrier-option">
                        <input type="checkbox" name="carriers" value="uhc" checked>
                        <span>UnitedHealthcare</span>
                    </label>
                    <label class="carrier-option">
                        <input type="checkbox" name="carriers" value="oscar">
                        <span>Oscar</span>
                    </label>
                </div>
                <p class="hint">Selected carriers decide which plan columns, provider links, and formulary sources are checked.</p>
            </div>
        </section>

        <section class="intake-block">
            <div class="intake-block-title">Run check</div>
            <p class="run-copy">Search CMS Marketplace data, then use carrier links for the exact NPI, product, tier, and final client quote.</p>
            <button type="submit" id="searchBtn">Run check</button>
        </section>
    </form>

    <div class="demo-scenarios" id="demoScenarios">
        <div class="demo-title">Try a mixed provider + prescription sample</div>
        <div class="demo-actions">
            <button type="button" class="demo-random" id="randomDemoBtn">Random mixed sample</button>
            <a href="?sample=dallas-specialty" class="demo-link" data-sample="dallas-specialty">Dallas doctor + hospital</a>
            <a href="?sample=houston-mix" class="demo-link" data-sample="houston-mix">Houston clinic + Rx</a>
            <a href="?sample=austin-mix" class="demo-link" data-sample="austin-mix">Austin facilities + Rx</a>
            <a href="?sample=san-antonio-wide" class="demo-link" data-sample="san-antonio-wide">San Antonio wide + Rx</a>
        </div>
    </div>

    <div id="sourceStatus"></div>
    <div id="results"></div>

    <div class="modal-backdrop" id="facilityModal" hidden>
        <div class="prescription-modal" role="dialog" aria-modal="true" aria-labelledby="facilityModalTitle">
            <div class="modal-header">
                <div class="modal-title" id="facilityModalTitle">Add a facility</div>
                <button type="button" class="modal-close" id="closeFacilityModal" aria-label="Close facility picker">&times;</button>
            </div>
            <div class="modal-body">
                <p class="modal-copy">Search by facility name, then choose the exact location by specialty and address.</p>
                <div class="provider-search-wrap">
                    <input type="search" id="facilitySearchInput" placeholder="Enter a facility name" autocomplete="off">
                    <button type="button" class="drug-clear" id="clearFacilitySearch">Clear</button>
                </div>
                <div id="facilitySearchResults" class="drug-option-list"></div>
                <div class="modal-selected">
                    <div class="modal-selected-title">Selected facilities</div>
                    <div id="modalSelectedFacilities" class="selected-rx-list"></div>
                </div>
            </div>
            <div class="modal-footer">
                <span class="hint">Exact NPI and address are used in the coverage matrix.</span>
                <button type="button" id="doneFacilityModal" class="secondary-action">Done</button>
            </div>
        </div>
    </div>

    <div class="modal-backdrop" id="prescriptionModal" hidden>
        <div class="prescription-modal" role="dialog" aria-modal="true" aria-labelledby="prescriptionModalTitle">
            <div class="modal-header">
                <div class="modal-title" id="prescriptionModalTitle">Add a prescription</div>
                <button type="button" class="modal-close" id="closePrescriptionModal" aria-label="Close prescription picker">&times;</button>
            </div>
            <div class="modal-body">
                <p class="modal-copy">Search by generic or brand name, then choose the exact option when you know it.</p>
                <div class="drug-search-wrap">
                    <input type="search" id="drugSearchInput" placeholder="Enter a prescription name" autocomplete="off">
                    <button type="button" class="drug-clear" id="clearDrugSearch">Clear</button>
                </div>
                <div id="drugSearchResults" class="drug-option-list"></div>
                <div class="modal-selected">
                    <div class="modal-selected-title">Selected prescriptions</div>
                    <div id="modalSelectedPrescriptions" class="selected-rx-list"></div>
                </div>
            </div>
            <div class="modal-footer">
                <span class="hint">Carrier formulary is still final for tier and restrictions.</span>
                <button type="button" id="donePrescriptionModal" class="secondary-action">Done</button>
            </div>
        </div>
    </div>

    <script>
        const sampleScenarios = {
            'dallas-specialty': {
                doctors: 'John Smith (Physician Assistant)',
                facilities: 'Baylor University Medical Center (General Acute)',
                prescriptions: 'Ibuprofen, Levothyroxine',
                location: 'dallas',
                radius: '25',
                city: 'Dallas',
            },
            'houston-mix': {
                doctors: 'Maria Garcia',
                facilities: 'Houston Methodist Hospital, Kelsey Seybold Clinic',
                prescriptions: 'Ozempic, Metformin',
                location: 'houston',
                radius: '25',
                city: 'Houston',
            },
            'austin-mix': {
                doctors: 'Sarah Lee',
                facilities: "St. David's Medical Center, Ascension Seton Medical Center Austin",
                prescriptions: 'Humira, Albuterol',
                location: 'austin',
                radius: '25',
                city: 'Austin',
            },
            'san-antonio-wide': {
                doctors: 'Juan Martinez',
                facilities: 'Methodist Hospital, University Hospital',
                prescriptions: 'Atorvastatin, Lisinopril',
                location: 'san antonio',
                radius: '50',
                city: 'San Antonio',
            },
        };

        const searchForm = document.getElementById('searchForm');
        const sourceStatusDiv = document.getElementById('sourceStatus');
        const resultsDiv = document.getElementById('results');
        const facilityInput = document.getElementById('facilities');
        const facilitySelectionsInput = document.getElementById('facilitySelections');
        const selectedFacilitiesDiv = document.getElementById('selectedFacilities');
        const modalSelectedFacilitiesDiv = document.getElementById('modalSelectedFacilities');
        const facilityModal = document.getElementById('facilityModal');
        const facilitySearchInput = document.getElementById('facilitySearchInput');
        const facilitySearchResults = document.getElementById('facilitySearchResults');
        const prescriptionInput = document.getElementById('prescriptions');
        const prescriptionSelectionsInput = document.getElementById('prescriptionSelections');
        const selectedPrescriptionsDiv = document.getElementById('selectedPrescriptions');
        const modalSelectedPrescriptionsDiv = document.getElementById('modalSelectedPrescriptions');
        const prescriptionModal = document.getElementById('prescriptionModal');
        const drugSearchInput = document.getElementById('drugSearchInput');
        const drugSearchResults = document.getElementById('drugSearchResults');
        const carrierInputs = Array.from(document.querySelectorAll('input[name="carriers"]'));
        let selectedFacilities = [];
        let facilitySearchTimer = null;
        let selectedPrescriptions = [];
        let drugSearchTimer = null;
        let drugSearchRequest = null;
        let drugSearchSequence = 0;
        let activeStatusPopover = null;

        searchForm.addEventListener('submit', async function(e) {
            e.preventDefault();

            const btn = document.getElementById('searchBtn');

            btn.disabled = true;
            btn.textContent = 'Searching...';
            resultsDiv.innerHTML = '<div class="loading"><div class="spinner"></div>Checking NPI Registry and public Marketplace data...</div>';

            const formData = new FormData(this);
            const params = new URLSearchParams(formData);

            try {
                const response = await fetch('/search?' + params.toString());
                const data = await response.json();

                const providerResults = Array.isArray(data) ? data : data.providers;
                const prescriptionResults = Array.isArray(data) ? [] : data.prescriptions;
                const networks = Array.isArray(data) ? collectNetworks(data) : data.networks;
                const sourceGroups = Array.isArray(data) ? [] : data.sources || [];

                let html = renderLookupMatrix(providerResults, prescriptionResults, networks);

                html += renderProviderDetails(providerResults);
                html += renderCarrierSourceDetails(sourceGroups);
                resultsDiv.innerHTML = html;

            } catch (error) {
                resultsDiv.innerHTML = '<div class="result-card"><div class="npi-item not-found">Error: ' + error.message + '</div></div>';
            }

            btn.disabled = false;
            btn.textContent = 'Run check';
        });

        resultsDiv.addEventListener('pointerover', function(e) {
            const cell = e.target.closest('[data-column-index]');
            if (!cell || !resultsDiv.contains(cell)) {
                return;
            }
            setColumnHover(cell);
        });

        resultsDiv.addEventListener('pointerleave', clearColumnHover);
        resultsDiv.addEventListener('click', function(e) {
            const cell = e.target.closest('.network-cell[data-status-detail]');
            if (!cell || !resultsDiv.contains(cell)) {
                return;
            }
            showStatusPopover(cell);
        });
        resultsDiv.addEventListener('keydown', function(e) {
            if (e.key !== 'Enter' && e.key !== ' ') {
                return;
            }
            const cell = e.target.closest('.network-cell[data-status-detail]');
            if (!cell || !resultsDiv.contains(cell)) {
                return;
            }
            e.preventDefault();
            showStatusPopover(cell);
        });
        document.addEventListener('click', function(e) {
            if (!activeStatusPopover) {
                return;
            }
            if (activeStatusPopover.contains(e.target) || e.target.closest('.network-cell[data-status-detail]')) {
                return;
            }
            closeStatusPopover();
        });
        document.addEventListener('keydown', function(e) {
            if (e.key === 'Escape') {
                closeStatusPopover();
            }
        });

        for (const link of document.querySelectorAll('[data-sample]')) {
            link.addEventListener('click', function(e) {
                e.preventDefault();
                applyScenario(this.dataset.sample);
            });
        }

        document.getElementById('randomDemoBtn').addEventListener('click', function() {
            const sampleIds = Object.keys(sampleScenarios);
            const sampleId = sampleIds[Math.floor(Math.random() * sampleIds.length)];
            applyScenario(sampleId);
        });

        document.getElementById('sampleToggle').addEventListener('click', function() {
            document.getElementById('demoScenarios').classList.toggle('is-open');
        });

        document.getElementById('openFacilityModal').addEventListener('click', openFacilityModal);
        document.getElementById('closeFacilityModal').addEventListener('click', closeFacilityModal);
        document.getElementById('doneFacilityModal').addEventListener('click', closeFacilityModal);
        document.getElementById('clearFacilitySearch').addEventListener('click', function() {
            facilitySearchInput.value = '';
            renderFacilitySearchResults([]);
            facilitySearchInput.focus();
        });
        facilityModal.addEventListener('click', function(e) {
            if (e.target === facilityModal) {
                closeFacilityModal();
            }
        });
        facilitySearchInput.addEventListener('input', function() {
            window.clearTimeout(facilitySearchTimer);
            const query = this.value.trim();
            facilitySearchTimer = window.setTimeout(function() {
                searchFacilityOptions(query);
            }, 250);
        });
        facilitySearchResults.addEventListener('click', function(e) {
            const textButton = e.target.closest('[data-add-facility-text]');
            if (textButton) {
                addFacilitySelection({
                    display_name: textButton.dataset.addFacilityText,
                    name: textButton.dataset.addFacilityText,
                    selection_type: 'text',
                });
                return;
            }
            const button = e.target.closest('[data-provider-index]');
            if (!button) {
                return;
            }
            const provider = JSON.parse(button.dataset.provider);
            addFacilitySelection(provider);
        });
        selectedFacilitiesDiv.addEventListener('click', removeFacilityFromClick);
        modalSelectedFacilitiesDiv.addEventListener('click', removeFacilityFromClick);

        document.getElementById('openPrescriptionModal').addEventListener('click', openPrescriptionModal);
        document.getElementById('closePrescriptionModal').addEventListener('click', closePrescriptionModal);
        document.getElementById('donePrescriptionModal').addEventListener('click', closePrescriptionModal);
        document.getElementById('clearDrugSearch').addEventListener('click', function() {
            drugSearchInput.value = '';
            if (drugSearchRequest) {
                drugSearchRequest.abort();
                drugSearchRequest = null;
            }
            renderDrugSearchResults([]);
            drugSearchInput.focus();
        });
        prescriptionModal.addEventListener('click', function(e) {
            if (e.target === prescriptionModal) {
                closePrescriptionModal();
            }
        });
        drugSearchInput.addEventListener('input', function() {
            window.clearTimeout(drugSearchTimer);
            const query = this.value.trim();
            drugSearchTimer = window.setTimeout(function() {
                searchDrugOptions(query);
            }, 250);
        });
        drugSearchResults.addEventListener('click', function(e) {
            const textButton = e.target.closest('[data-add-rx-text]');
            if (textButton) {
                addPrescriptionSelection({
                    display_name: textButton.dataset.addRxText,
                    prescription: textButton.dataset.addRxText,
                    selection_type: 'text',
                });
                return;
            }
            const button = e.target.closest('[data-drug-index]');
            if (!button) {
                return;
            }
            const drug = JSON.parse(button.dataset.drug);
            addPrescriptionSelection(drug);
        });
        selectedPrescriptionsDiv.addEventListener('click', removePrescriptionFromClick);
        modalSelectedPrescriptionsDiv.addEventListener('click', removePrescriptionFromClick);
        const initialSample = new URLSearchParams(window.location.search).get('sample');
        if (initialSample && sampleScenarios[initialSample]) {
            applyScenario(initialSample);
        }
        syncFacilitySelectionInputs();
        syncPrescriptionSelectionInputs();
        loadSourceStatus();

        function applyScenario(sampleId) {
            const scenario = sampleScenarios[sampleId];
            if (!scenario) {
                return;
            }

            document.getElementById('doctors').value = scenario.doctors || scenario.providers || '';
            setSelectedFacilitiesFromText(scenario.facilities || '');
            setSelectedPrescriptionsFromText(scenario.prescriptions || '');
            document.getElementById('location').value = scenario.location;
            document.getElementById('radius').value = scenario.radius;
            document.getElementById('city').value = scenario.city;
            searchForm.requestSubmit();
        }

        function openFacilityModal() {
            facilityModal.hidden = false;
            renderSelectedFacilities();
            window.setTimeout(function() {
                facilitySearchInput.focus();
            }, 0);
        }

        function closeFacilityModal() {
            facilityModal.hidden = true;
        }

        function setSelectedFacilitiesFromText(value) {
            selectedFacilities = String(value || '')
                .split(',')
                .map(item => item.trim())
                .filter(Boolean)
                .map(item => ({
                    display_name: item,
                    name: item,
                    selection_type: 'text',
                }));
            syncFacilitySelectionInputs();
        }

        function syncFacilitySelectionInputs() {
            facilityInput.value = selectedFacilities.map(providerDisplayName).join(', ');
            facilitySelectionsInput.value = JSON.stringify(
                selectedFacilities.filter(item => item.npi)
            );
            renderSelectedFacilities();
        }

        function providerDisplayName(item) {
            return item.display_name || item.name || '';
        }

        function providerMeta(item) {
            if (item.selection_type === 'text') {
                return 'Broad facility name';
            }
            return [item.specialty, item.address || item.location, item.npi ? `NPI ${item.npi}` : '']
                .filter(Boolean)
                .join(' · ');
        }

        function renderSelectedFacilities() {
            const html = selectedFacilities.length
                ? selectedFacilities.map((item, index) => {
                    return `<div class="selected-rx-chip">
                        <div>
                            <div class="selected-rx-name">${escapeHtml(providerDisplayName(item))}</div>
                            <div class="selected-rx-meta">${escapeHtml(providerMeta(item))}</div>
                        </div>
                        <button type="button" class="selected-rx-remove" data-remove-facility="${index}" aria-label="Remove ${escapeHtml(providerDisplayName(item))}">&times;</button>
                    </div>`;
                }).join('')
                : '<div class="selected-rx-empty">No facilities selected yet.</div>';
            selectedFacilitiesDiv.innerHTML = html;
            modalSelectedFacilitiesDiv.innerHTML = html;
        }

        function removeFacilityFromClick(e) {
            const button = e.target.closest('[data-remove-facility]');
            if (!button) {
                return;
            }
            selectedFacilities.splice(Number(button.dataset.removeFacility), 1);
            syncFacilitySelectionInputs();
        }

        function addFacilitySelection(provider) {
            if (provider.npi && selectedFacilities.some(item => item.npi === provider.npi)) {
                return;
            }
            const providerName = providerDisplayName(provider).toLowerCase();
            if (!provider.npi && selectedFacilities.some(item => providerDisplayName(item).toLowerCase() === providerName)) {
                return;
            }
            selectedFacilities.push(provider);
            syncFacilitySelectionInputs();
        }

        async function searchFacilityOptions(query) {
            if (query.length < 2) {
                renderFacilitySearchResults([]);
                return;
            }
            facilitySearchResults.innerHTML = '<div class="selected-rx-empty">Searching...</div>';
            const params = new URLSearchParams({
                q: query,
                type: 'facility',
                city: document.getElementById('city').value || '',
            });
            try {
                const response = await fetch('/providers/search?' + params.toString());
                const data = await response.json();
                renderFacilitySearchResults(data.providers || []);
            } catch (error) {
                facilitySearchResults.innerHTML = '<div class="selected-rx-empty">Facility lookup failed. Try again.</div>';
            }
        }

        function renderFacilitySearchResults(providers) {
            if (!providers.length) {
                const query = facilitySearchInput.value.trim();
                if (query.length >= 2) {
                    const safeQuery = escapeHtml(query);
                    facilitySearchResults.innerHTML = `<div class="drug-option">
                        <div>
                            <div class="drug-option-name">${safeQuery}</div>
                            <div class="drug-option-meta">No exact facility match found</div>
                        </div>
                        <button type="button" class="drug-option-add" data-add-facility-text="${safeQuery}">Add anyway</button>
                    </div>`;
                    return;
                }
                facilitySearchResults.innerHTML = '<div class="selected-rx-empty">Search for a hospital, clinic, or facility name.</div>';
                return;
            }
            facilitySearchResults.innerHTML = providers.map((provider, index) => {
                const payload = escapeHtml(JSON.stringify(provider));
                return `<div class="drug-option">
                    <div>
                        <div class="drug-option-name">${escapeHtml(provider.display_name)}</div>
                        <div class="drug-option-meta">${escapeHtml(provider.specialty || 'Facility')}</div>
                        <div class="drug-option-meta">${escapeHtml(provider.address || provider.location || '')}</div>
                    </div>
                    <button type="button" class="drug-option-add" data-provider-index="${index}" data-provider="${payload}">Add</button>
                </div>`;
            }).join('');
        }

        function openPrescriptionModal() {
            prescriptionModal.hidden = false;
            renderSelectedPrescriptions();
            window.setTimeout(function() {
                drugSearchInput.focus();
            }, 0);
        }

        function closePrescriptionModal() {
            prescriptionModal.hidden = true;
        }

        function setSelectedPrescriptionsFromText(value) {
            selectedPrescriptions = String(value || '')
                .split(',')
                .map(item => item.trim())
                .filter(Boolean)
                .map(item => ({
                    display_name: item,
                    prescription: item,
                    selection_type: 'text',
                }));
            syncPrescriptionSelectionInputs();
        }

        function syncPrescriptionSelectionInputs() {
            prescriptionInput.value = selectedPrescriptions.map(prescriptionDisplayName).join(', ');
            prescriptionSelectionsInput.value = JSON.stringify(
                selectedPrescriptions.filter(item => item.rxcui)
            );
            renderSelectedPrescriptions();
        }

        function prescriptionDisplayName(item) {
            return item.display_name || item.drug_name || item.prescription || item.name || '';
        }

        function prescriptionMeta(item) {
            const matches = item.formulary_matches || [];
            const matchLabel = matches.length
                ? 'In formulary: ' + matches.map(match => match.carrier).filter(Boolean).join(', ')
                : '';
            if (item.selection_type === 'text') {
                return ['Broad name', matchLabel].filter(Boolean).join(' · ');
            }
            return [item.coverage_type, item.dose_form, item.rxcui ? `RxCUI ${item.rxcui}` : '', matchLabel]
                .filter(Boolean)
                .join(' · ');
        }

        function renderSelectedPrescriptions() {
            const html = selectedPrescriptions.length
                ? selectedPrescriptions.map((item, index) => {
                    return `<div class="selected-rx-chip">
                        <div>
                            <div class="selected-rx-name">${escapeHtml(prescriptionDisplayName(item))}</div>
                            <div class="selected-rx-meta">${escapeHtml(prescriptionMeta(item))}</div>
                        </div>
                        <button type="button" class="selected-rx-remove" data-remove-rx="${index}" aria-label="Remove ${escapeHtml(prescriptionDisplayName(item))}">&times;</button>
                    </div>`;
                }).join('')
                : '<div class="selected-rx-empty">No prescriptions selected yet.</div>';
            selectedPrescriptionsDiv.innerHTML = html;
            modalSelectedPrescriptionsDiv.innerHTML = html;
        }

        function removePrescriptionFromClick(e) {
            const button = e.target.closest('[data-remove-rx]');
            if (!button) {
                return;
            }
            selectedPrescriptions.splice(Number(button.dataset.removeRx), 1);
            syncPrescriptionSelectionInputs();
        }

        function addPrescriptionSelection(drug) {
            if (drug.rxcui && selectedPrescriptions.some(item => item.rxcui === drug.rxcui)) {
                return;
            }
            const prescriptionName = prescriptionDisplayName(drug).toLowerCase();
            if (!drug.rxcui && selectedPrescriptions.some(item => prescriptionDisplayName(item).toLowerCase() === prescriptionName)) {
                return;
            }
            selectedPrescriptions.push(drug);
            syncPrescriptionSelectionInputs();
        }

        function selectedCarrierValues() {
            return carrierInputs
                .filter(input => input.checked)
                .map(input => input.value);
        }

        async function searchDrugOptions(query) {
            const sequence = ++drugSearchSequence;
            if (drugSearchRequest) {
                drugSearchRequest.abort();
                drugSearchRequest = null;
            }
            if (query.length < 2) {
                renderDrugSearchResults([]);
                return;
            }
            drugSearchRequest = new AbortController();
            drugSearchResults.innerHTML = '<div class="selected-rx-empty">Searching checked carrier formularies...</div>';
            try {
                const params = new URLSearchParams({
                    q: query,
                    formulary_only: 'true',
                    location: document.getElementById('location').value || 'dallas',
                });
                selectedCarrierValues().forEach(carrier => params.append('carriers', carrier));
                const response = await fetch('/drugs/search?' + params.toString(), {
                    signal: drugSearchRequest.signal,
                });
                const data = await response.json();
                if (sequence !== drugSearchSequence) {
                    return;
                }
                renderDrugSearchResults(data.drugs || [], data.message || '');
            } catch (error) {
                if (error.name === 'AbortError') {
                    return;
                }
                drugSearchResults.innerHTML = '<div class="selected-rx-empty">Drug lookup failed. Try again.</div>';
            } finally {
                if (sequence === drugSearchSequence) {
                    drugSearchRequest = null;
                }
            }
        }

        function renderDrugSearchResults(drugs, emptyMessage) {
            if (!drugs.length) {
                const query = drugSearchInput.value.trim();
                if (query.length >= 2) {
                    const safeQuery = escapeHtml(query);
                    drugSearchResults.innerHTML = `<div class="drug-option">
                        <div>
                            <div class="drug-option-name">${safeQuery}</div>
                            <div class="drug-option-meta">${escapeHtml(emptyMessage || 'No checked carrier formulary match found')}</div>
                        </div>
                        <button type="button" class="drug-option-add" data-add-rx-text="${safeQuery}">Add anyway</button>
                    </div>`;
                    return;
                }
                drugSearchResults.innerHTML = '<div class="selected-rx-empty">Search for a generic or brand name.</div>';
                return;
            }
            drugSearchResults.innerHTML = drugs.map((drug, index) => {
                const payload = escapeHtml(JSON.stringify(drug));
                const matchChips = (drug.formulary_matches || []).map(match => {
                    return `<span class="drug-formulary-chip">${escapeHtml(match.carrier)}</span>`;
                }).join('');
                return `<div class="drug-option">
                    <div>
                        <div class="drug-option-name">${escapeHtml(drug.display_name)}</div>
                        <div class="drug-option-meta">${escapeHtml(drug.coverage_type)} / ${escapeHtml(drug.dose_form || drug.route || 'Drug')}</div>
                        ${matchChips ? `<div class="drug-formulary-chips">${matchChips}</div>` : ''}
                    </div>
                    <button type="button" class="drug-option-add" data-drug-index="${index}" data-drug="${payload}">Add</button>
                </div>`;
            }).join('');
        }

        function formatProviderType(providerType) {
            if (providerType === 'not_found') {
                return 'Not found';
            }
            return providerType.charAt(0).toUpperCase() + providerType.slice(1);
        }

        function providerTagClass(providerType) {
            return providerType === 'not_found' ? 'not-found' : providerType;
        }

        function escapeHtml(value) {
            return String(value).replace(/[&<>"']/g, function(char) {
                return {
                    '&': '&amp;',
                    '<': '&lt;',
                    '>': '&gt;',
                    '"': '&quot;',
                    "'": '&#39;',
                }[char];
            });
        }

        function tooltipAttribute(value) {
            if (!value) {
                return '';
            }
            return ` data-tooltip="${escapeHtml(value)}"`;
        }

        function dataAttribute(name, value) {
            if (!value) {
                return '';
            }
            return ` ${name}="${escapeHtml(value)}"`;
        }

        function renderInfoTip(label, tooltip, className) {
            if (!tooltip) {
                return '';
            }
            const classes = className ? `info-tip ${className}` : 'info-tip';
            return `<span class="${classes}" tabindex="0" aria-label="${escapeHtml(label)}"${tooltipAttribute(tooltip)}>i</span>`;
        }

        function closeStatusPopover() {
            if (!activeStatusPopover) {
                return;
            }
            activeStatusPopover.remove();
            activeStatusPopover = null;
        }

        function showStatusPopover(cell) {
            const data = JSON.parse(cell.dataset.statusDetail || '{}');
            closeStatusPopover();
            activeStatusPopover = document.createElement('div');
            activeStatusPopover.className = 'status-popover';
            activeStatusPopover.setAttribute('role', 'dialog');
            activeStatusPopover.innerHTML = renderStatusPopover(data);
            document.body.appendChild(activeStatusPopover);
            activeStatusPopover.querySelector('[data-close-status-popover]').addEventListener('click', closeStatusPopover);

            const cellRect = cell.getBoundingClientRect();
            const popoverRect = activeStatusPopover.getBoundingClientRect();
            const left = Math.min(
                Math.max(16, cellRect.left),
                window.innerWidth - popoverRect.width - 16
            );
            const topBelow = cellRect.bottom + 8;
            const topAbove = cellRect.top - popoverRect.height - 8;
            activeStatusPopover.style.left = `${left}px`;
            activeStatusPopover.style.top = `${topBelow + popoverRect.height < window.innerHeight ? topBelow : Math.max(16, topAbove)}px`;
        }

        function renderStatusPopover(data) {
            let html = '<div class="status-popover-title">';
            html += `<span class="matrix-status-icon ${escapeHtml(data.className || '')}">${escapeHtml(data.icon || '·')}</span>`;
            html += `<span>${escapeHtml(data.label || 'Status')}</span>`;
            html += '<button type="button" class="status-popover-close" data-close-status-popover aria-label="Close status details">&times;</button>';
            html += '</div>';
            for (const row of data.rows || []) {
                html += '<div class="status-popover-row">';
                html += `<div class="status-popover-label">${escapeHtml(row.label)}</div>`;
                html += `<div class="status-popover-value${row.mono ? ' mono' : ''}">${escapeHtml(row.value)}</div>`;
                html += '</div>';
            }
            return html;
        }

        function clearColumnHover() {
            resultsDiv.querySelectorAll('.column-hover').forEach(function(element) {
                element.classList.remove('column-hover');
            });
        }

        function setColumnHover(cell) {
            const table = cell.closest('.network-matrix');
            if (!table) {
                return;
            }
            clearColumnHover();
            if (cell.dataset.columnIndex === '0') {
                return;
            }
            table.querySelectorAll(`[data-column-index="${cell.dataset.columnIndex}"]`).forEach(function(element) {
                element.classList.add('column-hover');
            });
        }

        function formatFreshnessTime(value) {
            if (!value) {
                return 'Never checked';
            }
            const date = new Date(value);
            if (Number.isNaN(date.getTime())) {
                return value;
            }
            return date.toLocaleString([], {
                month: 'short',
                day: 'numeric',
                hour: 'numeric',
                minute: '2-digit',
            });
        }

        function renderSourceStatus(statusInfo) {
            const status = statusInfo || { status: 'never_checked', counts: {} };
            const counts = status.counts || {};
            const pufUpdates = status.puf && status.puf.updates ? Object.entries(status.puf.updates) : [];
            const pufCopy = pufUpdates.length
                ? ` CMS PUF dates: ${pufUpdates.map(([name, date]) => `${name} ${date}`).join('; ')}.`
                : '';
            const changedCopy = `${counts.changed || 0} changed, ${counts.suspect || 0} suspect, ${counts.missing || 0} missing.`;

            let html = '<div class="source-freshness" aria-label="Last updated">';
            html += '<div>';
            html += '<div class="source-freshness-title">Last updated</div>';
            html += `<div class="source-freshness-copy">${escapeHtml(formatFreshnessTime(status.last_checked))}. ${counts.total || 0} sources watched; ${escapeHtml(changedCopy)}${escapeHtml(pufCopy)}</div>`;
            html += '</div>';
            html += '</div>';
            sourceStatusDiv.innerHTML = html;
        }

        async function loadSourceStatus() {
            try {
                const response = await fetch('/sources/status');
                const status = await response.json();
                renderSourceStatus(status);
            } catch (error) {
                renderSourceStatus({
                    status: 'suspect',
                    counts: {},
                    last_checked: '',
                    in_progress: false,
                });
            }
        }

        function networkStatusClass(status) {
            return {
                in: 'in',
                out: 'out',
                drug_covered: 'in',
                generic_covered: 'generic-covered',
                review_exact_drug: 'review-exact-drug',
                other_form_covered: 'other-form-covered',
                related_product_covered: 'related-product-covered',
                likely_in: 'likely-in',
                likely_covered: 'likely-covered',
                partial_coverage: 'partial-coverage',
                partial_match: 'partial-coverage',
                drug_not_covered: 'out',
                data_not_provided: 'data-not-provided',
                suspect: 'suspect',
                not_found: 'not-found',
                no_record: 'no-record',
                lookup_error: 'lookup-error',
                not_offered: 'not-offered',
                not_configured: 'not-configured',
                no_result: 'no-result',
                unknown: 'unknown',
            }[status] || 'no-result';
        }

        function matrixStatusClass(status) {
            return networkStatusClass(status).replaceAll('_', '-');
        }

        function networkStatusIcon(status) {
            if (status === 'in') {
                return '&check;';
            }
            if (status === 'drug_covered' || status === 'generic_covered') {
                return '&check;';
            }
            if (status === 'review_exact_drug' || status === 'other_form_covered' || status === 'related_product_covered') {
                return '!';
            }
            if (status === 'likely_in' || status === 'likely_covered') {
                return '&check;';
            }
            if (status === 'partial_coverage' || status === 'partial_match') {
                return '!';
            }
            if (status === 'out' || status === 'drug_not_covered') {
                return '&times;';
            }
            if (status === 'not_found' || status === 'data_not_provided' || status === 'suspect') {
                return '!';
            }
            if (status === 'lookup_error' || status === 'no_record') {
                return '!';
            }
            if (status === 'not_offered' || status === 'not_configured' || status === 'no_result') {
                return '';
            }
            return '?';
        }

        function networkStatusLabel(status) {
            if (status === 'in') {
                return 'Covered';
            }
            if (status === 'out') {
                return 'Not covered';
            }
            if (status === 'drug_covered') {
                return 'Covered';
            }
            if (status === 'generic_covered') {
                return 'Generic covered';
            }
            if (status === 'review_exact_drug') {
                return 'Review exact drug';
            }
            if (status === 'other_form_covered') {
                return 'Other form covered';
            }
            if (status === 'related_product_covered') {
                return 'Related product covered';
            }
            if (status === 'likely_in') {
                return 'Likely';
            }
            if (status === 'likely_covered') {
                return 'Likely';
            }
            if (status === 'partial_coverage' || status === 'partial_match') {
                return 'Partial coverage';
            }
            if (status === 'drug_not_covered') {
                return 'Not covered';
            }
            if (status === 'data_not_provided') {
                return 'No data';
            }
            if (status === 'suspect') {
                return 'Suspect';
            }
            if (status === 'not_found') {
                return 'Not found';
            }
            if (status === 'no_record') {
                return 'No record';
            }
            if (status === 'lookup_error') {
                return 'Error';
            }
            if (status === 'not_offered') {
                return 'Not offered';
            }
            if (status === 'not_configured') {
                return 'No lookup';
            }
            if (status === 'no_result') {
                return '-';
            }
            return '-';
        }

        function renderNetworkStatus(statusInfo) {
            const info = statusInfo || { status: 'no_result', detail: 'No lookup result', source: 'No result' };
            const status = info.status || 'no_result';

            let html = `<div class="network-status ${networkStatusClass(status)}">`;
            const icon = networkStatusIcon(status);
            if (icon) {
                html += `<span class="network-status-icon">${icon}</span>`;
            }
            html += `<span class="network-status-label">${networkStatusLabel(status)}</span>`;
            html += '</div>';
            return html;
        }

        function renderMatrixStatus(statusInfo) {
            const info = statusInfo || { status: 'no_result', detail: 'No lookup result', source: 'No result' };
            const status = info.status || 'no_result';
            const label = networkStatusLabel(status);
            const source = (info.source || '').toLowerCase().includes('cms') ? 'CMS' : '';
            const detail = matrixStatusDetail(info);
            let html = `<div class="matrix-status-card ${matrixStatusClass(status)}">`;
            html += '<div class="matrix-status-top">';
            html += `<span class="matrix-status-icon">${networkStatusIcon(status) || '&middot;'}</span>`;
            html += `<span class="matrix-status-label">${escapeHtml(label)}</span>`;
            if (source) {
                html += `<span class="matrix-source">${source}</span>`;
            }
            html += '</div>';
            if (detail) {
                html += `<div class="matrix-status-detail">${escapeHtml(detail)}</div>`;
            }
            html += '</div>';
            return html;
        }

        function matrixStatusDetail(statusInfo) {
            const info = statusInfo || {};
            const status = info.status || 'no_result';
            if (info.tier_label) {
                return [info.tier_label, info.restriction_label].filter(Boolean).join(' ');
            }
            if (status === 'drug_covered' || status === 'generic_covered') {
                return 'Tier not returned';
            }
            if (status === 'generic_covered') {
                return 'generic';
            }
            if (status === 'review_exact_drug') {
                return 'verify exact';
            }
            if (status === 'other_form_covered') {
                return 'other form';
            }
            if (status === 'related_product_covered') {
                return 'related';
            }
            if (status === 'partial_coverage' || status === 'partial_match') {
                return 'partial';
            }
            if (status === 'suspect') {
                return 'verify';
            }
            return '';
        }

        function networkStatusPopoverData(statusInfo) {
            const info = statusInfo || { status: 'no_result', detail: 'No lookup result', source: 'No result' };
            const status = info.status || 'no_result';
            const detail = info.detail || '';
            const source = info.source || '';
            const reason = networkStatusReason(status, detail);
            const rows = [];
            if (reason) {
                rows.push({ label: 'Meaning', value: reason });
            }
            if (source) {
                rows.push({ label: 'Source', value: source, mono: true });
            }
            if (info.tier_detail) {
                rows.push({ label: 'Tier', value: [info.tier_detail, info.restriction_label].filter(Boolean).join(' · '), mono: true });
            }
            for (const tier of info.formulary_tiers || []) {
                if (tier.formulary_name && tier.tier_detail) {
                    rows.push({
                        label: tier.formulary_name,
                        value: [tier.tier_detail, tier.restriction_label].filter(Boolean).join(' · '),
                        mono: true,
                    });
                }
            }
            if (detail && detail !== reason) {
                rows.push({ label: 'Detail', value: detail });
            }
            return {
                className: matrixStatusClass(status),
                icon: networkStatusIcon(status) || '·',
                label: networkStatusLabel(status),
                rows,
            };
        }

        function networkStatusReason(status, detail) {
            if (status === 'suspect') {
                if (detail.includes('Selected RxCUI')) {
                    return 'Selected RxCUI not covered; exact drug may differ';
                }
                return 'CMS data needs direct confirmation';
            }
            if (status === 'review_exact_drug') {
                return 'Coverage evidence found; confirm exact drug/form';
            }
            if (status === 'other_form_covered') {
                return 'A different matched strength or form is covered';
            }
            if (status === 'related_product_covered') {
                return 'A related combination product is covered';
            }
            if (status === 'likely_covered') {
                return 'Coverage found; confirm exact RxCUI';
            }
            if (status === 'likely_in') {
                return 'Coverage found; confirm exact NPI/location';
            }
            if (status === 'partial_coverage') {
                return 'Only some matched plan IDs covered';
            }
            if (status === 'not_offered') {
                return 'No matching plans in this location';
            }
            if (status === 'no_record') {
                return 'No CMS coverage row found';
            }
            if (status === 'lookup_error') {
                return 'CMS lookup failed';
            }
            return '';
        }

        function statusFamily(status) {
            if (status === 'in' || status === 'drug_covered' || status === 'generic_covered') {
                return 'positive';
            }
            if (status === 'likely_in' || status === 'likely_covered' || status === 'review_exact_drug' || status === 'other_form_covered' || status === 'related_product_covered' || status === 'partial_coverage' || status === 'partial_match') {
                return 'caution';
            }
            if (status === 'out' || status === 'drug_not_covered' || status === 'not_found' || status === 'suspect') {
                return 'negative';
            }
            if (status === 'not_offered' || status === 'not_configured' || status === 'no_result') {
                return 'na';
            }
            return 'unknown';
        }

        function allLookupItems(providerResults, prescriptionResults) {
            return []
                .concat((providerResults || []).map(function(result) {
                    return {
                        type: 'provider',
                        name: result.provider,
                        statuses: result.network_statuses || {},
                    };
                }))
                .concat((prescriptionResults || []).map(function(result) {
                    return {
                        type: 'rx',
                        name: result.prescription,
                        statuses: result.network_statuses || {},
                    };
                }));
        }

        function summarizeCarrier(network, items) {
            let positive = 0;
            let caution = 0;
            let negative = 0;
            let unknown = 0;
            let considered = 0;
            const blockers = [];
            const caveats = [];

            for (const item of items) {
                const info = item.statuses[network.id] || { status: 'no_result' };
                const status = info.status || 'no_result';
                const family = statusFamily(status);
                if (family === 'na') {
                    continue;
                }
                considered += 1;
                if (family === 'positive') {
                    positive += 1;
                } else if (family === 'caution') {
                    caution += 1;
                    caveats.push(`${item.name}: ${networkStatusLabel(status)}`);
                } else if (family === 'negative') {
                    negative += 1;
                    blockers.push(`${item.name}: ${networkStatusLabel(status)}`);
                } else {
                    unknown += 1;
                    caveats.push(`${item.name}: ${networkStatusLabel(status)}`);
                }
            }

            return {
                network: network,
                positive: positive,
                caution: caution,
                negative: negative,
                unknown: unknown,
                considered: considered,
                blockers: blockers,
                caveats: caveats,
                score: positive * 2 + caution - negative * 3 - unknown,
            };
        }

        function compactText(value, maxLength) {
            const text = String(value || '');
            if (text.length <= maxLength) {
                return text;
            }
            return text.slice(0, maxLength - 1) + '...';
        }

        function providerMatchTooltip(result) {
            if (!result.npi_found) {
                return 'No NPI Registry matches found. Try a different spelling or city.';
            }
            const matches = (result.npi_results || []).slice(0, 5).map(function(npi, index) {
                const parts = [
                    npi.name,
                    npi.npi ? `NPI ${npi.npi}` : '',
                    npi.specialty,
                    npi.location,
                    npi.phone,
                ].filter(Boolean);
                return `${index + 1}. ${parts.join(' | ')}`;
            });
            const extra = result.npi_count > matches.length ? ` ${result.npi_count - matches.length} more not shown.` : '';
            return `${result.npi_count} NPI match${result.npi_count === 1 ? '' : 'es'}. ${matches.join(' ')}${extra}`;
        }

        function providerMatchSummary(result) {
            const label = result.npi_found ? `${result.npi_count} NPI match${result.npi_count === 1 ? '' : 'es'}` : 'No NPI match';
            return escapeHtml(label);
        }

        function drugMatchName(drug) {
            return [drug.name, drug.strength, drug.route].filter(Boolean).join(' ') || drug.full_name || drug.rxcui || 'Drug match';
        }

        function prescriptionMatchTooltip(result) {
            if (!result.drug_found) {
                return 'No CMS RxCUI match found for this prescription name.';
            }
            const matches = (result.drug_results || []).slice(0, 5).map(function(drug, index) {
                return `${index + 1}. ${drugMatchName(drug)}${drug.rxcui ? ' | RxCUI ' + drug.rxcui : ''}`;
            });
            const count = result.drug_match_count || 0;
            const selected = `${result.drug_name || result.prescription} | RxCUI ${result.rxcui}`;
            const extra = count > matches.length ? ` ${count - matches.length} more not shown.` : '';
            return `Selected ${selected}; selected from ${count} RxCUI match${count === 1 ? '' : 'es'}. ${matches.join(' ')}${extra}`;
        }

        function prescriptionMatchSummary(result) {
            if (!result.drug_found) {
                return 'No RxCUI match';
            }
            const matchCount = result.drug_match_count || 0;
            const selected = compactText(`${result.drug_name || result.prescription} | RxCUI ${result.rxcui}`, 68);
            const selectedLine = escapeHtml(selected + (matchCount > 1 ? ` | ${matchCount} CMS matches` : ''));
            const matchNames = (result.drug_results || [])
                .slice(0, 4)
                .map(drugMatchName)
                .filter(Boolean);
            if (matchCount <= 1 || !matchNames.length) {
                return selectedLine;
            }
            return `${selectedLine}<br>${escapeHtml('Top matches: ' + matchNames.join('; '))}`;
        }

        function collectNetworks(results) {
            const networks = [];
            const seen = new Set();

            for (const result of results || []) {
                for (const network of result.networks || []) {
                    if (seen.has(network.id)) {
                        continue;
                    }
                    seen.add(network.id);
                    networks.push(network);
                }
            }

            return networks;
        }

        function renderLookupMatrix(providerResults, prescriptionResults, networks) {
            if (!providerResults.length && !prescriptionResults.length) {
                return '';
            }

            const items = allLookupItems(providerResults, prescriptionResults);
            let html = '<div class="network-matrix-wrap">';
            html += '<div class="network-matrix-title">Evidence · coverage matrix <span class="matrix-title-copy">every provider × every plan, with source authority</span><span class="matrix-actions"><span>Hide not offered</span><span>Side-by-side ↗</span></span></div>';
            html += '<div class="network-matrix-scroll"><table class="network-matrix">';
            html += '<thead><tr><th data-column-index="0" aria-label="Lookup item"><span class="plan-arrow">Plan →</span></th>';
            for (const [index, network] of networks.entries()) {
                html += renderCarrierHeader(network, summarizeCarrier(network, items), index + 1);
            }
            html += '<th class="row-total-header" data-column-index="total"><span class="plan-arrow">Row total</span></th>';
            html += '</tr></thead><tbody>';

            const doctorResults = providerResults.filter(result => providerGroup(result) === 'doctor');
            const facilityResults = providerResults.filter(result => providerGroup(result) === 'facility');

            if (doctorResults.length) {
                html += renderLookupSection(
                    'Doctors',
                    'Coverage by individual NPI match against carrier rosters',
                    networks.length + 2,
                    doctorResults.length
                );
                for (const result of doctorResults) {
                    html += renderProviderLookupRow(result, networks);
                }
            }

            if (facilityResults.length) {
                html += renderLookupSection(
                    'Facilities',
                    'Coverage by organization NPI match against carrier rosters',
                    networks.length + 2,
                    facilityResults.length
                );
                for (const result of facilityResults) {
                    html += renderProviderLookupRow(result, networks);
                }
            }

            if (prescriptionResults.length) {
                html += renderLookupSection(
                    'Prescriptions',
                    'Coverage by RxCUI · CMS = screening, carrier formulary = final',
                    networks.length + 2,
                    prescriptionResults.length
                );
                for (const result of prescriptionResults) {
                    html += renderPrescriptionLookupRow(result, networks);
                }
            }

            html += '</tbody></table></div>';
            html += renderMatrixLegend();
            html += '</div>';
            return html;
        }

        function renderCarrierHeader(network, summary, columnIndex) {
            const issuer = network.carrier || network.marketplace_issuer || '';
            const planType = network.name.includes('HMO') ? 'HMO' : network.name.includes('EPO') || network.name.includes('My Blue') ? 'EPO' : 'Exchange';
            let html = `<th class="carrier-header" data-column-index="${columnIndex}">`;
            html += `<div class="carrier-name">${escapeHtml(network.name)}</div>`;
            html += `<div class="carrier-meta">${escapeHtml(issuer)} · ${escapeHtml(planType)}</div>`;
            if (!summary.considered) {
                html += '<div class="carrier-counts">not offered</div>';
            } else {
                html += `<div class="carrier-counts"><span class="carrier-covered">${summary.positive}</span> /${summary.considered} covered</div>`;
                const parts = [];
                if (summary.caution) {
                    parts.push(`<span class="carrier-caution">${summary.caution} caution</span>`);
                }
                if (summary.negative) {
                    parts.push(`<span class="carrier-blocking">${summary.negative} blocking</span>`);
                }
                if (parts.length) {
                    html += `<div class="carrier-counts">${parts.join(' · ')}</div>`;
                }
            }
            html += '</th>';
            return html;
        }

        function providerGroup(result) {
            if (result.provider_group === 'facility') {
                return 'facility';
            }
            if (result.provider_group === 'doctor') {
                return 'doctor';
            }
            return result.provider_type === 'facility' ? 'facility' : 'doctor';
        }

        function renderLookupSection(title, copy, colspan, count) {
            let html = `<tr class="lookup-section-row"><td colspan="${colspan}">`;
            html += `<div class="lookup-section-title">${escapeHtml(title)}</div>`;
            html += `<div class="lookup-section-copy">${escapeHtml(copy)}</div>`;
            html += `<span class="section-count">${count} item${count === 1 ? '' : 's'}</span>`;
            html += '</td></tr>';
            return html;
        }

        function renderProviderLookupRow(result, networks) {
            let html = '<tr class="lookup-row">';
            html += '<td class="lookup-item-cell" data-column-index="0">';
            html += '<div class="lookup-item-content">';
            html += renderInfoTip('NPI match details', providerMatchTooltip(result), 'item-info');
            html += '<div class="lookup-item-heading-row">';
            html += `<div class="lookup-item-heading">${escapeHtml(result.provider)}</div>`;
            html += `<span class="provider-tag item-tag ${providerTagClass(result.provider_type)}">${formatProviderType(result.provider_type)}</span>`;
            html += '</div>';
            html += `<div class="lookup-item-meta">${providerMatchSummary(result)}</div>`;
            html += '</div>';
            html += '</td>';
            html += renderStatusCells(result.network_statuses || {}, networks);
            html += renderRowTotal(result.network_statuses || {}, networks);
            html += '</tr>';
            return html;
        }

        function renderPrescriptionLookupRow(result, networks) {
            let html = '<tr class="lookup-row">';
            const tagClass = result.drug_found ? 'rx' : 'not-found';
            const tagLabel = result.drug_found ? 'Rx' : 'Not found';
            html += '<td class="lookup-item-cell" data-column-index="0">';
            html += '<div class="lookup-item-content">';
            html += renderInfoTip('RxCUI match details', prescriptionMatchTooltip(result), 'item-info');
            html += '<div class="lookup-item-heading-row">';
            html += `<div class="lookup-item-heading">${escapeHtml(result.prescription)}</div>`;
            html += `<span class="provider-tag item-tag ${tagClass}">${tagLabel}</span>`;
            html += '</div>';
            html += `<div class="lookup-item-meta">${prescriptionMatchSummary(result)}</div>`;
            html += '</div>';
            html += '</td>';
            html += renderStatusCells(result.network_statuses || {}, networks, 'prescription');
            html += renderRowTotal(result.network_statuses || {}, networks);
            html += '</tr>';
            return html;
        }

        function renderStatusCells(statuses, networks, itemType) {
            let html = '';
            for (const [index, network] of networks.entries()) {
                const statusInfo = statuses[network.id];
                const popoverData = JSON.stringify(networkStatusPopoverData(statusInfo));
                html += `<td class="network-cell" data-column-index="${index + 1}" tabindex="0"${dataAttribute('data-status-detail', popoverData)}>`;
                html += itemType === 'prescription' ? renderPrescriptionMatrixStatus(statusInfo) : renderMatrixStatus(statusInfo);
                html += '</td>';
            }
            return html;
        }

        function renderPrescriptionMatrixStatus(statusInfo) {
            const tiers = (statusInfo && statusInfo.formulary_tiers || []).filter(tier => tier && tier.formulary_name);
            if (tiers.length < 2) {
                return renderMatrixStatus(statusInfo);
            }
            const info = statusInfo || { status: 'no_result', detail: 'No lookup result', source: 'No result' };
            const status = info.status || 'no_result';
            const label = networkStatusLabel(status);
            const source = (info.source || '').toLowerCase().includes('cms') ? 'CMS' : '';
            let html = `<div class="matrix-status-card formulary-split ${matrixStatusClass(status)}">`;
            html += '<div class="matrix-status-top">';
            html += `<span class="matrix-status-icon">${networkStatusIcon(status) || '&middot;'}</span>`;
            html += `<span class="matrix-status-label">${escapeHtml(label)}</span>`;
            if (source) {
                html += `<span class="matrix-source">${source}</span>`;
            }
            html += '</div>';
            html += '<div class="formulary-tier-grid">';
            for (const tier of tiers) {
                html += '<div class="formulary-tier-column">';
                html += `<div class="formulary-tier-name">${escapeHtml(tier.formulary_name)}</div>`;
                html += `<div class="formulary-tier-value">${escapeHtml(formularyTierDetail(tier))}</div>`;
                html += '</div>';
            }
            html += '</div>';
            html += '</div>';
            return html;
        }

        function formularyTierDetail(tier) {
            if (tier.tier_label) {
                return [tier.tier_label, tier.restriction_label].filter(Boolean).join(' ');
            }
            return tier.tier_detail || 'Tier not found';
        }

        function renderRowTotal(statuses, networks) {
            let positive = 0;
            let caution = 0;
            let considered = 0;
            for (const network of networks) {
                const info = statuses[network.id] || { status: 'no_result' };
                const family = statusFamily(info.status || 'no_result');
                if (family === 'na') {
                    continue;
                }
                considered += 1;
                if (family === 'positive') {
                    positive += 1;
                } else if (family === 'caution' || family === 'unknown') {
                    caution += 1;
                }
            }
            let html = '<td class="row-total-cell">';
            html += `<div class="row-total"><span class="row-total-covered">${positive}</span> /${considered} plans`;
            if (caution) {
                html += `<br><span class="row-total-caution">+${caution} caution</span>`;
            }
            html += '</div></td>';
            return html;
        }

        function renderMatrixLegend() {
            const statuses = [
                ['drug_covered', 'Covered'],
                ['generic_covered', 'Generic covered'],
                ['review_exact_drug', 'Review exact drug'],
                ['other_form_covered', 'Other form covered'],
                ['related_product_covered', 'Related product covered'],
                ['drug_not_covered', 'Not covered'],
                ['suspect', 'Suspect'],
                ['data_not_provided', 'No data'],
                ['not_offered', 'Not offered'],
            ];
            let html = '<div class="matrix-legend"><span class="matrix-legend-title">Legend</span>';
            for (const [status, label] of statuses) {
                html += `<span class="legend-status" tabindex="0"${tooltipAttribute(legendStatusTooltip(status))}><span class="matrix-status-icon ${matrixStatusClass(status)}">${networkStatusIcon(status) || '&middot;'}</span>${escapeHtml(label)}</span>`;
            }
            html += `<span class="legend-status" tabindex="0"${tooltipAttribute('CMS is screening evidence. Carrier portal or formulary should be treated as final when quoting a client.')}><strong>CMS</strong> = screening · <strong>Carrier</strong> = final</span>`;
            html += '</div>';
            return html;
        }

        function legendStatusTooltip(status) {
            return {
                drug_covered: 'Covered for this plan in the screening data. Confirm exact provider, drug, tier, and rules before quoting.',
                generic_covered: 'The generic is covered. Do not assume the brand is covered unless the carrier confirms it.',
                review_exact_drug: 'Coverage evidence exists, but the input may be broad. Confirm the exact drug, strength, form, and tier.',
                other_form_covered: 'A different strength or form is covered. Confirm whether the client can use that version.',
                related_product_covered: 'A related or combination product is covered. Confirm the exact prescribed product before quoting.',
                drug_not_covered: 'The plan is available, but this provider or drug was not covered in the checked data.',
                suspect: 'The data needs manual verification before quoting coverage.',
                data_not_provided: 'The lookup did not have enough source data. Verify manually.',
                not_offered: 'This plan is not available for the selected service area, so it should not be treated as a client option.',
            }[status] || '';
        }

        function renderProviderDetails(results) {
            if (!results.length) {
                return '';
            }

            let html = '<details class="provider-details">';
            html += '<summary>Provider NPI matches and carrier directory links</summary>';
            html += '<div class="results">';

            for (const result of results) {
                html += '<div class="result-card">';
                html += `<div class="doctor-name">${result.provider}`;
                html += ` <span class="provider-tag ${providerTagClass(result.provider_type)}">${formatProviderType(result.provider_type)}</span>`;
                if (result.specialty_filter) {
                    html += ` <span style="color: #6b7280; font-weight: normal;">[${result.specialty_filter}]</span>`;
                }
                if (result.npi_found) {
                    html += `<span class="badge badge-success">${result.npi_count} found</span>`;
                } else {
                    html += '<span class="badge badge-error">Not found</span>';
                }
                html += '</div>';

                html += '<div class="npi-results">';
                if (result.npi_found) {
                    for (const npi of result.npi_results) {
                        const credential = npi.credential ? `, ${npi.credential}` : '';
                        html += '<div class="npi-item">';
                        html += `<div class="npi-name">${npi.name}${credential}</div>`;
                        html += `<div class="npi-detail">NPI: ${npi.npi} | ${npi.specialty}</div>`;
                        html += `<div class="npi-detail">${npi.location}${npi.phone ? ' | ' + npi.phone : ''}</div>`;
                        html += '</div>';
                    }
                } else {
                    html += '<div class="npi-item not-found">';
                    html += '<div class="npi-name">No matches in NPI Registry</div>';
                    html += '<div class="npi-detail">Try different spelling or remove specialty/type filter</div>';
                    html += '</div>';
                }
                html += '</div>';

                html += '<div class="network-section">';
                html += '<div class="network-label">BCBSTX:</div>';
                html += '<div class="network-links">';
                for (const [key, urlInfo] of Object.entries(result.bcbstx_urls)) {
                    html += `<a href="${urlInfo.url}" target="_blank" class="network-link bcbstx">${urlInfo.name} &rarr;</a>`;
                }
                html += '</div></div>';

                html += '<div class="network-section">';
                html += '<div class="network-label">UHC:</div>';
                html += '<div class="network-links">';
                for (const [key, urlInfo] of Object.entries(result.uhc_urls)) {
                    html += `<a href="${urlInfo.url}" target="_blank" class="network-link uhc">${urlInfo.name} &rarr;</a>`;
                }
                html += '</div></div>';

                html += '</div>';
            }

            html += '</div></details>';
            return html;
        }

        function renderCarrierSourceDetails(sourceGroups) {
            if (!sourceGroups.length) {
                return '';
            }

            let html = '<details class="source-details">';
            html += '<summary>Carrier source map and open questions</summary>';
            for (const group of sourceGroups) {
                html += '<div class="source-group">';
                html += `<div class="source-carrier">${escapeHtml(group.carrier)}</div>`;
                for (const source of group.sources || []) {
                    html += '<div class="source-row">';
                    html += `<a class="source-link" href="${escapeHtml(source.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(source.name)}</a>`;
                    html += `<div class="source-meta">${escapeHtml(source.kind)} | ${escapeHtml(source.year)} | ${escapeHtml(source.identifier)} | Checked ${escapeHtml(source.checked_on)} | ${escapeHtml(source.validation)}</div>`;
                    if (source.description) {
                        html += `<div class="source-description">${escapeHtml(source.description)}</div>`;
                    }
                    html += `<div class="source-note">${escapeHtml(source.role)}</div>`;
                    if (source.note) {
                        html += `<div class="source-note">${escapeHtml(source.note)}</div>`;
                    }
                    html += '</div>';
                }
                for (const question of group.open_questions || []) {
                    html += `<div class="source-question">Open question: ${escapeHtml(question)}</div>`;
                }
                html += '</div>';
            }
            html += '</details>';
            return html;
        }
    </script>

    <footer class="footer">
        <a href="https://sommer.computer" target="_blank" rel="noopener noreferrer">sommer.computer</a>
    </footer>
</body>
</html>
"""


def normalize_provider_type(provider_type):
    if provider_type in {"doctor", "facility"}:
        return provider_type
    return "auto"


FACILITY_HINTS = {
    "baylor",
    "center",
    "clinic",
    "facility",
    "health",
    "hospital",
    "medical",
    "methodist",
    "scott",
    "university",
    "white",
}


def provider_query_has_facility_hint(name):
    words = set(normalize_search_text(name).split())
    return bool(words & FACILITY_HINTS)


def unique_search_queries(queries):
    unique = []
    seen = set()
    for query in queries:
        query = " ".join(str(query).replace("&", " ").split())
        if not query:
            continue
        key = normalize_search_text(query)
        if key in seen:
            continue
        seen.add(key)
        unique.append(query)
    return unique


def facility_search_queries(name):
    normalized = normalize_search_text(name)
    corrected = normalized.replace("bailer", "baylor")
    queries = [name]
    if corrected != normalized:
        queries.append(corrected)
    if "scott white" in corrected or "baylor scott" in corrected:
        queries.extend([
            "Baylor Scott White",
            "Baylor Scott and White",
            "Baylor University Medical Center",
        ])
    return unique_search_queries(queries)


def provider_search_queries(name, provider_type):
    if provider_type == "facility":
        return facility_search_queries(name)
    return unique_search_queries([name])


def search_provider_npi(name, state="TX", city=None, specialty=None, limit=10, provider_type="doctor"):
    results = []
    seen_npis = set()
    for query in provider_search_queries(name, provider_type):
        query_results = search_npi(
            query, state=state, city=city, specialty=specialty,
            limit=limit, provider_type=provider_type
        )
        for result in query_results:
            npi = result.get("npi")
            if npi and npi in seen_npis:
                continue
            if npi:
                seen_npis.add(npi)
            results.append(result)
            if len(results) >= limit:
                return results
    return results


def serialize_provider_candidate(provider, provider_type):
    return {
        "npi": str(provider.get("npi", "")),
        "name": provider.get("name", ""),
        "display_name": provider.get("name", ""),
        "credential": provider.get("credential", ""),
        "specialty": provider.get("specialty", ""),
        "address": provider.get("address", ""),
        "location": provider.get("location", ""),
        "phone": provider.get("phone", ""),
        "provider_type": provider_type,
    }


def search_npi(name, state="TX", city=None, specialty=None, limit=10, provider_type="doctor"):
    """Search the NPI Registry for providers."""
    provider_type = "facility" if provider_type == "facility" else "doctor"

    params = {
        "version": "2.1",
        "limit": limit,
        "enumeration_type": "NPI-2" if provider_type == "facility" else "NPI-1",
        "state": state,
    }

    if provider_type == "facility":
        params["organization_name"] = name.strip()
    else:
        name_parts = name.replace("Dr.", "").replace("Dr", "").strip()

        if "," in name_parts:
            last, first = [p.strip() for p in name_parts.split(",", 1)]
        else:
            parts = name_parts.split()
            if len(parts) >= 2:
                first = parts[0]
                last = " ".join(parts[1:])
            else:
                first = ""
                last = name_parts

        if first:
            params["first_name"] = first
        if last:
            params["last_name"] = last
    if city:
        params["city"] = city

    try:
        resp = requests.get(NPI_API, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        results = []
        for r in data.get("results", []):
            basic = r.get("basic", {})
            addresses = r.get("addresses", [])
            taxonomies = r.get("taxonomies", [])

            practice_addr = next(
                (a for a in addresses if a.get("address_purpose") == "LOCATION"),
                addresses[0] if addresses else {},
            )

            primary_tax = next(
                (t for t in taxonomies if t.get("primary")),
                taxonomies[0] if taxonomies else {},
            )

            specialty_name = primary_tax.get("desc", "Unknown")

            if specialty and specialty.lower() not in specialty_name.lower():
                continue

            result_name = basic.get("organization_name", "").strip()
            if provider_type == "doctor":
                result_name = f"{basic.get('first_name', '')} {basic.get('last_name', '')}".strip()

            address_parts = [
                practice_addr.get("address_1", ""),
                practice_addr.get("address_2", ""),
            ]
            street_address = " ".join(part for part in address_parts if part).strip()
            location = f"{practice_addr.get('city', '')}, {practice_addr.get('state', '')} {practice_addr.get('postal_code', '')[:5]}"
            full_address = ", ".join(part for part in [street_address, location] if part.strip(" ,"))

            results.append({
                "npi": r.get("number", ""),
                "name": result_name,
                "credential": basic.get("credential", ""),
                "specialty": specialty_name,
                "address": full_address,
                "location": location,
                "phone": practice_addr.get("telephone_number", ""),
            })

        return results
    except Exception:
        return []


def resolve_provider_npi(name, state="TX", city=None, specialty=None, limit=10, provider_type="auto"):
    provider_type = normalize_provider_type(provider_type)

    if provider_type in {"doctor", "facility"}:
        results = search_provider_npi(
            name, state=state, city=city, specialty=specialty,
            limit=limit, provider_type=provider_type
        )
        return results, provider_type if results else "not_found"

    doctor_results = search_provider_npi(
        name, state=state, city=city, specialty=specialty,
        limit=limit, provider_type="doctor"
    )
    facility_results = search_provider_npi(
        name, state=state, city=city, specialty=specialty,
        limit=limit, provider_type="facility"
    )
    if facility_results and provider_query_has_facility_hint(name):
        return facility_results, "facility"

    if doctor_results:
        return doctor_results, "doctor"

    if facility_results:
        return facility_results, "facility"

    return [], "not_found"


def normalize_search_text(value):
    return " ".join(value.lower().split())


def format_drug_name(drug):
    parts = [
        drug.get("name", ""),
        drug.get("strength", ""),
        drug.get("route", ""),
    ]
    return " ".join(part for part in parts if part).strip()


def drug_display_name(drug):
    name = drug.get("name", "")
    dose_form = drug.get("rxnorm_dose_form") or drug.get("route", "")
    strength = drug.get("strength", "")
    if strength and dose_form:
        return f"{name} {strength} {dose_form}"
    return format_drug_name(drug) or drug.get("full_name", "") or drug.get("rxcui", "")


def drug_coverage_type(drug):
    full_name = drug.get("full_name", "")
    name = drug.get("name", "")
    if "[" in full_name or name.isupper():
        return "Branded"
    return "Generic"


def serialize_drug_candidate(drug):
    return {
        "rxcui": str(drug.get("rxcui", "")),
        "name": drug.get("name", ""),
        "strength": drug.get("strength", ""),
        "route": drug.get("route", ""),
        "full_name": drug.get("full_name", ""),
        "dose_form": drug.get("rxnorm_dose_form") or drug.get("route", ""),
        "display_name": drug_display_name(drug),
        "coverage_type": drug_coverage_type(drug),
    }


def drug_match_score(query, drug):
    query_text = normalize_search_text(query)
    name = normalize_search_text(drug.get("name", ""))
    full_name = normalize_search_text(drug.get("full_name", ""))
    route = normalize_search_text(drug.get("route", ""))
    is_exact_brand = name == query_text and drug.get("name", "").isupper()
    score = 0

    if name == query_text:
        score += 100
    elif name.startswith(query_text):
        score += 60
    elif query_text in name:
        score += 30
    if query_text in full_name:
        score += 10
    if "/" not in name:
        score += 15
    if "[" not in full_name:
        score += 5
    if not is_exact_brand:
        if "oral pill" in route or "oral tablet" in full_name:
            score += 8
        if "injectable" in route or "injection" in full_name:
            score -= 8

    return score


def drug_is_combination_match(query, drug):
    query_text = normalize_search_text(query)
    if "/" in query_text:
        return False
    name_text = normalize_search_text(drug.get("name", ""))
    full_name = drug.get("full_name", "")
    return "/" in name_text or " / " in full_name


def search_drugs(name, limit=10):
    """Search the CMS Marketplace drug endpoint for RxCUI matches."""
    try:
        response = requests.get(
            f"{CMS_MARKETPLACE_API}/drugs/autocomplete",
            params={"apikey": CMS_MARKETPLACE_API_KEY, "q": name},
            timeout=NETWORK_LOOKUP_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError):
        return []

    if isinstance(data, dict):
        drugs = data.get("drugs", [])
    else:
        drugs = data
    return drugs[:limit]


def resolve_prescription(name):
    drug_results = search_drugs(name)
    if not drug_results:
        return {
            "prescription": name,
            "drug_found": False,
            "drug_match_count": 0,
            "drug_results": [],
        }

    selected = max(drug_results, key=lambda drug: drug_match_score(name, drug))
    selected_is_combination = drug_is_combination_match(name, selected)
    drug_match_warning = ""
    if selected_is_combination:
        drug_match_warning = (
            f"CMS autocomplete selected a combination-product RxCUI for broad input '{name}'; "
            "standalone drug coverage needs direct formulary confirmation. "
        )
    return {
        "prescription": name,
        "drug_found": True,
        "drug_match_count": len(drug_results),
        "drug_results": drug_results[:5],
        "rxcui": selected.get("rxcui", ""),
        "drug_name": format_drug_name(selected),
        "drug_detail": selected.get("full_name", ""),
        "selected_is_combination": selected_is_combination,
        "drug_match_warning": drug_match_warning,
    }


def resolve_selected_prescription(selection):
    rxcui = str(selection.get("rxcui", "")).strip()
    if not rxcui:
        return resolve_prescription(selection.get("display_name") or selection.get("prescription") or "")

    selected = {
        "rxcui": rxcui,
        "name": selection.get("name", ""),
        "strength": selection.get("strength", ""),
        "route": selection.get("route", ""),
        "full_name": selection.get("full_name", ""),
        "rxnorm_dose_form": selection.get("dose_form", ""),
    }
    display_name = selection.get("display_name") or drug_display_name(selected)
    return {
        "prescription": display_name,
        "drug_found": True,
        "drug_match_count": 1,
        "drug_results": [selected],
        "rxcui": rxcui,
        "drug_name": format_drug_name(selected) or display_name,
        "drug_detail": selected.get("full_name", ""),
        "selected_is_combination": drug_is_combination_match(display_name, selected),
        "drug_match_warning": "",
        "selected_from_picker": True,
    }


def generate_bcbstx_urls(name, lat, lon, radius):
    """Generate BCBSTX provider finder URLs."""
    urls = {}
    for key, config in BCBSTX_SEARCH_URLS.items():
        urls[key] = {
            "name": config["name"],
            "url": config["url"].format(query=quote(name), lat=lat, lon=lon, radius=radius),
        }
    return urls


def generate_uhc_urls():
    """Generate UHC provider finder URLs (Rally/werally.com)."""
    urls = {}
    for key, config in UHC_SEARCH_URLS.items():
        urls[key] = {
            "name": config["name"],
            "url": config["url"],
        }
    return urls


def make_network_status(status, source, detail, **extra):
    result = {
        "status": status,
        "source": source,
        "detail": detail,
    }
    result.update(extra)
    return result


def plan_matches_marketplace_network(plan, network):
    name = plan.get("name", "").lower()
    network_url = plan.get("network_url", "").lower()

    for fragment in network.get("marketplace_plan_name_contains", []):
        if fragment.lower() not in name:
            return False

    url_fragment = network.get("marketplace_network_url_contains")
    if url_fragment and url_fragment.lower() not in network_url:
        return False

    return True


def chunked(items, size):
    for index in range(0, len(items), size):
        yield items[index:index + size]


@lru_cache(maxsize=128)
def get_marketplace_plan_ids(
    issuer,
    plan_year,
    zipcode,
    countyfips,
    state,
    plan_name_contains,
    network_url_contains,
):
    plan_ids = []
    offset = 0
    network = {
        "marketplace_plan_name_contains": plan_name_contains,
        "marketplace_network_url_contains": network_url_contains,
    }

    while True:
        payload = {
            "market": "Individual",
            "place": {
                "zipcode": zipcode,
                "countyfips": countyfips,
                "state": state,
            },
            "year": plan_year,
            "offset": offset,
            "filter": {"issuer": issuer},
        }

        response = requests.post(
            f"{CMS_MARKETPLACE_API}/plans/search",
            params={"apikey": CMS_MARKETPLACE_API_KEY},
            json=payload,
            timeout=NETWORK_LOOKUP_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        plans = data.get("plans", [])

        for plan in plans:
            if plan_matches_marketplace_network(plan, network):
                plan_ids.append(plan["id"])

        if len(plans) < 10:
            break
        offset += 10

    return tuple(plan_ids)


def get_network_plan_ids(network, place):
    return get_marketplace_plan_ids(
        network["marketplace_issuer"],
        network["plan_year"],
        place["zipcode"],
        place["countyfips"],
        place["state"],
        tuple(network.get("marketplace_plan_name_contains", [])),
        network.get("marketplace_network_url_contains", ""),
    )


def check_marketplace_network_status(provider_name, provider_type, npi_results, network, place):
    expected_npis = {
        str(result.get("npi"))
        for result in npi_results
        if result.get("npi")
    }
    if not expected_npis:
        return make_network_status(
            "not_found",
            "NPI Registry",
            "Provider was not found in the NPI Registry.",
        )

    try:
        plan_ids = get_network_plan_ids(network, place)
    except (requests.RequestException, ValueError) as error:
        return make_network_status(
            "lookup_error",
            "CMS Marketplace API",
            f"Marketplace plan lookup failed: {error}",
        )

    if not plan_ids:
        return make_network_status(
            "not_offered",
            "CMS Marketplace API",
            f"No matching Marketplace plans found for {network['name']} in {place['zipcode']}.",
        )

    coverage_rows = []
    try:
        for plan_id_group in chunked(plan_ids, 10):
            response = requests.get(
                f"{CMS_MARKETPLACE_API}/providers/covered",
                params={
                    "apikey": CMS_MARKETPLACE_API_KEY,
                    "providerids": ",".join(sorted(expected_npis)),
                    "planids": ",".join(plan_id_group),
                    "year": network["plan_year"],
                },
                timeout=NETWORK_LOOKUP_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()
            coverage_rows.extend(
                row for row in data.get("coverage", [])
                if str(row.get("npi")) in expected_npis and row.get("plan_id") in plan_ids
            )
    except (requests.RequestException, ValueError) as error:
        return make_network_status(
            "lookup_error",
            "CMS Marketplace API",
            f"Marketplace coverage lookup failed: {error}",
        )

    if not coverage_rows:
        return make_network_status(
            "no_record",
            "CMS Marketplace API",
            "Marketplace coverage returned no record matching the NPI Registry match.",
        )

    covered_rows = [
        row for row in coverage_rows
        if str(row.get("coverage", "")).lower() == "covered"
    ]
    if covered_rows:
        covered_plan_ids = sorted({row["plan_id"] for row in covered_rows})
        covered_npis = sorted({str(row.get("npi")) for row in covered_rows if row.get("npi")})
        if len(covered_plan_ids) < len(plan_ids):
            return make_network_status(
                "partial_coverage",
                "CMS Marketplace API",
                f"CMS shows at least one NPI Registry match covered in {len(covered_plan_ids)} of {len(plan_ids)} matching plan IDs. {len(covered_npis)} of {len(expected_npis)} NPI matches had a Covered row; confirm the exact NPI and plan before treating this as fully in-network.",
            )
        if len(covered_npis) < len(expected_npis):
            return make_network_status(
                "likely_in",
                "CMS Marketplace API",
                f"CMS shows at least one NPI Registry match covered in all {len(plan_ids)} matching plan IDs. {len(covered_npis)} of {len(expected_npis)} NPI matches had a Covered row; confirm the exact NPI/location.",
            )
        return make_network_status(
            "in",
            "CMS Marketplace API",
            f"CMS shows {provider_name} covered in {len(covered_plan_ids)} of {len(plan_ids)} matching plan IDs.",
        )

    return make_network_status(
        "out",
        "CMS Marketplace API",
        f"CMS checked {len(plan_ids)} matching plan IDs and did not mark the NPI covered.",
    )


def summarize_coverage_counts(coverage_rows):
    counts = Counter(row.get("coverage", "Unknown") for row in coverage_rows)
    return ", ".join(
        f"{coverage}: {count}"
        for coverage, count in sorted(counts.items())
    )


TIER_KEYS = (
    "tier",
    "drug_tier",
    "formulary_tier",
    "formularyTier",
    "tier_level",
    "tierLevel",
)

RESTRICTION_KEYS = {
    "PA": ("prior_authorization", "priorAuthorization", "prior_auth", "pa"),
    "ST": ("step_therapy", "stepTherapy", "st"),
    "QL": ("quantity_limit", "quantityLimit", "quantity_limits", "ql"),
}


def normalize_tier_value(value):
    text = str(value or "").strip()
    if not text:
        return ""
    if text.lower().startswith("tier"):
        return "Tier " + text.split()[-1]
    if text.isdigit():
        return f"Tier {text}"
    return text


def coverage_row_tier(row):
    for key in TIER_KEYS:
        tier = normalize_tier_value(row.get(key))
        if tier:
            return tier
    return ""


def truthy_coverage_value(value):
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "required", "applies"}


def coverage_row_restrictions(row):
    labels = []
    for label, keys in RESTRICTION_KEYS.items():
        if any(truthy_coverage_value(row.get(key)) for key in keys):
            labels.append(label)
    return labels


def carrier_source_name(carrier):
    if carrier == "BCBSTX":
        return "BCBS"
    return carrier


def formulary_sources_for_network(network):
    source_carrier = carrier_source_name(network.get("carrier", ""))
    for group in CARRIER_SOURCE_GROUPS:
        if group["carrier"] != source_carrier:
            continue
        return [
            source for source in group.get("sources", [])
            if source.get("kind") == FORMULARY
            and source.get("validation") == HTTP_200_PDF
        ]
    return []


@lru_cache(maxsize=16)
def formulary_pdf_text(url):
    if PdfReader is None:
        return ""
    response = requests.get(url, timeout=NETWORK_LOOKUP_TIMEOUT)
    response.raise_for_status()
    reader = PdfReader(BytesIO(response.content))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def formulary_lookup_terms(prescription):
    terms = []
    for drug in prescription.get("drug_results", []):
        for value in (drug.get("name"), drug.get("full_name")):
            if value and value not in terms:
                terms.append(value)
    for value in (
        prescription.get("drug_name"),
        prescription.get("prescription"),
    ):
        if value and value not in terms:
            terms.append(value)
    return terms


def formulary_line_tier(line):
    text = re.sub(r"\d+\.\d+", " ", line)
    matches = re.findall(r"(?<![\w.-])([1-6])(?![\w.-])", text)
    return matches[-1] if matches else ""


def formulary_line_requirements(line):
    requirements = []
    for label in ("PA", "ST", "QL", "AC"):
        if re.search(rf"\b{label}\b", line, re.IGNORECASE):
            requirements.append(label)
    return " ".join(requirements)


def search_formulary_text_for_tier(text, terms):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    normalized_terms = [normalize_search_text(term) for term in terms if term]
    for index, line in enumerate(lines):
        normalized_line = normalize_search_text(line)
        if not any(term and term in normalized_line for term in normalized_terms):
            continue
        window = " ".join(lines[index:index + 5])
        tier = formulary_line_tier(window)
        if tier:
            return {
                "tier_label": f"Tier {tier}",
                "tier_detail": f"Drug Tier: {tier}",
                "restriction_label": formulary_line_requirements(window),
            }
    return {}


def lookup_formulary_tier(prescription, network):
    for tier in lookup_formulary_tiers(prescription, network):
        if tier.get("tier_label"):
            return tier
    return {}


def lookup_formulary_tiers(prescription, network):
    terms = formulary_lookup_terms(prescription)
    if not terms:
        return []
    tiers = []
    for source in formulary_sources_for_network(network):
        tier = {
            "formulary_name": source["name"],
            "tier_label": "",
            "tier_detail": "",
            "restriction_label": "",
        }
        try:
            text = formulary_pdf_text(source["url"])
        except (requests.RequestException, ValueError):
            tier["tier_detail"] = "Formulary unavailable"
            tiers.append(tier)
            continue
        match = search_formulary_text_for_tier(text, terms)
        if match:
            tier.update(match)
        else:
            tier["tier_detail"] = "Tier not found"
        tiers.append(tier)
    return tiers


def enrich_status_with_formulary_tier(status, prescription, network):
    if status.get("status") not in {
        "drug_covered",
        "generic_covered",
        "partial_coverage",
        "review_exact_drug",
        "other_form_covered",
        "related_product_covered",
    }:
        return status
    sources = formulary_sources_for_network(network)
    if status.get("tier_label") and len(sources) < 2:
        return status
    formulary_tiers = lookup_formulary_tiers(prescription, network)
    matched_tiers = [tier for tier in formulary_tiers if tier.get("tier_label")]
    formulary_tier = matched_tiers[0] if matched_tiers else {}
    if status.get("tier_label") and formulary_tiers:
        enriched = dict(status)
        enriched["formulary_tiers"] = formulary_tiers
        return enriched
    if not formulary_tier:
        return status
    enriched = dict(status)
    enriched.update({
        "source": f"{status.get('source', 'CMS Marketplace API')} + Carrier formulary",
        "tier_label": formulary_tier["tier_label"],
        "tier_detail": formulary_tier["tier_detail"],
        "restriction_label": formulary_tier.get("restriction_label", ""),
        "formulary_tiers": formulary_tiers,
        "detail": (
            status.get("detail", "") +
            f" Carrier formulary {formulary_tier.get('formulary_name', '')} shows {formulary_tier['tier_detail']}."
        ).strip(),
    })
    return enriched


def summarize_tiers(coverage_rows):
    tiers = []
    restrictions = []
    for row in coverage_rows:
        tier = coverage_row_tier(row)
        if tier and tier not in tiers:
            tiers.append(tier)
        for restriction in coverage_row_restrictions(row):
            if restriction not in restrictions:
                restrictions.append(restriction)
    if not tiers:
        return "", "", " ".join(restrictions)
    detail = "Tier: " + ", ".join(tiers)
    if len(tiers) == 1:
        label = tiers[0]
    else:
        label = "Tiers"
    return label, detail, " ".join(restrictions)


def plan_ids_by_coverage_status(coverage_rows):
    plan_ids_by_status = {}
    for row in coverage_rows:
        plan_ids_by_status.setdefault(row.get("coverage", ""), set()).add(row.get("plan_id"))
    return plan_ids_by_status


def prescription_match_note(prescription):
    warning = prescription.get("drug_match_warning", "")
    match_count = prescription.get("drug_match_count") or 0
    if match_count <= 1:
        return warning

    return (
        warning +
        f"Selected RxCUI {prescription.get('rxcui')} from {match_count} CMS autocomplete matches; "
        "exact strength/form can change coverage. "
    )


def prescription_candidate_rxcuis(prescription, limit=5):
    selected = str(prescription.get("rxcui", "")).strip()
    candidates = []
    if selected:
        candidates.append(selected)

    if (prescription.get("drug_match_count") or 0) > 1:
        for drug in prescription.get("drug_results", [])[:limit]:
            rxcui = str(drug.get("rxcui", "")).strip()
            if rxcui and rxcui not in candidates:
                candidates.append(rxcui)

    return tuple(candidates)


def related_rxcui_coverage_status(prescription, related_rows, plan_ids, selected_rxcui):
    related_covered_rows = [
        row for row in related_rows
        if row.get("coverage") in {"Covered", "GenericCovered"}
    ]
    if not related_covered_rows:
        return None

    covered_plan_ids = sorted({
        row["plan_id"]
        for row in related_covered_rows
        if row.get("plan_id")
    })
    related_rxcuis = sorted({
        str(row.get("rxcui"))
        for row in related_covered_rows
        if row.get("rxcui")
    })
    count_detail = summarize_coverage_counts(related_rows)
    status = "related_product_covered" if prescription.get("selected_is_combination") else "other_form_covered"
    if len(covered_plan_ids) < len(plan_ids):
        status = "suspect" if prescription.get("selected_is_combination") else "partial_coverage"

    detail_prefix = ""
    if prescription.get("drug_match_warning"):
        detail_prefix = prescription["drug_match_warning"]
    covered_label = "related product" if prescription.get("selected_is_combination") else "other strength/form"
    tier_label, tier_detail, restriction_label = summarize_tiers(related_covered_rows)
    tier_sentence = f" {tier_detail}." if tier_detail else ""
    return make_network_status(
        status,
        "CMS Marketplace API",
        f"{detail_prefix}Selected RxCUI {selected_rxcui} was not marked covered, but CMS shows {covered_label} RxCUI match(es) {', '.join(related_rxcuis)} covered in {len(covered_plan_ids)} of {len(plan_ids)} matching plan IDs.{tier_sentence} Confirm exact product, strength, form, and tier in the carrier formulary. {count_detail}.",
        tier_label=tier_label,
        tier_detail=tier_detail,
        restriction_label=restriction_label,
    )


def check_marketplace_drug_status(prescription, network, place):
    if not prescription.get("drug_found") or not prescription.get("rxcui"):
        return make_network_status(
            "not_found",
            "CMS Marketplace API",
            "No RxCUI match was found for this prescription name.",
        )

    try:
        plan_ids = get_network_plan_ids(network, place)
    except (requests.RequestException, ValueError) as error:
        return make_network_status(
            "lookup_error",
            "CMS Marketplace API",
            f"Marketplace plan lookup failed: {error}",
        )

    if not plan_ids:
        return make_network_status(
            "not_offered",
            "CMS Marketplace API",
            f"No matching Marketplace plans found for {network['name']} in {place['zipcode']}.",
        )

    rxcui = str(prescription["rxcui"])
    candidate_rxcuis = prescription_candidate_rxcuis(prescription)
    coverage_rows = []
    try:
        for plan_id_group in chunked(plan_ids, 10):
            response = requests.get(
                f"{CMS_MARKETPLACE_API}/drugs/covered",
                params={
                    "apikey": CMS_MARKETPLACE_API_KEY,
                    "drugs": ",".join(candidate_rxcuis),
                    "planids": ",".join(plan_id_group),
                    "year": network["plan_year"],
                },
                timeout=NETWORK_LOOKUP_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()
            coverage_rows.extend(
                row for row in data.get("coverage", [])
                if str(row.get("rxcui")) in candidate_rxcuis and row.get("plan_id") in plan_ids
            )
    except (requests.RequestException, ValueError) as error:
        return make_network_status(
            "lookup_error",
            "CMS Marketplace API",
            f"Marketplace drug coverage lookup failed: {error}",
        )

    selected_rows = [
        row for row in coverage_rows
        if str(row.get("rxcui")) == rxcui
    ]
    related_rows = [
        row for row in coverage_rows
        if str(row.get("rxcui")) != rxcui
    ]

    if not selected_rows:
        related_status = related_rxcui_coverage_status(
            prescription, related_rows, plan_ids, rxcui
        )
        if related_status:
            return related_status
        if len(candidate_rxcuis) > 1:
            return make_network_status(
                "suspect",
                "CMS Marketplace API",
                f"Selected RxCUI {rxcui} from {len(candidate_rxcuis)} CMS autocomplete candidates returned no matching coverage row. Exact strength/form may differ; confirm directly.",
            )
        return make_network_status(
            "no_record",
            "CMS Marketplace API",
            "Marketplace drug coverage returned no record matching the RxCUI.",
        )

    counts = Counter(row.get("coverage", "") for row in selected_rows)
    plan_ids_by_status = plan_ids_by_coverage_status(selected_rows)
    count_detail = summarize_coverage_counts(selected_rows)
    match_note = prescription_match_note(prescription)
    if counts["Covered"]:
        covered_plan_ids = plan_ids_by_status.get("Covered", set())
        covered_rows = [
            row for row in selected_rows
            if row.get("coverage") == "Covered"
        ]
        tier_label, tier_detail, restriction_label = summarize_tiers(covered_rows)
        tier_sentence = f" {tier_detail}." if tier_detail else ""
        if prescription.get("selected_is_combination"):
            return make_network_status(
                "related_product_covered",
                "CMS Marketplace API",
                f"{match_note}CMS found coverage for {prescription['drug_name']}.{tier_sentence} Treat this as related coverage evidence, not final standalone-drug coverage; confirm exact product, strength, form, and tier in the carrier formulary. {count_detail}.",
                tier_label=tier_label,
                tier_detail=tier_detail,
                restriction_label=restriction_label,
            )
        if len(covered_plan_ids) < len(plan_ids):
            return make_network_status(
                "partial_coverage",
                "CMS Marketplace API",
                f"{match_note}CMS shows {prescription['drug_name']} covered in {len(covered_plan_ids)} of {len(plan_ids)} matching plan IDs.{tier_sentence} {count_detail}.",
                tier_label=tier_label,
                tier_detail=tier_detail,
                restriction_label=restriction_label,
            )
        if match_note:
            return make_network_status(
                "review_exact_drug",
                "CMS Marketplace API",
                f"{match_note}CMS shows {prescription['drug_name']} covered in all {len(plan_ids)} matching plan IDs.{tier_sentence} Confirm exact product, strength, form, and tier in the carrier formulary before telling a client it is covered. {count_detail}.",
                tier_label=tier_label,
                tier_detail=tier_detail,
                restriction_label=restriction_label,
            )
        return make_network_status(
            "drug_covered",
            "CMS Marketplace API",
            f"CMS shows {prescription['drug_name']} covered in {len(covered_plan_ids)} of {len(plan_ids)} matching plan IDs.{tier_sentence} {count_detail}.",
            tier_label=tier_label,
            tier_detail=tier_detail,
            restriction_label=restriction_label,
        )
    if counts["GenericCovered"]:
        generic_plan_ids = plan_ids_by_status.get("GenericCovered", set())
        generic_rows = [
            row for row in selected_rows
            if row.get("coverage") == "GenericCovered"
        ]
        tier_label, tier_detail, restriction_label = summarize_tiers(generic_rows)
        tier_sentence = f" {tier_detail}." if tier_detail else ""
        generic_rxcuis = sorted({
            row.get("generic_rxcui", "")
            for row in selected_rows
            if row.get("generic_rxcui")
        })
        generic_note = f" Generic RxCUI: {', '.join(generic_rxcuis)}." if generic_rxcuis else ""
        if prescription.get("selected_is_combination"):
            return make_network_status(
                "related_product_covered",
                "CMS Marketplace API",
                f"{match_note}CMS found generic coverage for {prescription['drug_name']}.{tier_sentence} Treat this as related coverage evidence, not final standalone-drug coverage; confirm exact product, strength, form, and tier in the carrier formulary.{generic_note} {count_detail}.",
                tier_label=tier_label,
                tier_detail=tier_detail,
                restriction_label=restriction_label,
            )
        if len(generic_plan_ids) < len(plan_ids):
            return make_network_status(
                "partial_coverage",
                "CMS Marketplace API",
                f"{match_note}CMS shows a generic equivalent covered in {len(generic_plan_ids)} of {len(plan_ids)} matching plan IDs.{tier_sentence}{generic_note} {count_detail}.",
                tier_label=tier_label,
                tier_detail=tier_detail,
                restriction_label=restriction_label,
            )
        if match_note:
            return make_network_status(
                "review_exact_drug",
                "CMS Marketplace API",
                f"{match_note}CMS shows a generic equivalent covered in all {len(plan_ids)} matching plan IDs.{tier_sentence} Confirm whether the brand or only the generic is covered before telling a client it is covered.{generic_note} {count_detail}.",
                tier_label=tier_label,
                tier_detail=tier_detail,
                restriction_label=restriction_label,
            )
        return make_network_status(
            "generic_covered",
            "CMS Marketplace API",
            f"CMS shows a generic equivalent covered in {len(generic_plan_ids)} of {len(plan_ids)} matching plan IDs.{tier_sentence}{generic_note} {count_detail}.",
            tier_label=tier_label,
            tier_detail=tier_detail,
            restriction_label=restriction_label,
        )

    related_status = related_rxcui_coverage_status(
        prescription, related_rows, plan_ids, rxcui
    )
    if related_status:
        return related_status

    if counts["DataNotProvided"]:
        data_plan_ids = plan_ids_by_status.get("DataNotProvided", set())
        if len(data_plan_ids) < len(plan_ids):
            return make_network_status(
                "suspect",
                "CMS Marketplace API",
                f"CMS did not receive usable formulary data for {len(data_plan_ids)} of {len(plan_ids)} matching plan IDs. {count_detail}.",
            )
        return make_network_status(
            "data_not_provided",
            "CMS Marketplace API",
            f"CMS did not receive usable formulary data for {len(data_plan_ids)} of {len(plan_ids)} matching plan IDs. {count_detail}.",
        )

    if match_note:
        return make_network_status(
            "suspect",
            "CMS Marketplace API",
            f"{match_note}CMS checked {len(plan_ids)} matching plan IDs and did not mark the selected RxCUI covered. {count_detail}.",
        )

    return make_network_status(
        "drug_not_covered",
        "CMS Marketplace API",
        f"CMS checked {len(plan_ids)} matching plan IDs and did not mark the checked RxCUI candidate(s) covered. {count_detail}.",
    )


def check_network_statuses(provider_name, provider_type, npi_results, networks, place):
    statuses = {}
    for network in networks:
        if provider_type == "not_found":
            statuses[network["id"]] = make_network_status(
                "not_found",
                "NPI Registry",
                "Provider was not found in the NPI Registry.",
            )
        elif network.get("marketplace_issuer"):
            statuses[network["id"]] = check_marketplace_network_status(
                provider_name, provider_type, npi_results, network, place
            )
        else:
            statuses[network["id"]] = make_network_status(
                "not_configured",
                "Provider Finder",
                "No public lookup is configured for this network.",
            )
    return statuses


def check_prescription_statuses(prescription, networks, place):
    statuses = {}
    for network in networks:
        if network.get("marketplace_issuer"):
            status = check_marketplace_drug_status(
                prescription, network, place
            )
            statuses[network["id"]] = enrich_status_with_formulary_tier(
                status, prescription, network
            )
        else:
            statuses[network["id"]] = make_network_status(
                "not_configured",
                "Provider Finder",
                "No public lookup is configured for this network.",
            )
    return statuses


def checked_formulary_matches_for_drugs(drugs, networks, place):
    rxcuis = [
        str(drug.get("rxcui", "")).strip()
        for drug in drugs
        if str(drug.get("rxcui", "")).strip()
    ]
    matches_by_rxcui = {rxcui: [] for rxcui in rxcuis}
    if not rxcuis:
        return matches_by_rxcui

    for network in networks:
        if not network.get("marketplace_issuer"):
            continue
        try:
            plan_ids = get_network_plan_ids(network, place)
        except (requests.RequestException, ValueError):
            continue
        if not plan_ids:
            continue

        coverage_rows = []
        try:
            for plan_id_group in chunked(plan_ids, 10):
                for rxcui_group in chunked(rxcuis, 10):
                    response = requests.get(
                        f"{CMS_MARKETPLACE_API}/drugs/covered",
                        params={
                            "apikey": CMS_MARKETPLACE_API_KEY,
                            "drugs": ",".join(rxcui_group),
                            "planids": ",".join(plan_id_group),
                            "year": network["plan_year"],
                        },
                        timeout=NETWORK_LOOKUP_TIMEOUT,
                    )
                    response.raise_for_status()
                    data = response.json()
                    coverage_rows.extend(
                        row for row in data.get("coverage", [])
                        if str(row.get("rxcui")) in rxcui_group
                        and row.get("plan_id") in plan_ids
                        and row.get("coverage") in {"Covered", "GenericCovered"}
                    )
        except (requests.RequestException, ValueError):
            continue

        for rxcui in rxcuis:
            matching_rows = [
                row for row in coverage_rows
                if str(row.get("rxcui")) == rxcui
            ]
            if not matching_rows:
                continue
            matches_by_rxcui[rxcui].append({
                "carrier": network["carrier"],
                "plan": network["name"],
                "network_id": network["id"],
                "tier_label": "",
                "tier_detail": "",
                "restriction_label": "",
            })
    return matches_by_rxcui


def build_networks(bcbstx_urls, uhc_urls):
    networks = []
    for key, url_info in bcbstx_urls.items():
        config = BCBSTX_SEARCH_URLS[key]
        networks.append({
            "id": f"bcbstx:{key}",
            "carrier": "BCBSTX",
            "name": url_info["name"],
            "url": url_info["url"],
            "marketplace_issuer": config["marketplace_issuer"],
            "marketplace_plan_name_contains": config.get("marketplace_plan_name_contains", []),
            "marketplace_network_url_contains": config.get("marketplace_network_url_contains", ""),
            "plan_year": 2026,
        })
    for key, url_info in uhc_urls.items():
        config = UHC_SEARCH_URLS[key]
        networks.append({
            "id": f"uhc:{key}",
            "carrier": "UHC",
            "name": url_info["name"],
            "url": url_info["url"],
            "network_code": config["network_code"],
            "configuration": config["configuration"],
            "plan_year": config["plan_year"],
            "marketplace_issuer": config["marketplace_issuer"],
            "marketplace_network_url_contains": config["marketplace_network_url_contains"],
        })
    return networks


def parse_doctors(doctor_input, provider_type="auto"):
    """Parse provider input string."""
    provider_type = normalize_provider_type(provider_type)
    doctors = []
    for entry in doctor_input.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "(" in entry and ")" in entry:
            name_part = entry[:entry.index("(")].strip()
            specialty = entry[entry.index("(") + 1:entry.index(")")].strip()
            doctors.append({"name": name_part, "specialty": specialty, "provider_type": provider_type})
        else:
            doctors.append({"name": entry, "specialty": None, "provider_type": provider_type})
    return doctors


def parse_prescriptions(prescription_input):
    prescriptions = []
    for entry in prescription_input.split(","):
        entry = entry.strip()
        if entry:
            prescriptions.append(entry)
    return prescriptions


def parse_prescription_selections(selection_input):
    if not selection_input:
        return []
    try:
        selections = json.loads(selection_input)
    except (TypeError, ValueError):
        return []
    if not isinstance(selections, list):
        return []
    return [
        selection for selection in selections
        if isinstance(selection, dict) and selection.get("rxcui")
    ]


def parse_provider_selections(selection_input):
    if not selection_input:
        return []
    try:
        selections = json.loads(selection_input)
    except (TypeError, ValueError):
        return []
    if not isinstance(selections, list):
        return []
    return [
        selection for selection in selections
        if isinstance(selection, dict) and selection.get("npi")
    ]


def provider_result_group(requested_provider_type, resolved_provider_type):
    if requested_provider_type in {"doctor", "facility"}:
        return requested_provider_type
    if resolved_provider_type in {"doctor", "facility"}:
        return resolved_provider_type
    return "doctor"


def selected_carriers_from_request(args):
    if args.get("carrier_filter_submitted") != "true":
        return set(DEFAULT_CARRIERS)
    return {
        carrier
        for carrier in args.getlist("carriers")
        if carrier in CARRIER_VALUES
    }


def selected_carriers_from_values(values):
    selected = {
        carrier
        for carrier in values
        if carrier in CARRIER_VALUES
    }
    return selected or set(DEFAULT_CARRIERS)


def carrier_source_groups_for_selection(selected_carriers):
    allowed_source_carriers = {
        SOURCE_CARRIERS_BY_VALUE[carrier]
        for carrier in selected_carriers
        if carrier in SOURCE_CARRIERS_BY_VALUE
    }
    return [
        group for group in CARRIER_SOURCE_GROUPS
        if group["carrier"] in allowed_source_carriers
    ]


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/sources/status")
def sources_status():
    return jsonify(source_freshness_summary())


@app.route("/drugs/search")
def drugs_search():
    query = request.args.get("q", "").strip()
    if len(query) < 2:
        return jsonify({"drugs": []})
    formulary_only = request.args.get("formulary_only") == "true"
    carrier_values = request.args.getlist("carriers")
    selected_carriers = (
        selected_carriers_from_values(carrier_values)
        if carrier_values else set(DEFAULT_CARRIERS)
    )
    if formulary_only and not carrier_values:
        selected_carriers = set()
    location = request.args.get("location", "dallas").lower()
    lat, lon = TEXAS_LOCATIONS.get(location, TEXAS_LOCATIONS["dallas"])
    place = TEXAS_MARKETPLACE_PLACES.get(location, TEXAS_MARKETPLACE_PLACES["dallas"])
    bcbstx_urls = generate_bcbstx_urls("", lat, lon, 25) if "bcbstx" in selected_carriers else {}
    uhc_urls = generate_uhc_urls() if "uhc" in selected_carriers else {}
    networks = build_networks(bcbstx_urls, uhc_urls)

    raw_drugs = search_drugs(query, limit=10)
    matches_by_rxcui = (
        checked_formulary_matches_for_drugs(raw_drugs, networks, place)
        if formulary_only else {}
    )

    drugs = []
    for drug in raw_drugs:
        candidate = serialize_drug_candidate(drug)
        candidate["formulary_matches"] = matches_by_rxcui.get(candidate["rxcui"], [])
        if formulary_only and not candidate["formulary_matches"]:
            continue
        drugs.append(candidate)

    message = ""
    if formulary_only and not drugs:
        message = "No checked carrier formulary match found for this prescription."
    return jsonify({"drugs": drugs, "message": message})


@app.route("/providers/search")
def providers_search():
    query = request.args.get("q", "").strip()
    provider_type = normalize_provider_type(request.args.get("type", "facility"))
    if provider_type == "auto":
        provider_type = "facility"
    city = request.args.get("city", "").strip() or None
    if len(query) < 2:
        return jsonify({"providers": []})
    providers = [
        serialize_provider_candidate(provider, provider_type)
        for provider in search_provider_npi(
            query, state="TX", city=city, limit=25, provider_type=provider_type
        )
    ]
    return jsonify({"providers": providers})


@app.route("/search")
def search():
    legacy_providers_input = request.args.get("providers", "")
    doctors_input = request.args.get("doctors", "")
    facilities_input = request.args.get("facilities", "")
    facility_selections_input = request.args.get("facility_selections", "")
    prescriptions_input = request.args.get("prescriptions", "")
    prescription_selections_input = request.args.get("prescription_selections", "")
    provider_type = normalize_provider_type(request.args.get("provider_type", "auto"))
    location = request.args.get("location", "dallas").lower()
    radius = int(request.args.get("radius", 25))
    city = request.args.get("city", "").strip() or None
    selected_carriers = selected_carriers_from_request(request.args)

    lat, lon = TEXAS_LOCATIONS.get(location, TEXAS_LOCATIONS["dallas"])
    marketplace_place = TEXAS_MARKETPLACE_PLACES.get(location, TEXAS_MARKETPLACE_PLACES["dallas"])

    doctors = []
    doctors.extend(parse_doctors(doctors_input, provider_type="doctor"))
    facility_selections = parse_provider_selections(facility_selections_input)
    selected_facility_names = {
        (selection.get("display_name") or selection.get("name") or "").strip()
        for selection in facility_selections
    }
    for selection in facility_selections:
        doctors.append({
            "name": selection.get("display_name") or selection.get("name"),
            "specialty": None,
            "provider_type": "facility",
            "npi_results": [{
                "npi": selection.get("npi", ""),
                "name": selection.get("name", ""),
                "credential": selection.get("credential", ""),
                "specialty": selection.get("specialty", ""),
                "address": selection.get("address", ""),
                "location": selection.get("location", ""),
                "phone": selection.get("phone", ""),
            }],
        })
    for facility in parse_doctors(facilities_input, provider_type="facility"):
        if facility["name"] in selected_facility_names:
            continue
        doctors.append(facility)
    doctors.extend(parse_doctors(legacy_providers_input, provider_type=provider_type))
    prescription_selections = parse_prescription_selections(prescription_selections_input)
    prescription_names = parse_prescriptions(prescriptions_input)
    base_bcbstx_urls = generate_bcbstx_urls("", lat, lon, radius) if "bcbstx" in selected_carriers else {}
    base_uhc_urls = generate_uhc_urls() if "uhc" in selected_carriers else {}
    base_networks = build_networks(base_bcbstx_urls, base_uhc_urls)

    results = []
    for doctor in doctors:
        if doctor.get("npi_results"):
            npi_results = doctor["npi_results"]
            resolved_provider_type = doctor["provider_type"]
        else:
            npi_results, resolved_provider_type = resolve_provider_npi(
                doctor["name"], state="TX", city=city, specialty=doctor["specialty"],
                provider_type=doctor["provider_type"]
            )

        bcbstx_urls = generate_bcbstx_urls(doctor["name"], lat, lon, radius) if "bcbstx" in selected_carriers else {}
        uhc_urls = generate_uhc_urls() if "uhc" in selected_carriers else {}
        networks = build_networks(bcbstx_urls, uhc_urls)
        network_statuses = check_network_statuses(
            doctor["name"], resolved_provider_type, npi_results, networks, marketplace_place
        )

        results.append({
            "provider": doctor["name"],
            "doctor": doctor["name"],
            "provider_type": resolved_provider_type,
            "requested_provider_type": doctor["provider_type"],
            "provider_group": provider_result_group(doctor["provider_type"], resolved_provider_type),
            "specialty_filter": doctor["specialty"],
            "npi_found": len(npi_results) > 0,
            "npi_count": len(npi_results),
            "npi_results": npi_results[:5],
            "bcbstx_urls": bcbstx_urls,
            "uhc_urls": uhc_urls,
            "networks": networks,
            "network_statuses": network_statuses,
        })

    prescription_results = []
    selected_names = {
        (selection.get("display_name") or "").strip()
        for selection in prescription_selections
    }
    for selection in prescription_selections:
        prescription = resolve_selected_prescription(selection)
        prescription["network_statuses"] = check_prescription_statuses(
            prescription, base_networks, marketplace_place
        )
        prescription_results.append(prescription)
    for prescription_name in prescription_names:
        if prescription_name in selected_names:
            continue
        prescription = resolve_prescription(prescription_name)
        prescription["network_statuses"] = check_prescription_statuses(
            prescription, base_networks, marketplace_place
        )
        prescription_results.append(prescription)

    return jsonify({
        "providers": results,
        "prescriptions": prescription_results,
        "networks": base_networks,
        "sources": carrier_source_groups_for_selection(selected_carriers),
        "selected_carriers": sorted(selected_carriers),
    })


if __name__ == "__main__":
    if "--refresh-sources" in sys.argv:
        refreshed_cache = check_source_freshness()
        summary = source_freshness_summary(refreshed_cache)
        print(
            f"Updated {SOURCE_FRESHNESS_CACHE_PATH}: "
            f"{summary['counts']['total']} sources, "
            f"{summary['counts']['changed']} changed, "
            f"{summary['counts']['suspect']} suspect, "
            f"{summary['counts']['missing']} missing."
        )
        sys.exit(0)

    print("\n  Provider Network Checker")
    print("  Open http://127.0.0.1:5050 in your browser\n")
    app.run(debug=True, port=5050)
