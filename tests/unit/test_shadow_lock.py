import pytest

from trading_agent.shadow.lock import ShadowLock, ShadowLockError


def test_acquire_then_release_allows_reacquisition(tmp_path):
    lock_path = tmp_path / "shadow.lock"
    lock = ShadowLock(lock_path)
    lock.acquire()
    lock.release()

    other = ShadowLock(lock_path)
    other.acquire()
    other.release()


def test_concurrent_acquisition_raises_shadow_lock_error(tmp_path):
    lock_path = tmp_path / "shadow.lock"
    first = ShadowLock(lock_path)
    first.acquire()
    try:
        second = ShadowLock(lock_path)
        with pytest.raises(ShadowLockError):
            second.acquire()
    finally:
        first.release()


def test_context_manager_releases_on_exit_even_on_exception(tmp_path):
    lock_path = tmp_path / "shadow.lock"
    with pytest.raises(RuntimeError), ShadowLock(lock_path):
        raise RuntimeError("boom")

    # released - a fresh lock can acquire it immediately.
    other = ShadowLock(lock_path)
    other.acquire()
    other.release()


def test_lock_creates_parent_directory(tmp_path):
    lock_path = tmp_path / "nested" / "dir" / "shadow.lock"
    lock = ShadowLock(lock_path)
    lock.acquire()
    try:
        assert lock_path.exists()
    finally:
        lock.release()
