#!/usr/bin/env python3
"""Rebuild the NYC charter school campus dataset from official sources.

Usage:
    python scripts/build_data.py            # use cached raw files in data/raw/ when present
    python scripts/build_data.py --refresh  # download every source again

Sources (see README for details):
  1. NYSED Charter Schools Directory (.xlsx)  - which schools exist, authorizer, year opened
  2. NYSED per-school pages                    - school website
  3. NYC DOE LCGMS School Information Report   - which schools operate in 2026-27, grades, DOE address
  4. NYC Charter School Center campus map (KML) - every campus of every school
  5. NYC Planning GeoSearch                    - coordinates + building ID (BIN) for each address

Outputs:
  data/schools.csv          one row per school
  data/campuses.csv         one row per campus (a school at one building)
  data/campuses.geojson     same, as GeoJSON (GitHub renders it as a map)
  data/checks.md            every reconciliation and quality check, with counts
  docs/data/campuses.geojson  copy used by the web map
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data"
SITE_DATA = ROOT / "docs" / "data"

NYSED_DIR_PAGE = "https://www.nysed.gov/charter-schools/charter-schools-directory"
LCGMS_PAGE = "https://www.nycenet.edu/PublicApps/LCGMS.aspx"
LCGMS_BUTTON = "ctl00$ContentPlaceHolder1$LCGMSDataDownload$Button1"
CSC_MAP_KML = "https://www.google.com/maps/d/kml?mid=1928n9fge2TsZ2GTuejF2S-6Ml0QYpiQ&forcekml=1"
CSC_FACTS_PDF = "https://nyccharterschools.org/wp-content/uploads/2025/04/NYCCSC-General-Facts-2026-27SY.pdf"
GEOSEARCH = "https://geosearch.planninglabs.nyc/v2/search"
ZIP_AREAS = "https://data.cityofnewyork.us/resource/pri4-ifjk.geojson?$limit=500"  # NYC DOHMH MODZCTA boundaries

# Official benchmark: NYC Charter School Center, "NYC Charter School Facts 2026-2027", as of August 2026.
BENCHMARK = {"total": 286, "Bronx": 100, "Brooklyn": 92, "Manhattan": 52, "Queens": 34, "Staten Island": 8}

BORO_FROM_CODE = {"X": "Bronx", "K": "Brooklyn", "M": "Manhattan", "Q": "Queens", "R": "Staten Island"}
# Rough NYC bounding box; every point must fall inside it.
NYC_BBOX = (-74.26, 40.49, -73.69, 40.92)
GRADE_ORDER = ["PK", "K"] + [str(i) for i in range(1, 13)]


# --------------------------------------------------------------------------- http

def session() -> requests.Session:
    """requests session that trusts the usual CAs plus GlobalSign's intermediate.

    www.nysed.gov serves its certificate without the intermediate, so strict clients
    (Python, curl) fail verification. Browsers fill the gap automatically; we do the
    same by adding the intermediate (scripts/certs/) to the trust bundle. Verification
    stays on.
    """
    import certifi

    base = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE") or certifi.where()
    bundle = Path(tempfile.gettempdir()) / "nyc-charter-map-ca.pem"
    bundle.write_text(Path(base).read_text() + "\n" + (ROOT / "scripts/certs/globalsign-rsa-ov-ssl-ca-2018.pem").read_text())
    class _Session(requests.Session):
        # Session.verify is overridden by REQUESTS_CA_BUNDLE if set, so pass it per request.
        def request(self, *a, **kw):
            kw.setdefault("verify", str(bundle))
            return super().request(*a, **kw)

    s = _Session()
    s.headers["User-Agent"] = "nyc-charter-schools-map data build (github.com/a7madifar7an/nyc-charter-schools-map)"
    return s


def cached(path: Path, refresh: bool, fetch) -> Path:
    if refresh or not path.exists():
        print(f"  downloading -> {path.relative_to(ROOT)}")
        data = fetch()
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
    return path


# --------------------------------------------------------------------------- helpers

def norm_name(x: str) -> str:
    x = str(x).lower().replace("–", "-").replace("&", "and")
    x = re.sub(r"\(the\)|\bthe\b|charter|school|\bof\b|[^a-z0-9 ]", " ", x)
    roman = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10",
             "ll": "2", "lll": "3", "lv": "4"}  # DOE writes some numerals with a lowercase L ('lll')
    x = " ".join(roman.get(w, w) for w in x.split())
    return re.sub(r"\s+", " ", x).strip()


def numbers_in(x: str) -> list[str]:
    return re.findall(r"\d+", norm_name(x))


def parse_grades(text) -> list[str]:
    """'Pre-K,K,1,2' or '0K,01,02,SE' or 'PK,0K' -> ['PK','K','1','2'] (SE/ungraded dropped)."""
    if not isinstance(text, str):
        return []
    out = set()
    for tok in re.split(r"[,\s]+", text.strip()):
        t = tok.upper().replace("PRE-K", "PK")
        if t in ("PK", "K", "0K"):
            out.add("K" if t == "0K" else t)
        elif t.isdigit() and 1 <= int(t) <= 12:
            out.add(str(int(t)))
    return [g for g in GRADE_ORDER if g in out]


def doe_grades(text) -> list[str]:
    # DOE 'Grades' is either comma-separated ('0K,01,02') or packed ('09101112').
    if isinstance(text, str) and "," not in text and re.fullmatch(r"(\d\d|PK|0K)+", text):
        text = ",".join(text[i:i + 2] for i in range(0, len(text), 2))
    return parse_grades(text)


def grade_span(grades: list[str]) -> str:
    if not grades:
        return ""
    return grades[0] if len(grades) == 1 else f"{grades[0]}-{grades[-1]}"


def grade_bands(grades: list[str]) -> list[str]:
    g = set(grades)
    bands = []
    if g & {"PK", "K", "1", "2", "3", "4", "5"}:
        bands.append("Elementary")
    if g & {"6", "7", "8"}:
        bands.append("Middle")
    if g & {"9", "10", "11", "12"}:
        bands.append("High")
    return bands


def display_address(a: str) -> str:
    """'245 West 129Th Street, Manhattan, NY 10027' / '856 QUINCY STREET, ...' -> consistent title case for display."""
    a = re.sub(r"\s+", " ", a).strip()
    keep_upper = {"NY", "PS", "IS", "MS", "HS", "JHS"}
    out = []
    for i, w in enumerate(a.split(" ")):
        core = w.strip(",.")
        if i and core.lower() in {"of", "the", "and", "at"}:
            out.append(w.lower())
        elif core.upper() in keep_upper:
            out.append(w.upper())
        elif re.fullmatch(r"\d+(st|nd|rd|th)[,.]?", w, flags=re.I) or re.fullmatch(r"[\d-]+[,.]?", w):
            out.append(w.lower())
        else:
            out.append("-".join(part[:1].upper() + part[1:].lower() for part in w.split("-")))
    return " ".join(out)


OFFICE_RE = re.compile(r"\b(?:suite|ste|apt|apartment|floor|fl|room|rm|unit)\b\.?|#", re.I)


# --------------------------------------------------------------------------- sources

def load_nysed(s, refresh):
    page = cached(RAW / "nysed_directory_page.html", refresh, lambda: s.get(NYSED_DIR_PAGE, timeout=60).content)
    html = page.read_text(encoding="utf8")
    xlsx_url = re.search(r'href="([^"]+charter-school-directory[^"]+\.xlsx)"', html).group(1)
    xlsx = cached(RAW / "nysed_directory.xlsx", refresh, lambda: s.get(xlsx_url, timeout=60).content)
    title = pd.read_excel(xlsx, header=None, nrows=1).iloc[0, 0]
    d = pd.read_excel(xlsx, header=1, dtype=str)
    d = d[d["Region"] == "New York City"].copy()
    import html as htmllib

    links = dict((re.sub(r"\s+", " ", htmllib.unescape(t)).strip(), u) for u, t in re.findall(
        r'<a[^>]+href="(https://www\.nysed\.gov/charter-schools/[a-z0-9-]+)"[^>]*>([^<]+)</a>', html))
    by_norm = {norm_name(t): u for t, u in links.items()}

    def page_for(name):
        import difflib

        k = norm_name(name)
        if k in by_norm:
            return by_norm[k]
        # Fuzzy match only between names with the same numbers ('NYC 17' must not match 'NYC 7').
        same_numbers = [c for c in by_norm if numbers_in(c) == numbers_in(k)]
        close = difflib.get_close_matches(k, same_numbers, n=1, cutoff=0.9)
        return by_norm[close[0]] if close else None

    d["nysed_page"] = d["Charter School Name"].map(page_for)
    meta = {"title": title, "url": xlsx_url}
    return d, meta


def load_websites(s, nysed, refresh):
    """School website, read from each school's NYSED page. Cached in data/raw/nysed_websites.csv."""
    path = RAW / "nysed_websites.csv"
    cache = pd.read_csv(path, dtype=str).fillna("") if path.exists() else pd.DataFrame(columns=["nysed_page", "website"])
    known = dict(zip(cache.nysed_page, cache.website))
    todo = [u for u in nysed.nysed_page.dropna() if refresh or u not in known]
    for i, u in enumerate(todo, 1):
        print(f"  website {i}/{len(todo)}: {u}")
        h = s.get(u, timeout=60).text
        m = re.search(r'<a[^>]+href="([^"]+)"[^>]*>[^<]*website[^<]*</a>', h, flags=re.I)
        known[u] = m.group(1).strip() if m else ""
        time.sleep(0.3)
    pd.DataFrame(sorted(known.items()), columns=["nysed_page", "website"]).to_csv(path, index=False)
    return known


