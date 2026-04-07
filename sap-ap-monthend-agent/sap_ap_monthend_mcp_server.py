"""
SAP AP Month-End Close MCP Server
Uses API_OPLACCTGDOCITEMCUBE_SRV (open items) and API_SUPPLIERINVOICE_PROCESS_SRV.
"""
import os, json, logging, httpx
from datetime import date, timedelta
from collections import defaultdict
from mcp.server.fastmcp import FastMCP, Context

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sap_ap_monthend")

SAP_BASE_URL = os.environ.get("SAP_BASE_URL", "https://vhcals4hci.awspoc.club")
mcp = FastMCP("SAP AP Month-End Close MCP Server", host="0.0.0.0", stateless_http=True)

# Working OData service on this S/4HANA system
SUPPLIER_INV_LIST_PATH = "/sap/opu/odata/sap/MM_SUPPLIER_INVOICE_LIST_SRV/SupInvoice"
AP_SELECT = "SupplierInvoice,FiscalYear,CompanyCode,CompanyCodeName,InvoicingParty,InvoicingPartyName,InvoiceGrossAmount,DocumentCurrency,DueCalculationBaseDate,CashDiscount1Days,PostingDate,DocumentDate,PaymentBlockingReason,Status,StatusDescription"


def _get_token(ctx: Context) -> str:
    for h in ["x-amzn-bedrock-agentcore-runtime-custom-saptoken",
              "x-amzn-oidc-data", "x-amzn-oidc-access-token", "authorization"]:
        val = (ctx.request_context.request.headers.get(h) or "") if ctx else ""
        if val:
            return val.removeprefix("Bearer ").removeprefix("bearer ")
    return os.environ.get("SAP_BEARER_TOKEN", "")


def _sap_get(path: str, token: str, params: dict = None) -> dict:
    url = f"{SAP_BASE_URL}{path}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    p = {"$format": "json", **(params or {})}
    resp = httpx.get(url, headers=headers, params=p, timeout=30, verify=False)
    resp.raise_for_status()
    return resp.json()


def _parse_date(sap_val):
    if not sap_val: return None
    s = str(sap_val)
    if "/Date(" in s:
        try:
            ms = int(s.split("(")[1].split(")")[0])
            return date.fromtimestamp(ms / 1000)
        except: return None
    try: return date.fromisoformat(s[:10])
    except: return None


def _due_date(item: dict):
    base = _parse_date(item.get("DueCalculationBaseDate")) or _parse_date(item.get("PostingDate"))
    if not base: return None
    return base + timedelta(days=int(item.get("CashDiscount1Days") or 0))


def _days_overdue(due) -> int:
    if not due: return 0
    return max((date.today() - due).days, 0)


def _aging_bucket(days: int) -> str:
    if days <= 30:  return "0-30 days"
    if days <= 60:  return "31-60 days"
    return "60+ days"


def _fetch_invoices(token: str, company_code: str = "", vendor_id: str = "", top: int = 500) -> list:
    params = {"$select": AP_SELECT, "$top": str(top)}
    if company_code:
        params["$filter"] = f"CompanyCode eq '{company_code}'"
    if vendor_id:
        f = f"InvoicingParty eq '{vendor_id}'"
        params["$filter"] = (params.get("$filter", "") + f" and {f}").lstrip(" and ")
    data = _sap_get(SUPPLIER_INV_LIST_PATH, token, params)
    return data.get("d", {}).get("results", [])


@mcp.tool()
def get_overdue_vendor_invoices(ctx: Context, company_code: str = "", vendor_id: str = "", min_days_overdue: int = 1) -> str:
    """Get all open vendor invoices past their due date.
    Optional: company_code, vendor_id, min_days_overdue (default 1).
    Returns invoice number, vendor, due date, days overdue, amount, currency, company code."""
    token = _get_token(ctx)
    today = date.today()
    try:
        items = _fetch_invoices(token, company_code, vendor_id)
        result = []
        for i in items:
            due = _due_date(i)
            if not due or due >= today: continue
            days = _days_overdue(due)
            if days < min_days_overdue: continue
            result.append({
                "DocumentNumber": i.get("SupplierInvoice"),
                "FiscalYear": i.get("FiscalYear"),
                "Vendor": i.get("InvoicingParty"),
                "VendorName": i.get("InvoicingPartyName"),
                "CompanyCode": i.get("CompanyCode"),
                "CompanyCodeName": i.get("CompanyCodeName"),
                "PostingDate": (d := _parse_date(i.get("PostingDate"))) and d.isoformat() or "",
                "NetDueDate": due.isoformat(),
                "DaysOverdue": days,
                "AgingBucket": _aging_bucket(days),
                "Amount": float(i.get("InvoiceGrossAmount") or 0),
                "Currency": i.get("DocumentCurrency"),
                "PaymentBlock": i.get("PaymentBlockingReason") or "",
                "Status": i.get("StatusDescription") or i.get("Status") or "",
            })
        result.sort(key=lambda x: x["DaysOverdue"], reverse=True)
        return json.dumps({"status": "success", "as_of": today.isoformat(), "count": len(result), "invoices": result})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool()
