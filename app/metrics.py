"""反事实评估核心指标：截断 IPS 与自归一化（SNIPS）点击率。

纯函数实现，不依赖 FastAPI / SQLite，便于单元测试。

定义（按会话）：
- 权重 w_i = min(1 / propensity_i, cap)，cap 为用户输入的截断上限
- 截断 IPS 点击率  = sum(w_i * click_i) / n_logged_events
- 自归一化点击率    = sum(w_i * click_i) / sum(w_i)
- 有效样本量 ESS    = (sum w_i)^2 / sum(w_i^2)
仅对"新策略候选集中、且有日志曝光"的事件计入权重。
"""

from dataclasses import dataclass


@dataclass
class Exposure:
    item_id: str
    clicked: int
    propensity: float


def truncated_weight(propensity: float, cap: float) -> float:
    """截断逆倾向权重：min(1/p, cap)。"""
    if not 0 < propensity <= 1:
        raise ValueError(f"propensity 必须在 (0,1] 区间，得到 {propensity}")
    if cap <= 0:
        raise ValueError(f"截断上限必须为正数，得到 {cap}")
    return min(1.0 / propensity, cap)


def evaluate_session(exposures, candidate_ranks: dict, cap: float) -> dict:
    """评估单个会话。

    exposures: list[Exposure] 该会话全部日志曝光
    candidate_ranks: dict item_id -> 新策略排序位次
    """
    logged_items = {e.item_id for e in exposures}
    uncovered = [
        {"item_id": item, "new_rank": candidate_ranks[item]}
        for item in candidate_ranks
        if item not in logged_items
    ]
    uncovered.sort(key=lambda x: x["new_rank"])

    matched = [e for e in exposures if e.item_id in candidate_ranks]
    n_events = len(exposures)
    weights = [truncated_weight(e.propensity, cap) for e in matched]
    sum_w = sum(weights)
    sum_wc = sum(w for w, e in zip(weights, matched) if e.clicked)
    sum_w2 = sum(w * w for w in weights)
    ess = (sum_w * sum_w / sum_w2) if sum_w2 > 0 else 0.0

    return {
        "n_events": n_events,
        "n_candidates": len(candidate_ranks),
        "matched": len(matched),
        "sum_w": sum_w,
        "ess": ess,
        "ips_ctr": (sum_wc / n_events) if n_events else 0.0,
        "snips_ctr": (sum_wc / sum_w) if sum_w > 0 else None,
        "baseline_ctr": (sum(e.clicked for e in exposures) / n_events) if n_events else None,
        "uncovered": uncovered,
    }


def evaluate_dataset(sessions: dict, cap: float, min_ess: float) -> dict:
    """评估整个数据集。

    sessions: dict session_id -> {"exposures": [Exposure], "candidates": {item_id: rank}}

    排除规则（被排除的会话不计入通过结论）：
    1. 存在新策略候选未被日志覆盖（无对应曝光、拿不到倾向分数）→ excluded_uncovered
    2. 无任何匹配候选曝光，或有效样本量 ESS < min_ess → excluded_insufficient
    """
    per_session = []
    uncovered_list = []
    insufficient_list = []
    included = []

    for session_id in sorted(sessions):
        data = sessions[session_id]
        res = evaluate_session(data["exposures"], data["candidates"], cap)

        status = "included"
        reason = None
        if res["uncovered"]:
            status = "excluded_uncovered"
            reason = "存在未被日志覆盖的新策略候选"
            for u in res["uncovered"]:
                uncovered_list.append({"session_id": session_id, **u})
        elif res["matched"] == 0:
            status = "excluded_insufficient"
            reason = "无匹配的新策略候选曝光"
            insufficient_list.append(
                {"session_id": session_id, "reason": reason, "matched": 0, "ess": res["ess"]}
            )
        elif res["ess"] < min_ess:
            status = "excluded_insufficient"
            reason = f"有效样本量 ESS={res['ess']:.3f} 低于阈值 {min_ess}"
            insufficient_list.append(
                {"session_id": session_id, "reason": reason, "matched": res["matched"], "ess": res["ess"]}
            )

        row = {
            "session_id": session_id,
            "n_events": res["n_events"],
            "n_candidates": res["n_candidates"],
            "matched": res["matched"],
            "sum_w": round(res["sum_w"], 6),
            "ess": round(res["ess"], 6),
            "ips_ctr": round(res["ips_ctr"], 6),
            "snips_ctr": round(res["snips_ctr"], 6) if res["snips_ctr"] is not None else None,
            "baseline_ctr": round(res["baseline_ctr"], 6) if res["baseline_ctr"] is not None else None,
            "status": status,
            "reason": reason,
        }
        per_session.append(row)
        if status == "included":
            included.append(row)

    if included:
        overall = {
            "included_sessions": len(included),
            "excluded_sessions": len(per_session) - len(included),
            "total_events": sum(r["n_events"] for r in included),
            "ips_ctr": round(sum(r["ips_ctr"] for r in included) / len(included), 6),
            "snips_ctr": round(sum(r["snips_ctr"] for r in included) / len(included), 6),
            "baseline_ctr": round(sum(r["baseline_ctr"] for r in included) / len(included), 6),
        }
        verdict = "pass" if overall["ips_ctr"] >= overall["baseline_ctr"] else "fail"
    else:
        overall = {
            "included_sessions": 0,
            "excluded_sessions": len(per_session),
            "total_events": 0,
            "ips_ctr": None,
            "snips_ctr": None,
            "baseline_ctr": None,
        }
        verdict = "no_data"

    return {
        "cap": cap,
        "min_ess": min_ess,
        "overall": overall,
        "verdict": verdict,
        "per_session": per_session,
        "uncovered_candidates": uncovered_list,
        "insufficient_sessions": insufficient_list,
    }
