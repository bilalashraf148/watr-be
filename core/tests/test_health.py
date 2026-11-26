from rest_framework.test import APIClient


def test_health_is_public_and_ok():
    response = APIClient().get("/api/v1/health/")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_rejects_writes():
    response = APIClient().post("/api/v1/health/")

    assert response.status_code == 405
