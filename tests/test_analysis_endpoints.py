"""HTTP-level tests for the GET /v1/analysis/* endpoints — auth, shape, and
that they reflect data ingested through the normal POST endpoints."""

from tests.conftest import SESSION_MD, auth_headers, post_session


def test_cardio_trend_requires_auth(app_client) -> None:
    response = app_client.get("/v1/analysis/cardio-trend")
    assert response.status_code == 401


def test_cardio_trend_empty_db_returns_gray_semaphores(app_client) -> None:
    response = app_client.get("/v1/analysis/cardio-trend", headers=auth_headers())
    assert response.status_code == 200
    body = response.json()
    assert body["metrics"]["restingHr"]["semaphore"] == "GRAY"
    assert body["alerts"]  # "Only 0 session(s)..." always present with no data


def test_cardio_trend_reflects_ingested_session(app_client) -> None:
    post_session(app_client)  # SESSION_MD: restingHr=58, dated 2025-04-15
    response = app_client.get(
        "/v1/analysis/cardio-trend", params={"today": "2025-04-20"}, headers=auth_headers()
    )
    assert response.status_code == 200
    body = response.json()
    assert body["metrics"]["restingHr"]["current"] == 58.0


def test_acwr_trend_requires_auth(app_client) -> None:
    response = app_client.get("/v1/analysis/acwr-trend")
    assert response.status_code == 401


def test_acwr_trend_empty_db_returns_gray_semaphore(app_client) -> None:
    response = app_client.get("/v1/analysis/acwr-trend", headers=auth_headers())
    assert response.status_code == 200
    body = response.json()
    assert body["semaphore"] == "GRAY"
    assert body["acwr"] is None


def test_acwr_trend_reflects_ingested_session(app_client) -> None:
    post_session(app_client)  # SESSION_MD: totalTonnage=12450.0, dated 2025-04-15
    response = app_client.get(
        "/v1/analysis/acwr-trend", params={"today": "2025-04-20"}, headers=auth_headers()
    )
    assert response.status_code == 200
    body = response.json()
    assert body["acute_load"] == 12450.0


def test_session_load_trend_requires_auth(app_client) -> None:
    response = app_client.get("/v1/analysis/session-load-trend")
    assert response.status_code == 401


def test_session_load_trend_empty_db_returns_empty_list(app_client) -> None:
    response = app_client.get("/v1/analysis/session-load-trend", headers=auth_headers())
    assert response.status_code == 200
    assert response.json() == []


def test_session_load_trend_excludes_session_without_rpe(app_client) -> None:
    post_session(app_client)  # SESSION_MD has no sessionRpe field
    response = app_client.get(
        "/v1/analysis/session-load-trend", params={"today": "2025-04-20"}, headers=auth_headers()
    )
    assert response.status_code == 200
    assert response.json() == []


def test_session_load_trend_reflects_ingested_rpe(app_client) -> None:
    md = SESSION_MD.replace("hrr60s: 28\n", "hrr60s: 28\nsessionRpe: 7\nsessionLoad: 315.0\n")
    post_session(app_client, md_bytes=md.encode())
    response = app_client.get(
        "/v1/analysis/session-load-trend", params={"today": "2025-04-20"}, headers=auth_headers()
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["session_rpe"] == 7
    assert body[0]["session_load"] == 315.0


def test_resting_hr_trend_requires_auth(app_client) -> None:
    response = app_client.get("/v1/analysis/resting-hr-trend")
    assert response.status_code == 401


def test_resting_hr_trend_returns_bucket_list(app_client) -> None:
    response = app_client.get("/v1/analysis/resting-hr-trend", headers=auth_headers())
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_vo2max_trend_returns_bucket_list(app_client) -> None:
    response = app_client.get("/v1/analysis/vo2max-trend", headers=auth_headers())
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_scale_trend_returns_all_four_metrics(app_client) -> None:
    response = app_client.get("/v1/analysis/scale-trend", headers=auth_headers())
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"weightKg", "bmi", "bodyFatPercent", "leanMassPercent"}


def test_exercise_pr_requires_auth(app_client) -> None:
    response = app_client.get("/v1/analysis/exercises/ex-a1b2c3d4/pr")
    assert response.status_code == 401


def test_exercise_pr_no_data_returns_null_prs(app_client) -> None:
    response = app_client.get("/v1/analysis/exercises/ex-nonexistent/pr", headers=auth_headers())
    assert response.status_code == 200
    body = response.json()
    assert body["allTimeTonnagePr"] is None
    assert body["recentHeaviestSetPr"] is None


def test_exercise_pr_reflects_ingested_session(app_client) -> None:
    # SESSION_MD's ex-a1b2c3d4 has excludeFromTonnage: true, so it must
    # never surface as an all-time tonnage PR — confirms the endpoint
    # actually applies that filter, not just that it returns *something*.
    post_session(app_client)
    response = app_client.get("/v1/analysis/exercises/ex-a1b2c3d4/pr", headers=auth_headers())
    assert response.status_code == 200
    body = response.json()
    assert body["allTimeTonnagePr"] is None
