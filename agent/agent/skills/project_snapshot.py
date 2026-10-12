# Regenerable or machine-local: a snapshot is of the work, not of what a
# package manager or compiler can rebuild from it.
EXCLUDED = (
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "build",
    "dist",
    ".next",
    "target",
    ".gradle",
    "DerivedData",
)


async def run(args: dict, websocket, pending: dict) -> dict:
    """Save a point-in-time zip of a project's source under
    "~/Snapshots/<project>/<project>-<timestamp>.zip", and log it.

    Steps: file_info -> zip_folder (dependencies and build output left out;
    .git too unless include_git) -> append_text to the project's
    snapshots.log. The project is only read. Each snapshot has its own
    timestamped name, and zip_folder refuses to overwrite regardless.
    """
    from agent.skills import files_common as common

    log = common.StepLog(websocket, pending)
    source = common.resolve(args.get("path"))
    if source is None:
        return log.refuse("project_snapshot needs the project folder")

    info = await log.run("file_info", {"path": str(source)})
    if common.failed(info):
        return log.fail("file_info", info, "checking the project folder")
    if not info.get("exists") or info.get("kind") != "folder":
        return log.refuse(f"{source} is not a folder")

    root = common.resolve(args.get("snapshots_dir"), common.home() / "Snapshots") / source.name
    if root == source or source in root.parents:
        return log.refuse("snapshots cannot be stored inside the project they are a snapshot of")
    stamp = common.now().strftime("%Y%m%d-%H%M%S")
    destination = root / f"{source.name}-{stamp}.zip"
    exclude = list(EXCLUDED) + ([] if common.as_bool(args.get("include_git")) else [".git"])

    zipped = await log.run("zip_folder", {"source": str(source), "destination": str(destination), "exclude": exclude})
    if common.failed(zipped):
        return log.fail("zip_folder", zipped, f"zipping {source.name}")

    note = " ".join(str(args.get("note") or "").split())[:200]
    line = f"[{common.now().strftime('%Y-%m-%d %H:%M')}] {destination.name} ({zipped.get('size')})"
    if note:
        line += f" -- {note}"
    logged = await log.run("append_text", {"path": str(root / "snapshots.log"), "text": line})
    if common.failed(logged):
        return log.fail("append_text", logged, "recording the snapshot (the zip itself was saved)", path=str(destination))

    return log.done(
        f"Saved a snapshot of {source.name}: {common.plural(zipped.get('file_count', 0), 'file')}, "
        f"{zipped.get('size')}, in the Snapshots folder. Dependencies and build output were left out.",
        path=zipped.get("path"),
        file_count=zipped.get("file_count"),
        size=zipped.get("size"),
    )
