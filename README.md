# 陶瓷商城订单链路 · 接口自动化测试

一个**有状态、有事务、有并发**的订单系统接口测试。
52 条接口用例（50 通过 + 2 条标记为已知缺陷），抓出 2 个真实缺陷（1 个数据不一致、1 个对账错误）；
另有 6 条 Playwright UI 层用例（`-m ui` 单独跑），UI 操作后复用接口层对账口径查库。
另提供一个 JMeter 基础压测场景，用于验证下单与查询接口在 10 并发下是否稳定。

> 被测服务 `app/server.py` 是按真实后端常见结构自行搭建的模拟服务（有库存表、订单表、库存流水表），
> 用于提供可复现的测试对象。测试框架、用例、缺陷报告和压测由项目独立完成。之所以不用 jsonplaceholder 那类公开 API：
> 它不持久化、不校验参数，测它等于测一个不会出问题的东西。

---

## 一、为什么做这个项目

我原来的接口框架测的是 `jsonplaceholder` 的 posts，问题是：

| 问题 | 后果 |
|---|---|
| 被测服务无状态、不落库 | 「创建成功但查不到」这种真 bug 在它身上不可能出现 |
| 全是单表 CRUD | 测不出业务流程、状态流转、事务 |
| 用例串行执行 | 并发问题一条都发现不了 |
| 业务是 posts/users | 跟真实交易场景对不上 |

这个项目换掉被测对象，把测试重心放在三件事上：**状态流转、幂等、并发下的数据一致性**。

---

## 二、被测业务规则

| 编号 | 规则 |
|---|---|
| R1 | 下单时扣减库存，扣减与「库存是否充足」必须原子，不允许超卖 |
| R2 | 同一 `idempotency_key` 只产生一张订单，重复提交返回已有订单（200） |
| R3 | 状态流转：`pending→paid→shipped→completed`；`pending→cancelled`；`paid→refunded`。待支付超过 30 分钟自动取消 |
| R4 | 非法状态流转返回 409，且不改变任何数据 |
| R5 | 取消订单回滚库存，并写一条流水 |
| R6 | 退款回滚库存，并写一条流水 |
| R7 | 每一次库存变动都要写 `stock_log`，且 **流水合计 == 当前库存** |

R7 是对账口径：期初记一笔 +stock，之后每次增减都对冲，合计应当恒等于当前库存。
只要有一笔变动没记账，或记错方向，这个等式立刻不平。

---

## 三、用例分布（接口层 52 条：50 通过 + 2 条标记为已知缺陷）

| 文件 | 条数 | 测什么 |
|---|---|---|
| `test_order_state.py` | 11 | 正向链路、5 类非法流转、404、超时取消、超时后禁止支付 |
| `test_idempotency.py` | 5 | 同键幂等、同键换参、不同键、无键、失败不留脏数据 |
| `test_concurrency.py` | 4 | 20 人抢 10 件、库存不为负、同键并发只成一单、支付与取消并发竞态 |
| `test_reconcile.py` | 6 | 期初/下单/取消/超时取消后账是否平、取消与退款是否回滚库存 |
| `test_input_validation.py` | 16 | 入参校验：qty / product_id / price / stock / name 的等价类与边界（两张参数表） |
| `test_api_contract.py` | 6 | 接口契约：响应字段、商品查询、订单列表与状态过滤、排序 |
| `test_framework_retry.py` | 4 | 框架自身：GET 重试、POST 不重试、显式重试、非网络错误不重试 |

后 4 条是**框架自测**：用假 session 把「写操作不重试」钉死，谁把 POST 加进重试立刻红。
不在必讲清单里；被问一句话：读超时说明请求可能已被处理，写操作重发就是重复下单。

---

## 四、发现的缺陷

| 编号 | 严重度 | 现象 | 根因 | 对应用例 |
|---|---|---|---|---|
| BUG-001 | P1 | 退款成功后库存不回滚，货退回来了但账上还是卖掉的 | `refund` 只改订单状态，没调 `_release_stock` | `test_refund_returns_stock` |
| BUG-002 | P1 | 超时取消后库存回滚了，但流水记成负数，账实不符 | `timeout_cancel` 写流水时 `delta` 取了 `-qty`，应为 `+qty` | `test_after_timeout_cancel_balances` |

