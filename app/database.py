"""SQLite 连接管理、表结构与初始示例数据。

版本化设计：
- versions 记录每次成功提交 / 回滚产生的快照元数据；
- nodes / edges 每行都带 version_id，即"该快照中存在的元素"。
读取当前图时只查最大 version_id 的行；回滚 = 把旧快照的行复制为新版本。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DB_PATH = os.environ.get("DB_PATH", str(Path(__file__).resolve().parent.parent / "data" / "app.db"))

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message     TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('commit', 'rollback')),
    base_id     INTEGER REFERENCES versions(id),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS nodes (
    id             TEXT NOT NULL,
    version_id     INTEGER NOT NULL REFERENCES versions(id),
    type           TEXT NOT NULL CHECK (type IN ('event', 'evidence')),
    title          TEXT NOT NULL,
    description    TEXT NOT NULL DEFAULT '',
    pos_x          REAL NOT NULL,
    pos_y          REAL NOT NULL,
    earliest_time  TEXT,   -- 事件最早发生时间（UTC，...Z）；证据节点恒为 NULL
    latest_time    TEXT,   -- 事件最晚发生时间（UTC，...Z）
    reliability    REAL,   -- 证据可靠度（0~1）；仅证据节点；旧快照可为 NULL
    source_group   TEXT,   -- 证据来源组；同组每方向只计最高可靠度一条
    trigger_mode   TEXT,   -- 事件触发模式 ANY（任一前因）/ ALL（全部前因）；仅事件节点非空，证据恒为 NULL
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (id, version_id),
    CHECK (type = 'evidence' OR trigger_mode IN ('ANY', 'ALL'))
);

CREATE TABLE IF NOT EXISTS edges (
    id          TEXT NOT NULL,
    version_id  INTEGER NOT NULL REFERENCES versions(id),
    source_id   TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('causes', 'supports', 'refutes')),
    label       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (id, version_id)
);

CREATE INDEX IF NOT EXISTS idx_nodes_version  ON nodes(version_id);
CREATE INDEX IF NOT EXISTS idx_edges_version  ON edges(version_id);

-- 根因共识盲评：评审场绑定某个不可变版本快照，只读写以下 review_* 表，
-- 不改写因果图、版本或结论包。
CREATE TABLE IF NOT EXISTS review_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    version_id  INTEGER NOT NULL REFERENCES versions(id),
    min_votes   INTEGER NOT NULL CHECK (min_votes >= 1),
    status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    revision    INTEGER NOT NULL DEFAULT 1,   -- session_revision：每次投票 / 封榜自增
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    closed_at   TEXT
);

CREATE TABLE IF NOT EXISTS review_candidates (
    session_id  INTEGER NOT NULL REFERENCES review_sessions(id),
    node_id     TEXT NOT NULL,
    PRIMARY KEY (session_id, node_id)
);

CREATE TABLE IF NOT EXISTS review_reviewers (
    session_id  INTEGER NOT NULL REFERENCES review_sessions(id),
    reviewer    TEXT NOT NULL,
    PRIMARY KEY (session_id, reviewer)
);

CREATE TABLE IF NOT EXISTS review_ballots (
    session_id   INTEGER NOT NULL REFERENCES review_sessions(id),
    reviewer     TEXT NOT NULL,
    ranking      TEXT NOT NULL CHECK (json_valid(ranking)),  -- JSON 数组：全部候选的严格排序
    submitted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (session_id, reviewer)
);
"""

# (id, type, title, desc, x, y, earliest_time_utc, latest_time_utc, reliability, source_group, trigger_mode)
# 反事实示例：n-null 为 ALL 门（需「错误版本发布」与「配置开关失效」同时发生），其余事件为默认 ANY 门；
# 在沙盘中阻止任一前因即可看到 n-null 及下游被阻止。
SEED_NODES = [
    ("n-code", "event", "错误版本发布", "未灰度的构建被全量推送", 60, 200,
     "2026-09-20T02:00:30Z", "2026-09-20T02:01:00Z", None, None, "ANY"),
    ("n-config", "event", "配置开关失效", "特性开关默认值写成了 true", 60, 400,
     "2026-09-20T01:59:30Z", "2026-09-20T02:00:00Z", None, None, "ANY"),
    ("n-null", "event", "空指针异常", "支付服务读取到空的用户配置（需错误版本 + 开关失效同时成立）", 360, 300,
     "2026-09-20T02:00:00Z", "2026-09-20T02:01:30Z", None, None, "ALL"),
    ("n-outage", "event", "支付链路中断", "约 22 分钟无法下单", 660, 300,
     None, None, None, None, "ANY"),
    ("n-loss", "event", "订单损失", "峰值时段约 1.2 万笔失败", 960, 300,
     "2026-09-20T02:01:00Z", "2026-09-20T02:25:00Z", None, None, "ANY"),
    # 证据强度示例：e-log 与 e-metric 同属「监控平台」，在支持方向只有前者计权；
    # e-dissent 独立组反驳 n-null，使该事件成为 disputed；e-review 反驳 n-config。
    ("e-log", "evidence", "监控日志 E-01", "NullPointerException @ PaymentService:188，22 分钟", 360, 80,
     None, None, 0.9, "监控平台", None),
    ("e-deploy", "evidence", "发布单 R-4471", "构建时间与故障开始时间相差 40 秒", 60, 40,
     None, None, 0.75, "发布系统", None),
    ("e-metric", "evidence", "错误率指标 M-22", "错误率抬升与日志告警时间一致（与 E-01 同源）", 540, 70,
     None, None, 0.8, "监控平台", None),
    ("e-dissent", "evidence", "复盘异议 D-03", "部分失败工单早于 NPE，质疑空指针是唯一直接原因", 560, 200,
     None, None, 0.7, "内部复盘", None),
    ("e-review", "evidence", "评审记录 Q-09", "该发布未改动特性开关，开关默认值一直为 true", 60, 560,
     None, None, 0.8, "变更评审", None),
]

