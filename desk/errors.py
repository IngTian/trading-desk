"""Errors every layer raises, so none has to import another to name them."""


class Refused(ValueError):
    """The desk will not do this. The message says what was wrong and how to fix it."""
