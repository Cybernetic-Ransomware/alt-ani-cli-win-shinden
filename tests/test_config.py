import pytest

from alt_ani_core.config import XHR_BACKEND_NAMES, xhr_backend_name


@pytest.mark.unit
class TestXhrBackendName:
    def test_names_follow_the_config_literal(self):
        assert XHR_BACKEND_NAMES == ("new", "api4")

    @pytest.mark.parametrize(
        ("value", "expected"), [("new", "new"), ("api4", "api4"), (" API4 ", "api4"), ("New\n", "new"), ("\tnew", "new")]
    )
    def test_known_value_is_normalised(self, value, expected):
        assert xhr_backend_name(value) == expected

    @pytest.mark.parametrize("value", ["", " ", "auto", "new_xhr", "legacy", "api 4"])
    def test_unknown_value_gives_none(self, value):
        assert xhr_backend_name(value) is None
