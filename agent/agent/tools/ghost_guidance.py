def ghost_guidance(x: int = 0, y: int = 0, width: int = 0, height: int = 0, label: str = "", action: str = "highlight") -> dict:
    """Provide visual ghost cursor highlight and label guidance on screen."""
    return {
        "ok": True,
        "guidance": {
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "label": label,
            "action": action,
        },
    }
