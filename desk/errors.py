"""Errors every layer raises, so none has to import another to name them."""
import json
from datetime import date, time


class Refused(ValueError):
    """The desk will not do this. The message says what was wrong and how to fix it."""


def shown(value) -> str:
    """A value as a JSON client wrote it (true, null, "text"), cut to 80 characters."""
    if isinstance(value, date | time):
        text = value.isoformat()
    else:
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError, OverflowError):
            text = repr(value)
    return text if len(text) <= 80 else text[:79] + "…"
