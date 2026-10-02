"""Lease 分布式锁模拟器的自动化测试。全部使用 ManualClock，无真实 sleep。"""
from __future__ import annotations

import threading

import pytest

from lease_lock import (
    AutoRenewClient,
    FencedResource,
    FileStore,
    LeaseClient,
    LeaseHeldError,
    LeaseNotHeldError,
    LeaseServer,
    ManualClock,
    OpLog,
    StaleFencingTokenError,
)

TTL = 10.0


@pytest.fixture()
def clock() -> ManualClock:
    return ManualClock(start=1_000.0)


@pytest.fixture()
def server(clock: ManualClock) -> LeaseServer:
    return LeaseServer(clock)


def make_client(server: LeaseServer, name: str) -> LeaseClient:
    return LeaseClient(server, name, ttl=TTL)


# --------------------------------------------------------------- 基本获取/独占
def test_acquire_grants_lease_with_expiry(server, clock):
    lease = server.acquire("res", "a", TTL)
    assert lease.holder_id == "a"
    assert lease.token == 1
    assert lease.expires_at == pytest.approx(clock.now() + TTL)


def test_second_acquire_fails_while_lease_valid(server, clock):
    server.acquire("res", "a", TTL)
    clock.advance(TTL - 0.001)
    with pytest.raises(LeaseHeldError):
        server.acquire("res", "b", TTL)


def test_different_resources_are_independent(server):
    server.acquire("res-1", "a", TTL)
    lease = server.acquire("res-2", "b", TTL)
    assert lease.resource == "res-2"


# ------------------------------------------------------------------- 竞争获取
def test_contending_acquires_exactly_one_winner(server):
    winners: list[str] = []
    barrier = threading.Barrier(16)

    def contender(name: str) -> None:
        barrier.wait()
        try:
            server.acquire("res", name, TTL)
        except LeaseHeldError:
            return
        winners.append(name)

    threads = [threading.Thread(target=contender, args=(f"c{i}",)) for i in range(15)]
    for t in threads:
        t.start()
    barrier.wait()
    for t in threads:
        t.join()

    assert len(winners) == 1
    assert server.inspect("res").holder_id == winners[0]


# ------------------------------------------------------------------------- 续约
def test_renew_extends_expiry(server, clock):
    lease = server.acquire("res", "a", TTL)
    clock.advance(6)
    renewed = server.renew("res", "a", lease.token, TTL)
    assert renewed.expires_at == pytest.approx(clock.now() + TTL)
    assert renewed.token == lease.token  # 续约不产生新 token
    # 原过期点之后租约仍然有效
    clock.advance(TTL - 1)
    with pytest.raises(LeaseHeldError):
        server.acquire("res", "b", TTL)


def test_renew_with_wrong_token_or_holder_rejected(server):
    lease = server.acquire("res", "a", TTL)
    with pytest.raises(LeaseNotHeldError):
        server.renew("res", "b", lease.token, TTL)
    with pytest.raises(LeaseNotHeldError):
        server.renew("res", "a", lease.token + 1, TTL)


# --------------------------------------------------------------------- 租约超时
def test_expired_lease_cannot_be_renewed(server, clock):
    lease = server.acquire("res", "a", TTL)
    clock.advance(TTL + 0.001)
    with pytest.raises(LeaseNotHeldError):
        server.renew("res", "a", lease.token, TTL)


def test_expired_lease_allows_new_holder(server, clock):
    old = server.acquire("res", "a", TTL)
    clock.advance(TTL + 0.001)
    new = server.acquire("res", "b", TTL)
    assert new.token > old.token
    assert server.inspect("res").holder_id == "b"


# ------------------------------------------------------- 客户端暂停恢复 + fencing
def test_paused_client_resume_detected_by_fencing(server, clock):
    downstream = FencedResource()
    client_a = make_client(server, "a")
    client_b = make_client(server, "b")

    lease_a = client_a.acquire("res")
    downstream.write(lease_a.token, "a-write-1")

    # a 暂停超过 lease 时长（不续约），b 获得锁
    clock.advance(TTL + 1)
    lease_b = client_b.acquire("res")
    assert lease_b.token > lease_a.token
    downstream.write(lease_b.token, "b-write-1")

    # a 恢复后：续约被拒绝，旧 token 的写入被下游 fencing 拒绝
    with pytest.raises(LeaseNotHeldError):
        client_a.renew("res")
    with pytest.raises(StaleFencingTokenError):
        downstream.write(lease_a.token, "a-stale-write")
    assert downstream.read() == "b-write-1"


