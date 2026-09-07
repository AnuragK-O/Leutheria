import shutil


def get_disk_space() -> dict:
    total, used, free = shutil.disk_usage("/")
    gb = 1024**3
    return {
        "ok": True,
        "total_gb": round(total / gb, 1),
        "used_gb": round(used / gb, 1),
        "free_gb": round(free / gb, 1),
    }
