import os
import tempfile

# 必须在导入 app 之前指定独立数据库文件
_fd, _db_file = tempfile.mkstemp(suffix=".db", prefix="recs_eval_conf_")
os.close(_fd)
os.environ["DB_PATH"] = _db_file

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """每个用例使用独立的临时 SQLite 库。"""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db()
    with TestClient(app) as c:
        yield c


SAMPLE_CSV = """session_id,event_no,item_id,position,clicked,propensity,new_rank
s1,e1,A,1,1,0.5,1
s1,e2,B,2,0,0.25,2
s1,e3,C,3,1,0.5,
s2,e4,A,1,1,0.5,1
s2,e5,D,2,0,0.5,2
s3,e6,E,1,0,0.5,
s3,,F,,,,1
s4,e7,G,1,1,0.9,
"""


def import_csv(client, text, mode="append"):
    return client.post(
        "/api/import",
        files={"file": ("log.csv", text.encode("utf-8"), "text/csv")},
        data={"mode": mode},
    )