def html_table_as_strings(html: str) -> pd.DataFrame:
    """Parse the first HTML table, keeping every cell as text (codes and ZIPs keep leading zeros)."""
    import lxml.html

    rows = [[c.text_content().strip() for c in tr.xpath("./th|./td")] for tr in lxml.html.fromstring(html).xpath("//table[1]//tr")]
    return pd.DataFrame(rows[1:], columns=rows[0]).replace("", None)


def load_lcgms(s, refresh):
    path = RAW / "lcgms_school_data.csv"
    if refresh or not path.exists():
        print("  downloading DOE LCGMS report")
        page = s.get(LCGMS_PAGE, timeout=60).text
        form = dict(re.findall(r'<input[^>]+name="([^"]+)"[^>]*value="([^"]*)"', page))
        form.update({"__EVENTTARGET": LCGMS_BUTTON, "__EVENTARGUMENT": ""})
        r = s.post(LCGMS_PAGE, data=form, timeout=180)
        r.raise_for_status()
        fname = re.search(r"filename=([^;]+)", r.headers.get("content-disposition", "")).group(1)
        table = html_table_as_strings(r.content.decode("utf-16"))
        table.insert(0, "source_file", fname)
        table.to_csv(path, index=False)
    d = pd.read_csv(path, dtype=str)
    meta = {"file": d["source_file"].iloc[0]}
    c = d[d["Managed By Name"].str.contains("Charter", case=False, na=False)].copy()
    return d, c, meta


