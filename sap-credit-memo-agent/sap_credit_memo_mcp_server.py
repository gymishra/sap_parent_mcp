"""
SAP Credit Memo Request Monitoring MCP Server
Uses: API_CREDIT_MEMO_REQUEST_SRV + ZI_CREDITMEMO_EXT_CDS
"""
import os, json, logging, httpx
from datetime import date
from collections import defaultdict
from mcp.server.fastmcp import FastMCP, Context

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sap_credit_memo_agent")

SAP_BASE_URL = os.environ.get("SAP_BASE_URL", "https://vhcals4hci.awspoc.club")
mcp = FastMCP("sap_credit_memo_agent", host="0.0.0.0", stateless_http=True)

STD_SVC = "/sap/opu/odata/sap/API_CREDIT_MEMO_REQUEST_SRV"
EXT_SVC = "/sap/opu/odata/sap/ZI_CREDITMEMO_EXT_CDS"

def _get_token(ctx):
    try:
        req = ctx.request_context.request
        # Try all possible header names (case variations)
        for h in ["x-amzn-bedrock-agentcore-runtime-custom-saptoken",
                  "X-Amzn-Bedrock-AgentCore-Runtime-Custom-SapToken",
                  "authorization", "Authorization"]:
            val = req.headers.get(h, "")
            if val:
                token = val.replace("Bearer ", "").replace("bearer ", "") if val.lower().startswith("bearer ") else val
                if token:
                    return token
    except Exception as e:
        logger.warning("Token extraction failed: " + str(e))
    return os.environ.get("SAP_BEARER_TOKEN", "")

def _sap_get(path, token, params=None):
    url = SAP_BASE_URL + path
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    with httpx.Client(verify=False, timeout=30.0) as c:
        r = c.get(url, headers=headers, params=params or {})
        r.raise_for_status()
        return r.json()

def _parse_date(val):
    if not val:
        return ""
    s = str(val)
    if "/Date(" in s:
        try:
            ms = int(s.split("(")[1].split(")")[0])
            return date.fromtimestamp(ms / 1000).isoformat()
        except Exception:
            return s
    return s


