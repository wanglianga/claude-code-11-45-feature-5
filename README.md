# 潮位观测数据产品发布与订阅服务

面向沿海防潮指挥部门的潮位数据产品服务：把不同港区/海湾验潮站的读数统一到同一时空口径
（统一目标高程基准 + 共同时间格 + 质量等级），以**分阶段发布**（初报 → 确认产品）方式
向各消费系统交付，替代邮件交换表格、各自猜测基准与缺测含义的现有做法。

- 站点目录：观测位置、垂直基准（站点零点）、读数单位、正方向、设备状态
- 产品生成：原始观测 → 逐样本基准换算（按生效区间）→ 共同时间格重采样 → 质量等级
- 分阶段发布：`PRELIMINARY` 初报供值守研判，`CONFIRMED` 确认产品才进入正式模型
- 订阅交付：按区域/阶段订阅，产品+清单+待交付任务同事务提交，消费者分别确认、独立重试
- **空间授权治理（/access）**：联合范围求值、授权约束下的重新聚合、分页撤回控制与受控领取，
  使同一订阅的多项许可不会拼成从未获准的数据范围

## 技术栈

Python 3.12 · FastAPI · PostgreSQL（SQLAlchemy 2）· 单容器即可运行（API + 模拟消费端 + 交付 worker）

## 快速开始（Docker Compose，一键）

```bash
docker compose up -d --build db api consumer worker
# 验收回放（44 项断言，退出码非 0 即失败）
docker compose run --rm replay
# 空间授权治理回放（47 项断言）
docker compose run --rm replay-access
```

- API 与 Swagger UI：http://127.0.0.1:8000/docs （OpenAPI 3.x：/openapi.json，含全部 /access 接口）
- 模拟消费端：http://127.0.0.1:8001/docs

## 本地开发启动

```bash
# 需要 PostgreSQL（开发库 ccdev/ccdev@127.0.0.1:5432）
/opt/python-3.12/bin/python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 终端 1：API（建表在启动时自动完成）
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
# 终端 2：模拟消费端
.venv/bin/python -m uvicorn app.consumer:app --host 127.0.0.1 --port 8001
# 终端 3：交付工作进程（崩溃恢复语义见下）
.venv/bin/python -m app.worker
```

## 测试账号（演示用 API Key，请求头 `X-API-Key`）

| Key | 角色 | 权限 |
| --- | --- | --- |
| `admin-key` | admin | 基准/站点/订阅/空间资料与许可管理、成员重建、跨用户审计、/admin/reset |
| `operator-key` | operator | 观测接入、产品作业、确认签发、交付重试、成员重建 |
| `consumer-key` | consumer | 读取产品、单元、交付记录；消费端接收交付 |

> 仅用于隔离开发与验收，可通过环境变量 `ADMIN_KEY/OPERATOR_KEY/CONSUMER_KEY` 覆盖。
> 空间授权数据接口的访问主体是**虚构授权用户**（`POST /access/users` 创建，各自独立 Key），
> 不接真实账号或外部权限服务。

## 验证方式（本题验收）

```bash
# 1) 单元与集成测试（基准图/重采样/接入去重/版本快照/同事务交付/空间授权，42 项）
.venv/bin/python -m pytest tests/ -q

# 2) 端到端验收回放（需 API + 消费端已启动）
.venv/bin/python scripts/replay.py          # 原有链路：44 项断言
.venv/bin/python scripts/replay_access.py   # 空间授权治理：47 项断言
```

回放脚本 `scripts/replay.py` 会真实导入样例数据并核对：多基准站点换算值、重复报文去重、
迟到补传与质量更正生成的新版本、发布中途基准配置变更的版本隔离、路径冲突保留、
断档/插值/待核标记、回执丢失重送去重、消费者离线恢复、慢重试不恢复旧值、
订阅者分别确认与失败者独立重试。

`scripts/replay_access.py` 对空间授权逐项核查：多许可维度串用、内部孔洞、边界规则、
误差跨边界单列不可确定、迁站前后、窗口中途撤回、DENY 优先、低频后端重算、授权日期
跨界格元（MISSING/DECLINE）、部分站点区域汇总（合法分母、不当零值）、辅助字段约束、
连续翻页撤回终止、文件生成后取消许可的受控领取、重复领取留痕、成员关系失效保护、
审计可追溯与跨用户不泄漏。

