"""单位置试投 API 端到端测试：开轮快照、领取策略、反馈登记、并发上限、替换日志。"""

import threading

from fastapi.testclient import TestClient

from app.main import app

from .conftest import SAMPLE_CSV, import_csv


def open_round(client, item_ids):
    return client.post("/api/trial/rounds", json={"item_ids": item_ids})


def test_candidate_pool_empty_before_import(client):
    assert client.get("/api/trial/candidates").json() == []
    assert client.get("/api/trial/rounds/active").json() == {"active": False}


def test_candidate_pool_dedup_and_sorted(client):
    assert import_csv(client, SAMPLE_CSV).status_code == 200
    pool = client.get("/api/trial/candidates").json()
    # A 在 s1/s2 两次出现，去重后只保留一条；按物品 ID 升序
    assert [c["item_id"] for c in pool] == ["A", "B", "D", "F"]
    assert pool[0]["n_sessions"] == 2


def test_open_round_validation(client):
    assert import_csv(client, SAMPLE_CSV).status_code == 200

    # 未知物品
    r = open_round(client, ["A", "ZZZ"])
    assert r.status_code == 400 and "ZZZ" in r.json()["detail"]

    # 重复物品
    r = open_round(client, ["A", "A"])
    assert r.status_code == 400 and "重复" in r.json()["detail"]

    # 超过 20 个 → 422 校验错误
    r = open_round(client, [f"x{i}" for i in range(21)])
    assert r.status_code == 422

    # 空列表 → 422
    assert open_round(client, []).status_code == 422


def test_full_claim_and_feedback_flow(client):
    assert import_csv(client, SAMPLE_CSV).status_code == 200
    r = open_round(client, ["F", "A", "B", "D"])  # 故意乱序
    assert r.status_code == 201
    st = r.json()
    assert st["status"] == "active"
    assert st["candidate_count"] == 4
    # 候选快照按物品 ID 升序保存
    assert [c["item_id"] for c in st["candidates"]] == ["A", "B", "D", "F"]
    assert client.get("/api/status").json()["active_rounds"] == 1

    # 第二轮进行中不能再开
    assert open_round(client, ["A"]).status_code == 409

    # 新鲜阶段：全部无反馈，按物品 ID 升序依次领取
    e1 = client.post("/api/trial/claim").json()
    e2 = client.post("/api/trial/claim").json()
    e3 = client.post("/api/trial/claim").json()
    e4 = client.post("/api/trial/claim").json()
    assert [e["item_id"] for e in (e1, e2, e3, e4)] == ["A", "B", "D", "F"]
    assert [e["exposure_no"] for e in (e1, e2, e3, e4)] == [1, 2, 3, 4]

    # 每个物品各有一条待反馈 → 再领被拒，提示先登记
    r = client.post("/api/trial/claim")
    assert r.status_code == 409 and "先登记" in r.json()["detail"]

    # 登记 A 点击；A 解除待反馈后可再次领取（其余仍在等待）
    f1 = client.post("/api/trial/exposures/1/feedback", json={"clicked": 1})
    assert f1.status_code == 200 and f1.json()["feedback"] == 1
    e5 = client.post("/api/trial/claim").json()
    assert e5["item_id"] == "A" and e5["exposure_no"] == 5

    # 重复登记以首次结果为准：再次提交不同结果 → 409 且不改写
    dup = client.post("/api/trial/exposures/1/feedback", json={"clicked": 0})
    assert dup.status_code == 409 and "首次结果" in dup.json()["detail"]
    st = client.get("/api/trial/rounds/active").json()
    exp1 = next(e for e in st["exposures"] if e["exposure_no"] == 1)
    assert exp1["feedback"] == 1  # 首次点击保持不变

    # 不存在的曝光编号 → 404；非法 clicked → 422
    assert client.post("/api/trial/exposures/999/feedback", json={"clicked": 1}).status_code == 404
    assert client.post("/api/trial/exposures/2/feedback", json={"clicked": 9}).status_code == 422

    # 登记其余反馈：B=0, D=1, F=0, A(第二次)=0
    for no, clk in [(2, 0), (3, 1), (4, 0), (5, 0)]:
        assert client.post(f"/api/trial/exposures/{no}/feedback",
                           json={"clicked": clk}).status_code == 200

    # n_total=5：D 的 UCB1（1+sqrt(2ln5)≈2.79）最高 → 领取 D
    e6 = client.post("/api/trial/claim").json()
    assert e6["item_id"] == "D" and e6["exposure_no"] == 6

    # 刷新页面等价的 GET：待反馈与统计仍在
    st = client.get("/api/trial/rounds/active").json()
    assert st["active"] is True
    assert st["n_feedback"] == 5 and st["n_pending"] == 1
    assert st["stats"]["A"]["pulls"] == 2 and st["stats"]["A"]["clicks"] == 1
    assert st["stats"]["A"]["pending"] == 0 and st["stats"]["A"]["ucb1"] is not None
    assert st["stats"]["D"]["pending"] == 1
    pending = [e for e in st["exposures"] if e["feedback"] is None]
    assert len(pending) == 1 and pending[0]["exposure_no"] == 6


