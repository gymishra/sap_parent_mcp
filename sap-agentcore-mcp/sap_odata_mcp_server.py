"""SAP OData MCP Server - bearer_token as parameter (working version)."""
import os, json, logging, httpx, sys
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sap_mcp")

SAP_BASE_URL = os.environ.get("SAP_BASE_URL", "https://vhcals4hci.awspoc.club")
SAP_ODATA = "/sap/opu/odata/sap/API_SALES_ORDER_SRV/A_SalesOrder"
mcp = FastMCP(host="0.0.0.0", stateless_http=True)

def _call_sap(path, token, params=None):
    url = f"{SAP_BASE_URL}{path}"
    with httpx.Client(verify=False, timeout=30.0) as c:
        r = c.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}, params=params or {})
        r.raise_for_status()
        return r.json()

@mcp.tool()
def get_sales_orders(top: int = 10, skip: int = 0, bearer_token: str = "") -> str:
    """Get sales orders from SAP S/4HANA.
    Args:
        top: Number of sales orders to return (default 10)
        skip: Number of records to skip (default 0)
        bearer_token: OAuth2 bearer token for SAP (optional)
    """
    token = bearer_token or os.environ.get("SAP_BEARER_TOKEN", "")
    if not token:
        return json.dumps({"error": "No bearer token"})
    try:
        data = _call_sap(SAP_ODATA, token, {"$top": str(top), "$skip": str(skip), "$format": "json"})
        results = data.get("d",{}).get("results",[])
        orders = [{"SalesOrder": r.get("SalesOrder"), "SalesOrderType": r.get("SalesOrderType"),
            "SoldToParty": r.get("SoldToParty"), "CreationDate": r.get("CreationDate"),
            "TotalNetAmount": r.get("TotalNetAmount"), "TransactionCurrency": r.get("TransactionCurrency"),
            "OverallSDProcessStatus": r.get("OverallSDProcessStatus")} for r in results]
        return json.dumps(orders, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})

@mcp.tool()
def get_sales_order_by_id(sales_order_id: str, bearer_token: str = "") -> str:
    """Get a specific sales order by ID from SAP S/4HANA.
    Args:
        sales_order_id: The sales order number (e.g. '0000000001')
        bearer_token: OAuth2 bearer token for SAP (optional)
    """
    token = bearer_token or os.environ.get("SAP_BEARER_TOKEN", "")
    if not token:
        return json.dumps({"error": "No bearer token"})
    try:
        r = _call_sap(f"{SAP_ODATA}('{sales_order_id}')", token, {"$format": "json"}).get("d",{})
        return json.dumps({"SalesOrder": r.get("SalesOrder"), "SalesOrderType": r.get("SalesOrderType"),
            "SoldToParty": r.get("SoldToParty"), "CreationDate": r.get("CreationDate"),
            "TotalNetAmount": r.get("TotalNetAmount"), "TransactionCurrency": r.get("TransactionCurrency"),
            "OverallSDProcessStatus": r.get("OverallSDProcessStatus")}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
