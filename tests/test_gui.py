"""Server-rendered dashboard routes — no auth (see app/gui.py's module
docstring for why), so these tests just check the pages render successfully
and reflect ingested data, not auth behavior (covered elsewhere)."""

from tests.conftest import post_session


def test_dashboard_renders_with_no_data(app_client) -> None:
    response = app_client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Cardio trend" in response.text


def test_dashboard_reflects_ingested_session(app_client) -> None:
    post_session(app_client)  # SESSION_MD: restingHr=58, dated 2025-04-15
    response = app_client.get("/", params={"today": "2025-04-20"})
    assert response.status_code == 200
    assert "58.0" in response.text


def test_exercises_list_empty(app_client) -> None:
    response = app_client.get("/exercises")
    assert response.status_code == 200
    assert "No exercises synced yet" in response.text


def test_exercises_list_reflects_ingested_session(app_client) -> None:
    post_session(app_client)
    response = app_client.get("/exercises")
    assert response.status_code == 200
    assert "Bench Press" in response.text
    assert 'href="/exercises/ex-a1b2c3d4"' in response.text


def test_exercise_detail_no_data(app_client) -> None:
    response = app_client.get("/exercises/ex-nonexistent")
    assert response.status_code == 200
    assert "No qualifying set found" in response.text


def test_exercise_detail_reflects_ingested_session(app_client) -> None:
    # SESSION_MD's ex-a1b2c3d4 has excludeFromTonnage: true -> no all-time PR,
    # but recent_heaviest_set_pr doesn't filter on that, so it should show up.
    post_session(app_client)
    response = app_client.get("/exercises/ex-a1b2c3d4")
    assert response.status_code == 200
    assert "Bench Press" in response.text
    assert "No qualifying set found" in response.text  # excludeFromTonnage excludes it


def test_static_chartjs_is_served(app_client) -> None:
    response = app_client.get("/static/chart.umd.min.js")
    assert response.status_code == 200
    assert len(response.content) > 1000
