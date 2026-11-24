"""社区服务覆盖差异分析与外展安排支持工具。

读取 data/ 下的活动、边界版本、参与、人口、交通与问卷资料：

- 历史活动按“活动当日有效边界”解释，此后的边界或资料更正不改写历史段落；
- 当前覆盖与下一轮外展建议按“规划日有效边界”（当前口径）计算；
- 表达意向、完成报名、实际到场是不同证据，分别列示，绝不合并为单一需求数；
- 不用签到总人数为需求排序，覆盖差异连同交通条件、人口口径与资料缺口一起呈现。

用法：
    python3 coverage_analysis.py                      # 打印中文文本报告
    python3 coverage_analysis.py --json report.json   # 同时导出机器可读 JSON
    python3 coverage_analysis.py --planning-date 2026-11-24
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# 参与证据链的阶段。缺失一律记为 None（无记录），绝不当作 0。
STAGES = ("invited", "interested", "registered", "attended")
STAGE_LABELS = {
    "invited": "邀请",
    "interested": "表达意向",
    "registered": "完成报名",
    "attended": "实际到场",
}

MIN_SURVEY_RESPONDENTS = 10  # 问卷可解释的最低回应数
HIGH_UTILIZATION = 0.8       # 容量利用率偏高阈值
FAR_TRAVEL_MIN = 45          # 超过该分钟数视为交通不便
INTEREST_SUPPORT = 0.5       # 问卷“希望参加”比例达到该值视为有需求信号

INFERENCE_CAUTIONS = [
    "表达意向、完成报名、实际到场是不同证据，本报告分别列示，不能合并为单一需求数。",
    "签到总人数受交通条件与邀请覆盖影响，不能直接当作需求高低排序。",
    "边界版本调整前后的社区不是同一范围，跨期比较只能经重叠系数近似折算，且假设原社区内人口均匀分布。",
    "到场以家庭/学校团体整批记录，人数不是相互独立的个体意愿。",
    "问卷版本不同（如 F1、F2）时，“希望参加”比例不可直接跨期比较。",
]


# ---------------------------------------------------------------------- 读取

def load_tables(data_dir):
    """按 manifest 声明读取全部输入表，返回 {表名: [行字典]}。"""
    data_dir = Path(data_dir)
    manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))
    tables = {}
    for table in manifest["tables"]:
        name = table["file"].rsplit(".", 1)[0]
        with (data_dir / table["file"]).open(encoding="utf-8", newline="") as stream:
            tables[name] = list(csv.DictReader(stream))
    return tables


# ---------------------------------------------------------------------- 口径

def parse_day(value):
    return date.fromisoformat(value) if value else None


def quarter_of(day):
    return f"{day.year}Q{(day.month - 1) // 3 + 1}"


def areas_valid_on(areas, day):
    """某日有效的 {area_id: boundary_version}。边界更正只新增版本行，
    因此历史日期解析结果不受后续更正影响。"""
    valid = {}
    for row in areas:
        start = parse_day(row["valid_from"])
        end = parse_day(row["valid_to"])
        if (start is None or start <= day) and (end is None or day <= end):
            valid[row["area_id"]] = row["boundary_version"]
    return valid


def access_tier(travel_min):
    if travel_min is None:
        return "未知"
    if travel_min <= 20:
        return "近便"
    if travel_min <= FAR_TRAVEL_MIN:
        return "一般"
    return "不便"


def population_for(population, area_id, period):
    for row in population:
        if row["area_id"] == area_id and row["period"] == period:
            return row
    return None


def access_for(access, area_id, period):
    for row in access:
        if row["area_id"] == area_id and row["period"] == period:
            return row
    return None


def survey_for(surveys, area_id, event_id):
    for row in surveys:
        if row["area_id"] == area_id and row["event_id"] == event_id:
            return row
    return None


# ------------------------------------------------------------------ 活动分析

def _int_or_none(value):
    return int(value) if value not in (None, "") else None


def analyze_area_event(area_id, event, tables, valid_areas, period):
    """单个社区在单场活动中的证据链、人口口径、交通与问卷。"""
    labels = {row["area_id"]: row["area_label"] for row in tables["areas"]}
    records = [
        row for row in tables["participation"]
        if row["event_id"] == event["event_id"] and row["area_id"] == area_id
    ]
    funnel = {stage: None for stage in STAGES}
    for row in records:
        stage = row["stage"]
        headcount = _int_or_none(row["headcount"])
        if headcount is not None:
            funnel[stage] = (funnel[stage] or 0) + headcount
    groups = sorted({row["group_token"] for row in records if row["group_token"]})

    pop_row = population_for(tables["population"], area_id, period)
    residents = _int_or_none(pop_row["residents"]) if pop_row else None
    pop_status = pop_row["source_status"] if pop_row else None

    acc_row = access_for(tables["access"], area_id, period)
    travel_min = _int_or_none(acc_row["travel_min"]) if acc_row else None
    access = {
        "mode": acc_row["transport_mode"] if acc_row else None,
        "travel_min": travel_min,
        "tier": access_tier(travel_min),
        "source": acc_row["service_source"] if acc_row else None,
    }

    survey_row = survey_for(tables["surveys"], area_id, event["event_id"])
    survey = None
    if survey_row:
        respondents = _int_or_none(survey_row["respondents"])
        interested = _int_or_none(survey_row["interested_count"])
        survey = {
            "respondents": respondents,
            "interested_count": interested,
            "interest_share": (interested / respondents) if respondents else None,
            "form_version": survey_row["form_version"],
        }

    attended = funnel["attended"]
    attendance_rate = (attended / residents) if (attended is not None and residents) else None

    flags = []
    if area_id not in valid_areas:
        flags.append("边界口径不一致：该社区在活动当日不属于有效边界版本")
    if pop_row is None:
        flags.append(f"缺少 {period} 人口资料")
    elif pop_status != "confirmed":
        flags.append("人口为估计值，覆盖率分母不确定")
    missing = [STAGE_LABELS[s] for s in STAGES if funnel[s] is None]
    if missing:
        flags.append("证据链缺阶段记录：" + "、".join(missing))
    if acc_row is None:
        flags.append(f"缺少 {period} 交通资料")
    if survey is None:
        flags.append("无问卷记录")
    elif survey["respondents"] is not None and survey["respondents"] < MIN_SURVEY_RESPONDENTS:
        flags.append(f"问卷样本过少（n={survey['respondents']}），比例不稳定")
    if groups:
        flags.append("到场为团体整批记录（" + "、".join(groups) + "）")

    return {
        "area_id": area_id,
        "area_label": labels.get(area_id, area_id),
        "event_id": event["event_id"],
        "service_date": event["service_date"],
        "topic": event["topic"],
        "population": residents,
        "population_status": pop_status,
        "population_period": period if pop_row else None,
        "access": access,
        "funnel": funnel,
        "groups": groups,
        "attendance_rate": attendance_rate,
        "survey": survey,
        "flags": flags,
    }


def analyze_event(event, tables):
    """单场活动：按活动当日有效边界解释，容量与到场在活动层面汇总。"""
    day = date.fromisoformat(event["service_date"])
    period = quarter_of(day)
    valid_areas = areas_valid_on(tables["areas"], day)
    versions = sorted(set(valid_areas.values()))
    boundary_version = versions[0] if len(versions) == 1 else None

    venues = [v for v in tables["venues"] if v["event_id"] == event["event_id"]]
    capacity = sum(int(v["capacity"]) for v in venues if v["capacity"])
    venue_areas = [v["area_id"] for v in venues]

    area_ids = sorted(
        {r["area_id"] for r in tables["participation"] if r["event_id"] == event["event_id"]}
        | set(venue_areas)
    )
    areas = [analyze_area_event(a, event, tables, valid_areas, period) for a in area_ids]

    attended_values = [a["funnel"]["attended"] for a in areas if a["funnel"]["attended"] is not None]
    attended_total = sum(attended_values) if attended_values else None
    utilization = (attended_total / capacity) if (attended_total is not None and capacity) else None

    consistency = []
    if len(versions) != 1:
        consistency.append(f"活动当日有效边界版本不唯一：{versions or '无'}")
    for a in venue_areas:
        if a not in valid_areas:
            consistency.append(f"场地所在社区 {a} 在活动当日不属于有效边界版本")
    for a in {r["area_id"] for r in tables["participation"] if r["event_id"] == event["event_id"]}:
        if a not in valid_areas:
            consistency.append(f"参与记录社区 {a} 在活动当日不属于有效边界版本")

    return {
        "event_id": event["event_id"],
        "service_date": event["service_date"],
        "topic": event["topic"],
        "period": period,
        "boundary_version": boundary_version,
        "as_of_note": (
            f"按活动当日有效边界 {boundary_version} 解释；"
            "此后的边界或资料更正不影响本段。"
            if boundary_version else "活动当日无法确定有效边界版本。"
        ),
        "venues": [
            {"venue_id": v["venue_id"], "area_id": v["area_id"], "capacity": int(v["capacity"])}
            for v in venues
        ],
        "capacity": capacity,
        "attended_total": attended_total,
        "utilization": utilization,
        "areas": areas,
        "consistency_issues": consistency,
    }


# ------------------------------------------------------------------ 差异比较

def compare_pair(version, a, b):
    """同一边界口径期内两个社区的覆盖比较，列出所有阻碍“兴趣推断”的因素。"""
    confounders = []
    if a["access"]["tier"] != b["access"]["tier"]:
        confounders.append(
            f"交通层级不同（{a['area_id']} {a['access']['tier']}／{b['area_id']} {b['access']['tier']}），"
            "到场率差异可能反映可达性而非兴趣"
        )
    if a["access"]["mode"] != b["access"]["mode"]:
        confounders.append(
            f"交通方式不同（{a['area_id']} {a['access']['mode']}／{b['area_id']} {b['access']['mode']}），"
            "有无集体接送影响谁能到场"
        )
    if a["population_status"] != "confirmed" or b["population_status"] != "confirmed":
        confounders.append("人口底数含估计值，覆盖率分母不确定")
    fa = a["survey"]["form_version"] if a["survey"] else None
    fb = b["survey"]["form_version"] if b["survey"] else None
    if fa != fb:
        confounders.append(f"问卷版本不同（{fa or '无'}／{fb or '无'}）")
    for area in (a, b):
        s = area["survey"]
        if s and s["respondents"] is not None and s["respondents"] < MIN_SURVEY_RESPONDENTS:
            confounders.append(f"{area['area_id']} 问卷样本过少（n={s['respondents']}）")
    if any(a["funnel"][s] is None for s in STAGES) or any(b["funnel"][s] is None for s in STAGES):
        confounders.append("证据链阶段缺失，邀请到到场的转化情况未知")
    if a["groups"] or b["groups"]:
        confounders.append("到场为团体整批记录，不能当作独立个体意愿")

    return {
        "boundary_version": version,
        "areas": [a["area_id"], b["area_id"]],
        "attendance_rates": {a["area_id"]: a["attendance_rate"], b["area_id"]: b["attendance_rate"]},
        "confounders": confounders,
        "can_infer_interest": not confounders,
        "conclusion": (
            "在所列因素一致的范围内可谨慎比较，但仍非兴趣的直接测量。"
            if not confounders
            else "上述因素未排除，到场率差异不能据此推断为兴趣高低。"
        ),
    }


def compare_within_versions(event_reports):
    by_version = {}
    for rep in event_reports:
        by_version.setdefault(rep["boundary_version"], []).append(rep)
    comparisons = []
    for version, reps in sorted(by_version.items()):
        served = [a for rep in reps for a in rep["areas"]]
        for i in range(len(served)):
            for j in range(i + 1, len(served)):
                comparisons.append(compare_pair(version, served[i], served[j]))
    return comparisons


# ------------------------------------------------------------------ 当前规划

def translated_history(tables, event_reports, area_id, current_version):
    """把以往口径下的到场按边界重叠系数折算到当前社区（近似）。"""
    out = []
    for row in tables["boundary_crosswalk"]:
        if row["to_area"] != area_id:
            continue
        fraction = float(row["overlap_fraction"])
        for rep in event_reports:
            if rep["boundary_version"] in (None, current_version):
                continue
            for area in rep["areas"]:
                if area["area_id"] == row["from_area"] and area["funnel"]["attended"] is not None:
                    out.append({
                        "event_id": rep["event_id"],
                        "from_area": row["from_area"],
                        "from_version": row["from_version"],
                        "overlap_fraction": fraction,
                        "attended_equivalent": area["funnel"]["attended"] * fraction,
                    })
    return out


def recommend_for_area(ctx, era_median):
    """依据当前口径下的覆盖、交通与资料状态给出外展建议。规则透明、逐条可溯。"""
    recs = []
    utils = [h["utilization"] for h in ctx["service_history"] if h["utilization"] is not None]
    if any(u >= HIGH_UTILIZATION for u in utils):
        recs.append({
            "action": "增加场次或提高容量",
            "reason": f"已有场次容量利用率达到 {max(utils):.0%}（≥{HIGH_UTILIZATION:.0%}）",
        })
    access = ctx["access"]
    if access["travel_min"] is not None and access["travel_min"] > FAR_TRAVEL_MIN:
        if access["mode"] == "group_bus":
            recs.append({
                "action": "维持集体接送投入",
                "reason": f"路程约 {access['travel_min']} 分钟且已依赖集体接送",
            })
        else:
            recs.append({
                "action": "试点集体接送",
                "reason": f"路程约 {access['travel_min']} 分钟且仅有 {access['mode']}，交通不便",
            })
        recs.append({
            "action": "保留本地设点",
            "reason": "交通不便，避免仅集中在近便地点设点而漏掉该社区",
        })
    coverages = [h["attendance_rate"] for h in ctx["service_history"] if h["attendance_rate"] is not None]
    survey = ctx["survey"]
    interest_signal = survey is None or (
        survey["interest_share"] is not None and survey["interest_share"] >= INTEREST_SUPPORT
    )
    if era_median is not None and coverages and min(coverages) < era_median and interest_signal:
        recs.append({
            "action": "列为优先外展对象",
            "reason": "覆盖低于同期中位，且问卷未显示兴趣不足",
        })
    if ctx["population_status"] != "confirmed":
        recs.append({"action": "核实人口底数", "reason": "当前人口为估计值，影响覆盖率判断"})
    if survey is None or (survey["respondents"] or 0) < MIN_SURVEY_RESPONDENTS:
        recs.append({
            "action": "补充问卷调查",
            "reason": "问卷缺失或样本过少，需求信号不足",
        })
    return recs


def build_current_plan(tables, event_reports, planning_date):
    """下一轮外展建议：一律使用规划日有效的边界口径与当期资料。"""
    valid_areas = areas_valid_on(tables["areas"], planning_date)
    versions = sorted(set(valid_areas.values()))
    current_version = versions[-1] if versions else None
    period = quarter_of(planning_date)
    labels = {row["area_id"]: row["area_label"] for row in tables["areas"]}

    # 当前口径期内各社区覆盖率中位数，用于识别相对偏低。
    era_rates = [
        a["attendance_rate"]
        for rep in event_reports if rep["boundary_version"] == current_version
        for a in rep["areas"] if a["attendance_rate"] is not None
    ]
    era_median = statistics.median(era_rates) if era_rates else None

    plan_areas = []
    for area_id in sorted(valid_areas):
        pop_row = population_for(tables["population"], area_id, period)
        acc_row = access_for(tables["access"], area_id, period)
        travel_min = _int_or_none(acc_row["travel_min"]) if acc_row else None

        history = []
        surveys_seen = []
        for rep in event_reports:
            if rep["boundary_version"] != current_version:
                continue
            for area in rep["areas"]:
                if area["area_id"] != area_id:
                    continue
                util = rep["utilization"]
                history.append({
                    "event_id": rep["event_id"],
                    "service_date": rep["service_date"],
                    "topic": rep["topic"],
                    "attended": area["funnel"]["attended"],
                    "attendance_rate": area["attendance_rate"],
                    "utilization": util,
                })
                if area["survey"]:
                    surveys_seen.append(area["survey"])

        translated = translated_history(tables, event_reports, area_id, current_version)
        survey = surveys_seen[-1] if surveys_seen else None

        ctx = {
            "area_id": area_id,
            "population_status": pop_row["source_status"] if pop_row else None,
            "access": {
                "mode": acc_row["transport_mode"] if acc_row else None,
                "travel_min": travel_min,
                "tier": access_tier(travel_min),
            },
            "service_history": history,
            "survey": survey,
        }
        recs = recommend_for_area(ctx, era_median)
        if not history:
            recs.insert(0, {"action": "补做基线服务记录", "reason": "当前口径下尚无服务记录"})

        cautions = []
        if (
            ctx["access"]["tier"] == "不便"
            and era_median is not None
            and any(h["attendance_rate"] is not None and h["attendance_rate"] < era_median for h in history)
        ):
            cautions.append("到场率偏低与交通不便同现，不能解读为兴趣低。")
        if translated:
            cautions.append("历史折算基于重叠系数并假设原社区内人口均匀分布，仅为近似。")

        confidence = "中"
        if ctx["population_status"] != "confirmed":
            confidence = "低"

        plan_areas.append({
            "area_id": area_id,
            "area_label": labels.get(area_id, area_id),
            "population": _int_or_none(pop_row["residents"]) if pop_row else None,
            "population_status": ctx["population_status"],
            "access": ctx["access"],
            "service_history": history,
            "translated_history": translated,
            "translated_attended_total": sum(t["attended_equivalent"] for t in translated) if translated else None,
            "survey": survey,
            "recommendations": recs,
            "confidence": confidence,
            "cautions": cautions,
        })

    return {
        "planning_date": planning_date.isoformat(),
        "boundary_version": current_version,
        "period": period,
        "era_median_attendance_rate": era_median,
        "note": "当前计划使用规划日有效边界口径；历史活动仍按当时口径解释。",
        "areas": plan_areas,
    }


# ------------------------------------------------------------------ 资料缺口

def collect_data_gaps(tables, event_reports):
    gaps = []
    estimated = sorted({
        (row["area_id"], row["period"])
        for row in tables["population"] if row["source_status"] != "confirmed"
    })
    if estimated:
        gaps.append("人口为估计值：" + "、".join(f"{a}（{p}）" for a, p in estimated))
    small = [
        f"{row['area_id']}／{row['event_id']}（n={row['respondents']}，{row['form_version']}）"
        for row in tables["surveys"]
        if _int_or_none(row["respondents"]) is not None
        and int(row["respondents"]) < MIN_SURVEY_RESPONDENTS
    ]
    if small:
        gaps.append(f"问卷样本过少（<{MIN_SURVEY_RESPONDENTS}）：" + "、".join(small))
    forms = sorted({row["form_version"] for row in tables["surveys"]})
    if len(forms) > 1:
        gaps.append("问卷版本不一致（" + "、".join(forms) + "），跨期“希望参加”比例不可直接比较")
    for rep in event_reports:
        for area in rep["areas"]:
            missing = [STAGE_LABELS[s] for s in STAGES if area["funnel"][s] is None]
            if missing:
                gaps.append(f"{rep['event_id']}／{area['area_id']} 缺阶段记录：" + "、".join(missing))
    invited_events = {row["event_id"] for row in tables["participation"] if row["stage"] == "invited"}
    all_events = {row["event_id"] for row in tables["events"]}
    no_invite = sorted(all_events - invited_events)
    if no_invite:
        gaps.append("缺邀请记录，无法评估邀请覆盖：" + "、".join(no_invite))
    gaps.append("报名来源（个人主动／村社集中报送）未在资料中区分，主动报名与集中报送不可拆分")
    modes = {}
    for row in tables["access"]:
        modes.setdefault(row["area_id"], set()).add(row["transport_mode"])
    if modes and all(len(v) == 1 for v in modes.values()):
        gaps.append("交通资料每社区仅单一方式，且来源为 " + "、".join(sorted({r["service_source"] for r in tables["access"]})))
    for rep in event_reports:
        gaps.extend(f"{rep['event_id']}：{issue}" for issue in rep["consistency_issues"])
    return gaps


def feedback_comparability(event_reports):
    """供教研团队判断课程反馈可比性的分层信息（资料中不含反馈本身）。"""
    strata = []
    for rep in event_reports:
        strata.append({
            "event_id": rep["event_id"],
            "boundary_version": rep["boundary_version"],
            "access_tiers": {a["area_id"]: a["access"]["tier"] for a in rep["areas"]},
            "groups": sorted({g for a in rep["areas"] for g in a["groups"]}),
            "funnel_complete": all(a["funnel"][s] is not None for a in rep["areas"] for s in STAGES),
            "survey_forms": sorted({a["survey"]["form_version"] for a in rep["areas"] if a["survey"]}),
        })
    return {
        "note": "课程反馈差异需在同一交通层级内比较，并注意团体构成与问卷版本；本资料不含反馈数据，仅提供分层。",
        "strata": strata,
    }


# ------------------------------------------------------------------ 汇总

def boundary_version_summary(tables):
    versions = {}
    for row in tables["areas"]:
        entry = versions.setdefault(row["boundary_version"], {
            "version": row["boundary_version"],
            "valid_from": row["valid_from"],
            "valid_to": row["valid_to"] or None,
            "areas": [],
        })
        entry["valid_from"] = min(entry["valid_from"], row["valid_from"])
        if row["valid_to"]:
            entry["valid_to"] = row["valid_to"] if entry["valid_to"] is None else max(entry["valid_to"], row["valid_to"])
        entry["areas"].append({"area_id": row["area_id"], "area_label": row["area_label"]})
    return [versions[v] for v in sorted(versions)]


def analyze(tables, planning_date):
    event_reports = [analyze_event(e, tables) for e in sorted(tables["events"], key=lambda e: e["service_date"])]
    return {
        "generated_on": date.today().isoformat(),
        "planning_date": planning_date.isoformat(),
        "boundary_versions": boundary_version_summary(tables),
        "events": event_reports,
        "comparisons": compare_within_versions(event_reports),
        "cross_boundary_note": (
            "边界版本已调整，前后社区不是同一范围；跨期比较只能经重叠系数近似折算，"
            "并假设原社区内人口均匀分布，不作为精确依据。"
        ),
        "current_plan": build_current_plan(tables, event_reports, planning_date),
        "data_gaps": collect_data_gaps(tables, event_reports),
        "inference_cautions": list(INFERENCE_CAUTIONS),
        "feedback_comparability": feedback_comparability(event_reports),
    }


# ------------------------------------------------------------------ 文本报告

def pct(value):
    return "无数据" if value is None else f"{value:.1%}"


def render_text(report):
    lines = []
    add = lines.append
    add("社区服务覆盖差异分析报告")
    add(f"生成日期：{report['generated_on']}　规划口径日期：{report['planning_date']}"
        f"（当前边界版本 {report['current_plan']['boundary_version']}）")
    add("")
    add("一、口径说明")
    for v in report["boundary_versions"]:
        areas = "、".join(f"{a['area_id']} {a['area_label']}" for a in v["areas"])
        add(f"  边界版本 {v['version']}：{v['valid_from']} 至 {v['valid_to'] or '今'}（{areas}）")
    add("  历史活动按活动当日有效边界解释；当前规划使用规划日有效边界。")
    add("  表达意向、完成报名、实际到场分别列示，不合并为单一需求数。")
    add("")
    add("二、各活动服务覆盖（按当时口径）")
    for rep in report["events"]:
        add(f"  {rep['event_id']} {rep['service_date']} {rep['topic']}（边界口径 {rep['boundary_version']}）")
        venues = "、".join(f"{v['venue_id']}@{v['area_id']}" for v in rep["venues"])
        add(f"    场地 {venues}，容量 {rep['capacity']}；实际到场 "
            f"{rep['attended_total'] if rep['attended_total'] is not None else '无记录'}，"
            f"容量利用率 {pct(rep['utilization'])}")
        for area in rep["areas"]:
            pop = area["population"]
            pop_txt = "无资料" if pop is None else f"{pop}（{'已确认' if area['population_status'] == 'confirmed' else '估计值'}）"
            acc = area["access"]
            acc_txt = "无资料" if acc["travel_min"] is None else f"{acc['mode']} 约 {acc['travel_min']} 分钟（{acc['tier']}）"
            add(f"    {area['area_id']} {area['area_label']}：人口 {pop_txt}；交通 {acc_txt}")
            chain = "；".join(
                f"{STAGE_LABELS[s]} {area['funnel'][s] if area['funnel'][s] is not None else '无记录'}"
                for s in STAGES
            )
            add(f"      证据链：{chain}")
            add(f"      到场率 {pct(area['attendance_rate'])}"
                + (f"（{area['funnel']['attended']}/{pop}）" if area["attendance_rate"] is not None else ""))
            s = area["survey"]
            if s:
                add(f"      问卷（{s['form_version']}）：{s['respondents']} 人回应，"
                    f"{s['interested_count']} 人希望参加（{pct(s['interest_share'])}）")
            else:
                add("      问卷：无记录")
            for flag in area["flags"]:
                add(f"      标记：{flag}")
        for issue in rep["consistency_issues"]:
            add(f"    一致性问题：{issue}")
        add("")
    add("三、覆盖差异比较与不可推断项")
    for comp in report["comparisons"]:
        a, b = comp["areas"]
        rates = comp["attendance_rates"]
        add(f"  {comp['boundary_version']} 期内 {a} 与 {b}：到场率 {pct(rates[a])} 对 {pct(rates[b])}")
        for c in comp["confounders"]:
            add(f"    不可推断因素：{c}")
        add(f"    结论：{comp['conclusion']}")
    add(f"  跨期比较：{report['cross_boundary_note']}")
    add("")
    plan = report["current_plan"]
    add(f"四、下一轮外展建议（当前口径 {plan['boundary_version']}，{plan['period']}）")
    for area in plan["areas"]:
        acc = area["access"]
        acc_txt = "无资料" if acc["travel_min"] is None else f"{acc['mode']} 约 {acc['travel_min']} 分钟（{acc['tier']}）"
        pop_txt = "无资料" if area["population"] is None else f"{area['population']}（{'已确认' if area['population_status'] == 'confirmed' else '估计值'}）"
        add(f"  {area['area_id']} {area['area_label']}：人口 {pop_txt}；交通 {acc_txt}")
        for h in area["service_history"]:
            add(f"    服务记录：{h['event_id']}（{h['service_date']}）到场 "
                f"{h['attended'] if h['attended'] is not None else '无记录'}，"
                f"到场率 {pct(h['attendance_rate'])}，容量利用率 {pct(h['utilization'])}")
        if area["translated_attended_total"] is not None:
            add(f"    按现口径折算的历史到场约 {area['translated_attended_total']:.1f} 人"
                "（近似，假设原社区内人口均匀分布）")
        s = area["survey"]
        if s:
            add(f"    问卷（{s['form_version']}）：{s['respondents']} 人回应，"
                f"{s['interested_count']} 人希望参加（{pct(s['interest_share'])}）")
        for rec in area["recommendations"]:
            add(f"    建议：{rec['action']}——{rec['reason']}")
        for caution in area["cautions"]:
            add(f"    注意：{caution}")
        add(f"    建议信心：{area['confidence']}")
    add("")
    add("五、资料覆盖不足")
    for gap in report["data_gaps"]:
        add(f"  - {gap}")
    add("")
    add("六、教研可比性提示")
    add(f"  {report['feedback_comparability']['note']}")
    for stratum in report["feedback_comparability"]["strata"]:
        tiers = "、".join(f"{k} {v}" for k, v in stratum["access_tiers"].items())
        add(f"  {stratum['event_id']}（{stratum['boundary_version']}）：交通层级 {tiers}；"
            f"团体 {len(stratum['groups'])} 个；证据链{'完整' if stratum['funnel_complete'] else '不完整'}；"
            f"问卷版本 {'、'.join(stratum['survey_forms']) or '无'}")
    add("")
    add("七、推断注意事项")
    for caution in report["inference_cautions"]:
        add(f"  - {caution}")
    return "\n".join(lines)


# ------------------------------------------------------------------ 入口

def main(argv=None):
    parser = argparse.ArgumentParser(description="社区服务覆盖差异分析与外展安排支持工具")
    parser.add_argument("--data", default=str(ROOT / "data"), help="输入资料目录（默认 ./data）")
    parser.add_argument("--planning-date", default=date.today().isoformat(),
                        help="规划口径日期 YYYY-MM-DD（默认今天）")
    parser.add_argument("--json", dest="json_path", default=None, help="同时导出 JSON 到该路径")
    args = parser.parse_args(argv)

    tables = load_tables(args.data)
    report = analyze(tables, date.fromisoformat(args.planning_date))
    print(render_text(report))
    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\nJSON 已写入 {args.json_path}")


if __name__ == "__main__":
    main()
