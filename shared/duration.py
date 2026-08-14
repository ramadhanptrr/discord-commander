from __future__ import annotations


def format_duration(seconds: float) -> str:
    total_minutes = max(int(seconds // 60), 0)
    hours, minutes = divmod(total_minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"
