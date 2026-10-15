"""The PR workflow's decision table (step 4).

    decide.py --structural <structural.json> [--screen <result.json>]
              [--human-approved] --out <decision.json>

Inputs: check_submission's result and screen/screen.py's result
({verdict, risk, findings, summary, description_matches_steps,
static: {verdict, findings, ...}, model, screen_version}).

    structural failure                       -> close
    --human-approved (maintainer's label)    -> merge, verdict human_approved
    screener reject, or static reject        -> close
    no / malformed screener result           -> review   (fail toward a human)
    screener flag, static not "clean",
      risk high, or description mismatch     -> review
    otherwise (approve + clean)              -> merge, verdict auto_approved

Writes {"action": "merge"|"review"|"close", "verdict": "auto_approved"|
"human_approved"|null, "reasons": [...], "comment": "<markdown for the PR>"}.
Always exits 0 when it could decide; 2 on a usage error.
"""

import argparse
import json
import sys
from pathlib import Path

import common


def screen_problems(screen) -> list:
    if not isinstance(screen, dict):
        return ["screener result is not an object"]
    problems = []
    if screen.get("verdict") not in ("approve", "flag", "reject"):
        problems.append("screener verdict missing or unknown")
    if screen.get("risk") not in common.RISKS:
        problems.append("screener risk missing or unknown")
    if not isinstance(screen.get("findings"), list):
        problems.append("screener findings missing")
    if not isinstance(screen.get("static"), dict):
        problems.append("static analysis result missing")
    if not isinstance(screen.get("description_matches_steps"), bool):
        problems.append("description_matches_steps missing")
    return problems


def _findings_md(screen: dict) -> str:
    lines = []
    for f in (screen.get("static") or {}).get("findings") or []:
        lines.append(f"- static: {common.flag_text(f)}")
    for f in screen.get("findings") or []:
        lines.append(f"- screener: {common.flag_text(f)}")
    return "\n".join(lines[:40]) or "- (none)"


def decide(structural: dict, screen, human_approved: bool) -> dict:
    if not structural.get("ok"):
        errors = structural.get("errors") or ["structural check failed"]
        return {"action": "close", "verdict": None, "reasons": errors,
                "comment": "This submission can't be accepted:\n\n" + "\n".join(f"- {e}" for e in errors)}
    if human_approved:
        return {"action": "merge", "verdict": "human_approved", "reasons": ["approved by a maintainer"],
                "comment": "Approved by a maintainer; merging and publishing."}
    problems = screen_problems(screen) if screen is not None else ["no screener result"]
    if problems:
        return {"action": "review", "verdict": None, "reasons": problems,
                "comment": "Automatic screening didn't complete, so a maintainer will review this:\n\n"
                           + "\n".join(f"- {p}" for p in problems)}
    static = screen["static"]
    summary = common.summary_of(str(screen.get("summary") or ""))
    if screen["verdict"] == "reject" or static.get("verdict") == "reject":
        return {"action": "close", "verdict": None, "reasons": ["rejected by screening"],
                "comment": f"Screening rejected this skill.\n\n{summary}\n\n{_findings_md(screen)}"}
    reasons = []
    if screen["verdict"] == "flag":
        reasons.append("screener flagged it")
    if static.get("verdict") != "clean":
        reasons.append("static analysis needs review")
    if screen["risk"] == "high":
        reasons.append("high risk")
    if not screen["description_matches_steps"]:
        reasons.append("description does not match the steps")
    if reasons:
        return {"action": "review", "verdict": None, "reasons": reasons,
                "comment": "A maintainer will review this skill ("
                           + ", ".join(reasons) + f").\n\n{summary}\n\n{_findings_md(screen)}"}
    return {"action": "merge", "verdict": "auto_approved", "reasons": ["screening passed"],
            "comment": f"Screening passed; merging and publishing.\n\n{summary}"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Decide what happens to a submission")
    parser.add_argument("--structural", type=Path, required=True)
    parser.add_argument("--screen", type=Path)
    parser.add_argument("--human-approved", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        structural = common.read_json(args.structural)
    except (OSError, ValueError) as e:
        structural = {"ok": False, "errors": [f"structural result unreadable: {e}"]}
    screen = None
    if args.screen is not None:
        try:
            screen = common.read_json(args.screen)
        except (OSError, ValueError):
            screen = "unreadable"
    decision = decide(structural, screen, args.human_approved)
    common.write_json(args.out, decision)
    print(f"{decision['action'].upper()}  {'; '.join(decision['reasons'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
