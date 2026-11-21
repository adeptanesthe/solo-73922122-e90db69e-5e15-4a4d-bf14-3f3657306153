"""单位置试投的选择策略（纯函数，不依赖 FastAPI / SQLite）。

规则（需求约定）：
- 同一轮中，每个物品最多有一条待反馈曝光；已经有待反馈曝光的物品不可再领。
- 可领物品中，优先挑选尚无任何反馈的；并列时按物品 ID 升序。
- 若可领物品都已有反馈，按本轮已登记结果计算 UCB1，取得分最高者；
  并列时按物品 ID 升序。

UCB1 公式（每个物品的统计只使用本轮该物品已登记的反馈）：
    avg_i + sqrt(2 * ln(N) / n_i)
其中 N = 本轮全部已登记反馈数，n_i = 物品 i 的已登记反馈数，
avg_i = 物品 i 已登记反馈中的点击比例。
"""

import math


def select_item(candidate_ids, pending_item_ids, feedback_stats):
    """按试投规则选择下一个要展示的物品。

    candidate_ids: 本轮候选物品 ID 列表（开轮快照）。
    pending_item_ids: 当前存在待反馈曝光的物品 ID 集合。
    feedback_stats: dict item_id -> {"pulls": int, "clicks": int}，
                    只统计本轮已登记（点击/未点击）的反馈。

    返回被选物品 ID；没有可领物品（候选为空或全部在等待反馈）时返回 None。
    """
    available = sorted(i for i in candidate_ids if i not in pending_item_ids)
    if not available:
        return None

    # 1) 优先：尚无任何反馈的物品，按物品 ID 升序
    fresh = [i for i in available if feedback_stats.get(i, {"pulls": 0})["pulls"] == 0]
    if fresh:
        return fresh[0]  # available 已排序，fresh 保持 ID 升序

    # 2) 都有反馈：UCB1 得分最高者，并列按物品 ID 升序
    n_total = sum(s["pulls"] for s in feedback_stats.values())
    best_id, best_score = None, None
    for item_id in available:
        s = feedback_stats[item_id]
        avg = s["clicks"] / s["pulls"]
        score = avg + math.sqrt(2.0 * math.log(n_total) / s["pulls"])
        if best_score is None or score > best_score:
            best_id, best_score = item_id, score
    return best_id
