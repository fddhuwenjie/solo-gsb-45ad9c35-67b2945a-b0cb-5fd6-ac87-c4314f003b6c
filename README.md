# 事故复盘因果图（Incident Causal Graph）

基于 **FastAPI + SQLite + 原生 JavaScript** 的单页 Web 应用，用于事故复盘时录入「事件 / 证据」节点，
在画布上拖拽连线构建因果图，并以**事务**方式整批提交修改：任何会使图成环、或使事件时间窗传播后
产生矛盾的变更都会被**整批拒绝**。每次成功提交生成不可变的**版本快照**，可浏览旧版、一键回滚，
并对指定节点重算下游影响。内置**证据强度审查**：证据带可靠度与来源组，按支持 / 反驳两个方向
聚合并自动判定每个事件「已支持 / 已反驳 / 存在争议 / 证据不足」。另有只读的**反事实干预沙盘**：
勾选若干被阻止事件并指定观察目标，按事件的 `trigger_mode`（ANY / ALL）拓扑推演 prevented /
still_active，并给出目标的递归依据树。**可复算复盘结论包**：把任一事件连同全部 causes 祖先、
关联证据、版本元数据与重算结论打包成带 SHA-256 摘要的自包含文件，可下载留存、可上传只读核验。
**根因共识盲评**：从某个不可变版本选取 3~20 个事件作为候选，登记唯一评审人并设置最低票数；
每位评审人提交覆盖全部候选的严格排序，封榜前只公开已提交名单与数量、不泄露任何排序，
封榜（写事务内校验 `session_revision` 与最低票数）后按 **Schulze 法**计算两两偏好与最强路径，
逐层生成并列名次并公开全部选票。

## ✨ 功能特性

- **画布编辑（原生 JS + SVG）**
  - 录入事件（event）与证据（evidence）节点，填写标题与描述
  - 事件节点可填写**带时区的最早 / 最晚发生时间**（均可留空）
  - 证据节点可填写**可靠度 reliability（0~1）**与**来源组 source_group**（留空为独立来源）
  - 拖拽节点移动；从节点右侧圆点拖出到目标节点即创建连线
  - 连线方向规则（由端点类型自动判定）：
    - 事件 → 事件：因果 `causes`（实线箭头，表示源事件**不晚于**目标事件）
    - 证据 → 事件：证据连线，默认支持 `supports`（虚线箭头），可在连线详情切换为反驳 `refutes`（红色点线）
  - 拖空白处平移、滚轮缩放、Delete 删除选中元素
- **证据强度审查（Evidence Strength Review）**
  - 每条证据连线带立场：`supports`（支持）或 `refutes`（反驳），二者可在连线详情中互切
  - **同源去重**：同一 `source_group` 在每个方向（支持 / 反驳）只采用 reliability **最高**的一条，
    其余标记为「同源未计权」；未填写来源组的证据各自视为独立组
  - **独立聚合**：各入选组按独立证据合并 —— 支持强度 `S = 1 − ∏(1−r)`，反驳强度 `R = 1 − ∏(1−r)`
  - **状态判定**（按顺序）：`S≥0.6 且 R≥0.6` → 存在争议（disputed）；
    `S≥0.7` → 已支持（supported）；`R≥0.7` → 已反驳（refuted）；其余 → 证据不足（insufficient）
  - 选中事件即展示 S / R、状态，以及每个来源组的**入选证据**与**同源未计权证据**清单；
    事件节点右上角有状态色点（绿=已支持，红=已反驳，黄=存在争议）
  - 未提交的工作图**实时重算**；当前版与历史快照各自按该版本数据重算，**不改写任何旧版数据**
- **整组修改 + 事务校验**
  - 新建 / 更新 / 删除节点与连线先在本地累计为一个 changeset
  - 提交时在**一个 SQLite 事务**内把变更应用到整张图并重新做 DAG 与时间窗校验
  - 成环（含自环）、悬空端点、类型不合法、重复边、时间窗矛盾等任何一项失败 → `ROLLBACK`，全部变更拒绝
  - 乐观锁：页面基于的 `base_version_id` 必须是服务端最新版本，否则拒绝
- **事件时间窗传播（UTC 统一换算）**
  - 提交的时间必须带时区（如 `2026-09-20T10:00:00+08:00` / `...Z`），后端统一换算为 UTC 存储
  - 每条 `causes` 边表示 `t(源) ≤ t(目标)`，提交时在**应用后的整张图**上传播：
    - 有效下界 = 自身与**全部祖先**下界（earliest）的最大值
    - 有效上界 = 自身与**全部后代**上界（latest）的最小值
  - 选中事件可查看有效时间窗及**收紧来源节点 / 因果链**；未提交时前端按工作图实时重算
  - 任一事件「有效下界晚于有效上界」→ 整批回滚，响应给出提供两端界限的节点与可追溯因果链，
    前端红色高亮冲突链并保留全部未提交内容
