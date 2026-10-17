import pytest


@pytest.fixture(autouse=True)
def no_backoff_sleep(monkeypatch):
    """Retry backoff must not make mocked-failure tests slow."""
    import agents.models as models
    monkeypatch.setattr(models.time, "sleep", lambda s: None)
