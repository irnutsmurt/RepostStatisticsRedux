import pytest

flask = pytest.importorskip("flask")

from repostbot.database import RepostDatabase
from repostbot.webapp import create_app


def test_dashboard_and_blocklist(monkeypatch, tmp_path):
    db_path = tmp_path / "web.sqlite3"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "bot.log"))
    monkeypatch.setenv("WEB_JOB_LOG_DIR", str(tmp_path / "jobs"))

    app = create_app()
    app.testing = True
    client = app.test_client()

    response = client.get("/")
    assert response.status_code == 200
    assert b"RepostStatisticsRedux" in response.data

    response = client.post(
        "/blocklist",
        data={
            "csrf_token": app.config["CSRF_TOKEN"],
            "kind": "username",
            "value": "Alice",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200

    db = RepostDatabase(str(db_path))
    assert db.is_blocked(None, "alice") is True
    db.close()


def test_pid_matches_web_job_rejects_unrelated_process(monkeypatch, tmp_path):
    import subprocess
    import sys
    from repostbot.webapp import _pid_matches_web_job

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert _pid_matches_web_job(proc.pid, 77, tmp_path) is False
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_refresh_marks_reused_or_unrelated_pid_exited(monkeypatch, tmp_path):
    import subprocess
    import sys
    from repostbot.webapp import JobManager

    db_path = tmp_path / "web.sqlite3"
    db = RepostDatabase(str(db_path))
    job_id = db.create_web_job("backfill", {"pages": 10}, str(tmp_path / "job.log"))
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        db.update_web_job(job_id, status="running", pid=proc.pid)
        db.close()

        manager = JobManager(tmp_path, str(db_path), str(tmp_path / "jobs"))
        manager.refresh()

        db = RepostDatabase(str(db_path))
        row = db.get_web_job(job_id)
        assert row["status"] == "exited"
        assert row["finished_at"] is not None
        db.close()
    finally:
        proc.terminate()
        proc.wait(timeout=5)
