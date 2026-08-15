"""app.scheduler: weekly-report screenshot day/hour config — persistence,
validation, and the GUI form that drives it. Doesn't test that APScheduler
actually fires a job at the right wall-clock time (that's APScheduler's own
contract, not ours to verify); tests what this module owns: reading/writing
the setting, validating it, and keeping the running job in sync."""

import pytest

from app import scheduler


def test_get_schedule_defaults_to_sunday_6am_when_unset(app_client) -> None:
    day, hour = scheduler.get_schedule()
    assert (day, hour) == ("sun", 6)


def test_set_schedule_persists_and_is_read_back(app_client) -> None:
    scheduler.set_schedule("wed", 18)
    assert scheduler.get_schedule() == ("wed", 18)


@pytest.mark.parametrize("day", ["", "sunday", "Mon", "xyz"])
def test_set_schedule_rejects_invalid_day(app_client, day) -> None:
    with pytest.raises(ValueError):
        scheduler.set_schedule(day, 6)
    # Rejected write must not partially persist.
    assert scheduler.get_schedule() == ("sun", 6)


@pytest.mark.parametrize("hour", [-1, 24, 100])
def test_set_schedule_rejects_invalid_hour(app_client, hour) -> None:
    with pytest.raises(ValueError):
        scheduler.set_schedule("mon", hour)
    assert scheduler.get_schedule() == ("sun", 6)


def test_weekly_report_page_shows_current_schedule(app_client) -> None:
    response = app_client.get("/weekly-report")
    assert response.status_code == 200
    assert 'value="sun" selected' in response.text
    assert 'value="6" selected' in response.text


def test_weekly_report_schedule_form_saves_and_redirects(app_client) -> None:
    response = app_client.post("/weekly-report/schedule", data={"day": "fri", "hour": "20"})
    assert response.status_code == 200  # TestClient follows the 303 redirect
    assert response.url.path == "/weekly-report"
    assert 'value="fri" selected' in response.text
    assert 'value="20" selected' in response.text
    assert scheduler.get_schedule() == ("fri", 20)


def test_weekly_report_schedule_form_rejects_invalid_input(app_client) -> None:
    response = app_client.post("/weekly-report/schedule", data={"day": "someday", "hour": "6"})
    assert response.status_code == 400
    assert scheduler.get_schedule() == ("sun", 6)
