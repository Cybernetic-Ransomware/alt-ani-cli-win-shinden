"""selectolax (lexbor) backend; its Node already satisfies the contract, including identity hashing by mem_id."""

from selectolax.parser import HTMLParser


def parse(html: str) -> HTMLParser:
    return HTMLParser(html)
