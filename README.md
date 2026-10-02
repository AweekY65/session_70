# Lease 分布式锁模拟器（完全本地）

一个纯本地的 Lease 分布式锁模拟器：多客户端由同一进程内的线程模拟，锁状态、
fencing token 与操作日志只保存在**本地文件或内存**中，不依赖 Redis、etcd、
ZooKeeper 或任何外部服务。

## 功能

- `acquire` / `renew` / `release`，每个 lease 带有明确的过期时间（`expires_at`）
- 同一资源任意时刻最多一个有效 lease；过期租约无法续期
- 每次成功获取产生**严格递增的 fencing token**（全局单调计数器，持久化）
- 所有时间逻辑通过可注入 `Clock` 接口，测试使用 `ManualClock`，零真实 sleep
- 客户端暂停/恢复模拟（`pause()` / `resume()`），旧持有者恢复后被 fencing token 识别
- 本地 JSON 文件持久化（原子写入），重启后过期 lease 不会被错误恢复

## 目录结构

```
lease_lock/
  clock.py           # Clock 接口、SystemClock、ManualClock（可推进的测试时钟）
  server.py          # LeaseServer：acquire / renew / release，状态机与持久化
  client.py          # LeaseClient 与带后台续约线程的 AutoRenewClient
  fenced_resource.py # 下游资源模拟：拒绝过期 fencing token 的写入
  store.py           # MemoryStore / FileStore（原子 JSON 落盘）/ OpLog（JSONL 日志）
  errors.py          # LeaseHeldError / LeaseNotHeldError / StaleFencingTokenError
tests/
  test_lease_lock.py # 17 个自动化测试
```

## Lease 状态机

针对单个资源，租约处于以下状态之一：

```
                 acquire
            ┌───────────────┐
            ▼               │
          FREE ──acquire──▶ HELD ──release──▶ FREE
            ▲               │  ▲
            │          renew│  │（延长 expires_at，token 不变）
            │               ▼  │
            │   clock.now() ≥ expires_at
            └────── EXPIRED ◀──┘
                 （推导状态，不落盘）
```

- `EXPIRED` 不是存储字段，而是由 `clock.now() >= expires_at` 动态推导，
  因此重启后只要时钟正确，过期租约自然失效，不会被“恢复”。
- `renew` 要求 holder 与 token 同时匹配且租约未过期，否则抛 `LeaseNotHeldError`。
- `release` 要求 token 匹配，旧持有者无法释放新持有者的租约。

## Fencing 原理

锁服务内部维护一个**全局单调递增计数器**，每次 `acquire` 成功：

1. 计数器 +1，作为新 lease 的 `token`；
2. 计数器随状态一起持久化，重启后继续递增，绝不回退。

客户端操作下游资源时携带自己的 token。下游资源（`FencedResource`）记录已见
最大 token，拒绝任何 `token <= last_seen` 的写入。因此即使旧持有者在暂停后
恢复、仍误以为自己持有锁，它的写入也会被下游拒绝——这就是 fencing：

```
client A: acquire → token=3 ──暂停──▶ 恢复后 write(token=3) ✗ 被拒绝
client B:            acquire → token=4, write(token=4) ✓ 成功（last_seen=4）
```

## 时间模型

- 服务端**只**通过注入的 `Clock.now()` 读取时间，从不直接调用 `time`。
- 生产环境使用 `SystemClock`（`time.monotonic`）。
- 测试使用 `ManualClock`：时间只能通过 `advance(seconds)` 前进，单调、线程安全；
  `ManualClock.sleep()` 供自动续约线程按虚拟时间阻塞，全部测试无任何真实 sleep。

## 持久化与重启

- `FileStore` 将 `{fencing_token, leases}` 以 JSON 原子写入本地文件
  （tmp 文件 + `os.replace`），崩溃不会产生半截状态。
- 重启时加载持久化状态：fencing 计数器继续递增；lease 是否有效完全由
  `expires_at` 与当前时钟比较决定，**过期 lease 不会被错误恢复**。
- `OpLog` 以 JSONL 追加方式把 acquire/renew/release 及拒绝事件写入本地日志文件。

## 运行测试

```bash
cd /mnt2/zjh/code/Goleta/session_70/b
python3 -m pytest tests/ -v
```

测试覆盖：竞争获取（16 线程仅 1 胜者）、续约延长、租约超时后不可续期、
客户端暂停超过 lease 后恢复（fencing 识别）、token 单调性、释放后重获、
过期 token 无法释放他人租约、重启后有效 lease 保留 / 过期 lease 不恢复等 17 个场景。

## 快速示例

```python
from lease_lock import FileStore, LeaseClient, LeaseServer, ManualClock

clock = ManualClock()
server = LeaseServer(clock, FileStore("state.json"))
a, b = LeaseClient(server, "a", ttl=10), LeaseClient(server, "b", ttl=10)

lease_a = a.acquire("order-db")        # token=1
clock.advance(11)                      # a 暂停超过 lease
lease_b = b.acquire("order-db")        # token=2，b 成为新持有者
a.renew("order-db")                    # 抛 LeaseNotHeldError：旧租约已过期
```