def load_campus_map(s, refresh):
    kml = cached(RAW / "csc_campuses.kml", refresh, lambda: s.get(CSC_MAP_KML, timeout=60).content)
    ns = "{http://www.opengis.net/kml/2.2}"
    root = ET.parse(kml).getroot()
    folder = root.find(f".//{ns}Folder/{ns}name").text
    rows = []
    for p in root.iter(ns + "Placemark"):
        r = {"campus_name": (p.findtext(ns + "name") or "").strip()}
        for dnode in p.iter(ns + "Data"):
            r[dnode.get("name")] = (dnode.findtext(ns + "value") or "").strip()
        rows.append(r)
    k = pd.DataFrame(rows)
    code = k["DBN+Building Code"].str.split("-", n=1)
    k["doe_code"] = code.str[0].str.strip()
    k["building_code"] = code.str[1].str.strip()
    return k, {"layer": folder}


SUFFIX = {"ST": "STREET", "AVE": "AVENUE", "AV": "AVENUE", "BLVD": "BOULEVARD", "PKWY": "PARKWAY", "PL": "PLACE",
          "RD": "ROAD", "DR": "DRIVE", "TER": "TERRACE", "CT": "COURT", "LN": "LANE", "HWY": "HIGHWAY", "SQ": "SQUARE",
          "E": "EAST", "W": "WEST", "N": "NORTH", "S": "SOUTH"}


def clean_address(a: str) -> str:
    a = re.sub(r"\s+", " ", a).strip()
    a = re.sub(r"\b(Street|Avenue|Road|Boulevard|Place|Drive)\.\s", r"\1, ", a, flags=re.I)  # '128 Street. Queens'
    fixes = [(r"\bStreeet\b", "Street"), (r"\bPKWAY\b", "PARKWAY"), (r"\bPARKWAY EA\b", "PARKWAY EAST"),
             (r"\bBroadway Avenue\b", "Broadway")]  # typos seen in the source files
    for pat, rep in fixes:
        a = re.sub(pat, rep, a, flags=re.I)
    a = re.sub(r"\b([NSEW])\.?\s+(?=\d)", lambda m: SUFFIX[m.group(1).upper()].title() + " ", a)  # 'E 169' -> 'East 169'
    return re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", a, flags=re.I)  # '162Th Street' -> '162 Street'


def split_address(a: str):
    """'144-176 East 128 Street, Manhattan, NY' -> (('range', 144, 176), 'EAST 128 STREET')."""
    street_part = a.split(",")[0].upper().replace(".", "")
    m = re.match(r"\s*(\d+)(?:-(\d+))?[A-Z]?\s+(.*)", street_part)
    if not m:
        return None, None
    lo, hi, street = m.group(1), m.group(2), m.group(3)
    street = " ".join(SUFFIX.get(w, w) for w in street.split())
    queens = bool(re.search(r"QUEENS|\b11[0-6]\d\d\b", a.upper())) and not re.search(r"\b112\d\d\b", a)
    if hi and queens:  # Queens hyphenated house number '36-49' = block 36, house 49
        return ("num", int(lo) * 10000 + int(hi)), street
    if hi:  # a building that spans a range, e.g. '1962-84' -> 1962..1984
        h = int(hi) if len(hi) >= len(lo) else int(lo[: len(lo) - len(hi)] + hi)
        return ("range", int(lo), h), street
    return ("num", int(lo)), street


def number_value(n, queens_style):
    if queens_style and "-" in n:
        a, b = n.split("-")[:2]
        return int(a) * 10000 + int(re.sub(r"\D", "", b) or 0)
    return int(re.sub(r"\D.*", "", n) or -1)


def geocode(s, addresses, refresh):
    """Geocode with NYC Planning GeoSearch and verify the result is the same building.

    GeoSearch returns *some* address on the right street when it cannot find the exact one,
    with no change in its confidence score. So each result is checked: same street, and the
    same house number (or within the address range). If not found, the closest house number
    on the same street is used and the location is marked 'approximate'.
    """
    path = RAW / "geocode_cache.json"
    cache = json.loads(path.read_text()) if path.exists() and not refresh else {}
    for i, a in enumerate(sorted(set(addresses) - set(cache)), 1):
        print(f"  geocode {i}: {a}")
        feats = s.get(GEOSEARCH, params={"text": clean_address(a), "size": 10}, timeout=60).json().get("features", [])
        if pick_location(a, [flat(f) for f in feats]) is None:
            # The search endpoint sometimes misses addresses that autocomplete finds; try it without the ZIP.
            short = re.sub(r",?\s*NY\s*\d{5}.*$", "", clean_address(a))
            feats += s.get(GEOSEARCH.replace("/search", "/autocomplete"), params={"text": short}, timeout=60).json().get("features", [])
        cache[a] = {"features": [flat(f) for f in feats]}
        time.sleep(0.1)
    path.write_text(json.dumps(cache, indent=1, sort_keys=True))
    return {a: pick_location(a, cache[a]["features"]) for a in addresses}


