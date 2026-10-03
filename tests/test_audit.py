"""培养方案要求版本与达成审核：引擎单元测试与 HTTP API 集成测试（独立临时数据库）。"""
import os

import pytest
from fastapi.testclient import TestClient

from app.audit import run_audit


def audit(credits, required, groups, completed, equiv=None):
    elective_groups = [{"candidates": list(cands), "min_credits": cap}
                       for cands, cap in groups]
    return run_audit(credits, equiv or [], required, elective_groups, set(completed))


# ---------------------------------------------------------------- 引擎：必修归属

def test_required_exact_and_unsatisfied():
    credits = {"A": 3, "B": 3, "C": 3}
    r = audit(credits, ["A", "B", "C"], [], completed=["A", "C"])
    assert [x["satisfied"] for x in r["required"]] == [True, False, True]
    assert r["unsatisfied_required"] == ["B"]
    assert r["attained"] is False


def test_required_satisfied_by_equivalent():
    """互认课程可满足对应必修项。"""
    credits = {"A": 3, "B": 3}
    r = audit(credits, ["A"], [], completed=["B"], equiv=[["A", "B"]])
    assert r["required"][0]["satisfied"] is True
    assert r["required"][0]["assigned_course"] == "B"
    assert r["attained"] is True


def test_same_equiv_group_counts_at_most_one():
    """同一互认组多门已修课至多计入一门：同组两个必修项只能满足一个。"""
    credits = {"A": 3, "B": 3}
    r = audit(credits, ["A", "B"], [], completed=["A", "B"], equiv=[["A", "B"]])
    satisfied = [x for x in r["required"] if x["satisfied"]]
    assert len(satisfied) == 1
    # 归属课程代码序列字典序最小：满足声明顺序最早的必修项，并用代码最小的已修课
    assert r["required"][0] == {"course": "A", "satisfied": True, "assigned_course": "A"}
    assert r["required"][1]["satisfied"] is False
    assert r["unassigned_courses"] == ["B"]


def test_required_picks_smallest_code_in_cluster():
    """簇内多门已修课可满足同一必修项时，归属代码最小者。"""
    credits = {"A": 3, "B": 3, "C": 3}
    r = audit(credits, ["C"], [], completed=["A", "B"], equiv=[["A", "B", "C"]])
    assert r["required"][0]["assigned_course"] == "A"
    assert r["unassigned_courses"] == ["B"]


# ---------------------------------------------------------------- 引擎：选修组

def test_elective_group_credits_capped_at_threshold():
    """计入学分不超过该组门槛；缺口 = 门槛 - 计入。"""
    credits = {"A": 4, "B": 4}
    r = audit(credits, [], [(["A", "B"], 6)], completed=["A", "B"])
    g = r["elective_groups"][0]
    assert g["counted_credits"] == 6   # 4+4=8  capped at 6
    assert g["gap"] == 0
    assert [c["code"] for c in g["assigned_courses"]] == ["A", "B"]  # 两门都有贡献
    assert r["attained"] is True


def test_elective_group_gap():
    credits = {"A": 3, "B": 3}
    r = audit(credits, [], [(["A", "B"], 5)], completed=["A"])
    g = r["elective_groups"][0]
    assert g["counted_credits"] == 3
    assert g["gap"] == 2
    assert r["attained"] is False


def test_one_course_belongs_to_one_item():
    """一门实际已修课只归属一项：同课是两个组的候选时只计入一个组。"""
    credits = {"A": 3}
    r = audit(credits, [], [(["A"], 3), (["A"], 3)], completed=["A"])
    g1, g2 = r["elective_groups"]
    # 按选修组顺序最大化：先满足第 1 组
    assert (g1["counted_credits"], g1["gap"]) == (3, 0)
    assert (g2["counted_credits"], g2["gap"]) == (0, 3)


def test_group_priority_order_maximization():
    """按选修组顺序依次最大化：共享簇优先让给排在前面的组。"""
    credits = {"A": 3, "B": 3}
    # A 同时是两组候选；B 只是第 2 组候选
    r = audit(credits, [], [(["A"], 3), (["A", "B"], 3)], completed=["A", "B"])
    g1, g2 = r["elective_groups"]
    assert g1["assigned_courses"] == [{"code": "A", "credits": 3}]
    assert g2["assigned_courses"] == [{"code": "B", "credits": 3}]
    assert r["attained"] is True


def test_lexicographic_tiebreak_on_assigned_codes():
    """计入学分相同则按归属课程代码序列取字典序最小。"""
    credits = {"A": 3, "B": 3}
    r = audit(credits, [], [(["A", "B"], 3)], completed=["A", "B"])
    g = r["elective_groups"][0]
    # A、B 单独均达门槛，计入同为 3；字典序最小 -> 归属 A
    assert g["assigned_courses"] == [{"code": "A", "credits": 3}]
    assert r["unassigned_courses"] == ["B"]


