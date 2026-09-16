# 中药饮片炮制放行

该项目表达物料转换、工艺参数、偏差和质量放行资料。拆分、合批、损耗与返工均保留前后批次关系，签署后的过程记录通过偏差补充。

`process/contracts.py` 定义批次和工序结构，`fixtures/processing_batch.json` 提供一批脱敏的酒炙饮片记录。Python 3.11 环境可运行 `python -m compileall process` 检查语法。