def flat(f):
    p = f["properties"]
    return {"lon": f["geometry"]["coordinates"][0], "lat": f["geometry"]["coordinates"][1], "label": p.get("label"),
            "borough": p.get("borough"), "housenumber": p.get("housenumber"), "street": p.get("street"),
            "bin": (p.get("addendum") or {}).get("pad", {}).get("bin")}


def pick_location(address, feats):
    want, street = split_address(clean_address(address))
    if not want:
        return None
    queens_style = want[0] == "num" and want[1] >= 10000
    best = None
    for f in feats:
        fs = " ".join(SUFFIX.get(w, w) for w in (f["street"] or "").upper().split())
        if fs != street or not f["housenumber"]:
            continue
        v = number_value(f["housenumber"], queens_style)
        if (want[0] == "num" and v == want[1]) or (want[0] == "range" and want[1] <= v <= want[2]):
            return {**f, "quality": "exact"}
        target = want[1]
        if (v % 2) == (target % 2):  # same side of the street
            d = abs(v - target)
            if best is None or d < best[0]:
                best = (d, f)
    if best and best[0] <= 120:
        return {**best[1], "quality": "approximate"}
    return None


def zip_centroids(s, refresh):
    """ZIP -> [lat, lon] for the map's 'search near a ZIP' box (GeoSearch does not handle bare ZIPs)."""
    path = cached(RAW / "modzcta.geojson", refresh, lambda: s.get(ZIP_AREAS, timeout=120).content)
    out = {}
    for f in json.loads(path.read_text())["features"]:
        polys = f["geometry"]["coordinates"] if f["geometry"]["type"] == "MultiPolygon" else [f["geometry"]["coordinates"]]
        ring = max((p[0] for p in polys), key=len)  # outer ring of the largest part
        a = cx = cy = 0.0
        for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
            c = x1 * y2 - x2 * y1
            a += c
            cx += (x1 + x2) * c
            cy += (y1 + y2) * c
        lon, lat = cx / (3 * a), cy / (3 * a)
        for z in re.findall(r"\d{5}", f["properties"].get("zcta") or f["properties"]["modzcta"]):
            out[z] = [round(lat, 5), round(lon, 5)]
    return out