完整的缺陷单（复现步骤、影响范围、定位建议、验证标准）见 [02-缺陷记录.md](02-缺陷记录.md)。

两条都用 `@pytest.mark.xfail(strict=True)` 标记，理由写着缺陷编号。
`strict=True` 的意思是：**如果哪天研发修好了，这两条会变成 XPASS 而报错**，提醒把标记撤掉——
不然修好了还挂着 xfail，等于用一个「已知问题」掩盖了一个已验证的功能。

---

## 五、怎么跑

依赖拆三个文件，按需安装：`requirements.txt`（接口层，零额外依赖）、
`requirements-ui.txt`（Playwright）、`requirements-mysql.txt`（PyMySQL）。

```bash
pip install -r requirements.txt                            # 接口层
pip install -r requirements.txt -r requirements-ui.txt     # 加 UI 层
pip install -r requirements.txt -r requirements-mysql.txt  # 加 MySQL 后端
python -m pytest                      # 接口层：50 passed, 2 xfailed
python -m pytest -m ui                # UI 层：6 passed（另需 playwright install chromium）
python -m pytest -m smoke             # 冒烟：5 条核心链路，40 秒内出结果
python perf/race_probe.py --rounds 25 # 并发竞态探针：继承当前 env，可探 SQLite 或 MySQL
python -m pytest -k concurrency       # 只跑并发
python -m pytest -rx                  # 看 xfail 的原因
```

不需要手动起服务：`conftest.py` 用 werkzeug 在子线程里起 HTTP 服务，跑完自动关闭。
每个用例用独立商品，跑完清理，**连跑一百次结果一样**。

---

## 六、JMeter 基础压测

项目提供可复现的 JMeter 基础场景，覆盖：

- 10 个并发用户，3 秒逐步启动，持续 20 秒；
- `POST /api/orders` 写接口；
- `GET /api/products/{id}` 查询接口；
- 响应码断言、HTML 报告、平均响应时间、P95、错误率和吞吐量统计。

运行：

```powershell
python perf/run_load_test.py
```

本次本地结果：2267 次请求成功、0 错误、平均 25.62 ms、P95 61 ms、约 114 requests/s。
详见 [perf/LOAD_TEST_REPORT.md](perf/LOAD_TEST_REPORT.md)。

> 这是 Flask + SQLite 单机基础压测，不是生产容量测试。更深入的测试需要独立压测机、连接池和监控指标。

---

## 七、UI 层（Playwright）

`app/templates/index.html` 是给被测服务加的最小下单页：只调用既有 API，不含独立业务逻辑。
`tests/test_ui_order.py` 6 条用例，每条都是 **UI → API → DB 三层对齐**：
页面断言（订单行、状态、错误提示）之后，用接口层同一套「流水合计 = 当前库存」口径查库。

覆盖：UI 下单对账平、非法数量不留脏数据、超库存提示且库存不动、UI 支付状态一致、
连点两下提交只成一单（幂等键防重复提交）、取消回滚库存后非法支付被拒。

为什么 UI 用例也要查库：页面显示「下单成功」不代表库里真的扣了库存。
「点出来的绿」和「账真的平」是两件事。

运行：`python -m pytest -m ui`（默认 `pytest` 只跑接口层，CI 里 UI 是独立 job）。

依赖说明：`test_ui_order.py` 对 playwright 的顶层导入是容错的（缺失时 `expect=None`）。
接口层 CI（test / mysql-test job）不装 playwright 也能正常收集用例；
真跑 `-m ui` 需先装 `requirements-ui.txt`，否则 browser fixture 会在 setup 阶段报缺依赖，而不是收集期崩溃。

---

## 八、MySQL 双后端

默认 SQLite：免安装免密码，clone 下来就能跑，CI 零依赖。
设 `SHOP_DB=mysql` 切到 MySQL（`MYSQL_HOST/PORT/USER/PASSWORD/DATABASE` 可配），
验证 InnoDB 行锁下的同一套业务规则。SQL 主体两种库通用，差异只在 `app/dbi.py` 四处：

