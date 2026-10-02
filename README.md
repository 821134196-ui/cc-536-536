# 图书馆阅读挑战系统

按年龄段发布阅读挑战：馆员配置可选书目、借阅期限与完成条件；家长为孩子报名、
提交/补录阅读记录；馆员核对借阅与完成情况，达标后发放实体纪念章的预约领取凭证。

纯 Python 标准库 + 原生前端，**零第三方依赖**，SQLite 存储，一条本地命令启动。

## 启动

```bash
./run.sh            # 默认 http://127.0.0.1:8000
./run.sh 8080       # 指定端口
# 或：python3 -m app
```

首次启动自动建库并写入演示数据（含重复书目案例）。重置数据：删除 `data/` 后重启。

## 三端界面（白 / 藏青 #1d335c / 克制绿 #2f9e6e）

- **孩子端**：书封面墙 + 进度条，状态徽章（草稿 / 已提交 / 已核验 / 需补充）。
- **家长端**：按年龄组报名（开始日周岁边界提示）、提交/补录记录（修改留版本）、
  待核验项、借阅同步状态、提醒通知。
- **馆员端**：左右两栏清楚区分 **已提交待核对** 与 **已借阅未提交**；
  库存、候补队列、补货转正、凭证核销；自己孩子的按钮自动禁用（审核回避）。

## 关键业务规则

| 场景 | 处理 |
|---|---|
| 同一本书重复计数 | 同 ISBN 的不同书目行视为同一本书，入口拒绝 + 达标计数按 ISBN 去重兜底 |
| 借阅记录晚同步 | `LoanAdapter`：借阅先 `pending`，到 `sync_after` 后轮询才 `synced`，未同步不能核验 |
| 家长补录 | 可修改非 verified 记录，每次写一条版本历史，修改后回到草稿需重新提交 |
| 孩子跨年龄组 | 按活动开始日周岁闭区间 `[age_min, age_max]` 判定，可报其他符合的年龄组 |
| 活动中途换书目 | 可加/撤书目（留审计），撤下后旧记录保留但不再计入达标 |
| 馆员审核回避 | `users.own_children` 命中即禁止核验/驳回/达标审核，按钮禁用 + 服务端拦截 |
| 纪念章缺货 | 达标即 `waitlisted`，按达标时间 FIFO 入候补；补货后自动按序转正发凭证 |
| 并发重复发章 | `BEGIN IMMEDIATE` + 条件 UPDATE `stock>0`，两馆员并发恰好一枚成功 |
| 重复领取 | 凭证核销幂等，二次核销提示已领取时间 |

## 模块

```
app/db.py        SQLite schema（活动/书目/报名/记录版本/借阅/审核/勋章/候补/凭证/通知）
app/service.py   领域服务：报名、记录版本、核对、回避、达标、原子发章、候补与补货
app/adapters.py  模拟适配器：延迟借阅 ILS、提醒（notifications 表 + data/reminders.log）
app/seed.py      演示数据（重复书目、6/8 岁边界、延迟借阅、馆员孩子、库存 1 枚竞争）
app/server.py    零依赖 HTTP/JSON API + 静态前端（线程局部连接）
app/web/         三端单页界面，封面为本地生成的 SVG
tests/           13 个测试：去重/年龄边界/借阅延迟/审核回避/库存竞争（含真实线程）
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：同 ISBN 去重与计数兜底、版本留痕、年龄边界（恰好 6/8 岁、差一天）、
跨组报名、借阅延迟轮询、审核回避（核验/驳回/发章三级）、顺序与并发发章、
同报名双馆员并发只发一枚、候补 FIFO 补货转正、凭证幂等核销、中途撤书目。

## 主要 API

`GET /api/activities` · `/api/people` · `/api/children` · `/api/enrollments` ·
`/api/progress?enrollment_id=` · `/api/queue?activity_id=` · `/api/versions?record_id=`
`POST /api/enroll` · `/api/records` · `/api/records/{id}/{submit,verify,reject}` ·
`/api/loans` · `/api/loans/sync` · `/api/approve` · `/api/restock` · `/api/pickup`
