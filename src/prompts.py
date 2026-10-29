"""任务协议仅给模型约束和工具定义，不预设工具调用序列。"""
TASK_GUIDANCE = {
    'schedule': '''素材逐项读取和审核，仅已通过的素材可以查时段后保存草稿。相同请求内素材不得使用相同日期的相同时段；
原时段被占用时可建议空闲时段，必须 status=pending_confirmation，confirmation 必须精确为
“原要求 {requested_time} 不可用，建议 {suggested_time}，等待用户确认”；原时段可用时为 status=ready、confirmation=null。
草稿字段严格限定为工具描述中的字段，published 必须为 false，绝不能发布。
查询时传入素材 ID，以便排除它自己的已有草稿；保存返回 slot_conflict 时，重新查询并根据新结果调整或停止。
逐项输出 items，每项包括 id、date、requested_time、review_status、risk_reasons、missing、saved、suggested_time、confirmation；
总体包含 status（ready、pending_confirmation 或 incomplete）、items、saved、published:false。
不要宣称失败的保存成功，部分失败时保留各项真实状态。''',
    'plan': '''用户的 deadline_hours 是从现在开始的可用小时数；若输入含 deadline，则 deadline_hours 是系统计算的剩余时长，
保存计划使用原始 deadline 字段而不是 deadline_hours。先获取真实工具结果再选择计划；费用或时长超限时可估算另一选项。
只有同时满足预算和剩余时间的方案才能保存；不能满足时继续估算其他选项，全部超限则如实结束。调用 save_plan 时 args 必须严格为 {"plan":{...}}，
不能把计划对象字段放在 args 顶层，也不能自行添加字段或状态。
素材必须与用户主题关键词匹配；费用表中的各方案由检索结果的 available_options 提供。
保存所需 plan 的 cost、hours、steps 必须和某次估算完全相同，material_id 和 script 必须来自生成结果。
plan.task_step_status 按 steps 的顺序列出 {name:步骤名,status:"planned"}；保存方案不表示这些活动已经执行。
成功时 result 为 {status:"completed",plan:保存的完整计划,saved:true}；失败时说明原因和已成功步骤。''',
    'qa': '''先用 required_query 检索，再按需补充检索。next_required_actions 非空时必须从列表中选择一个动作执行，不能重新执行相同检索。required_document_reads 中每个 ID 都必须先调用 read_doc 成功读取，列表为空之前不能结束或回答；不能只读第一份。
对 search_docs 返回的每一份匹配文档，都必须读取后再判断答案。
若 evidence_status.possible_conflict=true 且 all_matches_read=true，必须以 conflict 结束、列出所有原文引用并请求确认适用范围；不得选择其中一份作为答案。
不得凭空回答；资料明确矛盾时不得自行判断优先级。
仅有不同表述、无法确认矛盾时，说明证据不足并请求补充，不得直接称为冲突。
所有引用严格采用 {id,date,excerpt:文档完整text}，按读取顺序列出。完成时 answer 必须原样引用唯一原文，
result 为 {status:"completed",answer:原文,citations:[...],gaps:[]}；冲突时 result 为
{status:"conflict",answer:"资料冲突，请确认适用制度。",citations:[...],gaps:["适用范围或优先级"]}；
无依据时 status 为 insufficient 并说明缺口。''',
    'video': '''仅使用检索到的产品事实，分镜只使用 seconds、visual、narration、source_id 四个字段，visual 只能是“文字卡片”、“竖屏文字卡片”或“纯色背景”，
事实旁白逐字等于素材原文并注明素材 ID；不含事实的旁白使用“一起看看这项功能。”、“你会怎么使用？”、“来看看这个功能！”或“你想怎么用？”，source_id 使用 null。
估算所有分镜总时长；超过目标需修改并重新估算。检查品牌时 args 必须同时提供 scenes 和 source_ids（使用本次检索命中的素材 ID 列表），并检查同一版本分镜。
若检查结果 passed=false 或 risks 非空，必须根据 risks 实际改写 scenes（事实旁白逐字采用命中素材的 fact，并把对应 ID 填入 source_id），不能重复检查相同分镜。
成功时 result 为 {status:"completed",title:用户主题,audience:用户受众,platform:用户平台,style:用户风格,scenes:已估时且通过检查的分镜,seconds:估算秒数,estimated_scene_seconds:估时工具返回的scene_seconds,
source_ids:检查时使用的ID列表,risks:[]}；素材不足或检查未通过时说明未完成。''',
}