def get_ap_aging_summary(ctx: Context, company_code: str = "") -> str:
    """AP aging summary grouped by CompanyCode with buckets: 0-30, 31-60, 60+ days.
    Returns invoice count and total exposure per bucket per company code."""
    token = _get_token(ctx)
    today = date.today()
    try:
        items = _fetch_invoices(token, company_code)
        summary = defaultdict(lambda: {
            "0-30 days": {"count": 0, "total": 0.0},
            "31-60 days": {"count": 0, "total": 0.0},
            "60+ days": {"count": 0, "total": 0.0},
            "total_invoices": 0, "total_exposure": 0.0, "CompanyCodeName": "", "currency": ""
        })
        for i in items:
            due = _due_date(i)
            if not due or due >= today: continue
            days = _days_overdue(due)
            amt = float(i.get("InvoiceGrossAmount") or 0)
            cc = i.get("CompanyCode", "UNKNOWN")
            bucket = _aging_bucket(days)
            summary[cc]["CompanyCode"] = cc
            summary[cc]["CompanyCodeName"] = i.get("CompanyCodeName", "")
            summary[cc]["currency"] = i.get("DocumentCurrency", "")
            summary[cc][bucket]["count"] += 1
            summary[cc][bucket]["total"] = round(summary[cc][bucket]["total"] + amt, 2)
            summary[cc]["total_invoices"] += 1
            summary[cc]["total_exposure"] = round(summary[cc]["total_exposure"] + amt, 2)
        result = [{"CompanyCode": cc, **v} for cc, v in summary.items()]
        result.sort(key=lambda x: x["total_exposure"], reverse=True)
        return json.dumps({"status": "success", "as_of": today.isoformat(), "company_codes": len(result), "summary": result})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool()
def get_payment_blocked_invoices(ctx: Context, company_code: str = "") -> str:
    """Get supplier invoices with a payment block (PaymentBlockingReason is set).
    Optional: company_code filter."""
    token = _get_token(ctx)
    try:
        items = _fetch_invoices(token, company_code)
        result = []
        for i in items:
            if not i.get("PaymentBlockingReason"): continue
            due = _due_date(i)
            result.append({
                "DocumentNumber": i.get("SupplierInvoice"),
                "FiscalYear": i.get("FiscalYear"),
                "Vendor": i.get("InvoicingParty"),
                "VendorName": i.get("InvoicingPartyName"),
                "CompanyCode": i.get("CompanyCode"),
                "PostingDate": (d := _parse_date(i.get("PostingDate"))) and d.isoformat() or "",
                "NetDueDate": due.isoformat() if due else "",
                "Amount": float(i.get("InvoiceGrossAmount") or 0),
                "Currency": i.get("DocumentCurrency"),
                "PaymentBlockingReason": i.get("PaymentBlockingReason"),
            })
        return json.dumps({"status": "success", "count": len(result), "invoices": result})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool()
def get_vendor_open_exposure(ctx: Context, company_code: str, vendor_id: str = "") -> str:
    """Total open AP exposure per vendor for a given company code.
    Required: company_code. Optional: vendor_id for single-vendor drill-down."""
    token = _get_token(ctx)
    today = date.today()
    try:
        items = _fetch_invoices(token, company_code, vendor_id)
        vendors: dict = {}
        for i in items:
            due = _due_date(i)
            if not due or due >= today: continue
            v = i.get("InvoicingParty", "")
            amt = float(i.get("InvoiceGrossAmount") or 0)
            if v not in vendors:
                vendors[v] = {"Vendor": v, "VendorName": i.get("InvoicingPartyName", ""),
                              "CompanyCode": company_code, "InvoiceCount": 0,
                              "TotalExposure": 0.0, "Currency": i.get("DocumentCurrency", "")}
            vendors[v]["InvoiceCount"] += 1
            vendors[v]["TotalExposure"] = round(vendors[v]["TotalExposure"] + amt, 2)
        result = sorted(vendors.values(), key=lambda x: x["TotalExposure"], reverse=True)
        return json.dumps({"status": "success", "as_of": today.isoformat(), "company_code": company_code, "vendors": result})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool()
def get_grir_mismatch_invoices(ctx: Context, company_code: str = "", fiscal_year: str = "") -> str:
    """Find supplier invoices with a payment block — potential GR/IR mismatch candidates.
    Optional: company_code, fiscal_year."""
    token = _get_token(ctx)
    try:
        items = _fetch_invoices(token, company_code)
        result = []
        for i in items:
            if not i.get("PaymentBlockingReason"): continue
            if fiscal_year and i.get("FiscalYear") != fiscal_year: continue
            due = _due_date(i)
            result.append({
                "SupplierInvoice": i.get("SupplierInvoice"),
                "FiscalYear": i.get("FiscalYear"),
                "Vendor": i.get("InvoicingParty"),
                "VendorName": i.get("InvoicingPartyName"),
                "CompanyCode": i.get("CompanyCode"),
                "PostingDate": (d := _parse_date(i.get("PostingDate"))) and d.isoformat() or "",
                "NetDueDate": due.isoformat() if due else "",
                "Amount": float(i.get("InvoiceGrossAmount") or 0),
                "Currency": i.get("DocumentCurrency"),
                "PaymentBlock": i.get("PaymentBlockingReason"),
                "MismatchReason": "Invoice with payment block — verify GR/IR matching",
            })
        total = round(sum(r["Amount"] for r in result), 2)
        return json.dumps({"status": "success", "count": len(result), "total_exposure": total, "mismatches": result})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


if __name__ == "__main__":
    logger.info("=== SAP AP Month-End Close MCP Server Starting ===")
    mcp.run(transport="streamable-http")
