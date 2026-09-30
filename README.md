# 演出录音播放版税结算服务

为演出录音制作方提供播放版税结算的 HTTP 服务：登记作品与分成协议、导入播放记录（含更正链与幂等）、按播放发生时间匹配协议、逐笔按比例把整数金额分到最小货币单位，并按期间出具权利人应付单与可正可负的追补单。

技术栈：Python 3.12 · FastAPI · PostgreSQL 16（GiST 排他约束、事务咨询锁、约束触发器）· Uvicorn · psycopg 3。

---

## 1. 一键启动

需要 Docker 与 Docker Compose v2。

```bash
docker compose up -d --build
```

该命令会：

1. 启动 PostgreSQL 16，并在首次初始化时自动执行 [`sql/schema.sql`](sql/schema.sql) 建库建表；
2. 构建并启动 API 容器，启动时会再次幂等地应用 schema（连接外部数据库也可用）；
3. 通过健康检查等待数据库就绪后再对外服务。

服务地址与端口：

| 服务 | 地址 |
| --- | --- |
| API 基础地址 | <http://localhost:8000> |
| 健康检查 | <http://localhost:8000/health> |
| Swagger UI（可在线调用） | <http://localhost:8000/docs> |
| ReDoc | <http://localhost:8000/redoc> |
| OpenAPI JSON | <http://localhost:8000/openapi.json> |
| PostgreSQL | `localhost:5432`（库/用户/密码均为 `royalty`） |

停止与清理：

```bash
docker compose down          # 停止
docker compose down -v       # 停止并删除数据库数据卷（清空所有数据）
```

日志：

```bash
docker compose logs -f api
```

### 不使用 Docker 本地运行

```bash
pip install -r requirements.txt
export DATABASE_URL=postgresql://royalty:royalty@localhost:5432/royalty
psql "$DATABASE_URL" -f sql/schema.sql        # 或交由应用启动时自动执行
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## 2. 配置

全部通过环境变量配置（见 [`docker-compose.yml`](docker-compose.yml)）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://royalty:royalty@db:5432/royalty` | PostgreSQL 连接串 |
| `DB_MIN_CONN` / `DB_MAX_CONN` | `1` / `10` | 连接池大小 |
| `SCHEMA_PATH` | `/app/sql/schema.sql` | 启动时幂等执行的 DDL 路径 |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | 监听地址与端口 |
| `DOC_PREFIX` | `STMT` | 单据号前缀，单据号形如 `STMT-202601-P3-0`（前缀-期间起始月-期间ID-序号） |

数据库容器的环境变量：`POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB`，均为 `royalty`。

---

## 3. 业务规则

### 金额与时间

- **金额单位**：所有金额均为非负整数，单位是最小货币单位（如人民币分），全链路不引入浮点。
- **协议区间与结算期间**：统一使用半开区间 `[start, end)`。协议在 `valid_from ≤ 播放时间 < valid_to` 时适用；`valid_to` 省略表示开放结束。因此一段协议在 7 月 1 日 00:00 结束、下一段同时刻开始不算重叠。
- 时间字段接受 ISO-8601（建议一律带时区，如 `2026-02-10T20:00:00Z`）；不带时区的时间戳按 UTC 解释。
- 分成比例使用定点 `NUMERIC(12,6)`，以字符串（如 `"33.33333334"`）传入，合计必须**精确等于 100**。比例在响应中也以字符串返回，避免 JSON 数字的二进制浮点误差。

### 作品与分成协议

- 权利人、作品均以业务编号唯一登记；重复编号同名幂等，异名/异标题拒绝（422）。
- 每个协议包含适用起止时间与多名权利人的比例清单：
  - 比例必须严格为正、权利人不得重复、**合计恰好 100%**（应用层校验 + 数据库约束触发器双重保证）；
  - **同一作品的协议区间不得重叠**：数据库用 GiST 排他约束（`btree_gist` + `tstzrange`）硬性保证，应用返回 422 `agreement_overlap`。

