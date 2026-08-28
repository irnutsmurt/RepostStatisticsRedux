from io import BytesIO

from PIL import Image

from repostbot.database import RepostDatabase
from repostbot.hashing import signature_from_bytes
from repostbot.models import GalleryPost


def image_bytes() -> bytes:
    img = Image.new("RGB", (64, 64), "white")
    for x in range(32):
        for y in range(64):
            img.putpixel((x, y), (0, 0, 0))
    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def post(post_id: str, ts: int) -> GalleryPost:
    return GalleryPost(post_id, "hello", ts, 1, "u", "https://i.imgur.com/x.png", False, 64, 64, 100, 0)


def test_duplicate_lookup(tmp_path):
    db = RepostDatabase(str(tmp_path / "db.sqlite3"))
    sig = signature_from_bytes(image_bytes())
    db.insert_post(post("old", 100), sig)
    found = db.find_duplicates(
        post("new", 200), sig,
        ahash_max_distance=3,
        dhash_h_max_distance=10,
        dhash_v_max_distance=10,
        bitcount_padding=22,
    )
    assert [x.id for x in found] == ["old"]
    assert found[0].exact_match is True
    db.close()


def test_runtime_settings_and_api_summary(tmp_path):
    db = RepostDatabase(str(tmp_path / "db.sqlite3"))
    db.set_runtime_setting("DRY_RUN", "false")
    assert db.get_runtime_settings()["DRY_RUN"] == "false"

    db.record_api_event({
        "method": "GET", "path": "/gallery/top/viral/all/12", "status": 503,
        "client_limit": 12500, "client_remaining": 12000,
        "user_limit": 500, "user_remaining": 490, "user_reset": 123456,
    })
    summary = db.api_summary()
    assert summary["calls_24h"] == 1
    assert summary["errors_503_24h"] == 1
    assert summary["latest"]["client_remaining"] == 12000
    db.close()


def test_web_job_lifecycle(tmp_path):
    db = RepostDatabase(str(tmp_path / "db.sqlite3"))
    job_id = db.create_web_job("backfill", {"pages": 10}, str(tmp_path / "job.log"))
    db.update_web_job(job_id, status="running", pid=1234, progress_json={"current_page": 3})
    row = db.get_web_job(job_id)
    assert row["status"] == "running"
    assert row["pid"] == 1234
    assert '"current_page": 3' in row["progress_json"]
    db.close()
