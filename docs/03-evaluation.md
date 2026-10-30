# 评估与验证

## 可复现离线验证

默认运行不需要模型服务：

```sh
python -m src.cli --input examples/inputs/schedule_conflict.json
python -m src.cli --input examples/inputs/schedule_normal.json
python -m src.cli --input examples/inputs/schedule_failure.json
python -m pytest -q
```

输入、结果与逐步 Trace 分别保存在 `examples/inputs/` 和 `examples/outputs/`；当前离线结果汇总在 `examples/test-results.txt`。覆盖原时段空闲、时段冲突及待确认、素材信息缺失、规则风险、重复提交、保存失败、调用上限、多素材部分失败和并发占位冲突。`python -m examples.generate` 可重新生成主要离线记录。

`examples/outputs/http_schedule_busy.json` 与 `http_schedule_free.json` 由真实 OpenAI SDK 经过本地模拟 HTTP 服务生成。它们展示了不同工具观察对应不同后续动作，但动作由本地确定性模拟器产生，不是大模型能力证据。

## 历史真实模型试跑

暂停本地模型后不再启动或运行 Ollama。仓库保留了先前生成的两条真实 Qwen 排期轨迹，作为历史验证记录：

- [`real_qwen2_schedule_conflict.json`](../examples/outputs/real_qwen2_schedule_conflict.json)：历史运行成功。模型查询到 18:00 占用后选择 19:00，并保存为待确认草稿；未发布。
- [`real_qwen2_schedule_free_retry.json`](../examples/outputs/real_qwen2_schedule_free_retry.json)：历史运行成功。模型观察到保存参数错误后修正参数，保留原要求 20:00 并保存草稿；未发布。

这些记录来自早先一次本地实测，输入为合成资料。它们提供两条按工具观察改变动作的实际模型轨迹，但样本数很少，不代表稳定成功率。当前复现与测试步骤均不需要、也不会启动 Ollama。

## 结果解释与限制

- 单元和端到端测试验证程序约束、Mock 流程、工具失败处理、轨迹和 SQLite 幂等，不等价于模型评测。
- 本地 HTTP 模拟器验证 OpenAI SDK 请求和观察传递，不证明模型决策质量。
- 两条历史真实轨迹仅覆盖排期路径中的一种模型、有限输入和合成工具结果。
- 审核为有限规则；真实部署前仍需接入权威审核规则与排期系统，并补充权限、审计和外部并发处理。

## 独立评审

独立 agent 以候选人评审视角只读复核了收敛后的工作树。结论：单题入口、验收材料和 mock/HTTP/历史真实模型边界说明通过有限样例审查；未发现阻断性排期实现残留。评审确认不能把两条历史 Qwen 轨迹外推为当前重构版的重新实测，也未运行测试或模型。27 项测试结果来自本仓库本地运行记录。评审还要求在提交后提供最终 SHA，并将提交者实际用时、个人贡献与人工复核情况据实填写；这些个人事实无法由代码评审推断。
