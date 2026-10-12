REPORT_FOLDER = "Leutheria Reports"
MAX_SETS_IN_REPORT = 50


async def run(args: dict, websocket, pending: dict) -> dict:
    """Find duplicate files in a folder and write the list to a text report,
    since a list of paths can't usefully be read aloud.

    Steps: find_duplicates (read-only) -> write_new_file (the report, in
    "~/Documents/Leutheria Reports", create-only). Nothing is moved or
    removed: deciding which copy to keep is the user's call.
    """
    from agent.skills import files_common as common

    log = common.StepLog(websocket, pending)
    folder = common.resolve(args.get("path"), common.downloads())

    found = await log.run("find_duplicates", {"path": str(folder), "limit": MAX_SETS_IN_REPORT})
    if common.failed(found):
        return log.fail("find_duplicates", found, f"checking {folder.name}")

    sets = found.get("duplicate_sets", 0)
    caveat = " The folder was too large to check completely, so there may be more." if found.get("incomplete") else ""
    if not sets:
        return log.done(f"No duplicate files in {folder.name}.{caveat}", duplicate_sets=0, report=None)

    summary = (
        f"Found {common.plural(sets, 'set')} of duplicate files in {folder.name}, "
        f"{found.get('wasted')} that could be freed."
    )
    if not common.as_bool(args.get("save_report"), True):
        return log.done(summary + caveat, duplicate_sets=sets, wasted=found.get("wasted"), report=None)

    moment = common.now()
    lines = [
        f"Duplicate files in {folder}",
        f"Checked {moment.strftime('%Y-%m-%d %H:%M')}. {sets} set(s), {found.get('wasted')} could be freed.",
        "Nothing has been moved or removed. Each set below is files with identical content.",
    ]
    if found.get("incomplete"):
        lines.append("The folder was too large to check completely; there may be more.")
    if found.get("truncated"):
        lines.append(f"Only the {MAX_SETS_IN_REPORT} largest sets are listed.")
    for number, group in enumerate(found.get("groups", []), start=1):
        lines.append("")
        lines.append(f"Set {number}: {group['count']} copies, {group['size']} each")
        lines.extend(f"  {file_path}" for file_path in group["files"])
        if group["count"] > len(group["files"]):
            lines.append(f"  ... and {group['count'] - len(group['files'])} more")

    reports = common.documents() / REPORT_FOLDER
    name = f"Duplicates in {folder.name or 'folder'} {moment.strftime('%Y-%m-%d %H%M%S')}.txt"
    report, error = await common.free_path(log, reports, name)
    if error:
        return error
    written = await log.run("write_new_file", {"path": str(report), "content": "\n".join(lines) + "\n"})
    if common.failed(written):
        return log.fail("write_new_file", written, "saving the report")

    return log.done(
        f"{summary} The full list is in {report.name}, in {REPORT_FOLDER} in Documents. Nothing was removed.{caveat}",
        duplicate_sets=sets,
        wasted=found.get("wasted"),
        report=written.get("path"),
    )
