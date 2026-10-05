# 志愿权益兑换

本项目维护志愿权益兑换的领域约定、角色边界与样例数据，并提供可运行的 Python 后端服务，供接口调用和自动化验证统一使用。当前契约覆盖文博中心运营员、志愿者、监护人、场馆负责人，并明确积分库存双冻结、兑换履约状态机、补偿分录守恒、超时释放幂等等关键约束。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/redemption/`：兑换后端服务（模型、核心服务、HTTP 接口）。
- `tools/check_contract.py`：命令行摘要检查。
- `tools/serve.py`：启动后端服务。
- `tests/`：契约完整性回归测试与后端行为测试。

## 后端设计

`src/redemption/` 对应契约中的四条不变量：

- **积分库存双冻结**：`redeem` 在同一临界区内完成库存与积分的检查与冻结，多志愿者并发兑换不会超卖、不会透支。
- **兑换履约状态机**：`待履约 → 已履约 / 部分履约 / 已取消 / 已过期`，终态不可逆；履约确认才把冻结额正式转为扣减。
- **补偿分录守恒**：取消、审核驳回、部分履约剩余、资格变化、预约超时都通过补偿分录把冻结额完整回冲；`verify_conservation()` 可从台账重放核对账户与批次额度。
- **超时释放幂等**：`release_expired` 按状态守卫 + 幂等键（`expire:{order_id}`）释放，任务可任意重跑、可并发执行；重复履约回调按幂等键去重，只记录零增量审计分录。

权益批次维护：适用等级、库存（可用/冻结/消耗）、积分价格、预约期限（秒）。接口可查询账户余额、冻结额，以及台账中每次状态变化的原因。

## 接口

启动：`python3 tools/serve.py --port 8080`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/batches` | 维护权益批次（等级/库存/价格/预约期限） |
| GET | `/batches/{id}` | 批次详情（可用/冻结/消耗库存） |
| POST | `/batches/{id}/status` | 开放或关闭批次 |
| POST | `/accounts` | 开户（等级、初始积分） |
| GET | `/accounts/{id}` | 账户余额与冻结额 |
| GET | `/accounts/{id}/ledger` | 每次状态变化的分录与原因 |
| POST | `/accounts/{id}/topup` | 积分充值 |
| POST | `/accounts/{id}/level` | 资格变化，自动取消失格订单并补偿释放 |
| POST | `/redemptions` | 提交兑换（双冻结） |
| GET | `/redemptions/{id}` | 兑换单详情与分录轨迹 |
| POST | `/redemptions/{id}/confirm` | 履约确认（支持 `Idempotency-Key` 与部分履约） |
| POST | `/redemptions/{id}/cancel` | 取消（`user_cancel` / `review_rejected`） |
| POST | `/admin/release-expired` | 过期释放任务（可安全重跑，适合定时调用） |
| GET | `/admin/conservation` | 补偿分录守恒校验 |
| GET | `/ledger` | 全量台账 |

当前为内存存储实现，进程重启后数据不保留；核心服务 `RedemptionService` 与存储、接口解耦，可替换为持久化实现。

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
