"""
Lambda MCP Bridge — sits between QuickSight and AgentCore Runtime.
Extracts JWT from Authorization header and injects it as bearer_token parameter.

QuickSight → API Gateway → This Lambda → AgentCore MCP Runtime → SAP
"""
import json
import os
import logging
import urllib.request
import urllib.parse
import ssl

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AGENTCORE_RUNTIME_ARN = os.environ.get("AGENTCORE_RUNTIME_ARN", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")


def get_mcp_url():
    encoded = AGENTCORE_RUNTIME_ARN.replace(":", "%3A").replace("/", "%2F")
    return (
        f"https://bedrock-agentcore.{AWS_REGION}.amazonaws.com"
        f"/runtimes/{encoded}/invocations?qualifier=DEFAULT"
    )


def lambda_handler(event, context):
    """Handle incoming MCP requests from QuickSight via API Gateway."""
    logger.info("=== LAMBDA MCP BRIDGE INVOKED ===")

    # Log all headers for debugging
    headers = event.get("headers", {}) or {}
    logger.info(f"All headers: {json.dumps(headers)}")

    # Extract JWT from Authorization header
    auth_header = headers.get("authorization", "") or headers.get("Authorization", "")
    jwt_token = ""
    if auth_header.lower().startswith("bearer "):
        jwt_token = auth_header[7:].strip()
        logger.info(f"JWT extracted from Authorization header, length={len(jwt_token)}")
        logger.info(f"JWT first 50 chars: {jwt_token[:50]}...")
    else:
        logger.warning(f"No Bearer token in Authorization header. Header value: '{auth_header[:30]}...'")

    # Parse the MCP request body
    body = event.get("body", "")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            logger.error(f"Invalid JSON body: {body[:200]}")
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"error": "Invalid JSON body"}),
            }

    logger.info(f"MCP request method: {body.get('method')}")
    logger.info(f"MCP request params: {json.dumps(body.get('params', {}))[:500]}")

    # If this is a tools/call request, inject bearer_token into arguments
    if body.get("method") == "tools/call":
        params = body.get("params", {})
        arguments = params.get("arguments", {})
        if jwt_token:
            arguments["bearer_token"] = jwt_token
            params["arguments"] = arguments
            body["params"] = params
            logger.info(f"Injected bearer_token into tool arguments for tool: {params.get('name')}")
        else:
            logger.warning("No JWT to inject — tool call will likely fail")

    # Forward to AgentCore Runtime
    mcp_url = get_mcp_url()
    logger.info(f"Forwarding to AgentCore: {mcp_url[:80]}...")

    # Sign the request with SigV4 for AgentCore
    # We need boto3 for SigV4 signing
    import boto3
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    session = boto3.Session()
    credentials = session.get_credentials().get_frozen_credentials()

    request_body = json.dumps(body)
    forward_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    # Pass through the Authorization header for AgentCore JWT auth
    if auth_header:
        forward_headers["Authorization"] = auth_header

    # For AgentCore with customJWTAuthorizer, we pass the JWT in Authorization header
    # AgentCore validates it and forwards the MCP call to the container
    req = urllib.request.Request(
        mcp_url,
        data=request_body.encode("utf-8"),
        headers=forward_headers,
        method="POST",
    )

    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            response_body = resp.read().decode("utf-8")
            status = resp.status
            resp_headers = dict(resp.headers)
            logger.info(f"AgentCore response status: {status}")
            logger.info(f"AgentCore response body (first 500): {response_body[:500]}")

            # Parse SSE response if needed
            # AgentCore returns text/event-stream with "event: message\ndata: {...}\n"
            result_data = response_body
            if "event: message" in response_body:
                for line in response_body.split("\n"):
                    if line.startswith("data: "):
                        result_data = line[6:]
                        break

            return {
                "statusCode": status,
                "headers": {
                    "Content-Type": "application/json",
                    "Access-Control-Allow-Origin": "*",
                },
                "body": result_data,
            }
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8") if e.fp else ""
        logger.error(f"AgentCore error: {e.code} {e.reason} - {error_body[:500]}")
        return {
            "statusCode": e.code,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": f"AgentCore returned {e.code}: {error_body[:200]}"}),
        }
    except Exception as e:
        logger.error(f"Error forwarding to AgentCore: {e}")
        return {
            "statusCode": 502,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": str(e)}),
        }
