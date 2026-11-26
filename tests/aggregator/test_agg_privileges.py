"""Start-up as root: own the data directory, then drop root."""

import os

import pytest

from aggregator import privileges


@pytest.fixture
def calls(monkeypatch):
    """Record the privileged calls instead of making them."""
    log = []
    monkeypatch.setattr(os, "lchown",
                        lambda p, u, g: log.append(("lchown", str(p), u, g)))
    monkeypatch.setattr(os, "setgroups", lambda g: log.append(("setgroups", g)))
    monkeypatch.setattr(os, "setgid", lambda g: log.append(("setgid", g)))
    monkeypatch.setattr(os, "setuid", lambda u: log.append(("setuid", u)))
    monkeypatch.setenv("HOME", "/root")
    return log


def _as_root(monkeypatch, root=True):
    monkeypatch.setattr(os, "geteuid", lambda: 0 if root else 1000)


def test_not_root_changes_nothing(tmp_path, monkeypatch, calls):
    _as_root(monkeypatch, False)
    assert privileges.drop_privileges(str(tmp_path), 10001, 10001) is False
    assert calls == []
    assert os.environ["HOME"] == "/root"


def test_root_owns_data_dir_then_drops(tmp_path, monkeypatch, calls):
    data = tmp_path / "data"
    (data / "audio").mkdir(parents=True)
    (data / "aggregator.db").write_bytes(b"x")
    (data / "audio" / "a.ogg").write_bytes(b"x")
    st = os.stat(data)
    uid, gid = st.st_uid + 1, st.st_gid + 1
    _as_root(monkeypatch)

    assert privileges.drop_privileges(str(data), uid, gid) is True

    chowned = {c[1] for c in calls if c[0] == "lchown"}
    assert chowned == {str(data), str(data / "audio"),
                       str(data / "aggregator.db"),
                       str(data / "audio" / "a.ogg")}
    assert all(c[2:] == (uid, gid) for c in calls if c[0] == "lchown")
    # Root is given up last, after the files and the groups
    assert [c[0] for c in calls if c[0] != "lchown"] == [
        "setgroups", "setgid", "setuid"]
    assert calls[-3:] == [("setgroups", []), ("setgid", gid), ("setuid", uid)]
    assert os.environ["HOME"] == str(data)


def test_root_creates_missing_data_dir(tmp_path, monkeypatch, calls):
    data = tmp_path / "new" / "data"
    _as_root(monkeypatch)
    privileges.drop_privileges(str(data), 4242, 4242)
    assert data.is_dir()
    assert ("lchown", str(data), 4242, 4242) in calls


def test_already_owned_entries_are_left_alone(tmp_path, monkeypatch, calls):
    (tmp_path / "aggregator.db").write_bytes(b"x")
    st = os.stat(tmp_path)
    _as_root(monkeypatch)
    privileges.drop_privileges(str(tmp_path), st.st_uid, st.st_gid)
    assert [c for c in calls if c[0] == "lchown"] == []
    assert calls[-1] == ("setuid", st.st_uid)


def test_symlinks_are_not_followed(tmp_path, monkeypatch, calls):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"x")
    data = tmp_path / "data"
    data.mkdir()
    (data / "link").symlink_to(outside, target_is_directory=True)
    _as_root(monkeypatch)
    uid = os.stat(data).st_uid + 1

    privileges.drop_privileges(str(data), uid, uid)

    chowned = {c[1] for c in calls if c[0] == "lchown"}
    assert chowned == {str(data), str(data / "link")}


def test_require_writable(tmp_path, monkeypatch):
    privileges.require_writable(str(tmp_path))
    with pytest.raises(SystemExit) as e:
        privileges.require_writable(str(tmp_path / "missing"))
    assert "not writable" in str(e.value)
