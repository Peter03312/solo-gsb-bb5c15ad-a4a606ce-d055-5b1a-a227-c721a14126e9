"""培养方案要求版本的达成审核引擎。

要求版本（由教务员按培养方案编号登记，绑定一个已有课程目录版本）：
- 必修课程列表（声明顺序）；
- 按优先级排列的选修组，每组给出候选课程与最低学分门槛；
- 同门课可出现在不同要求中（必修与选修候选、不同选修组之间均可）。

达成审核（以要求版本 + 已修课程清单生成，可回看，后续目录或要求修订不改变历史审核）：
- 互认课程（按绑定目录版本的互认组）可满足对应必修或选修候选；
- 一门实际已修课只归属一项（某个必修项或某个选修组）；
- 同一互认组的多门已修课在整次审核中至多计入一门。

归属按以下顺序取优（字典序）：
1. 最大化满足的必修项数；
2. 按选修组优先级顺序，依次最大化各组计入学分（不超过该组门槛）；
3. 按归属课程代码序列取字典序最小：必修项按声明顺序列出归属课程
   （未满足的必修项记为最大占位），再按选修组顺序列出各组归属课程
   （组内代码升序连接）。
"""
from __future__ import annotations

from .planner import PlanError

NODE_BUDGET = 200_000  # 选修组归属搜索节点上限，防止指数爆炸


def _equiv_map(equiv_groups):
    """互认组列表 -> {课程代码: 所在组（frozenset）}；未声明时为空映射。"""
    mapping = {}
    for group in equiv_groups or []:
        members = frozenset(c.strip().upper() for c in group if c.strip())
        for code in members:
            mapping[code] = members
    return mapping


def run_audit(credits, equiv_groups, required, elective_groups, completed):
    """生成达成审核结果（纯计算，持久化由调用方负责）。

    credits:         {课程代码: 学分}（要求版本绑定的目录版本）
    equiv_groups:    绑定目录版本声明的课程互认组
    required:        必修课程代码列表（声明顺序；调用方已校验无重复且在目录中）
    elective_groups: [{"candidates": [...], "min_credits": n}, ...]（按优先级顺序）
    completed:       已修课程代码集合（目录外课程不参与归属，列入未归属课程）
    """
    equiv = _equiv_map(equiv_groups)

    def cluster_of(code):
        """课程所属的互认簇：同簇已修课在整次审核中至多计入一门。"""
        return equiv.get(code, frozenset({code}))

    # 已修课程按互认簇归并（仅目录内课程可参与归属）
    clusters = {}
    for c in completed:
        if c in credits:
            clusters.setdefault(cluster_of(c), set()).add(c)

    # ---- 必修阶段 ------------------------------------------------------
    # 每个必修项的可满足课程都在同一个簇（该必修课的互认簇）内；同簇必修项
    # 共享一个簇，而簇内已修课至多计入一门 -> 同簇必修项至多满足一项。
    # 因此“满足的必修项数”已被确定：凡有已修课的簇必须用于满足一个必修项
    # （否则满足的必修项数不是最大）；簇内归属取声明顺序最早的必修项、
    # 代码最小的已修课（归属课程代码序列字典序最小）。
    items_by_cluster = {}
    for idx, code in enumerate(required):
        items_by_cluster.setdefault(cluster_of(code), []).append(idx)

    assigned_required = [None] * len(required)
    used_clusters = set()
    for ckey, idxs in items_by_cluster.items():
        pool = clusters.get(ckey)
        if not pool:
            continue  # 簇内无已修课：这些必修项均不满足
        assigned_required[min(idxs)] = min(pool)
        used_clusters.add(ckey)

    # ---- 选修阶段 ------------------------------------------------------
    # 剩余簇在选修组间分配：每簇至多归属一个选修组、组内任选一门已修课计入。
    caps = [g["min_credits"] for g in elective_groups]
    avail = []  # (簇内已修课代码升序列表, 可归属的选修组下标元组)
    for ckey, pool in clusters.items():
        if ckey in used_clusters:
            continue
        eligible = tuple(
            j for j, g in enumerate(elective_groups)
            if any(cluster_of(k) == ckey for k in g["candidates"])
        )
        if eligible:
            avail.append((sorted(pool), eligible))
    avail.sort(key=lambda item: item[0][0])

    chosen = _assign_groups(avail, caps, credits, len(caps))

    # ---- 汇总结果 ------------------------------------------------------
    group_assigned = [[] for _ in caps]
    for j, code in chosen:
        group_assigned[j].append(code)

    required_out = []
    unsatisfied = []
    for idx, code in enumerate(required):
        assigned = assigned_required[idx]
        required_out.append({
            "course": code,
            "satisfied": assigned is not None,
            "assigned_course": assigned,
        })
        if assigned is None:
            unsatisfied.append(code)

    groups_out = []
    for j, g in enumerate(elective_groups):
        cap = g["min_credits"]
        assigned = sorted(group_assigned[j])
        counted = min(sum(credits[c] for c in assigned), cap)
        groups_out.append({
            "index": j + 1,
            "candidates": list(g["candidates"]),
            "min_credits": cap,
            "counted_credits": counted,
            "gap": cap - counted,
            "assigned_courses": [{"code": c, "credits": credits[c]} for c in assigned],
        })

    assigned_all = sorted(
        {a for a in assigned_required if a is not None}
        | {c for codes in group_assigned for c in codes}
    )
    unassigned = sorted(set(completed) - set(assigned_all))
    return {
        "attained": not unsatisfied and all(g["gap"] == 0 for g in groups_out),
        "required": required_out,
        "unsatisfied_required": unsatisfied,
        "elective_groups": groups_out,
        "assigned_courses": assigned_all,
        "unassigned_courses": unassigned,
    }


