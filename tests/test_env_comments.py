"""Docker Compose reads `KEY=        # comment` in .env as the value
"# comment": such values are taken as empty, others are left alone."""

import config
from aggregator import config as agg_config


def test_a_comment_only_value_is_empty(monkeypatch):
    for mod in (config, agg_config):
        monkeypatch.setenv("TRANSCRIPTION_MODEL", "# empty = bundled KB-Whisper small")
        monkeypatch.setenv("NODE_NAME", "  # Shown on the aggregator")
        monkeypatch.setenv("JUST_HASH", "#")
        monkeypatch.setenv("ADMIN_PASSWORD", "#hemligt")      # a value, not a comment
        monkeypatch.setenv("FM_FREQUENCY", "103.5M")
        found = mod._drop_env_comments()
        import os
        assert os.environ["TRANSCRIPTION_MODEL"] == "" and os.environ["NODE_NAME"] == ""
        assert os.environ["JUST_HASH"] == ""
        assert os.environ["ADMIN_PASSWORD"] == "#hemligt"
        assert os.environ["FM_FREQUENCY"] == "103.5M"
        assert {"TRANSCRIPTION_MODEL", "NODE_NAME", "JUST_HASH"} <= set(found)


def test_the_examples_have_no_comment_where_a_value_goes():
    import re
    for path in (".env.example", "aggregator/.env.example"):
        with open(path, encoding="utf-8") as f:
            bad = [l for l in f if re.match(r"^[A-Z_]+=\s+#", l)]
        assert bad == [], f"{path}: {bad}"
