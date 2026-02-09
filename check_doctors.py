#!/usr/bin/env python3
"""
Check doctor availability using NPI Registry and generate BCBSTX network search links.

The NPI Registry is a public government database of all healthcare providers.
For network-specific verification, this script generates direct links to BCBSTX's
provider finder that you can open in your browser.
"""

import requests
import argparse
import sys
from dataclasses import dataclass
from typing import Optional
import json
from urllib.parse import quote

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

# Common Texas cities/zips for offline geocoding
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
    "garland": (32.9126, -96.6389),
    "richardson": (32.9483, -96.7299),
    "carrollton": (32.9537, -96.8903),
    "lewisville": (33.0462, -96.9942),
    "el paso": (31.7619, -106.4850),
    "corpus christi": (27.8006, -97.3964),
    "lubbock": (33.5779, -101.8552),
    "amarillo": (35.2220, -101.8313),
    "waco": (31.5493, -97.1467),
    "midland": (31.9973, -102.0779),
    "odessa": (31.8457, -102.3676),
    "beaumont": (30.0802, -94.1266),
    "tyler": (32.3513, -95.3011),
    "round rock": (30.5083, -97.6789),
    "sugar land": (29.6197, -95.6349),
    "the woodlands": (30.1658, -95.4613),
    "katy": (29.7858, -95.8245),
    "pasadena": (29.6911, -95.2091),
    "mesquite": (32.7668, -96.5992),
    # Common zip codes
    "75201": (32.7872, -96.7985),
    "75202": (32.7830, -96.8010),
    "76102": (32.7593, -97.3283),
    "77002": (29.7545, -95.3592),
    "78201": (29.4650, -98.5254),
    "73301": (30.3265, -97.7713),
    "78701": (30.2711, -97.7437),
}


@dataclass
class Doctor:
    name: str
    specialty: Optional[str] = None


@dataclass
class NPIResult:
    npi: str
    name: str
    credential: str
    specialty: str
    address: str
    city: str
    state: str
    zip_code: str
    phone: str


def geocode_location(location: str) -> tuple[float, float]:
    """Convert a location string to coordinates."""
    loc_lower = location.lower().strip()
    loc_lower = loc_lower.replace(", tx", "").replace(",tx", "").replace(" tx", "")
    loc_lower = loc_lower.replace(", texas", "").replace(",texas", "").replace(" texas", "")
    loc_lower = loc_lower.strip()

    if loc_lower in TEXAS_LOCATIONS:
        return TEXAS_LOCATIONS[loc_lower]

    if "tx" not in location.lower() and "texas" not in location.lower():
        location = f"{location}, TX"

    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": location, "format": "json", "limit": 1, "countrycodes": "us"}
    headers = {"User-Agent": "Doctor-Network-Checker/1.0"}

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        results = resp.json()
        if results:
            return float(results[0]["lat"]), float(results[0]["lon"])
    except requests.RequestException:
        pass

    raise ValueError(f"Could not geocode: {location}. Use --coords or a major Texas city.")


def search_npi(
    name: str,
    state: str = "TX",
    city: Optional[str] = None,
    specialty: Optional[str] = None,
    limit: int = 10,
) -> list[NPIResult]:
    """Search the NPI Registry for providers."""
    # Parse name (handle "Last, First" or "First Last")
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
        "enumeration_type": "NPI-1",  # Individual providers only
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

            # Get practice address (location_type == "primary")
            practice_addr = next(
                (a for a in addresses if a.get("address_purpose") == "LOCATION"),
                addresses[0] if addresses else {},
            )

            # Get primary taxonomy (specialty)
            primary_tax = next(
                (t for t in taxonomies if t.get("primary")),
                taxonomies[0] if taxonomies else {},
            )

            specialty_name = primary_tax.get("desc", "Unknown")

            # Filter by specialty if provided
            if specialty:
                if specialty.lower() not in specialty_name.lower():
                    continue

            results.append(
                NPIResult(
                    npi=r.get("number", ""),
                    name=f"{basic.get('first_name', '')} {basic.get('last_name', '')}".strip(),
                    credential=basic.get("credential", ""),
                    specialty=specialty_name,
                    address=practice_addr.get("address_1", ""),
                    city=practice_addr.get("city", ""),
                    state=practice_addr.get("state", ""),
                    zip_code=practice_addr.get("postal_code", "")[:5],
                    phone=practice_addr.get("telephone_number", ""),
                )
            )

        return results

    except requests.RequestException as e:
        print(f"  Warning: NPI lookup error: {e}", file=sys.stderr)
        return []


def generate_bcbstx_urls(name: str, lat: float, lon: float, radius: int) -> dict:
    """Generate BCBSTX provider finder URLs for a doctor."""
    urls = {}
    for key, config in BCBSTX_SEARCH_URLS.items():
        urls[key] = {
            "name": config["name"],
            "url": config["url"].format(
                query=quote(name), lat=lat, lon=lon, radius=radius
            ),
        }
    return urls


