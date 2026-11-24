# 社区服务覆盖分析

本地演练资料记录合成社区分界版本、活动场地、报名邀请、集体接送和实际到场。表达意愿、完成报名与现场参加分别记录。

## Local checks

Python 3.9 or newer is sufficient. The repository has no third-party runtime dependencies.

Run `python3 input_audit.py` to verify the declared files and columns. Run `python3 -m unittest discover -s tests -v` to check the prepared input contract.

The records are synthetic fixtures. Empty values and status labels retain their literal input meaning. The manifest lists file columns and the stable row identifier used by the input audit.

## 覆盖分析工具

`coverage_analysis.py` 比较各社区服务覆盖差异并支持下一轮外展安排：

- 历史活动按活动当日有效边界版本解释；当前覆盖与建议按规划日有效边界（当前口径）计算。边界或接送资料更正后，当前建议随之更新，历史段落不变。
- 表达意向、完成报名、实际到场分别列示，缺失记为“无记录”，绝不合并为单一需求数；签到总人数不用于需求排序。
- 覆盖差异连同交通条件、人口口径（估计/已确认）、问卷版本与样本量一起呈现，并列出不能据此推断为兴趣高低的因素。
- 输出下一轮外展建议（场次与容量、集体接送、设点、优先对象、需补核的资料）以及资料覆盖不足清单。

用法：

```
python3 coverage_analysis.py                      # 打印中文文本报告
python3 coverage_analysis.py --json report.json   # 同时导出机器可读 JSON
python3 coverage_analysis.py --planning-date 2026-11-24 --data data
```
