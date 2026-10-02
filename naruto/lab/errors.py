"""The lab's refusal: a request it won't carry out, said plainly."""


class LabError(Exception):
    """A request the lab refuses; the message is for whoever asked.
    ``code``: invalid | not_found | conflict | forbidden | unauthorized."""

    def __init__(self, message: str, code: str = "invalid", **details):
        super().__init__(message)
        self.code = code
        self.details = details