def test_lexicographic_sequence_prefers_smaller_prefix():
    """归属序列按字典序比较：(A,B) 小于 (C,)——即使课程更多。"""
    credits = {"A": 2, "B": 2, "C": 4}
    r = audit(credits, [], [(["A", "B", "C"], 4)], completed=["A", "B", "C"])
    g = r["elective_groups"][0]
    assert g["counted_credits"] == 4
    assert [c["code"] for c in g["assigned_courses"]] == ["A", "B"]


def test_equiv_course_counts_for_elective_candidate():
    """互认课程可满足选修候选；同组多门已修课在整次审核中至多计入一门。"""
    credits = {"A": 3, "B": 3}
    r = audit(credits, [], [(["A"], 3)], completed=["B"], equiv=[["A", "B"]])
    g = r["elective_groups"][0]
    assert g["counted_credits"] == 3
    assert g["assigned_courses"] == [{"code": "B", "credits": 3}]
    # 同组两门都修了也只能计入一门
    r = audit(credits, [], [(["A"], 3), (["A"], 3)], completed=["A", "B"],
              equiv=[["A", "B"]])
    counted = [g["counted_credits"] for g in r["elective_groups"]]
    assert counted == [3, 0]


def test_required_beats_elective_for_shared_cluster():
    """同一簇既可用于必修又可用于选修候选时，先最大化满足的必修项数。"""
    credits = {"A": 3, "B": 3}
    r = audit(credits, ["A"], [(["A"], 3)], completed=["B"], equiv=[["A", "B"]])
    assert r["required"][0]["satisfied"] is True
    assert r["required"][0]["assigned_course"] == "B"
    assert r["elective_groups"][0]["counted_credits"] == 0
    assert r["elective_groups"][0]["gap"] == 3


def test_same_course_in_required_and_candidates():
    """同门课可出现在不同要求中：必修消耗后不再计入选修组。"""
    credits = {"A": 3, "B": 3}
    r = audit(credits, ["A"], [(["A", "B"], 3)], completed=["A", "B"])
    assert r["required"][0]["assigned_course"] == "A"
    g = r["elective_groups"][0]
    assert g["assigned_courses"] == [{"code": "B", "credits": 3}]
    assert r["attained"] is True


def test_course_choice_within_cluster_prefers_lexicographic():
    """簇内课程学分不同：计入相同则归属代码最小者。"""
    credits = {"A": 4, "B": 3}
    r = audit(credits, [], [(["X"], 3)], completed=["A", "B"],
              equiv=[["A", "B", "X"]])
    g = r["elective_groups"][0]
    # A(4) 或 B(3) 计入均为 3（封顶）；字典序最小 -> A
    assert g["assigned_courses"] == [{"code": "A", "credits": 4}]
    assert g["counted_credits"] == 3


def test_higher_credit_course_frees_cluster_for_later_group():
    """高学分课程让先修组更早达门槛，释放簇给后续组（按组序最大化）。"""
    credits = {"A": 2, "B": 4, "C": 3}
    # 簇 {A,B} 可归属组1；C 可归属组2
    r = audit(credits, [], [(["X"], 4), (["C"], 3)], completed=["A", "B", "C"],
              equiv=[["A", "B", "X"]])
    g1, g2 = r["elective_groups"]
    # 组1 用 B(4) 一门即达门槛（若用 A(2) 也只有 2 学分）
    assert g1["assigned_courses"] == [{"code": "B", "credits": 4}]
    assert g2["counted_credits"] == 3
    assert r["attained"] is True


def test_unknown_completed_courses_ignored_for_assignment():
    """目录外已修课不参与归属，列入未归属课程。"""
    credits = {"A": 3}
    r = audit(credits, ["A"], [], completed=["A", "ZZZ"])
    assert r["attained"] is True
    assert r["unassigned_courses"] == ["ZZZ"]


def test_empty_requirement_attained():
    r = audit({"A": 3}, [], [], completed=["A"])
    assert r["attained"] is True
    assert r["assigned_courses"] == []
    assert r["unassigned_courses"] == ["A"]


def test_no_redundant_assignment_beyond_threshold():
    """归属集合保持最小：不为挤占序列而归属对计入无贡献的课程。"""
    credits = {"A": 2, "B": 2, "C": 4}
    r = audit(credits, [], [(["A", "B", "C"], 5)], completed=["A", "B", "C"])
    g = r["elective_groups"][0]
    # {A,B,C} 中 A/B 移除后计入不降（冗余），合法最优为计入 5 的 {A,C} 或 {B,C}；
    # 字典序最小 -> {A,C}
    assert g["counted_credits"] == 5
    assert [c["code"] for c in g["assigned_courses"]] == ["A", "C"]
    assert r["unassigned_courses"] == ["B"]