# --------------------------------------------------------------------------- build

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="re-download all sources")
    args = ap.parse_args()
    RAW.mkdir(parents=True, exist_ok=True)
    SITE_DATA.mkdir(parents=True, exist_ok=True)
    s = session()
    report: list[str] = []
    say = report.append

    print("Loading sources")
    nysed, nysed_meta = load_nysed(s, args.refresh)
    lcgms_all, doe, lcgms_meta = load_lcgms(s, args.refresh)
    kml, kml_meta = load_campus_map(s, args.refresh)
    overrides = pd.read_csv(OUT / "overrides.csv", dtype=str).fillna("")

    # ---- 1. which schools operate in 2026-27: the DOE report is the frame
    doe = doe.rename(columns={"ATS System Code": "doe_code", "Location Name": "doe_name", "BEDS Number": "beds"})
    excluded = overrides[overrides.action == "exclude"].set_index("doe_code").reason.to_dict()
    say(f"# Data build checks\n\nGenerated by `scripts/build_data.py`. Every number below is computed from the source files.\n")
    say("## Sources\n")
    say(f"- NYSED directory: **{nysed_meta['title']}** ({nysed_meta['url']}) - {len(nysed)} NYC rows")
    say(f"- NYC DOE LCGMS report: **{lcgms_meta['file']}** - {len(doe)} rows managed by Charter")
    say(f"- NYC Charter School Center campus map, layer **{kml_meta['layer']}** - {len(kml)} campuses")
    say(f"- Benchmark: NYC Charter School Center, *NYC Charter School Facts 2026-2027* (as of August 2026) - {BENCHMARK['total']} schools\n")

    say("## Step 1. Which schools operate in 2026-27\n")
    say("The DOE report lists every school the DOE expects to operate this school year, updated each July.\n")
    outside = doe[doe["Administrative District Name"].fillna("").str.contains("OUTSIDE")]
    for _, r in outside.iterrows():
        say(f"- Excluded {r.doe_name} ({r.City}): DOE marks it as outside NYC")
    doe = doe.drop(outside.index)
    rows_excl = doe[doe.doe_code.isin(excluded)]
    for _, r in rows_excl.iterrows():
        say(f"- Excluded `{r.doe_code}` {r.doe_name}: {excluded[r.doe_code]}")
    doe = doe[~doe.doe_code.isin(excluded)].copy()
    assert doe.doe_code.is_unique, "duplicate DOE codes"
    doe["borough"] = doe.doe_code.str[2].map(BORO_FROM_CODE)
    assert doe.borough.notna().all(), doe[doe.borough.isna()]
    doe["grades_list"] = doe.Grades.map(doe_grades)
    no_grades = doe[doe.grades_list.str.len() == 0]
    assert no_grades.empty, f"schools with no grades: {no_grades.doe_name.tolist()}"
    say(f"\n**{len(doe)} schools** operate in NYC in 2026-27.\n")

    # ---- 2. match to NYSED for authorizer, year opened, website
    nysed["serial"] = nysed["SED Code"].str[-6:]
    doe["serial"] = doe.beds.fillna("").str[-6:]
    nysed["nk"] = nysed["Charter School Name"].map(norm_name)
    doe["nk"] = doe.doe_name.map(norm_name)
    nysed_serial_counts = nysed.serial.value_counts()
    doe_serial_counts = doe.serial.value_counts()
    manual = overrides[overrides.action == "match"].set_index("doe_code").nysed_sed_code.to_dict()
    by_code = nysed.set_index("SED Code")
    match, how = {}, {}
    for _, r in doe.iterrows():
        sed = None
        if r.doe_code in manual:
            sed, h = manual[r.doe_code], "manual"
        elif r.beds in by_code.index:
            sed, h = r.beds, "BEDS code"
        elif r.serial.startswith("86") and nysed_serial_counts.get(r.serial, 0) == 1 and doe_serial_counts.get(r.serial) == 1:
            sed, h = nysed.loc[nysed.serial == r.serial, "SED Code"].iloc[0], "serial number"
        else:
            hit = nysed[nysed.nk == r.nk]
            if len(hit) == 1:
                sed, h = hit["SED Code"].iloc[0], "name"
        if sed:
            match[r.doe_code], how[r.doe_code] = sed, h
    doe["sed_code"] = doe.doe_code.map(match)
    doe["match_how"] = doe.doe_code.map(how)
    assert doe.sed_code.dropna().is_unique, "two DOE schools matched one NYSED school"
    say("## Step 2. Match to the NYSED directory\n")
    say("Matched by exact BEDS code, then by the 6-digit school serial (when unique), then by name, then by a short list of manual decisions in `data/overrides.csv`.\n")
    for k, v in doe.match_how.value_counts(dropna=False).items():
        say(f"- {k if isinstance(k, str) else 'not matched'}: {v}")
    unmatched_doe = doe[doe.sed_code.isna()]
    assert unmatched_doe.empty, f"DOE schools not in NYSED: {unmatched_doe.doe_name.tolist()}"
    m = doe.merge(nysed, left_on="sed_code", right_on="SED Code", how="left")
    renamed = m[m.nk_x != m.nk_y]
    renamed = renamed[[not (a in b or b in a) for a, b in zip(renamed.nk_x, renamed.nk_y)]]
    say(f"\nSchools whose DOE name differs from the NYSED name (renames; both names are searchable on the map): {len(renamed)}\n")
    for _, r in renamed.iterrows():
        say(f"- {r['Charter School Name']} -> {r.doe_name}")
    not_open = nysed[~nysed["SED Code"].isin(doe.sed_code)]
    say(f"\nNYSED rows not operating in 2026-27 (approved but not yet open, or closed): **{len(not_open)}**\n")
    for _, r in not_open.sort_values("Charter School Name").iterrows():
        say(f"- {r['Charter School Name']} ({r.County}, opened/approved {r['Year Opened']}, {r.Authorizer})")

    websites = load_websites(s, nysed, args.refresh)
    m["website"] = m.nysed_page.map(lambda u: websites.get(u, "") if isinstance(u, str) else "")

    # ---- 3. benchmark
    say("\n## Step 3. Count vs. the official benchmark\n")
    say("| | This map | Charter Center (Aug 2026) | Difference |\n|---|---|---|---|")
    counts = m.borough.value_counts()
    for b in ["Bronx", "Brooklyn", "Manhattan", "Queens", "Staten Island"]:
        say(f"| {b} | {counts.get(b, 0)} | {BENCHMARK[b]} | {counts.get(b, 0) - BENCHMARK[b]:+d} |")
    say(f"| **Total** | **{len(m)}** | **{BENCHMARK['total']}** | **{len(m) - BENCHMARK['total']:+d}** |\n")
    m["open_date"] = pd.to_datetime(m["Open Date"].str.replace(r"\s+", " ", regex=True), errors="coerce")
    new = m[m.open_date >= "2026-01-01"].sort_values("open_date")
    say("Schools that opened in 2026 (the benchmark is a snapshot, so timing can explain small differences):\n")
    for _, r in new.iterrows():
        say(f"- {r.doe_name} ({r.borough}), DOE open date {r.open_date:%b %-d %Y}")

    # ---- 4. campuses
    say("\n## Step 4. Campuses\n")
    kml_codes = set(kml.doe_code)
    gone = kml[~kml.doe_code.isin(m.doe_code)]
    say(f"Campus map rows: {len(kml)} for {kml.doe_code.nunique()} schools.\n")
    say(f"Dropped {len(gone)} campus rows for schools that do not operate in 2026-27:\n")
    for _, r in gone.iterrows():
        say(f"- {r.campus_name} (`{r.doe_code}`), {r['Full Address']}")
    camp = kml[kml.doe_code.isin(m.doe_code)].copy()
    camp["address"] = camp["Full Address"]
    camp["listed_borough"] = camp["Borough"]
    camp["grades_list"] = camp["Grades Served at this Campus"].map(parse_grades)
    camp["district"] = camp.CSD.str.replace("CSD ", "", regex=False).str.lstrip("0")
    camp["campus_source"] = "Charter Center campus map"
    missing = m[~m.doe_code.isin(kml_codes)]
    say(f"\nSchools not on the campus map (mostly new in 2026); placed at their DOE address: {len(missing)}\n")
    extra = []
    for _, r in missing.iterrows():
        say(f"- {r.doe_name}: {r['Primary Address']}, {r.borough}")
        extra.append({
            "doe_code": r.doe_code, "building_code": r["Building Code"], "campus_name": r.doe_name,
            "address": f"{r['Primary Address']}, {r.borough}, NY {str(r.Zip).split('.')[0]}",
            "grades_list": r.grades_list, "district": str(r["Geographical District Code"]).lstrip("0"),
            "campus_source": "DOE report", "listed_borough": r.borough,
        })
    camp = pd.concat([camp, pd.DataFrame(extra)], ignore_index=True)
    camp["lat_override"] = None
    camp["lon_override"] = None

    # Documented manual corrections (data/overrides.csv), each with its source.
    fixes = overrides[overrides.action.isin(["move_campus", "drop_campus"])]
    say(f"\nManual campus corrections from `data/overrides.csv`: {len(fixes)}\n")
    for _, o in fixes.iterrows():
        hit = (camp.doe_code == o.doe_code) & camp.address.str.contains(o.match_address, case=False, regex=False)
        assert hit.sum() == 1, f"override matched {hit.sum()} campuses: {o.to_dict()}"
        before = camp.loc[hit, "address"].iloc[0]
        if o.action == "drop_campus":
            camp = camp[~hit].copy()
            say(f"- Dropped {before} (`{o.doe_code}`): {o.reason}")
        else:
            camp.loc[hit, "address"] = o.new_address
            camp.loc[hit, "campus_source"] = "Charter Center campus map, address corrected"
            if o.lat:
                camp.loc[hit, "lat_override"] = float(o.lat)
                camp.loc[hit, "lon_override"] = float(o.lon)
            say(f"- Moved `{o.doe_code}` from {before} to {o.new_address}: {o.reason}")

    # ---- 5. geocode + checks
    say("\n## Step 5. Locations and quality checks\n")
    camp["address"] = camp.address.str.replace(r"\s+", " ", regex=True).str.strip()
    geo = geocode(s, camp.address.tolist(), args.refresh)
    camp["geo"] = camp.address.map(geo)
    pinned = camp.lat_override.notna()
    camp.loc[pinned, "geo"] = [
        {"lon": r.lon_override, "lat": r.lat_override, "label": r.address, "borough": r.listed_borough
         if isinstance(r.listed_borough, str) else r.address.split(",")[1].strip(),
         "bin": None, "quality": "coordinates from school website"} for _, r in camp[pinned].iterrows()]
    failed = camp[camp.geo.isna()]
    assert failed.empty, f"geocode failed: {failed.address.tolist()}"
    camp["lon"] = camp.geo.map(lambda g: g["lon"])
    camp["lat"] = camp.geo.map(lambda g: g["lat"])
    camp["bin"] = camp.geo.map(lambda g: g["bin"])
    camp["location"] = camp.geo.map(lambda g: g["quality"])
    camp["geo_borough"] = camp.geo.map(lambda g: g["borough"])
    camp["borough"] = camp.geo_borough
    # Checks
    in_box = camp.lon.between(NYC_BBOX[0], NYC_BBOX[2]) & camp.lat.between(NYC_BBOX[1], NYC_BBOX[3])
    say(f"- Every point inside NYC: {'PASS' if in_box.all() else 'FAIL'} ({in_box.sum()}/{len(camp)})")
    boro_ok = camp.geo_borough == camp.listed_borough
    say(f"- Building's borough matches the borough written in the source address: {boro_ok.sum()}/{len(camp)}. "
        "Where they differ, the ZIP code and geocoder agree on the real borough, which the map uses:")
    for _, r in camp[~boro_ok].iterrows():
        say(f"  - {r.campus_name}: \"{r.address}\" is in {r.geo_borough}")
    home = m.set_index("doe_code").borough
    cross = camp[camp.borough != camp.doe_code.map(home)]
    say(f"- Campuses in a different borough from the school's DOE borough: {len(cross)} (kept; a charter can run campuses in several boroughs)")
    for _, r in cross.iterrows():
        say(f"  - {r.campus_name} ({home[r.doe_code]} school) at {r.address}")
    # DOE primary address should be one of the school's campus buildings
    doe_addr = {r.doe_code: f"{r['Primary Address']}, {r.borough}, NY {str(r.Zip).split('.')[0]}" for _, r in m.iterrows()}
    doe_geo = geocode(s, list(doe_addr.values()), args.refresh)
    bins = camp.groupby("doe_code").bin.apply(set)
    off = [(c, a) for c, a in doe_addr.items() if (doe_geo.get(a) or {}).get("bin") not in bins.get(c, set())]
    unverified = [a for a in doe_addr.values() if doe_geo.get(a) is None]
    say(f"- Schools whose DOE address is not one of their campus buildings: {len(off)}, of which {len(unverified)} DOE addresses could not be found in the city's address database "
        "(the DOE lists one address per school, often an older or administrative site; the map uses the campus list)")
    for c, a in off:
        say(f"  - {m.set_index('doe_code').doe_name[c]}: DOE {a}; campuses: "
            + "; ".join(camp[camp.doe_code == c].address))
    approx = camp[camp.geo.map(lambda g: g["quality"] == "approximate")]
    pinned_n = (camp.location == "coordinates from school website").sum()
    say(f"- Geocoded to the exact building: {len(camp) - len(approx) - pinned_n}/{len(camp)}; "
        f"placed with coordinates published by the school (building too new for the city database): {pinned_n}")
    say(f"- Address not in the city's address database, placed at the nearest address on the same side of the same street (marked approximate on the map): {len(approx)}")
    for _, r in approx.iterrows():
        say(f"  - {r.campus_name}: {r.address} -> {r.geo['label']}")
    dup = camp.duplicated(["doe_code", "bin"]) & camp.bin.notna()
    say(f"- Duplicate campus rows (same school, same building): {dup.sum()} removed")
    camp = camp[~dup].copy()
    office = camp[camp.address.str.contains(OFFICE_RE)]
    say(f"- Addresses that look like an office or mailing address (suite, floor, apt, room): {len(office)}")
    for _, r in office.iterrows():
        say(f"  - {r.campus_name}: {r.address}")
    # Grades: DOE (2026-27) is per school; the campus map (2025-26) is per campus. Use DOE wherever it can answer.
    doe_gr = m.set_index("doe_code").grades_list
    n_by_school = camp.groupby("doe_code").doe_code.transform("size")
    union = camp.groupby("doe_code").grades_list.apply(lambda s: set().union(*map(set, s)))
    single = n_by_school == 1
    # DOE grade lists for charters leave out Pre-K, so Pre-K is compared and kept from the campus map.
    k12 = lambda g: set(g) - {"PK"}
    agrees = camp.doe_code.map(lambda c: k12(union[c]) == set(doe_gr[c]))
    with_pk = lambda own, doe: (["PK"] if "PK" in own else []) + list(doe)
    camp.loc[single, "grades_list"] = pd.Series(
        [with_pk(g, doe_gr[c]) for g, c in zip(camp[single].grades_list, camp[single].doe_code)], index=camp[single].index)
    camp["grades_source"] = "DOE 2026-27"
    camp.loc[~single & agrees, "grades_source"] = "campus map, matches DOE 2026-27"
    camp.loc[~single & ~agrees, "grades_source"] = "campus map 2025-26"
    # A campus whose grades share nothing with the school's 2026-27 grades (ignoring a Pre-K-only site) is
    # contradicted by DOE: show the school's DOE grades instead and list it for review.
    conflict = [bool(k12(g)) and not (k12(g) & set(doe_gr[c])) for g, c in zip(camp.grades_list, camp.doe_code)]
    conflict = pd.Series(conflict, index=camp.index)
    for _, r in camp[conflict].iterrows():
        say(f"  - Grade conflict: {r.campus_name} at {r.address} lists {grade_span(r.grades_list)}, "
            f"but the school's DOE 2026-27 grades are {grade_span(doe_gr[r.doe_code])}; showing the school's grades")
    camp.loc[conflict, "grades_list"] = pd.Series([list(doe_gr[c]) for c in camp[conflict].doe_code], index=camp[conflict].index, dtype=object)
    camp.loc[conflict, "grades_source"] = "DOE 2026-27 school grades (campus list conflicted)"
    say(f"- Campus grades: single-campus schools use DOE 2026-27 grades ({single.sum()} campuses); "
        f"multi-campus schools whose campus grades add up to the DOE grades: {camp[~single & agrees].doe_code.nunique()} schools; "
        f"multi-campus schools where they differ (campus grades shown as 2025-26): {camp[~single & ~agrees].doe_code.nunique()} schools")
    nogr = camp[camp.grades_list.str.len() == 0]
    say(f"- Campuses with no grades listed: {len(nogr)}")
    every_school = set(m.doe_code) == set(camp.doe_code)
    say(f"- Every school has at least one campus: {'PASS' if every_school else 'FAIL'}")
    shared = camp.groupby("bin").doe_code.nunique()
    say(f"- Buildings shared by two or more schools: {(shared > 1).sum()} (largest: {shared.max()} schools in one building)")

    # ---- 5b. cross-check against a network's own published locations
    say("\n## Step 6. Cross-check against a school network's own website\n")
    sa = pd.read_csv(OUT / "crosscheck" / "success_academy_locations_2026-11.csv", dtype=str)
    sa["lat"], sa["lon"] = sa.lat.astype(float), sa.lon.astype(float)
    ours = camp[camp.doe_code.map(m.set_index("doe_code").doe_name).str.contains("Success Academy")]

    def addr_key(a):
        num, street = split_address(clean_address(a))
        return (num, street) if num else None

    def dist(lat1, lon1, lat2, lon2):
        return (((lat1 - lat2) * 111_000) ** 2 + ((lon1 - lon2) * 84_000) ** 2) ** 0.5

    ours = ours.assign(key=ours.address.map(addr_key))
    sa["key"] = sa.address.map(addr_key)

    def match(r):
        same = ours[ours.key == r.key]
        if len(same):
            return same.iloc[0], "same address"
        d = ours.apply(lambda q: dist(r.lat, r.lon, q.lat, q.lon), axis=1)
        return (ours.loc[d.idxmin()], "within 150 m") if d.min() <= 150 else (None, None)

    not_open = sa.status.fillna("").str.contains("2027-28")
    say("Success Academy publishes its locations with coordinates (snapshot: "
        "`data/crosscheck/success_academy_locations_2026-11.csv`, from successacademies.org/locations, Nov 2026). "
        "A location counts as found when the address is the same, or the published pin is within 150 m of a campus.\n")
    say(f"- Locations listed: {len(sa)}; marked as opening or reopening in 2027-28 (not open now): {not_open.sum()} "
        f"({', '.join(sa[not_open].name)})")
    res = sa[~not_open].apply(lambda r: match(r), axis=1)
    found = res.map(lambda x: x[0] is not None)
    say(f"- Open locations found on this map: {found.sum()}/{(~not_open).sum()}")
    matched_ids = set()
    far_pins = []
    for (_, r), (q, how) in zip(sa[~not_open].iterrows(), res):
        if q is None:
            say(f"  - Not on this map: {r['name']} (grades {r.grades}), {r.address}. City and state records do not tie "
                "this site to a charter school, so it is listed as a known gap rather than guessed.")
            continue
        # every campus at that address or within 150 m (several schools can share one building)
        matched_ids |= set(ours[(ours.key == r.key) | (ours.apply(lambda o: dist(r.lat, r.lon, o.lat, o.lon), axis=1) <= 150)].index)
        if how == "same address" and dist(r.lat, r.lon, q.lat, q.lon) > 300:
            far_pins.append((r, q))
    say(f"- Success Academy campuses on this map that match a published location: {len(matched_ids)}/{len(ours)}")
    for _, q in ours[~ours.index.isin(matched_ids)].iterrows():
        say(f"  - Not listed by Success Academy: {q.campus_name}, {q.address}")
    for r, q in far_pins:
        say(f"- Published pin far from its own address: {r['name']} ({r.address}) is pinned "
            f"{dist(r.lat, r.lon, q.lat, q.lon):.0f} m away; the city address database places the address where this map shows it.")

    # ---- 6. outputs
    m["authorizer"] = m.Authorizer.replace({"Regents": "Board of Regents", "NYCDOE": "NYC DOE"})
    m["grades"] = m.grades_list.map(grade_span)
    m["district"] = m["Geographical District Code"].astype(str).str.lstrip("0")
    m["name"] = m.doe_name.str.replace("–", "-").str.replace(r"\s+", " ", regex=True).str.strip()
    m["nysed_name"] = m["Charter School Name"].str.replace(r"\s+", " ", regex=True).str.strip()
    m["year_opened"] = m["Year Opened"]
    n_campus = camp.groupby("doe_code").size()
    m["campuses"] = m.doe_code.map(n_campus)
    m["phone"] = m["Principal Phone Number"].fillna("").str.strip()
    m["new_2026_27"] = m.open_date >= "2026-01-01"
    schools = m[["doe_code", "name", "nysed_name", "borough", "district", "grades", "authorizer", "year_opened",
                 "website", "phone", "new_2026_27", "campuses", "sed_code", "match_how"]].sort_values("name")
    schools.to_csv(OUT / "schools.csv", index=False)

    info = schools.set_index("doe_code")
    bin_count = camp.groupby("bin").doe_code.nunique()
    camp["grades"] = camp.grades_list.map(grade_span)
    camp["levels"] = camp.grades_list.map(lambda g: ", ".join(grade_bands(g)))
    camp["school"] = camp.doe_code.map(info.name)
    camp["schools_in_building"] = camp.bin.map(bin_count).fillna(1).astype(int)
    camp = camp.sort_values(["school", "campus_name"]).reset_index(drop=True)
    camp["campus_id"] = camp.doe_code + "-" + camp.building_code.fillna("") + "-" + camp.groupby(["doe_code", "building_code"]).cumcount().astype(str)
    cols = ["campus_id", "doe_code", "school", "campus_name", "address", "borough", "district", "grades", "grades_source", "levels",
            "lat", "lon", "location", "bin", "schools_in_building", "campus_source"]
    camp[cols].to_csv(OUT / "campuses.csv", index=False)

    feats = []
    for _, r in camp.iterrows():
        sc = info.loc[r.doe_code]
        feats.append({"type": "Feature",
                      "geometry": {"type": "Point", "coordinates": [round(r.lon, 6), round(r.lat, 6)]},
                      "properties": {
                          "school": sc["name"], "campus": r.campus_name if r.campus_name != sc["name"] else "",
                          "address": display_address(r.address), "borough": r.borough, "district": r.district,
                          "grades": r.grades, "grades_2025_26": r.grades_source == "campus map 2025-26", "levels": r.levels,
                          "authorizer": sc.authorizer,
                          "school_grades": sc.grades, "website": sc.website if isinstance(sc.website, str) else "",
                          "phone": sc.phone, "new": bool(sc.new_2026_27), "opened": sc.year_opened,
                          "school_campuses": int(sc.campuses), "schools_in_building": int(r.schools_in_building),
                          "also_known_as": sc.nysed_name if norm_name(sc.nysed_name) != norm_name(sc["name"]) else "",
                          "approximate": r.location == "approximate", "doe_code": r.doe_code,
                          "bin": r.bin if isinstance(r.bin, str) else None,
                      }})
    gj = {"type": "FeatureCollection", "features": feats}
    (OUT / "campuses.geojson").write_text(json.dumps(gj, indent=0, allow_nan=False))
    (SITE_DATA / "campuses.geojson").write_text(json.dumps(gj, separators=(",", ":"), allow_nan=False))
    zips = zip_centroids(s, args.refresh)
    (SITE_DATA / "zip_centroids.json").write_text(json.dumps(zips, separators=(",", ":"), sort_keys=True))

    say("\n## Result\n")
    say(f"- **{len(schools)} schools** at **{len(camp)} campuses** in **{camp.bin.nunique()} buildings**")
    for k, v in schools.authorizer.value_counts().items():
        say(f"- Authorizer {k}: {v} schools")
    say(f"- Schools with a website link: {(schools.website.fillna('') != '').sum()}/{len(schools)}")
    (OUT / "checks.md").write_text("\n".join(report) + "\n")
    print("\n".join(report))


if __name__ == "__main__":
    sys.exit(main())
