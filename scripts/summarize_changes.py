#!/usr/bin/env python3
"""Write a plain-English summary of what changed in the data since the last commit.

Used by the scheduled data-update workflow as the pull request description.

Usage:
    python scripts/summarize_changes.py > change_summary.md
"""
from __future__ import annotations

import io
import re
import subprocess
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent


def previous(path: str) -> pd.DataFrame:
    try:
        old = subprocess.run(["git", "show", f"HEAD:{path}"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        return pd.read_csv(io.StringIO(old), dtype=str).fillna("")
    except subprocess.CalledProcessError:
        return pd.DataFrame()


def current(path: str) -> pd.DataFrame:
    return pd.read_csv(ROOT / path, dtype=str).fillna("")


def main() -> None:
    old_s, new_s = previous("data/schools.csv"), current("data/schools.csv")
    old_c, new_c = previous("data/campuses.csv"), current("data/campuses.csv")
    out = ["## Data update summary", ""]
    out.append("| | Before | After |")
    out.append("|---|---|---|")
    out.append(f"| Schools | {len(old_s)} | {len(new_s)} |")
    out.append(f"| Campuses | {len(old_c)} | {len(new_c)} |")
    out.append("")

    if len(old_s):
        added = new_s[~new_s.doe_code.isin(old_s.doe_code)]
        removed = old_s[~old_s.doe_code.isin(new_s.doe_code)]
        both = new_s.merge(old_s, on="doe_code", suffixes=("", "_old"))
        renamed = both[both.name != both.name_old]
        regraded = both[both.grades != both.grades_old]
        sections = [
            ("Schools added", [f"{r['name']} ({r.borough}, grades {r.grades})" for _, r in added.iterrows()]),
            ("Schools removed", [f"{r['name']} ({r.borough})" for _, r in removed.iterrows()]),
            ("Schools renamed", [f"{r.name_old} -> {r['name']}" for _, r in renamed.iterrows()]),
            ("Grade changes", [f"{r['name']}: {r.grades_old} -> {r.grades}" for _, r in regraded.iterrows()]),
        ]
        if len(old_c):
            key = ["doe_code", "address"]
            new_a = new_c.merge(old_c[key], on=key, how="left", indicator=True)
            old_a = old_c.merge(new_c[key], on=key, how="left", indicator=True)
            sections.append(("Campus addresses added", [f"{r.school}: {r.address}" for _, r in new_a[new_a._merge == "left_only"].iterrows()]))
            sections.append(("Campus addresses removed", [f"{r.school}: {r.address}" for _, r in old_a[old_a._merge == "left_only"].iterrows()]))
        for title, rows in sections:
            out.append(f"### {title} ({len(rows)})")
            out.extend([f"- {x}" for x in rows] or ["- None"])
            out.append("")

    checks = (ROOT / "data/checks.md").read_text()
    flagged = re.findall(r"^.*no longer applies.*$", checks, flags=re.M)
    if flagged:
        out.append("### Manual corrections to review")
        out.extend(flagged)
        out.append("")
    bench = re.search(r"## Step 3\..*?(?=\n## )", checks, flags=re.S)
    if bench:
        out.append(bench.group(0).replace("## Step 3.", "### Benchmark:"))
        out.append("")
    out.append("Full report: `data/checks.md`. Review the changes, spot-check a few schools on the preview, then merge to publish.")
    print("\n".join(out))


if __name__ == "__main__":
    main()
