import json
import logging

from fre_platform.logging_setup import JsonFormatter, RedactingTextFormatter

URI = "mongodb+srv://bob:hunter2pass@c0.example.mongodb.net/db"


def _record(msg, **extra):
    rec = logging.LogRecord("t", logging.ERROR, __file__, 1, msg, None, None)
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def test_json_formatter_redacts_message_and_extras():
    out = JsonFormatter().format(_record(f"failed {URI}", detail={"uri": URI}))
    assert "hunter2pass" not in out
    data = json.loads(out)
    assert data["level"] == "ERROR" and "****" in data["msg"] and "detail" in data


def test_text_formatter_redacts():
    out = RedactingTextFormatter("%(message)s").format(_record(URI))
    assert "hunter2pass" not in out and "bob:****@" in out
