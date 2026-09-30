"""变更提案合并：绑定 base_version 的提案保存，与基线 / 提案 A / 提案 B 三方合并。

流程：
- 保存：把一组未提交修改（changeset）绑定到某个不可变基线版本；保存时在**该基线**上
  复用 graph.apply_changes 做整图校验（成环 / 时间窗 / 字段语义），通过即落库，
  **不生成版本**；提案可以在最新版本继续前进时仍然保留其基线语义。
- 预览：选择两份「同基线且未合并」的提案后做三方比较：
  * 单侧修改 / 双方相同修改 → 直接采纳（auto）；
  * 同一字段 / 连线被改成不同值、删除与修改相遇 → 列出 conflict，逐项由用户在
    A / B / 基线之间裁决；冲突未裁决完不得提交合并。
- 合并：服务端在 SQLite 写事务（BEGIN IMMEDIATE）中**重新合并**：再次确认两份提案
  仍未使用、且当前版本仍等于 base_version，按裁决重建 changeset 并复用整图校验；
  全部通过才生成一个 merge 版本并同时标记两份提案已合并，任一失败都整体回滚 ——
  不留版本、不留已合并状态。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.graph import (
    Edge,
    Graph,
    GraphError,
    Node,
    apply_changes,
    compute_time_windows,
    find_cycle,
    load_graph,
    persist_graph,
    _conflict_message,
)
from app.schemas import Changeset, to_utc_iso

# ---------- 实体投影：把内存 Node / Edge 投影为可比较 / 可持久化的字段字典 ----------

_NODE_FIELDS = (
    "type", "title", "description", "pos_x", "pos_y",
    "earliest_time", "latest_time", "reliability", "source_group", "trigger_mode",
)
_EDGE_FIELDS = ("source_id", "target_id", "kind", "label")


def node_view(n: Node) -> dict[str, Any]:
    return {
        "id": n.id, "type": n.type, "title": n.title, "description": n.description,
        "pos_x": n.pos_x, "pos_y": n.pos_y,
        "earliest_time": n.earliest_time, "latest_time": n.latest_time,
        "reliability": n.reliability, "source_group": n.source_group,
        "trigger_mode": n.trigger_mode,
    }


def edge_view(e: Edge) -> dict[str, Any]:
    return {"id": e.id, "source_id": e.source_id, "target_id": e.target_id,
            "kind": e.kind, "label": e.label}


# ---------- 提案存取 ----------

def changeset_to_json(changes: Changeset) -> str:
    """把 Changeset 序列化为紧凑 JSON：只写显式提供的字段（含显式 null，用于清除）。

    时间统一换算为 UTC `...Z`（与 apply_changes 存储口径一致），保证提案落库 JSON
    可被任意工具直接阅读、且重新加载时与原修改语义完全一致。
    """
    def dump_create(c) -> dict[str, Any]:
        d = c.model_dump(exclude_none=True)
        for tf in ("earliest_time", "latest_time"):
            if tf in d and d[tf] is not None:
                d[tf] = to_utc_iso(d[tf])
        return d

    def dump_update(u, time_fields: bool) -> dict[str, Any]:
        d = {"id": u.id, **u.model_dump(exclude={"id"}, exclude_unset=True)}
        if time_fields:
            for tf in ("earliest_time", "latest_time"):
                if tf in d:
                    d[tf] = to_utc_iso(d[tf]) if d[tf] is not None else None
        return d

    data = {
        "base_version_id": changes.base_version_id,
        "node_creates": [dump_create(c) for c in changes.node_creates],
        "node_updates": [dump_update(u, time_fields=True) for u in changes.node_updates],
        "node_deletes": list(changes.node_deletes),
        "edge_creates": [dump_create(c) for c in changes.edge_creates],
        "edge_updates": [dump_update(u, time_fields=False) for u in changes.edge_updates],
        "edge_deletes": list(changes.edge_deletes),
    }
    return json.dumps(data, ensure_ascii=False)


def json_to_changeset(raw: str | dict[str, Any]) -> Changeset:
    """把存储的 JSON 还原为 Changeset；datetime 串由 Pydantic 重新解析（必须带时区）。"""
    data = json.loads(raw) if isinstance(raw, str) else raw
    return Changeset.model_validate(data)


def _counts(changes: Changeset) -> dict[str, int]:
    return {
        "node_creates": len(changes.node_creates),
        "node_updates": len(changes.node_updates),
        "node_deletes": len(changes.node_deletes),
        "edge_creates": len(changes.edge_creates),
        "edge_updates": len(changes.edge_updates),
        "edge_deletes": len(changes.edge_deletes),
    }


def save_proposal(conn: sqlite3.Connection, *, title: str, changes: Changeset) -> dict[str, Any]:
    """保存提案：绑定 base_version；在该基线上复用整图校验，但不生成版本。"""
    title = (title or "").strip()
    if not title:
        raise GraphError("提案标题不能为空", "invalid_title")
    if not any([changes.node_creates, changes.node_updates, changes.node_deletes,
                changes.edge_creates, changes.edge_updates, changes.edge_deletes]):
        raise GraphError("没有任何可保存的修改", "empty_changeset")

    base = load_graph(conn, changes.base_version_id)
    if base is None:
        raise GraphError(f"基线版本不存在：{changes.base_version_id}", "version_not_found")
    # 只在对应基线上校验（不要求该基线是当前最新版本）；复用现有整图校验，失败不落库
    apply_changes(base, changes)

    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT INTO change_proposals (title, base_version, changes_json) VALUES (?, ?, ?)",
            (title, changes.base_version_id, changeset_to_json(changes)),
        )
        pid = cur.lastrowid
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_proposal(conn, pid)


def _row_or_404(conn: sqlite3.Connection, proposal_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM change_proposals WHERE id = ?", (proposal_id,)).fetchone()
    if row is None:
        raise GraphError(f"提案不存在：{proposal_id}", "proposal_not_found")
    return row


def _summary(row: sqlite3.Row) -> dict[str, Any]:
    changes = json_to_changeset(row["changes_json"])
    return {
        "id": row["id"],
        "title": row["title"],
        "base_version_id": row["base_version"],
        "status": row["status"],
        "merged_into": row["merged_into"],
        "created_at": row["created_at"],
        "merged_at": row["merged_at"],
        "counts": _counts(changes),
    }


def list_proposals(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute("SELECT * FROM change_proposals ORDER BY id DESC").fetchall()
    current = conn.execute("SELECT MAX(id) FROM versions").fetchone()[0]
    return {"current_version_id": current, "proposals": [_summary(r) for r in rows]}


def get_proposal(conn: sqlite3.Connection, proposal_id: int) -> dict[str, Any]:
    row = _row_or_404(conn, proposal_id)
    out = _summary(row)
    out["changes"] = json.loads(row["changes_json"])
    return out


def preview_proposal_graph(conn: sqlite3.Connection, proposal_id: int) -> Graph:
    """只读：把提案应用到其基线，返回预览图（不生成版本、不写库）。"""
    row = _row_or_404(conn, proposal_id)
    changes = json_to_changeset(row["changes_json"])
    base = load_graph(conn, row["base_version"])
    if base is None:
        raise GraphError(f"基线版本不存在：{row['base_version']}", "version_not_found")
    nodes, edges = apply_changes(base, changes)
    return Graph(row["base_version"], nodes, edges)


# ---------- 三方比较（基线 / A / B） ----------

def _project(base: Graph, changes: Changeset) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """把单个提案应用到基线（该提案保存时已校验通过），投影为节点 / 连线字段字典。"""
    nodes, edges = apply_changes(base, changes)
    return ({nid: node_view(n) for nid, n in nodes.items()},
            {eid: edge_view(e) for eid, e in edges.items()})


def _record(id_: str, kind: str, *, conflict_type: str,
            conflict_fields: list[dict[str, Any]],
            auto_entity: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "id": id_,
        "kind": kind,                       # node | edge
        "conflict_type": conflict_type,     # none | fields | create_create | delete_modify
        "conflict_fields": conflict_fields,
        "auto_entity": auto_entity,         # 自动采纳时组成的实体（无冲突）；含 action
    }


def _auto_record(id_: str, kind: str, action: str,
                 entity: dict[str, Any] | None, *, dependent_edge_ids: list[str] | None = None
                 ) -> dict[str, Any]:
    rec = _record(id_, kind, conflict_type="none", conflict_fields=[],
                  auto_entity=dict(entity or {}, action=action))
    if dependent_edge_ids is not None:
        rec["dependent_edge_ids"] = dependent_edge_ids
    return rec


def _field_conflict(kind: str, field: str, base_v: Any, a_v: Any, b_v: Any,
                    *, base_available: bool) -> dict[str, Any]:
    return {
        "kind": kind, "field": field,
        "base": base_v, "a": a_v, "b": b_v,
        # 「两边各新建」时基线侧没有该实体，不允许选基线
        "resolutions": ["a", "b"] + (["base"] if base_available else []),
    }


def _field_conflicts(kind: str, fields: tuple[str, ...], base_e: dict[str, Any] | None,
                     a_e: dict[str, Any], b_e: dict[str, Any], *, base_available: bool
                     ) -> list[dict[str, Any]]:
    """逐字段三方比较：仅当 A、B 都改动了同一字段且改成不同值时才算冲突。

    - 基线存在：单侧修改（另一侧保持基线）与双方相同修改都不冲突（由调用方自动采纳）；
    - 基线不存在（双方新建）：任一字段取值不同即为冲突。
    """
    out: list[dict[str, Any]] = []
    for f in fields:
        bv = base_e.get(f) if base_e is not None else None
        av, bvv = a_e.get(f), b_e.get(f)
        conflict = (av != bvv) if base_e is None else (av != bv and bvv != bv and av != bvv)
        if conflict:
            out.append(_field_conflict(kind, f, bv, av, bvv, base_available=base_available))
    return out


def _merged_entity(fields: tuple[str, ...], base_e: dict[str, Any] | None,
                   a_e: dict[str, Any], b_e: dict[str, Any],
                   diffs: list[dict[str, Any]]) -> dict[str, Any]:
    """自动合并后的实体：逐字段取「单侧修改 / 双方相同修改」，冲突字段回退为基线占位。"""
    diff_fields = {d["field"] for d in diffs}
    if base_e is None:
        # 双方新建：非冲突字段在两侧必然相等（否则已列入冲突），取 A 即可
        merged = {f: a_e.get(f) for f in fields}
    else:
        merged = dict(base_e)
        for f in fields:
            bv = base_e.get(f)
            if a_e.get(f) != bv:
                merged[f] = a_e.get(f)
            elif b_e.get(f) != bv:
                merged[f] = b_e.get(f)
    merged["id"] = (a_e.get("id") or b_e.get("id"))
    for f in diff_fields:
        merged.pop(f, None)  # 冲突字段在 auto 预览中缺席（仅用于展示），真正结果等待裁决
    return merged


def _merge_entity_record(eid: str, kind: str, fields: tuple[str, ...],
                         base_e: dict[str, Any] | None,
                         a_e: dict[str, Any] | None, b_e: dict[str, Any] | None
                         ) -> dict[str, Any]:
    """对单个实体做三方比较，返回 record（auto_entity 或 conflict 明细）。"""
    in_base, in_a, in_b = base_e is not None, a_e is not None, b_e is not None

    # 双方都不存在：无操作
    if not in_a and not in_b:
        return _record(eid, kind, conflict_type="none", conflict_fields=[], auto_entity=None)

    # 双方都存在
    if in_a and in_b:
        if in_base:
            diffs = _field_conflicts(kind, fields, base_e, a_e, b_e, base_available=True)
            if not diffs:
                # 任一侧对基线的非冲突修改都直接采纳：优先取改动过的一侧（双方相同修改等价）
                changed_a = _entity_diverges(fields, base_e, a_e)
                changed_b = _entity_diverges(fields, base_e, b_e)
                changed_e = a_e if changed_a else (b_e if changed_b else a_e)
                action = "update" if (changed_a or changed_b) else "keep"
                return _auto_record(eid, kind, action, changed_e)
            # 有字段级冲突：未冲突字段仍可自动合并（单侧 / 双方相同修改直接采纳）
            merged = _merged_entity(fields, base_e, a_e, b_e, diffs)
            rec = _record(eid, kind, conflict_type="fields",
                          conflict_fields=diffs, auto_entity=dict(merged, action="partial"))
            rec["base_entity"] = base_e
            rec["a_entity"] = a_e
            rec["b_entity"] = b_e
            return rec
        # 双方都新建（基线没有）
        diffs = _field_conflicts(kind, fields, None, a_e, b_e, base_available=False)
        if not diffs:
            return _auto_record(eid, kind, "create", a_e)
        merged = _merged_entity(fields, None, a_e, b_e, diffs)
        rec = _record(eid, kind, conflict_type="create_create",
                      conflict_fields=diffs, auto_entity=None)
        rec["base_entity"] = None
        rec["a_entity"] = a_e
        rec["b_entity"] = b_e
        rec["partial_entity"] = merged
        return rec

    # 仅一方存在：existing = 保留下来的一侧；另一侧为「删除（基线有）」或「未新建（基线无）」
    existing = a_e if in_a else b_e
    existing_side = "a" if in_a else "b"
    if not in_base:
        # 基线没有：只有一方新建，另一侧本就没有 → 单侧新建直接采纳
        return _auto_record(eid, kind, "create", existing)
    # 基线存在但只在一侧结果中出现：另一侧删除了该实体
    rec = _record(eid, kind, conflict_type="delete_modify", conflict_fields=[], auto_entity=None)
    rec["base_entity"] = base_e
    rec["a_entity"] = a_e
    rec["b_entity"] = b_e
    rec["delete_side"] = "b" if in_a else "a"
    rec["existing_side"] = existing_side
    return rec


def _entity_diverges(fields: tuple[str, ...], base_e: dict[str, Any], e: dict[str, Any] | None) -> bool:
    if e is None:
        return True  # 删除相对基线总是「分歧」
    return any(base_e.get(f) != e.get(f) for f in fields)


def _build_node_records(base_nodes, a_nodes, b_nodes) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    all_ids = sorted(set(base_nodes) | set(a_nodes) | set(b_nodes))
    for nid in all_ids:
        records[nid] = _merge_entity_record(
            nid, "node", _NODE_FIELDS,
            base_nodes.get(nid), a_nodes.get(nid), b_nodes.get(nid))
    return records


def _edge_state_by_node_delete(eid: str, base_e: dict[str, Any],
                               a_e: dict[str, Any] | None, b_e: dict[str, Any] | None,
                               node_records: dict[str, dict[str, Any]]
                               ) -> tuple[bool, str | None]:
    """该边是否只因「某节点的单方/双方删除」而消失。

    返回 (absorbed, follow_node_id)：
    - absorbed=True：该边不单独列为冲突，跟随相关节点冲突的裁决（节点删除→边消失；
      节点保留→边按未删除一侧 / 基线保留）；
    - 双方都删的节点（auto delete）边直接自动消失。
    """
    endpoints = {base_e["source_id"], base_e["target_id"]} if base_e is not None else set()
    if not endpoints:
        present = a_e or b_e
        endpoints = {present["source_id"], present["target_id"]} if present else set()
    for nid in endpoints:
        nrec = node_records.get(nid)
        if not nrec:
            continue
        auto = nrec.get("auto_entity")
        if auto and auto.get("action") == "delete":
            return True, None  # 双方都删节点：边必然级联消失
        if nrec["conflict_type"] == "delete_modify":
            # 节点一方删除、另一方保留（可能同时修改了该边）：边的去留完全跟随节点裁决，
            # 吸收进节点冲突的 dependent_edge_ids（保留时按保留侧版本，含其字段修改），
            # 不再单独生成一个边冲突要求用户重复裁决
            return True, nid
    return False, None


def _build_edge_records(base_edges, a_edges, b_edges,
                        node_records: dict[str, dict[str, Any]]
                        ) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    all_ids = sorted(set(base_edges) | set(a_edges) | set(b_edges))

    for eid in all_ids:
        be, ae, be2 = base_edges.get(eid), a_edges.get(eid), b_edges.get(eid)
        absorbed, follow_nid = _edge_state_by_node_delete(eid, be, ae, be2, node_records)
        if absorbed:
            if follow_nid is None:
                # 跟随双方删除节点：自动删除（级联），不生成 record
                continue
            # 跟随节点删除冲突：记录到 dependent_edge_ids，并在组成时随节点裁决处理
            deps = node_records[follow_nid].setdefault("dependent_edge_ids", [])
            if eid not in deps:
                deps.append(eid)
            # 节点裁决为「保留」时该边应有的样子：取保留一侧的版本（可能含其字段修改），
            # 保留侧没动过则回退基线
            follow = node_records[follow_nid].setdefault("follow_edges", {})
            delete_side = node_records[follow_nid]["delete_side"]
            kept_edge = be2 if delete_side == "a" else ae
            if kept_edge is not None:
                follow[eid] = dict(kept_edge)
            elif be is not None:
                follow[eid] = dict(be)
            continue
        rec = _merge_entity_record(eid, "edge", _EDGE_FIELDS, be, ae, be2)
        # 「节点删除冲突 vs 关联边被新增」升级到节点冲突（附带受影响边），不悬空
        if rec["conflict_type"] == "none" and rec.get("auto_entity", {}).get("action") == "create":
            ent = rec["auto_entity"]
            for nid in (ent["source_id"], ent["target_id"]):
                nrec = node_records.get(nid)
                if nrec and nrec["conflict_type"] == "delete_modify":
                    deps = nrec.setdefault("dependent_edge_ids", [])
                    if eid not in deps:
                        deps.append(eid)
        records[eid] = rec
    return records


# ---------- 裁决与合并 ----------

def _load_two(conn: sqlite3.Connection, proposal_a_id: int, proposal_b_id: int
              ) -> tuple[sqlite3.Row, sqlite3.Row]:
    if proposal_a_id == proposal_b_id:
        raise GraphError("必须选择两份不同的提案", "same_proposal")
    ra = _row_or_404(conn, proposal_a_id)
    rb = _row_or_404(conn, proposal_b_id)
    for r in (ra, rb):
        if r["status"] != "open":
            raise GraphError(f"提案「{r['title']}」已经合并，不能再次使用", "proposal_already_merged", 409)
    if ra["base_version"] != rb["base_version"]:
        raise GraphError(
            f"两份提案基线不同（v{ra['base_version']} vs v{rb['base_version']}），不能三方合并",
            "different_base")
    return ra, rb


def build_merge_plan(conn: sqlite3.Connection, proposal_a_id: int, proposal_b_id: int) -> dict[str, Any]:
    """只读：计算三方比较结果（自动采纳项 + 冲突清单），并给出「仅自动项」预览的整图校验。"""
    ra, rb = _load_two(conn, proposal_a_id, proposal_b_id)
    base_vid = ra["base_version"]
    base = load_graph(conn, base_vid)
    if base is None:
        raise GraphError(f"基线版本不存在：{base_vid}", "version_not_found")
    ch_a = json_to_changeset(ra["changes_json"])
    ch_b = json_to_changeset(rb["changes_json"])
    a_nodes, a_edges = _project(base, ch_a)
    b_nodes, b_edges = _project(base, ch_b)
    base_nodes = {nid: node_view(n) for nid, n in base.nodes.items()}
    base_edges = {eid: edge_view(e) for eid, e in base.edges.items()}

    node_records = _build_node_records(base_nodes, a_nodes, b_nodes)
    edge_records = _build_edge_records(base_edges, a_edges, b_edges, node_records)

    conflicts = [r for r in (*node_records.values(), *edge_records.values())
                 if r["conflict_type"] != "none"]

    # 「仅自动采纳」预览：裁决前让用户看到已确定部分合在一起是否已经成环 / 时间窗矛盾
    auto_nodes, auto_edges, auto_changes = _compose(
        base, node_records, edge_records, {}, strict=False)
    auto_check = _validate_maps(base, auto_nodes, auto_edges)

    return {
        "proposal_a_id": ra["id"], "proposal_a_title": ra["title"],
        "proposal_b_id": rb["id"], "proposal_b_title": rb["title"],
        "base_version_id": base_vid,
        "conflicts": [_conflict_out(r) for r in conflicts],
        "auto_changes": _changes_summary(auto_changes),
        "auto_preview": auto_check,
    }


def _conflict_out(rec: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": rec["id"],
        "kind": rec["kind"],
        "conflict_type": rec["conflict_type"],
        "conflict_fields": rec["conflict_fields"],
        "delete_side": rec.get("delete_side"),
        "existing_side": rec.get("existing_side"),
        "dependent_edge_ids": rec.get("dependent_edge_ids", []),
        "base_entity": rec.get("base_entity"),
        "a_entity": rec.get("a_entity"),
        "b_entity": rec.get("b_entity"),
    }


def _changes_summary(ch: Changeset) -> dict[str, Any]:
    return {"changes": _counts(ch)}


def _validate_maps(base: Graph, nodes: dict[str, dict[str, Any]],
                   edges: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """在组成的字段字典上直接跑成环 / 时间窗校验（无持久化、无版本）。"""
    node_objs = {nid: Node(
        id=nid, type=v["type"], title=v["title"], description=v["description"],
        pos_x=v["pos_x"], pos_y=v["pos_y"], created_at="",
        earliest_time=v["earliest_time"], latest_time=v["latest_time"],
        reliability=v["reliability"], source_group=v["source_group"],
        trigger_mode=v.get("trigger_mode") or "ANY",
    ) for nid, v in nodes.items()}
    edge_objs = {eid: Edge(eid, v["source_id"], v["target_id"], v["kind"], v["label"], "")
                 for eid, v in edges.items() if v["source_id"] in nodes and v["target_id"] in nodes}

    out: dict[str, Any] = {"ok": True, "code": None, "message": None,
                           "cycle": None, "conflict": None, "graph": None}
    g = Graph(base.version_id, node_objs, edge_objs)
    cycle = find_cycle(node_objs, edge_objs)
    if cycle:
        names = [node_objs[nid].title for nid in cycle if nid in node_objs]
        out.update(ok=False, code="cycle", cycle=cycle,
                   message="自动采纳部分会使图形成环："
                           + " → ".join(names + [names[0]] if names else []),
                   graph=serialize_graph(g))
        return out
    _windows, conflict = compute_time_windows(node_objs, edge_objs)
    if conflict is not None:
        out.update(ok=False, code="time_conflict", conflict=conflict,
                   message=_conflict_message(node_objs, conflict),
                   graph=serialize_graph(g))
    return out


def serialize_graph(g: Graph) -> dict[str, Any]:
    """序列化内存图（结构与 /api/versions/{id} 一致，但不附重算窗口 / 审查）。

    时间窗与证据审查由前端在脏图模式下本地重算（与未提交工作图同一路径）。
    """
    return {
        "version_id": g.version_id,
        "nodes": [node_view(n) for n in g.nodes.values()],
        "edges": [edge_view(e) for e in g.edges.values()],
    }


def _field_choice(resolutions: dict[str, str], eid: str, field: str,
                  entity_choice: str | None) -> str | None:
    """某字段的裁决：优先逐字段键 `<id>:<field>`，否则回退实体级键 `<id>`。"""
    return resolutions.get(f"{eid}:{field}", entity_choice)


def _resolve_entity(rec: dict[str, Any], choice: str | None, *, strict: bool,
                    resolutions: dict[str, str] | None = None
                    ) -> tuple[dict[str, Any] | None, bool]:
    """返回 (entity_or_None, was_unresolved)。

    - 无冲突：直接返回 auto_entity；
    - fields：未冲突字段按三方结果自动合并；每个冲突字段可用 `<id>:<field>` 逐字段裁决，
      未提供逐字段键时回退到实体级选择 `<id>`；
    - create_create：同上，但字段级裁决只能取 a/b；
    - delete_modify：实体级选择，删除侧 → None，保留侧 / 基线 → 对应完整实体。
    """
    resolutions = resolutions or {}
    ctype = rec["conflict_type"]
    eid = rec["id"]
    if ctype == "none":
        return rec.get("auto_entity"), False

    if ctype in ("fields", "create_create"):
        field_sides = {f["field"]: _field_choice(resolutions, eid, f["field"], choice)
                       for f in rec["conflict_fields"]}
        missing = [f for f, s in field_sides.items() if s is None]
        if missing:
            if strict:
                raise GraphError(
                    f"冲突 {eid} 还有字段未裁决：{', '.join(missing)}", "unresolved_conflict")
            return None, True
        if any(s not in ("a", "b", "base") for s in field_sides.values()):
            raise GraphError(f"冲突 {eid} 的裁决非法", "invalid_resolution")
        if ctype == "create_create" and any(s == "base" for s in field_sides.values()):
            raise GraphError(f"冲突 {eid} 不能选择基线（双方都是新建）", "invalid_resolution")
        base = rec.get("auto_entity") if ctype == "fields" else rec.get("partial_entity")
        merged = {k: v for k, v in (base or {}).items() if k != "action"}
        for fld in rec["conflict_fields"]:
            side = field_sides[fld["field"]]
            src = {"a": rec["a_entity"], "b": rec["b_entity"], "base": rec.get("base_entity")}[side]
            merged[fld["field"]] = src.get(fld["field"]) if src is not None else None
        merged["id"] = eid
        return merged, False

    # delete_modify
    if choice is None:
        if strict:
            raise GraphError(f"冲突 {eid} 缺少裁决", "unresolved_conflict")
        return None, True
    if choice not in ("a", "b", "base"):
        raise GraphError(f"冲突 {eid} 的裁决非法：{choice}", "invalid_resolution")
    src = {"a": rec.get("a_entity"), "b": rec.get("b_entity"), "base": rec.get("base_entity")}[choice]
    return (dict(src) if src is not None else None), False


def _compose(base: Graph, node_records: dict[str, dict[str, Any]],
             edge_records: dict[str, dict[str, Any]],
             resolutions: dict[str, str], *, strict: bool
             ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], Changeset]:
    """按裁决把所有实体组成最终字段字典，再对基线求差生成一个 changeset。

    strict=False（预览）：未裁决的 fields 冲突按基线占位、delete_modify 按保留基线、
    create_create 缺席处理；strict=True（真正合并）：任何未裁决都抛错。
    """
    chosen_nodes: dict[str, dict[str, Any]] = {}
    chosen_edges: dict[str, dict[str, Any]] = {}
    unresolved: list[str] = []

    def place(records: dict[str, dict[str, Any]], chosen: dict[str, dict[str, Any]], is_edge: bool):
        for eid, rec in records.items():
            choice = resolutions.get(eid)
            try:
                ent, unresolved_here = _resolve_entity(
                    rec, choice, strict=strict, resolutions=resolutions)
            except GraphError:
                if strict:
                    raise
                unresolved_here = True
                ent = None
            if unresolved_here:
                unresolved.append(eid)
                # 预览占位：fields 冲突以「未冲突字段 + 冲突字段保持基线」呈现（新建实体缺席）；
                # delete_modify 保留基线
                base_ent = rec.get("base_entity")
                if rec["conflict_type"] == "fields":
                    ent = {k: v for k, v in rec["auto_entity"].items() if k != "action"}
                    for fld in rec["conflict_fields"]:
                        ent[fld["field"]] = base_ent.get(fld["field"])
                    ent["id"] = eid
                elif rec["conflict_type"] == "create_create":
                    ent = None  # 双方新建冲突在自动预览中不放入
                elif base_ent is not None:
                    ent = dict(base_ent)
            if ent is None:
                continue
            if is_edge:
                missing = [p for p in (ent["source_id"], ent["target_id"]) if p not in chosen_nodes]
                if missing:
                    if strict:
                        # 真正合并：边的端点被裁决删除 → 悬空端点，整图校验拒绝（事务回滚）
                        raise GraphError(
                            f"连线 {eid} 的端点 {', '.join(missing)} 已被裁决删除，"
                            "该连线无法保留（请改判保留相关节点，或放弃该连线所在提案）",
                            "dangling_after_resolution")
                    # 预览：不放入，避免悬空
                    continue
            chosen[eid] = ent

    place(node_records, chosen_nodes, is_edge=False)

    # 跟随节点删除冲突的边：节点保留 → 恢复这些边（保留侧的版本）；节点删除 → 不放入（级联消失）
    for nid, rec in node_records.items():
        if nid in chosen_nodes:
            for eid, edge_ent in (rec.get("follow_edges") or {}).items():
                chosen_edges.setdefault(eid, dict(edge_ent))

    place(edge_records, chosen_edges, is_edge=True)

    if strict and unresolved:
        raise GraphError(
            f"还有 {len(set(unresolved))} 个冲突未裁决，不能合并：{', '.join(sorted(set(unresolved)))}",
            "unresolved_conflict",
        )

    changes = _maps_to_changes(base, chosen_nodes, chosen_edges)
    return chosen_nodes, chosen_edges, changes


def _maps_to_changes(base: Graph, nodes: dict[str, dict[str, Any]],
                     edges: dict[str, dict[str, Any]]) -> Changeset:
    """对基线求差，生成等价 changeset（随后统一走 apply_changes 的全套校验）。"""
    base_nodes = {nid: node_view(n) for nid, n in base.nodes.items()}
    base_edges = {eid: edge_view(e) for eid, e in base.edges.items()}

def _normalize_node_for_type(m: dict[str, Any]) -> dict[str, Any]:
    """按节点类型清掉对方类型才允许的字段（投影里这些字段为 None，求差时不能误判为修改）。"""
    out = dict(m)
    if m.get("type") == "event":
        out["reliability"] = None
        out["source_group"] = None
        out["trigger_mode"] = m.get("trigger_mode") or "ANY"
    else:  # evidence
        out["earliest_time"] = None
        out["latest_time"] = None
        out["trigger_mode"] = None
    return out


def _maps_to_changes(base: Graph, nodes: dict[str, dict[str, Any]],
                     edges: dict[str, dict[str, Any]]) -> Changeset:
    """对基线求差，生成等价 changeset（随后统一走 apply_changes 的全套校验）。"""
    base_nodes = {nid: node_view(n) for nid, n in base.nodes.items()}
    base_edges = {eid: edge_view(e) for eid, e in base.edges.items()}

    node_creates, node_updates, node_deletes = [], [], []
    for nid in sorted(set(base_nodes) | set(nodes)):
        b, m = base_nodes.get(nid), nodes.get(nid)
        if b is None and m is not None:
            m = _normalize_node_for_type(m)
            node_creates.append({k: m[k] for k in ("id", *_NODE_FIELDS)})
        elif b is not None and m is None:
            node_deletes.append(nid)
        elif b is not None and m is not None:
            b = _normalize_node_for_type(b)
            m = _normalize_node_for_type(m)
            patch = {"id": nid}
            for f in _NODE_FIELDS:
                if b.get(f) != m.get(f):
                    patch[f] = m.get(f)
            if len(patch) > 1:
                node_updates.append(patch)

    edge_creates, edge_updates, edge_deletes = [], [], []
    for eid in sorted(set(base_edges) | set(edges)):
        b, m = base_edges.get(eid), edges.get(eid)
        if b is None and m is not None:
            edge_creates.append({k: m[k] for k in ("id", *_EDGE_FIELDS)})
        elif b is not None and m is None:
            edge_deletes.append(eid)
        elif b is not None and m is not None:
            patch = {"id": eid}
            for f in ("kind", "label"):  # 现有边只可能改立场 / 说明（端点 / id 不可变）
                if b.get(f) != m.get(f):
                    patch[f] = m.get(f)
            if len(patch) > 1:
                edge_updates.append(patch)

    return Changeset.model_validate({
        "base_version_id": base.version_id,
        "node_creates": node_creates,
        "node_updates": node_updates,
        "node_deletes": node_deletes,
        "edge_creates": edge_creates,
        "edge_updates": edge_updates,
        "edge_deletes": edge_deletes,
    })


def merge_proposals(conn: sqlite3.Connection, *, proposal_a_id: int, proposal_b_id: int,
                    message: str, resolutions: dict[str, str]) -> dict[str, Any]:
    """写事务内重新合并：校验提案仍 open、当前版本仍等于基线；重跑三方合并与整图校验，
    全部通过才生成 merge 版本并同时标记两份提案已合并；任一失败整体回滚。"""
    message = (message or "").strip()
    if not message:
        raise GraphError("合并提交说明不能为空", "invalid_message")

    new_id: int | None = None
    try:
        conn.execute("BEGIN IMMEDIATE")

        ra, rb = _load_two(conn, proposal_a_id, proposal_b_id)
        base_vid = ra["base_version"]
        current = conn.execute("SELECT MAX(id) FROM versions").fetchone()[0]
        if current != base_vid:
            raise GraphError(
                f"当前版本 v{current} 已不是提案基线 v{base_vid}，请刷新后重新合并",
                "stale_version", 409)

        base = load_graph(conn, base_vid)
        ch_a = json_to_changeset(ra["changes_json"])
        ch_b = json_to_changeset(rb["changes_json"])
        a_nodes, a_edges = _project(base, ch_a)
        b_nodes, b_edges = _project(base, ch_b)
        base_nodes = {nid: node_view(n) for nid, n in base.nodes.items()}
        base_edges = {eid: edge_view(e) for eid, e in base.edges.items()}
        node_records = _build_node_records(base_nodes, a_nodes, b_nodes)
        edge_records = _build_edge_records(base_edges, a_edges, b_edges, node_records)

        _validate_resolutions(node_records, edge_records, resolutions)
        _nodes_m, _edges_m, changes = _compose(
            base, node_records, edge_records, resolutions, strict=True)

        # 复用现有整图校验（成环 / 时间窗 / 悬空端点 / 类型 / 平行边 …）；
        # 失败时附带合并后整图供前端定位，事务随即回滚。
        new_nodes, new_edges = apply_changes(base, changes, capture_partial=True)

        cur = conn.execute(
            "INSERT INTO versions (message, kind, base_id) VALUES (?, 'merge', ?)",
            (message, base_vid),
        )
        new_id = cur.lastrowid
        persist_graph(conn, new_id, new_nodes, new_edges)
        conn.execute(
            "UPDATE change_proposals SET status = 'merged', merged_into = ?, "
            "merged_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id IN (?, ?)",
            (new_id, proposal_a_id, proposal_b_id),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    row = conn.execute("SELECT * FROM versions WHERE id = ?", (new_id,)).fetchone()
    return {
        "version_id": new_id,
        "message": message,
        "kind": "merge",
        "created_at": row["created_at"],
        "merged_proposal_ids": [proposal_a_id, proposal_b_id],
    }


def _validate_resolutions(node_records, edge_records, resolutions) -> None:
    """校验裁决键合法：实体级 `<id>`（删除/修改）或字段级 `<id>:<field>`（字段冲突）。"""
    records = [r for r in (*node_records.values(), *edge_records.values())
               if r["conflict_type"] != "none"]
    by_id = {r["id"]: r for r in records}
    field_conflict_fields = {
        (r["id"], f["field"]) for r in records for f in r["conflict_fields"]
    }
    for key, val in resolutions.items():
        if val not in ("a", "b", "base"):
            raise GraphError(f"冲突 {key} 的裁决非法：{val}", "invalid_resolution")
        if key in by_id:
            rec = by_id[key]
            if rec["conflict_type"] in ("fields", "create_create") and val == "base" \
                    and rec["conflict_type"] == "create_create":
                raise GraphError(f"冲突 {key} 不能选择基线（双方都是新建）", "invalid_resolution")
            continue
        if ":" in key:
            eid, _, field = key.partition(":")
            if (eid, field) not in field_conflict_fields:
                raise GraphError(f"裁决项不属于任何冲突字段：{key}", "invalid_resolution")
            if val == "base" and eid in by_id \
                    and by_id[eid]["conflict_type"] == "create_create":
                raise GraphError(f"冲突 {key} 不能选择基线（双方都是新建）", "invalid_resolution")
            continue
        raise GraphError(f"裁决项不属于任何冲突：{key}", "invalid_resolution")
