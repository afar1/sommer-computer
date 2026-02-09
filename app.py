#!/usr/bin/env python3
"""
Web interface for Doctor Network Checker
"""

from flask import Flask, render_template_string, request, jsonify
import requests
from urllib.parse import quote
from dataclasses import dataclass
from typing import Optional

app = Flask(__name__)

# NPI Registry API
NPI_API = "https://npiregistry.cms.hhs.gov/api/"

# BCBSTX Provider Finder URL templates
BCBSTX_SEARCH_URLS = {
    "blue_advantage_hmo": {
        "name": "Blue Advantage HMO",
        "url": "https://my.providerfinderonline.com/search/name/{query}?ci=tx-blueadvantage-retail&network_id=1000128&geo_location={lat},{lon}&locale=en&corp_code=TX&radius={radius}",
    },
    "my_blue_health": {
        "name": "My Blue Health",
        "url": "https://my.providerfinderonline.com/search/name/{query}?ci=tx-myblue-health&network_id=240000127&geo_location={lat},{lon}&locale=en&corp_code=TX&radius={radius}",
    },
}

# UHC Provider Finder URLs (Rally/werally.com)
UHC_SEARCH_URLS = {
    "tx_individual_exchange": {
        "name": "UHC TX Individual Exchange",
        "url": "https://connect.werally.com/guest/eyJwbGFuTmFtZSI6IlRYIEluZGl2aWR1YWwgRXhjaGFuZ2UgQmVuZWZpdCBQbGFuIiwiZGVsc3lzIjoiOTA4IiwiY292ZXJhZ2VUeXBlIjoibWVkaWNhbCIsInBhcnRuZXJJZCI6InVoYyIsImxhbmd1YWdlIjoiZW4iLCJzaG93Q29zdHMiOnRydWUsImZpcHNDb2RlIjoiNDgifQMQFY_1U6GK3dWzJO0xysxZD0H-Ei_AJ0Wm_n0zlgcUI?planYear=2026",
    },
    "tx_kelsey_seybold": {
        "name": "UHC TX Kelsey-Seybold",
        "url": "https://connect.werally.com/guest/eyJwbGFuTmFtZSI6IlRYIEtlbHNleS1TZXlib2xkIEluZGl2aWR1YWwgRXhjaGFuZ2UgQmVuZWZpdCBQbGFuIiwiZGVsc3lzIjoiOTMzIiwiY292ZXJhZ2VUeXBlIjoibWVkaWNhbCIsInBhcnRuZXJJZCI6InVoYyIsImxhbmd1YWdlIjoiZW4iLCJzaG93Q29zdHMiOnRydWUsImZpcHNDb2RlIjoiNDgifQXmvT5Dh8azZlM00ptmfLp0BrlP0yAn-7TQUdErUzIbs?planYear=2026",
    },
    "tx_sanitas_anchor": {
        "name": "UHC TX Sanitas Anchor",
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

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Doctor Network Checker</title>
    <style>
        * {
            box-sizing: border-box;
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            max-width: 900px;
            margin: 0 auto;
            padding: 20px;
            background: #f5f5f5;
        }
        h1 {
            color: #1f2937;
            margin-bottom: 5px;
        }
        .subtitle {
            color: #666;
            margin-bottom: 30px;
        }
        .search-form {
            background: white;
            padding: 25px;
            border-radius: 12px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            margin-bottom: 30px;
        }
        .form-group {
            margin-bottom: 20px;
        }
        label {
            display: block;
            margin-bottom: 8px;
            font-weight: 600;
            color: #333;
        }
        input, select {
            width: 100%;
            padding: 12px;
            border: 2px solid #e0e0e0;
            border-radius: 8px;
            font-size: 16px;
            transition: border-color 0.2s;
        }
        input:focus, select:focus {
            outline: none;
            border-color: #2563eb;
        }
        .form-row {
            display: flex;
            gap: 15px;
        }
        .form-row .form-group {
            flex: 1;
        }
        button {
            background: #2563eb;
            color: white;
            padding: 14px 28px;
            border: none;
            border-radius: 8px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            width: 100%;
            transition: background 0.2s;
        }
        button:hover {
            background: #1d4ed8;
        }
        button:disabled {
            background: #93c5fd;
            cursor: not-allowed;
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
            .search-form {
                padding: 16px;
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
        }
    </style>
</head>
<body>
    <h1>Doctor Network Checker</h1>
    <p class="subtitle">Search for doctors in the NPI Registry and get insurance network links</p>

    <div class="search-form">
        <form id="searchForm">
            <div class="form-group">
                <label for="doctors">Doctor Name(s)</label>
                <input type="text" id="doctors" name="doctors"
                       placeholder="e.g., John Smith, Jane Doe" required>
                <p class="hint">Separate multiple names with commas. Add (Specialty) to filter, e.g., "John Smith (Cardiology)"</p>
            </div>

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

            <button type="submit" id="searchBtn">Search Doctors</button>
        </form>
    </div>

    <div id="results"></div>

    <script>
        document.getElementById('searchForm').addEventListener('submit', async function(e) {
            e.preventDefault();

            const btn = document.getElementById('searchBtn');
            const resultsDiv = document.getElementById('results');

            btn.disabled = true;
            btn.textContent = 'Searching...';
            resultsDiv.innerHTML = '<div class="loading"><div class="spinner"></div>Searching NPI Registry...</div>';

            const formData = new FormData(this);
            const params = new URLSearchParams(formData);

            try {
                const response = await fetch('/search?' + params.toString());
                const data = await response.json();

                let html = '<div class="results">';

                for (const result of data) {
                    html += '<div class="result-card">';
                    html += `<div class="doctor-name">${result.doctor}`;
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
                            html += '<div class="npi-item">';
                            html += `<div class="npi-name">${npi.name}, ${npi.credential}</div>`;
                            html += `<div class="npi-detail">NPI: ${npi.npi} | ${npi.specialty}</div>`;
                            html += `<div class="npi-detail">${npi.location}${npi.phone ? ' | ' + npi.phone : ''}</div>`;
                            html += '</div>';
                        }
                    } else {
                        html += '<div class="npi-item not-found">';
                        html += '<div class="npi-name">No matches in NPI Registry</div>';
                        html += '<div class="npi-detail">Try different spelling or remove specialty filter</div>';
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

                html += '</div>';
                resultsDiv.innerHTML = html;

            } catch (error) {
                resultsDiv.innerHTML = '<div class="result-card"><div class="npi-item not-found">Error: ' + error.message + '</div></div>';
            }

            btn.disabled = false;
            btn.textContent = 'Search Doctors';
        });
    </script>

    <footer class="footer">
        <a href="https://sommer.computer" target="_blank" rel="noopener noreferrer">sommer.computer</a>
    </footer>
</body>
</html>
"""


def search_npi(name, state="TX", city=None, specialty=None, limit=10):
    """Search the NPI Registry for providers."""
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

    params = {
        "version": "2.1",
        "limit": limit,
        "enumeration_type": "NPI-1",
        "state": state,
    }

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

            results.append({
                "npi": r.get("number", ""),
                "name": f"{basic.get('first_name', '')} {basic.get('last_name', '')}".strip(),
                "credential": basic.get("credential", ""),
                "specialty": specialty_name,
                "location": f"{practice_addr.get('city', '')}, {practice_addr.get('state', '')} {practice_addr.get('postal_code', '')[:5]}",
                "phone": practice_addr.get("telephone_number", ""),
            })

        return results
    except Exception:
        return []


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


def parse_doctors(doctor_input):
    """Parse doctor input string."""
    doctors = []
    for entry in doctor_input.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "(" in entry and ")" in entry:
            name_part = entry[:entry.index("(")].strip()
            specialty = entry[entry.index("(") + 1:entry.index(")")].strip()
            doctors.append({"name": name_part, "specialty": specialty})
        else:
            doctors.append({"name": entry, "specialty": None})
    return doctors


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/search")
def search():
    doctors_input = request.args.get("doctors", "")
    location = request.args.get("location", "dallas").lower()
    radius = int(request.args.get("radius", 25))
    city = request.args.get("city", "").strip() or None

    lat, lon = TEXAS_LOCATIONS.get(location, TEXAS_LOCATIONS["dallas"])

    doctors = parse_doctors(doctors_input)

    results = []
    for doctor in doctors:
        npi_results = search_npi(
            doctor["name"], state="TX", city=city, specialty=doctor["specialty"]
        )

        bcbstx_urls = generate_bcbstx_urls(doctor["name"], lat, lon, radius)
        uhc_urls = generate_uhc_urls()

        results.append({
            "doctor": doctor["name"],
            "specialty_filter": doctor["specialty"],
            "npi_found": len(npi_results) > 0,
            "npi_count": len(npi_results),
            "npi_results": npi_results[:5],
            "bcbstx_urls": bcbstx_urls,
            "uhc_urls": uhc_urls,
        })

    return jsonify(results)


if __name__ == "__main__":
    print("\n  Doctor Network Checker")
    print("  Open http://127.0.0.1:5050 in your browser\n")
    app.run(debug=True, port=5050)
