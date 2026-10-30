"""Instructions for the selected scheduling agent."""
TASK_GUIDANCE = {
    'schedule': '''逐项读取素材并检查规则；仅已通过审核的素材可以查询时段并保存草稿。
原时段被占用时可建议空闲时段，必须 status=pending_confirmation，confirmation 必须精确为
“原要求 {requested_time} 不可用，建议 {suggested_time}，等待用户确认”；原时段可用时为 status=ready、confirmation=null。
草稿字段严格限定为工具描述中的字段，published 必须为 false，绝不能发布。
查询时传入素材 ID，以便排除它自己的已有草稿；保存返回 slot_conflict 时，重新查询并根据新结果调整或停止。
逐项输出 items，每项包括 id、date、requested_time、review_status、risk_reasons、missing、saved、suggested_time、confirmation；
总体包含 status（ready、pending_confirmation 或 incomplete）、items、saved、published:false。
不要宣称失败的保存成功；部分失败时保留各项真实状态。'''
}
