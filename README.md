# 社区服务覆盖分析

本地演练资料记录合成社区分界版本、活动场地、报名邀请、集体接送和实际到场。表达意愿、完成报名与现场参加分别记录。

## Local checks

Python 3.9 or newer is sufficient. The repository has no third-party runtime dependencies.

Run `python3 input_audit.py` to verify the declared files and columns. Run `python3 -m unittest discover -s tests -v` to check the prepared input contract.

The records are synthetic fixtures. Empty values and status labels retain their literal input meaning. The manifest lists file columns and the stable row identifier used by the input audit.
