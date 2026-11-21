"""试投选择策略（新鲜优先 + UCB1）的纯函数单元测试。"""

from app.bandit import select_item


def test_empty_and_all_pending_returns_none():
    assert select_item([], set(), {}) is None
    # 候选全部在等待反馈 → 无可领物品
    assert select_item(["A", "B"], {"A", "B"}, {}) is None


def test_fresh_items_have_priority_sorted_by_id():
    # B 有反馈，A/C 无反馈 → 取 ID 最小的 A
    stats = {"B": {"pulls": 3, "clicks": 3}}
    assert select_item(["A", "B", "C"], set(), stats) == "A"


def test_fresh_excludes_pending_items_and_keeps_id_order():
    # A 无反馈但在等待，C 无反馈可领 → 取 C
    stats = {"B": {"pulls": 1, "clicks": 1}}
    assert select_item(["A", "B", "C"], {"A"}, stats) == "C"


def test_ucb1_when_all_have_feedback():
    # n_total=10:
    # A: avg=0.8, ucb=0.8+sqrt(2ln10/5)≈1.759
    # B: avg=0.4, ucb=0.4+sqrt(2ln10/3)≈1.637
    # C: avg=0.5, pulls=2, ucb≈2.139 → 最高
    stats = {
        "A": {"pulls": 5, "clicks": 4},
        "B": {"pulls": 3, "clicks": 1},
        "C": {"pulls": 2, "clicks": 1},
    }
    assert select_item(["A", "B", "C"], set(), stats) == "C"


def test_ucb1_tie_breaks_by_item_id_ascending():
    # 相同 pulls/clicks → 得分完全相同 → ID 升序取 A
    stats = {
        "B": {"pulls": 2, "clicks": 1},
        "A": {"pulls": 2, "clicks": 1},
        "C": {"pulls": 2, "clicks": 1},
    }
    assert select_item(["A", "B", "C"], set(), stats) == "A"


def test_fresh_tie_breaks_by_item_id_ascending():
    assert select_item(["Z", "A", "M"], set(), {}) == "A"


def test_pending_item_blocked_even_if_ucb1_highest():
    # 若不被阻止，C 的 UCB1 最高；但 C 在等待反馈 → 改选 A
    stats = {
        "A": {"pulls": 5, "clicks": 4},
        "B": {"pulls": 3, "clicks": 1},
        "C": {"pulls": 2, "clicks": 2},
    }
    assert select_item(["A", "B", "C"], {"C"}, stats) == "A"