**验证结论（实际执行）**：本仓库交付前已在容器内完成 `pytest`（42/42 通过）、
`scripts/replay.py`（44/44 通过）与 `scripts/replay_access.py`（47/47 通过，
本地 uvicorn + 本机 PostgreSQL 18）。

## 基准样例（回放使用，体现换算语义）

目标高程：`ND85`（1985 国家高程）。转换边语义 `height(to) = height(from) + offset_m`，
单位米，水位向上为正，每条边带生效区间 `[valid_from, valid_to)`：

| 边 | 偏移(m) | 生效区间 | 说明 |
| --- | --- | --- | --- |
| PORT_A → ND85 | +0.832 | 2025-01-01 ~ 2026-06-01T00:00Z | 重测前 |
| PORT_A → ND85 | +0.815 | 2026-06-01T00:00Z ~ | 重测后（跨零点重测日） |
| BAY_B → ND85 | +1.204 | 2025-01-01 ~ | |
| LOCAL_SG → PORT_A | −0.310 | 2025-01-01 ~ | 多段路径第一段 |
| LOCAL_SG → ND85 | +0.520 | 2025-01-01 ~ | 与多段路径（−0.310+0.832=0.522）不相容 → 保留冲突 |
| ND85 → PORT_A | −0.832 | ~ 2026-06-01T00:00Z | 反向边构成环，简单路径约束下不重复加偏移 |

站点样例（读数单位/正方向/站点零点）：

| 站 | 区域 | 单位 | 正方向 | 零点基准 | 场景 |
| --- | --- | --- | --- | --- | --- |
| ST-A1 | gangqu-a | m | up（水位向上为正） | PORT_A | 10min 规则采样，跨重测日 |
| ST-A2 | gangqu-a | cm | up | PORT_A→BAY_B（中途变更） | 不规则采样按代表时段加权 |
| ST-C1 | gangqu-a | m | up | LOCAL_SG | 两条路径不相容 → CONFLICT |
| ST-NP | gangqu-a | m | up | ISOLATED | 无目标基准路径 → NO_PATH（不用邻站补齐） |
| ST-B1 | haiwan-b | m | up | BAY_B | 长断档/短断档/突跳待核/疑似更正 |
| ST-D1 | haiwan-b | mm | down（距水面距离向下为正） | BAY_B | 单位与符号换算（500mm → −0.5+1.204=0.704） |

产品实际按上述关系计算换算值（如 ST-A1 读数 1.000m → 重测前 1.832m、重测后 1.815m），
而非只替换标签。

## 关键设计语义

- **逐样本换算再上格**：跨重测日窗口先按各样本时刻的适用区间换算，再形成共同时间格；
  边界格元为两偏移的加权混合，不做“先平均再套一个偏移”。
- **基准版本快照**：每次基准/站点零点变更产生新 `datum_version`；作业创建时固定版本，
  运行全程使用该快照——发布中途配置变更不会出现一半站点按旧基准、一半按新基准。
- **多段路径与冲突**：转换边构成有向图，DFS 枚举简单路径（节点不重复，循环路径不重复加偏移）；
  两条有效路径结果不相容时保留全部候选并标记 `CONFLICT`；无路径站点标记 `NO_PATH`，
  不用邻站偏移补齐。
- **重采样**：格元边界明确；不规则采样按代表时段（Voronoi 区间，单侧最多 max_rep/2）加权；
  重复序号不增加权重；覆盖率不足时仅短断档（≤30min 且两侧均为 GOOD）允许线性插值；
  长断档记 `MISSING`；突跳待核段记 `PENDING`，不插成平滑实测曲线。
- **可溯源产品单元**：每格元记录参与的原始样本 id、覆盖时长、换算路径（含各边偏移与
  生效区间）、质量等级与原因、基准版本，接收方能分清实测/允许的插值/缺测/待核/冲突。
