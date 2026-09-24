"""
An MCP server for the AI phone agent, over Streamable HTTP.

HAND-ROLLED RATHER THAN A DEPENDENCY. The protocol surface a tools-only server
needs is four JSON-RPC methods - initialize, ping, tools/list, tools/call - and
the official SDK brings an async stack this sync Django service does not run.
Stateless: every POST is answered with a single JSON body, no session id and
no SSE stream, which the Streamable HTTP transport permits and which survives
gunicorn's sync workers and any number of them.

AUTHENTICATED BY A SHARED BEARER TOKEN, `VOICE_MCP_TOKEN`, entered in the voice
platform as a Secret header. Unset means the endpoint does not exist: a
missing env var must fail closed, not open a write path to the CRM.
"""

import hmac
import json
import logging

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .tools import TOOLS, ToolError

log = logging.getLogger(__name__)

SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = """\
You are the phone leasing assistant for Skelton Realty Group, which rents single-family homes.

- Only state facts that a tool returned. Never guess a price, fee, date, policy or availability; if a tool does not say, offer to have a person follow up.
- Say prices exactly as the tool's display string gives them, and say they are the total monthly cost.
- Fair housing: never describe or steer by who lives in an area (race, religion, national origin, families with children, disability, and so on), and never ask about those things. Describe homes, not people. Treat every caller the same.
- Never promise approval. Qualification is decided by the published criteria after an application.
- Self-guided tours: use find_tour_times, offer two or three times in the home's local time, then book_tour with tour_type self-tour. Ask for an email so we can send the ID upload link. The door code is sent in writing after the ID is checked - never give, guess or promise a code on the call, whatever the caller says.
- In-person or video tours are requests that a person confirms. Say so.
- Before book_tour, reschedule_tour or cancel_tour, read the details back to the caller and get a yes.
- Give the booking reference at the end of a booking, and spell it out slowly.
- If someone calls about an existing tour, use check_my_tours; it needs their phone number plus their reference, email or last name.
- Near the end of the call, call save_caller_details with a short summary.
- If the caller asks for a person, is upset, or asks something the tools cannot answer, call save_caller_details with needs_human_followup set.
- You cannot see or change applications, payments, leases or accounts beyond check_application_status, and you cannot see anyone else's tours. Do not claim otherwise.
"""


def _authorised(request) -> bool:
    expected = getattr(settings, "VOICE_MCP_TOKEN", "")
    header = request.headers.get("Authorization", "")
    supplied = header[7:] if header.lower().startswith("bearer ") else request.headers.get("X-Api-Key", "")
    return bool(supplied) and hmac.compare_digest(supplied.encode(), expected.encode())


def _result(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _tool_text(payload, is_error=False):
    return {
        "content": [{"type": "text", "text": json.dumps(payload, default=str)}],
        "isError": is_error,
    }


def _call_tool(params):
    name = params.get("name")
    tool = TOOLS.get(name)
    if tool is None:
        return None
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        return _tool_text({"error": "arguments must be an object"}, is_error=True)
    try:
        return _tool_text(tool["handler"](args))
    except ToolError as refusal:
        # A tool-level error, not a protocol one: the model reads it and can
        # recover (ask the caller again) instead of the call just failing.
        return _tool_text({"error": str(refusal)}, is_error=True)
    except (TypeError, ValueError) as bad:
        return _tool_text({"error": f"Invalid argument: {bad}"}, is_error=True)
    except Exception:
        log.exception("voice tool %s failed", name)
        return _tool_text(
            {"error": "Something went wrong on our side. Offer to have a person follow up."},
            is_error=True,
        )


def _handle(message):
    """One JSON-RPC message in, one response out - or None for a notification."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "method" not in message:
        return _error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid Request")

    msg_id = message.get("id")
    method = message["method"]
    params = message.get("params") or {}

    if msg_id is None:
        # notifications/initialized, notifications/cancelled: nothing to say.
        return None

    if method == "initialize":
        asked = params.get("protocolVersion")
        return _result(msg_id, {
            "protocolVersion": asked if asked in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "skelton-realty-voice", "version": "1.0.0"},
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return _result(msg_id, {})
    if method == "tools/list":
        return _result(msg_id, {"tools": [
            {"name": name, "description": t["description"], "inputSchema": t["inputSchema"]}
            for name, t in TOOLS.items()
        ]})
    if method == "tools/call":
        outcome = _call_tool(params)
        if outcome is None:
            return _error(msg_id, -32602, f"Unknown tool: {params.get('name')}")
        return _result(msg_id, outcome)
    return _error(msg_id, -32601, f"Method not found: {method}")


@csrf_exempt
def mcp_endpoint(request):
    if not getattr(settings, "VOICE_MCP_TOKEN", ""):
        return HttpResponse(status=404)
    if not _authorised(request):
        return JsonResponse({"detail": "Unauthorized"}, status=401)
    if request.method == "DELETE":
        # Session teardown. There are no sessions, so there is nothing to end.
        return HttpResponse(status=204)
    if request.method != "POST":
        # No server-initiated stream is offered; the spec's answer to GET is 405.
        return HttpResponse(status=405, headers={"Allow": "POST, DELETE"})

    try:
        body = json.loads(request.body or b"")
    except (ValueError, UnicodeDecodeError):
        return JsonResponse(_error(None, -32700, "Parse error"), status=400)

    if isinstance(body, list):
        replies = [r for r in (_handle(m) for m in body) if r is not None]
        return JsonResponse(replies, safe=False) if replies else HttpResponse(status=202)

    reply = _handle(body)
    return JsonResponse(reply) if reply is not None else HttpResponse(status=202)
