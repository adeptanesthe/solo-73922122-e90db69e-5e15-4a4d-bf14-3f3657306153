"""推荐策略反事实评估工具 — FastAPI 入口。"""

import json
import math
import sqlite3
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import bandit, db
from .importer import CsvFormatError, parse_log_csv
from .metrics import Exposure, evaluate_dataset

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
SAMPLE_CSV = BASE_DIR.parent / "sample_data" / "sample_log.csv"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="推荐策略反事实评估工具", lifespan=lifespan)


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/sample-csv")
def sample_csv():
    if not SAMPLE_CSV.exists():
        raise HTTPException(status_code=404, detail="示例文件不存在")
    return FileResponse(SAMPLE_CSV, media_type="text/csv", filename="sample_log.csv")


@app.get("/api/status")
def status():
    conn = db.get_conn()
    try:
        ds = db.DEFAULT_DATASET_ID
        events = conn.execute("SELECT COUNT(*) c FROM events WHERE dataset_id=?", (ds,)).fetchone()["c"]
        sessions = conn.execute(
            "SELECT COUNT(DISTINCT session_id) c FROM events WHERE dataset_id=?", (ds,)
        ).fetchone()["c"]
        candidates = conn.execute(
            "SELECT COUNT(*) c FROM candidates WHERE dataset_id=?", (ds,)
        ).fetchone()["c"]
        evaluations = conn.execute(
            "SELECT COUNT(*) c FROM evaluations WHERE dataset_id=?", (ds,)
        ).fetchone()["c"]
        active_round = conn.execute(
            "SELECT COUNT(*) c FROM rounds WHERE dataset_id=? AND status='active'", (ds,)
        ).fetchone()["c"]
        return {"events": events, "sessions": sessions, "candidates": candidates,
                "evaluations": evaluations, "active_rounds": active_round}
    finally:
        conn.close()


