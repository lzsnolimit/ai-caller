import re


class GatewayError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def address(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", value):
        raise GatewayError("invalid_address")
    return value.upper()


def phone_number(value):
    """A dialed number or numeric caller ID. Names and withheld IDs are not stored."""
    if isinstance(value, str) and re.fullmatch(r"\+?[0-9]{1,20}", value):
        return value
    return None
