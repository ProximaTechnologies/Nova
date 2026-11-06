#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

sarif_path = Path(sys.argv[1])
tool_name = sys.argv[2] if len(sys.argv) > 2 else sarif_path.stem
limit = int(os.environ.get("SARIF_SUMMARY_LIMIT", "50"))
summary_path = os.environ.get("GITHUB_STEP_SUMMARY")


def esc(x):
    return str(x or "").replace("|", "\\|").replace("\n", " ").strip()


with open(summary_path, "a", encoding="utf-8") as out:
    out.write(f"\n## {esc(tool_name)}\n\n")

    if not sarif_path.exists() or sarif_path.stat().st_size == 0:
        out.write("No SARIF file generated.\n\n")
        sys.exit(0)

    data = json.loads(sarif_path.read_text(encoding="utf-8"))
    rows = []

    for run in data.get("runs", []):
        for result in run.get("results", []):
            loc = ""
            locations = result.get("locations", [])
            if locations:
                phys = locations[0].get("physicalLocation", {})
                file = phys.get("artifactLocation", {}).get("uri", "")
                line = phys.get("region", {}).get("startLine", "")
                loc = f"{file}:{line}" if file and line else file

            props = result.get("properties", {})
            rows.append({
                "severity": props.get("security-severity")
                            or props.get("severity")
                            or result.get("level")
                            or "unknown",
                "rule": result.get("ruleId", ""),
                "location": loc,
                "message": result.get("message", {}).get("text", ""),
            })

    if not rows:
        out.write("No findings.\n\n")
        sys.exit(0)

    out.write(f"Findings: **{len(rows)}**\n\n")
    out.write("| Severity | Rule | Location | Message |\n")
    out.write("|---|---|---|---|\n")

    for row in rows[:limit]:
        out.write(
            f"| {esc(row['severity'])} "
            f"| `{esc(row['rule'])}` "
            f"| `{esc(row['location'])}` "
            f"| {esc(row['message'])} |\n"
        )

    if len(rows) > limit:
        out.write(f"\nShowing first {limit} findings. Download the SARIF artifact for the full report.\n")

    out.write("\n")
