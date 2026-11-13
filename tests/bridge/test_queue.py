"""Queue through the contract: preview bound to commit, delayed repeats that change nothing, interaction without grants."""

import json

from alt_ani_core.services.offline.sources import SourceUnavailable

from .conftest import EPISODE_URL, SERIES, ep


class TestPreview:
    def test_selection_modes(self, v2):
        listing = [ep(n) for n in (1, 2, 3, 4)]
        by_numbers = v2.plan(episodes=listing, selection={"mode": "episodes", "numbers": [4, 2]})
        assert [e["number"] for e in by_numbers["plan"]["episodes"]] == [2, 4]
        by_range = v2.plan(episodes=listing, selection={"mode": "range", "range": "2-3"})
        assert [e["number"] for e in by_range["plan"]["episodes"]] == [2, 3]
        assert (
            v2.error("queue_preview", series=SERIES, episodes=listing, selection={"mode": "range", "range": "x"})["params"][
                "problem"
            ]
            == "unknown_format"
        )

    def test_unwatched_includes_gaps(self, v2):
        for number in (1, 3):
            v2.data("playback_begin", series=SERIES, episode=ep(number))
            v2.data("watch_set_watched", series_id="7", number=number, watched=True, expected_revision=1)
        plan = v2.plan(episodes=[ep(n) for n in (1, 2, 3, 4)], selection={"mode": "unwatched"})["plan"]
        assert [e["number"] for e in plan["episodes"]] == [2, 4]

    def test_skips_queued_and_downloaded(self, v2):
        v2.download(1)
        v2.enqueue((2,))
        preview = v2.plan((1, 2, 3))
        assert [e["number"] for e in preview["plan"]["episodes"]] == [3]
        assert {(s["number"], s["reason"]) for s in preview["skipped"]} == {(1, "already_downloaded"), (2, "already_queued")}

    def test_unknown_numbers_are_refused(self, v2):
        error = v2.error("queue_preview", series=SERIES, episodes=[ep(1)], selection={"mode": "episodes", "numbers": [9]})
        assert error["params"]["problem"] == "not_in_listing"


class TestPlanCommit:
    def test_enqueue_uses_the_plan_not_the_preferences(self, v2):
        plan = v2.plan((1,), criteria={"audio": "jp", "quality": "max:720"})["plan"]
        v2.data("preferences_update", changes={"audio": "pl", "quality": "best"}, expected_revision=0)
        (job,) = v2.data("queue_enqueue", plan=plan)["added"]
        assert job["criteria"] == {"audio": "jp", "subs": "any", "quality": "max:720"}

    def test_full_criteria_are_required(self, v2):
        plan = v2.plan((1,))["plan"]
        plan["criteria"].pop("subs")
        assert v2.error("queue_enqueue", plan=plan)["params"] == {"field": "plan.criteria.subs", "problem": "missing"}

    def test_delayed_repeat_after_cancel_adds_nothing(self, v2):
        plan = v2.plan((1, 2))["plan"]
        added = v2.data("queue_enqueue", plan=plan)["added"]
        v2.data("queue_cancel", job_id=added[0]["job_id"], expected_revision=added[0]["revision"])
        again = v2.data("queue_enqueue", plan=plan)
        assert again["added"] == []
        assert {s["reason"] for s in again["skipped"]} == {"changed_since_preview"}

    def test_client_may_drop_episodes_from_the_plan(self, v2):
        plan = v2.plan((1, 2))["plan"]
        plan["episodes"] = plan["episodes"][:1]
        assert [j["episode"]["number"] for j in v2.data("queue_enqueue", plan=plan)["added"]] == [1]


class TestJobActions:
    def test_delayed_pause_after_resume_changes_nothing(self, v2):
        (job,) = v2.enqueue()
        paused = v2.data("queue_pause", job_id=job["job_id"], expected_revision=job["revision"])["job"]
        v2.data("queue_resume", job_id=job["job_id"], expected_revision=paused["revision"])
        error = v2.error("queue_pause", job_id=job["job_id"], expected_revision=job["revision"])
        assert (error["code"], error["kind"]) == ("state.conflict", "conflict")
        assert v2.job(job["job_id"])["state"] == "queued"

    def test_delayed_retry_after_another_failure_changes_nothing(self, v2):
        (job,) = v2.enqueue()
        v2.sources.open_error = SourceUnavailable("gone", transient=False)
        v2.data("executor_start")
        failed = v2.wait_job(job["job_id"], "failed")
        v2.data("queue_retry", job_id=job["job_id"], expected_revision=failed["revision"])
        failed_again = v2.wait_job(job["job_id"], "failed")
        assert failed_again["revision"] != failed["revision"]
        error = v2.error("queue_retry", job_id=job["job_id"], expected_revision=failed["revision"])
        assert error["code"] == "state.conflict"
        assert v2.job(job["job_id"])["state"] == "failed"

    def test_delayed_cellular_consent_after_revocation(self, v2):
        (job,) = v2.enqueue()
        allowed = v2.data("queue_allow_cellular", job_id=job["job_id"], allowed=True, expected_revision=job["revision"])["job"]
        v2.data("queue_allow_cellular", job_id=job["job_id"], allowed=False, expected_revision=allowed["revision"])
        assert (
            v2.error("queue_allow_cellular", job_id=job["job_id"], allowed=True, expected_revision=job["revision"])["code"]
            == "state.conflict"
        )

    def test_revision_is_required(self, v2):
        (job,) = v2.enqueue()
        assert v2.error("queue_pause", job_id=job["job_id"])["params"] == {"field": "expected_revision", "problem": "missing"}

    def test_actions_are_listed(self, v2):
        (job,) = v2.enqueue()
        assert job["actions"] == ["allow_cellular", "cancel", "choose_source", "pause"]

    def test_reorder(self, v2):
        jobs = v2.enqueue((1, 2))
        snapshot = v2.data("queue_snapshot")
        ids = [j["job_id"] for j in reversed(jobs)]
        revision = v2.data("queue_reorder", job_ids=ids, expected_revision=snapshot["queue_revision"])["queue_revision"]
        assert [j["job_id"] for j in v2.data("queue_snapshot")["jobs"]] == ids
        assert v2.error("queue_reorder", job_ids=ids, expected_revision=revision - 1)["code"] == "state.conflict"


