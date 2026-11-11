"""灌区供水分配：可供水量列解析 + 字典序最大满足率的精确求解。

业务规则
========
* 从「转换结果」按**列位置**选择可供水量列（首列固定为水库名，故可选
  列为第 2 ~ width 列）；该列每个正文行读出一座水库的可供水量。
* 水库最多 6 座；可供水量为**非负整数**，单位统一按**万立方米**处理；
  **同名水库拒绝**（同一水库名出现两次即整列拒绝）。
* 页面录入最多 12 个灌区：名称、正整数需水量（万立方米）、可供水库
  子集（允许为空，表示无水源，满足率恒为 0，其余灌区照常优化）。
* 水只能沿指定的「水库 → 灌区」关系分配；每座水库供水总量不超过其
  可供水量，每个灌区所得不超过其需水量。

优化目标
========
记灌区 i 的满足率 r_i = 所得水量 / 需水量。把全部灌区满足率按升序排
成向量，求该向量的**字典序最大值**（先最大化最低满足率，再依次最大
化次低满足率……），即经典的 lexicographic max-min fair allocation。

算法（精确有理数，全部使用 ``fractions.Fraction``）
====================================================
1. **渐进注水 / 参数化割**：每轮为尚未冻结的灌区求最大公共满足率 λ。
   由最大流最小割，对水库集合的每个子集 Z（水库 ≤ 6 座，可枚举）：

   ``λ ≤ ( C(Z) − F(Z) ) / D(Z)``

   其中 C(Z) 为 Z 内水库可供水量之和，D(Z) 为可供水库全部落在 Z 内
   的未冻结灌区需水量之和，F(Z) 为已冻结灌区（可供水库全部落在 Z
   内）已锁定的水量之和；λ 同时不超过 1（所得不超过需求）。取最小
   值即本轮 λ；达到最小值的「最紧」子集（含 D(Z)=0 但 C(Z)=F(Z) 的
   零需求紧集）取并集 Z*，可供水库落在 Z* 内的未冻结灌区本轮被冻结
   在 λ——它们的供水源容量已被占满，任何可行方案都无法再提高。每轮
   至少冻结一个灌区，至多 12 轮结束；无水源灌区第一轮即以 λ=0 冻结。
2. **精确最大流还原分配**：全部灌区冻结后，以 r_i·需水量 作为各灌区
   到汇点的容量，跑一次 Fraction 版 Dinic 最大流，得到一组实现该满
   足率向量的「水库 → 灌区」分配水量（最优分配不唯一时任取其一）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Optional

MAX_RESERVOIRS = 6
MAX_DISTRICTS = 12
UNIT = "万立方米"

_NONNEG_INT = re.compile(r"\d+")


class AllocationError(Exception):
    """供水分配输入非法，无法求解。"""


@dataclass(frozen=True)
class Reservoir:
    """一座水库及其可供水量（万立方米，非负整数）。"""

    name: str
    supply: int


@dataclass(frozen=True)
class District:
    """一个灌区：名称、正整数需水量（万立方米）、可供水库名（可为空）。"""

    name: str
    demand: int
    sources: tuple[str, ...]


@dataclass
class AllocationResult:
    """求解产物，全部数值为最简分数。"""

    rates: list[Fraction]  # 各灌区满足率（与 districts 同序）
    received: list[Fraction]  # 各灌区所得水量
    gaps: list[Fraction]  # 各灌区缺口 = 需水量 − 所得
    flows: list[list[Fraction]]  # flows[r][i]：水库 r 分给灌区 i 的水量
    reservoir_used: list[Fraction]  # 各水库实际供水合计
    sorted_rates: list[tuple[int, Fraction]]  # (灌区下标, 满足率) 升序


# ---------------------------------------------------------------------------
# 可供水量列解析
# ---------------------------------------------------------------------------

def parse_supply_column(
    records: list[dict],
    column: int,
    width: int,
) -> list[Reservoir]:
    """从转换结果记录中按列位置（1 基）读出水库可供水量清单。

    该列每个正文行对应一座水库；行数即水库数，最多 6 座；单元格文本
    必须是非负整数；同名水库拒绝。任何违规都抛出 :class:`AllocationError`。
    """
    if not isinstance(column, int) or not 2 <= column <= width:
        raise AllocationError(
            f"可供水量列位置无效：首列固定为水库名，可选范围为第 2～{width} 列。"
        )

    rows = [r for r in records if r.get("source_col") == column]
    if not rows:
        raise AllocationError(
            f"第 {column} 列没有读到任何正文观测记录，无法作为可供水量列。"
        )
    if len(rows) > MAX_RESERVOIRS:
        raise AllocationError(
            f"第 {column} 列共读出 {len(rows)} 座水库，超过 {MAX_RESERVOIRS} 座上限；"
            "请先汇总后再选择可供水量列。"
        )

    reservoirs: list[Reservoir] = []
    seen: set[str] = set()
    for rec in rows:
        name = (rec.get("reservoir") or "").strip()
        coord = rec.get("source_cell") or f"原表第 {rec.get('source_row', '?')} 行"
        if not name:
            raise AllocationError(
                f"第 {column} 列存在水库名为空的记录（{coord}），无法录入。"
            )
        raw = (rec.get("value") or "").strip()
        if not _NONNEG_INT.fullmatch(raw):
            shown = raw if raw else "（空）"
            raise AllocationError(
                f"水库「{name}」的可供水量 {shown}（{coord}）不是非负整数；"
                f"可供水量须为以{UNIT}为单位的非负整数。"
            )
        if name in seen:
            raise AllocationError(
                f"水库「{name}」在第 {column} 列中出现多次；同名水库拒绝录入，"
                "请先按水库汇总为每库一行后再选择该列。"
            )
        seen.add(name)
        reservoirs.append(Reservoir(name=name, supply=int(raw)))
    return reservoirs


# ---------------------------------------------------------------------------
# 灌区录入校验
# ---------------------------------------------------------------------------

def validate_districts(
    rows: list[tuple[str, str, list[str]]],
    reservoir_names: list[str],
) -> list[District]:
    """校验页面提交的灌区行 ``(名称, 需水量文本, 可供水库名列表)``。"""
    if not rows:
        raise AllocationError("请至少录入 1 个灌区。")
    if len(rows) > MAX_DISTRICTS:
        raise AllocationError(
            f"灌区数量最多 {MAX_DISTRICTS} 个，当前提交了 {len(rows)} 个。"
        )

    known = set(reservoir_names)
    seen: set[str] = set()
    districts: list[District] = []
    for idx, (name, demand_raw, sources) in enumerate(rows, start=1):
        name = (name or "").strip()
        if not name:
            raise AllocationError(f"第 {idx} 个灌区的名称不能为空。")
        if name in seen:
            raise AllocationError(f"灌区「{name}」重复录入，请合并或更名。")
        seen.add(name)

        demand_raw = (demand_raw or "").strip()
        if not _NONNEG_INT.fullmatch(demand_raw) or int(demand_raw) <= 0:
            raise AllocationError(
                f"灌区「{name}」的需水量「{demand_raw or '（空）'}」不是正整数"
                f"（单位：{UNIT}）。"
            )

        clean_sources: list[str] = []
        for s in sources:
            if s not in known:
                raise AllocationError(
                    f"灌区「{name}」勾选了不存在的水库「{s}」，请刷新页面重试。"
                )
            if s not in clean_sources:
                clean_sources.append(s)
        districts.append(
            District(name=name, demand=int(demand_raw), sources=tuple(clean_sources))
        )
    return districts


# ---------------------------------------------------------------------------
# 字典序最大满足率
# ---------------------------------------------------------------------------

def _lexicographic_rates(
    caps: list[int],
    demands: list[int],
    neighbors: list[frozenset],
) -> list[Fraction]:
    """渐进注水求各灌区满足率（与 demands 同序的 Fraction 列表）。"""
    n = len(demands)
    m = len(caps)
    rates: list[Optional[Fraction]] = [None] * n
    active = list(range(n))
    # 水库 ≤ 6 座，枚举全部子集
    subsets = [
        frozenset(r for r in range(m) if mask >> r & 1)
        for mask in range(1 << m)
    ]

    while active:
        best: Optional[Fraction] = None
        tight_best: set[int] = set()  # 达到最小值的需求紧集之并
        tight_zero: set[int] = set()  # D(Z)=0 且 C(Z)=F(Z) 的零需求紧集之并
        for z in subsets:
            rest = sum(caps[r] for r in z)
            rest -= sum(
                rates[j] * demands[j]  # type: ignore[operator]
                for j in range(n)
                if rates[j] is not None and neighbors[j] <= z
            )
            if rest < 0:
                raise AllocationError("内部错误：已冻结分配超出水库可供水量。")
            dem = sum(demands[i] for i in active if neighbors[i] <= z)
            if dem == 0:
                if rest == 0:
                    tight_zero |= z
                continue
            val = Fraction(rest, dem)
            if best is None or val < best:
                best = val
                tight_best = set(z)
            elif val == best:
                tight_best |= z

        # best 必存在：全集子集覆盖所有活跃灌区且需水量为正
        if best is not None and best >= 1:
            # 全部活跃灌区都能同时灌满，统一冻结在 1
            lam = Fraction(1)
            frozen_now = set(active)
        else:
            lam = best if best is not None else Fraction(1)
            tight = tight_best | tight_zero
            frozen_now = {i for i in active if neighbors[i] <= tight}
            if not frozen_now:  # 理论兜底，不应出现
                raise AllocationError("内部错误：未找到本轮可冻结的灌区。")
        for i in frozen_now:
            rates[i] = lam
        active = [i for i in active if i not in frozen_now]

    return [r for r in rates if r is not None]


# ---------------------------------------------------------------------------
# 精确分数最大流（Dinic）
# ---------------------------------------------------------------------------

def _max_flow(
    n_nodes: int,
    edges: list[tuple[int, int, Fraction]],
    source: int,
    sink: int,
) -> tuple[Fraction, dict[int, Fraction]]:
    """返回 ``(总流量, {原边下标: 流量})``，仅保留流量为正的边。"""
    graph: list[list[list]] = [[] for _ in range(n_nodes)]
    positions: list[tuple[int, int, Fraction]] = []
    for u, v, c in edges:
        cap = Fraction(c)
        positions.append((u, len(graph[u]), cap))
        fwd = [v, cap, len(graph[v])]
        rev = [u, Fraction(0), len(graph[u])]
        graph[u].append(fwd)
        graph[v].append(rev)

    inf = sum((Fraction(c) for _, _, c in edges), Fraction(0)) + 1
    total = Fraction(0)
    while True:
        level = [-1] * n_nodes
        level[source] = 0
        queue = [source]
        for u in queue:
            for e in graph[u]:
                if e[1] > 0 and level[e[0]] < 0:
                    level[e[0]] = level[u] + 1
                    queue.append(e[0])
        if level[sink] < 0:
            break

        it = [0] * n_nodes

        def dfs(u: int, f: Fraction) -> Fraction:
            if u == sink:
                return f
            while it[u] < len(graph[u]):
                e = graph[u][it[u]]
                if e[1] > 0 and level[e[0]] == level[u] + 1:
                    d = dfs(e[0], min(f, e[1]))
                    if d > 0:
                        e[1] -= d
                        graph[e[0]][e[2]][1] += d
                        return d
                it[u] += 1
            return Fraction(0)

        while True:
            pushed = dfs(source, inf)
            if pushed == 0:
                break
            total += pushed

    flows: dict[int, Fraction] = {}
    for k, (u, idx, cap) in enumerate(positions):
        used = cap - graph[u][idx][1]
        if used > 0:
            flows[k] = used
    return total, flows


def _exact_flows(
    caps: list[int],
    demands: list[int],
    neighbors: list[frozenset],
    rates: list[Fraction],
) -> list[list[Fraction]]:
    """按冻结满足率跑一次最大流，还原水库 → 灌区分配矩阵。"""
    m, n = len(caps), len(demands)
    source, sink = 0, m + n + 1
    edges: list[tuple[int, int, Fraction]] = []
    for r, c in enumerate(caps):
        edges.append((source, 1 + r, Fraction(c)))
    arc_index: dict[tuple[int, int], int] = {}
    for i in range(n):
        for r in neighbors[i]:
            arc_index[(r, i)] = len(edges)
            # 容量取需水量即可：灌区所得本就不超过需求
            edges.append((1 + r, 1 + m + i, Fraction(demands[i])))
    for i in range(n):
        edges.append((1 + m + i, sink, rates[i] * demands[i]))

    total, edge_flows = _max_flow(sink + 1, edges, source, sink)
    need = sum(rates[i] * demands[i] for i in range(n))
    if total != need:
        raise AllocationError("内部错误：最大流未达到冻结满足率对应的水量。")

    matrix = [[Fraction(0) for _ in range(n)] for _ in range(m)]
    for (r, i), k in arc_index.items():
        f = edge_flows.get(k, Fraction(0))
        if f > 0:
            matrix[r][i] = f
    return matrix


# ---------------------------------------------------------------------------
# 求解入口
# ---------------------------------------------------------------------------

def solve_allocation(
    reservoirs: list[Reservoir],
    districts: list[District],
) -> AllocationResult:
    """求字典序最大的满足率向量及一组实现它的分配水量。"""
    caps = [r.supply for r in reservoirs]
    demands = [d.demand for d in districts]
    rindex = {r.name: i for i, r in enumerate(reservoirs)}
    neighbors = [frozenset(rindex[s] for s in d.sources) for d in districts]

    rates = _lexicographic_rates(caps, demands, neighbors)
    flows = _exact_flows(caps, demands, neighbors, rates)

    received = [rates[i] * demands[i] for i in range(len(districts))]
    gaps = [Fraction(demands[i]) - received[i] for i in range(len(districts))]
    used = [
        sum(flows[r][i] for i in range(len(districts)))
        for r in range(len(reservoirs))
    ]
    order = sorted(
        range(len(districts)), key=lambda i: (rates[i], districts[i].name)
    )
    return AllocationResult(
        rates=rates,
        received=received,
        gaps=gaps,
        flows=flows,
        reservoir_used=used,
        sorted_rates=[(i, rates[i]) for i in order],
    )
