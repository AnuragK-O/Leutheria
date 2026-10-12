import os
import stat
from pathlib import Path

from agent.tools import fs_common

MAX_CHILDREN_COUNTED = 10_000


def file_info(path: str) -> dict:
    """Read-only facts about one path. A path that doesn't exist is an
    answer ("exists": False), not an error -- "is there a file called X" is
    one of the things this is for, and skills use it to pick a free name."""
    target = Path(path).expanduser()
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        return {"ok": True, "exists": False, "path": str(target), "message": f"{target} does not exist"}
    except OSError as e:
        return {"ok": False, "error": str(e)}

    if stat.S_ISLNK(st.st_mode):
        kind = "symlink"
    elif stat.S_ISDIR(st.st_mode):
        kind = "folder"
    elif stat.S_ISREG(st.st_mode):
        kind = "file"
    else:
        kind = "other"

    info = {
        "ok": True,
        "exists": True,
        "path": str(target),
        "name": target.name,
        "parent": str(target.parent),
        "kind": kind,
        "hidden": fs_common.is_hidden(target.name),
        "modified": fs_common.iso_minutes(st.st_mtime),
    }
    birth = getattr(st, "st_birthtime", None)
    if birth:
        info["created"] = fs_common.iso_minutes(birth)

    if kind == "file":
        info["extension"] = fs_common.suffix_of(target.name)
        info["size_bytes"] = st.st_size
        info["size"] = fs_common.human_size(st.st_size)
        info["message"] = f"{target.name} is a {info['size']} file, last modified {info['modified']}"
    elif kind == "folder":
        try:
            count = 0
            with os.scandir(target) as it:
                for _ in it:
                    count += 1
                    if count >= MAX_CHILDREN_COUNTED:
                        break
            info["item_count"] = count
            info["message"] = (
                f"{target.name} is a folder with {count} item{'' if count == 1 else 's'} directly inside, "
                f"last modified {info['modified']}"
            )
        except OSError:
            info["message"] = f"{target.name} is a folder that cannot be read"
    elif kind == "symlink":
        try:
            info["link_target"] = os.readlink(target)
        except OSError:
            pass
        info["message"] = f"{target.name} is a link"
    else:
        info["message"] = f"{target.name} is not a regular file or folder"
    return info
