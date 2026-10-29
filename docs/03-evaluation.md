# 评估与验证

## 可复现的离线验证

运行 `.venv/bin/python -m pytest -q`。本次结果为 75 passed；测试结果保存在 [`examples/test-results.txt`](../examples/test-results.txt)，输入在 `examples/inputs/`，完整 JSON 结果和逐步工具轨迹在 `examples/outputs/`。`python -m examples.generate` 可重新生成离线规则模式记录；`python -m examples.http_demo` 生成四题（排期两条路径）的本地模拟 HTTP 服务记录，不是真实模型。

离线用例按四题覆盖正常、缺失、风险/冲突、工具失败、重复提交或重规划等边界。它们验证工具约束和最终结果校验，不证明模型推理能力。题目要求的实际结果与轨迹须区分模型来源，不能将 mock 或模拟 HTTP 输出称为真实模型结果。

模拟 OpenAI HTTP 回路的五份 JSON 记录为 `http_schedule_busy.json`、`http_schedule_free.json`、`http_plan_normal.json`、`http_qa_conflict.json`、`http_video_normal.json`。它们展示工具观察怎样改变下一动作，但决策由本地 mock 规则生成；不能作为真实模型 ReAct 证据。

## 本地真实模型试跑

本机 Ollama 的 `qwen2:latest`（7.6B Q4_0）通过 OpenAI 兼容接口执行，真实模型决策的每步轨迹分别保存在以下文件；每条记录均带 `evaluation.provider/model/note`：

- [`real_qwen2_schedule_conflict.json`](../examples/outputs/real_qwen2_schedule_conflict.json)：成功。模型读素材、检查规则、查到 18:00 占用，选择 19:00 并保存为待确认草稿；`published=false`。
- [`real_qwen2_schedule_free_retry.json`](../examples/outputs/real_qwen2_schedule_free_retry.json)：成功。模型首次确认字段错误，观察到工具错误后修正为 `null`，保存原要求 20:00；`published=false`。
- [`real_qwen2_plan_retry2.json`](../examples/outputs/real_qwen2_plan_retry2.json)：失败。模型选择费用 12、超出预算 10 的方案；工具拒绝后未重规划，最终答案被证据校验器拒绝。
- [`real_qwen2_qa_conflict_after_prompt.json`](../examples/outputs/real_qwen2_qa_conflict_after_prompt.json)：失败。检索到 D01、D02 但只读取 D01，最终冲突结论因缺少另一份原文被拒绝。
- [`real_qwen2_video_normal_after_prompt.json`](../examples/outputs/real_qwen2_video_normal_after_prompt.json)：失败。模型修正了 `source_ids` 参数后，仍未按品牌检查结果将“支持字幕”及 P01 来源写入分镜，重复检查至调用上限。
- [`real_qwen2_qa_conflict_guarded.json`](../examples/outputs/real_qwen2_qa_conflict_guarded.json)：加入未读文档清单和重复读取保护后的失败记录。模型仍未形成通过校验的冲突答案。
- [`real_qwen2_video_normal_guarded.json`](../examples/outputs/real_qwen2_video_normal_guarded.json)：加入明确的逐字修正建议和重复检查保护后的失败记录。模型仍未将“支持字幕”及 P01 写入分镜。
- [`real_llama3_video_normal.json`](../examples/outputs/real_llama3_video_normal.json)：使用本机另一款 Llama 3.2 模型复核，首轮检索成功后返回无效 JSON，被执行器安全终止。

较早失败轨迹也保留在同目录中，可对照提示和错误反馈改进前后的行为。真实模型实测证明两条排期轨迹能根据工具观察调整；另外三题的试跑明确未通过，故不能声称四题均已满足真实模型动态决策要求，也不能把局部成功外推为可靠成功率。测试和实测均使用合成业务资料。

复跑示例（需本地 Ollama 服务和已下载模型；不需要向仓库写密钥）：

```sh
OPENAI_API_KEY=ollama OPENAI_BASE_URL=http://127.0.0.1:11434/v1 OPENAI_MODEL=qwen2:latest \
  python -m src.cli schedule --input examples/inputs/schedule_conflict.json --model real \
  --trace-file /tmp/schedule.jsonl --runtime /tmp/schedule-runtime
```

## 局限

- 离线策略是确定性参考实现；本地真实模型是单机小模型、少量手工样例，不构成统计评估。
- 规划、问答、视频真实模型试跑失败；需要进一步修复工具协议提示、结果反馈和规划/改写能力，再做多路径复测。
- FAQ 召回、冲突识别、时长估算和品牌审查均是有限规则，无法保证语义完备或真实录音时长。
- SQLite 只协调同一 runtime 文件；不连接线上发布、费用或文档系统，也不保证跨机器并发。

## 独立候选人视角复核

2026-10-29 使用 GPT-6.1 Terra 做只读第二模型评审并复核；没有启动 Ollama，也没有把评审结论当作运行测试。评审指出问答数字冲突匹配过窄、`insufficient` 可能掩盖已经读到的冲突、规划缺口报告未扣除已耗时，以及不同适用范围可能因词汇重叠而误判。对应修复已加入源码和回归测试：冲突启发式把数字绑定到局部属性，并比较词表识别出的适用范围；范围标签冲突或属性不明时不据此判冲突。未覆盖的范围词、同义表达和复杂适用条件仍可能导致漏报或误报，因此这些词汇规则不能验证完整语义。最终答案若请求不足但已读材料与问题相关且规则可检测出冲突，则返回带双份引用的 `conflict`；保存约束与缺口报告共用剩余时限计算，并覆盖相对与绝对截止时间。后续基于失败轨迹又补充了规划各选项状态和视频品牌失败后的分阶段修订提示，并由 HTTP 契约测试验证提示随观察更新；第二模型指出规划脚本实际与选项无关，以及视频提示需跟踪估时阶段、聚合先前检索事实，均已调整并覆盖回归测试。这仍是离线模拟，不能替代真实模型复测。评审也确认候选人个人贡献、实际时长和人工复核无法从仓库推断，仍需本人据实补充，见 `docs/04-ai-usage.md`。
