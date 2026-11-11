"""English technical message templates; applications translate by error code, never by parsing this text."""

MESSAGES: dict[str, dict[str, str]] = {
    "cli": {
        "filter_no_match": "No players match the requested filters ({filters}). Pass --allow-fallback to use the full "
        "player list.",
        "invalid_xhr_backend": "ALT_ANI_CLI_XHR_BACKEND must be 'new' or 'api4', got {value}.",
    },
    "api": {
        "xhr_players_changed": "The player list of this episode changed after the token refresh ({detail}). Automatic "
        "attempts stopped — reload the episode to see the current players.",
        "xhr_failed": "shinden player API {stage} failed for online_id={online_id}: {detail}",
        "xhr_access_blocked": "shinden blocked access to its player API on {stage} ({detail}). Automatic attempts stopped "
        "— you can retry later by picking a player manually.",
        "xhr_unavailable": "shinden player API is unavailable on {stage} ({detail}). Automatic attempts stopped — you can "
        "retry later by picking a player manually.",
        "xhr_protocol": "shinden player API returned an unexpected response on {stage} ({detail}). Automatic attempts stopped.",
        "retry_after_hint": "The server asks to wait {seconds} s.",
        "no_iframe": "player_show returned no iframe for online_id={online_id} ({length} characters). Try setting "
        "ALT_ANI_CLI_ANTIBOT_DELAY=7",
        "iframe_no_src": "iframe has no src for online_id={online_id}",
        "unexpected_iframe_src": "iframe src is not an http(s) URL for online_id={online_id}",
    },
    "series": {
        "parse_error": "Cannot extract series ID from URL: {url}",
        "age_gate": "Shinden requires age verification (18+) for this series.\n"
        "Log in to shinden.pl in your browser and confirm age, then copy the session cookies to a cookies "
        "file or use --url with a logged-in session.",
    },
    "shinden": {
        "parser_drift": "shinden page structure changed ({page}): {detail}. The parser needs an update.",
        "challenge": "shinden returned a Cloudflare challenge page instead of content ({page}) — the cf_clearance "
        "cookie is probably no longer accepted.",
        "episodes_incomplete": "Episode list for {title} is incomplete ({fetched}/{total} pages, {reason}). Automatic "
        "episode selection needs the full list — retry later or pick episodes interactively.",
    },
    "extract": {
        "js_only_host": "{host} requires JavaScript execution (pure SPA — no static video URL)",
        "unsupported_host": "{host} is not supported — pick another player",
        "encrypted_host": "{host} serves end-to-end encrypted files — no player or extractor can decrypt the stream; "
        "pick another player",
        "ytdlp_failed": "yt-dlp could not extract stream from {host}: {exc}",
        "all_failed": "All extractors failed for {host}",
        "all_failed_exc": "All extractors failed for {host}: {exc}",
    },
    "dood": {
        "bad_base_url": "dood: cannot parse base URL from {embed_url}",
        "no_pass_md5": "dood: no pass_md5 path found in {embed_url}",  # nosec B105
    },
    "jwplayer": {"no_video_url": "jwplayer extractor: cannot find video URL in {embed_url}"},
    "mp4upload": {"no_video_url": "mp4upload: cannot find video URL in {embed_url}"},
    "streamtape": {"no_token": "streamtape: cannot parse token from {embed_url}"},  # nosec B105
    "vidara": {
        "bad_embed_url": "vidara: cannot parse filecode from {embed_url}",
        "no_stream_url": "vidara: no streaming_url in API response for {embed_url}",
    },
    "playmate": {
        "bad_embed_url": "playmate: cannot parse filecode from {embed_url}",
        "no_stream_url": "playmate: no sx URL in API response for {embed_url}",
    },
    "uqload": {
        "bad_embed_url": "uqload: cannot parse file code from {embed_url}",
        "no_video_url": "uqload: cannot find video URL in unpacked response for {embed_url}",
    },
    "flyf": {
        "bad_embed_url": "flyf: cannot parse token from {embed_url}",
        "no_stream_url": "flyf: no playable url in streaming/assign response for {embed_url}",
    },
    "lycoris": {
        "bad_embed_url": "lycoris: cannot parse id/episode from {embed_url}",
        "no_primary_source": "lycoris: no primarySource in API response for {embed_url}",
        "no_stream_url": "lycoris: no playable URL in API response for {embed_url}",
    },
    "ytdlp": {
        "no_info": "yt-dlp returned no info for {embed_url}",
        "no_url": "No playable URL in yt-dlp output for {embed_url}",
    },
    "download": {
        "failed": "Download failed.",
        "replace_failed": "Could not replace {path} — the previous file was kept unchanged (is it open in another program?).",
        "staging_locked": "Could not remove leftovers of a previous attempt: {path} — redownload aborted, the "
        "previous file was kept unchanged.",
        "staging_mismatch": "Could not prepare a temporary file next to {path} — redownload aborted, the previous "
        "file was kept unchanged.",
    },
}


def text(code: str, **params: object) -> str:
    section, _, key = code.partition(".")
    return MESSAGES[section][key].format(**params)
