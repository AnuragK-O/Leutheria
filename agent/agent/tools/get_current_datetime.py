from datetime import datetime


def get_current_datetime() -> dict:
    now = datetime.now()
    return {
        "ok": True,
        "iso": now.isoformat(),
        "human": now.strftime("%A, %B %d, %Y at %I:%M %p"),
    }
