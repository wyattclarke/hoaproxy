from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hoaware.law import _dedupe_citations, _electronic_proxy_status
from scripts.legal.build_source_map import build_source_map, merge_discovered_seeds
from scripts.legal.discover_state_proxy_sources import _extract_links, _fallback_seed_url_for_bucket, _score_bucket
from scripts.legal.extract_rules import _classify_sentence, _dedupe_normalized_rows, _extract_rules_from_text
from scripts.legal.fetch_law_texts import _derived_fallback_urls, _scope_key
from scripts.legal.normalize_law_texts import _is_low_quality_pdf_text, _scope_snapshot_key, _split_sections
from scripts.legal.source_quality import classify_source_quality, extraction_allowed
from scripts.legal.proxy_matrix import evaluate_proxy_coverage


class TestLegalHardening(unittest.TestCase):
    def test_proxy_directed_classification(self) -> None:
        topic, rule = _classify_sentence(
            "A proxy may be directed by the member and must be in writing.",
            bucket="proxy_voting",
        ) or (None, None)
        self.assertEqual(topic, "proxy_voting")
        self.assertEqual(rule, "proxy_directed_option")

    def test_records_deadline_classification(self) -> None:
        topic, rule = _classify_sentence(
            "The association shall provide records within 10 business days after written request.",
            bucket="records_access",
        ) or (None, None)
        self.assertEqual(topic, "records_access")
        self.assertEqual(rule, "records_response_deadline")

    def test_dedupe_normalized_rows_picks_latest(self) -> None:
        rows = [
            {
                "normalized_at": "20260101T000000Z",
                "jurisdiction": "NC",
                "community_type": "hoa",
                "entity_form": "unknown",
                "governing_law_bucket": "records_access",
                "citation": "N.C. Gen. Stat. 47F-3-118",
                "source_url": "https://example.com/law",
                "raw_text_checksum_sha256": "abc",
            },
            {
                "normalized_at": "20260201T000000Z",
                "jurisdiction": "NC",
                "community_type": "hoa",
                "entity_form": "unknown",
                "governing_law_bucket": "records_access",
                "citation": "N.C. Gen. Stat. 47F-3-118",
                "source_url": "https://example.com/law",
                "raw_text_checksum_sha256": "abc",
            },
        ]
        deduped = _dedupe_normalized_rows(rows)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["normalized_at"], "20260201T000000Z")

    def test_citations_use_source_type(self) -> None:
        rows = [
            {
                "citation": "Foo § 1",
                "citation_url": "https://example.com/foo",
                "source_type": "statute",
                "value_text": "Members may inspect records.",
                "last_verified_date": "2026-02-28",
            },
            {
                "citation": "Bar v. Baz",
                "citation_url": "https://example.com/case",
                "source_type": "case_law",
                "value_text": "Court interpreted proxy scope.",
                "last_verified_date": "2026-02-28",
            },
        ]
        citations = _dedupe_citations(rows)
        source_types = {row["source_type"] for row in citations}
        self.assertIn("statute", source_types)
        self.assertIn("case_law", source_types)

    def test_proxy_cluster_evaluation(self) -> None:
        clusters = [
            {"cluster_id": "permission", "any_of": ["proxy_allowed", "proxy_disallowed"]},
            {"cluster_id": "duration", "any_of": ["proxy_validity_duration"]},
        ]
        rules = [
            {"rule_type": "proxy_allowed"},
        ]
        missing, matched = evaluate_proxy_coverage(rules, clusters)
        self.assertEqual(missing, ["duration"])
        self.assertEqual(matched["permission"], ["proxy_allowed"])

    def test_source_map_supports_multiple_seed_sources_per_bucket(self) -> None:
        rows = build_source_map()
        fl_proxy_rows = [
            row
            for row in rows
            if row.get("jurisdiction") == "FL"
            and row.get("community_type") == "hoa"
            and row.get("governing_law_bucket") == "proxy_voting"
            and row.get("retrieval_status") == "seeded"
        ]
        self.assertGreaterEqual(len(fl_proxy_rows), 1)
        fl_seed_rows = [
            row
            for row in rows
            if row.get("jurisdiction") == "FL"
            and row.get("community_type") == "hoa"
            and row.get("retrieval_status") == "seeded"
        ]
        self.assertGreaterEqual(len(fl_seed_rows), 3)

    def test_source_map_loads_registry_seed_rows(self) -> None:
        rows = build_source_map()
        wa_community_rows = [
            row
            for row in rows
            if row.get("jurisdiction") == "WA"
            and row.get("community_type") == "hoa"
            and row.get("governing_law_bucket") == "community_act"
            and row.get("retrieval_status") == "seeded"
        ]
        self.assertGreaterEqual(len(wa_community_rows), 1)

    def test_merge_discovered_accepts_official_fallback_url(self) -> None:
        rows = build_source_map()
        discovered = [
            {
                "jurisdiction": "AL",
                "community_type": "hoa",
                "entity_form": "unknown",
                "governing_law_bucket": "proxy_voting",
                "source_type": "statute",
                "citation": "Alabama proxy voting fallback (seed)",
                "source_url": "http://alisondb.legislature.state.al.us/",
                "publisher": "alisondb.legislature.state.al.us",
                "priority": 96,
                "retrieval_status": "seeded",
                "verification_status": "discovered_unverified",
                "notes": "Fallback emitted from seed URL because bucket discovery returned no ranked candidates; bucket_hint_terms=proxy, vote",
            }
        ]
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "discovered.json"
            p.write_text(json.dumps(discovered), encoding="utf-8")
            merged = merge_discovered_seeds(rows, p)
        self.assertTrue(
            any(
                row.get("jurisdiction") == "AL"
                and row.get("governing_law_bucket") == "proxy_voting"
                and str(row.get("source_url") or "") == "http://alisondb.legislature.state.al.us/"
                for row in merged
            )
        )

    def test_merge_discovered_rejects_aggregator_fallback_without_statute_signals(self) -> None:
        rows = build_source_map()
        discovered = [
            {
                "jurisdiction": "PA",
                "community_type": "hoa",
                "entity_form": "unknown",
                "governing_law_bucket": "proxy_voting",
                "source_type": "secondary_aggregator",
                "citation": "Pennsylvania proxy fallback",
                "source_url": "https://govt.westlaw.com/pac/home",
                "publisher": "govt.westlaw.com",
                "priority": 96,
                "retrieval_status": "seeded",
                "verification_status": "discovered_unverified",
                "notes": "Fallback emitted from seed URL because bucket discovery returned no ranked candidates; bucket_hint_terms=proxy, vote",
            }
        ]
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "discovered.json"
            p.write_text(json.dumps(discovered), encoding="utf-8")
            merged = merge_discovered_seeds(rows, p)
        self.assertFalse(
            any(
                row.get("jurisdiction") == "PA"
                and row.get("governing_law_bucket") == "proxy_voting"
                and str(row.get("source_url") or "") == "https://govt.westlaw.com/pac/home"
                for row in merged
            )
        )

    def test_classifies_electronic_assignment(self) -> None:
        topic, rule = _classify_sentence(
            "Any copy, facsimile transmission, or other reliable reproduction of the original proxy may be substituted.",
            bucket="proxy_voting",
        ) or (None, None)
        self.assertEqual(topic, "proxy_voting")
        self.assertEqual(rule, "proxy_electronic_assignment_allowed")

    def test_classifies_electronic_signature_required_acceptance(self) -> None:
        topic, rule = _classify_sentence(
            "An electronic signature on a proxy shall be accepted by the association.",
            bucket="proxy_voting",
        ) or (None, None)
        self.assertEqual(topic, "proxy_voting")
        self.assertEqual(rule, "proxy_electronic_signature_required_acceptance")

    def test_electronic_proxy_status_priority(self) -> None:
        status = _electronic_proxy_status(
            [
                {"rule_type": "proxy_electronic_assignment_allowed", "citation": "a", "citation_url": "", "value_text": "x"},
                {"rule_type": "proxy_electronic_assignment_required_acceptance", "citation": "b", "citation_url": "", "value_text": "y"},
            ],
            prefix="proxy_electronic_assignment",
        )
        self.assertEqual(status.status, "required_to_accept")

    def test_discovery_extract_links(self) -> None:
        html = """
        <html>
          <a href="/codes/florida/title-xl/chapter-720/section-720-306/">Proxy voting</a>
          <a href="mailto:test@example.com">Mail</a>
          <a href="https://law.justia.com/codes/florida/title-xxxix/chapter-617/">Nonprofit</a>
        </html>
        """
        links = _extract_links(html, "https://law.justia.com/codes/florida/", "florida")
        urls = {url for url, _ in links}
        self.assertIn("https://law.justia.com/codes/florida/title-xl/chapter-720/section-720-306/", urls)
        self.assertIn("https://law.justia.com/codes/florida/title-xxxix/chapter-617/", urls)
        self.assertEqual(len(links), 2)

    def test_discovery_bucket_score_proxy_priority(self) -> None:
        proxy_score = _score_bucket(
            "proxy_voting",
            "https://law.justia.com/codes/florida/title-xl/chapter-720/section-720-306/",
            "Proxies; amendments; and meetings.",
            state_slug="florida",
        )
        electronic_score = _score_bucket(
            "electronic_transactions_overlay",
            "https://law.justia.com/codes/florida/title-xl/chapter-720/section-720-306/",
            "Proxies; amendments; and meetings.",
            state_slug="florida",
        )
        self.assertGreater(proxy_score, electronic_score)

    def test_discovery_fallback_seed_url_prefers_bucket_hints(self) -> None:
        seed_urls = [
            "https://example.state.gov/statutes/",
            "https://example.state.gov/statutes/proxy-voting",
            "https://example.state.gov/statutes/electronic-signatures",
        ]
        proxy_url = _fallback_seed_url_for_bucket(seed_urls, bucket="proxy_voting")
        electronic_url = _fallback_seed_url_for_bucket(seed_urls, bucket="electronic_transactions_overlay")
        self.assertEqual(proxy_url, "https://example.state.gov/statutes/proxy-voting")
        self.assertEqual(electronic_url, "https://example.state.gov/statutes/electronic-signatures")

    def test_discovery_fallback_seed_url_prefers_official_host(self) -> None:
        seed_urls = [
            "https://www.lexisnexis.com/hottopics/statecode/",
            "https://www.example.state.gov/laws/",
        ]
        selected = _fallback_seed_url_for_bucket(seed_urls, bucket="proxy_voting", prefer_official=True)
        self.assertEqual(selected, "https://www.example.state.gov/laws/")

    def test_overlay_electronic_signature_inference(self) -> None:
        text = (
            "If a law requires a signature, an electronic signature satisfies the law. "
            "A signature may not be denied legal effect solely because it is in electronic form."
        )
        rules = _extract_rules_from_text(text, bucket="electronic_transactions_overlay")
        rule_types = {row["rule_type"] for row in rules}
        self.assertIn("proxy_electronic_signature_required_acceptance", rule_types)

    def test_overlay_electronic_record_inference(self) -> None:
        text = (
            "If a law requires a record to be in writing, an electronic record satisfies the law. "
            "A record may not be denied legal effect solely because it is in electronic form."
        )
        rules = _extract_rules_from_text(text, bucket="electronic_transactions_overlay")
        rule_types = {row["rule_type"] for row in rules}
        self.assertIn("proxy_electronic_assignment_required_acceptance", rule_types)

    def test_fetch_scope_key_distinguishes_community_type(self) -> None:
        hoa = {
            "jurisdiction": "AZ",
            "community_type": "hoa",
            "entity_form": "unknown",
            "governing_law_bucket": "electronic_transactions_overlay",
            "source_url": "https://example.com/law",
        }
        condo = dict(hoa)
        condo["community_type"] = "condo"
        self.assertNotEqual(_scope_key(hoa), _scope_key(condo))

    def test_normalize_scope_snapshot_key_distinguishes_scope(self) -> None:
        row1 = {
            "jurisdiction": "FL",
            "community_type": "hoa",
            "entity_form": "unknown",
            "governing_law_bucket": "electronic_transactions_overlay",
            "snapshot_path": "legal_corpus/raw/FL/shared.pdf",
        }
        row2 = dict(row1)
        row2["community_type"] = "condo"
        self.assertNotEqual(_scope_snapshot_key(row1), _scope_snapshot_key(row2))

    def test_pdf_quality_heuristic_flags_short_extracts(self) -> None:
        poor_text = "Section 554D.107 (17, 0)"
        rich_text = (
            "Construction and application. This chapter shall be construed and applied "
            "to facilitate electronic transactions consistent with other applicable law "
            "and to make uniform the law among states enacting it."
        )
        self.assertTrue(_is_low_quality_pdf_text(poor_text))
        self.assertFalse(_is_low_quality_pdf_text(rich_text))

    def test_split_sections_ignores_footer_only_markers(self) -> None:
        body = (
            "554D.107 Construction and application. This chapter shall be construed and applied "
            "to facilitate electronic transactions and make uniform the law among states."
        )
        text = f"{body}\n\nSection 554D.107 (17, 0)"
        sections = _split_sections(text)
        self.assertEqual(len(sections), 1)
        self.assertIn("Construction and application", sections[0]["text"])

    def test_source_quality_classifies_aggregator(self) -> None:
        quality = classify_source_quality(
            source_type="statute",
            source_url="https://law.justia.com/codes/florida/",
        )
        self.assertEqual(quality, "aggregator")
        self.assertFalse(extraction_allowed(source_quality=quality, include_aggregators=False))

    def test_source_quality_classifies_official_primary(self) -> None:
        quality = classify_source_quality(
            source_type="statute",
            source_url="https://www.flsenate.gov/Laws/Statutes/2025/720.306",
        )
        self.assertEqual(quality, "official_primary")
        self.assertTrue(extraction_allowed(source_quality=quality, include_aggregators=False))

    def test_source_quality_classifies_ms_billstatus_official(self) -> None:
        quality = classify_source_quality(
            source_type="statute",
            source_url="https://billstatus.ls.state.ms.us/",
        )
        self.assertEqual(quality, "official_primary")

    def test_nc_fallback_derivation_for_html_section(self) -> None:
        source_url = "https://www.ncleg.gov/EnactedLegislation/Statutes/HTML/BySection/Chapter_55A/GS_55A-7-24.html"
        fallbacks = _derived_fallback_urls(source_url)
        self.assertIn(
            "https://www.ncleg.gov/EnactedLegislation/Statutes/PDF/BySection/Chapter_55A/GS_55A-7-24.pdf",
            fallbacks,
        )
        self.assertIn(
            "https://www.ncleg.net/EnactedLegislation/Statutes/PDF/BySection/Chapter_55A/GS_55A-7-24.pdf",
            fallbacks,
        )

    def test_ny_fallback_derivation_for_law_url(self) -> None:
        source_url = "https://www.nysenate.gov/legislation/laws/BSC/609"
        fallbacks = _derived_fallback_urls(source_url)
        self.assertIn("https://www.nysenate.gov/legislation/laws/BSC/609?view=all", fallbacks)
        self.assertIn("https://legislation.nysenate.gov/laws/BSC/609", fallbacks)

    def test_ct_ssl_host_derives_http_fallback(self) -> None:
        source_url = "https://www.cga.ct.gov/lco/statutes.asp"
        fallbacks = _derived_fallback_urls(source_url)
        self.assertIn("http://www.cga.ct.gov/lco/statutes.asp", fallbacks)

    def test_nj_legacy_host_derives_modern_statutes_fallback(self) -> None:
        source_url = "http://lis.njleg.state.nj.us/cgi-bin/om_isapi.dll?infobase=statutes.nfo"
        fallbacks = _derived_fallback_urls(source_url)
        self.assertIn("https://www.njleg.state.nj.us/statutes", fallbacks)


if __name__ == "__main__":
    unittest.main()
