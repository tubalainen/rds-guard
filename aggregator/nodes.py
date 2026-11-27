"""Node (satellite) management: setup snippet + command-line interface.

Usage inside the aggregator container:

    python3 -m aggregator.nodes add "Kalle, Karlstad"
    python3 -m aggregator.nodes list
    python3 -m aggregator.nodes rotate "Kalle, Karlstad"
    python3 -m aggregator.nodes disable "Kalle, Karlstad"
    python3 -m aggregator.nodes enable "Kalle, Karlstad"
    python3 -m aggregator.nodes remove "Kalle, Karlstad"
"""

import argparse
import sys
from pathlib import Path

from . import config, privileges, store

MAX_NAME_LEN = 64


def clean_name(name):
    """Validate a node name. Returns it stripped, or raises ValueError."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Name must not be empty")
    if len(name) > MAX_NAME_LEN:
        raise ValueError(f"Name must be at most {MAX_NAME_LEN} characters")
    if any(ord(c) < 32 for c in name):
        raise ValueError("Name must not contain control characters")
    return name


def setup_snippet(node_name, token):
    """The .env lines to send to the person running the satellite."""
    url = config.PUBLIC_URL or "https://<your-aggregator-address>"
    return (
        f"UPLINK_URL={url}\n"
        f"UPLINK_TOKEN={token}\n"
        f"NODE_NAME={node_name}\n"
    )


def remove_audio_files(paths):
    audio_dir = Path(config.AUDIO_DIR)
    for p in paths or []:
        try:
            (audio_dir / Path(p).name).unlink()
        except FileNotFoundError:
            pass


def _find(name):
    node = store.get_node_by_name(name)
    if node is None:
        print(f"No node named '{name}'", file=sys.stderr)
        sys.exit(1)
    return node


def _print_token(node_name, token):
    print("Token (shown only once):")
    print(f"  {token}")
    print()
    print("Send these lines to the satellite owner for their .env file:")
    print()
    print(setup_snippet(node_name, token))


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python3 -m aggregator.nodes",
        description="Manage RDS Guard satellites (nodes) of this aggregator.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="List nodes")
    sub.add_parser("pending", help="List satellites waiting to be approved")
    p = sub.add_parser("approve", help="Approve a waiting satellite by its code")
    p.add_argument("code", help="the code in the satellite's log, e.g. K7Q-3PD")
    p.add_argument("--name", help="node name (default: the one it sent)")
    p.add_argument("--location", help="where it is, 'Ort, Län'")
    p.add_argument("--replace", metavar="NODE",
                   help="give this existing node the satellite's token instead")
    p = sub.add_parser("reject", help="Reject a waiting satellite by its code")
    p.add_argument("code")
    for cmd, helptext in (("add", "Create a node and print its token"),
                          ("rotate", "Replace a node's token"),
                          ("disable", "Reject a node's reports"),
                          ("enable", "Accept a node's reports again"),
                          ("remove", "Delete a node and all its events")):
        p = sub.add_parser(cmd, help=helptext)
        p.add_argument("name")
    args = parser.parse_args(argv)

    store.init_db(config.DB_PATH)

    if args.cmd == "list":
        nodes = store.list_nodes()
        if not nodes:
            print("No nodes yet. Create one with: add \"<name>\"")
        for n in nodes:
            state = "enabled " if n["enabled"] else "disabled"
            print(f"{n['name']:<32} {state}  token {n['token_prefix']}…  "
                  f"last seen {n['last_seen_at'] or 'never'}")
        return

    if args.cmd == "pending":
        reqs = store.list_join_requests()
        if not reqs:
            print("No satellites are waiting.")
        for r in reqs:
            print(f"{r['verify']}  {r['node_name'] or '(no name)':<32} "
                  f"{r['suggested_location'] or '-':<28} {r['address'] or '?':<16} "
                  f"{r['version'] or ''}  since {r['created_at']}")
        return

    if args.cmd in ("approve", "reject"):
        req = store.join_request_by_verify(args.code)
        if req is None:
            print(f"No waiting satellite with code {args.code}.", file=sys.stderr)
            sys.exit(1)
        if args.cmd == "reject":
            store.reject_join(req["id"])
            print(f"Rejected {req['verify']} ({req['node_name']}).")
            return
        replace = _find(args.replace)["id"] if args.replace else None
        try:
            name = clean_name(args.name) if args.name else store.free_name(
                req["node_name"] or f"Nod {req['verify']}")
            node = store.approve_join(req["id"], name, args.location or
                                      req["suggested_location"], replace)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
        print(f"Approved: the satellite now reports as node '{node['name']}'.")
        return

    if args.cmd == "add":
        try:
            name = clean_name(args.name)
            node, token = store.create_node(name)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
        print(f"Node '{node['name']}' created.")
        _print_token(node["name"], token)
        return

    node = _find(args.name)
    if args.cmd == "rotate":
        token = store.rotate_token(node["id"])
        print(f"New token for '{node['name']}'. The old one no longer works.")
        _print_token(node["name"], token)
    elif args.cmd == "disable":
        store.set_node_enabled(node["id"], False)
        print(f"Node '{node['name']}' disabled.")
    elif args.cmd == "enable":
        store.set_node_enabled(node["id"], True)
        print(f"Node '{node['name']}' enabled.")
    elif args.cmd == "remove":
        remove_audio_files(store.delete_node(node["id"]))
        print(f"Node '{node['name']}' and its events removed.")


if __name__ == "__main__":
    # 'docker compose exec' starts this as root; write the database as the
    # same unprivileged user as the server does.
    privileges.drop_privileges()
    main()
