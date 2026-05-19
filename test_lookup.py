import json
import os
import tempfile
import unittest
from urllib.parse import quote
from unittest.mock import patch

import app as web_app
import check_doctors


class FakeResponse:
    def __init__(self, payload=None, status_code=200, headers=None, text=None, content=None, url="https://example.com"):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(payload or {})
        self.content = content if content is not None else self.text.encode("utf-8")
        self.url = url

    def raise_for_status(self):
        if self.status_code >= 400:
            raise web_app.requests.HTTPError(f"HTTP {self.status_code}")
        return None

    def json(self):
        return self.payload

    def iter_content(self, chunk_size=8192):
        for index in range(0, len(self.content), chunk_size):
            yield self.content[index:index + chunk_size]


FACILITY_PAYLOAD = {
    "results": [
        {
            "number": "1234567890",
            "basic": {"organization_name": "BAYLOR HOSPITAL"},
            "addresses": [
                {
                    "address_purpose": "LOCATION",
                    "address_1": "3500 Gaston Ave",
                    "city": "DALLAS",
                    "state": "TX",
                    "postal_code": "752010000",
                    "telephone_number": "555-0100",
                }
            ],
            "taxonomies": [
                {"primary": True, "desc": "General Acute Care Hospital"}
            ],
        }
    ]
}


