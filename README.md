# 志愿权益兑换

本项目维护志愿权益兑换的领域约定、角色边界与样例数据，供后端服务、接口和自动化验证统一使用。当前契约覆盖文博中心运营员、志愿者、监护人、场馆负责人，并明确积分库存双冻结、兑换履约状态机、补偿分录守恒、超时释放幂等等关键约束。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/redemption/`：兑换后端服务（批次、账户、订单、分录、HTTP 接口）。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约完整性回归测试与后端行为回归测试。

## 后端服务（src/redemption/）

围绕契约四条不变量实现：

- **积分库存双冻结**：提交兑换在同一事务内冻结积分与库存，任一侧不足整体回滚；库存与积分扣减均为条件更新（`WHERE ... >= ?`），并发下不会超卖、不会超额冻结。
- **兑换履约状态机**：`冻结中 → 已履约 / 部分履约 / 已取消 / 已驳回 / 已过期`，终态不可逆；批次走 `草拟 → 待核验 → 已确认 → 执行中 → 已归档`，有冻结库存时禁止归档。
- **补偿分录守恒**：履约确认正式扣减；部分履约、取消、驳回、资格变化（等级下调）均通过补偿分录释放剩余冻结额。每个订单每个维度满足：终态时 `冻结 = 扣减 + 释放`，冻结中不得有任何扣减/释放，可用守恒接口随时校验。
- **超时释放幂等**：`POST /tasks/release-expired` 逐订单独立事务 + 状态守卫更新，可并发、可安全重跑；重复回调按 `callback_id` 去重，返回首次处理结果。

### 运行

```bash
PYTHONPATH=src python3 -m redemption          # 默认 127.0.0.1:8080，PORT 环境变量可改
PYTHONPATH=src python3 -m redemption &        # 后台运行后可 curl 下列接口
```

### 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/batches` | 创建权益批次（名称、适用等级、库存、积分价格、预约期限） |
| GET | `/batches`、`/batches/{id}` | 批次目录 / 详情（可用、冻结、已扣减库存与状态历史） |
| POST | `/batches/{id}/transition` | 批次状态流转（必填原因） |
| POST | `/volunteers` | 开立志愿者账户（等级、初始积分） |
| GET | `/volunteers/{id}/account` | 账户余额、冻结额、总额与近期变动 |
| POST | `/volunteers/{id}/points` | 积分调整（记录原因） |
| POST | `/volunteers/{id}/level` | 等级变更；低于批次要求时自动补偿释放其冻结订单 |
| POST | `/redemptions` | 提交兑换（`idempotency_key` 幂等），同时冻结积分与库存 |
| GET | `/redemptions/{id}` | 订单详情：状态流转原因、分录流水 |
| POST | `/redemptions/{id}/confirm` | 履约确认（支持部分履约、`callback_id` 去重） |
| POST | `/redemptions/{id}/cancel` | 取消预约，全额补偿释放 |
| POST | `/redemptions/{id}/reject` | 审核驳回（必填原因），全额补偿释放 |
| GET | `/redemptions/{id}/conservation` | 分录守恒校验 |
| POST | `/tasks/release-expired` | 过期释放任务，可安全重跑 |

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
