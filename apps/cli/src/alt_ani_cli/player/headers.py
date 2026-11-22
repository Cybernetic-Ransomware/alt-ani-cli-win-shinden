from collections.abc import Mapping


def get_header(headers: Mapping[str, str], name: str) -> str | None:
    lname = name.lower()
    return next((v for k, v in headers.items() if k.lower() == lname), None)