@mcp.tool()
def get_unapproved_credit_memos(ctx: Context, sales_org: str = "", top: int = 100) -> str:
    """Get credit memo requests that are still unapproved.
    Uses custom CDS view ZI_CREDITMEMO_EXT_CDS for reason code, net value, approval status.
    Optional: filter by sales_org."""
    token = _get_token(ctx)
    if not token:
        return json.dumps({"error": "No bearer token"})
    try:
        params = {"$format": "json", "$top": str(top)}
        f = "ApprovalStatus ne 'A'"
        if sales_org:
            f = f + " and SalesOrganization eq '" + sales_org + "'"
        params["$filter"] = f
        data = _sap_get(EXT_SVC + "/ZI_CREDITMEMO_EXT", token, params)
        items = data.get("d", {}).get("results", [])
        result = []
        for i in items:
            result.append({
                "CreditMemoRequest": i.get("CreditMemoRequestNumber", ""),
                "ReasonCode": i.get("ReasonCode", ""),
                "NetValue": float(i.get("NetValue") or 0),
                "Currency": i.get("Currency", ""),
                "SoldToParty": i.get("SoldToParty", ""),
                "SalesOrg": i.get("SalesOrganization", ""),
                "CreationDate": i.get("CreationDate", ""),
                "ApprovalStatus": i.get("ApprovalStatus", ""),
            })
        total = round(sum(r["NetValue"] for r in result), 2)
        return json.dumps({"status": "success", "count": len(result),
                           "total_exposure": total, "invoices": result})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_credit_memo_aging(ctx: Context, sales_org: str = "", top: int = 200) -> str:
    """Credit memo aging analysis grouped by sales org and reason code.
    Returns aging buckets: 0-30, 31-60, 60+ days with total exposure."""
    token = _get_token(ctx)
    if not token:
        return json.dumps({"error": "No bearer token"})
    try:
        params = {"$format": "json", "$top": str(top)}
        if sales_org:
            params["$filter"] = "SalesOrganization eq '" + sales_org + "'"
        data = _sap_get(EXT_SVC + "/ZI_CREDITMEMO_EXT", token, params)
        items = data.get("d", {}).get("results", [])
        today = date.today()
        summary = defaultdict(lambda: {
            "0-30 days": {"count": 0, "total": 0.0},
            "31-60 days": {"count": 0, "total": 0.0},
            "60+ days": {"count": 0, "total": 0.0},
            "total_count": 0, "total_exposure": 0.0
        })
        for i in items:
            key = i.get("SalesOrganization", "?") + "|" + i.get("ReasonCode", "?")
            amt = abs(float(i.get("NetValue") or 0))
            created = i.get("CreationDate", "")
            days = 0
            if created:
                try:
                    days = (today - date.fromisoformat(str(created)[:10])).days
                except Exception:
                    pass
            bucket = "0-30 days" if days <= 30 else "31-60 days" if days <= 60 else "60+ days"
            s = summary[key]
            s[bucket]["count"] += 1
            s[bucket]["total"] = round(s[bucket]["total"] + amt, 2)
            s["total_count"] += 1
            s["total_exposure"] = round(s["total_exposure"] + amt, 2)
        result = []
        for key, v in summary.items():
            parts = key.split("|")
            result.append({"SalesOrg": parts[0], "ReasonCode": parts[1], **v})
        result.sort(key=lambda x: x["total_exposure"], reverse=True)
        return json.dumps({"status": "success", "groups": len(result), "summary": result})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_repeat_offenders(ctx: Context, min_requests: int = 3, top: int = 500) -> str:
    """Find customers with more than N credit memo requests. Flags repeat offenders."""
    token = _get_token(ctx)
    if not token:
        return json.dumps({"error": "No bearer token"})
    try:
        data = _sap_get(EXT_SVC + "/ZI_CREDITMEMO_EXT", token,
                        {"$format": "json", "$top": str(top)})
        items = data.get("d", {}).get("results", [])
        customers = defaultdict(lambda: {"count": 0, "total": 0.0, "currency": ""})
        for i in items:
            cust = i.get("SoldToParty", "")
            if not cust:
                continue
            customers[cust]["count"] += 1
            customers[cust]["total"] = round(
                customers[cust]["total"] + abs(float(i.get("NetValue") or 0)), 2)
            customers[cust]["currency"] = i.get("Currency", "")
        offenders = [{"Customer": k, **v} for k, v in customers.items()
                     if v["count"] >= min_requests]
        offenders.sort(key=lambda x: x["count"], reverse=True)
        return json.dumps({"status": "success", "threshold": min_requests,
                           "offenders": len(offenders), "results": offenders})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_credit_memo_process_flow(ctx: Context, credit_memo_id: str = "", top: int = 50) -> str:
    """Get billing/approval process flow for credit memo requests.
    Uses standard API_CREDIT_MEMO_REQUEST_SRV."""
    token = _get_token(ctx)
    if not token:
        return json.dumps({"error": "No bearer token"})
    try:
        params = {"$format": "json", "$top": str(top)}
        if credit_memo_id:
            params["$filter"] = "CreditMemoRequest eq '" + credit_memo_id + "'"
        data = _sap_get(STD_SVC + "/A_CreditMemoReqSubsqntProcFlow", token, params)
        items = data.get("d", {}).get("results", [])
        result = []
        for i in items:
            result.append({
                "CreditMemoRequest": i.get("CreditMemoRequest", ""),
                "SubsequentDocument": i.get("SubsequentDocument", ""),
                "SDDocumentCategory": i.get("SDDocumentCategory", ""),
                "OverallBillingStatus": i.get("OverallSDProcessStatus", ""),
                "CreationDate": _parse_date(i.get("CreationDate")),
            })
        return json.dumps({"status": "success", "count": len(result), "flow": result})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_credit_exposure_summary(ctx: Context, top: int = 500) -> str:
    """Total credit exposure grouped by sales org, reason code, and approval status."""
    token = _get_token(ctx)
    if not token:
        return json.dumps({"error": "No bearer token"})
    try:
        data = _sap_get(EXT_SVC + "/ZI_CREDITMEMO_EXT", token,
                        {"$format": "json", "$top": str(top)})
        items = data.get("d", {}).get("results", [])
        groups = defaultdict(lambda: {"count": 0, "total": 0.0, "currency": ""})
        for i in items:
            key = (i.get("SalesOrganization", "?") + "|" +
                   i.get("ReasonCode", "?") + "|" +
                   i.get("ApprovalStatus", "?"))
            groups[key]["count"] += 1
            groups[key]["total"] = round(
                groups[key]["total"] + abs(float(i.get("NetValue") or 0)), 2)
            groups[key]["currency"] = i.get("Currency", "")
        result = []
        for key, v in groups.items():
            parts = key.split("|")
            result.append({"SalesOrg": parts[0], "ReasonCode": parts[1],
                           "ApprovalStatus": parts[2], **v})
        result.sort(key=lambda x: x["total"], reverse=True)
        grand_total = round(sum(r["total"] for r in result), 2)
        return json.dumps({"status": "success", "groups": len(result),
                           "grand_total": grand_total, "summary": result})
    except Exception as e:
        return json.dumps({"error": str(e)})


if __name__ == "__main__":
    logger.info("=== SAP Credit Memo Agent Starting ===")
    mcp.run(transport="streamable-http")