def check_doctor(
    doctor: Doctor, lat: float, lon: float, radius: int, city: Optional[str] = None
) -> dict:
    """Check a doctor in NPI Registry and generate BCBSTX links."""
    npi_results = search_npi(
        doctor.name, state="TX", city=city, specialty=doctor.specialty
    )

    bcbstx_urls = generate_bcbstx_urls(doctor.name, lat, lon, radius)

    return {
        "doctor": doctor.name,
        "specialty_filter": doctor.specialty,
        "npi_found": len(npi_results) > 0,
        "npi_count": len(npi_results),
        "npi_results": [
            {
                "npi": r.npi,
                "name": r.name,
                "credential": r.credential,
                "specialty": r.specialty,
                "location": f"{r.city}, {r.state} {r.zip_code}",
                "phone": r.phone,
            }
            for r in npi_results[:5]
        ],
        "bcbstx_urls": bcbstx_urls,
    }


def print_results(results: list[dict], show_urls: bool = True):
    """Print results in a readable format."""
    print("\n" + "=" * 80)
    print("DOCTOR SEARCH RESULTS")
    print("=" * 80)

    for r in results:
        print(f"\n{'─' * 80}")
        print(f"SEARCH: {r['doctor']}", end="")
        if r["specialty_filter"]:
            print(f" [{r['specialty_filter']}]", end="")
        print()
        print("─" * 80)

        if r["npi_found"]:
            print(f"\n✅ Found {r['npi_count']} match(es) in NPI Registry:\n")
            for i, npi in enumerate(r["npi_results"], 1):
                print(f"  {i}. {npi['name']}, {npi['credential']}")
                print(f"     NPI: {npi['npi']}")
                print(f"     Specialty: {npi['specialty']}")
                print(f"     Location: {npi['location']}")
                if npi["phone"]:
                    print(f"     Phone: {npi['phone']}")
                print()
        else:
            print("\n❌ No matches found in NPI Registry")
            print("   (Try different name spelling or remove specialty filter)\n")

        if show_urls:
            print("  BCBSTX Network Search Links:")
            print("  (Open in browser to verify network status)")
            for key, url_info in r["bcbstx_urls"].items():
                print(f"\n  → {url_info['name']}:")
                print(f"    {url_info['url']}")

    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)

    found = sum(1 for r in results if r["npi_found"])
    not_found = len(results) - found

    print(f"\n  NPI Registry Results:")
    print(f"    ✅ Found: {found}")
    if not_found:
        print(f"    ❌ Not found: {not_found}")

    print(f"\n  Note: NPI Registry confirms doctors are licensed in Texas.")
    print(f"  Use the BCBSTX links above to verify network status.")
    print()


def parse_doctors(doctor_input: str) -> list[Doctor]:
    """Parse doctor input string."""
    doctors = []
    for entry in doctor_input.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "(" in entry and ")" in entry:
            name_part = entry[: entry.index("(")].strip()
            specialty = entry[entry.index("(") + 1 : entry.index(")")].strip()
            doctors.append(Doctor(name=name_part, specialty=specialty))
        else:
            doctors.append(Doctor(name=entry))
    return doctors


def main():
    parser = argparse.ArgumentParser(
        description="Check doctor availability using NPI Registry and generate BCBSTX search links",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --doctors "John Smith, Jane Doe" --location "Dallas"
  %(prog)s --doctors "John Smith (Cardiology)" --location 75201
  %(prog)s --file doctors.txt --location "Austin" --city "Austin"
  %(prog)s --doctors "Smith" --coords "32.7767,-96.7970" --json
        """,
    )

    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument(
        "--doctors", "-d",
        help="Comma-separated list of doctor names. Use (Specialty) for filtering.",
    )
    input_group.add_argument(
        "--file", "-f",
        help="File with one doctor per line",
    )

    parser.add_argument(
        "--location", "-l",
        help="Location for BCBSTX search (zip code or 'City')",
    )
    parser.add_argument(
        "--coords", "-c",
        help="Direct coordinates as 'lat,lon'",
    )
    parser.add_argument(
        "--city",
        help="City name to filter NPI results (e.g., 'Dallas')",
    )
    parser.add_argument(
        "--radius", "-r",
        type=int,
        default=25,
        help="Search radius in miles for BCBSTX links (default: 25)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON",
    )
    parser.add_argument(
        "--no-urls",
        action="store_true",
        help="Don't show BCBSTX search URLs",
    )

    args = parser.parse_args()

    # Parse doctors
    if args.doctors:
        doctors = parse_doctors(args.doctors)
    else:
        with open(args.file, "r") as f:
            doctors = parse_doctors(f.read().replace("\n", ","))

    if not doctors:
        print("Error: No doctors specified", file=sys.stderr)
        sys.exit(1)

    # Get coordinates
    if args.coords:
        try:
            lat, lon = map(float, args.coords.split(","))
        except ValueError:
            print("Error: --coords must be 'lat,lon'", file=sys.stderr)
            sys.exit(1)
    elif args.location:
        try:
            lat, lon = geocode_location(args.location)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        # Default to Dallas
        lat, lon = TEXAS_LOCATIONS["dallas"]

    if not args.json:
        print(f"Location: {lat:.4f}, {lon:.4f}")
        print(f"Checking {len(doctors)} doctor(s)...")

    # Check each doctor
    results = []
    for doctor in doctors:
        if not args.json:
            specialty_note = f" [{doctor.specialty}]" if doctor.specialty else ""
            print(f"  Searching: {doctor.name}{specialty_note}")
        result = check_doctor(doctor, lat, lon, args.radius, args.city)
        results.append(result)

    # Output
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print_results(results, show_urls=not args.no_urls)


if __name__ == "__main__":
    main()