def test_auto_renew_client_pause_and_resume(server, clock):
    downstream = FencedResource()
    client_a = AutoRenewClient(server, "a", clock, ttl=6.0, renew_interval=2.0)
    lease_a = client_a.acquire("res")

    # 自动续约维持租约：每推进一个续约周期，等待 expires_at 被续约推高
    last_expiry = server.inspect("res").expires_at
    for _ in range(5):
        clock.advance(2)
        _wait_for(lambda: server.inspect("res") is not None
                  and server.inspect("res").expires_at > last_expiry)
        last_expiry = server.inspect("res").expires_at
    assert server.inspect("res").holder_id == "a"

    # 暂停超过 lease，b 抢占；a 恢复后续约失败并被记录
    client_a.pause()
    clock.advance(7)
    client_b = make_client(server, "b")
    lease_b = client_b.acquire("res")
    client_a.resume()
    clock.advance(3)  # 让续约线程醒来并尝试续约
    _wait_for(lambda: len(client_a.renew_errors) > 0)
    assert isinstance(client_a.renew_errors[0], LeaseNotHeldError)
    downstream.write(lease_b.token, "fresh")  # 新持有者写入，抬高 fencing 水位
    with pytest.raises(StaleFencingTokenError):
        downstream.write(lease_a.token, "stale")
    assert downstream.read() == "fresh"
    client_a.close()


def _wait_for(pred, attempts: int = 200) -> None:
    import time

    for _ in range(attempts):
        if pred():
            return
        time.sleep(0.005)
    raise AssertionError("condition not met in time")


# ------------------------------------------------------------- fencing token 单调
def test_fencing_tokens_strictly_increasing(server, clock):
    tokens = []
    for i in range(5):
        lease = server.acquire("res", f"c{i}", TTL)
        tokens.append(lease.token)
        server.release("res", f"c{i}", lease.token)
        clock.advance(1)
    assert tokens == sorted(tokens)
    assert len(set(tokens)) == len(tokens)
    # 跨资源也共享同一单调计数器
    other = server.acquire("other", "x", TTL)
    assert other.token > tokens[-1]


def test_old_holder_never_gets_newer_token(server, clock):
    old = server.acquire("res", "a", TTL)
    clock.advance(TTL + 1)
    new = server.acquire("res", "b", TTL)
    assert old.token < new.token
    # 旧持有者恢复后任何操作都失败，且不会获得新 token
    with pytest.raises(LeaseNotHeldError):
        server.renew("res", "a", old.token, TTL)
    with pytest.raises(LeaseNotHeldError):
        server.release("res", "a", old.token)
    assert server.inspect("res").token == new.token


# ----------------------------------------------------------------- 释放后重获
def test_release_then_reacquire(server, clock):
    lease = server.acquire("res", "a", TTL)
    server.release("res", "a", lease.token)
    assert server.inspect("res") is None
    new = server.acquire("res", "b", TTL)
    assert new.token > lease.token


def test_release_with_stale_token_does_not_free_lease(server, clock):
    old = server.acquire("res", "a", TTL)
    clock.advance(TTL + 1)
    new = server.acquire("res", "b", TTL)
    with pytest.raises(LeaseNotHeldError):
        server.release("res", "a", old.token)
    assert server.inspect("res").holder_id == "b"
    assert server.inspect("res").token == new.token


# --------------------------------------------------------------------- 重启恢复
def test_restart_preserves_valid_lease_and_token_counter(tmp_path, clock):
    store_path = tmp_path / "state.json"
    log_path = tmp_path / "ops.log"
    server1 = LeaseServer(clock, FileStore(store_path), OpLog(log_path))
    lease = server1.acquire("res", "a", TTL)
    clock.advance(3)

    # 模拟重启：新实例读取同一状态文件
    server2 = LeaseServer(clock, FileStore(store_path), OpLog(log_path))
    with pytest.raises(LeaseHeldError):
        server2.acquire("res", "b", TTL)
    # fencing token 计数器持久化，重启后继续递增
    server2.release("res", "a", lease.token)
    new = server2.acquire("res", "b", TTL)
    assert new.token > lease.token
    assert log_path.exists()


def test_restart_does_not_restore_expired_lease(tmp_path, clock):
    store_path = tmp_path / "state.json"
    server1 = LeaseServer(clock, FileStore(store_path))
    old = server1.acquire("res", "a", TTL)

    clock.advance(TTL + 5)  # 停机期间租约过期
    server2 = LeaseServer(clock, FileStore(store_path))
    new = server2.acquire("res", "b", TTL)  # 不应被旧租约阻塞
    assert new.holder_id == "b"
    assert new.token > old.token


def test_restart_with_memory_store_loses_state(clock):
    from lease_lock import MemoryStore

    store = MemoryStore()
    server1 = LeaseServer(clock, store)
    server1.acquire("res", "a", TTL)
    server2 = LeaseServer(clock, MemoryStore())  # 新内存 = 无持久化
    lease = server2.acquire("res", "b", TTL)
    assert lease.holder_id == "b"