class TestInteraction:
    def _waiting(self, v2) -> dict:
        from alt_ani_core.services.offline.interaction import InteractionNeeded

        (job,) = v2.enqueue()
        v2.sources.open_error = InteractionNeeded("host", host="host.example", online_id="1")
        v2.data("executor_start")
        return v2.wait_job(job["job_id"], "waiting")

    def test_context_and_retry_without_a_grant(self, v2):
        job = self._waiting(v2)
        assert job["wait_reason"] == "interaction"
        assert job["interaction"] == {"scope": "host", "host": "host.example", "online_id": "1"}
        assert {"retry_interaction", "choose_source", "cancel"} <= set(job["actions"])
        v2.sources.open_error = None
        v2.transfer.release.set()
        v2.data("queue_retry_interaction", job_id=job["job_id"], expected_revision=job["revision"])
        assert v2.wait_job(job["job_id"], "completed")["state"] == "completed"
        assert v2.sources.last_grant is None

    def test_stale_answer_is_refused(self, v2):
        job = self._waiting(v2)
        v2.data("queue_cancel", job_id=job["job_id"], expected_revision=job["revision"])
        error = v2.error("queue_retry_interaction", job_id=job["job_id"], expected_revision=job["revision"])
        assert error["code"] == "state.conflict"


class TestSources:
    def test_job_sources_and_manual_choice(self, v2):
        plan = v2.plan(episodes=[ep(1, EPISODE_URL)], selection={"mode": "episodes", "numbers": [1]})["plan"]
        (job,) = v2.data("queue_enqueue", plan=plan)["added"]
        listed = v2.data("queue_job_sources", job_id=job["job_id"])
        dood_en = next(s for s in listed["sources"] if s["lang_subs"] == "en")
        assert listed["partial"] is None and dood_en["matches_criteria"] is True
        chosen = v2.data(
            "queue_choose_source",
            job_id=job["job_id"],
            session_id=listed["session_id"],
            online_id=dood_en["online_id"],
            expected_revision=job["revision"],
        )["job"]
        assert chosen["selection_mode"] == "manual" and chosen["source"]["online_id"] == dood_en["online_id"]

    def test_session_of_another_job_is_refused(self, v2):
        plan = v2.plan(numbers=(1, 2), episodes=[ep(1, EPISODE_URL), ep(2)])["plan"]
        first, second = v2.data("queue_enqueue", plan=plan)["added"]
        listed = v2.data("queue_job_sources", job_id=first["job_id"])
        error = v2.error(
            "queue_choose_source",
            job_id=second["job_id"],
            session_id=listed["session_id"],
            online_id=listed["sources"][0]["online_id"],
            expected_revision=second["revision"],
        )
        assert (error["code"], error["params"]["problem"]) == ("bad_request", "other_episode")
        assert v2.job(second["job_id"])["revision"] == second["revision"]

    def test_job_continues_after_a_runtime_restart(self, v2, tmp_path):
        from .conftest import make_v2

        (job,) = v2.enqueue()
        v2.data("executor_start")
        assert v2.transfer.started.acquire(timeout=5)
        v2.transfer.io_done.set()
        assert v2.data("runtime_close", timeout_ms=5000)["ended"]
        fresh = make_v2(tmp_path)
        fresh.create(push=False, storage={"kind": "fs", "root": str(tmp_path / "library")})
        # the shutdown gave the slot back unspent; the next executor picks the job up again
        assert fresh.job(job["job_id"])["state"] == "queued"
        fresh.transfer.release.set()
        fresh.data("executor_start")
        assert fresh.wait_job(job["job_id"], "completed")["state"] == "completed"
        json.loads(fresh.bridge.shutdown(5))