- **版本快照与回滚**
  - 每次成功提交写入一条 `versions` 记录，并把全量 nodes/edges（含时间窗与证据强度字段）复制为该版本快照
  - 右侧版本列表面板可只读浏览任意历史版本，有效时间窗与证据强度审查**按所查看版本重算**
  - 「以此版本回滚」会把旧快照复制成一个**新版本**（不删除历史，可审计）
- **下游影响重算**
  - 选中任意节点 → 「重算下游影响」：服务端沿所有出边做 BFS 分层
  - 返回受影响节点 / 事件 / 证据数量、最大层级、分层节点与路径边，并在画布上绿色高亮
  - 存在未提交修改时自动改用本地计算并标注「基于未提交工作图」
- **反事实干预沙盘（只读 Counterfactual Sandbox）**
  - 事件节点新增 `trigger_mode`：`ANY` 表示任一 `causes` 前因发生即可触发，
    `ALL` 表示全部前因均发生才触发；**没有前因的事件默认发生**（可在事件详情中切换，随事务提交）
  - 右侧沙盘面板可在**当前工作图（含未提交修改）或任一历史快照**中勾选多个**被阻止事件**（干预点）
    并指定**观察目标**，系统按拓扑顺序推演每个事件的 `prevented` / `still_active`
  - 目标返回**递归依据树**：ANY 发生时取 **ID 最小的仍发生前因**，ANY 被阻止时列出全部前因；
    ALL 发生时列出全部前因，ALL 被阻止时取 **ID 最小的已阻止前因**；干预点标记为 `intervention`，不再展开
  - 画布三类高亮：🟧 **干预点**（橙色加粗 + ⛔ 标记）、🟥 **被阻止节点**（红色虚线，含推演得出）、
    🟪 **依据树**（紫色节点描边与因果边）；面板同时给出计数与可点击跳转的依据树
  - **完全只读**：不产生 changeset、不生成版本、不写库；未提交工作图在前端本地即时推演
    （与后端同构），干净当前版 / 历史快照调 `POST /api/counterfactual/simulate`
  - `trigger_mode` 随事务提交、快照复制与回滚保存；旧数据库启动时幂等迁移，历史事件行回填默认 `ANY`
- **可复算复盘结论包（Recomputable Conclusion Package）**
  - 从当前版或任一历史快照选择一个事件，服务端收集**该事件、全部 `causes` 祖先、
    以及这些事件关联的 `supports` / `refutes 证据**，连同**版本元数据**、按包内子图重算的
    **有效时间窗**与**证据审查结果**，组成**自包含 payload**
  - 规范化：对象键按**字典序**、节点与连线按 **ID 排序**，对 payload 的**紧凑 UTF-8 JSON**
    计算 **SHA-256**；页面可下载含 digest 的 `.json` 文件（文件内容即规范化 JSON）
  - **上传核验（只读）**：先校验 digest，再**仅凭包内原始节点与连线重建 DAG**，
    重算时间窗与证据审查，与随包结果**逐字段比对**；任何差异都列出
    **JSON Pointer、包内值、重算值**；**摘要匹配且重算一致**才显示「✔ 通过」
  - 导出与核验均**不写库、不生成版本、不改变工作图**；核验甚至不接触数据库（自包含）
- **根因共识盲评（Schulze Blind Review）**
  - **建场**：从某个**不可变版本快照**选取 **3~20 个事件**作为候选，登记**唯一评审人**并设置**最低票数**；
    评审场只读绑定该版本，不改写因果图、版本或结论包
  - **盲投**：每位登记评审人提交「包含全部候选且不重复」的**严格排序**；封榜前接口只做
    存在性公开（已提交名单 + 数量），**不泄露任何排序**；可覆盖自己的选票
  - **封榜**：在 SQLite 写事务（`BEGIN IMMEDIATE`）中校验 `session_revision` 与最低票数后
    冻结选票；revision 过期、重复封榜、票数不足的封榜均返回 **409**
  - **计票**：按 **Schulze 法**计算两两偏好 `d` 与最强路径 `p`，按胜出关系**逐层生成并列名次**；
    候选 ID 仅用于稳定展示，**不打断算法并列**；任意一对胜负都返回**确定性的最强路径见证**
  - 封榜后公开全部选票，名次 / 矩阵 / 见证均可在前端「评审室」弹窗中查看

## 🚀 一条命令启动

需要已安装 Docker（含 Compose 插件）：

```bash
docker compose up -d --build
```

打开 <http://localhost:8000> 即可使用（首次启动自动建表并写入一份「支付链路中断」示例快照）。

停止与清理：

```bash
docker compose down          # 停止（数据保留在 Docker volume causal-data 中）
docker compose down -v       # 停止并删除数据卷
```

## 🧑‍💻 本地开发（不使用 Docker）

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

SQLite 数据库默认位于 `./data/app.db`，可用环境变量 `DB_PATH` 覆盖（删除该文件即恢复初始示例）。

## 🔌 HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/state` | 当前最新版本的完整图（nodes + edges） |
| GET | `/api/versions` | 版本快照列表（含节点/连线计数，倒序） |
| GET | `/api/versions/{id}` | 查看指定历史版本的图 |
| POST | `/api/changes/commit` | 事务提交一组修改（见下） |
| POST | `/api/versions/rollback` | 以 `target_version_id` 生成回滚新版本 |
| GET | `/api/nodes/{id}/downstream?version_id=` | 重算某节点下游影响（不传版本号则取最新） |
| POST | `/api/counterfactual/simulate` | 反事实干预沙盘推演（只读，见下） |
| GET | `/api/conclusion/export/{event_id}?version_id=` | 导出可复算复盘结论包文件（只读，见下） |
| POST | `/api/conclusion/verify` | 上传结论包文件做只读核验（见下） |
| GET | `/api/reviews` | 根因共识盲评：评审场列表（含计数与状态） |
| POST | `/api/reviews` | 新建评审场（绑定版本 + 3~20 候选事件 + 唯一评审人 + 最低票数） |
| GET | `/api/reviews/{id}` | 评审场详情：封榜前只含已提交名单/数量，封榜后附 Schulze 结果 |
| POST | `/api/reviews/{id}/ballots` | 提交 / 覆盖选票（登记评审人 + 全候选严格排序；封榜后 409） |
| POST | `/api/reviews/{id}/close` | 封榜（写事务校验 `expected_revision` 与最低票数；过期/重复 409） |
| GET | `/health` | 健康检查 |

