from repostbot.config import load_settings
from repostbot.database import RepostDatabase


def test_runtime_database_overrides_env(monkeypatch, tmp_path):
    path = tmp_path / "db.sqlite3"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("MIN_NET_SCORE", "40")

    db = RepostDatabase(str(path))
    db.set_runtime_setting("DRY_RUN", "false")
    db.set_runtime_setting("MIN_NET_SCORE", "77")
    db.close()

    settings = load_settings()
    assert settings.dry_run is False
    assert settings.min_net_score == 77
