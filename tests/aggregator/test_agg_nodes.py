"""Node tokens and the management CLI."""

import pytest

from aggregator import config, nodes


def test_token_is_hashed_and_authenticates(agg_store):
    node, token = agg_store.create_node("Kalle")
    raw = agg_store._conn().execute("SELECT token_hash FROM nodes").fetchone()[0]
    assert token not in raw
    assert agg_store.authenticate(token)["id"] == node["id"]
    assert agg_store.authenticate(token + "x") is None
    assert agg_store.authenticate("") is None


def test_rotate_invalidates_old_token(agg_store):
    node, old = agg_store.create_node("Kalle")
    new = agg_store.rotate_token(node["id"])
    assert agg_store.authenticate(old) is None
    assert agg_store.authenticate(new)["id"] == node["id"]


def test_duplicate_name(agg_store):
    agg_store.create_node("Kalle")
    with pytest.raises(ValueError):
        agg_store.create_node("Kalle")


@pytest.mark.parametrize("bad", ["", "   ", "x" * 65, "a\nb"])
def test_clean_name_rejects(bad):
    with pytest.raises(ValueError):
        nodes.clean_name(bad)


def test_snippet_uses_public_url(monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_URL", "https://rds.example.com")
    s = nodes.setup_snippet("Kalle", "tok")
    assert "UPLINK_URL=https://rds.example.com\n" in s
    assert "UPLINK_TOKEN=tok\n" in s
    assert "NODE_NAME=Kalle\n" in s


def test_cli_add_list_disable_remove(agg_store, capsys):
    nodes.main(["add", "Kalle"])
    out = capsys.readouterr().out
    assert "UPLINK_TOKEN=" in out
    nodes.main(["disable", "Kalle"])
    assert agg_store.get_node_by_name("Kalle")["enabled"] is False
    nodes.main(["list"])
    assert "disabled" in capsys.readouterr().out
    nodes.main(["remove", "Kalle"])
    assert agg_store.get_node_by_name("Kalle") is None