@app.post("/api/import")
async def import_logs(file: UploadFile = File(...), mode: str = Form("append")):
    if mode not in ("append", "replace"):
        raise HTTPException(status_code=400, detail="mode 只能是 append 或 replace")
    raw = (await file.read()).decode("utf-8-sig")
    try:
        exposures, candidates = parse_log_csv(raw)
    except CsvFormatError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="文件必须是 UTF-8 编码的 CSV")

    ds = db.DEFAULT_DATASET_ID
    conn = db.get_conn()
    try:
        if mode == "replace":
            conn.execute("DELETE FROM events WHERE dataset_id=?", (ds,))
            conn.execute("DELETE FROM candidates WHERE dataset_id=?", (ds,))

        inserted = 0
        for r in exposures:
            cur = conn.execute(
                """INSERT OR IGNORE INTO events
                   (dataset_id, session_id, event_no, item_id, position, clicked, propensity, new_rank)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (ds, r["session_id"], r["event_no"], r["item_id"], r["position"],
                 r["clicked"], r["propensity"], r["new_rank"]),
            )
            inserted += cur.rowcount

        # 候选集 = 仅候选行 ∪ 曝光行中带 new_rank 的物品
        cand_rows = list(candidates)
        cand_rows += [
            {"session_id": r["session_id"], "item_id": r["item_id"], "new_rank": r["new_rank"]}
            for r in exposures if r["new_rank"] is not None
        ]
        cand_inserted = 0
        for c in cand_rows:
            cur = conn.execute(
                "INSERT OR IGNORE INTO candidates (dataset_id, session_id, item_id, new_rank) VALUES (?,?,?,?)",
                (ds, c["session_id"], c["item_id"], c["new_rank"]),
            )
            cand_inserted += cur.rowcount
        conn.commit()
    finally:
        conn.close()

    return {
        "mode": mode,
        "exposure_rows": len(exposures),
        "inserted": inserted,
        "duplicates_skipped": len(exposures) - inserted,
        "candidate_only_rows": len(candidates),
        "candidate_items_inserted": cand_inserted,
    }


@app.delete("/api/logs")
def clear_logs():
    ds = db.DEFAULT_DATASET_ID
    conn = db.get_conn()
    try:
        e = conn.execute("DELETE FROM events WHERE dataset_id=?", (ds,)).rowcount
        c = conn.execute("DELETE FROM candidates WHERE dataset_id=?", (ds,)).rowcount
        conn.commit()
        return {"deleted_events": e, "deleted_candidates": c}
    finally:
        conn.close()


class EvalRequest(BaseModel):
    cap: float = Field(10.0, gt=0, description="截断上限 M：权重 w=min(1/p, M)")
    min_ess: float = Field(2.0, ge=0, description="有效样本量阈值，低于则判为样本不足")


def _load_sessions(conn, ds: int) -> dict:
    sessions: dict = {}
    for e in conn.execute(
        "SELECT * FROM events WHERE dataset_id=? ORDER BY session_id, position", (ds,)
    ).fetchall():
        s = sessions.setdefault(e["session_id"], {"exposures": [], "candidates": {}})
        s["exposures"].append(
            Exposure(item_id=e["item_id"], clicked=e["clicked"], propensity=e["propensity"])
        )
        if e["new_rank"] is not None:
            s["candidates"][e["item_id"]] = e["new_rank"]
    for c in conn.execute("SELECT * FROM candidates WHERE dataset_id=?", (ds,)).fetchall():
        s = sessions.setdefault(c["session_id"], {"exposures": [], "candidates": {}})
        s["candidates"][c["item_id"]] = c["new_rank"]
    return sessions


@app.post("/api/evaluate")
def evaluate(req: EvalRequest):
    ds = db.DEFAULT_DATASET_ID
    conn = db.get_conn()
    try:
        sessions = _load_sessions(conn, ds)
        if not sessions:
            raise HTTPException(status_code=400, detail="当前没有日志数据，请先导入")
        result = evaluate_dataset(sessions, req.cap, req.min_ess)
        created_at = db.utcnow()
        cur = conn.execute(
            "INSERT INTO evaluations (dataset_id, cap, min_ess, created_at, result_json) VALUES (?,?,?,?,?)",
            (ds, req.cap, req.min_ess, created_at, json.dumps(result, ensure_ascii=False)),
        )
        conn.commit()
        eval_id = cur.lastrowid
    finally:
        conn.close()
    return {"id": eval_id, "created_at": created_at, **result}


@app.get("/api/evaluations")
def list_evaluations():
    ds = db.DEFAULT_DATASET_ID
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT id, cap, min_ess, created_at, result_json FROM evaluations WHERE dataset_id=? ORDER BY id DESC",
            (ds,),
        ).fetchall()
        out = []
        for r in rows:
            res = json.loads(r["result_json"])
            out.append(
                {
                    "id": r["id"],
                    "cap": r["cap"],
                    "min_ess": r["min_ess"],
                    "created_at": r["created_at"],
                    "verdict": res["verdict"],
                    "included_sessions": res["overall"]["included_sessions"],
                    "ips_ctr": res["overall"]["ips_ctr"],
                    "snips_ctr": res["overall"]["snips_ctr"],
                }
            )
        return out
    finally:
        conn.close()


@app.get("/api/evaluations/{eval_id}")
def get_evaluation(eval_id: int):
    conn = db.get_conn()
    try:
        r = conn.execute(
            "SELECT id, created_at, result_json FROM evaluations WHERE id=?", (eval_id,)
        ).fetchone()
        if r is None:
            raise HTTPException(status_code=404, detail="评估记录不存在")
        return {"id": r["id"], "created_at": r["created_at"], **json.loads(r["result_json"])}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 单位置试投（bandit 小流量）
# ---------------------------------------------------------------------------

class OpenRoundRequest(BaseModel):
    item_ids: list[str] = Field(..., min_length=1, max_length=20,
                                description="开轮候选物品 ID，1~20 个")


class FeedbackRequest(BaseModel):
    clicked: int = Field(..., ge=0, le=1, description="反馈结果：1=点击，0=未点击")


def _get_active_round(conn, ds: int):
    return conn.execute(
        "SELECT * FROM rounds WHERE dataset_id=? AND status='active' ORDER BY id DESC LIMIT 1",
        (ds,),
    ).fetchone()


@contextmanager
def immediate_write_conn(retries: int = 30, delay: float = 0.1):
    """获取带写事务的连接。

    SQLite 单写者模型下，并发请求在 BEGIN IMMEDIATE 处串行化；
    等不到锁时短暂重试，重试用尽后抛出 503，避免直接 500。
    业务异常（HTTPException）触发回滚，正常退出提交由调用方负责。
    """
    conn = db.get_conn()
    try:
        for _ in range(retries):
            try:
                conn.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError:  # database is locked，等待后重试
                time.sleep(delay)
        else:
            raise HTTPException(status_code=503,
                                detail="当前操作繁忙（数据库写锁等待超时），请稍后重试")
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def _round_candidate_ids(conn, round_id: int) -> list[str]:
    return [
        r["item_id"]
        for r in conn.execute(
            "SELECT item_id FROM round_candidates WHERE round_id=? ORDER BY item_id",
            (round_id,),
        ).fetchall()
    ]


def _build_round_state(conn, rnd) -> dict:
    """组装一轮试投的完整状态（候选快照、曝光、各物品统计与 UCB1）。"""
    round_id = rnd["id"]
    candidates = [
        {"item_id": r["item_id"], "session_id": r["session_id"], "new_rank": r["new_rank"]}
        for r in conn.execute(
            "SELECT item_id, session_id, new_rank FROM round_candidates "
            "WHERE round_id=? ORDER BY item_id",
            (round_id,),
        ).fetchall()
    ]
    exposures = [
        {
            "exposure_no": r["exposure_no"],
            "item_id": r["item_id"],
            "created_at": r["created_at"],
            "feedback": r["feedback"],
            "feedback_at": r["feedback_at"],
        }
        for r in conn.execute(
            "SELECT exposure_no, item_id, created_at, feedback, feedback_at "
            "FROM exposures WHERE round_id=? ORDER BY exposure_no",
            (round_id,),
        ).fetchall()
    ]

    stats = {c["item_id"]: {"pulls": 0, "clicks": 0, "pending": 0, "ucb1": None}
             for c in candidates}
    n_total = 0
    n_pending = 0
    for e in exposures:
        s = stats[e["item_id"]]
        if e["feedback"] is None:
            s["pending"] += 1
            n_pending += 1
        else:
            s["pulls"] += 1
            s["clicks"] += e["feedback"]
            n_total += 1
    for item_id, s in stats.items():
        if s["pulls"] > 0:
            avg = s["clicks"] / s["pulls"]
            s["ucb1"] = round(avg + math.sqrt(2.0 * math.log(n_total) / s["pulls"]), 6)

    return {
        "id": rnd["id"],
        "status": rnd["status"],
        "created_at": rnd["created_at"],
        "closed_at": rnd["closed_at"],
        "candidate_count": len(candidates),
        "candidates": candidates,
        "exposures": exposures,
        "stats": stats,
        "n_feedback": n_total,
        "n_pending": n_pending,
    }


@app.get("/api/trial/candidates")
def trial_candidate_pool():
    """已导入数据中的去重候选物品池，供开轮时勾选。"""
    ds = db.DEFAULT_DATASET_ID
    conn = db.get_conn()
    try:
        rows = conn.execute(
            """SELECT item_id,
                      MIN(session_id) AS session_id,
                      MIN(new_rank) AS new_rank,
                      COUNT(DISTINCT session_id) AS n_sessions
               FROM candidates WHERE dataset_id=?
               GROUP BY item_id ORDER BY item_id""",
            (ds,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.post("/api/trial/rounds", status_code=201)
def open_trial_round(req: OpenRoundRequest):
    """从已导入候选物品中选 1~20 个开一轮；同一时间只允许一个进行中的轮次。"""
    ds = db.DEFAULT_DATASET_ID
    item_ids = req.item_ids
    if len(set(item_ids)) != len(item_ids):
        raise HTTPException(status_code=400, detail="候选物品不能重复")
    conn = db.get_conn()
    try:
        with immediate_write_conn() as conn:
            if _get_active_round(conn, ds) is not None:
                raise HTTPException(status_code=409,
                                    detail="已有进行中的试投轮次，请先关闭后再开新轮")
            qmarks = ",".join("?" for _ in item_ids)
            rows = conn.execute(
                f"SELECT item_id, MIN(session_id) AS session_id, MIN(new_rank) AS new_rank "
                f"FROM candidates WHERE dataset_id=? AND item_id IN ({qmarks}) "
                f"GROUP BY item_id",
                (ds, *item_ids),
            ).fetchall()
            found = {r["item_id"] for r in rows}
            missing = sorted(set(item_ids) - found)
            if missing:
                raise HTTPException(
                    status_code=400,
                    detail=f"以下物品不在已导入的候选物品中：{', '.join(missing)}",
                )
            now = db.utcnow()
            round_id = conn.execute(
                "INSERT INTO rounds (dataset_id, status, created_at) VALUES (?, 'active', ?)",
                (ds, now),
            ).lastrowid
            conn.executemany(
                "INSERT INTO round_candidates (round_id, item_id, session_id, new_rank) "
                "VALUES (?,?,?,?)",
                [(round_id, r["item_id"], r["session_id"], r["new_rank"]) for r in rows],
            )
            conn.commit()
            rnd = conn.execute("SELECT * FROM rounds WHERE id=?", (round_id,)).fetchone()
            return _build_round_state(conn, rnd)
    except HTTPException:
        raise
    finally:
        conn.close()


@app.get("/api/trial/rounds/active")
def active_trial_round():
    """当前进行中的轮次完整状态；无进行中轮次时返回 active=false。"""
    ds = db.DEFAULT_DATASET_ID
    conn = db.get_conn()
    try:
        rnd = _get_active_round(conn, ds)
        if rnd is None:
            return {"active": False}
        return {"active": True, **_build_round_state(conn, rnd)}
    finally:
        conn.close()


@app.post("/api/trial/rounds/close")
def close_trial_round():
    ds = db.DEFAULT_DATASET_ID
    conn = db.get_conn()
    try:
        with immediate_write_conn() as conn:
            rnd = _get_active_round(conn, ds)
            if rnd is None:
                raise HTTPException(status_code=400, detail="当前没有进行中的试投轮次")
            conn.execute(
                "UPDATE rounds SET status='closed', closed_at=? WHERE id=?",
                (db.utcnow(), rnd["id"]),
            )
            conn.commit()
            rnd = conn.execute("SELECT * FROM rounds WHERE id=?", (rnd["id"],)).fetchone()
            return _build_round_state(conn, rnd)
    except HTTPException:
        raise
    finally:
        conn.close()


@app.post("/api/trial/claim")
def claim_exposure():
    """领取一次曝光：返回展示物品与曝光编号。

    以事务生效时的候选状态为准；每个物品在同一轮至多一条待反馈曝光，
    由数据库唯一索引与应用层选择逻辑共同保证（多页面并发领取同样受限）。
    """
    ds = db.DEFAULT_DATASET_ID
    conn = db.get_conn()
    try:
        with immediate_write_conn() as conn:
            # BEGIN IMMEDIATE 立即取写锁，并发领取在此串行化
            rnd = _get_active_round(conn, ds)
            if rnd is None:
                raise HTTPException(status_code=400, detail="当前没有进行中的试投轮次，请先开轮")
            round_id = rnd["id"]

            candidate_ids = _round_candidate_ids(conn, round_id)
            exp_rows = conn.execute(
                "SELECT item_id, feedback FROM exposures WHERE round_id=?", (round_id,)
            ).fetchall()
            pending = {r["item_id"] for r in exp_rows if r["feedback"] is None}
            stats: dict = {}
            for r in exp_rows:
                if r["feedback"] is not None:
                    s = stats.setdefault(r["item_id"], {"pulls": 0, "clicks": 0})
                    s["pulls"] += 1
                    s["clicks"] += r["feedback"]

            chosen = bandit.select_item(candidate_ids, pending, stats)
            if chosen is None:
                raise HTTPException(
                    status_code=409,
                    detail="所有候选物品都在等待反馈，请先登记已有曝光的点击结果",
                )

            next_no = conn.execute(
                "SELECT COALESCE(MAX(exposure_no), 0) + 1 AS n FROM exposures WHERE round_id=?",
                (round_id,),
            ).fetchone()["n"]
            now = db.utcnow()
            try:
                conn.execute(
                    "INSERT INTO exposures (round_id, exposure_no, item_id, created_at) "
                    "VALUES (?,?,?,?)",
                    (round_id, next_no, chosen, now),
                )
            except sqlite3.IntegrityError:
                # 并发下该物品已存在待反馈曝光（唯一索引兜底）
                raise HTTPException(
                    status_code=409,
                    detail=f"物品 {chosen} 已存在待反馈曝光，请先登记后再领取",
                )
            conn.commit()
        return {
            "round_id": round_id,
            "exposure_no": next_no,
            "item_id": chosen,
            "created_at": now,
            "feedback": None,
        }
    except HTTPException:
        raise
    finally:
        conn.close()


@app.post("/api/trial/exposures/{exposure_no}/feedback")
def register_feedback(exposure_no: int, req: FeedbackRequest):
    """按曝光编号登记点击/未点击；重复登记以首次结果为准（返回 409 且不改写）。"""
    ds = db.DEFAULT_DATASET_ID
    conn = db.get_conn()
    try:
        with immediate_write_conn() as conn:
            rnd = _get_active_round(conn, ds)
            if rnd is None:
                raise HTTPException(status_code=400, detail="当前没有进行中的试投轮次")
            exp = conn.execute(
                "SELECT * FROM exposures WHERE round_id=? AND exposure_no=?",
                (rnd["id"], exposure_no),
            ).fetchone()
            if exp is None:
                raise HTTPException(status_code=404,
                                    detail=f"曝光编号 {exposure_no} 不存在")
            if exp["feedback"] is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"曝光 {exposure_no} 已登记首次结果"
                           f"（{'点击' if exp['feedback'] == 1 else '未点击'}），"
                           "重复登记以首次结果为准，不做改写",
                )
            now = db.utcnow()
            conn.execute(
                "UPDATE exposures SET feedback=?, feedback_at=? WHERE id=?",
                (req.clicked, now, exp["id"]),
            )
            conn.commit()
        return {
            "exposure_no": exp["exposure_no"],
            "item_id": exp["item_id"],
            "created_at": exp["created_at"],
            "feedback": req.clicked,
            "feedback_at": now,
        }
    except HTTPException:
        raise
    finally:
        conn.close()
