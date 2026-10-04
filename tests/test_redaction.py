import pytest

from alt_ani_cli.redaction import REDACTED, redact_header_value, redact_headers, redact_text


@pytest.mark.unit
class TestRedactText:
    def test_single_query_value_redacted(self):
        assert redact_text("https://cdn/x.m3u8?token=SECRET") == f"https://cdn/x.m3u8?token={REDACTED}"

    def test_all_parameter_values_redacted(self):
        out = redact_text("https://cdn.example/a?a=1&token=2&expires=3")
        assert out == f"https://cdn.example/a?a={REDACTED}&token={REDACTED}&expires={REDACTED}"

    def test_non_secret_looking_values_redacted_too(self):
        out = redact_text("https://cdn.example/hls/master.m3u8?token=ABC&expires=123&quality=1080")
        assert out == f"https://cdn.example/hls/master.m3u8?token={REDACTED}&expires={REDACTED}&quality={REDACTED}"

    def test_url_without_query_unchanged(self):
        url = "https://player.example:8443/e/abc123/video.mp4"
        assert redact_text(url) == url

    def test_scheme_host_port_path_preserved(self):
        out = redact_text("https://cdn.example:8443/hls/v1/master.m3u8?sig=XYZ")
        assert out.startswith("https://cdn.example:8443/hls/v1/master.m3u8?sig=")
        assert "XYZ" not in out

    def test_bare_filename_with_query(self):
        out = redact_text("ERROR: fragment failed:\nmaster.m3u8?token=SECRET&x=1")
        assert out == f"ERROR: fragment failed:\nmaster.m3u8?token={REDACTED}&x={REDACTED}"

    def test_request_path_with_query(self):
        assert redact_text("GET /hls/master.txt?sig=ABC") == f"GET /hls/master.txt?sig={REDACTED}"

    def test_aws_presigned_query(self):
        out = redact_text("https://b.s3.amazonaws.com/k?X-Amz-Credential=AAA&X-Amz-Signature=BBB&X-Amz-Date=CCC")
        for secret in ("AAA", "BBB", "CCC"):
            assert secret not in out
        assert "X-Amz-Credential=" in out and "X-Amz-Signature=" in out and "X-Amz-Date=" in out

    def test_cloudfront_signed_query(self):
        out = redact_text("https://d1.cloudfront.net/v.mp4?Policy=AAA&Signature=BBB&Key-Pair-Id=CCC")
        for secret in ("AAA", "BBB", "CCC"):
            assert secret not in out
        assert "Policy=" in out and "Signature=" in out and "Key-Pair-Id=" in out

    def test_plain_fragment_and_surrounding_text_stay_readable(self):
        out = redact_text("ERROR: unable to download https://cdn.example/hls/master.m3u8?token=ABC#part1 HTTP Error 403")
        assert out == f"ERROR: unable to download https://cdn.example/hls/master.m3u8?token={REDACTED}#part1 HTTP Error 403"

    def test_query_like_fragment_values_redacted(self):
        out = redact_text("https://host.example/cb#access_token=SECRET&expires_in=3600")
        assert out == f"https://host.example/cb#access_token={REDACTED}&expires_in={REDACTED}"

    def test_multiline_ytdlp_message_keeps_status_and_location(self):
        msg = "ERROR: unable to download\nhttps://cdn.example/hls/master.m3u8?token=ABC&expires=123\nHTTP Error 403"
        out = redact_text(msg)
        assert "ABC" not in out and "=123" not in out
        assert "cdn.example/hls/master.m3u8?token=" in out and "&expires=" in out
        assert "HTTP Error 403" in out

    def test_trailing_prose_punctuation_kept_outside_value(self):
        assert redact_text("(see https://h.example/x?a=1).") == f"(see https://h.example/x?a={REDACTED})."

    def test_quoted_url_in_exception_repr(self):
        assert redact_text("failed GET 'https://cdn/x?token=S'") == f"failed GET 'https://cdn/x?token={REDACTED}'"

    def test_url_userinfo_redacted(self):
        out = redact_text("https://user:hunter2@host.example/path")
        assert out == f"https://{REDACTED}@host.example/path"

    @pytest.mark.parametrize(
        "text",
        [
            "https://cdn.example/hls/master.m3u8?token=ABC&expires=123&quality=1080",
            "master.m3u8?token=SECRET&x=1 then (https://h/x?a=1). <https://h/y?b=2>",
            "https://u:p@h.example/a?b=1#c=2",
            "https://h/p?a=1&&flag&c=",
        ],
    )
    def test_idempotent(self, text):
        once = redact_text(text)
        assert redact_text(once) == once

    @pytest.mark.parametrize(
        "text",
        ["", "?", "??==&&", "=&?#", "https://", "?=&=", "a?b=c?d=e#f=g#h", "\x00\xff?\ud800=1", "<redacted>?<redacted>"],
    )
    def test_malformed_input_never_raises(self, text):
        assert isinstance(redact_text(text), str)

    def test_non_string_input_never_raises(self):
        class Boom:
            def __str__(self):
                raise RuntimeError("nope")

        assert redact_text(None) == "None"
        assert redact_text(ValueError("x https://h/a?t=S")) == f"x https://h/a?t={REDACTED}"
        assert redact_text(Boom()) == REDACTED

    @pytest.mark.parametrize(
        "text",
        [
            r"C:\Users\Test\Videos\Show - ep4.mp4",
            r"\\?\C:\Users\Test\Videos\a=b\Show.mp4",
            "Shingeki no Kyojin: The Final Season - Kanketsu-hen",
            "Overwrite existing file? [y/N]",
            "Pobieranie nie powiodło się.",
        ],
    )
    def test_text_without_url_query_unchanged(self, text):
        assert redact_text(text) == text


@pytest.mark.unit
class TestRedactHeaders:
    @pytest.mark.parametrize(
        "name",
        ["Authorization", "proxy-authorization", "COOKIE", "Set-Cookie", "X-Api-Key", "api-key", "X-Auth-Token"],
    )
    def test_sensitive_header_fully_redacted(self, name):
        assert redact_header_value(name, "Bearer abc https://h/x") == REDACTED

    def test_other_header_gets_query_redaction(self):
        out = redact_header_value("Referer", "https://host/e?id=123&token=ABC")
        assert out == f"https://host/e?id={REDACTED}&token={REDACTED}"

    def test_plain_header_value_unchanged(self):
        assert redact_header_value("User-Agent", "Mozilla/5.0 (Windows NT 10.0)") == "Mozilla/5.0 (Windows NT 10.0)"

    def test_redact_headers_returns_new_dict(self):
        headers = {"Cookie": "s=1", "Referer": "https://h/e?t=2"}
        out = redact_headers(headers)
        assert out == {"Cookie": REDACTED, "Referer": f"https://h/e?t={REDACTED}"}
        assert headers == {"Cookie": "s=1", "Referer": "https://h/e?t=2"}
