"""The envelope middleware on its own (second layer behind ToolKit.guard).
Mutation M11 showed the end-to-end test cannot tell the layers apart."""

import json

import pytest

from driveguard.mcp_servers.common import envelope_middleware, normalize_error_text


@pytest.mark.parametrize("raw,code", [
    ("Error executing tool get_trip", "INTERNAL"),
    ('Error executing tool get_trip: {"code": "FORBIDDEN", "message": "m", "retryable": false}', "FORBIDDEN"),
    ("Error executing tool x: 1 validation error for xArguments\nlimit\n  Input should be less than or equal "
     "to 500 [type=less_than_equal, input_value=9999, input_type=int]\n    For further information visit "
     "https://errors.pydantic.dev/2/v/less_than_equal", "INVALID_ARGUMENT"),
    ("Unknown tool: drop_tables", "NOT_FOUND"),
    ("something unexpected", "INVALID_ARGUMENT"),
])
def test_normalize(raw, code):
    env = json.loads(normalize_error_text(raw))
    assert env["code"] == code and set(env) == {"code", "message", "retryable"}
    assert "9999" not in env["message"] and "pydantic.dev" not in env["message"]
    assert normalize_error_text(normalize_error_text(raw)) == normalize_error_text(raw), "idempotent"


async def test_middleware_rewrites_only_tool_errors():
    class Ctx:
        def __init__(self, method):
            self.method = method

    async def nxt_err(_):
        return {"isError": True, "content": [{"type": "text", "text": "Error executing tool t"}],
                "structuredContent": {"leak": "x"}}

    out = await envelope_middleware(Ctx("tools/call"), nxt_err)
    assert json.loads(out["content"][0]["text"])["code"] == "INTERNAL" and "structuredContent" not in out

    async def nxt_ok(_):
        return {"isError": False, "content": [{"type": "text", "text": "Error executing tool t"}]}

    assert (await envelope_middleware(Ctx("tools/call"), nxt_ok))["content"][0]["text"] == "Error executing tool t"
    assert (await envelope_middleware(Ctx("tools/list"), nxt_err))["content"][0]["text"] == "Error executing tool t"