交互文档：启动后访问 <http://localhost:8000/docs>。

### 提交示例

```bash
curl -X POST http://localhost:8000/api/changes/commit \
  -H 'Content-Type: application/json' \
  -d '{
    "message": "补充根因：缓存雪崩",
    "changes": {
      "base_version_id": 1,
      "node_creates": [
        {"id": "ev-cache", "type": "event", "title": "缓存雪崩",
         "description": "大批 key 同时过期", "pos_x": 120, "pos_y": 560}
      ],
      "edge_creates": [
        {"id": "edge-x1", "source_id": "ev-cache", "target_id": "n-null",
         "kind": "causes", "label": "缓存未命中打满下游"}
      ],
      "node_updates": [],
      "node_deletes": [],
      "edge_updates": [],
      "edge_deletes": []
    }
  }'
```

成功返回 `{"version_id": 2, ...}`。若该连线会成环，返回 HTTP 400：

```json
{ "detail": "该变更会使图形成环：A → B → A", "code": "cycle", "cycle": ["...", "..."] }
```

### 触发模式与反事实干预沙盘示例

事件节点可设置 `trigger_mode`（`ANY` / `ALL`，缺省 `ANY`；仅事件节点可设，证据节点提交该字段返回 400
`trigger_mode_not_allowed`）。它随事务提交并写入每个快照：

```json
"node_updates": [
  {"id": "n-null", "trigger_mode": "ALL"}
]
```

初始示例中「空指针异常」就是 ALL 门（需「错误版本发布」与「配置开关失效」同时发生）。沙盘推演为
**只读**接口 —— 在请求体中给出观察目标与被阻止事件（干预点），`version_id` 留空表示当前最新版本：

```bash
curl -X POST http://localhost:8000/api/counterfactual/simulate \
  -H 'Content-Type: application/json' \
  -d '{
    "target_id": "n-loss",
    "prevented_ids": ["n-code"]
  }'
```

响应给出每个事件的拓扑推演状态与目标的递归依据树（节选）：

```json
{
  "target_id": "n-loss",
  "target_status": "prevented",
  "intervened_ids": ["n-code"],
  "prevented_ids": ["n-code", "n-loss", "n-null", "n-outage"],
  "still_active_ids": ["n-config"],
  "events": [
    {"node_id": "n-code", "trigger_mode": "ANY", "status": "prevented", "reason": "intervention"},
    {"node_id": "n-null", "trigger_mode": "ALL", "status": "prevented", "reason": "some_prevented"},
    {"node_id": "n-outage", "trigger_mode": "ANY", "status": "prevented", "reason": "all_prevented"}
  ],
  "tree_edge_ids": ["e1", "e3", "e4"],
  "tree": {
    "node_id": "n-loss", "trigger_mode": "ANY", "status": "prevented", "reason": "all_prevented",
    "children": [
      { "node_id": "n-outage", "trigger_mode": "ANY", "status": "prevented", "reason": "all_prevented",
        "children": [
          { "node_id": "n-null", "trigger_mode": "ALL", "status": "prevented", "reason": "some_prevented",
            "children": [
              { "node_id": "n-code", "trigger_mode": "ANY", "status": "prevented",
                "reason": "intervention", "children": [] }
            ] }
        ] }
    ]
  }
}
```

