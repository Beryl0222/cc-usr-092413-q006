# 公共健身资产问效

服务用于连接公益资金、健身设施、使用情况和维护责任，为年度预算评审提供
"保留 / 迁移 / 改造 / 退役 / 替换" 的可核验决策链。

运行 `python3 service.py --check` 可核对服务配置；执行
`python3 service.py --port 8000` 后访问 `/health` 可确认服务身份。

## 决策链

实现在 `decision_chain.py`，只依赖标准库：

1. **证据时间汇总**：按时间窗口汇总资金来源、安装批次、使用证据、安全事件、
   维修成本与服务覆盖。观测区分"有数据（含测得零使用）"与"缺测"——
   `missing_windows` 单独计数，缺测永不折算成零使用
   （`missing_treated_as_zero=false`），证据全缺时结论为"证据不足"。
2. **四方门控**：运营单位提方案、财务核对未摊销资金、安全负责人确认风险、
   社区代表只对服务缺口签署（`scope=service_gap_only`）。四方齐备、风险被
   接受、缺口有交代后，才能定稿并写入生效日；定稿不可改、不可重复定稿。
3. **退役执行**：关闭生效日之后的未来工单，二维码指导下线，未结维保承诺
   显式兑现/退款/转移；事故与支出历史原样保留，设施置为 `retired`。
4. **替换执行**：单事务原子承接**适用的**场地服务义务、二维码扫码通知缓存、
   未过期且可转移的保修责任；旧设施的工单、事故绝不转移，执行前强制故障
   隔离校验，失败则整批不落库。
5. **预算排序**：按已批准规则 `public-value-v1`（使用 0.4 / 覆盖 0.3 /
   安全 0.2 / 资金止损 0.1）打分；证据完整者优先于纯缺测者；预算不足按
   顺序分配。人工调整必须给出理由，调整前后位次与理由全部留痕并重算分配。
6. **回执幂等**：现场回执按 `receipt_no` 去重，重复提交返回 `duplicate=true`
   且不产生任何新记录。
7. **撤回与分期**：可只执行部分步骤（分期），执行中可撤回；撤回后可恢复，
   已完成步骤不重复，从中断处继续。已全部执行完毕的决定不可撤回。
8. **资金追踪**：回答每笔资金为何继续投入（未结工单/无生效决定）、何时
   停止（退役或替换生效日）、是否随替换承接，以及居民服务是否得到补位
   （新资产承接场地义务或同场地仍有活跃设施）。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/receipts` | 现场回执幂等录入（设施、资金、工单、观测、事故、覆盖、二维码、保修、维保承诺） |
| GET | `/api/facilities/<id>/timeline?start=&end=` | 证据时间汇总 |
| POST | `/api/decisions` | 创建决定（`action`: keep/migrate/renovate/retire/replace） |
| POST | `/api/decisions/<id>/sign` | 四方门控签署 |
| POST | `/api/decisions/<id>/finalize` | 定稿（`effective_on` 生效日） |
| POST | `/api/decisions/<id>/execute` | 执行（可传 `steps` 分期执行） |
| POST | `/api/decisions/<id>/withdraw` | 撤回（须给理由） |
| POST | `/api/decisions/<id>/restore` | 撤回后恢复 |
| GET | `/api/decisions` | 决定列表 |
| POST | `/api/budget/rankings` | 公共价值规则排序 |
| POST | `/api/budget/rankings/<id>/adjust` | 人工调整（必须说明理由） |
| GET | `/api/funding/<id>/trace?as_of=` | 资金追踪 |

领域校验失败返回 `400`，状态冲突（未定稿执行、重复定稿等）返回 `409`。

## 测试与构建

执行完整测试：

```bash
npm test
```

执行编译检查：

```bash
python3 -m compileall -q .
```

两条命令都可在单个 Linux 应用容器内直接运行，不需要额外服务。
