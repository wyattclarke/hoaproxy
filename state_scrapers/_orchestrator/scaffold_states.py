#!/usr/bin/env python3
"""Scaffold the 9-state overnight run.

Generates state_scrapers/{state}/ directories from _template, populates
COUNTY_RUNS in run_state_ingestion.py, writes per-county Serper query files
and one state-wide host-family + mgmt-co query file per state.

Idempotent: re-running overwrites generated files but leaves manual edits in
notes/ alone.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


# Tier-1 metro counties (>50k pop) per state, ordered by population descending.
# Tuples: (slug, county_label, anchor_label_for_query) where county_label is the
# value emitted onto Lead.county and anchor_label is the wording inside queries
# (e.g. "Jefferson Parish" for LA, "Capitol Hill" for DC).
STATE_CONFIGS: dict[str, dict] = {
    "DC": {
        "name": "District of Columbia",
        "tier": 0,
        "max_docai_usd": 10,
        "bbox": {"min_lat": 38.79, "max_lat": 39.00, "min_lon": -77.12, "max_lon": -76.91},
        "anchor_kind": "neighborhood",
        "anchor_word": "neighborhood",
        "counties": [
            ("capitol-hill", "DC", "Capitol Hill"),
            ("georgetown", "DC", "Georgetown"),
            ("foggy-bottom", "DC", "Foggy Bottom"),
            ("dupont-circle", "DC", "Dupont Circle"),
            ("adams-morgan", "DC", "Adams Morgan"),
            ("logan-circle", "DC", "Logan Circle"),
            ("navy-yard", "DC", "Navy Yard"),
            ("petworth", "DC", "Petworth"),
        ],
        "mgmt_cos": [
            "FirstService Residential",
            "Legum and Norman",
            "Comsource Management",
            "Capitol Property Management",
            "Smith Property Management",
            "Cardinal Management",
        ],
        "statute_phrases": [
            "DC Condominium Act",
            "DC Cooperative Housing Association",
            "Recorder of Deeds District of Columbia",
        ],
    },
    "HI": {
        "name": "Hawaii",
        "tier": 1,
        "max_docai_usd": 25,
        "bbox": {"min_lat": 18.86, "max_lat": 22.24, "min_lon": -160.27, "max_lon": -154.75},
        "anchor_kind": "county",
        "anchor_word": "County",
        "counties": [
            ("honolulu", "Honolulu", "Honolulu"),
            ("hawaii", "Hawaii", "Hawaii"),
            ("maui", "Maui", "Maui"),
            ("kauai", "Kauai", "Kauai"),
        ],
        "mgmt_cos": [
            "Hawaiiana Management",
            "Associa Hawaii",
            "Touchstone Properties",
            "Cadmus Properties",
            "Hawaii First",
            "Certified Management",
            "Destination Residences Hawaii",
            "Honu Group",
        ],
        "statute_phrases": [
            "Hawaii Condominium Property Act",
            "HRS Chapter 514B",
            "Bureau of Conveyances",
            "Hawaii Planned Community Associations Act",
        ],
    },
    "IA": {
        "name": "Iowa",
        "tier": 1,
        "max_docai_usd": 25,
        "bbox": {"min_lat": 40.36, "max_lat": 43.50, "min_lon": -96.64, "max_lon": -90.14},
        "anchor_kind": "county",
        "anchor_word": "County",
        "counties": [
            ("polk", "Polk", "Polk"),
            ("linn", "Linn", "Linn"),
            ("scott", "Scott", "Scott"),
            ("johnson", "Johnson", "Johnson"),
            ("black-hawk", "Black Hawk", "Black Hawk"),
            ("woodbury", "Woodbury", "Woodbury"),
            ("dubuque", "Dubuque", "Dubuque"),
            ("story", "Story", "Story"),
            ("dallas", "Dallas", "Dallas"),
            ("pottawattamie", "Pottawattamie", "Pottawattamie"),
        ],
        "mgmt_cos": [
            "Iowa Realty Property Management",
            "All Property Management Iowa",
            "First Realty Management",
        ],
        "statute_phrases": [
            "Iowa Horizontal Property Act",
            "Iowa Code Chapter 499B",
            "Iowa Code Chapter 504",
        ],
    },
    "ID": {
        "name": "Idaho",
        "tier": 1,
        "max_docai_usd": 25,
        "bbox": {"min_lat": 41.99, "max_lat": 49.00, "min_lon": -117.24, "max_lon": -111.04},
        "anchor_kind": "county",
        "anchor_word": "County",
        "counties": [
            ("ada", "Ada", "Ada"),
            ("canyon", "Canyon", "Canyon"),
            ("kootenai", "Kootenai", "Kootenai"),
            ("bonneville", "Bonneville", "Bonneville"),
            ("twin-falls", "Twin Falls", "Twin Falls"),
            ("bannock", "Bannock", "Bannock"),
            ("madison", "Madison", "Madison"),
        ],
        "mgmt_cos": [
            "Idaho Property Management",
            "First Rate Property Management",
            "Park Place Property Management",
        ],
        "statute_phrases": [
            "Idaho Condominium Property Act",
            "Idaho Code Title 55 Chapter 15",
            "Idaho Homeowner's Association Act",
        ],
    },
    "KY": {
        "name": "Kentucky",
        "tier": 1,
        "max_docai_usd": 25,
        "bbox": {"min_lat": 36.49, "max_lat": 39.15, "min_lon": -89.57, "max_lon": -81.96},
        "anchor_kind": "county",
        "anchor_word": "County",
        "counties": [
            ("jefferson", "Jefferson", "Jefferson"),
            ("fayette", "Fayette", "Fayette"),
            ("kenton", "Kenton", "Kenton"),
            ("warren", "Warren", "Warren"),
            ("boone", "Boone", "Boone"),
            ("hardin", "Hardin", "Hardin"),
            ("daviess", "Daviess", "Daviess"),
            ("madison", "Madison", "Madison"),
            ("campbell", "Campbell", "Campbell"),
            ("bullitt", "Bullitt", "Bullitt"),
            ("christian", "Christian", "Christian"),
            ("mccracken", "McCracken", "McCracken"),
        ],
        "mgmt_cos": [
            "Realty Solutions Kentucky",
            "Greater Louisville HOA Management",
            "Bluegrass Property Management",
        ],
        "statute_phrases": [
            "Kentucky Horizontal Property Law",
            "KRS Chapter 381",
            "Kentucky Condominium Act",
            "KRS Chapter 273",
        ],
    },
    "AL": {
        "name": "Alabama",
        "tier": 1,
        "max_docai_usd": 30,
        "bbox": {"min_lat": 30.14, "max_lat": 35.01, "min_lon": -88.47, "max_lon": -84.89},
        "anchor_kind": "county",
        "anchor_word": "County",
        "counties": [
            ("jefferson", "Jefferson", "Jefferson"),
            ("madison", "Madison", "Madison"),
            ("mobile", "Mobile", "Mobile"),
            ("baldwin", "Baldwin", "Baldwin"),
            ("shelby", "Shelby", "Shelby"),
            ("tuscaloosa", "Tuscaloosa", "Tuscaloosa"),
            ("montgomery", "Montgomery", "Montgomery"),
            ("lee", "Lee", "Lee"),
            ("morgan", "Morgan", "Morgan"),
            ("calhoun", "Calhoun", "Calhoun"),
            ("etowah", "Etowah", "Etowah"),
            ("houston", "Houston", "Houston"),
            ("limestone", "Limestone", "Limestone"),
            ("marshall", "Marshall", "Marshall"),
            ("st-clair", "St. Clair", "St. Clair"),
            ("elmore", "Elmore", "Elmore"),
            ("cullman", "Cullman", "Cullman"),
            ("talladega", "Talladega", "Talladega"),
        ],
        "mgmt_cos": [
            "Sentry Management Alabama",
            "FirstService Residential Alabama",
            "RealManage Alabama",
            "AHI Properties",
            "Hoar Construction Property",
        ],
        "statute_phrases": [
            "Alabama Uniform Condominium Act",
            "Alabama Code Title 35 Chapter 8A",
            "Alabama Code Title 35 Chapter 8",
        ],
    },
    "LA": {
        "name": "Louisiana",
        "tier": 1,
        "max_docai_usd": 25,
        "bbox": {"min_lat": 28.93, "max_lat": 33.02, "min_lon": -94.04, "max_lon": -88.81},
        "anchor_kind": "parish",
        "anchor_word": "Parish",
        "counties": [
            ("east-baton-rouge", "East Baton Rouge", "East Baton Rouge"),
            ("jefferson", "Jefferson", "Jefferson"),
            ("orleans", "Orleans", "Orleans"),
            ("st-tammany", "St. Tammany", "St. Tammany"),
            ("lafayette", "Lafayette", "Lafayette"),
            ("caddo", "Caddo", "Caddo"),
            ("calcasieu", "Calcasieu", "Calcasieu"),
            ("ouachita", "Ouachita", "Ouachita"),
            ("livingston", "Livingston", "Livingston"),
            ("rapides", "Rapides", "Rapides"),
            ("tangipahoa", "Tangipahoa", "Tangipahoa"),
            ("ascension", "Ascension", "Ascension"),
            ("bossier", "Bossier", "Bossier"),
            ("terrebonne", "Terrebonne", "Terrebonne"),
            ("lafourche", "Lafourche", "Lafourche"),
            ("iberia", "Iberia", "Iberia"),
        ],
        "mgmt_cos": [
            "Latter and Blum Property Management",
            "Sentry Management Louisiana",
            "FirstService Residential Louisiana",
        ],
        "statute_phrases": [
            "Louisiana Condominium Act",
            "Louisiana Revised Statutes Title 9 Chapter 3",
            "Louisiana Homeowners Association Act",
            "La. R.S. 9:1141",
        ],
    },
    "NV": {
        "name": "Nevada",
        "tier": 1,
        "max_docai_usd": 30,
        "bbox": {"min_lat": 35.00, "max_lat": 42.00, "min_lon": -120.01, "max_lon": -114.04},
        "anchor_kind": "county",
        "anchor_word": "County",
        "counties": [
            ("clark", "Clark", "Clark"),
            ("washoe", "Washoe", "Washoe"),
            ("carson-city", "Carson City", "Carson City"),
            ("lyon", "Lyon", "Lyon"),
            ("douglas", "Douglas", "Douglas"),
            ("elko", "Elko", "Elko"),
        ],
        "mgmt_cos": [
            "FirstService Residential Nevada",
            "Associa Sierra North",
            "Nevada Association Services",
            "RPMG",
            "Eugene Burger Management",
            "Olympia Companies Las Vegas",
        ],
        "statute_phrases": [
            "Nevada Common-Interest Communities Act",
            "NRS Chapter 116",
            "NRS 116.31",
            "Nevada Real Estate Division",
        ],
    },
    "IL": {
        "name": "Illinois",
        "tier": 3,
        "max_docai_usd": 150,
        "bbox": {"min_lat": 36.97, "max_lat": 42.51, "min_lon": -91.52, "max_lon": -87.49},
        "anchor_kind": "county",
        "anchor_word": "County",
        "counties": [
            # Wave A — Chicago metro (condo + HOA dense)
            ("cook", "Cook", "Cook"),
            ("dupage", "DuPage", "DuPage"),
            ("lake", "Lake", "Lake"),
            ("will", "Will", "Will"),
            ("kane", "Kane", "Kane"),
            ("mchenry", "McHenry", "McHenry"),
            ("kendall", "Kendall", "Kendall"),
            # Wave B — downstate metros
            ("winnebago", "Winnebago", "Winnebago"),
            ("sangamon", "Sangamon", "Sangamon"),
            ("champaign", "Champaign", "Champaign"),
            ("peoria", "Peoria", "Peoria"),
            ("mclean", "McLean", "McLean"),
            ("st-clair", "St. Clair", "St. Clair"),
            ("madison", "Madison", "Madison"),
            ("rock-island", "Rock Island", "Rock Island"),
            ("tazewell", "Tazewell", "Tazewell"),
            ("kankakee", "Kankakee", "Kankakee"),
        ],
        "mgmt_cos": [
            "FirstService Residential Illinois",
            "Associa Chicagoland",
            "Foster Premier",
            "ACM Community Management",
            "Sudler Property Management",
            "Vanguard Community Management",
            "Habitat Chicago",
            "Lieberman Management Services",
            "Property Specialists Illinois",
            "Heil Heil Smart Golee",
            "RealManage Chicago",
            "Inland Residential Real Estate",
        ],
        "statute_phrases": [
            "Illinois Condominium Property Act",
            "765 ILCS 605",
            "Illinois Common Interest Community Association Act",
            "765 ILCS 160",
            "Illinois General Not For Profit Corporation Act",
            "805 ILCS 105",
            "Cook County Recorder of Deeds",
        ],
    },
    "UT": {
        "name": "Utah",
        "tier": 1,
        "max_docai_usd": 30,
        "bbox": {"min_lat": 36.99, "max_lat": 42.00, "min_lon": -114.05, "max_lon": -109.04},
        "anchor_kind": "county",
        "anchor_word": "County",
        "counties": [
            ("salt-lake", "Salt Lake", "Salt Lake"),
            ("utah", "Utah", "Utah"),
            ("davis", "Davis", "Davis"),
            ("weber", "Weber", "Weber"),
            ("washington", "Washington", "Washington"),
            ("cache", "Cache", "Cache"),
            ("tooele", "Tooele", "Tooele"),
            ("iron", "Iron", "Iron"),
            ("box-elder", "Box Elder", "Box Elder"),
            ("summit", "Summit", "Summit"),
        ],
        "mgmt_cos": [
            "FCS Community Management",
            "Treo Community Management",
            "Advantage Management Utah",
            "Community Solutions Utah",
            "Premier Property Management Utah",
        ],
        "statute_phrases": [
            "Utah Community Association Act",
            "Utah Condominium Ownership Act",
            "Utah Code Title 57 Chapter 8",
            "Utah Code Title 57 Chapter 8a",
        ],
    },
}


def per_county_queries(state_name: str, anchor_word: str, anchor_label: str) -> list[str]:
    """Generate ~16 deterministic queries per county/anchor combination."""
    if anchor_word == "neighborhood":
        anchor_phrase = anchor_label
        with_state = f'"{anchor_label}" "{state_name}"'
    else:
        anchor_phrase = f"{anchor_label} {anchor_word}"
        with_state = f'"{anchor_phrase}" "{state_name}"'
    return [
        f'filetype:pdf "{anchor_phrase}" "{state_name}" "Declaration of Covenants" "Homeowners Association"',
        f'filetype:pdf "{anchor_phrase}, {state_name}" "Declaration of Restrictions" "Homes Association"',
        f'filetype:pdf "{anchor_phrase}" "{state_name}" "Declaration of Condominium" "Association"',
        f'filetype:pdf "Register of Deeds" "{anchor_phrase}, {state_name}" "Homeowners Association"',
        f'filetype:pdf "Articles of Incorporation" "{anchor_phrase}, {state_name}" "Homeowners Association"',
        f'filetype:pdf "Amendment to Declaration" "{anchor_phrase}, {state_name}" "Homeowners Association"',
        f'filetype:pdf "Restated Bylaws" "{state_name}" "{anchor_label}" "Homeowners Association"',
        f'filetype:pdf "Supplemental Declaration" "{state_name}" "{anchor_phrase}"',
        f'filetype:pdf "{anchor_phrase}" "{state_name}" "Property Owners Association" "Covenants"',
        f'filetype:pdf "{anchor_phrase}" "{state_name}" "Master Deed" "Condominium"',
        f'{with_state} "HOA documents" "bylaws"',
        f'{with_state} "homes association" documents',
        f'{with_state} "governing documents" "homeowners association"',
        f'{with_state} "condominium association" "declaration"',
        f'site:.gov/DocumentCenter/View "{state_name}" "{anchor_label}" "Homeowners Association" "Declaration"',
        f'site:.gov/AgendaCenter/ViewFile "{state_name}" "{anchor_label}" "Homeowners Association"',
        f'inurl:/wp-content/uploads/ "{state_name}" "{anchor_label}" "homeowners association" bylaws',
        f'inurl:/wp-content/uploads/ "{state_name}" "{anchor_label}" "condominium association" declaration',
    ]


def host_family_queries(state_name: str, mgmt_cos: list[str], statute_phrases: list[str]) -> list[str]:
    """State-wide host-family + mgmt-co + statute-anchored queries."""
    qs = [
        # Host families (Appendix E catalog)
        f'site:eneighbors.com "{state_name}" HOA documents covenants',
        f'site:eneighbors.com "{state_name}" "Homeowners Association" "documents"',
        f'site:gogladly.com/connect/document "{state_name}" "homeowners association" bylaws',
        f'inurl:hmsft-doc "{state_name}" "homes association" "deed restrictions"',
        f'inurl:/file/document/ "{state_name}" "homeowners association" covenants',
        f'inurl:/wp-content/uploads/ "{state_name}" "homeowners association" bylaws',
        f'inurl:/wp-content/uploads/ "{state_name}" "homes association" restrictions',
        f'inurl:/wp-content/uploads/ "{state_name}" "condominium association" declaration',
        f'inurl:/Files/ "{state_name}" "Declaration of Condominium" "Association"',
        f'site:cdn.shopify.com "{state_name}" "Homeowners Association" filetype:pdf',
        f'site:squarespace.com "{state_name}" "homeowners association" "covenants"',
        f'"{state_name}" "HOA documents" "bylaws" -eneighbors -hopb -hoamanagement',
        f'"{state_name}" "governing documents" "homeowners association" -eneighbors -hopb',
        f'"{state_name}" "condominium association" "declaration of condominium" filetype:pdf',
        f'"{state_name}" "property owners association" "covenants" filetype:pdf',
        f'filetype:pdf "{state_name} not-for-profit corporation" "Homeowners Association"',
        f'filetype:pdf "{state_name} non-profit corporation" "Homes Association"',
    ]
    # Statute-anchored
    for phrase in statute_phrases:
        qs.append(f'filetype:pdf "{phrase}" "Association"')
        qs.append(f'filetype:pdf "{phrase}" "Declaration"')
    # Management-co anchored
    for co in mgmt_cos:
        qs.append(f'"{co}" "{state_name}" "Homeowners Association" filetype:pdf')
        qs.append(f'"{co}" "{state_name}" "covenants" "declaration"')
    return qs


def render_runner(state_code: str, cfg: dict) -> str:
    """Substitute placeholders in the template runner for one state."""
    template = (ROOT / "state_scrapers/_template/scripts/run_state_ingestion.py").read_text(encoding="utf-8")
    bbox_literal = json.dumps(cfg["bbox"], sort_keys=True)
    rendered = (
        template
        .replace("__STATE_NAME__", cfg["name"])
        .replace("__STATE_BBOX__", bbox_literal)
        .replace("__TIER__", str(cfg["tier"]))
        .replace("__MAX_DOCAI_USD__", str(cfg["max_docai_usd"]))
        .replace("__DISCOVERY_SOURCE__", "keyword-serper")
        .replace("__STATE__", state_code)  # do last; matches inside __STATE_NAME__
    )
    # Build COUNTY_RUNS list literal
    lines = []
    for slug, county_label, _anchor in cfg["counties"]:
        qfile = f"{state_code.lower()}_{slug}_serper_queries.txt"
        lines.append(f'    ("{qfile}", "{county_label}"),')
    # Plus host-family file as a final pseudo-county sweep with no default_county
    host_qfile = f"{state_code.lower()}_host_family_serper_queries.txt"
    lines.append(f'    ("{host_qfile}", None),')
    county_runs_block = "COUNTY_RUNS: list[tuple[str, str | None]] = [\n" + "\n".join(lines) + "\n]"
    rendered = rendered.replace(
        "COUNTY_RUNS: list[tuple[str, str | None]] = []",
        county_runs_block,
    )
    return rendered


def write_query_files(state_code: str, cfg: dict) -> None:
    qdir = ROOT / f"state_scrapers/{state_code.lower()}/queries"
    qdir.mkdir(parents=True, exist_ok=True)
    for slug, _county_label, anchor_label in cfg["counties"]:
        qfile = qdir / f"{state_code.lower()}_{slug}_serper_queries.txt"
        qs = per_county_queries(cfg["name"], cfg["anchor_word"], anchor_label)
        qfile.write_text("\n".join(qs) + "\n", encoding="utf-8")
    # State-wide host-family file
    host = qdir / f"{state_code.lower()}_host_family_serper_queries.txt"
    qs = host_family_queries(cfg["name"], cfg["mgmt_cos"], cfg["statute_phrases"])
    host.write_text("\n".join(qs) + "\n", encoding="utf-8")


def scaffold_state(state_code: str, cfg: dict) -> dict:
    state_dir = ROOT / f"state_scrapers/{state_code.lower()}"
    template_dir = ROOT / "state_scrapers/_template"
    # Make sure base layout exists
    (state_dir / "scripts").mkdir(parents=True, exist_ok=True)
    (state_dir / "queries").mkdir(parents=True, exist_ok=True)
    (state_dir / "leads").mkdir(parents=True, exist_ok=True)
    (state_dir / "results").mkdir(parents=True, exist_ok=True)
    (state_dir / "notes").mkdir(parents=True, exist_ok=True)
    # Render runner
    runner = render_runner(state_code, cfg)
    (state_dir / "scripts" / "run_state_ingestion.py").write_text(runner, encoding="utf-8")
    (state_dir / "scripts" / "run_state_ingestion.py").chmod(0o755)
    # Copy README hint (if not already present)
    readme = state_dir / "README.md"
    if not readme.exists():
        readme.write_text(
            f"# {cfg['name']} ({state_code}) HOA scraping\n\n"
            f"Tier {cfg['tier']}, max DocAI ${cfg['max_docai_usd']}.\n"
            f"Counties: {', '.join(c[1] for c in cfg['counties'])}.\n"
            f"See `docs/multi-state-ingestion-playbook.md` for the canonical pipeline.\n",
            encoding="utf-8",
        )
    # Query files
    write_query_files(state_code, cfg)
    return {
        "state": state_code,
        "counties": [c[1] for c in cfg["counties"]],
        "max_docai_usd": cfg["max_docai_usd"],
        "host_family_queries": True,
    }


def main() -> int:
    print("Scaffolding 9-state overnight run...")
    summary = []
    for state_code, cfg in STATE_CONFIGS.items():
        result = scaffold_state(state_code, cfg)
        summary.append(result)
        print(f"  {state_code}: {len(result['counties'])} counties, ${result['max_docai_usd']} DocAI cap")
    # Write summary
    out = ROOT / "state_scrapers/_orchestrator/scaffold_summary.json"
    out.write_text(json.dumps({"states": summary}, indent=2, sort_keys=True), encoding="utf-8")
    print(f"\nScaffold complete. Summary at {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