依据树子节点选择规则：

| 节点状态 | trigger_mode=ANY | trigger_mode=ALL |
| --- | --- | --- |
| still_active | 取 **ID 最小的仍发生前因**（`any_active`） | 列出**全部前因**（`all_active`） |
| prevented | 列出**全部前因**（`all_prevented`） | 取 **ID 最小的已阻止前因**（`some_prevented`） |

无前因事件的 `reason` 为 `no_causes`（默认发生），依据树无子节点；显式勾选的干预点
`reason` 为 `intervention` 且不展开。目标 / 干预点不是事件或不存在时返回 HTTP 400。
在前端，未提交工作图由本地同构引擎即时推演（画布即时高亮），历史快照与干净当前版才发请求；
任何推演都**不会**产生 changeset 或版本。

### 证据强度审查示例

证据节点带 `reliability`（0~1）与 `source_group`；证据连线 `kind` 为 `supports` 或 `refutes`：

```bash
curl -X POST http://localhost:8000/api/changes/commit \
  -H 'Content-Type: application/json' \
  -d '{
    "message": "为链路中断补充正反证据",
    "changes": {
      "base_version_id": 1,
      "node_creates": [
        {"id": "e-sre", "type": "evidence", "title": "SRE 值班记录",
         "pos_x": 600, "pos_y": 460, "reliability": 0.6, "source_group": "值班记录"},
        {"id": "e-isp", "type": "evidence", "title": "运营商公告",
         "pos_x": 760, "pos_y": 460, "reliability": 0.7, "source_group": "运营商"}
      ],
      "edge_creates": [
        {"id": "e10", "source_id": "e-sre", "target_id": "n-outage", "kind": "supports", "label": "确认中断"},
        {"id": "e11", "source_id": "e-isp", "target_id": "n-outage", "kind": "refutes", "label": "公告说更早恢复"}
      ]
    }
  }'
```

提交后 `GET /api/state` 中每个事件节点附带按该版本重算的 `evidence_review`：

```json
{
  "id": "n-outage",
  "evidence_review": {
    "support_score": 0.6, "refute_score": 0.7, "status": "disputed",
    "groups": [
      {"group": "值班记录", "direction": "supports",
       "selected": {"node_id": "e-sre", "edge_id": "e10", "title": "SRE 值班记录",
                    "reliability": 0.6, "reliability_set": true, "source_group": "值班记录"},
       "unweighted": []},
      {"group": "运营商", "direction": "refutes",
       "selected": {"node_id": "e-isp", "edge_id": "e11", "title": "运营商公告",
                    "reliability": 0.7, "reliability_set": true, "source_group": "运营商"},
       "unweighted": []}
    ],
    "selected_evidence": [ ... ],
    "unweighted_evidence": [ ]
  }
}
```

同源去重：若 `e-a(r=0.9)` 与 `e-b(r=0.8)` 同属 `"监控平台"` 且都 supports 同一事件，
只有 `e-a` 计入 `S`，`e-b` 出现在 `unweighted_evidence`（`reason: "same_source"`）。
`source_group` 留空（或提交 `null`）的证据各自独立计权。

连线立场切换（仅 `supports` ↔ `refutes`，`causes` 不可改）：

```json
"edge_updates": [{"id": "e6", "kind": "refutes"}]
```

约束：`reliability` 必须在 0~1（否则 422）；`reliability` / `source_group` 仅证据节点可设
（事件节点提交这两个字段返回 400 `evidence_field_not_allowed`）；清除来源组时显式传
`"source_group": null`。历史快照中未设置 `reliability` 的证据按 0 参与计算，并在
`selected.reliability_set = false` 中标注。

时间字段（`earliest_time` / `latest_time`，仅事件节点，可省略或为 `null`）必须带时区：

```json
"node_updates": [
  {"id": "n-null", "earliest_time": "2026-09-20T10:00:00+08:00", "latest_time": "2026-09-20T10:01:30Z"}
]
```

> 清除某条界限时显式传 `null`（与「不更新该字段」相区别）；不传时区（如 `2026-09-20T10:00:00`）返回 422。

时间窗传播后若下界晚于上界，返回 HTTP 400：

```json
{
  "detail": "时间窗冲突：「错误版本发布」给出的下界 … 晚于「空指针异常」给出的上界 …，但因果链 … 要求源事件不晚于目标事件",
  "code": "time_conflict",
  "conflict": {
    "node_id": "n-code",
    "lower_bound": "2026-09-20T03:00:00Z",
    "upper_bound": "2026-09-20T02:01:30Z",
    "lower_node_id": "n-code", "lower_node_title": "错误版本发布",
    "upper_node_id": "n-null", "upper_node_title": "空指针异常",
    "chain_node_ids": ["n-code", "n-null"],
    "chain_edge_ids": ["e1"]
  }
}
```

