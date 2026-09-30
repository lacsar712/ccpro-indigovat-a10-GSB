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

1. **染缸还原台**：横滑缸位条，每缸显示状态、最近电位与 redox sparkline；工坊 chip 上的「N 口可染色」为**去重数**
2. **拼缸出库专页**（顶栏进入）：一单挂同坊多口可染色缸，染缸工维护单头/明细，主管点已出库
3. **工坊 chip**：仅作缸位筛选，无独立工坊 CRUD 页
4. **点缸展开**：同页内登记浸染批次、改状态、看近几笔；无平行「染缸表 / 批次表」

**业务规则**：

- 状态改为 `ready`（可染色）时，最新批次 `redoxMv` 须已填且 ≤ -500（见 `vat_rules.py`）。
- **可染色只有一个口径**（`vat_is_dyeable` / `query_dyeable_vats`）：缸状态 `ready` 且最新浸染批次电位 ≤ -500 mV。还原台可染色去重数、拼缸出库专页可选缸、主管出库时的逐缸复核全部走这一处。
- **米数合计**：计划出库米数必须为正数，上限 = 明细各缸**最新一笔浸染布米之和**；新建与更新单头共用同一校验。
- **出库互斥**：同一口缸在「组单中」单里至多出现一次（DB 部分唯一索引 `uniq_active_vat` 兜底，禁止跨坊挂缸）；已出库单的缸释放后可再次挂新单。
- **整单原子出库**：主管确认时对单据与每口缸加行锁，逐缸复核「仍为可染色且最新读数仍过门槛」；任一缸失败整单拒绝并回滚，绝不半出库。单据行锁串行化并发，两人几乎同时点同一组单中单子时至多一次成功。

### 角色

- `worker`（染缸工）：新建/更新组单中单头与明细、加/移除缸、删单。
- `admin`（主管）：只读组单页，点「确认已出库」触发逐缸门槛复核。

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
4. **OutboundOrder**：归属工坊、`code`（同坊唯一）、`plannedMeters`、状态 `draft|shipped`、开单人
5. **OutboundItem**：出库单↔染缸明细，`released=False` 分区上同缸唯一（未出库互斥）

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
      pages.py       # 还原台
      outbound.py   # 拼缸出库专页
    services/
      vat_rules.py  # 可染色口径/电位门槛（唯一来源）
      outbound.py   # 组单校验 + 整单原子出库
    templates/   # base / bay / login / outbound
```
