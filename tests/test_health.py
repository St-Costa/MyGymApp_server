def test_health_returns_200(app_client) -> None:
    response = app_client.get("/health")
    assert response.status_code == 200


def test_health_requires_no_auth(app_client) -> None:
    # No Authorization header at all — /health is explicitly exempt.
    response = app_client.get("/health", headers={})
    assert response.status_code == 200