前端会把 `chain_node_ids` / `chain_edge_ids` 红色高亮，且数据库中不留任何半成品（事务已回滚，未提交编辑保留在页面）。

`/api/state` 与 `/api/versions/{id}` 的每个事件节点还附带按该版本重算的有效时间窗：

```json
{
  "id": "n-null",
  "earliest_time": "2026-09-20T02:00:00Z", "latest_time": "2026-09-20T02:01:30Z",
  "time_window": {
    "earliest_time": "2026-09-20T02:00:00Z", "latest_time": "2026-09-20T02:01:30Z",
    "effective_lower": "2026-09-20T02:00:30Z",
    "effective_upper": "2026-09-20T02:01:30Z",
    "lower_source": {"node_id": "n-code", "title": "错误版本发布"},
    "upper_source": {"node_id": "n-null", "title": "空指针异常"},
    "lower_chain": ["n-code", "n-null"], "upper_chain": ["n-null"]
  }
}
```

证据节点不参与时间窗，其 `earliest_time` / `latest_time` / `time_window` 恒为 `null`。

每个事件节点还附带按同一版本重算的证据强度审查（节选）：

```json
"evidence_review": {
  "support_score": 0.9, "refute_score": 0.7, "status": "disputed",
  "groups": [
    {"group": "监控平台", "direction": "supports",
     "selected": {"node_id": "e-log", "edge_id": "e5", "title": "监控日志 E-01",
                  "reliability": 0.9, "reliability_set": true, "source_group": "监控平台"},
     "unweighted": [
       {"node_id": "e-metric", "edge_id": "e7", "title": "错误率指标 M-22",
        "reliability": 0.8, "reliability_set": true, "source_group": "监控平台",
        "reason": "same_source", "group": "监控平台", "direction": "supports"}
     ]}
  ],
  "selected_evidence": [ ... ],
  "unweighted_evidence": [ ... ]
}
```

证据节点的 `reliability` / `source_group` 随节点对象直接返回（未设置为 `null`）。

### 可复算复盘结论包示例

**导出**：`GET /api/conclusion/export/{event_id}`（`version_id` 留空取当前最新版，传历史快照 id
则按该版本导出）。响应即结论包文件（`Content-Disposition: attachment`），内容为规范化 JSON
（对象键按字典序、节点与连线按 id 排序）：

```bash
curl -OJ http://localhost:8000/api/conclusion/export/n-null
# 保存为 conclusion-n-null-v1-<digest前8位>.json；导出历史快照：
curl -OJ "http://localhost:8000/api/conclusion/export/n-null?version_id=1"
```

文件结构（节选）：

```json
{
  "format": "incident-conclusion-package",
  "format_version": 1,
  "digest": {
    "algorithm": "sha256",
    "canonicalization": "json:utf8,compact,sort_keys,integral-floats-as-int",
    "value": "36b288a4…"
  },
  "payload": {
    "meta": { "root_event_id": "n-null", "version_id": 1, "version_message": "…",
              "generated_at": "2026-09-26T…", "node_count": 8, "edge_count": 7 },
    "nodes": [ { "id": "e-deploy", "type": "evidence", … }, … ],
    "edges": [ { "id": "e1", "kind": "causes", … }, … ],
    "results": {
      "time_windows":     { "n-null": { "effective_lower": "2026-09-20T02:00:30Z", … }, … },
      "evidence_reviews": { "n-null": { "support_score": 0.9, "refute_score": 0.7,
                                        "status": "disputed", … }, … }
    }
  }
}
```

- `payload.nodes` / `payload.edges` 是**原始数据**（根事件 + 全部 causes 祖先 + 关联证据）；
  `payload.results` 是**在包内子图上重算**的时间窗与证据审查 —— 因此包是自包含的：
  结论只依赖包内数据（注意：根事件下游不在包内，其 latest 不参与有效上界）。
- `digest.value` = 对 `payload` 的紧凑 UTF-8 JSON（`sort_keys`、无空格分隔符，且整数值浮点
  如 `60.0` 规范化为 `60`）计算的 SHA-256。数字规范化使摘要可用任意标准 JSON 工具复算，例如：
  `jq -cjS .payload pkg.json | sha256sum`（`-j` 去掉末尾换行；已实测与 jq / Node `JSON.stringify` 一致）。

**核验**：`POST /api/conclusion/verify`，请求体为上传文件的原始内容（`Content-Type: application/json`）：

```bash
curl -X POST http://localhost:8000/api/conclusion/verify \
  -H 'Content-Type: application/json' \
  --data-binary @conclusion-n-null-v1-36b288a4.json
```

响应（通过时）：