def test_exact_counted_when_below_threshold():
    """计入未达门槛的组按实际学分计入；归属学分和恰好等于计入值。"""
    credits = {"A": 2, "B": 3}
    r = audit(credits, [], [(["A"], 5), (["B"], 5)], completed=["A", "B"])
    g1, g2 = r["elective_groups"]
    assert (g1["counted_credits"], g1["gap"]) == (2, 3)
    assert (g2["counted_credits"], g2["gap"]) == (3, 2)
    assert r["attained"] is False


def test_cluster_split_across_groups_by_priority():
    """不同簇分别适合不同组时按组序分配；先修满排在前面的组。"""
    credits = {"A": 3, "B": 3, "C": 3}
    groups = [(["A", "B"], 3), (["B", "C"], 3)]
    r = audit(credits, [], groups, completed=["A", "B", "C"])
    g1, g2 = r["elective_groups"]
    # 计入均为 (3,3)；归属序列按组序比较：第 1 组取 A 后，第 2 组取 B 比取 C 字典序更小
    assert [c["code"] for c in g1["assigned_courses"]] == ["A"]
    assert [c["code"] for c in g2["assigned_courses"]] == ["B"]
    assert r["unassigned_courses"] == ["C"]
    assert r["attained"] is True


# ---------------------------------------------------------------- API 集成

@pytest.fixture()
def client(tmp_path):
    os.environ["DB_PATH"] = str(tmp_path / "test.db")
    from app.main import app
    with TestClient(app) as c:
        yield c


def requirement_payload(**kw):
    payload = {
        "program_code": "CS-2026",
        "catalog_version": 1,
        "required_courses": ["CS101", "CS201", "CS301", "CS401"],
        "elective_groups": [
            {"candidates": ["CS202", "CS302", "CS303"], "min_credits": 3},
            {"candidates": ["CS402", "PHYS201"], "min_credits": 3},
        ],
    }
    payload.update(kw)
    return payload


def _register(client, **kw):
    r = client.post("/requirements", json=requirement_payload(**kw))
    assert r.status_code == 201, r.text
    return r.json()


def _revise_with_equiv(client, groups):
    current = client.get("/catalog/current").json()
    r = client.post("/catalog/versions", json={
        "note": "互认修订", "courses": current["courses"], "equivalence_groups": groups})
    assert r.status_code == 201, r.text
    return r.json()["version_id"]


def test_register_requirement_and_review(client):
    body = _register(client, note="2026 级培养要求")
    assert body["requirement_version_id"] == 1
    assert body["program_code"] == "CS-2026"
    assert body["catalog_version"] == 1
    assert body["required_courses"] == ["CS101", "CS201", "CS301", "CS401"]
    assert body["elective_groups"][0]["min_credits"] == 3
    # 回看
    r = client.get("/requirements/1")
    assert r.status_code == 200
    assert r.json() == body
    # 列表（含按培养方案编号过滤）
    lst = client.get("/requirements").json()
    assert [x["requirement_version_id"] for x in lst] == [1]
    assert lst[0]["required_count"] == 4 and lst[0]["elective_group_count"] == 2
    assert client.get("/requirements", params={"program_code": "CS-2026"}).json() != []
    assert client.get("/requirements", params={"program_code": "OTHER"}).json() == []
    # 再次登记 -> 新版本
    body2 = _register(client, note="修订")
    assert body2["requirement_version_id"] == 2
    assert client.get("/requirements/999").status_code == 404


def test_register_rejections_leave_no_record(client):
    # 未知目录版本
    r = client.post("/requirements", json=requirement_payload(catalog_version=99))
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "NOT_FOUND"
    # 未知课程（必修 / 选修候选）
    r = client.post("/requirements", json=requirement_payload(
        required_courses=["CS101", "NOPE101"]))
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "COURSE_MISSING"
    r = client.post("/requirements", json=requirement_payload(
        elective_groups=[{"candidates": ["CS202", "NOPE202"], "min_credits": 3}]))
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "COURSE_MISSING"
    # 重复必修项（大小写不敏感）
    r = client.post("/requirements", json=requirement_payload(
        required_courses=["CS101", "cs101"]))
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_INPUT"
    # 非正学分门槛
    r = client.post("/requirements", json=requirement_payload(
        elective_groups=[{"candidates": ["CS202"], "min_credits": 0}]))
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_INPUT"
    r = client.post("/requirements", json=requirement_payload(
        elective_groups=[{"candidates": ["CS202"], "min_credits": -3}]))
    assert r.status_code == 422
    # 空候选组
    r = client.post("/requirements", json=requirement_payload(
        elective_groups=[{"candidates": [], "min_credits": 3}]))
    assert r.status_code == 422
    # 均未留记录
    assert client.get("/requirements").json() == []