SEED_EDGES = [
    ("e1", "n-code", "n-null", "causes", "引入空配置路径"),
    ("e2", "n-config", "n-null", "causes", "开关误开新代码"),
    ("e3", "n-null", "n-outage", "causes", ""),
    ("e4", "n-outage", "n-loss", "causes", ""),
    ("e5", "e-log", "n-null", "supports", "时间线吻合"),
    ("e6", "e-deploy", "n-code", "supports", "发布时间线"),
    ("e7", "e-metric", "n-null", "supports", "同源指标互证（被同源去重）"),
    ("e8", "e-dissent", "n-null", "refutes", "质疑唯一直接原因"),
    ("e9", "e-review", "n-config", "refutes", "开关非本次发布改动"),
]


def connect() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """建表；为旧库补齐证据强度列 / refutes 连线类型（幂等）；versions 为空时写入 v1 示例快照。"""
    conn.executescript(SCHEMA)
    _migrate_time_columns(conn)
    _migrate_evidence_columns(conn)
    _migrate_trigger_mode(conn)
    _migrate_edge_kinds(conn)
    count = conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0]
    if count == 0:
        cur = conn.execute(
            "INSERT INTO versions (message, kind, base_id) VALUES (?, 'commit', NULL)",
            ("初始示例：支付链路中断复盘",),
        )
        vid = cur.lastrowid
        conn.executemany(
            "INSERT INTO nodes (id, version_id, type, title, description, pos_x, pos_y, "
            "earliest_time, latest_time, reliability, source_group, trigger_mode) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(nid, vid, t, title, desc, x, y, earliest, latest, rel, grp, trig)
             for nid, t, title, desc, x, y, earliest, latest, rel, grp, trig in SEED_NODES],
        )
        conn.executemany(
            "INSERT INTO edges (id, version_id, source_id, target_id, kind, label) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(eid, vid, s, t, k, label) for eid, s, t, k, label in SEED_EDGES],
        )


def _migrate_time_columns(conn: sqlite3.Connection) -> None:
    """旧版本数据库没有 earliest_time / latest_time 列，幂等补齐（历史行留空）。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(nodes)")}
    if "earliest_time" not in cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN earliest_time TEXT")
    if "latest_time" not in cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN latest_time TEXT")


def _migrate_evidence_columns(conn: sqlite3.Connection) -> None:
    """旧库没有 reliability / source_group 列，幂等补齐（历史行留空，计权时按 0 处理）。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(nodes)")}
    if "reliability" not in cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN reliability REAL")
    if "source_group" not in cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN source_group TEXT")


def _migrate_trigger_mode(conn: sqlite3.Connection) -> None:
    """旧库没有 trigger_mode 列：幂等补齐；事件行回填默认 ANY，证据行恒为 NULL。

    新表带 CHECK（证据节点可为 NULL，事件节点必须为 ANY/ALL），
    因此回填必须先于「新建表直接满足约束」的全新库路径之外的旧库迁移场景。
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(nodes)")}
    if "trigger_mode" not in cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN trigger_mode TEXT")
        conn.execute("UPDATE nodes SET trigger_mode = 'ANY' WHERE type = 'event'")


def _migrate_edge_kinds(conn: sqlite3.Connection) -> None:
    """旧库 edges.kind 的 CHECK 不含 'refutes'；SQLite 不能改 CHECK，按官方方式重建表。

    幂等：检测建表 SQL 中已含 'refutes' 则跳过；历史行原样复制，不改写任何数据。
    """
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='edges'").fetchone()
    if row is None or "'refutes'" in (row["sql"] or ""):
        return
    conn.executescript("""
        CREATE TABLE edges_new (
            id          TEXT NOT NULL,
            version_id  INTEGER NOT NULL REFERENCES versions(id),
            source_id   TEXT NOT NULL,
            target_id   TEXT NOT NULL,
            kind        TEXT NOT NULL CHECK (kind IN ('causes', 'supports', 'refutes')),
            label       TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            PRIMARY KEY (id, version_id)
        );
        INSERT INTO edges_new (id, version_id, source_id, target_id, kind, label, created_at)
            SELECT id, version_id, source_id, target_id, kind, label, created_at FROM edges;
        DROP TABLE edges;
        ALTER TABLE edges_new RENAME TO edges;
        CREATE INDEX IF NOT EXISTS idx_edges_version ON edges(version_id);
    """)