```json
{
  "ok": true,
  "digest":   { "algorithm": "sha256", "expected": "36b288a4…", "actual": "36b288a4…", "match": true },
  "recompute": { "consistent": true, "error": null, "events_checked": 3,
                 "diff_count": 0, "diffs_truncated": false, "diffs": [] },
  "meta": { "root_event_id": "n-null", "version_id": 1, … }
}
```

核验分两步，**都通过** `ok` 才为 `true`（页面显示「✔ 通过」）：

1. **摘要校验**：对包内 `payload` 重新规范化并计算 SHA-256，与 `digest.value` 比对；
2. **重算比对**：仅凭 `payload.nodes` / `payload.edges` 重建 DAG，重算时间窗与证据审查，
   与 `payload.results` 逐字段比对。任何差异都在 `recompute.diffs` 中列出
   **JSON Pointer、包内值、重算值**（缺失侧为 `null`，差异最多列 200 条）：

```json
{ "pointer": "/results/evidence_reviews/n-null/support_score",
  "packaged": 0.45, "recomputed": 0.9 }
```

即使篡改者改动结果后重新计算了 digest（摘要匹配），重算比对仍会逐字段抓出差异。
文件不是合法 JSON、缺 `format` / `digest` / `payload` 或版本号不支持时返回 HTTP 400。
导出与核验均**不写库、不生成版本、不改变工作图**；核验不接触数据库。

### 根因共识盲评示例

**建场**：绑定某个不可变版本，选 3~20 个事件作为候选，登记唯一评审人并设最低票数：

```bash
curl -X POST http://localhost:8000/api/reviews \
  -H 'Content-Type: application/json' \
  -d '{
    "title": "支付故障根因排序",
    "version_id": 1,
    "candidate_ids": ["n-code", "n-config", "n-null", "n-outage"],
    "reviewers": ["alice", "bob", "carol"],
    "min_votes": 2
  }'
```

返回评审场详情（`revision` 从 1 开始）。候选必须是该版本快照中的**事件节点**，
评审人去空白后必须唯一，`min_votes` 不得超过评审人数量，否则返回 400。

**投票**：每位登记评审人提交「包含全部候选且不重复」的严格排序；重复提交即覆盖自己的选票：

```bash
curl -X POST http://localhost:8000/api/reviews/1/ballots \
  -H 'Content-Type: application/json' \
  -d '{"reviewer": "alice", "ranking": ["n-null", "n-code", "n-outage", "n-config"]}'
```

每次投票使 `session_revision` 自增。`GET /api/reviews/1` 在封榜前只返回
`submitted`（已提交名单）与 `submitted_count`，**不包含任何排序内容**；
未登记评审人返回 403，排序缺候选 / 有重复 / 含非候选返回 400。

**封榜**：在 SQLite 写事务（`BEGIN IMMEDIATE`）中校验 `expected_revision` 等于当前
`session_revision`、已提交票数达到 `min_votes`，随后冻结选票：

```bash
curl -X POST http://localhost:8000/api/reviews/1/close \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 3}'
```

- revision 过期 → `409 {"code": "stale_revision"}`；重复封榜 → `409 {"code": "already_closed"}`；
- 未达最低票数 → `409 {"code": "min_votes_not_met"}`；封榜后再投票 → `409 {"code": "session_closed"}`。

**计票结果**（封榜后 `GET /api/reviews/1` 的 `result` 字段，节选）：

```json
{
  "method": "schulze",
  "candidate_order": ["n-code", "n-config", "n-null", "n-outage"],
  "pairwise":  { "n-null": { "n-code": 2, "n-outage": 2, "n-config": 2 }, "...": {} },
  "strongest": { "n-null": { "n-code": 2, "n-outage": 2, "n-config": 2 }, "...": {} },
  "ranks": [
    { "rank": 1, "node_ids": ["n-null"] },
    { "rank": 2, "node_ids": ["n-code", "n-outage"] },
    { "rank": 3, "node_ids": ["n-config"] }
  ],
  "witnesses": {
    "n-null": { "n-code": { "strength": 2, "path": ["n-null", "n-code"] } }
  },
  "ballots": [ { "reviewer": "alice", "ranking": ["n-null", "..."], "submitted_at": "..." } ]
}
```

- `pairwise`：`d[i][j]` = 把 i 严格排在 j 之前的选票数；
- `strongest`：Schulze 最强路径强度 `p[i][j]`（只保留 `d[i][j] > d[j][i]` 的有向边，
  路径强度 = 路径上最弱一环，取所有路径的最大值）；
- `ranks`：每轮取出「不被其余候选击败」（对所有 j 都有 `p[j][i] ≤ p[i][j]`）的集合作为
  同一**并列层**；同层即算法并列，层内按候选 ID 排序**仅为稳定展示，不打断并列**；