def test_audit_flow_and_review(client):
    req = _register(client)
    rid = req["requirement_version_id"]
    r = client.post(f"/requirements/{rid}/audits", json={
        "completed_courses": ["CS101", "CS101L", "CS201", "CS202", "CS302", "CS402"]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["requirement_version_id"] == rid
    assert body["program_code"] == "CS-2026"
    assert body["catalog_version"] == 1
    res = body["result"]
    # 各项归属（目录 v1 无互认组：CS302 与 CS301 无关）
    assert [x["assigned_course"] for x in res["required"]] == [
        "CS101", "CS201", None, None]
    assert res["unsatisfied_required"] == ["CS301", "CS401"]
    # 各组选修学分缺口
    assert [(g["counted_credits"], g["gap"]) for g in res["elective_groups"]] == [
        (3, 0), (3, 0)]
    # 第 1 组门槛 3：CS202/CS302 各 3 学分，归属一门即达门槛，字典序最小取 CS202；
    # CS302 与 CS101L 未归属
    assert res["elective_groups"][0]["assigned_courses"] == [
        {"code": "CS202", "credits": 3}]
    assert res["assigned_courses"] == ["CS101", "CS201", "CS202", "CS402"]
    assert res["unassigned_courses"] == ["CS101L", "CS302"]
    assert res["attained"] is False
    # 可回看
    audit_id = body["audit_id"]
    again = client.get(f"/audits/{audit_id}")
    assert again.status_code == 200
    assert again.json() == body
    # 审核列表
    lst = client.get(f"/requirements/{rid}/audits").json()
    assert [x["audit_id"] for x in lst] == [audit_id]
    assert lst[0]["attained"] is False
    assert client.get("/audits/unknown").status_code == 404
    assert client.get("/requirements/999/audits").status_code == 404
    r = client.post("/requirements/999/audits", json={"completed_courses": []})
    assert r.status_code == 404


def test_audit_attained_true(client):
    req = _register(client, required_courses=["CS101"],
                    elective_groups=[{"candidates": ["CS202"], "min_credits": 3}])
    r = client.post(f"/requirements/{req['requirement_version_id']}/audits",
                    json={"completed_courses": ["CS101", "CS202"]})
    res = r.json()["result"]
    assert res["attained"] is True
    assert res["unsatisfied_required"] == []
    assert all(g["gap"] == 0 for g in res["elective_groups"])


def test_audit_uses_bound_catalog_equivalence(client):
    """互认按绑定目录版本判断：已修 CS302 满足必修 CS301；同组课程至多计入一门。"""
    vid = _revise_with_equiv(client, [["CS301", "CS302"], ["CS401", "CS402"]])
    req = _register(client, catalog_version=vid)
    rid = req["requirement_version_id"]
    r = client.post(f"/requirements/{rid}/audits", json={
        "completed_courses": ["CS101", "CS101L", "CS201", "CS302", "CS401", "CS402"]})
    res = r.json()["result"]
    # CS302 满足必修 CS301；CS401 满足必修 CS401（簇内代码最小者）
    assert [x["assigned_course"] for x in res["required"]] == [
        "CS101", "CS201", "CS302", "CS401"]
    # 簇被必修消耗：CS302 不再计入第 1 组，CS402（与 CS401 同组）不再计入第 2 组
    assert [(g["counted_credits"], g["gap"]) for g in res["elective_groups"]] == [
        (0, 3), (0, 3)]
    assert res["unassigned_courses"] == ["CS101L", "CS402"]
    assert res["attained"] is False


def test_audit_history_immune_to_later_revisions(client):
    """后续目录或要求修订不改变历史审核。"""
    req = _register(client)
    rid = req["requirement_version_id"]
    r = client.post(f"/requirements/{rid}/audits",
                    json={"completed_courses": ["CS101", "CS201"]})
    audit = r.json()
    # 目录修订（引入互认组）与要求修订（登记新版本）
    _revise_with_equiv(client, [["CS301", "CS302"]])
    _register(client, required_courses=["CS101"])
    again = client.get(f"/audits/{audit['audit_id']}")
    assert again.status_code == 200
    assert again.json() == audit
    assert again.json()["catalog_version"] == 1
    # 原要求版本回看亦不变
    assert client.get(f"/requirements/{rid}").json()["required_courses"] == [
        "CS101", "CS201", "CS301", "CS401"]


def test_existing_plan_flow_unaffected(client):
    """既有规划/确认行为保持兼容。"""
    r = client.post("/plans", json={
        "completed_courses": [], "target_courses": ["CS401"],
        "start_semester": "2026-SPRING", "max_credits_per_semester": 12,
        "num_semesters": 6})
    assert r.status_code == 201
    plan = r.json()
    assert plan["result"]["completion_semester"] == "2027-FALL"
    assert client.post(f"/plans/{plan['plan_id']}/confirm").status_code == 200