### 播放记录、幂等与更正

- 播放记录含唯一编号、作品、发生时间、非负整数金额。
- **同编号同内容**重复导入：幂等成功，返回 `200` 且不产生新数据。
- **同编号异内容**：拒绝，返回 `422 play_code_content_mismatch`。
- **更正**必须引用原编号（`corrects_code`）并给一个全新编号：
  - 被更正记录会保留留痕（`superseded=true`），但**只有更正链的链尾参与核算**；
  - **分叉拒绝**：已被更正的记录不能再被第二条记录更正（`corrects_id` 上的唯一约束 + 行锁）；
  - **循环拒绝**：结构上不可构造，接口仍做防御性校验，返回 `422 correction_cycle`；
  - 更正可以改变作品、时间或金额，因此可能跨结算期间或改变适用协议（见追补单）。
- 批量导入接口逐条在保存点中处理：单条失败只拒绝该条并回传原因，其余照常入库。

### 匹配协议与逐笔分账

- 按**播放发生时间**（而非导入时间）匹配该作品唯一适用的协议；
- 找不到适用协议的链尾播放列入**待处理清单（pending）**，**不计入任何权利人应付额**；
- 每一笔播放独立拆分：先取 `floor(金额 × 比例 / 100)`，剩余整数单位按**小数部分降序**逐个补足，小数部分相同时按**权利人编号升序**补足；
- 使用高精度定点十进制计算，**逐笔拆分后金额合计与原金额严格守恒**（有单元测试覆盖，见 `tests/`）。

### 期间单据与追补单

- `POST /api/v1/settlement/statements` 对半开期间 `[period_from, period_to)` 出具单据：
  - 首次出具为 **初始单（`kind=initial`, `seq=0`）**，包含各权利人应付额及当前待处理清单快照；
  - **已出具单据不可修改、不可覆盖**；
  - 后补协议（让原待处理播放变为可结算）或播放更正（改变金额/时间/作品）后再次出具，生成**追补单（`kind=adjustment`）**，每个权利人的金额 = 当前重新核算的应付总额 − 此前所有单据累计额，因此**追补金额可正可负，也可以为 0（不出现在明细中）**；
  - 同一期间若无任何变化（无差额、待处理清单也未变），拒绝重复出具：`409 no_change`；
  - 期间内没有任何有效播放记录时不出具：`409 empty_period`；
  - 同一期间的并发出具用事务级咨询锁串行化，保证**不会产生重复单据**；
  - 待处理清单随每次出具快照保存，可对比其变化。

---

## 4. HTTP API 一览

所有接口前缀 `/api/v1`，请求/响应均为 JSON。完整交互式文档见 `/docs`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST / GET | `/rightsholders` | 登记 / 列出权利人（同编号同名幂等 200，异名 422） |
| POST / GET | `/works` | 登记 / 列出作品 |
| POST / GET | `/agreements` | 创建协议（可 `?work_code=` 过滤）；区间重叠/比例非 100 返回 422 |
| POST | `/plays` | 导入单条播放或更正；创建 201 / 幂等 200 / 异内容或分叉 422 |
| POST | `/plays/batch` | 批量导入，逐条返回 `created/idempotent/rejected` |
| GET | `/plays` | 列出链尾播放；`?include_superseded=true` 看留痕全量 |
| GET | `/plays/{code}/chain` | 查看某编号的更正链（根 → 链尾） |
| GET | `/settlement/compute?period_from=&period_to=&detail=` | 试算：当前应付、待处理、此前单据与累计；`detail=true` 返回逐笔分账 |
| POST | `/settlement/statements` | 出具初始单或追补单（请求体：期间起止） |
| GET | `/settlement/statements` | 单据列表（可按期间过滤） |
| GET | `/settlement/statements/{id}` | 单据详情（明细 + 待处理快照） |

错误响应统一格式：

```json
{ "error": { "code": "agreement_overlap", "message": "agreement interval overlaps ...", "details": {} } }
```

---

## 5. 调用示例