def _assign_groups(avail, caps, credits, m):
    """两阶段分支限界求选修组最优归属，返回 [(选修组下标, 课程代码), ...]。

    avail: [(簇内课程代码升序列表, 可归属的组下标元组), ...]（每簇至多归属一次）。
    阶段一：按组序最大化各组计入学分元组（min(学分和, 门槛)）；
    阶段二：在计入学分元组不变的约束下，求归属课程代码序列（各组内代码升序、
    按组序连接）字典序最小的归属。归属集合保持最小：组内每门课移除后计入
    学分都会下降（不归属对计入无贡献的课程）。
    """
    n = len(avail)
    if n == 0 or m == 0:
        return []

    # 后缀上界：rem[i][j] = 第 i 簇起可归属组 j 的最大学分和（每簇取其最高学分课程）
    rem = [[0] * m for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        codes, eligible = avail[i]
        top = max(credits[c] for c in codes)
        for j in range(m):
            rem[i][j] = rem[i + 1][j] + (top if j in eligible else 0)

    nodes = [0]
    picked = []  # 当前路径：[(簇下标, 组下标, 课程代码)]

    def tick():
        nodes[0] += 1
        if nodes[0] > NODE_BUDGET:
            raise PlanError("TOO_COMPLEX", "选修课归属组合过多，超出审核复杂度限制")

    def minimal_ok(sums):
        """最小性不变式：任一已归属课程移除后计入都会下降（归属只增不减，违反则无法修复）。"""
        for _, j, code in picked:
            if sums[j] > caps[j] and sums[j] - credits[code] >= caps[j]:
                return False
        return True

    # ---- 阶段一：计入学分元组字典序最大（同值剪枝，不看归属序列）----
    best_counted = [None]

    def dfs_counted(i, sums):
        tick()
        if not minimal_ok(sums):
            return
        if best_counted[0] is not None:
            upper = tuple(min(caps[j], sums[j] + rem[i][j]) for j in range(m))
            if upper <= best_counted[0]:
                return  # 剩余簇全部用上也无法严格更优
        if i == n:
            counted = tuple(min(sums[j], caps[j]) for j in range(m))
            if best_counted[0] is None or counted > best_counted[0]:
                best_counted[0] = counted
            return
        codes, eligible = avail[i]
        for j in eligible:
            if sums[j] >= caps[j]:
                continue  # 该组计入已达门槛，再归属必然冗余
            for code in codes:
                w = credits[code]
                sums[j] += w
                picked.append((i, j, code))
                dfs_counted(i + 1, sums)
                picked.pop()
                sums[j] -= w
        dfs_counted(i + 1, sums)  # 该簇不归属任何组

    dfs_counted(0, [0] * m)
    tstar = best_counted[0]  # 必然存在（全不归属即全 0）

    # ---- 阶段二：计入学分元组固定为 tstar，求字典序最小的归属序列 ----
    # exact[j]：计入未达门槛的组，归属学分和须恰好等于 tstar[j]；
    # 其余组计入已达门槛，归属学分和可超出（受最小性约束）。
    exact = [tstar[j] < caps[j] for j in range(m)]
    best_seq = [None]
    best_picked = [None]

    def seq_of():
        per = [[] for _ in range(m)]
        for _, j, code in picked:
            per[j].append(code)
        return tuple(tuple(sorted(cs)) for cs in per)

    def seq_lower_bound(i, sums):
        """当前路径可完成的最小归属序列下界（松弛：忽略每簇一门与学分精确性）。

        对每组：已归属集合 P 的基础上，池（剩余簇中可归属该组的课程代码）内
        所有比 max(P) 小的代码并入都会使序列更小；若计入仍需 k 门课，再补池内
        最小者凑足 k 门。返回 None 表示该分支不可能达到 tstar。
        """
        lbs = []
        for j in range(m):
            if tstar[j] == 0:
                lbs.append(())
                continue
            pj = sorted(code for _, jj, code in picked if jj == j)
            need = tstar[j] - min(sums[j], tstar[j])
            pool = sorted(c for k in range(i, n) if j in avail[k][1] for c in avail[k][0])
            if need > 0:
                if not pool:
                    return None
                top = max(credits[c] for c in pool)
                k = -(-need // top)  # 至少还需 k 门课
            else:
                k = 0
            if not pj:
                if len(pool) < k:
                    return None
                lbs.append(tuple(pool[:k]))
                continue
            extra = [c for c in pool if c < pj[-1]]
            if len(extra) < k:
                rest = [c for c in pool if c >= pj[-1]]
                extra += rest[: k - len(extra)]
                if len(extra) < k:
                    return None
            lbs.append(tuple(sorted(pj + extra)))
        return tuple(lbs)

    def dfs_seq(i, sums):
        tick()
        if not minimal_ok(sums):
            return
        for j in range(m):
            if exact[j] and sums[j] > tstar[j]:
                return  # 精确计入的组不得超出
            if min(caps[j], sums[j] + rem[i][j]) < tstar[j]:
                return  # 剩余簇全部用上也无法达到 tstar[j]
        if best_seq[0] is not None:
            lb = seq_lower_bound(i, sums)
            if lb is None or lb >= best_seq[0]:
                return
        if i == n:
            seq = seq_of()
            if best_seq[0] is None or seq < best_seq[0]:
                best_seq[0] = seq
                best_picked[0] = list(picked)
            return
        codes, eligible = avail[i]
        for j in eligible:
            if tstar[j] == 0:
                continue  # 该组计入为 0：任何归属都会使计入为正
            for code in codes:
                w = credits[code]
                if exact[j]:
                    if sums[j] + w > tstar[j]:
                        continue
                elif sums[j] >= caps[j]:
                    continue  # 该组计入已达门槛，再归属必然冗余
                sums[j] += w
                picked.append((i, j, code))
                dfs_seq(i + 1, sums)
                picked.pop()
                sums[j] -= w
        dfs_seq(i + 1, sums)  # 该簇不归属任何组

    dfs_seq(0, [0] * m)
    return [(j, code) for _, j, code in (best_picked[0] or [])]
