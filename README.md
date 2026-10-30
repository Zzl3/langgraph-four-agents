# 素材审核与发布排期 Agent

小影校招 AI Agent 开发考核第 1 题。项目帮助内容团队审核待处理素材、核对发布时段并生成可确认的排期草稿。只保存本地草稿，不接入发布系统。

## 运行

Python 3.10+。默认离线 mock，不需要密钥或模型服务：

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m src.cli --input examples/inputs/schedule_conflict.json
python -m pytest -q
```

可用 `--model real` 接入 OpenAI 兼容 Chat Completions JSON 服务；需设置 `OPENAI_API_KEY`、`OPENAI_MODEL`，以及可选 `OPENAI_BASE_URL`。本项目当前交付不要求启动 Ollama 或其他本地模型。模型服务不可用时应使用默认离线 mock；mock 验证流程，不代表模型决策效果。

可调参数：`--max-calls 20` 限制工具调用次数；`--runtime runtime` 指定本地 SQLite 草稿库；`--trace-file /tmp/run.jsonl` 写入增量轨迹（目标文件必须不存在）。输入支持单素材或 `items` 多素材，格式见 `examples/inputs/`。

## 设计摘要

每个素材按需读取、审核、查询时段，再保存草稿。工具结果进入下一轮决策；占用冲突可重新查询并建议空闲时段，但改动原要求必须标记 `pending_confirmation`。SQLite 以原始请求做幂等键，同一请求重复提交只保留一份；并发占用会拒绝写入并触发重新查询。工具证据校验最终状态；调用上限返回 `incomplete` 和已完成步骤。

LangGraph 1.2.x `StateGraph`、状态更新和条件边承担执行循环；工具由本地 Python 函数实现。离线策略明确标为 mock。框架不负责业务规则或证据校验。

## 评审入口

- [需求与验收](docs/01-requirements.md)
- [设计与取舍](docs/02-solution.md)
- [评估、复现命令与已知限制](docs/03-evaluation.md)
- [AI 使用记录](docs/04-ai-usage.md)
- [复盘](docs/05-retrospective.md)
- 输入和实际离线输出：[`examples/inputs/`](examples/inputs/)、[`examples/outputs/`](examples/outputs/)
- 测试：[`tests/`](tests/)
- 依赖版本范围：[`requirements.txt`](requirements.txt)；无密钥模板：[`.env.example`](.env.example)

## 提交信息

- 题号：1，素材审核与发布排期 Agent
- GitHub：https://github.com/Zzl3/langgraph-four-agents
- 最终 commit SHA：提交前取 `git rev-parse HEAD`
- 投入时长、候选人个人贡献和人工复核须由提交者据实填写；AI 辅助范围见 `docs/04-ai-usage.md`。