- 可直接执行的端到端脚本（需要 `curl` 与 `jq`）：

  ```bash
  docker compose up -d --build
  ./examples/walkthrough.sh
  ```

  脚本覆盖：登记、协议重叠/比例错误拒绝、幂等、异内容拒绝、更正、分叉拒绝、更正链、试算、初始单、重复出具拒绝、后补协议后的追补单。

- VS Code REST Client / JetBrains HTTP Client 文件：[`examples/api.http`](examples/api.http)。
- 也可直接打开 <http://localhost:8000/docs> 在浏览器里逐项调用。

### 最小 curl 流程

```bash
# 权利人 / 作品
curl -s localhost:8000/api/v1/rightsholders -H 'Content-Type: application/json' \
  -d '{"code":"RH-001","name":"作曲家甲"}'
curl -s localhost:8000/api/v1/works -H 'Content-Type: application/json' \
  -d '{"code":"W-001","title":"夜场录音 A"}'

# 协议（50/30/20）
curl -s localhost:8000/api/v1/agreements -H 'Content-Type: application/json' -d '{
  "work_code":"W-001",
  "valid_from":"2026-01-01T00:00:00Z",
  "valid_to":"2026-07-01T00:00:00Z",
  "shares":[
    {"rightsholder_code":"RH-001","ratio":"50"},
    {"rightsholder_code":"RH-002","ratio":"30"},
    {"rightsholder_code":"RH-003","ratio":"20"}
  ]
}'

# 播放 101 分：floor 50.5/30.3/20.2 -> 50/30/20，余数 1 分给小数最大的 RH-001 -> 51/30/20
curl -s localhost:8000/api/v1/plays -H 'Content-Type: application/json' \
  -d '{"code":"P-0002","work_code":"W-001","occurred_at":"2026-02-11T20:00:00Z","amount":101}'

# 试算（带逐笔明细）
curl -s 'localhost:8000/api/v1/settlement/compute?period_from=2026-01-01T00:00:00Z&period_to=2026-04-01T00:00:00Z&detail=true'

# 出具初始单
curl -s -X POST localhost:8000/api/v1/settlement/statements -H 'Content-Type: application/json' \
  -d '{"period_from":"2026-01-01T00:00:00Z","period_to":"2026-04-01T00:00:00Z"}'
```

---

## 6. 工程结构

```
.
├── app/
│   ├── main.py       # FastAPI 路由与依赖（连接/事务边界）
│   ├── service.py    # 业务逻辑：协议、播放更正链、试算、单据/追补
│   ├── split.py      # 纯函数：逐笔整数分账与余数分配（无第三方依赖）
│   ├── models.py     # Pydantic 请求/响应模型
│   ├── errors.py     # 统一错误模型与处理器
│   ├── db.py         # psycopg 连接池、启动建表、连接等待
│   └── config.py     # 环境变量配置
├── sql/schema.sql    # 表结构、排他约束、比例合计触发器
├── examples/         # walkthrough.sh 与 api.http 调用示例
├── tests/test_split.py  # 分账算法单元测试（标准库 unittest）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 7. 测试

分账算法的单元测试不依赖数据库与第三方库：

```bash
python3 -m unittest discover -s tests -v
```

覆盖：总额守恒（0–249 金额 × 多种比例组合）、余数按小数部分降序/编号升序补足（每人最多补 1 个单位）、零金额、比例非 100 拒绝、负数与空份额拒绝等。

端到端集成测试（真实 PostgreSQL，覆盖登记/重叠/幂等/更正链/分叉/批量/试算/初始单/重复出具/后补协议追补/跨期更正负差）：测试脚本在未提供 `DATABASE_URL` 时会通过 [pgserver](https://pypi.org/project/pgserver/)（便携 PostgreSQL，需另行 `pip install pgserver`）自动拉起一个实例：

```bash
pip install -r requirements.txt pgserver
SCHEMA_PATH=$PWD/sql/schema.sql python tests/integration_test.py
```
