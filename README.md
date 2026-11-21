# 推荐策略反事实评估工具

基于 **FastAPI + SQLite** 的 Web 工具：编辑在浏览器导入旧策略曝光日志（含新策略候选排序），
按会话计算**截断逆倾向加权点击率（Truncated IPS CTR）**与**自归一化点击率（SNIPS CTR）**，
用于上线前反事实评估新推荐策略。

## 快速启动

### Docker Compose（推荐）

```bash
docker compose up --build
```

打开 http://localhost:8000 。SQLite 数据持久化在命名卷 `recs_eval_data` 中。

### 本地运行

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

### 运行自测

```bash
# 本地
python -m pytest tests -v
# 或在容器内
docker compose run --rm web python -m pytest tests -v
```

## 日志 CSV 格式

列（表头必须齐全，顺序不限）：

| 列 | 含义 |
|---|---|
| `session_id` | 用户会话标识 |
| `event_no` | 事件号（**相同事件号只处理一次**，重复自动跳过） |
| `item_id` | 物品 ID |
| `position` | 曝光位置（≥1 的整数） |
| `clicked` | 点击标记（0/1） |
| `propensity` | 旧策略倾向分数，(0,1] |
| `new_rank` | 该物品在新策略候选排序中的位次（≥1）；留空表示未被新策略推荐 |

两种行：

- **曝光行**：`event_no` 非空，字段按上表校验。
- **仅候选行**：`event_no` 留空，只填 `session_id,item_id,new_rank`，表示新策略推荐了但日志中
  没有曝光的物品，用于识别"新策略未覆盖的候选"。

示例见 `sample_data/sample_log.csv`（页面内也可直接下载）。

## 指标定义（按会话）

设会话内与候选集匹配的曝光事件权重 `w_i = min(1/propensity_i, M)`，`M` 为用户输入的截断上限：

- **截断 IPS 点击率** = `Σ w_i·click_i / 会话曝光数`
- **自归一化 SNIPS 点击率** = `Σ w_i·click_i / Σ w_i`
- **有效样本量 ESS** = `(Σ w_i)² / Σ w_i²`

总体结论 = 纳入会话指标的算术平均。判定规则（供编辑参考）：纳入会话的平均截断 IPS 点击率
≥ 旧策略实测点击率 → **通过**，否则 **未通过**。

## 排除规则（不计入通过结论）

以下记录在页面**单列展示**，且**不计入**通过结论：

1. **新策略未覆盖的候选**：候选物品在日志中无对应曝光（拿不到倾向分数）→ 其所在会话整体排除；
2. **样本不足的会话**：无匹配候选曝光，或 ESS 低于阈值（默认 2.0，可在页面调整）→ 会话排除。

## 修改日志后重新评估

- 导入时选 **替换** 模式（或先点"清空日志"）上传新日志，再点 **运行评估**；
- 每次评估的参数与结果都存入 `evaluations` 表，页面底部"历史评估"可回看任意一次。

## 单位置试投（线上小流量 bandit）

页面中部"单位置试投"区块提供一个可在浏览器演示的单物品试投入口：

1. **开轮**：从已导入会话的候选物品中勾选 **1～20 个**物品开一轮；开轮时把候选快照写入
   `round_candidates`，之后即使替换日志，本轮仍使用开轮时的候选。同一时间只允许一个进行中的轮次。
2. **领取曝光**：点击"领取曝光"，接口返回答展示的物品与曝光编号（轮内从 1 递增）。
   - 同一轮中**每个物品最多有一条待反馈曝光**，由数据库部分唯一索引
     （`WHERE feedback IS NULL`）与 `BEGIN IMMEDIATE` 写事务共同保证，多页面同时领取也不超限；
   - 可领物品中**优先选尚无任何反馈的**，并列按物品 ID 升序；
   - 若可领物品都已有反馈，按本轮已登记结果计算 **UCB1**：
     `avg_i + sqrt(2·ln(N)/n_i)`，`N` 为本轮已登记反馈总数，取得分最高者，并列按物品 ID 升序；
   - 候选全部在等待反馈时返回 409，页面提示先登记。
3. **登记反馈**：刷新页面仍能看到待反馈曝光，按曝光编号登记"点击/未点击"；
   **重复登记以首次结果为准**（后续请求返回 409 且不改写）。
4. **关闭轮次**：关闭后不能再领取或登记；关闭后才可开新一轮。

### 试投 API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/trial/candidates` | 已导入候选物品池（按物品 ID 去重排序） |
| POST | `/api/trial/rounds` | 开轮，JSON `{"item_ids": [...]}`（1~20 个） |
| GET | `/api/trial/rounds/active` | 当前进行中轮次的完整状态（候选/曝光/统计/UCB1） |
| POST | `/api/trial/rounds/close` | 关闭当前轮次 |
| POST | `/api/trial/claim` | 领取一次曝光，返回 `{exposure_no, item_id, ...}` |
| POST | `/api/trial/exposures/{no}/feedback` | 按编号登记，JSON `{"clicked": 0|1}`；重复登记 409 |

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 操作页面 |
| POST | `/api/import` | 导入 CSV（`file` + `mode=append|replace`） |
| DELETE | `/api/logs` | 清空曝光与候选 |
| POST | `/api/evaluate` | 运行评估，JSON `{cap, min_ess}` |
| GET | `/api/evaluations` | 历史评估列表 |
| GET | `/api/evaluations/{id}` | 单次评估详情 |
| GET | `/api/status` | 当前数据量 |
| GET | `/api/sample-csv` | 下载示例 CSV |
| GET | `/api/trial/candidates` | 试投候选物品池（见上节） |
| POST | `/api/trial/rounds` | 开/关试投轮次、领取、登记（见上节） |

## 目录结构

```
app/
  main.py       # FastAPI 路由（评估 + 单位置试投）
  metrics.py    # 截断 IPS / SNIPS / ESS 纯函数
  bandit.py     # 试投选择策略（新鲜优先 + UCB1）纯函数
  importer.py   # CSV 解析校验
  db.py         # SQLite 连接与建表（含试投轮次/快照/曝光表）
  static/index.html  # 操作页面
tests/          # pytest 自测（指标 + API 端到端 + 试投策略/并发）
sample_data/    # 示例日志
Dockerfile / docker-compose.yml
```
