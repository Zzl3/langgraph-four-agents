# 四个 LangGraph Agent（评估中）

本仓库分别实现素材排期、内容规划、企业知识问答、短视频脚本，题目原文和统一验收标准见 [题目.txt](题目.txt)。离线流程与测试覆盖四题；本地 Ollama/Qwen 真实模型试跑成功完成两条排期路径，但规划、问答和视频试跑仍失败。因此**项目尚未满足四题均由模型动态决策的完整验收**，不能将 mock 或模拟 HTTP 输出当作真实模型效果。实测细节及失败轨迹见 [评估文档](docs/03-evaluation.md)。

## 提交信息

- 考核范围：按用户要求实现四题；原考核规则本身允许任选一题。
- GitHub：https://github.com/Zzl3/langgraph-four-agents
- 最新提交 SHA：以仓库 `main` 分支当前 HEAD 为准；评审时可从 GitHub 仓库首页复制。
- 候选人个人贡献、实际投入时长和人工复核情况未由系统推断；请提交者据实补充 [AI 使用记录](docs/04-ai-usage.md)。

## 安装与运行

Python 3.10+。以下命令在仓库根目录执行：

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m src.cli schedule --input examples/inputs/schedule_conflict.json
python -m src.cli plan --input examples/inputs/plan_normal.json
python -m src.cli qa --input examples/inputs/qa_conflict.json
python -m src.cli video --input examples/inputs/video_normal.json
python -m pytest -q
python -m examples.generate
python -m examples.http_demo
```

`--model mock` 为默认模式。输出 JSON 含工具轨迹、模型最终提案和经证据校验后的结果；`final_decision.proposal` 保留未经校验的结束提案，便于对照是否被校验器拒绝。提案和 `model_reason` 仍可能含冗余或敏感内容；模型理由最多保留 300 字符，排期理由还会做安全过滤，非逐字留存。分享或提交轨迹前请检查这些字段及工具参数。`--max-calls N` 限制工具调用；可用 `--trace-file 路径` 增量写入 JSONL 轨迹，该路径必须不存在，续跑须另选路径。轨迹包含输入相关工具参数和返回，需按资料敏感性保管；运行中断时可能没有结束事件。`--runtime DIR` 指定 SQLite 保存位置，默认为 `runtime/`。测试产生的示例是合成数据；不连接发布系统。同一 runtime 中已保存的草稿（包括待确认草稿）会暂占其建议时段；不同 runtime 不共享占用。当前没有草稿取消或到期功能。

内容规划支持跨进程续跑：第一次使用新的 `--run-id` 保存轨迹，后续使用相同运行 ID 和 `--resume`。例如使用一个尚未存在 `resume-demo` 的 runtime：

```sh
python -m src.cli plan --input examples/inputs/plan_resume_initial.json --runtime runtime/resume --run-id resume-demo
python -m src.cli plan --input examples/inputs/plan_resume_retry.json --runtime runtime/resume --run-id resume-demo --resume
```

续跑保留成功和失败记录，工具调用上限按整个运行累计；核心输入及素材费用表必须一致，示例中的故障注入标志可移除。相对截止时间从首次运行开始计算，暂停期间也计入；运行 ID 已存在时不会覆盖。该机制仅适用于同一机器上的同一 SQLite 文件，属于可信本地运行记录。

`examples.http_demo` 会在本机启动离线模拟服务并通过真实 SDK 的 HTTP 接口生成四题的轨迹（排期两条路径），输出明确标为模拟服务，不能证明真实模型效果。记录保存在 `examples/outputs/http_*.json`。

真实模型模式使用 OpenAI 兼容 Chat Completions JSON 接口：设置 `.env.example` 中的环境变量（CLI 会加载工作区 `.env`，环境变量优先），再追加 `--model real`。例如 `cp .env.example .env` 后填写配置，运行 `python -m src.cli qa --input examples/inputs/qa_conflict.json --model real`。所选服务需支持 `response_format=json_object`。`OPENAI_TIMEOUT_SECONDS` 默认 30 秒，模型请求不自动重试；超时或其他服务错误会返回未完成并保留已产生的工具轨迹。真实模型试跑记录及其失败边界见 [评估文档](docs/03-evaluation.md)；不能据少量排期成功案例推断其他任务或总体成功率。模型服务不支持或请求失败时返回未完成。

排期支持单个 `material_id`，或通过 `items` 逐项指定 `id`、`date`、`time`；可通过 `materials` 提供按 ID 索引的素材元数据，通过 `blocked_terms` 追加审核禁词；示例见 `examples/inputs/schedule_dates.json`。规划可使用 `deadline_hours`（从现在起的可用小时数），或使用带时区的 ISO 8601 `deadline`；两者同时提供时以 `deadline` 为准。默认素材与费用表在 `data/plan.json`，可通过 `plan_data` 自定义；素材须有 ID、描述和关键词列表，方案须列出非负费用及耗时；可用 `depends_on` 指定已在列表前面出现的步骤。示例见 `examples/inputs/plan_custom.json`。

## 评审入口

- [设计](docs/02-solution.md)、[需求与已知缺口](docs/01-requirements.md)、[评估](docs/03-evaluation.md)
- [测试](tests/test_acceptance.py)、[实际离线运行轨迹](examples/outputs/)、[输入](examples/inputs/)
- [AI 使用](docs/04-ai-usage.md)、[复盘](docs/05-retrospective.md)

依赖范围见 requirements.txt；本机测试使用 LangGraph 1.2.18、OpenAI SDK 2.54.0 和 pytest 9.2.0。
