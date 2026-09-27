# 5G-A 场景加速运营服务

这是一个面向运营商网络优化与产品运营团队的 Python 后端，用于管理高铁、地铁、演唱会和大型场馆中的应用体验采样、质差识别、动态加速、用户权益、策略版本、容量预留与运营审计。服务使用 FastAPI 与 SQLite，所有运行数据保存在单个本地数据库文件中，不依赖另行部署的数据库、缓存、消息队列或浏览器界面。

## 已有能力

- 场景与区段：维护高铁线路、地铁区段、演唱会和场馆的容量、顺序、停留时间及运行状态。
- 应用画像：按游戏、直播、视频通话、视频和办公类别保存时延、丢包、上下行速率与优先级目标。
- 策略版本：校验质差评分权重、严重度阈值、资源倍数和会话时长，支持草稿、发布、生效与退役状态。
- 体验样本：使用业务采样键进行幂等写入，保存脱敏用户标识、终端类型、速度和网络指标。
- 质差事件：将应用目标与实测指标进行确定性比较，记录原因、严重度和处理状态。
- 动态加速：校验用户权益与生效策略，按区段容量预留上下行资源，支持完成、取消和超时释放。
- 运营分析：提供场景与应用质差率、容量利用率、会话成效和可恢复事件游标。
- 容量情景模型：按时间槽生成需求曲线，识别并发上限、上下行容量与候补积压瓶颈；同一预案保存多版本并对比扩容、限流、调整优先级的差异，审批后冻结输入与计算摘要，推演过程不改变实时预留。
- 身份与审计：提供管理员初始化、用户、角色、会话、权限、操作审计和后台维护能力。

## 运行环境

- Python 3.11
- SQLite 3，由 Python 标准库提供
- Linux、macOS 或 Windows

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据库位于 `./data/network-acceleration.db`。可以复制 `.env.example` 并通过 `NETWORK_DATABASE_PATH` 指定其他本地路径。

## 数据库初始化与检查

```bash
python -m app.cli init-db
python -m app.cli check-db
```

## 启动 API

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

5G-A 运营接口统一使用 `/api/network` 前缀，场景、应用、策略、权益、样本、事件、会话和分析报告都在该路径下。

## 容量情景模型

演唱会等大型活动开票后，场馆保障团队可以在观众入场前，基于场景区段、应用画像、历史高峰分布与人工假设（观众规模、应用构成、权益渗透率、区段客流占比）进行容量沙盘推演，接口前缀为 `/api/network/capacity`：

- `POST /scenarios/evaluate`：一次性试算。提交一组确定假设，直接返回按时间槽的需求曲线、峰值时段、上下行缺口、受影响应用和建议动作（扩容、限流、提高并发、调整优先级），不落库、不触碰实时预留。
- `POST /plans`、`GET /plans`、`GET /plans/{id}`：维护保障预案。
- `POST /plans/{id}/versions`：同一预案可保存多个版本，每个版本冻结当时的区段容量、应用画像、人工假设和计算摘要。
- `GET /plans/{id}/compare?left=&right=`：对比扩容、限流或调整优先级两个版本之间的峰值、缺口、候补积压差异与各自建议动作。
- `POST /versions/{id}/recompute`：用冻结输入重算，摘要必须与首次计算完全一致，否则返回 409；过程只读实时业务表。
- `POST /versions/{id}/approve`：审批通过后版本进入 `approved` 状态，输入与摘要冻结；审批前会再做一次重算校验。`/archive` 可归档已审批版本。

引擎（`app/network/scenario_engine.py`）是纯函数、无时钟、无随机数：高峰模板（`concert`/`venue`/`transit`）或人工关键点按时间槽线性插值，放行模拟按优先级分配容量，未满足需求按 `backlog_carry_ratio` 滚入下一时间槽形成候补积压。同一假设的结果指纹（SHA-256）恒定，预案推演与实时 `capacity_reservations` 完全隔离。

## 测试

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest
```

测试覆盖身份初始化、角色权限、审计脱敏、场景与应用登记、策略发布、样本幂等、质差判定、权益校验、容量拒绝、会话完成、固定时钟过期恢复、分析游标，以及容量情景试算、版本对比、重算一致性、审批冻结与实时预留隔离。

## 编译检查

```bash
python -m compileall -q app tests
```

## API 与 CLI 冒烟

```bash
python -m app.cli smoke
python -m app.cli network-demo
```

`smoke` 在进程内检查根路径、健康接口和运营摘要；`network-demo` 会创建高铁场景、应用画像与策略，登记有效权益，写入一条质差样本并启动加速会话。

## 目录结构

```text
app/
  network/         场景、应用、策略、采样、质差、加速、容量和分析
  api/             用户、角色、认证、审计、系统与维护接口
  core/            时钟、安全、异常、隐私和分页能力
  repositories/    通用 SQLite 查询与身份持久化
  schemas/         身份与管理接口输入模型
  services/        认证、审计、用户、后台任务和维护服务
  cli.py           初始化、检查和业务冒烟入口
  database.py      SQLite 连接、基础表结构与权限初始化
tests/             核心、身份、网络运营和分析回归测试
tools/             本地维护脚本
```

## 数据一致性

SQLite 连接启用外键、WAL、busy timeout 和同步写入策略。策略发布、样本与事件创建、容量预留、会话终止和过期恢复使用即时事务。体验样本保存脱敏用户标识，登录令牌只保存摘要，审计和会话事件不会记录明文密码或令牌。