- **幂等接入**：同一站点重复送达同一采样序号且内容相同 → 去重；内容不同 → 更正，
  生成新观测版本并触发后续新产品版本。
- **分阶段发布**：作业只能生成 `PRELIMINARY`；确认产品经 `/products/{id}/confirm` 单独签发
  （系列内新版本），初报不能伪装成确认；订阅按阶段过滤，区域模型只收 CONFIRMED。
- **事务性发布**：产品、清单（产品单元）与待交付任务在同一事务提交；产品本体完整提交后
  才进入交付队列。
- **交付与恢复**：每订阅者一条交付任务，`delivery_key` 全局唯一；worker 发送前崩溃 → 任务
  保持 PENDING 重发；发送后崩溃/确认包丢失 → 重送保留同一标识，消费端按标识去重后再确认；
  消费端按 `publish_seq` 应用，慢重试的旧版本不会把接收方恢复成旧值；已确认消费者无需重收。

## 空间授权治理语义（/access）

- **一条完整条件**：许可 = 区域（多边形几何）× 观测适用日期 × 产品阶段 × 允许字段 ×
  最细时间粒度，五维同时成立才放行。多条许可只取各自完整条件成立的数据**并集**，
  绝不先并区域再套最宽粒度——“北湾低频确认 + 南湾高频初报”拼不出北湾高频序列。
- **明确拒绝优先**：`DENY` 在其适用范围优先于任何 `ALLOW`；已撤回（`revoked_at`）或
  尚未到生效时刻/已过期的许可不参与放行，撤回与生效事实均保留在记录中。
- **实际几何判断**：所有几何落在声明的**共同坐标系**下，支持多个不相连多边形与内部孔洞；
  粗略区域名称、包围盒命中、离港口最近都不能替代点面判断（bbox 仅作预过滤）；
  无效多边形或第二套 CRS 直接返回 422 范围配置问题，不静默按全区域读取。
- **边界显式归属**：每个港区声明 `boundary_rule=INCLUDE/EXCLUDE`，边界站点按规则归属。
- **迁站按观测时刻定位**：站点安装记录带半开生效区间，观测位置按观测发生时适用的安装
  记录解释；搬迁不改变旧观测的位置资格。定位误差圆横跨授权边界且不能证明完全位于区内时
  单列 `UNCERTAIN`，绝不用中心点擅自放行。
- **授权约束下重算**：低频订阅在后端按获准窗口重新计算值与有效覆盖，不先返回高频行再让
  前端降采样；跨授权日期边界的格元只用合法样本重算并声明覆盖不足（`MISSING`），或按契约
  整格拒绝（`DECLINE`），不沿用全窗口平均值；最大值/极值时刻等辅助字段同样受字段与粒度
  约束，不能用来绕过限制。
- **部分站点区域汇总**：一个区域产品只获准部分站点时，重新生成明确命名的受限范围产品
  （`{region}-restricted:{stations}`），分母只数实际合法且有值的站点，未授权站点不当零
  水位，观测不足保留缺测，也不直接删掉站点明细却继续提供全区域值。
- **同观测只纳一次**：多条许可覆盖同一条观测时样本不重复计权；一个站点获准高频不会使
  同区域其他站点一同升格。
- **分页撤回控制**：会话固定产品范围与数据截止点（游标绑定 scope_hash），每次取页仍联查
  当前许可；许可撤回或条件更正即终止旧会话并要求重新请求，不让前后页暗用不同许可却
  宣称是同一份完整文件。
- **受控领取**：生成任务的暂存内容与可领取内容物理分目录存放；晋升（promote）与领取
  （claim）前都重新联查当前许可，文件生成后撤回会被拦截并标记 `REVOKED`，只返回受控领取
  接口而非长期公开链接；相同请求重试核对当前范围，不再合格的旧文件不复用；一次领取完成
  后无法收回对方已存副本，只登记已交付的确切范围（内容哈希+许可+站点+格数）与领取记录。
- **可审计**：范围求值逐条观测绑定安装位置、命中许可与放行/排除原因；交付登记可从一个
  文件查回实际使用了哪些条件，而不是只看文件名里的港区。普通用户的错误信息不确认其他
  港区站点是否存在，跨用户审计仅 admin 可用。

