"""日志 CSV 解析与校验。

CSV 列（表头必须包含以下列，顺序不限）：
  session_id,event_no,item_id,position,clicked,propensity,new_rank

行类型：
- 曝光行：event_no 非空。position>=1，clicked∈{0,1}，propensity∈(0,1]，
  new_rank 可空（空表示该物品不在新策略候选排序中）。
- 仅候选行：event_no 为空，表示新策略推荐了但日志中没有曝光的物品，
  此时必须提供 new_rank。用于识别"新策略未覆盖的候选"。
"""

import csv
import io

COLUMNS = ["session_id", "event_no", "item_id", "position", "clicked", "propensity", "new_rank"]


class CsvFormatError(Exception):
    pass


def _parse_int(value, name, lineno, errors, min_v=None, choices=None):
    try:
        v = int(value)
    except (TypeError, ValueError):
        errors.append(f"第{lineno}行: {name} 必须是整数，得到 '{value}'")
        return None
    if min_v is not None and v < min_v:
        errors.append(f"第{lineno}行: {name} 必须 >= {min_v}，得到 {v}")
        return None
    if choices is not None and v not in choices:
        errors.append(f"第{lineno}行: {name} 只能是 {choices}，得到 {v}")
        return None
    return v


def _parse_float(value, name, lineno, errors):
    try:
        return float(value)
    except (TypeError, ValueError):
        errors.append(f"第{lineno}行: {name} 必须是数字，得到 '{value}'")
        return None


def parse_log_csv(text: str):
    """解析日志 CSV，返回 (exposures, candidates)。

    exposures: [{session_id, event_no, item_id, position, clicked, propensity, new_rank}]
    candidates: [{session_id, item_id, new_rank}]  （仅候选行）
    解析失败抛出 CsvFormatError，消息中包含全部行级错误。
    """
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise CsvFormatError("文件为空或缺少表头")
    missing = [c for c in COLUMNS if c not in reader.fieldnames]
    if missing:
        raise CsvFormatError(
            f"缺少必需列: {', '.join(missing)}；需要的列为: {', '.join(COLUMNS)}"
        )

    exposures, candidates, errors = [], [], []
    for lineno, row in enumerate(reader, start=2):
        col = lambda name: (row.get(name) or "").strip()
        session_id, event_no, item_id = col("session_id"), col("event_no"), col("item_id")
        position, clicked = col("position"), col("clicked")
        propensity, new_rank = col("propensity"), col("new_rank")

        if not session_id or not item_id:
            errors.append(f"第{lineno}行: session_id 和 item_id 不能为空")
            continue

        if not event_no:
            # 仅候选行：新策略推荐了但无日志曝光
            nr = _parse_int(new_rank, "new_rank", lineno, errors, min_v=1) if new_rank else None
            if not new_rank:
                errors.append(f"第{lineno}行: 仅候选行（event_no 为空）必须提供 new_rank")
            elif nr is not None:
                candidates.append({"session_id": session_id, "item_id": item_id, "new_rank": nr})
            continue

        pos = _parse_int(position, "position", lineno, errors, min_v=1)
        clk = _parse_int(clicked, "clicked", lineno, errors, choices=(0, 1))
        prop = _parse_float(propensity, "propensity", lineno, errors)
        if prop is not None and not 0 < prop <= 1:
            errors.append(f"第{lineno}行: propensity 必须在 (0,1] 区间，得到 {prop}")
            prop = None
        nr = _parse_int(new_rank, "new_rank", lineno, errors, min_v=1) if new_rank else None

        if pos is None or clk is None or prop is None or (new_rank and nr is None):
            continue
        exposures.append(
            {
                "session_id": session_id,
                "event_no": event_no,
                "item_id": item_id,
                "position": pos,
                "clicked": clk,
                "propensity": prop,
                "new_rank": nr,
            }
        )

    if errors:
        raise CsvFormatError("；".join(errors[:20]))
    if not exposures and not candidates:
        raise CsvFormatError("文件中没有有效数据行")
    return exposures, candidates
