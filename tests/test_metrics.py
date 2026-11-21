"""指标核心逻辑的单元测试。"""

import pytest

from app.metrics import Exposure, evaluate_dataset, evaluate_session, truncated_weight


def test_truncated_weight_caps_large_values():
    # 倾向 0.01 → 1/p = 100，截断上限 10 → 权重应为 10
    assert truncated_weight(0.01, 10.0) == 10.0
    # 倾向 0.5 → 1/p = 2，未触及上限
    assert truncated_weight(0.5, 10.0) == 2.0
    # 倾向 1.0 → 权重恒为 1
    assert truncated_weight(1.0, 5.0) == 1.0


def test_truncated_weight_rejects_invalid():
    with pytest.raises(ValueError):
        truncated_weight(0.0, 10.0)
    with pytest.raises(ValueError):
        truncated_weight(1.5, 10.0)
    with pytest.raises(ValueError):
        truncated_weight(0.5, 0.0)


def test_evaluate_session_math():
    exposures = [
        Exposure("A", clicked=1, propensity=0.5),   # 候选，w=2
        Exposure("B", clicked=0, propensity=0.25),  # 候选，w=4
        Exposure("C", clicked=1, propensity=0.5),   # 非候选，不计权重
    ]
    res = evaluate_session(exposures, {"A": 1, "B": 2}, cap=10.0)
    assert res["matched"] == 2
    assert res["sum_w"] == pytest.approx(6.0)
    # 截断 IPS = (2*1 + 4*0) / 3 条曝光 = 2/3
    assert res["ips_ctr"] == pytest.approx(2.0 / 3.0)
    # SNIPS = 2 / 6 = 1/3
    assert res["snips_ctr"] == pytest.approx(1.0 / 3.0)
    # ESS = 36 / (4+16) = 1.8
    assert res["ess"] == pytest.approx(1.8)
    assert res["uncovered"] == []
    assert res["baseline_ctr"] == pytest.approx(2.0 / 3.0)


def test_evaluate_session_truncation_applied():
    exposures = [Exposure("A", clicked=1, propensity=0.01)]
    res = evaluate_session(exposures, {"A": 1}, cap=10.0)
    # 权重被截断为 10 而非 100
    assert res["sum_w"] == pytest.approx(10.0)
    assert res["ips_ctr"] == pytest.approx(10.0)
    assert res["snips_ctr"] == pytest.approx(1.0)


def test_evaluate_dataset_exclusions():
    sessions = {
        "ok": {
            "exposures": [Exposure("A", 1, 0.5), Exposure("B", 0, 0.5)],
            "candidates": {"A": 1, "B": 2},
        },
        "has_uncovered": {
            "exposures": [Exposure("A", 1, 0.5)],
            "candidates": {"A": 1, "Z": 2},  # Z 无曝光 → 未覆盖
        },
        "no_match": {
            "exposures": [Exposure("Q", 1, 0.5)],
            "candidates": {},  # 无候选 → 样本不足
        },
    }
    res = evaluate_dataset(sessions, cap=10.0, min_ess=1.0)
    assert res["overall"]["included_sessions"] == 1
    assert res["overall"]["excluded_sessions"] == 2
    assert [u["item_id"] for u in res["uncovered_candidates"]] == ["Z"]
    assert res["uncovered_candidates"][0]["session_id"] == "has_uncovered"
    assert [s["session_id"] for s in res["insufficient_sessions"]] == ["no_match"]
    # 结论只来自 ok 会话：IPS = (2*1+2*0)/2 = 1.0，SNIPS = 2/4 = 0.5
    assert res["overall"]["ips_ctr"] == pytest.approx(1.0)
    assert res["overall"]["snips_ctr"] == pytest.approx(0.5)
    assert res["verdict"] == "pass"  # 1.0 >= baseline 0.5


def test_evaluate_dataset_ess_threshold():
    sessions = {
        "s": {
            "exposures": [Exposure("A", 1, 0.5), Exposure("B", 0, 0.25)],
            "candidates": {"A": 1, "B": 2},
        }
    }
    # ESS = 36/20 = 1.8 < 2.0 → 样本不足，结论为 no_data
    res = evaluate_dataset(sessions, cap=10.0, min_ess=2.0)
    assert res["overall"]["included_sessions"] == 0
    assert res["verdict"] == "no_data"
    assert res["insufficient_sessions"][0]["session_id"] == "s"
