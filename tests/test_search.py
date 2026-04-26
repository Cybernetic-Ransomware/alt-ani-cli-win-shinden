from alt_ani_cli.shinden.search import _parse_results


def _row(series_id, slug, title, series_type="TV", genres=("Akcja",)):
    genre_links = "".join(
        f'<li class="genre-col"><a href="/series?genres[]={g}">{g}</a></li>'
        for g in genres
    )
    return (
        f'<ul class="div-row">'
        f'<li class="title-kind-col">'
        f'<a href="/series/{series_id}-{slug}">{title}</a>'
        f'</li>'
        f'<li class="type-col">{series_type}</li>'
        f'<li class="episodes-col">12</li>'
        f'{genre_links}'
        f'</ul>'
    )


_HEADER_ROW = (
    '<ul class="div-row">'
    '<li class="title-kind-col"><a href="/series">Nazwa</a></li>'
    '<li class="type-col"><a href="/series?type=TV">Typ</a></li>'
    '</ul>'
)

_SINGLE_HTML = f"<html><body>{_row('65137','fate-strange-fake','Fate/strange Fake')}</body></html>"

_MULTI_HTML = (
    "<html><body>"
    + _HEADER_ROW
    + _row("65137", "fate-strange-fake", "Fate/strange Fake", "TV")
    + _row("12345", "demon-slayer", "Demon Slayer", "TV")
    + "</body></html>"
)

_GENRES_HTML = (
    "<html><body>"
    + _row("65137", "fate-strange-fake", "Fate/strange Fake", "TV", ("Akcja", "Magia"))
    + "</body></html>"
)

_EMPTY_HTML = "<html><body><p>Brak wynikow.</p></body></html>"

_DUPE_HTML = (
    "<html><body>"
    + _row("65137", "fate-strange-fake", "Fate/strange Fake")
    + _row("65137", "fate-strange-fake", "Fate/strange Fake (kopia)")
    + "</body></html>"
)


def test_single_result():
    hits = _parse_results(_SINGLE_HTML)
    assert len(hits) == 1
    assert hits[0].id == "65137"
    assert hits[0].title == "Fate/strange Fake"


def test_title_extracted_correctly():
    hits = _parse_results(_SINGLE_HTML)
    assert hits[0].title == "Fate/strange Fake"


def test_url_is_absolute():
    hits = _parse_results(_SINGLE_HTML)
    assert hits[0].url == "https://shinden.pl/series/65137-fate-strange-fake"


def test_slug_extracted():
    hits = _parse_results(_SINGLE_HTML)
    assert hits[0].slug == "fate-strange-fake"


def test_series_type_extracted():
    hits = _parse_results(_MULTI_HTML)
    fate = next(h for h in hits if h.id == "65137")
    assert fate.series_type == "TV"


def test_header_row_skipped():
    hits = _parse_results(_MULTI_HTML)
    # header row has no /series/{id}-{slug} link — must not appear in results
    ids = {h.id for h in hits}
    assert ids == {"65137", "12345"}


def test_genre_links_not_treated_as_series():
    hits = _parse_results(_GENRES_HTML)
    assert len(hits) == 1
    assert hits[0].id == "65137"


def test_deduplication():
    hits = _parse_results(_DUPE_HTML)
    assert len(hits) == 1


def test_empty_page():
    assert _parse_results(_EMPTY_HTML) == []


def test_multiple_results_order():
    hits = _parse_results(_MULTI_HTML)
    ids = [h.id for h in hits]
    assert "65137" in ids and "12345" in ids