- `witnesses`：任意 `p[i][j] > 0` 的方向都给出确定性最强路径见证 —— 在
  「`d[u][v] > d[v][u]` 且 `d[u][v] ≥ p[i][j]`」的子图上按候选 ID 升序取第一条路径，
  其最弱边恰为 `p[i][j]`，可复现；
- `ballots`：封榜后公开全部选票，结果可离线复算。

前端在右侧「根因共识盲评」面板建场，点击评审场打开**评审室**弹窗：
「投票」页选择评审人并点击候选构建严格排序、提交 / 覆盖选票、达到最低票数后封榜；
「名次」页展示逐层并列名次与已公开选票；「两两矩阵」页展示 `d` / `p` 矩阵，
点击最强路径矩阵单元格查看该方向的见证路径。评审场只读绑定原版本快照，
**不改写因果图、版本或结论包**。

## 🏗️ 架构与目录

```
.
├── app/
│   ├── main.py       # FastAPI 路由：提交事务、回滚、下游查询、反事实推演、结论包导出/核验、盲评建场/投票/封榜、静态托管
│   ├── graph.py      # 图加载、changeset 校验（成环检测/证据字段）、时间窗传播、证据强度审查、BFS 下游、反事实拓扑推演、快照持久化
│   ├── conclusion.py # 可复算复盘结论包：自包含 payload 收集、规范化 SHA-256 摘要、上传包重建 DAG 重算并逐字段比对（纯只读）
│   ├── review.py     # 根因共识盲评：评审场 CRUD（写事务校验 revision/最低票数）、Schulze 计票（两两偏好/最强路径/逐层并列/确定性见证）
│   ├── database.py   # SQLite 连接、表结构、v1 示例数据
│   └── schemas.py    # Pydantic 请求/响应模型
├── static/
│   ├── index.html    # 三栏布局：顶栏 / 画布 / 侧栏 + 评审室弹窗
│   ├── style.css
│   └── app.js        # 状态管理、SVG 渲染、拖拽/连线、changeset 累计与提交、结论包导出下载与核验展示、盲评建场/投票/封榜与矩阵视图
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

### 数据模型（版本即快照）

- `versions(id, message, kind[commit|rollback], base_id, created_at)`
- `nodes(id, version_id, type, title, description, pos_x, pos_y, earliest_time, latest_time,
  reliability, source_group, trigger_mode, created_at)`，主键 `(id, version_id)`；
  `earliest_time` / `latest_time` 为 UTC（`...Z`），仅事件节点非空；
  `reliability`（0~1）/ `source_group` 仅证据节点非空；
  `trigger_mode ∈ {ANY, ALL}` 仅事件节点非空（证据节点恒为 NULL）
- `edges(id, version_id, source_id, target_id, kind, label, created_at)`，主键 `(id, version_id)`；
  `kind ∈ {causes, supports, refutes}`
- 根因共识盲评（只读写以下表，不触碰上面的图数据）：
  - `review_sessions(id, title, version_id→versions, min_votes, status[open|closed],
    revision, created_at, closed_at)` —— `revision` 即 `session_revision`，每次投票 / 封榜自增
  - `review_candidates(session_id, node_id)`、`review_reviewers(session_id, reviewer)`
  - `review_ballots(session_id, reviewer, ranking, submitted_at)`，主键 `(session_id, reviewer)`；
    `ranking` 为 JSON 数组（全部候选的严格排序），重复提交即覆盖

最新版本 = `MAX(versions.id)`；读取图时只查该版本的行。回滚不修改任何旧行，而是把目标版本的
nodes/edges 复制到一条新的 rollback 版本中，因此历史始终可追溯。旧版数据库启动时会幂等
`ALTER TABLE` 补齐时间列、证据强度列与 `trigger_mode` 列（历史事件行回填默认 `ANY`），并重建
edges 表把 CHECK 约束扩展到 `refutes`（历史行原样复制）。

### 事务校验流程（`graph.apply_changes`）

1. 批次内操作 id 去重检查
2. 删节点（级联删除关联边）→ 删边 → 更新节点 → 建节点 → 更新边 → 建边
3. 建边时校验：端点存在、非自环、端点类型符合 `causes/supports/refutes` 语义、无同类型平行边；
   更新边时仅允许 `supports ↔ refutes` 立场切换（`causes` 不可改类型）
4. 在应用后**的整张图**上跑 DFS 三色标记，发现回边即返回环路径并抛错
5. 在「事件 + causes」子图上做拓扑传播：祖先下界取 max、后代上界取 min；发现某节点
   `effective_lower > effective_upper` 即构造两端来源节点 + 因果链并抛 `time_conflict`
6. 全部通过后才在同一事务内插入新版本与快照行；任一步失败 → `ROLLBACK`

### 证据强度计算（`graph.compute_evidence_review`）

1. 收集每个事件的 `supports` / `refutes` 入边，按 `source_group` 分桶；未分组证据各自独立成桶
2. 每桶每方向取 `reliability` 最高者入选（并列取较小节点 id，保证确定），其余记为同源未计权
3. 各入选桶按 `S = 1 − ∏(1−r)`、`R = 1 − ∏(1−r)` 聚合（保留 4 位小数）
4. 依次判定：`S≥0.6 且 R≥0.6` → `disputed`；`S≥0.7` → `supported`；`R≥0.7` → `refuted`；否则 `insufficient`
5. 该函数为纯函数：查询当前版或历史快照时按该版本的 nodes/edges 现算，不写库、不改旧版

### 反事实推演（`graph.simulate_counterfactual`）

1. 只取「事件 + `causes`」子图，前因列表按节点 id 升序；Kahn 拓扑排序（初始队列按 id 排序）保证结果可复现
2. 用户勾选的干预点强制 `prevented`（`intervention`）；其余事件：无前因 → `still_active`
   （`no_causes`，即默认发生）；ANY 门看是否存在仍发生前因；ALL 门看是否存在被阻止前因
3. 依据树递归到前因：ANY 发生取 id 最小的仍发生前因、阻止时列全部；
   ALL 发生列全部、阻止时取 id 最小的已阻止前因；干预点与无前因事件不展开
4. 纯只读：不写库、不产生 changeset / 版本；前端对未提交工作图用同构 JS 引擎即时推演

### 可复算复盘结论包（`conclusion.build_package` / `conclusion.verify_package`）

1. **收集范围**：从根事件沿 `causes` 边反向遍历得到全部祖先事件；包内连线 = 祖先锥内的
   `causes` 边 + 指向这些事件的 `supports` / `refutes` 边（证据节点随边纳入）
2. **自包含结果**：有效时间窗与证据审查在**包内子图**上重算（与 `/api/state` 同一套
   `compute_time_windows` / `compute_evidence_review`），节点 / 连线先按 id 排序再计算，
   保证导出端与核验端遍历顺序一致、结果可逐字节复现
3. **摘要**：`digest.value = SHA-256(紧凑 UTF-8 JSON(payload))` —— 对象键按字典序、无空格分隔符，
   整数值浮点规范化为整数（`60.0` → `60`，与 jq / JavaScript 的序列化一致）；
   导出文件本身也是键按字典序、数字规范化后的 JSON
4. **核验**：重新规范化 `payload` 并比对 digest → 仅用包内 `nodes` / `edges` 重建内存 DAG
   （不接触数据库）→ 重算结果与 `payload.results` 递归逐字段比对，差异以 JSON Pointer 报告
   （`~0` / `~1` 转义，缺失侧为 `null`，数字 `1` 与 `1.0` 视为相等，最多列 200 条）
5. 两个方向都纯只读：不写库、不生成版本、不改变工作图

### 根因共识盲评（`review.create_session` / `submit_ballot` / `close_session` / `schulze_result`）

1. **建场**：校验版本存在、候选为 3~20 个不重复的该版本事件节点、评审人去空白后唯一、
   `1 ≤ min_votes ≤ 评审人数`；一个 `BEGIN IMMEDIATE` 事务写入 3 张表，`revision` 从 1 开始
2. **投票**：写事务内校验评审场开放、评审人已登记、排序恰为全部候选的一个排列；
   `INSERT ... ON CONFLICT DO UPDATE` 覆盖本人选票，`revision` 自增；封榜后一律 409
3. **封榜**：写事务内依次校验 `status = open`（否则 409 `already_closed`）、
   `expected_revision = revision`（否则 409 `stale_revision`）、票数 ≥ `min_votes`
   （否则 409 `min_votes_not_met`），然后置 `closed` 并自增 `revision` —— 此后投票接口拒绝，
   选票即冻结
4. **计票**（纯函数，读取时按冻结选票重算）：`d[i][j]` 为两两偏好计数；最强路径
   `p[i][j]` 只保留 `d[i][j] > d[j][i]` 的有向边，用 Floyd-Warshall 变体取「最弱一环」的
   最大值；每轮取出不被其余候选击败的集合作为同一并列层（Schulze 胜出关系是严格偏序，
   该集合必然非空）；候选 ID 仅用于矩阵 / 层内稳定排序与见证路径的确定性选择，不打断并列
5. **见证**：对任意 `p[i][j] > 0` 的方向，在「`d[u][v] > d[v][u]` 且 `d[u][v] ≥ p[i][j]`」
   的子图上按候选 ID 升序 DFS 取第一条路径 —— 其最弱边恰为 `p[i][j]`，确定性可复现

## 📌 说明

- 节点 / 连线 id 由客户端生成（`[A-Za-z0-9_-]+`），便于一次批次中同时「创建并连边」。
- 并发安全：提交使用 `BEGIN IMMEDIATE` 写锁 + 基准版本乐观校验，后提交者会收到 `stale_version` 错误。
