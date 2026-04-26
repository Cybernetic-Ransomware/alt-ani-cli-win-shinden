"""Dump full content of search rows to understand what shinden returns."""
import sys
sys.path.insert(0, "src")

from alt_ani_cli.shinden.http import make_client
from alt_ani_cli.config import SHINDEN_BASE
from selectolax.parser import HTMLParser

client = make_client()

# Try the correct param 'q' with referer set to the search page
query = sys.argv[1] if len(sys.argv) > 1 else "fate strange fake"
resp = client.get(
    f"{SHINDEN_BASE}/series",
    params={"q": query},
    headers={"Referer": f"{SHINDEN_BASE}/series"},
)
tree = HTMLParser(resp.text)
rows = tree.css("ul.div-row")
print(f"q={query!r} -> {len(rows)} rows")
print(f"Final URL: {resp.url}\n")

for i, row in enumerate(rows):
    links = [(a.attributes.get("href",""), a.text(strip=True)) for a in row.css("a[href]") if a.text(strip=True)]
    cols  = [li.text(strip=True) for li in row.css("li") if li.text(strip=True)]
    if links or cols:
        print(f"Row {i}: {cols[:4]} | links: {[t for _,t in links[:3]]}")

# Also check if there's a "no results" or count indicator
print()
for node in tree.css("p, span, div, h2, h3"):
    t = node.text(strip=True)
    if any(w in t.lower() for w in ("wynik", "result", "brak", "found", "znaleziono")):
        print(f"  match indicator: {t[:120]!r}")
        break

# Check total page vs filtered page HTML size difference
resp_empty = client.get(f"{SHINDEN_BASE}/series")
print(f"\nNo-param page size:   {len(resp_empty.text)}")
print(f"q=query page size:    {len(resp.text)}")

# Try POST instead of GET
resp_post = client.post(f"{SHINDEN_BASE}/series", data={"q": query})
tree_post = HTMLParser(resp_post.text)
print(f"\nPOST q={query!r}: {len(tree_post.css('ul.div-row'))} rows, status={resp_post.status_code}")