### 两种范围求值策略（按站点规模与授权变动频率选择）

| | online（默认，每次联查） | membership（预计算成员关系） |
| --- | --- | --- |
| 求值方式 | 每次查询实时联查站点有效安装与多边形几何 | 预生成“站点安装区间 × 许可”的成员表，常用查询只连接已核对成员 |
| 查询等待 | 复杂边界与长历史会反复做几何求值，站点多/窗口长时延迟上升 | 几何只在重建时算一次，批量导出更快、等待更短 |
| 失效检查 | 位置/边界/授权日期变动在下一次查询即时生效，无重建窗口 | 迁站、边界或授权更正后相关成员置 `STALE`；覆盖时刻仅有 STALE 行时求值返回 503，**绝不使用已失效的放行关系**，重建完成才恢复 |
| 位置修订工作量 | 无额外维护，修订即生效 | 每次迁站/边界更正/许可更正都要重建相关成员（`POST /access/memberships/rebuild`），变动频繁时维护成本高 |

实现上没有用“忽略孔洞”或“只判断当前位置”来缩短任何一条路径的等待：membership 表也
保留 `OUT/UNCERTAIN` 结论与安装区间，重建期的缺口显式报错而不是静默放行。

## API 概览（详见 Swagger UI /docs）

- 目录：`POST/GET /datums`、`POST/GET /datum-conversions`、`GET /datum-versions`、
  `POST/GET /stations`、`PUT /stations/{id}/datum`
- 接入：`POST /observations:ingest`、`GET /observations`
- 产品：`POST /jobs`、`POST /jobs/{id}/run`、`POST /products/{id}/confirm`、
  `GET /products`、`GET /products/{id}/units`、`GET /products/{id}/superseded-deliveries`
- 订阅交付：`POST/GET /subscriptions`、`GET /deliveries`、`POST /deliveries/{id}/retry`、
  `POST /worker/run-once`
- 空间授权（前缀 `/access`）：
  - 资料导入：`POST/GET /users`、`POST/GET /crs`、`POST/GET /zones`、
    `POST/GET /installations`、`POST/GET /licenses`、`POST /licenses/{id}/revoke`
  - 范围与重算：`POST /evaluate`、`POST /recompute`、`GET /field-catalog`
  - 分页：`POST /sessions`、`GET /sessions/{token}/pages`
  - 文件与领取：`POST /extractions`、`GET /extractions`、
    `POST /extractions/{id}/promote`、`POST /extractions/{id}/claim`、`GET /my/deliveries`、
    `GET /my/evaluations/{id}/audit`
  - 成员关系：`POST /memberships/rebuild`、`GET /memberships`
  - 内部审计（admin）：`GET /audit/evaluations/{id}`、`GET /audit/deliveries`
- 模拟消费端（8001）：`POST /receive/{name}`、`GET /state`、`GET /inbox`、
  `PUT /flags/{drop_ack|offline}`、`POST /reset`

## 目录结构

```
app/
  main.py       # API（FastAPI，Swagger UI 于 /docs）
  access_api.py # 空间授权治理路由（资料导入/求值/重算/分页/领取/审计）
  access.py     # 联合范围求值、受限重聚合、会话撤回控制、受控领取、成员关系
  geometry.py   # 共同 CRS 下多边形校验、含孔洞点面判断、边界规则、误差圆归属
  consumer.py   # 模拟消费端（去重、按 publish_seq 应用、故障注入）
  worker.py     # 交付工作进程（崩溃可恢复）
  services.py   # 接入去重/更正、产品生成、确认签发、同事务交付
  datum.py      # 基准转换图：简单路径、冲突保留、生效区间
  grid.py       # 重采样：代表时段加权、覆盖、插值/缺测/待核
  models.py     # 站点/基准版本/观测版本链/产品/订阅/交付/授权治理表
scripts/replay.py         # 原有验收回放（44 项断言）
scripts/replay_access.py  # 空间授权治理回放（47 项断言）
tests/                    # pytest（41 项，含 tests/test_access.py）
```