def test_ucb1_tie_breaks_by_item_id(client):
    assert import_csv(client, SAMPLE_CSV).status_code == 200
    open_round(client, ["B", "F"])
    # 各领一次并都登记未点击，统计完全相同 → 并列按 ID 升序选 B
    first = client.post("/api/trial/claim").json()
    second = client.post("/api/trial/claim").json()
    assert [first["item_id"], second["item_id"]] == ["B", "F"]
    client.post("/api/trial/exposures/1/feedback", json={"clicked": 0})
    client.post("/api/trial/exposures/2/feedback", json={"clicked": 0})
    third = client.post("/api/trial/claim").json()
    assert third["item_id"] == "B"


def test_close_round_blocks_claim_and_feedback(client):
    assert import_csv(client, SAMPLE_CSV).status_code == 200
    open_round(client, ["A", "B"])
    client.post("/api/trial/claim")  # #1 A，待反馈
    st = client.post("/api/trial/rounds/close").json()
    assert st["status"] == "closed" and st["closed_at"] is not None
    assert client.post("/api/trial/claim").status_code == 400
    assert client.post("/api/trial/exposures/1/feedback",
                       json={"clicked": 1}).status_code == 400
    # 关闭后可以开新一轮
    r = open_round(client, ["A"])
    assert r.status_code == 201


def test_round_snapshot_survives_log_replace(client):
    assert import_csv(client, SAMPLE_CSV).status_code == 200
    open_round(client, ["A", "B", "D", "F"])
    client.post("/api/trial/claim")  # #1 A

    # 替换日志：候选池变为新日志的 A、B，但已开轮次继续使用开轮时快照
    new_csv = (
        "session_id,event_no,item_id,position,clicked,propensity,new_rank\n"
        "n1,x1,A,1,0,0.5,1\n"
        "n1,x2,B,2,0,0.5,2\n"
        "n1,x3,C,3,1,0.5,\n"
    )
    assert import_csv(client, new_csv, mode="replace").status_code == 200
    pool = [c["item_id"] for c in client.get("/api/trial/candidates").json()]
    assert pool == ["A", "B"]

    st = client.get("/api/trial/rounds/active").json()
    assert [c["item_id"] for c in st["candidates"]] == ["A", "B", "D", "F"]
    # 快照里的 F 仍可参与领取（先登记 #1，再依次领 B、D、F）
    client.post("/api/trial/exposures/1/feedback", json={"clicked": 1})
    picked = []
    for _ in range(3):
        picked.append(client.post("/api/trial/claim").json()["item_id"])
    assert picked == ["B", "D", "F"]


def test_concurrent_claims_respect_one_pending_per_item(client):
    csv_text = (
        "session_id,event_no,item_id,position,clicked,propensity,new_rank\n"
        + "".join(f"s1,e{i},{chr(65 + i)},{i + 1},1,0.5,{i + 1}\n" for i in range(5))
    )
    assert import_csv(client, csv_text).status_code == 200
    open_round(client, ["A", "B", "C", "D", "E"])

    results = []
    errors = []

    def worker():
        c = TestClient(app)  # 每个线程独立 client，共享同一 SQLite 文件
        r = c.post("/api/trial/claim")
        if r.status_code == 200:
            results.append(r.json())
        else:
            errors.append(r.status_code)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 恰好 5 条曝光，每个物品一条；其余 15 个请求被 409 拒绝
    assert len(results) == 5
    assert sorted(e["item_id"] for e in results) == ["A", "B", "C", "D", "E"]
    assert len(errors) == 15 and set(errors) == {409}
    st = client.get("/api/trial/rounds/active").json()
    assert st["n_pending"] == 5 and len(st["exposures"]) == 5


def test_concurrent_feedback_first_result_wins(client):
    assert import_csv(client, SAMPLE_CSV).status_code == 200
    open_round(client, ["A", "B"])
    client.post("/api/trial/claim")  # #1 A

    outcomes = []

    def worker(clicked):
        c = TestClient(app)
        r = c.post("/api/trial/exposures/1/feedback", json={"clicked": clicked})
        outcomes.append((r.status_code, clicked))

    t1 = threading.Thread(target=worker, args=(1,))
    t2 = threading.Thread(target=worker, args=(0,))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert sorted(code for code, _ in outcomes) == [200, 409]
    winner = next(clicked for code, clicked in outcomes if code == 200)
    st = client.get("/api/trial/rounds/active").json()
    exp1 = next(e for e in st["exposures"] if e["exposure_no"] == 1)
    assert exp1["feedback"] == winner  # 先到的首次结果保留，后到的不改写