1. 建表 DDL（AUTOINCREMENT/TEXT/REAL vs AUTO_INCREMENT/VARCHAR/DOUBLE）；
2. 事务与读锁（BEGIN IMMEDIATE 整库写锁、普通 SELECT 就够 vs START TRANSACTION + 状态机必须 FOR UPDATE 锁定读，否则快照读会放过非法流转）；
3. 幂等兜底（SQLite 靠库锁无窗口期；MySQL 先查后插有窗口期，撞唯一约束 1062 后回滚回查赢家订单）；
4. 锁错误判定（SQLite "database is locked" vs MySQL 1205 锁等待超时 / 1213 死锁）。

第 3 点是面试重点：**唯一约束才是幂等的最终兜底**，先查后插只是快路径。
第 2 点是第二个重点：同一段「先查状态再流转」的代码，在 SQLite 上安全、在 MySQL 上有窗口期——行锁与库锁的本质差异。

**实测记录（2026-11-22，本机 MySQL 8.0 / InnoDB）**：接口层 50 passed + 2 xfailed、UI 层 6 passed，
与 SQLite 结果一致；并发竞态探针连跑 25 轮无抖动（`perf/race_probe.py` 可复现，继承当前 env）。
快照读窗口期未被实际复现，按 InnoDB 语义补 FOR UPDATE 是为了不依赖运气。
CI 另有独立 `mysql-test` job（mysql:8.0 service 容器）：先跑同一套用例，再跑
`python perf/race_probe.py --rounds 25`，任何人可凭仓库复现用例与探针两组结论。

```powershell
$env:SHOP_DB="mysql"; $env:MYSQL_PASSWORD="你的密码"
python -m pytest                      # 同一套 52 条用例跑在 MySQL 上
```

---

## 九、目录

```
项目一-订单链路测试/
├── 00-需求与接口说明.md        # 需求规则 / 接口清单 / 错误码 / 状态机 / Q1–Q3 需求空档
├── app/server.py              # 被测服务（Flask，SQLite/MySQL 双后端）
├── app/dbi.py                 # 数据库连接层：两种库的四处差异在这里
├── app/templates/index.html   # 最小下单页（UI 层被测对象）
├── common/
│   ├── client.py              # HTTP 客户端：重试策略在这里
│   ├── db.py                  # 直连数据库对账
│   ├── factory.py             # 数据工厂：造商品、造订单、跑完清理
│   └── logger.py              # 日志：文件日志（排查入口 logs/api.log）
├── tests/
│   ├── conftest.py            # 起服务、给 client/db/factory
│   ├── test_order_state.py
│   ├── test_idempotency.py
│   ├── test_concurrency.py
│   ├── test_reconcile.py
│   ├── test_input_validation.py   # 入参校验：等价类 + 边界（两张参数表）
│   ├── test_api_contract.py       # 接口契约与查询：字段、列表、过滤、排序
│   ├── test_ui_order.py           # UI 层 6 条（-m ui）：Playwright + 跨层对账
│   └── test_framework_retry.py    # 框架自测 4 条：钉死写操作不重试
├── perf/
│   ├── order_api_load_test.jmx # JMeter 基础压测场景
│   ├── run_load_test.py        # 启动服务、执行 JMeter、汇总结果
│   ├── race_probe.py           # 并发竞态探针：N 轮连跑统计抖动
│   ├── LOAD_TEST_REPORT.md     # 本次压测结果与限制
│   └── results/                # 本地原始样本与 HTML 报告（不入库）
├── 02-缺陷记录.md
├── 03-测试报告.md
├── pytest.ini
└── requirements.txt
```

---

## 十、已知限制

1. **默认 SQLite 不是生产库**：已提供 MySQL 双后端验证行锁并发，但连接池、主从延迟、事务隔离级别对比仍未覆盖。
2. **只测了单商品**：多商品下单、购物车拆单没覆盖，属于下一轮范围。
3. **性能覆盖较浅**：已完成 10 并发的基础压测，但仍是 Flask + SQLite 单机环境，未验证 MySQL 行锁、连接池和生产 SLA。
4. **超时取消是懒过期**：只在读订单时触发，没有定时任务。真实系统两种都会有，定时任务那条链路没测。

