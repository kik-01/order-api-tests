# JMeter 基础压测

这是一个**基础性能冒烟**，用于验证订单写接口和商品查询接口在持续并发下能否稳定返回，并练习 JMeter 的线程组、HTTP 请求、响应断言、结果汇总和报告解读。

它不是容量测试，也不用于证明生产系统能承受多少并发。

## 场景

- 10 个并发用户，3 秒内逐步启动；
- 每个用户循环执行，持续约 20 秒；
- 每次循环依次请求：
  - `POST /api/orders`
  - `GET /api/products/{id}`
- 请求间隔约 50 ms；
- 响应码断言：下单为 `201`，查询为 `200`；
- 输出错误率、平均响应时间、P95、吞吐量和响应码分布。

## 运行

项目依赖安装完成后，在项目根目录运行：

```powershell
python perf/run_load_test.py
```

脚本会自动启动临时 Flask 服务、创建压测商品、调用 JMeter、生成结果，然后关闭服务。

也可以调整参数：

```powershell
python perf/run_load_test.py --threads 10 --ramp-up 3 --duration 20
```

如果 JMeter 不在 `D:\测试工具\JMeter`，通过 `--jmeter` 指定：

```powershell
python perf/run_load_test.py --jmeter D:\path\to\jmeter.bat
```

## 结果

每次运行会写入 `perf/results/<timestamp>/`：

- `summary.md`：简版结果；
- `summary.json`：结构化结果；
- `samples.jtl`：JMeter 原始样本；
- `jmeter.log`：本次 JMeter 运行日志；
- `html-report/index.html`：JMeter HTML 报告。

## 局限性

- 被测服务使用单机 Flask + SQLite，写操作存在数据库锁竞争；
- 测试机、服务和 JMeter 在同一台电脑上，会互相争抢 CPU 和磁盘；
- 只覆盖单商品下单和商品查询，不覆盖多商品、支付链路和真实网络延迟；
- 结果只能说明当前环境下的基础稳定性，不能外推为生产容量。
