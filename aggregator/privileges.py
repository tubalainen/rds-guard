"""Hand the data directory to an unprivileged user and stop being root.

The data directory is a folder on the host (./aggregator-data:/data), and
Docker creates such a folder owned by root.  So the container starts as
root, makes the folder writable for the service user, and then drops root
for good, before the database is opened or anything listens on the network.

Started as another user already (``user:`` in compose, ``docker run
--user``), nothing is changed; that user then has to own the folder itself.
"""

import logging
import os

from . import config

log = logging.getLogger("rds-aggregator")


def _chown_tree(top, uid, gid):
    """Give top and everything below it to uid:gid. Returns entries changed."""
    changed = 0

    def fix(path):
        nonlocal changed
        st = os.lstat(path)
        if st.st_uid != uid or st.st_gid != gid:
            # lchown: a symlink is re-owned itself, never the file it names
            os.lchown(path, uid, gid)
            changed += 1

    fix(top)
    for root, dirs, files in os.walk(top):
        for name in dirs + files:
            fix(os.path.join(root, name))
    return changed


def drop_privileges(data_dir=None, uid=None, gid=None):
    """If running as root: own the data directory as uid:gid, become that user.

    Returns True when privileges were dropped, False when the process was
    not root to begin with.
    """
    if os.geteuid() != 0:
        return False
    data_dir = config.DATA_DIR if data_dir is None else data_dir
    uid = config.RUN_UID if uid is None else uid
    gid = config.RUN_GID if gid is None else gid

    os.makedirs(data_dir, exist_ok=True)
    changed = _chown_tree(data_dir, uid, gid)
    if changed:
        log.info("Data directory %s: gave %d entries to uid %d, gid %d",
                 data_dir, changed, uid, gid)

    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    os.environ["HOME"] = data_dir
    return True


def require_writable(data_dir=None):
    """Exit with a clear message when the data directory cannot be written."""
    data_dir = config.DATA_DIR if data_dir is None else data_dir
    if os.path.isdir(data_dir) and os.access(data_dir, os.W_OK | os.X_OK):
        return
    raise SystemExit(
        f"Data directory {data_dir} is not writable for uid {os.geteuid()}. "
        "Give the mounted folder to that user on the host, e.g. "
        f"'sudo chown -R {os.geteuid()}:{os.getegid()} aggregator-data'.")
