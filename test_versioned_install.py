"""Versioned upgrades must succeed while old executables deny delete sharing."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import windows_install as wi
from test_install_file_access import NoDeleteShare


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows installer")


@pytest.fixture
def install_case(tmp_path, monkeypatch):
    source = tmp_path / "download.exe"
    source.write_bytes(b"new-version")
    root = tmp_path / "installed"
    root.mkdir()
    previous = root / "QuotaDock.exe"
    previous.write_bytes(b"old-version")
    events = {"launches": [], "stops": [], "settings": [], "failure": None}

    class Settings:
        def __init__(self, target, *args):
            self.target = target

        def prepare(self):
            events["settings"].append("prepare")

        def apply(self):
            events["settings"].append(self.target)
            if events["failure"] == "settings":
                raise OSError("settings failure")

        def rollback(self):
            events["settings"].append("rollback")

    def run(args, **kwargs):
        events["stops"].append((kwargs["env"]["QUOTADOCK_INSTALL_TARGET"],
                                kwargs["env"]["QUOTADOCK_PREVIOUS_TARGET"]))
        return SimpleNamespace(stdout=b"")

    def launch(args, **kwargs):
        events["launches"].append(Path(args[0]))
        return SimpleNamespace()

    def ready(*args):
        if events["failure"] == "startup":
            raise RuntimeError("startup failure")

    monkeypatch.setattr(wi.UpdateOrigin, "from_environment", lambda: None)
    monkeypatch.setattr(wi, "InstallSettings", Settings)
    monkeypatch.setattr(wi.subprocess, "run", run)
    monkeypatch.setattr(wi.subprocess, "Popen", launch)
    monkeypatch.setattr(wi, "wait_for_startup", ready)
    return source, root, previous, events


def test_upgrade_succeeds_with_native_lock_on_previous_exe(install_case):
    source, root, previous, events = install_case
    holder = NoDeleteShare(previous)
    try:
        target = wi.install_versioned_release(source, root, "1.4.8", True)
        assert previous.read_bytes() == b"old-version"
        assert target.read_bytes() == b"new-version"
        assert wi.active_install_target(root) == target
        assert events["launches"] == [target]
        assert events["settings"] == ["prepare", target]
        assert events["stops"] == [(str(target), str(previous))]
    finally:
        holder.close()


def test_next_upgrade_and_same_version_reinstall_use_new_paths(install_case):
    source, root, original, events = install_case
    first = wi.install_versioned_release(source, root, "1.4.8", False)
    holder = NoDeleteShare(first)
    try:
        second = wi.install_versioned_release(source, root, "1.4.8", False)
    finally:
        holder.close()
    assert first != second
    assert first.read_bytes() == second.read_bytes() == b"new-version"
    assert wi.active_install_target(root) == second
    assert events["stops"][-1] == (str(second), str(first))
    assert original.read_bytes() == b"old-version"


@pytest.mark.parametrize("failure", ["startup", "settings", "activation"])
@pytest.mark.parametrize("versioned_previous", [False, True])
def test_failure_restores_pointer_settings_and_restarts_previous(
        install_case, monkeypatch, failure, versioned_previous):
    source, root, previous, events = install_case
    if versioned_previous:
        previous = wi.install_versioned_release(source, root, "1.4.7", False)
    pointer = root / "current.json"
    original_pointer = pointer.read_bytes() if pointer.exists() else None
    events["failure"] = failure
    if failure == "activation":
        atomic = wi._atomic_bytes
        calls = []

        def fail_once(path, data):
            calls.append(True)
            if len(calls) == 1:
                raise OSError("activation failure")
            atomic(path, data)

        monkeypatch.setattr(wi, "_atomic_bytes", fail_once)
    with pytest.raises(RuntimeError, match="failure"):
        wi.install_versioned_release(source, root, "1.4.8", True)
    assert wi.active_install_target(root) == previous
    assert (pointer.read_bytes() if pointer.exists() else None) == original_pointer
    assert events["launches"][-1] == previous
    assert events["settings"][-1] == "rollback"
    assert previous.is_file()


def test_inactive_failed_new_exe_need_not_be_deleted_to_recover(install_case, monkeypatch):
    source, root, previous, events = install_case
    holder = []

    def locked_failure(*args):
        holder.append(NoDeleteShare(events["launches"][-1]))
        raise RuntimeError("startup failure")

    monkeypatch.setattr(wi, "wait_for_startup", locked_failure)
    try:
        with pytest.raises(RuntimeError, match="startup failure"):
            wi.install_versioned_release(source, root, "1.4.8", False)
        assert events["launches"][-1] == previous
        assert wi.active_install_target(root) == previous
    finally:
        for lock in holder:
            lock.close()


def test_concurrent_install_is_rejected_before_stopping_old_app(install_case):
    source, root, previous, events = install_case
    with wi._install_lock(root):
        with pytest.raises(RuntimeError, match="另一個更新安裝"):
            wi.install_versioned_release(source, root, "1.4.8", False)
    assert not events["stops"] and not events["launches"]


@pytest.mark.parametrize("value", ["../outside/QuotaDock.exe", "QuotaDock.exe", "/QuotaDock.exe"])
def test_invalid_active_pointer_does_not_stop_any_process(install_case, value):
    source, root, previous, events = install_case
    (root / "current.json").write_text(json.dumps({"executable": value}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="版本指標無效"):
        wi.install_versioned_release(source, root, "1.4.8", False)
    assert not events["stops"] and previous.read_bytes() == b"old-version"
