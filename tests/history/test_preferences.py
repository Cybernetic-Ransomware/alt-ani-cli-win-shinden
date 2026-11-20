import pytest

from alt_ani_core.errors import StateConflictError
from alt_ani_core.state import DownloadNetwork, QualityCriterion

from .state_support import open_state


@pytest.fixture
def prefs(tmp_path):
    return open_state(tmp_path / "history.db").preferences


@pytest.mark.unit
class TestPreferences:
    def test_round_trip_of_every_field(self, prefs):
        prefs.update(
            audio="jp",
            subs="none",
            quality=QualityCriterion(720),
            download_network=DownloadNetwork.ANY,
            auto_alternatives=True,
            max_active_downloads=2,
        )
        got = prefs.get()
        assert (got.audio, got.subs, got.quality, got.download_network) == ("jp", "none", QualityCriterion(720), "any")
        assert (got.auto_alternatives, got.max_active_downloads, got.revision) == (True, 2, 1)

    def test_stale_revision_is_a_conflict(self, prefs):
        first = prefs.get()
        prefs.update(audio="pl", expected_revision=first.revision)
        with pytest.raises(StateConflictError) as info:
            prefs.update(audio="jp", expected_revision=first.revision)
        assert info.value.code == "state.conflict"
        assert prefs.get().audio == "pl"

    @pytest.mark.parametrize("changes", [{"audio": "none"}, {"max_active_downloads": 0}, {"subs": ""}])
    def test_invalid_values_are_rejected_without_writing(self, prefs, changes):
        with pytest.raises(ValueError):
            prefs.update(**changes)
        assert prefs.get().revision == 0

    def test_revision_cannot_be_set_directly(self, prefs):
        with pytest.raises(TypeError):
            prefs.update(revision=5)
