"""API 端到端测试：导入去重、评估、排除规则、修改后重新评估。"""

import pytest

from .conftest import SAMPLE_CSV, import_csv


def test_import_and_dedup_same_event_no(client):
    r = import_csv(client, SAMPLE_CSV)
    assert r.status_code == 200
    body = r.json()
    assert body["exposure_rows"] == 7
    assert body["inserted"] == 7
    assert body["duplicates_skipped"] == 0

    # 追加同一文件：相同事件号只处理一次，全部跳过
    r2 = import_csv(client, SAMPLE_CSV)
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["inserted"] == 0
    assert body2["duplicates_skipped"] == 7

    status = client.get("/api/status").json()
    assert status["events"] == 7
    assert status["sessions"] == 4


def test_import_validation_error(client):
    bad = "session_id,event_no,item_id,position,clicked,propensity,new_rank\ns1,e1,A,1,1,0,1\n"
    r = import_csv(client, bad)
    assert r.status_code == 400
    assert "propensity" in r.json()["detail"]


def test_import_missing_column(client):
    bad = "session_id,event_no,item_id\ns1,e1,A\n"
    r = import_csv(client, bad)
    assert r.status_code == 400
    assert "缺少必需列" in r.json()["detail"]


def test_evaluate_flow_and_exclusions(client):
    assert import_csv(client, SAMPLE_CSV).status_code == 200
    r = client.post("/api/evaluate", json={"cap": 10.0, "min_ess": 1.0})
    assert r.status_code == 200
    res = r.json()

    # s1: IPS=(2*1+4*0)/3=2/3, SNIPS=2/6=1/3, baseline=2/3
    # s2: IPS=(2*1+2*0)/2=1.0, SNIPS=2/4=0.5, baseline=0.5
    # s3: 候选 F 未覆盖 → 排除；s4: 无候选 → 样本不足排除
    overall = res["overall"]
    assert overall["included_sessions"] == 2
    assert overall["excluded_sessions"] == 2
    assert overall["ips_ctr"] == pytest.approx((2 / 3 + 1.0) / 2, abs=1e-6)
    assert overall["snips_ctr"] == pytest.approx((1 / 3 + 0.5) / 2, abs=1e-6)
    assert overall["baseline_ctr"] == pytest.approx((2 / 3 + 0.5) / 2, abs=1e-6)
    assert res["verdict"] == "pass"

    # 未覆盖候选单列，且不计入结论
    assert res["uncovered_candidates"] == [{"session_id": "s3", "item_id": "F", "new_rank": 1}]
    # 样本不足单列
    assert len(res["insufficient_sessions"]) == 1
    assert res["insufficient_sessions"][0]["session_id"] == "s4"

    status_by_session = {s["session_id"]: s["status"] for s in res["per_session"]}
    assert status_by_session == {
        "s1": "included",
        "s2": "included",
        "s3": "excluded_uncovered",
        "s4": "excluded_insufficient",
    }


def test_uncovered_session_not_counted_in_conclusion(client):
    # 只有一个会话且存在未覆盖候选 → 无有效会话，无法得出结论
    csv_text = (
        "session_id,event_no,item_id,position,clicked,propensity,new_rank\n"
        "s1,e1,A,1,1,0.5,1\n"
        "s1,,B,,,,2\n"
    )
    assert import_csv(client, csv_text).status_code == 200
    res = client.post("/api/evaluate", json={"cap": 10.0, "min_ess": 1.0}).json()
    assert res["overall"]["included_sessions"] == 0
    assert res["overall"]["ips_ctr"] is None
    assert res["verdict"] == "no_data"
    assert res["uncovered_candidates"][0]["item_id"] == "B"


def test_reevaluate_after_log_modification(client):
    assert import_csv(client, SAMPLE_CSV).status_code == 200
    first = client.post("/api/evaluate", json={"cap": 10.0, "min_ess": 1.0}).json()

    # 修改日志：替换为一份新日志后重新评估
    # 候选 A、B 均无点击（IPS=0），非候选 C 有点击（旧策略实测 CTR=1/3）→ 应判未通过
    new_csv = (
        "session_id,event_no,item_id,position,clicked,propensity,new_rank\n"
        "n1,x1,A,1,0,0.5,1\n"
        "n1,x2,B,2,0,0.5,2\n"
        "n1,x3,C,3,1,0.5,\n"
    )
    r = import_csv(client, new_csv, mode="replace")
    assert r.status_code == 200
    assert client.get("/api/status").json()["events"] == 3

    second = client.post("/api/evaluate", json={"cap": 10.0, "min_ess": 1.0}).json()
    assert second["id"] != first["id"]
    assert second["overall"]["included_sessions"] == 1
    assert second["overall"]["ips_ctr"] == pytest.approx(0.0)
    assert second["overall"]["baseline_ctr"] == pytest.approx(1 / 3, abs=1e-6)
    assert second["verdict"] == "fail"  # 0 < baseline 1/3

    # 历史保留两次评估
    history = client.get("/api/evaluations").json()
    assert len(history) == 2
    assert {h["id"] for h in history} == {first["id"], second["id"]}
    # 可按 ID 回看历史评估
    detail = client.get(f"/api/evaluations/{first['id']}").json()
    assert detail["overall"]["included_sessions"] == 2


def test_clear_logs(client):
    assert import_csv(client, SAMPLE_CSV).status_code == 200
    r = client.delete("/api/logs")
    assert r.status_code == 200
    assert r.json()["deleted_events"] == 7
    assert client.get("/api/status").json()["events"] == 0
    # 清空后无数据，评估返回 400
    assert client.post("/api/evaluate", json={"cap": 10.0}).status_code == 400


def test_index_page_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "推荐策略反事实评估工具" in r.text
