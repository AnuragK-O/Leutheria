"""Run the registry's CI flow locally, on a copy, with the same scripts.

    local_pipeline.py submit --work <dir> --leutheria <dir> --skill <file.json>
                             --author <login> --key-file <pem> --keys <keys.json>
                             [--path skills/<name>/<hash>.json] [--screen-script <screen.py>]
                             [--human-approve [--override-static-reject]]
                             [--catalog <path>] [--now <iso>]
    local_pipeline.py yank   --work <dir> --leutheria <dir> --name <n> --content-hash <h>
                             --key-file <pem> --keys <keys.json> [--catalog <path>]

<work>/repo is "main" of the registry: copied from this repository on first
use and reused afterwards, so successive submissions build on each other.
A submission is staged as a PR head in <work>/head (main + one file), then:
check_submission -> screen.py (only if structural passes; default
<this repo>/screen/screen.py, absent means no result) -> decide -> on merge:
head becomes main, record_review, build_index, sign_index, verify_index --
the same order and the same scripts as .github/workflows/. Prints one JSON
summary {structural, decision, recorded, index, verified}; exits 0 if the
pipeline ran (whatever it decided), 1 if a step that must not fail did.
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import common

TOOLS = Path(__file__).resolve().parent


def run(script: str, *args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(TOOLS / script), *map(str, args)],
                          capture_output=True, text=True, timeout=120)


def _repo(work: Path) -> Path:
    repo = work / "repo"
    if not repo.exists():
        shutil.copytree(common.REGISTRY_ROOT, repo,
                        ignore=shutil.ignore_patterns(".git", "__pycache__", "index.json", "index.json.sig"))
    return repo


def _catalog_args(args):
    return ["--leutheria", args.leutheria] + (["--catalog", args.catalog] if args.catalog else [])


def publish(args, repo: Path) -> dict:
    out = {}
    build = run("build_index.py", "--repo", repo, *_catalog_args(args), *(["--now", args.now] if args.now else []))
    out["index"] = build.returncode == 0
    if not out["index"]:
        out["error"] = build.stderr.strip()
        return out
    sign = run("sign_index.py", "--index", repo / "index.json", "--out", repo / "index.json.sig",
               "--key-file", args.key_file)
    verify = run("verify_index.py", "--repo", repo, "--keys", args.keys, *_catalog_args(args),
                 *(["--now", args.now] if args.now else []))
    out["verified"] = sign.returncode == 0 and verify.returncode == 0
    if not out["verified"]:
        out["error"] = (sign.stderr + verify.stderr).strip()
    return out


def submit(args) -> dict:
    work = Path(args.work)
    repo = _repo(work)
    data = Path(args.skill).read_bytes()
    rel = args.path
    if rel is None:
        try:
            d = json.loads(data)
            rel = f"skills/{d['name']}/{d['content_hash']}.json"
        except (ValueError, KeyError, TypeError):
            rel = "skills/unnamed/unnamed.json"
    head = work / "head"
    if head.exists():
        shutil.rmtree(head)
    shutil.copytree(repo, head, symlinks=True)
    target = head / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)

    summary = {"path": rel}
    structural = run("check_submission.py", "--base", repo, "--head", head, *_catalog_args(args),
                     "--author", args.author, "--out", work / "structural.json")
    summary["structural"] = common.read_json(work / "structural.json")
    screen_path = work / "screen.json"
    screen_path.unlink(missing_ok=True)
    screen_script = Path(args.screen_script) if args.screen_script else common.REGISTRY_ROOT / "screen" / "screen.py"
    if structural.returncode == 0 and screen_script.is_file():
        catalog = args.catalog or common.default_catalog_path(args.leutheria)
        subprocess.run([sys.executable, str(screen_script), str(target), "--catalog", str(catalog),
                        "--out", str(screen_path)], capture_output=True, text=True, timeout=300)
    decide_args = ["--structural", work / "structural.json", "--out", work / "decision.json"]
    if screen_path.exists():
        decide_args += ["--screen", screen_path]
    if args.human_approve:  # stands for a verified approval of this exact head (human_approval.py)
        decide_args.append("--human-approved")
        if args.override_static_reject:
            decide_args.append("--override-static-reject")
    run("decide.py", *decide_args)
    decision = common.read_json(work / "decision.json")
    summary["decision"] = decision
    if decision["action"] != "merge":
        shutil.rmtree(head)
        return summary

    # Merge: the PR head becomes main.
    shutil.rmtree(repo)
    head.rename(repo)
    record = run("record_review.py", "--repo", repo, "--path", rel, "--verdict", decision["verdict"],
                 "--author", args.author, *(["--screen", screen_path] if screen_path.exists() else []),
                 *(["--published-at", args.now] if args.now else []))
    summary["recorded"] = record.returncode == 0
    if not summary["recorded"]:
        summary["error"] = record.stderr.strip()
        return summary
    summary.update(publish(args, repo))
    return summary


def yank_cmd(args) -> dict:
    repo = _repo(Path(args.work))
    y = run("yank.py", "--repo", repo, "--name", args.name, "--content-hash", args.content_hash,
            "--reason", args.reason, "--by", "local-maintainer")
    summary = {"yanked": y.returncode == 0}
    if not summary["yanked"]:
        summary["error"] = y.stderr.strip()
        return summary
    summary.update(publish(args, repo))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Simulate the registry CI flow locally")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("submit", "yank"):
        p = sub.add_parser(name)
        p.add_argument("--work", default=None)
        p.add_argument("--leutheria", required=True)
        p.add_argument("--catalog")
        p.add_argument("--key-file", required=True)
        p.add_argument("--keys", required=True)
        p.add_argument("--now")
    s = sub.choices["submit"]
    s.add_argument("--skill", required=True)
    s.add_argument("--author", required=True)
    s.add_argument("--path")
    s.add_argument("--screen-script")
    s.add_argument("--human-approve", action="store_true")
    s.add_argument("--override-static-reject", action="store_true")
    y = sub.choices["yank"]
    y.add_argument("--name", required=True)
    y.add_argument("--content-hash", required=True)
    y.add_argument("--reason", default="withdrawn")
    args = parser.parse_args()
    if args.work is None:
        args.work = tempfile.mkdtemp(prefix="leutheria_registry_")
    summary = submit(args) if args.cmd == "submit" else yank_cmd(args)
    summary["work"] = str(args.work)
    print(json.dumps(summary, indent=2))
    failed = summary.get("recorded") is False or summary.get("index") is False or \
        summary.get("verified") is False or summary.get("yanked") is False
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