## 十一、变更记录

个人项目最大的破绽是"一次成型、没有迭代痕迹"。这里如实记：

| 版本 | 改动 | 起因 |
|---|---|---|
| V1 | 28 条用例 + 模拟订单服务 + 2 张缺陷单 | 换掉 jsonplaceholder 靶场（它不落库，测出来的绿是假绿） |
| V1.1 | 补 CI（3.11/3.12 双版本）、补 `.gitignore`、删 `ship_order()` 里冗余的 check-then-act、`_parallel()` 去掉未使用参数、README 用例数 26 → 28 | 自审查出 10 处问题（自审报告在本地，不随仓库发布） |
| V1.2 | 新增 `03-测试报告.md`（执行统计 / 规则覆盖 / 缺陷分布 / 定位过程 / 环境与数据 / 出口准则） | 复盘发现只有缺陷单、没有整体结论，补上 |
| V1.3 | 补 24 条用例：入参校验 16（两张等价类参数表）、接口契约与查询 6、支付与取消并发竞态 1、超时取消后禁止支付 1；把 `logger.py` 从旧框架接过来（文件轮转 + 打码）；启用 `-m smoke` 5 条冒烟 | 覆盖率自审发现：全部 28 条里没有一条参数校验用例（400 类为零），商品查询和订单列表两个接口只有"顺带调用"没有独立断言；`pytest.ini` 里定义了 smoke 标记却没用 |
| V1.4 | 新增 JMeter 基础压测、自动运行脚本、HTML 报告和结果摘要 | 补齐最基础的性能测试入口，验证写接口与查询接口在 10 并发下的可用性，并明确单机 SQLite 的容量限制 |
| V1.5 | 删 `test_framework_retry.py`（4 条框架自测）、简化 `logger.py`（去轮转与打码）、清理 client 死字段；52 → 48 条 | 对照 27 届实习 JD 裁剪：框架 mock 自测属测开岗能力，目标 lane 几乎不问，内化成本高于回报；「仅 GET 重试」规则保留在 client.py |
| V1.6 | 新增最小下单页 + Playwright UI 层 6 条（UI→API→DB 跨层对账）、CI 独立 ui job；新增 `app/dbi.py` SQLite/MySQL 双后端与 MySQL 幂等唯一约束兜底 | 补 JD 高频的 UI 自动化与 MySQL 两项广度；UI 融入同一被测系统而非另起第三方靶场，避免教程模板形状 |
| V1.7 | MySQL 实测通过（50+2 / UI 6，与 SQLite 一致，25 轮并发探针无抖动）；状态机改 FOR UPDATE 锁定读堵 InnoDB 快照读窗口期；MySQL 测试库开跑前清空夹具 | 跨后端验证把「支持 MySQL」从代码分支变成实测证据；并发正确性不再依赖探针运气 |
| V1.8 | 恢复框架自测 4 条（第 4 条改为「非网络错误不重试」，原耗时字段断言随字段删除而移除）；新增 `perf/race_probe.py` 与 CI `mysql-test` job；依赖拆三文件；修正 logger/5xx/UI 覆盖/差异处数四处文档过时 | 外部审查指出：宣传规则的保护用例不该裁、探针数据必须可复现、可选依赖不能混在主清单；逐条采纳 |
| V1.9 | 修 CI 收集期崩溃：`test_ui_order.py` 顶层硬导入 playwright 改为容错导入（不装 playwright 的 job 收集期 exit 2）；`mysql-test` job 补跑 25 轮探针步骤 | 推送后 CI 实跑暴露：test/mysql job 红、ui job 绿；本机 Python 3.12 干净依赖复现 exit 2 定位根因，修复后双后端 50+2 全绿 |
| V1.10 | `_clean_mysql` 改为先幂等建表再清空（全新 MySQL 库上「先清后建」撞 1146）；CI mysql job 增加服务器信息步与 run summary 输出步 | CI 容器是完全全新库，暴露本机「表早已存在」掩盖的顺序问题；修复后真正一条命令从零复现；本机删表模拟全新库验证通过 |
