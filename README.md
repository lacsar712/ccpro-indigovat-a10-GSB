# IndigoVat-01 · 染缸还原台

FastAPI + PostgreSQL + Jinja2：主界面是横向**缸位条**（Alpine 反应式），不是工坊/染缸/批次三表导航。Session Cookie 登录；规则在 `app/services/vat_rules.py`。

## 技术栈

- FastAPI、SQLAlchemy 2、PostgreSQL
- 启动时 `create_all` + 幂等种子（蓝靛湾一号坊 / 清水江二号坊）
- Session Cookie 认证（Starlette SessionMiddleware）
- Jinja2 + Alpine.js + Pico（叠靛蓝水墨自定义样式）
- Docker Compose：`web` + `db`

## 端口与数据库

| 服务 | 端口 |
|------|------|
| Web  | **4720** |
| Postgres | **6120**（容器内 5432） |

数据库账号：`indigovat` / `indigovat` / 库名 `indigovat`

## 快速启动

```bash
cd IndigoVat/IndigoVat-01
docker compose up --build -d
```

浏览器打开：http://localhost:4720

演示账号（登录页已预填）：

- `admin` / `123456`
- `worker` / `123456`

## 交互（信息架构）

1. **染缸还原台**：横滑缸位条，每缸显示状态、最近电位与 redox sparkline；工坊 chip 显示「可染色 N」去重数
2. **工坊 chip**：仅作缸位筛选，无独立工坊 CRUD 页
3. **点缸展开**：同页内登记浸染批次、改状态、看近几笔；无平行「染缸表 / 批次表」
4. **拼缸出库专页**（顶栏进入）：染缸工组一单挂多口**同坊可染色缸**，核对米数；主管点「已出库」整单出库

**业务规则（全系统同一口径，见 `services/vat_rules.py`）**：状态改为 `ready`（可染色）时，最新批次 `redoxMv` 须已填且 ≤ -500 mV。还原台改状态复核、还原台「可染色 N」去重数、拼缸专页可选缸、主管出库逐缸确认，全部走同一函数与同一条 SQL 口径。

### 拼缸出库规则（米数合计与出库互斥）

- **单头字段**：工坊、出库号、计划米数、状态（`grouping` 组单中 / `out` 已出库）、开单人；**同坊出库号唯一**，新建与更新共用同一套唯一与计划米数校验。
- **明细只挂同坊可染色缸**：跨坊挂缸直接拒绝；同缸在任一**未出库单**中不可重复（跨单互斥，数据库部分唯一索引兜底）。
- **米数合计**：计划出库米数须为正数，上限取明细各缸**最新一笔浸染布米之和**（与可选缸的「最新一笔」同一条 SQL）。
- **出库互斥（原子性）**：主管点「已出库」时对单头加行锁，逐缸确认仍为可染色且最新读数仍 ≤ -500 mV；**任一缸失败整单拒绝，绝不半出库**。两人几乎同时点同一张组单，至多一人成功；失败后专页与还原台仍可正常打开。
- 已出库单据不可再编辑；出库成功后其挂缸释放，可挂入新单。

## 本地开发（可选）

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
set POSTGRES_HOST=localhost
set POSTGRES_PORT=6120
uvicorn app.main:app --host 0.0.0.0 --port 4720 --reload
```

## 业务模型

1. **Workshop**：`name`、`region`、`notes`（UI 上仅为筛选片）
2. **Vat**：归属工坊、`code`、`dyeType`、`volumeL`、状态 `idle|reducing|ready`
3. **DipLot**：归属染缸、`dippedAt`、`clothMeters`、`redoxMv`（可空）
4. **OutboundOrder**：归属工坊、`code`（同坊唯一）、`plannedMeters`、状态 `grouping|out`、开单人、出库时间
5. **OutboundItem**：出库单挂缸（单内同缸唯一；`open_flag` + 部分唯一索引保证同缸在未出库单间互斥）

## 目录结构

```
IndigoVat-01/
  Dockerfile
  entrypoint.sh
  docker-compose.yml
  requirements.txt
  app/
    main.py
    db.py
    models.py
    schemas.py
    auth.py
    seed.py
    routers/
    services/vat_rules.py
    templates/   # base / bay / login
```