class LookupTests(unittest.TestCase):
    def test_web_search_npi_uses_type_two_for_facilities(self):
        with patch.object(web_app.requests, "get") as mock_get:
            mock_get.return_value = FakeResponse(FACILITY_PAYLOAD)

            results = web_app.search_npi("Baylor Hospital", provider_type="facility")

        params = mock_get.call_args.kwargs["params"]
        self.assertEqual(params["enumeration_type"], "NPI-2")
        self.assertEqual(params["organization_name"], "Baylor Hospital")
        self.assertNotIn("first_name", params)
        self.assertNotIn("last_name", params)
        self.assertEqual(results[0]["name"], "BAYLOR HOSPITAL")
        self.assertEqual(results[0]["address"], "3500 Gaston Ave, DALLAS, TX 75201")

    def test_web_route_auto_resolves_mixed_provider_types(self):
        def fake_search(name, **kwargs):
            if name == "John Smith" and kwargs["provider_type"] == "doctor":
                return [{"npi": "1", "name": "John Smith"}]
            if name == "Baylor Hospital" and kwargs["provider_type"] == "facility":
                return [{"npi": "2", "name": "BAYLOR HOSPITAL"}]
            return []

        with patch.object(web_app, "search_npi", side_effect=fake_search), \
             patch.object(web_app, "check_network_statuses", return_value={}):
            client = web_app.app.test_client()
            response = client.get("/search?providers=John+Smith,Baylor+Hospital")

        self.assertEqual(response.status_code, 200)
        providers = response.json["providers"]
        self.assertEqual(providers[0]["provider"], "John Smith")
        self.assertEqual(providers[0]["provider_type"], "doctor")
        self.assertEqual(providers[1]["provider"], "Baylor Hospital")
        self.assertEqual(providers[1]["provider_type"], "facility")

    def test_web_route_resolves_doctors_and_facilities_separately(self):
        def fake_search(name, **kwargs):
            if name == "Maria Garcia" and kwargs["provider_type"] == "doctor":
                return [{"npi": "1", "name": "Maria Garcia"}]
            if name == "Baylor Scott White" and kwargs["provider_type"] == "facility":
                return [{"npi": "2", "name": "BAYLOR SCOTT WHITE"}]
            return []

        prescription = {"prescription": "Ozempic", "drug_found": False}

        with patch.object(web_app, "search_npi", side_effect=fake_search), \
             patch.object(web_app, "check_network_statuses", return_value={}), \
             patch.object(web_app, "resolve_prescription", return_value=prescription), \
             patch.object(web_app, "check_prescription_statuses", return_value={}):
            client = web_app.app.test_client()
            response = client.get(
                "/search?doctors=Maria+Garcia&facilities=Baylor+Scott+White&prescriptions=Ozempic"
            )

        self.assertEqual(response.status_code, 200)
        providers = response.json["providers"]
        self.assertEqual(providers[0]["provider"], "Maria Garcia")
        self.assertEqual(providers[0]["provider_type"], "doctor")
        self.assertEqual(providers[0]["provider_group"], "doctor")
        self.assertEqual(providers[1]["provider"], "Baylor Scott White")
        self.assertEqual(providers[1]["provider_type"], "facility")
        self.assertEqual(providers[1]["provider_group"], "facility")

    def test_provider_search_route_returns_facility_picker_candidates(self):
        with patch.object(web_app.requests, "get") as mock_get:
            mock_get.return_value = FakeResponse(FACILITY_PAYLOAD)
            client = web_app.app.test_client()
            response = client.get("/providers/search?q=Baylor&type=facility&city=Dallas")

        self.assertEqual(response.status_code, 200)
        provider = response.json["providers"][0]
        self.assertEqual(provider["npi"], "1234567890")
        self.assertEqual(provider["display_name"], "BAYLOR HOSPITAL")
        self.assertEqual(provider["provider_type"], "facility")
        self.assertEqual(provider["address"], "3500 Gaston Ave, DALLAS, TX 75201")

    def test_route_uses_exact_facility_selection_npi(self):
        selection = [{
            "npi": "9876543210",
            "name": "BAYLOR HOSPITAL",
            "display_name": "BAYLOR HOSPITAL",
            "specialty": "General Acute Care Hospital",
            "address": "3500 Gaston Ave, DALLAS, TX 75201",
            "location": "DALLAS, TX 75201",
            "provider_type": "facility",
        }]

        with patch.object(web_app, "search_npi", return_value=[]), \
             patch.object(web_app, "check_network_statuses", return_value={}):
            client = web_app.app.test_client()
            response = client.get(
                "/search?facilities=BAYLOR+HOSPITAL"
                "&facility_selections=" + quote(json.dumps(selection))
            )

        self.assertEqual(response.status_code, 200)
        provider = response.json["providers"][0]
        self.assertEqual(provider["provider_type"], "facility")
        self.assertEqual(provider["npi_count"], 1)
        self.assertEqual(provider["npi_results"][0]["npi"], "9876543210")

    def test_facility_search_queries_include_baylor_scott_white_aliases(self):
        queries = web_app.facility_search_queries("bailer scott and white")

        self.assertIn("baylor scott and white", queries)
        self.assertIn("Baylor Scott White", queries)
        self.assertIn("Baylor University Medical Center", queries)

    def test_auto_prefers_facility_for_facility_like_query_even_with_doctor_match(self):
        def fake_search(name, **kwargs):
            if kwargs["provider_type"] == "doctor":
                return [{"npi": "1", "name": "Baylor White"}]
            if kwargs["provider_type"] == "facility":
                return [{"npi": "2", "name": "BAYLOR UNIVERSITY MEDICAL CENTER"}]
            return []

        with patch.object(web_app, "search_npi", side_effect=fake_search):
            results, provider_type = web_app.resolve_provider_npi("scott white")

        self.assertEqual(provider_type, "facility")
        self.assertEqual(results[0]["name"], "BAYLOR UNIVERSITY MEDICAL CENTER")

    def test_web_route_tags_unresolved_provider_as_not_found(self):
        with patch.object(web_app, "search_npi", return_value=[]):
            client = web_app.app.test_client()
            response = client.get("/search?providers=Unknown+Provider")

        self.assertEqual(response.status_code, 200)
        provider = response.json["providers"][0]
        self.assertEqual(provider["provider_type"], "not_found")
        self.assertFalse(provider["npi_found"])

    def test_web_route_returns_network_columns(self):
        with patch.object(web_app, "search_npi", return_value=[]):
            client = web_app.app.test_client()
            response = client.get("/search?providers=John+Smith")

        network_ids = [network["id"] for network in response.json["providers"][0]["networks"]]
        self.assertEqual(
            network_ids,
            [
                "bcbstx:blue_advantage_hmo",
                "bcbstx:my_blue_health",
                "uhc:tx_individual_exchange",
                "uhc:tx_kelsey_seybold",
                "uhc:tx_sanitas_anchor",
            ],
        )

    def test_bcbstx_networks_include_marketplace_network_url_filters(self):
        networks = web_app.build_networks(
            web_app.generate_bcbstx_urls("", 0, 0, 25),
            web_app.generate_uhc_urls(),
        )
        filters = {
            network["id"]: network.get("marketplace_network_url_contains")
            for network in networks
        }

        self.assertEqual(
            filters["bcbstx:blue_advantage_hmo"],
            "tx-blueadvantage-retail",
        )
        self.assertEqual(filters["bcbstx:my_blue_health"], "tx-myblue-health")

    def test_web_route_returns_network_statuses(self):
        statuses = {
            "uhc:tx_individual_exchange": {
                "status": "in",
                "source": "UHC directory",
                "detail": "Matched by NPI.",
            }
        }

        with patch.object(web_app, "search_npi", return_value=[{"npi": "1", "name": "John Smith"}]), \
             patch.object(web_app, "check_network_statuses", return_value=statuses):
            client = web_app.app.test_client()
            response = client.get("/search?providers=John+Smith")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["providers"][0]["network_statuses"], statuses)

    def test_web_route_returns_carrier_source_map(self):
        with patch.object(web_app, "search_npi", return_value=[]):
            client = web_app.app.test_client()
            response = client.get("/search?providers=Unknown+Provider")

        self.assertEqual(response.status_code, 200)
        sources = response.json["sources"]
        carriers = [group["carrier"] for group in sources]
        self.assertEqual(
            carriers,
            ["BCBS", "UHC"],
        )

        flat_sources = [
            source
            for group in sources
            for source in group["sources"]
        ]
        self.assertEqual(len(flat_sources), 11)
        self.assertTrue(all(source["url"] for source in flat_sources))
        self.assertTrue(all(source["checked_on"] == "2026-05-13" for source in flat_sources))
        self.assertTrue(all(source["validation"].startswith("HTTP 200") for source in flat_sources))
        self.assertTrue(all(source["description"] for source in flat_sources))

        identifiers = " ".join(source["identifier"] for source in flat_sources)
        self.assertIn("network_id=1000128", identifiers)
        self.assertIn("GPX526TX", identifiers)

        descriptions = " ".join(source["description"] for source in flat_sources)
        self.assertIn("manual cross-check", descriptions)
        self.assertIn("manual confirmation", descriptions)
        self.assertIn("manually confirm drug coverage", descriptions)

        questions = " ".join(
            question
            for group in sources
            for question in group["open_questions"]
        )
        self.assertIn("2027", questions)
        self.assertIn("coordinate-dependent", questions)
        self.assertIn("behavioral directories", questions)

    def test_web_route_filters_networks_and_sources_by_selected_carrier(self):
        with patch.object(web_app, "search_npi", return_value=[]):
            client = web_app.app.test_client()
            response = client.get(
                "/search?providers=Unknown+Provider"
                "&carrier_filter_submitted=true&carriers=bcbstx"
            )

        self.assertEqual(response.status_code, 200)
        network_ids = [
            network["id"]
            for network in response.json["providers"][0]["networks"]
        ]
        self.assertEqual(
            network_ids,
            ["bcbstx:blue_advantage_hmo", "bcbstx:my_blue_health"],
        )
        self.assertEqual(
            [group["carrier"] for group in response.json["sources"]],
            ["BCBS"],
        )

    def test_source_freshness_check_records_metadata(self):
        source_groups = [{
            "carrier": "Test Carrier",
            "sources": [
                web_app.provider_source(
                    "Test Directory",
                    "test-directory",
                    "https://example.com/directory",
                    web_app.CMS_PRIMARY_ROLE,
                    "Test note.",
                )
            ],
            "open_questions": [],
        }]
        puf_html = (
            "<html><body>"
            "Plan Attributes PUF <span>Updated May 1, 2026</span>"
            "Network PUF <span>Updated May 2, 2026</span>"
            "</body></html>"
        )

        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(web_app, "SOURCE_FRESHNESS_CACHE_PATH", os.path.join(tmpdir, "sources.json")), \
             patch.object(web_app, "CARRIER_SOURCE_GROUPS", source_groups), \
             patch.object(web_app.requests, "head") as mock_head, \
             patch.object(web_app.requests, "get") as mock_get:
            mock_head.return_value = FakeResponse(
                status_code=200,
                headers={
                    "ETag": '"abc"',
                    "Last-Modified": "Wed, 01 May 2026 12:00:00 GMT",
                    "Content-Length": "1234",
                    "Content-Type": "text/html",
                },
                url="https://example.com/directory",
            )
            mock_get.return_value = FakeResponse(text=puf_html)

            cache = web_app.check_source_freshness({})

            self.assertTrue(os.path.exists(web_app.SOURCE_FRESHNESS_CACHE_PATH))

        self.assertEqual(cache["sources"][0]["status"], "ok")
        self.assertEqual(cache["sources"][0]["etag"], '"abc"')
        self.assertEqual(cache["sources"][0]["content_length"], "1234")
        self.assertEqual(cache["puf"]["updates"]["Network PUF"], "May 2, 2026")
        self.assertEqual(mock_head.call_args.args[0], "https://example.com/directory")

    def test_search_route_does_not_trigger_source_freshness_check(self):
        with patch.object(web_app, "search_npi", return_value=[]), \
             patch.object(web_app, "check_network_statuses", return_value={}), \
             patch.object(web_app, "check_source_freshness") as mock_check:
            client = web_app.app.test_client()
            response = client.get("/search?providers=Unknown+Provider")

        self.assertEqual(response.status_code, 200)
        mock_check.assert_not_called()

    def test_source_status_route_reads_committed_snapshot_without_refresh(self):
        cache = {
            "last_checked": web_app.utc_now_iso(),
            "ttl_seconds": web_app.SOURCE_FRESHNESS_TTL_SECONDS,
            "puf": {"status": "ok"},
            "sources": [{"status": "ok"}],
        }

        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(web_app, "SOURCE_FRESHNESS_CACHE_PATH", os.path.join(tmpdir, "sources.json")), \
             patch.object(web_app, "check_source_freshness") as mock_check:
            with open(web_app.SOURCE_FRESHNESS_CACHE_PATH, "w", encoding="utf-8") as handle:
                json.dump(cache, handle)
            client = web_app.app.test_client()
            response = client.get("/sources/status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "fresh")
        self.assertEqual(response.json["counts"]["ok"], 1)
        self.assertFalse(response.json["in_progress"])
        mock_check.assert_not_called()

    def test_marketplace_plan_lookup_filters_matching_plan_ids(self):
        web_app.get_marketplace_plan_ids.cache_clear()
        first_page = {
            "plans": [
                {
                    "id": "match-1",
                    "name": "Blue Advantage Bronze HMO Standard",
                    "network_url": "https://example.com/blue",
                },
                *[
                    {
                        "id": f"other-{index}",
                        "name": "MyBlue Health Bronze",
                        "network_url": "https://example.com/myblue",
                    }
                    for index in range(9)
                ],
            ]
        }
        second_page = {
            "plans": [
                {
                    "id": "match-2",
                    "name": "Blue Advantage Silver HMO 205",
                    "network_url": "https://example.com/blue",
                }
            ]
        }

        with patch.object(web_app.requests, "post") as mock_post:
            mock_post.side_effect = [FakeResponse(first_page), FakeResponse(second_page)]
            plan_ids = web_app.get_marketplace_plan_ids(
                "Blue Cross and Blue Shield of Texas",
                2026,
                "77030",
                "48201",
                "TX",
                ("blue advantage", "hmo"),
                "",
            )

        offsets = [call.kwargs["json"]["offset"] for call in mock_post.call_args_list]
        self.assertEqual(plan_ids, ("match-1", "match-2"))
        self.assertEqual(offsets, [0, 10])

    def test_marketplace_lookup_matches_npi_and_marks_in_network(self):
        marketplace_payload = {
            "coverage": [
                {
                    "npi": "1548387418",
                    "plan_id": "33602TX0461041",
                    "coverage": "Covered",
                }
            ]
        }

        network = {
            "id": "bcbstx:blue_advantage_hmo",
            "name": "Blue Advantage HMO",
            "marketplace_issuer": "Blue Cross and Blue Shield of Texas",
            "plan_year": 2026,
        }
        place = {"zipcode": "77030", "countyfips": "48201", "state": "TX"}

        with patch.object(web_app, "get_marketplace_plan_ids", return_value=("33602TX0461041",)), \
             patch.object(web_app.requests, "get") as mock_get:
            mock_get.return_value = FakeResponse(marketplace_payload)
            status = web_app.check_marketplace_network_status(
                "Houston Methodist Hospital",
                "facility",
                [{"npi": "1548387418"}],
                network,
                place,
            )

        self.assertEqual(status["status"], "in")
        self.assertEqual(status["source"], "CMS Marketplace API")
        params = mock_get.call_args.kwargs["params"]
        self.assertEqual(params["providerids"], "1548387418")
        self.assertEqual(params["planids"], "33602TX0461041")

    def test_marketplace_lookup_marks_partial_coverage_when_some_provider_plans_are_covered(self):
        marketplace_payload = {
            "coverage": [
                {
                    "npi": "1548387418",
                    "plan_id": "plan-1",
                    "coverage": "Covered",
                },
                {
                    "npi": "1548387418",
                    "plan_id": "plan-2",
                    "coverage": "NotCovered",
                },
            ]
        }

        network = {
            "id": "bcbstx:blue_advantage_hmo",
            "name": "Blue Advantage HMO",
            "marketplace_issuer": "Blue Cross and Blue Shield of Texas",
            "plan_year": 2026,
        }
        place = {"zipcode": "77030", "countyfips": "48201", "state": "TX"}

        with patch.object(web_app, "get_marketplace_plan_ids", return_value=("plan-1", "plan-2")), \
             patch.object(web_app.requests, "get") as mock_get:
            mock_get.return_value = FakeResponse(marketplace_payload)
            status = web_app.check_marketplace_network_status(
                "Houston Methodist Hospital",
                "facility",
                [{"npi": "1548387418"}],
                network,
                place,
            )

        self.assertEqual(status["status"], "partial_coverage")
        self.assertIn("1 of 2 matching plan IDs", status["detail"])

    def test_marketplace_lookup_marks_likely_in_when_only_some_npis_are_covered(self):
        marketplace_payload = {
            "coverage": [
                {
                    "npi": "1548387418",
                    "plan_id": "plan-1",
                    "coverage": "Covered",
                }
            ]
        }

        network = {
            "id": "bcbstx:blue_advantage_hmo",
            "name": "Blue Advantage HMO",
            "marketplace_issuer": "Blue Cross and Blue Shield of Texas",
            "plan_year": 2026,
        }
        place = {"zipcode": "77030", "countyfips": "48201", "state": "TX"}

        with patch.object(web_app, "get_marketplace_plan_ids", return_value=("plan-1",)), \
             patch.object(web_app.requests, "get") as mock_get:
            mock_get.return_value = FakeResponse(marketplace_payload)
            status = web_app.check_marketplace_network_status(
                "Methodist Hospital",
                "facility",
                [{"npi": "1548387418"}, {"npi": "1295925436"}],
                network,
                place,
            )

        self.assertEqual(status["status"], "likely_in")
        self.assertIn("1 of 2 NPI matches", status["detail"])

    def test_marketplace_lookup_chunks_plan_ids_for_coverage_api(self):
        plan_ids = tuple(f"plan-{index}" for index in range(11))
        network = {
            "id": "uhc:tx_individual_exchange",
            "name": "UHC TX Individual Exchange",
            "marketplace_issuer": "UnitedHealthcare",
            "plan_year": 2026,
        }
        place = {"zipcode": "78205", "countyfips": "48029", "state": "TX"}

        with patch.object(web_app, "get_marketplace_plan_ids", return_value=plan_ids), \
             patch.object(web_app.requests, "get") as mock_get:
            mock_get.side_effect = [
                FakeResponse({"coverage": []}),
                FakeResponse({
                    "coverage": [
                        {"npi": "1548387418", "plan_id": "plan-10", "coverage": "Covered"}
                    ]
                }),
            ]

            status = web_app.check_marketplace_network_status(
                "Houston Methodist Hospital",
                "facility",
                [{"npi": "1548387418"}],
                network,
                place,
            )

        planid_calls = [call.kwargs["params"]["planids"] for call in mock_get.call_args_list]
        self.assertEqual(status["status"], "partial_coverage")
        self.assertEqual(len(mock_get.call_args_list), 2)
        self.assertEqual(planid_calls[0], ",".join(plan_ids[:10]))
        self.assertEqual(planid_calls[1], "plan-10")

    def test_marketplace_lookup_marks_network_not_offered_when_no_plans_match(self):
        network = {
            "id": "uhc:tx_kelsey_seybold",
            "name": "UHC TX Kelsey-Seybold",
            "marketplace_issuer": "UnitedHealthcare",
            "plan_year": 2026,
        }
        place = {"zipcode": "78205", "countyfips": "48029", "state": "TX"}

        with patch.object(web_app, "get_marketplace_plan_ids", return_value=()):
            status = web_app.check_marketplace_network_status(
                "Methodist Hospital",
                "facility",
                [{"npi": "1295925436"}],
                network,
                place,
            )

        self.assertEqual(status["status"], "not_offered")
        self.assertIn("No matching Marketplace plans", status["detail"])

    def test_marketplace_lookup_marks_request_failures_as_lookup_error(self):
        network = {
            "id": "uhc:tx_individual_exchange",
            "name": "UHC TX Individual Exchange",
            "marketplace_issuer": "UnitedHealthcare",
            "plan_year": 2026,
        }
        place = {"zipcode": "78205", "countyfips": "48029", "state": "TX"}

        with patch.object(web_app, "get_marketplace_plan_ids", return_value=("plan-1",)), \
             patch.object(web_app.requests, "get", side_effect=web_app.requests.RequestException("timeout")):
            status = web_app.check_marketplace_network_status(
                "Methodist Hospital",
                "facility",
                [{"npi": "1295925436"}],
                network,
                place,
            )

        self.assertEqual(status["status"], "lookup_error")
        self.assertIn("coverage lookup failed", status["detail"])

    def test_drug_search_resolves_rxcui_from_marketplace_autocomplete(self):
        drug_payload = [
            {
                "rxcui": "197805",
                "name": "Ibuprofen",
                "strength": "400 mg",
                "route": "Oral Pill",
                "full_name": "ibuprofen 400 MG Oral Tablet",
            }
        ]

        with patch.object(web_app.requests, "get") as mock_get:
            mock_get.return_value = FakeResponse(drug_payload)
            result = web_app.resolve_prescription("Ibuprofen")

        params = mock_get.call_args.kwargs["params"]
        self.assertTrue(result["drug_found"])
        self.assertEqual(result["rxcui"], "197805")
        self.assertEqual(params["q"], "Ibuprofen")

    def test_drug_search_route_returns_picker_candidates(self):
        drug_payload = [
            {
                "rxcui": "259255",
                "name": "Atorvastatin",
                "strength": "80 mg",
                "route": "Oral Pill",
                "full_name": "atorvastatin 80 MG Oral Tablet",
                "rxnorm_dose_form": "Oral Tablet",
            },
            {
                "rxcui": "153165",
                "name": "LIPITOR",
                "strength": "20 mg",
                "route": "Oral Pill",
                "full_name": "atorvastatin 20 MG Oral Tablet [Lipitor]",
                "rxnorm_dose_form": "Oral Tablet",
            },
        ]

        with patch.object(web_app.requests, "get") as mock_get:
            mock_get.return_value = FakeResponse(drug_payload)
            client = web_app.app.test_client()
            response = client.get("/drugs/search?q=atorvastatin")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["drugs"][0]["rxcui"], "259255")
        self.assertEqual(response.json["drugs"][0]["coverage_type"], "Generic")
        self.assertEqual(response.json["drugs"][1]["coverage_type"], "Branded")
        self.assertIn("Atorvastatin 80 mg Oral Tablet", response.json["drugs"][0]["display_name"])

    def test_route_uses_exact_prescription_selection_rxcui(self):
        selection = [{
            "rxcui": "259255",
            "name": "Atorvastatin",
            "strength": "80 mg",
            "route": "Oral Pill",
            "full_name": "atorvastatin 80 MG Oral Tablet",
            "display_name": "Atorvastatin 80 mg Oral Tablet",
            "coverage_type": "Generic",
            "dose_form": "Oral Tablet",
        }]

        with patch.object(web_app, "search_npi", return_value=[]), \
             patch.object(web_app, "check_prescription_statuses", return_value={}):
            client = web_app.app.test_client()
            response = client.get(
                "/search?prescriptions=Atorvastatin+80+mg+Oral+Tablet"
                "&prescription_selections=" + quote(json.dumps(selection))
            )

        self.assertEqual(response.status_code, 200)
        prescription = response.json["prescriptions"][0]
        self.assertEqual(prescription["rxcui"], "259255")
        self.assertTrue(prescription["selected_from_picker"])
        self.assertEqual(prescription["drug_match_count"], 1)

    def test_prescription_resolution_flags_combination_match_for_broad_input(self):
        drug_payload = [
            {
                "rxcui": "1043563",
                "name": "metFORMIN/sAXagliptin XR",
                "strength": "1,000-2.5 mg",
                "route": "Oral Pill",
                "full_name": "metformin hydrochloride 1000 MG / saxagliptin 2.5 MG Extended Release Oral Tablet",
            }
        ]

        with patch.object(web_app.requests, "get") as mock_get:
            mock_get.return_value = FakeResponse(drug_payload)
            result = web_app.resolve_prescription("Metformin")

        self.assertTrue(result["selected_is_combination"])
        self.assertIn("combination-product RxCUI", result["drug_match_warning"])

    def test_prescription_resolution_does_not_treat_dosage_unit_slash_as_combination(self):
        drug_payload = [
            {
                "rxcui": "1991311",
                "name": "OZEMPIC",
                "strength": "1.34 mg/ml",
                "route": "Injectable",
                "full_name": "semaglutide 1.34 MG/ML Auto-Injector [Ozempic]",
            }
        ]

        with patch.object(web_app.requests, "get") as mock_get:
            mock_get.return_value = FakeResponse(drug_payload)
            result = web_app.resolve_prescription("Ozempic")

        self.assertFalse(result["selected_is_combination"])
        self.assertEqual(result["drug_match_warning"], "")

    def test_marketplace_drug_lookup_marks_generic_coverage(self):
        network = {
            "id": "bcbstx:blue_advantage_hmo",
            "name": "Blue Advantage HMO",
            "marketplace_issuer": "Blue Cross and Blue Shield of Texas",
            "plan_year": 2026,
        }
        place = {"zipcode": "77030", "countyfips": "48201", "state": "TX"}
        prescription = {
            "prescription": "Brand Drug",
            "drug_found": True,
            "rxcui": "12345",
            "drug_name": "Brand Drug 10 mg Oral Pill",
        }
        coverage_payload = {
            "coverage": [
                {
                    "rxcui": "12345",
                    "plan_id": "plan-1",
                    "coverage": "GenericCovered",
                    "generic_rxcui": "67890",
                }
            ]
        }

        with patch.object(web_app, "get_marketplace_plan_ids", return_value=("plan-1",)), \
             patch.object(web_app.requests, "get") as mock_get:
            mock_get.return_value = FakeResponse(coverage_payload)
            status = web_app.check_marketplace_drug_status(
                prescription, network, place
            )

        self.assertEqual(status["status"], "generic_covered")
        self.assertIn("Generic RxCUI: 67890", status["detail"])

    def test_marketplace_drug_lookup_keeps_combination_match_suspect_when_covered(self):
        network = {
            "id": "bcbstx:blue_advantage_hmo",
            "name": "Blue Advantage HMO",
            "marketplace_issuer": "Blue Cross and Blue Shield of Texas",
            "plan_year": 2026,
        }
        place = {"zipcode": "77030", "countyfips": "48201", "state": "TX"}
        prescription = {
            "prescription": "Metformin",
            "drug_found": True,
            "drug_match_count": 3,
            "rxcui": "1043563",
            "drug_name": "metFORMIN/sAXagliptin XR 1,000-2.5 mg Oral Pill",
            "selected_is_combination": True,
            "drug_match_warning": "CMS autocomplete selected a combination-product RxCUI for broad input 'Metformin'; standalone drug coverage needs direct formulary confirmation. ",
        }
        coverage_payload = {
            "coverage": [
                {"rxcui": "1043563", "plan_id": "plan-1", "coverage": "Covered"},
            ]
        }

        with patch.object(web_app, "get_marketplace_plan_ids", return_value=("plan-1",)), \
             patch.object(web_app.requests, "get") as mock_get:
            mock_get.return_value = FakeResponse(coverage_payload)
            status = web_app.check_marketplace_drug_status(
                prescription, network, place
            )

        self.assertEqual(status["status"], "related_product_covered")
        self.assertIn("related coverage evidence", status["detail"])

    def test_marketplace_drug_lookup_marks_review_exact_drug_when_rxcui_match_is_ambiguous(self):
        network = {
            "id": "bcbstx:blue_advantage_hmo",
            "name": "Blue Advantage HMO",
            "marketplace_issuer": "Blue Cross and Blue Shield of Texas",
            "plan_year": 2026,
        }
        place = {"zipcode": "77030", "countyfips": "48201", "state": "TX"}
        prescription = {
            "prescription": "Ibuprofen",
            "drug_found": True,
            "drug_match_count": 3,
            "rxcui": "12345",
            "drug_name": "Ibuprofen 200 mg Oral Pill",
        }
        coverage_payload = {
            "coverage": [
                {"rxcui": "12345", "plan_id": "plan-1", "coverage": "Covered"},
            ]
        }

        with patch.object(web_app, "get_marketplace_plan_ids", return_value=("plan-1",)), \
             patch.object(web_app.requests, "get") as mock_get:
            mock_get.return_value = FakeResponse(coverage_payload)
            status = web_app.check_marketplace_drug_status(
                prescription, network, place
            )

        self.assertEqual(status["status"], "review_exact_drug")
        self.assertIn("Selected RxCUI 12345 from 3 CMS autocomplete matches", status["detail"])
        self.assertIn("Confirm exact product, strength, form, and tier", status["detail"])

    def test_marketplace_drug_lookup_includes_tier_when_source_provides_it(self):
        network = {
            "id": "bcbstx:blue_advantage_hmo",
            "name": "Blue Advantage HMO",
            "marketplace_issuer": "Blue Cross and Blue Shield of Texas",
            "plan_year": 2026,
        }
        place = {"zipcode": "77030", "countyfips": "48201", "state": "TX"}
        prescription = {
            "prescription": "Atorvastatin 80 mg Oral Tablet",
            "drug_found": True,
            "rxcui": "259255",
            "drug_name": "Atorvastatin 80 mg Oral Pill",
        }
        coverage_payload = {
            "coverage": [
                {
                    "rxcui": "259255",
                    "plan_id": "plan-1",
                    "coverage": "Covered",
                    "tier": "1",
                    "prior_authorization": True,
                    "quantity_limit": "true",
                },
            ]
        }

        with patch.object(web_app, "get_marketplace_plan_ids", return_value=("plan-1",)), \
             patch.object(web_app.requests, "get") as mock_get:
            mock_get.return_value = FakeResponse(coverage_payload)
            status = web_app.check_marketplace_drug_status(
                prescription, network, place
            )

        self.assertEqual(status["status"], "drug_covered")
        self.assertEqual(status["tier_label"], "T1")
        self.assertEqual(status["tier_detail"], "Tier: Tier 1")
        self.assertEqual(status["restriction_label"], "PA QL")
        self.assertIn("Tier: Tier 1", status["detail"])

    def test_marketplace_drug_lookup_marks_other_form_covered_when_related_rxcui_is_covered(self):
        network = {
            "id": "bcbstx:blue_advantage_hmo",
            "name": "Blue Advantage HMO",
            "marketplace_issuer": "Blue Cross and Blue Shield of Texas",
            "plan_year": 2026,
        }
        place = {"zipcode": "77030", "countyfips": "48201", "state": "TX"}
        prescription = {
            "prescription": "Metformin",
            "drug_found": True,
            "drug_match_count": 2,
            "rxcui": "selected",
            "drug_name": "Metformin 500 mg Oral Tablet",
            "drug_results": [
                {"rxcui": "selected", "name": "Metformin", "strength": "500 mg", "route": "Oral Tablet"},
                {"rxcui": "related", "name": "Metformin", "strength": "850 mg", "route": "Oral Tablet"},
            ],
        }
        coverage_payload = {
            "coverage": [
                {"rxcui": "selected", "plan_id": "plan-1", "coverage": "NotCovered"},
                {"rxcui": "related", "plan_id": "plan-1", "coverage": "Covered"},
            ]
        }

        with patch.object(web_app, "get_marketplace_plan_ids", return_value=("plan-1",)), \
             patch.object(web_app.requests, "get") as mock_get:
            mock_get.return_value = FakeResponse(coverage_payload)
            status = web_app.check_marketplace_drug_status(
                prescription, network, place
            )

        self.assertEqual(status["status"], "other_form_covered")
        self.assertIn("other strength/form RxCUI", status["detail"])
        self.assertIn("Confirm exact product, strength, form, and tier", status["detail"])
        self.assertEqual(mock_get.call_args.kwargs["params"]["drugs"], "selected,related")

    def test_marketplace_drug_lookup_marks_ambiguous_no_record_as_suspect(self):
        network = {
            "id": "bcbstx:blue_advantage_hmo",
            "name": "Blue Advantage HMO",
            "marketplace_issuer": "Blue Cross and Blue Shield of Texas",
            "plan_year": 2026,
        }
        place = {"zipcode": "77030", "countyfips": "48201", "state": "TX"}
        prescription = {
            "prescription": "Ibuprofen",
            "drug_found": True,
            "drug_match_count": 2,
            "rxcui": "selected",
            "drug_name": "Ibuprofen 200 mg Oral Tablet",
            "drug_results": [
                {"rxcui": "selected", "name": "Ibuprofen", "strength": "200 mg", "route": "Oral Tablet"},
                {"rxcui": "related", "name": "Ibuprofen", "strength": "400 mg", "route": "Oral Tablet"},
            ],
        }

        with patch.object(web_app, "get_marketplace_plan_ids", return_value=("plan-1",)), \
             patch.object(web_app.requests, "get") as mock_get:
            mock_get.return_value = FakeResponse({"coverage": []})
            status = web_app.check_marketplace_drug_status(
                prescription, network, place
            )

        self.assertEqual(status["status"], "suspect")
        self.assertIn("Exact strength/form may differ", status["detail"])
        self.assertEqual(mock_get.call_args.kwargs["params"]["drugs"], "selected,related")

    def test_marketplace_drug_lookup_marks_partial_coverage_when_some_plans_cover_drug(self):
        network = {
            "id": "bcbstx:blue_advantage_hmo",
            "name": "Blue Advantage HMO",
            "marketplace_issuer": "Blue Cross and Blue Shield of Texas",
            "plan_year": 2026,
        }
        place = {"zipcode": "77030", "countyfips": "48201", "state": "TX"}
        prescription = {
            "prescription": "Ibuprofen",
            "drug_found": True,
            "rxcui": "12345",
            "drug_name": "Ibuprofen 200 mg Oral Pill",
        }
        coverage_payload = {
            "coverage": [
                {"rxcui": "12345", "plan_id": "plan-1", "coverage": "Covered"},
                {"rxcui": "12345", "plan_id": "plan-2", "coverage": "NotCovered"},
            ]
        }

        with patch.object(web_app, "get_marketplace_plan_ids", return_value=("plan-1", "plan-2")), \
             patch.object(web_app.requests, "get") as mock_get:
            mock_get.return_value = FakeResponse(coverage_payload)
            status = web_app.check_marketplace_drug_status(
                prescription, network, place
            )

        self.assertEqual(status["status"], "partial_coverage")
        self.assertIn("1 of 2 matching plan IDs", status["detail"])

    def test_route_returns_prescription_results_and_network_statuses(self):
        prescription = {
            "prescription": "Ibuprofen",
            "drug_found": True,
            "rxcui": "197805",
            "drug_name": "Ibuprofen 400 mg Oral Pill",
        }

        with patch.object(web_app, "search_npi", return_value=[]), \
             patch.object(web_app, "resolve_prescription", return_value=prescription), \
             patch.object(web_app, "check_prescription_statuses", return_value={"bcbstx:blue_advantage_hmo": {"status": "drug_covered"}}):
            client = web_app.app.test_client()
            response = client.get("/search?prescriptions=Ibuprofen")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["providers"], [])
        self.assertEqual(response.json["prescriptions"][0]["rxcui"], "197805")
        self.assertEqual(
            response.json["prescriptions"][0]["network_statuses"]["bcbstx:blue_advantage_hmo"]["status"],
            "drug_covered",
        )

    def test_home_page_has_network_status_matrix_markup(self):
        client = web_app.app.test_client()
        response = client.get("/")
        html = response.get_data(as_text=True)

        self.assertIn("Evidence · coverage matrix", html)
        self.assertIn("every provider × every plan, with source authority", html)
        self.assertIn("Row total", html)
        self.assertIn("matrix-status-card", html)
        self.assertIn("carrier-covered", html)
        self.assertIn("matrix-legend", html)
        self.assertIn("legendStatusTooltip", html)
        self.assertIn("Carrier portal or formulary should be treated as final", html)
        self.assertNotIn("Practical lead", html)
        self.assertNotIn("No clean winner", html)
        self.assertNotIn("Client-safe summary", html)
        self.assertIn("Case · Provider Network Checker", html)
        self.assertIn("Run check", html)
        self.assertIn("Coverage by individual NPI match against carrier rosters", html)
        self.assertIn("Coverage by organization NPI match against carrier rosters", html)
        self.assertIn("Doctors", html)
        self.assertIn("Facilities", html)
        self.assertIn("Prescriptions", html)
        self.assertIn("Carriers", html)
        self.assertIn("Blue Cross and Blue Shield of Texas", html)
        self.assertIn("UnitedHealthcare", html)
        self.assertIn('name="carrier_filter_submitted"', html)
        self.assertIn('name="carriers"', html)
        self.assertIn('id="doctors"', html)
        self.assertIn('id="facilities"', html)
        self.assertIn('id="facilitySelections"', html)
        self.assertIn('id="prescriptions"', html)
        self.assertIn('id="prescriptionSelections"', html)
        self.assertIn("Add a facility", html)
        self.assertIn("/providers/search", html)
        self.assertIn("choose the exact facility when the address matters", html)
        self.assertIn("Add a prescription", html)
        self.assertIn("/drugs/search", html)
        self.assertIn("Exact RxCUI selection reduces broad-name ambiguity", html)
        self.assertIn("CMS = screening, carrier formulary = final", html)
        self.assertIn("return 'Generic covered';", html)
        self.assertIn("Review exact drug", html)
        self.assertIn("Other form covered", html)
        self.assertIn("Related product covered", html)
        self.assertIn("No data", html)
        self.assertIn("return 'Likely';", html)
        self.assertIn("Partial coverage", html)
        self.assertIn("Top matches:", html)
        self.assertIn("selected from", html)
        self.assertIn("Suspect", html)
        self.assertIn("Selected RxCUI not covered; exact drug may differ", html)
        self.assertIn("return [info.tier_label, info.restriction_label].filter(Boolean).join(' ');", html)
        self.assertIn("Coverage found; confirm exact NPI/location", html)
        self.assertIn("Coverage evidence found; confirm exact drug/form", html)
        self.assertIn("Only some matched plan IDs covered", html)
        self.assertIn("info-tip", html)
        self.assertIn("item-info", html)
        self.assertIn("lookup-item-heading-row", html)
        self.assertIn("lookup-item-heading", html)
        self.assertIn("lookup-item-meta", html)
        self.assertIn("Last updated", html)
        self.assertNotIn("Check for updates", html)
        self.assertNotIn("freshness-pill", html)
        self.assertIn("/sources/status", html)
        self.assertNotIn("/sources/check", html)
        self.assertIn("renderSourceStatus", html)
        self.assertIn("network-matrix tbody tr.lookup-row:hover td:not(:first-child)", html)
        self.assertIn("border-left: 1px solid #eef2f7", html)
        self.assertIn("data-column-index", html)
        self.assertIn("column-hover", html)
        self.assertIn("network-cell", html)
        self.assertIn("provider-details", html)
        self.assertIn("source-details", html)
        self.assertIn("source-description", html)
        self.assertIn("Carrier source map and open questions", html)
        self.assertIn("renderLookupMatrix", html)
        self.assertIn("renderCarrierSourceDetails", html)
        self.assertIn("<details class=\"provider-details\">", html)
        self.assertNotIn("<details class=\"provider-details\" open>", html)
        self.assertIn("Not offered", html)
        self.assertIn("CMS lookup failed", html)
        self.assertIn("No record", html)
        self.assertIn("No lookup", html)
        self.assertIn("No result", html)
        self.assertIn("network-matrix", html)
        self.assertIn("network-status", html)
        self.assertIn("Dr. John Doe, Maria Garcia", html)
        self.assertIn("Houston Methodist Hospital", html)
        self.assertIn("Kelsey Seybold Clinic", html)
        self.assertIn("These search individual NPI records", html)
        self.assertIn("Search organization NPI records", html)
        self.assertNotIn(">Item</th>", html)
        self.assertNotIn("status-toggle", html)
        self.assertNotIn("providerNetworkStatuses", html)
        self.assertNotIn('label for="provider_type"', html)

    def test_home_page_has_real_sample_scenarios(self):
        client = web_app.app.test_client()
        response = client.get("/")
        html = response.get_data(as_text=True)

        self.assertIn('id="randomDemoBtn"', html)
        self.assertIn('href="?sample=dallas-specialty"', html)
        self.assertIn('href="?sample=houston-mix"', html)
        self.assertIn('href="?sample=austin-mix"', html)
        self.assertIn('href="?sample=san-antonio-wide"', html)
        self.assertIn("Try a mixed provider + prescription sample", html)
        self.assertIn("Random mixed sample", html)
        self.assertIn("Baylor University Medical Center", html)
        self.assertIn("Houston Methodist Hospital", html)
        self.assertIn("St. David's Medical Center", html)
        self.assertIn("University Hospital", html)
        self.assertIn("Ibuprofen, Levothyroxine", html)
        self.assertIn("Ozempic, Metformin", html)
        self.assertIn("Humira, Albuterol", html)
        self.assertIn("Atorvastatin, Lisinopril", html)

    def test_cli_search_npi_uses_type_two_for_facilities(self):
        with patch.object(check_doctors.requests, "get") as mock_get:
            mock_get.return_value = FakeResponse(FACILITY_PAYLOAD)

            results = check_doctors.search_npi(
                "Baylor Hospital", provider_type="facility"
            )

        params = mock_get.call_args.kwargs["params"]
        self.assertEqual(params["enumeration_type"], "NPI-2")
        self.assertEqual(params["organization_name"], "Baylor Hospital")
        self.assertEqual(results[0].name, "BAYLOR HOSPITAL")

    def test_cli_auto_resolves_facility_after_doctor_miss(self):
        with patch.object(check_doctors, "search_npi") as mock_search:
            mock_search.side_effect = [
                [],
                [check_doctors.NPIResult(
                    npi="2",
                    name="BAYLOR HOSPITAL",
                    credential="",
                    specialty="General Acute Care Hospital",
                    address="",
                    city="DALLAS",
                    state="TX",
                    zip_code="75201",
                    phone="",
                )],
            ]

            results, provider_type = check_doctors.resolve_provider_npi("Baylor Hospital")

        self.assertEqual(provider_type, "facility")
        self.assertEqual(results[0].name, "BAYLOR HOSPITAL")


if __name__ == "__main__":
    unittest.main()
