"""A mutation is stopped by a cancel or a close only before its write; after it the client gets the write's outcome."""

from unittest.mock import patch

from alt_ani_bridge.methods import common

from .conftest import EPISODE_URL, SERIES, ep


def _runtime(v2):
    (runtime,) = v2.bridge._runtimes.values()
    return runtime


class TestBeforeTheWrite:
    def test_cancel_accepted_before_the_write_changes_nothing(self, v2):
        real_key = common.key

        def key_then_cancel(r, *args, **kwargs):
            assert v2.bridge.cancel("mark-1")
            return real_key(r, *args, **kwargs)

        with patch.object(common, "key", key_then_cancel):
            error = v2.error("watch_set_watched", series_id="7", number=1, watched=True, expected_revision=0, op_id="mark-1")
        assert (error["code"], error["kind"]) == ("cancelled", "cancelled")
        assert v2.data("watch_get", series_id="7", numbers=[1])["episodes"][0]["revision"] == 0

    def test_cancel_before_the_call_changes_nothing(self, v2):
        assert v2.bridge.cancel("mark-0") is False
        error = v2.error("watch_set_watched", series_id="7", number=1, watched=True, expected_revision=0, op_id="mark-0")
        assert (error["code"], error["kind"]) == ("cancelled", "cancelled")
        assert v2.data("watch_get", series_id="7", numbers=[1])["episodes"][0]["revision"] == 0


class TestAfterTheWrite:
    def test_cancel_after_the_write_reports_the_write(self, v2):
        watch = _runtime(v2).state.watch
        real_set = watch.set_watched

        def set_then_cancel(*args, **kwargs):
            result = real_set(*args, **kwargs)
            assert v2.bridge.cancel("mark-2") is True
            return result

        v2.data("playback_begin", series=SERIES, episode=ep(1))
        (before,) = v2.data("watch_get", series_id="7", numbers=[1])["episodes"]
        with patch.object(watch, "set_watched", set_then_cancel):
            reply = v2.call(
                "watch_set_watched", series_id="7", number=1, watched=True, expected_revision=before["revision"], op_id="mark-2"
            )
        assert reply["ok"], reply
        (after,) = v2.data("watch_get", series_id="7", numbers=[1])["episodes"]
        assert after["watched"] is True
        assert after["revision"] == reply["data"]["watch"]["revision"] > before["revision"]

    def test_closing_the_session_after_the_write_reports_the_choice(self, v2):
        plan = v2.plan(episodes=[ep(1, EPISODE_URL)], selection={"mode": "episodes", "numbers": [1]})["plan"]
        (job,) = v2.data("queue_enqueue", plan=plan)["added"]
        listed = v2.data("queue_job_sources", job_id=job["job_id"])
        queue = _runtime(v2).state.queue
        real_choose = queue.choose_source

        def choose_then_close(*args, **kwargs):
            result = real_choose(*args, **kwargs)
            assert v2.data("close_session", session_id=listed["session_id"])["cancelled_operations"] == 1
            return result

        with patch.object(queue, "choose_source", choose_then_close):
            reply = v2.call(
                "queue_choose_source",
                job_id=job["job_id"],
                session_id=listed["session_id"],
                online_id=listed["sources"][0]["online_id"],
                expected_revision=job["revision"],
            )
        assert reply["ok"], reply
        assert reply["data"]["job"]["revision"] > job["revision"]
        assert v2.job(job["job_id"])["selection_mode"] == "manual"
