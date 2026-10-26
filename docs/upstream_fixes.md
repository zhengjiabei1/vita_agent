# 官方出租车工具的可审计兼容补丁

适用官方提交：`f60169e89f30499cb7883f3dad76bd03facc908d`。`vita_agent.upstream_fixes.install_upstream_fixes()` 是显式运行时补丁，**必须对 baseline 和所有消融组一致启用**。不修改官方源码、数据库字段、rubric 或 reward。实验记录应包含 `patch_info()`；与未打补丁的公开排行榜结果比较时，需要说明运行环境有此差异。

实际代码检查与测试确认了两个阻断缺陷：

1. `OTATools.create_taxi_order` 扣减库存后调用 `DB.assign_order_id("taxi", ...)`；后者的 scenario table 缺少 taxi，抛出 `ValueError`。
2. `Order.__repr__` 和 `Order.__str__` 引用 `id_str["taxi"]`；映射也缺少 taxi，抛出 `KeyError`，导致创建结果、详情及搜索结果无法正常输出。

补丁仅为 taxi 提供不与现有订单冲突的 `OO` 前缀标识，并将 taxi 订单输出改为 `VITA_TAXI_ORDER_V1 ` 前缀加完整模型的 JSON。所有字段、产品列表、用户归属和状态均来自真实订单对象；JSON 对引号和换行进行转义。其他订单类型的 ID、repr、str 完全调用原函数。启动时校验 `tasks.py`、`environment/db.py` 和 `ota/tools.py` 的 SHA-256；源文件与所核对版本不符时拒绝安装补丁。

新的可信事实解析器只从 `get_taxi_info` 获取产品价格和库存，不以 `estimated_price` 冒充实际费用，不把草稿中的报价视为证据。创建按实际产品价格乘数量校验；付款和取消须有已观察的订单，校验订单类型、用户归属及状态。每次写操作后清空缓存，要求重新读取状态。

`tests/test_upstream_fixes.py` 在真实官方 `OTATools` 和 `OTADB` 上执行本地 `get_taxi_info → create_taxi_order → pay_taxi_order → get_taxi_order_detail → cancel_taxi_order → search_taxi_order`，验证库存、总额、状态和观察解析；不调用 LLM 或外部叫车服务。

另一项已观察到的上游行为：取消会退款并标为 cancelled，但不会返还 taxi 库存。补丁保留这一规则，避免只改变候选方案的模拟环境。适配器也不会自行增加库存；取消后须重新观察当前库存。表格容量等上游未公开的字段仍未知，不能声称已得到验证。
