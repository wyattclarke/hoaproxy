"""Post-OCR PII detection filter.

Scans extracted text for personally identifiable information patterns.
Documents with PII are quarantined rather than ingested.

Checks for:
  - Social Security Numbers
  - Phone numbers (in context of personal info)
  - Email addresses (in context of directories/lists)
  - Dense Name + Address patterns (membership rosters)
  - Account/lot numbers paired with owner names
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class PIIResult:
    has_pii: bool = False
    risk_level: str = "none"  # none, low, high
    findings: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.has_pii


# SSN: 123-45-6789 or 123 45 6789 (but not dates like 2023-01-6789)
SSN_PATTERN = re.compile(
    r'(?<!\d)(?!(?:19|20)\d{2})[0-8]\d{2}[-\s]\d{2}[-\s]\d{4}(?!\d)'
)

# Phone: (555) 123-4567 or 555-123-4567 or 555.123.4567
PHONE_PATTERN = re.compile(
    r'(?:\(\d{3}\)\s*|\d{3}[-.\s])\d{3}[-.\s]\d{4}'
)

# Email
EMAIL_PATTERN = re.compile(
    r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
)

# Name + Address pattern (e.g., "John Smith\n123 Main St")
NAME_ADDRESS_PATTERN = re.compile(
    r'[A-Z][a-z]+\s+[A-Z][a-z]+\s*\n\s*\d+\s+[A-Z]',
    re.MULTILINE,
)

# "Lot/Unit # + Owner Name" pattern
LOT_OWNER_PATTERN = re.compile(
    r'(?:lot|unit|apt)\s*(?:#|no\.?|num)?\s*\d+\s*[-:,]?\s*[A-Z][a-z]+\s+[A-Z][a-z]+',
    re.IGNORECASE,
)

# Directory-style: Name followed by phone or email on same/next line
DIRECTORY_ENTRY_PATTERN = re.compile(
    r'[A-Z][a-z]+\s+[A-Z][a-z]+\s*[-:,]?\s*(?:\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}|[a-zA-Z0-9._%+-]+@)',
)

# Exclude common non-PII emails (generic/business)
NON_PII_EMAILS = {
    "info@", "contact@", "admin@", "office@", "hoa@", "board@",
    "management@", "support@", "noreply@", "no-reply@",
}


def scan_for_pii(text: str) -> PIIResult:
    """Scan text for PII patterns. Returns PIIResult with findings."""
    if not text or not text.strip():
        return PIIResult()

    findings = []
    details: dict = {}

    # SSN check — always high risk
    ssns = SSN_PATTERN.findall(text)
    if ssns:
        findings.append(f"SSN-like patterns found: {len(ssns)}")
        details["ssn_count"] = len(ssns)

    # Email check — only flag personal emails in bulk
    emails = EMAIL_PATTERN.findall(text)
    personal_emails = [
        e for e in emails
        if not any(e.lower().startswith(prefix) for prefix in NON_PII_EMAILS)
    ]
    if len(personal_emails) >= 3:
        findings.append(f"Personal email addresses: {len(personal_emails)}")
        details["personal_email_count"] = len(personal_emails)

    # Name + Address roster pattern
    name_addrs = NAME_ADDRESS_PATTERN.findall(text)
    if len(name_addrs) >= 5:
        findings.append(f"Name+Address pairs (roster pattern): {len(name_addrs)}")
        details["name_address_count"] = len(name_addrs)

    # Lot/Unit + Owner pattern
    lot_owners = LOT_OWNER_PATTERN.findall(text)
    if len(lot_owners) >= 5:
        findings.append(f"Lot/Unit+Owner pairs: {len(lot_owners)}")
        details["lot_owner_count"] = len(lot_owners)

    # Directory entries (name + phone/email)
    directory_entries = DIRECTORY_ENTRY_PATTERN.findall(text)
    if len(directory_entries) >= 3:
        findings.append(f"Directory-style entries: {len(directory_entries)}")
        details["directory_entry_count"] = len(directory_entries)

    # Determine risk level
    if not findings:
        return PIIResult()

    has_ssn = details.get("ssn_count", 0) > 0
    has_roster = details.get("name_address_count", 0) >= 5
    has_directory = details.get("directory_entry_count", 0) >= 3
    has_lot_owners = details.get("lot_owner_count", 0) >= 5

    if has_ssn or has_roster or has_directory:
        risk_level = "high"
    elif has_lot_owners or details.get("personal_email_count", 0) >= 5:
        risk_level = "high"
    else:
        risk_level = "low"

    return PIIResult(
        has_pii=True,
        risk_level=risk_level,
        findings=findings,
        details=details,
    )


def scan_document_pages(pages_text: list[str]) -> PIIResult:
    """Scan all pages of a document for PII. Returns aggregate result."""
    combined_findings = []
    combined_details: dict = {}
    worst_risk = "none"

    for i, page_text in enumerate(pages_text):
        result = scan_for_pii(page_text)
        if result.has_pii:
            for finding in result.findings:
                combined_findings.append(f"Page {i+1}: {finding}")
            for k, v in result.details.items():
                combined_details[k] = combined_details.get(k, 0) + v
            if result.risk_level == "high":
                worst_risk = "high"
            elif result.risk_level == "low" and worst_risk == "none":
                worst_risk = "low"

    if not combined_findings:
        return PIIResult()

    return PIIResult(
        has_pii=True,
        risk_level=worst_risk,
        findings=combined_findings,
        details=combined_details,
    )
