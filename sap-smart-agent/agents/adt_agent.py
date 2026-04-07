"""
SAP ADT Sub-Agent — ABAP development tools.
Handles: read/write source code, syntax check, ATC, transports, DDIC, search.
Exposed as a FastMCP server so the parent Strands agent can call it as a tool.
"""
import os, json, logging, httpx, xml.etree.ElementTree as ET
import sys; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp.server.fastmcp import FastMCP, Context
from strands import Agent
from strands.models import BedrockModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("adt_agent")

MODEL_ID = "us.anthropic.claude-sonnet-4-6"
SAP_BASE_URL = os.environ.get("SAP_BASE_URL", "https://vhcals4hci.awspoc.club")

mcp = FastMCP("AI Factory — ADT Agent", host="0.0.0.0", port=8101, stateless_http=True)


def _get_token(ctx: Context) -> str:
    try:
        req = ctx.request_context.request
        for h in ["x-amzn-bedrock-agentcore-runtime-custom-saptoken", "authorization"]:
            val = req.headers.get(h, "")
            if val:
                return val.replace("Bearer ", "").replace("bearer ", "") if val.lower().startswith("bearer ") else val
    except Exception: pass
    return os.environ.get("SAP_BEARER_TOKEN", "")


def _adt_get(path: str, token: str, accept: str = "application/json", params: dict = None) -> str:
    url = f"{SAP_BASE_URL}{path}"
    with httpx.Client(verify=False, timeout=30.0) as c:
        r = c.get(url, headers={"Authorization": f"Bearer {token}", "Accept": accept}, params=params or {})
        r.raise_for_status()
        return r.text


def _adt_post(path: str, token: str, body: str, content_type: str, accept: str = "application/xml") -> str:
    url = f"{SAP_BASE_URL}{path}"
    with httpx.Client(verify=False, timeout=30.0) as c:
        # get CSRF token first
        csrf = c.get(url, headers={"Authorization": f"Bearer {token}", "X-CSRF-Token": "Fetch"}).headers.get("x-csrf-token", "")
        r = c.post(url, content=body.encode(),
                   headers={"Authorization": f"Bearer {token}", "Content-Type": content_type,
                             "Accept": accept, "X-CSRF-Token": csrf})
        r.raise_for_status()
        return r.text


# ── Source Code ───────────────────────────────────────────────────────────────

@mcp.tool()
def get_abap_program(ctx: Context, program_name: str) -> str:
    """Read ABAP program/report source code from SAP."""
    token = _get_token(ctx)
    try:
        return _adt_get(f"/sap/bc/adt/programs/programs/{program_name.upper()}/source/main", token,
                        accept="text/plain")
    except Exception as e: return json.dumps({"error": str(e)})


@mcp.tool()
def get_abap_class(ctx: Context, class_name: str, include: str = "main") -> str:
    """Read ABAP class source code. include: main | definitions | implementations | testclasses"""
    token = _get_token(ctx)
    include_map = {"main": "source/main", "definitions": "includes/definitions/source/main",
                   "implementations": "includes/implementations/source/main",
                   "testclasses": "includes/testclasses/source/main"}
    path = f"/sap/bc/adt/oo/classes/{class_name.upper()}/{include_map.get(include, 'source/main')}"
    try:
        return _adt_get(path, token, accept="text/plain")
    except Exception as e: return json.dumps({"error": str(e)})


@mcp.tool()
def get_function_module(ctx: Context, function_group: str, function_name: str) -> str:
    """Read function module source code from SAP."""
    token = _get_token(ctx)
    path = f"/sap/bc/adt/functions/groups/{function_group.upper()}/fmodules/{function_name.upper()}/source/main"
    try:
        return _adt_get(path, token, accept="text/plain")
    except Exception as e: return json.dumps({"error": str(e)})


@mcp.tool()
def get_abap_interface(ctx: Context, interface_name: str) -> str:
    """Read ABAP interface source code from SAP."""
    token = _get_token(ctx)
    try:
        return _adt_get(f"/sap/bc/adt/oo/interfaces/{interface_name.upper()}/source/main",
                        token, accept="text/plain")
    except Exception as e: return json.dumps({"error": str(e)})


@mcp.tool()
def get_abap_include(ctx: Context, include_name: str) -> str:
    """Read ABAP include program source code from SAP."""
    token = _get_token(ctx)
    try:
        return _adt_get(f"/sap/bc/adt/programs/includes/{include_name.upper()}/source/main",
                        token, accept="text/plain")
    except Exception as e: return json.dumps({"error": str(e)})


# ── Search & Discovery ────────────────────────────────────────────────────────

@mcp.tool()
def search_objects(ctx: Context, query: str, object_type: str = "", max_results: int = 50) -> str:
    """Search SAP repository objects. object_type: PROG, CLAS, INTF, FUGR, TABL, TRAN, DEVC etc."""
    token = _get_token(ctx)
    params = {"operation": "quickSearch", "query": f"{query}*", "maxResults": max_results}
    if object_type: params["objectType"] = object_type
    try:
        xml_text = _adt_get("/sap/bc/adt/repository/informationsystem/search", token,
                            accept="application/xml", params=params)
        root = ET.fromstring(xml_text)
        ns = {"adtcore": "http://www.sap.com/adt/core"}
        results = [{"name": o.get("{http://www.sap.com/adt/core}name", ""),
                    "type": o.get("{http://www.sap.com/adt/core}type", ""),
                    "description": o.get("{http://www.sap.com/adt/core}description", "")}
                   for o in root.findall(".//{http://www.sap.com/adt/core}objectReference")]
        return json.dumps({"count": len(results), "results": results}, indent=2)
    except Exception as e: return json.dumps({"error": str(e)})


@mcp.tool()
def get_package(ctx: Context, package_name: str) -> str:
    """Get SAP package (development class) contents."""
    token = _get_token(ctx)
    try:
        return _adt_get(f"/sap/bc/adt/repository/nodestructure?parent_name={package_name.upper()}&parent_tech_name={package_name.upper()}&parent_type=DEVC/K",
                        token, accept="application/xml")
    except Exception as e: return json.dumps({"error": str(e)})


@mcp.tool()
def get_transaction(ctx: Context, tcode: str) -> str:
    """Look up a SAP transaction code and find the associated program/object."""
    token = _get_token(ctx)
    try:
        return _adt_get(f"/sap/bc/adt/repository/informationsystem/search?operation=quickSearch&query={tcode.upper()}&objectType=TRAN&maxResults=5",
                        token, accept="application/xml")
    except Exception as e: return json.dumps({"error": str(e)})


# ── DDIC ──────────────────────────────────────────────────────────────────────

@mcp.tool()
def get_table_definition(ctx: Context, table_name: str) -> str:
    """Get DDIC table or structure definition (fields, types, keys)."""
    token = _get_token(ctx)
    try:
        return _adt_get(f"/sap/bc/adt/ddic/tables/{table_name.upper()}", token, accept="application/xml")
    except Exception as e: return json.dumps({"error": str(e)})


@mcp.tool()
def get_type_info(ctx: Context, type_name: str) -> str:
    """Get DDIC type information — data element, domain, or type details (e.g. MATNR, BUKRS, VBELN)."""
    token = _get_token(ctx)
    try:
        # Try data element first, fall back to domain
        for path in [f"/sap/bc/adt/ddic/dataelements/{type_name.upper()}",
                     f"/sap/bc/adt/ddic/domains/{type_name.upper()}"]:
            try:
                return _adt_get(path, token, accept="application/xml")
            except Exception:
                continue
        return json.dumps({"error": f"Type {type_name} not found as data element or domain"})
    except Exception as e: return json.dumps({"error": str(e)})


@mcp.tool()
def get_table_contents(ctx: Context, sql_query: str, max_rows: int = 100) -> str:
    """Execute a SQL SELECT on SAP database tables via ADT Data Preview.
    Tries freestyle POST first, then sqlConsole GET as fallback."""
    token = _get_token(ctx)
    try:
        with httpx.Client(verify=False, timeout=30.0) as c:
            # Method 1: freestyle POST (requires CSRF)
            csrf_resp = c.get(f"{SAP_BASE_URL}/sap/bc/adt/discovery",
                              headers={"Authorization": f"Bearer {token}", "x-csrf-token": "Fetch",
                                       "Accept": "*/*"})
            csrf = csrf_resp.headers.get("x-csrf-token", "")
            # Try multiple Accept headers — SAP versions differ
            for accept_hdr in ["application/xml",
                                "application/vnd.sap.adt.datapreview.table.v1+xml",
                                "*/*"]:
                r = c.post(f"{SAP_BASE_URL}/sap/bc/adt/datapreview/freestyle",
                           content=sql_query.encode("utf-8"),
                           headers={"Authorization": f"Bearer {token}", "Content-Type": "text/plain",
                                    "Accept": accept_hdr, "x-csrf-token": csrf},
                           params={"rowNumber": str(max_rows)})
                if r.status_code == 200:
                    return r.text
                if r.status_code != 406:
                    break  # not a content negotiation issue
            # Method 2: sqlConsole GET (fallback)
            import urllib.parse
            r2 = c.get(f"{SAP_BASE_URL}/sap/bc/adt/datapreview/sqlConsole",
                       headers={"Authorization": f"Bearer {token}", "Accept": "application/xml"},
                       params={"rowNumber": str(max_rows), "sqlCommand": sql_query})
            if r2.status_code == 200:
                return r2.text
            return json.dumps({"error": f"freestyle={r.status_code}, sqlConsole={r2.status_code}",
                               "freestyle_body": r.text[:300], "sqlConsole_body": r2.text[:300]})
    except Exception as e: return json.dumps({"error": str(e)})


# ── Code Quality ──────────────────────────────────────────────────────────────

@mcp.tool()
def syntax_check(ctx: Context, object_url: str) -> str:
    """Run ABAP syntax check on a program or class. object_url: ADT URI e.g. /sap/bc/adt/programs/programs/Z_MY_PROG"""
    token = _get_token(ctx)
    try:
        return _adt_get(f"{object_url}/syntaxcheck", token, accept="application/xml")
    except Exception as e: return json.dumps({"error": str(e)})


@mcp.tool()
def run_atc_check(ctx: Context, object_uri: str, check_variant: str = "DEFAULT") -> str:
    """Run ATC (ABAP Test Cockpit) code quality check on an object."""
    token = _get_token(ctx)
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<atcworklist:worklist xmlns:atcworklist="http://www.sap.com/adt/atc/worklist"
    xmlns:adtcore="http://www.sap.com/adt/core">
  <atcworklist:objectSets>
    <atcworklist:objectSet kind="inclusive">
      <adtcore:objectReferences>
        <adtcore:objectReference adtcore:uri="{object_uri}"/>
      </adtcore:objectReferences>
    </atcworklist:objectSet>
  </atcworklist:objectSets>
</atcworklist:worklist>"""
    try:
        return _adt_post("/sap/bc/adt/atc/runs?worklistId=1", token, body,
                         "application/xml", accept="application/xml")
    except Exception as e: return json.dumps({"error": str(e)})


# ── Transport Management ──────────────────────────────────────────────────────

@mcp.tool()
def create_transport(ctx: Context, description: str, target_system: str = "",
                     transport_type: str = "K") -> str:
    """Create a new transport request in SAP. transport_type: K=workbench, W=customizing"""
    token = _get_token(ctx)
    body = f"""<?xml version="1.0" encoding="utf-8"?>
<tm:root xmlns:tm="http://www.sap.com/cts/adt/tm">
  <tm:workbenchRequest tm:category="{transport_type}" tm:target="{target_system}"
      tm:description="{description}"/>
</tm:root>"""
    try:
        return _adt_post("/sap/bc/adt/cts/transportrequests", token, body,
                         "application/vnd.sap.adt.tm.transportrequest+xml")
    except Exception as e: return json.dumps({"error": str(e)})


@mcp.tool()
def release_transport(ctx: Context, transport_number: str) -> str:
    """Release a transport request or task in SAP."""
    token = _get_token(ctx)
    try:
        url = f"{SAP_BASE_URL}/sap/bc/adt/cts/transportrequests/{transport_number}/newreleasejobs"
        with httpx.Client(verify=False, timeout=30.0) as c:
            csrf = c.get(f"{SAP_BASE_URL}/sap/bc/adt/cts/transportrequests/{transport_number}",
                         headers={"Authorization": f"Bearer {token}", "X-CSRF-Token": "Fetch"}).headers.get("x-csrf-token", "")
            r = c.post(url, headers={"Authorization": f"Bearer {token}", "X-CSRF-Token": csrf})
            r.raise_for_status()
            return json.dumps({"success": True, "transport": transport_number})
    except Exception as e: return json.dumps({"error": str(e)})


@mcp.tool()
def list_user_transports(ctx: Context, user: str = "", status: str = "D") -> str:
    """List transport requests. status: D=modifiable, R=released"""
    token = _get_token(ctx)
    params = {"user": user or "", "target": "", "status": status}
    try:
        return _adt_get("/sap/bc/adt/cts/transportrequests", token, accept="application/xml", params=params)
    except Exception as e: return json.dumps({"error": str(e)})


# ── OData Service Creation & Activation ──────────────────────────────────────

@mcp.tool()
def create_odata_service(ctx: Context, description: str, cds_name: str = "") -> str:
    """Create a new OData service in SAP by generating a CDS view with @OData.publish.
    Uses Claude (Bedrock) to generate CDS source from a natural language description,
    then creates, writes and activates it via ADT. After creation call activate_odata_service.
    """
    import boto3, uuid as _uuid
    token = _get_token(ctx)
    if not token: return json.dumps({"error": "No bearer token"})
    steps = []
    try:
        bedrock = boto3.client("bedrock-runtime", region_name=boto3.session.Session().region_name)
        if not cds_name:
            words = [w.strip(",.;:!?").lower() for w in description.split()
                     if len(w.strip(",.;:!?")) > 3 and w.strip(",.;:!?") not in
                     {"with","that","this","from","show","list","details","including"}]
            cds_name = "zi_" + "_".join(words[:3])[:20]
        cds_name = cds_name.lower().strip()
        if not cds_name.startswith("z"): cds_name = f"z{cds_name}"

        system = (
            "You are an SAP ABAP CDS expert. Generate a CDS view source.\n"
            "Output ONLY the CDS source code, no markdown, no explanation, no ```.\n\n"
            "CRITICAL REQUIREMENTS for @OData.publish to work:\n"
            "1. MUST use 'define view' syntax (NOT 'define view entity' — that doesn't support @OData.publish)\n"
            "2. MUST include @AbapCatalog.sqlViewName with a short Z-name (max 16 chars)\n"
            "3. MUST include @AbapCatalog.compiler.compareFilter: true\n"
            "4. MUST include @AbapCatalog.preserveKey: true\n"
            "5. MUST include @OData.publish: true\n"
            "6. MUST include @AccessControl.authorizationCheck: #CHECK\n"
            "7. MUST have at least one 'key' field\n"
            "8. Use @EndUserText.label for the view description\n"
            "9. Add @Semantics annotations for amounts/currencies/dates where applicable\n"
            "10. Add @ObjectModel.usageType annotations\n\n"
            "TEMPLATE:\n"
            "@AbapCatalog.sqlViewName: 'ZV_SHORT_NAME'\n"
            "@AbapCatalog.compiler.compareFilter: true\n"
            "@AbapCatalog.preserveKey: true\n"
            "@AccessControl.authorizationCheck: #CHECK\n"
            "@EndUserText.label: 'Description here'\n"
            "@OData.publish: true\n"
            "@ObjectModel.usageType:{ serviceQuality: #X, sizeCategory: #M, dataClass: #TRANSACTIONAL }\n"
            "define view VIEW_NAME as select from TABLE_NAME {\n"
            "  key field1,\n"
            "  field2,\n"
            "  @Semantics.amount.currencyCode: 'CurrencyField'\n"
            "  amount_field,\n"
            "  @Semantics.currencyCode: true\n"
            "  currency_field\n"
            "}\n"
        )
        resp = bedrock.invoke_model(
            modelId="us.anthropic.claude-sonnet-4-6",
            body=json.dumps({"anthropic_version": "bedrock-2023-05-31", "max_tokens": 2048,
                             "system": system,
                             "messages": [{"role": "user", "content":
                                           f"Create CDS view {cds_name.upper()} that exposes: {description}"}]}),
            contentType="application/json", accept="application/json")
        cds_source = json.loads(resp["body"].read())["content"][0]["text"].strip()
        if cds_source.startswith("```"):
            cds_source = "\n".join(cds_source.split("\n")[1:]).rsplit("```", 1)[0].strip()
        steps.append("CDS source generated")

        ddl_path = f"/sap/bc/adt/ddic/ddl/sources"
        with httpx.Client(verify=False, timeout=120) as c:
            base = {"Authorization": f"Bearer {token}", "X-sap-adt-sessiontype": "stateful"}
            csrf = c.get(f"{SAP_BASE_URL}/sap/bc/adt/discovery",
                         headers={**base, "x-csrf-token": "Fetch",
                                  "Accept": "application/atomsvc+xml"}).headers.get("x-csrf-token", "")
            if not csrf: return json.dumps({"error": "Could not fetch CSRF token"})
            steps.append("CSRF fetched")

            # Create if not exists
            if c.get(f"{SAP_BASE_URL}{ddl_path}/{cds_name}/source/main",
                     headers={**base, "Accept": "text/plain"}).status_code != 200:
                cr = c.post(f"{SAP_BASE_URL}{ddl_path}",
                            headers={**base, "x-csrf-token": csrf,
                                     "Content-Type": "application/vnd.sap.adt.ddlsource+xml",
                                     "Accept": "*/*"},
                            content=(f'<?xml version="1.0" encoding="UTF-8"?>'
                                     f'<ddl:ddlSource xmlns:ddl="http://www.sap.com/adt/ddic/ddlsources" '
                                     f'xmlns:adtcore="http://www.sap.com/adt/core" '
                                     f'adtcore:type="DDLS/DF" adtcore:description="{description[:60]}" '
                                     f'adtcore:language="EN" adtcore:name="{cds_name.upper()}" '
                                     f'adtcore:masterLanguage="EN" adtcore:responsible="DEVELOPER">'
                                     f'<adtcore:packageRef adtcore:name="$TMP"/></ddl:ddlSource>'))
                steps.append(f"Created ({cr.status_code})")

            # Lock → Write → Unlock → Activate
            lock_resp = c.post(f"{SAP_BASE_URL}{ddl_path}/{cds_name}",
                               headers={**base, "x-csrf-token": csrf,
                                        "Accept": "application/vnd.sap.as+xml;charset=UTF-8;dataname=com.sap.adt.lock.result"},
                               params={"_action": "LOCK", "accessMode": "MODIFY"})
            lock_handle = ""
            try:
                for el in ET.fromstring(lock_resp.text).iter():
                    if el.tag.split("}")[-1] == "LOCK_HANDLE" and el.text:
                        lock_handle = el.text; break
            except ET.ParseError: pass
            steps.append("Locked")

            wr = c.put(f"{SAP_BASE_URL}{ddl_path}/{cds_name}/source/main",
                       headers={**base, "x-csrf-token": csrf, "Content-Type": "text/plain; charset=utf-8"},
                       params={"lockHandle": lock_handle}, content=cds_source.encode("utf-8"))
            c.post(f"{SAP_BASE_URL}{ddl_path}/{cds_name}",
                   headers={**base, "x-csrf-token": csrf},
                   params={"_action": "UNLOCK", "lockHandle": lock_handle})
            if wr.status_code >= 400:
                return json.dumps({"error": f"Write failed {wr.status_code}", "steps": steps})
            steps.append("Written & unlocked")

            act = c.post(f"{SAP_BASE_URL}/sap/bc/adt/activation",
                         headers={**base, "x-csrf-token": csrf,
                                  "Content-Type": "application/xml", "Accept": "application/xml"},
                         params={"method": "activate", "preauditRequested": "true"},
                         content=(f'<?xml version="1.0" encoding="UTF-8"?>'
                                  f'<adtcore:objectReferences xmlns:adtcore="http://www.sap.com/adt/core">'
                                  f'<adtcore:objectReference adtcore:uri="{ddl_path}/{cds_name}" '
                                  f'adtcore:name="{cds_name.upper()}"/></adtcore:objectReferences>'))
            steps.append(f"Activated ({act.status_code})")

            # Retry activation if first attempt didn't trigger SADL (common issue)
            if act.status_code == 200:
                import time as _time
                _time.sleep(2)
                # Re-activate to ensure SADL runtime artifacts are generated
                act2 = c.post(f"{SAP_BASE_URL}/sap/bc/adt/activation",
                              headers={**base, "x-csrf-token": csrf,
                                       "Content-Type": "application/xml", "Accept": "application/xml"},
                              params={"method": "activate", "preauditRequested": "true"},
                              content=(f'<?xml version="1.0" encoding="UTF-8"?>'
                                       f'<adtcore:objectReferences xmlns:adtcore="http://www.sap.com/adt/core">'
                                       f'<adtcore:objectReference adtcore:uri="{ddl_path}/{cds_name}" '
                                       f'adtcore:name="{cds_name.upper()}"/></adtcore:objectReferences>'))
                steps.append(f"Re-activated for SADL ({act2.status_code})")

        svc_name = f"{cds_name.upper()}_CDS"
        return json.dumps({"status": "success", "cds_view": cds_name.upper(),
                           "odata_service_name": svc_name, "steps": steps,
                           "next_step": f"Activate in /IWFND/MAINT_SERVICE → Add Service → LOCAL → {svc_name}",
                           "note": "If service not visible, run /IWBEP/CACHE_CLEANUP first"}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e), "steps": steps})


@mcp.tool()
def activate_odata_service(ctx: Context, service_name: str,
                            service_version: str = "0001", system_alias: str = "LOCAL") -> str:
    """Activate an OData service in SAP Gateway (/IWFND/MAINT_SERVICE equivalent).
    Registers the backend service in the frontend hub so it's accessible via OData URL.
    """
    token = _get_token(ctx)
    if not token: return json.dumps({"error": "No bearer token"})
    svc = service_name.strip().upper()
    steps = []
    try:
        # Check if already active
        with httpx.Client(verify=False, timeout=15) as c:
            r = c.get(f"{SAP_BASE_URL}/sap/opu/odata/sap/{svc}/$metadata",
                      headers={"Authorization": f"Bearer {token}"})
            if r.status_code == 200:
                return json.dumps({"status": "already_active", "service": svc})
        steps.append(f"Not yet active ({r.status_code})")

        # Try catalog AddService API
        with httpx.Client(verify=False, timeout=60) as c:
            base = {"Authorization": f"Bearer {token}"}
            csrf = c.get(f"{SAP_BASE_URL}/sap/opu/odata/IWFND/CATALOGSERVICE;v=2/",
                         headers={**base, "x-csrf-token": "Fetch"}).headers.get("x-csrf-token", "")
            if csrf:
                r = c.post(f"{SAP_BASE_URL}/sap/opu/odata/IWFND/CATALOGSERVICE;v=2/AddService",
                           headers={**base, "x-csrf-token": csrf,
                                    "Content-Type": "application/json", "Accept": "application/json"},
                           content=json.dumps({"TechnicalServiceName": svc,
                                               "TechnicalServiceVersion": int(service_version),
                                               "SystemAlias": system_alias,
                                               "ExternalServiceName": svc}))
                if r.status_code in (200, 201, 204):
                    return json.dumps({"status": "activated", "service": svc, "steps": steps})
                steps.append(f"AddService HTTP {r.status_code}: {r.text[:200]}")

        return json.dumps({"status": "needs_manual_activation", "service": svc, "steps": steps,
                           "manual": [f"tcode /IWFND/MAINT_SERVICE → Add Service → {system_alias} → {svc}"]})
    except Exception as e:
        return json.dumps({"error": str(e), "steps": steps})


@mcp.tool()
def list_backend_services(ctx: Context, search: str = "", max_results: int = 20) -> str:
    """List OData services in the SAP backend catalog (registered but possibly not activated).
    Use to find services before calling activate_odata_service.
    """
    token = _get_token(ctx)
    if not token: return json.dumps({"error": "No bearer token"})
    try:
        params: dict = {"$format": "json", "$top": max_results}
        if search: params["$filter"] = f"substringof('{search.upper()}',TechnicalServiceName)"
        url = f"{SAP_BASE_URL}/sap/opu/odata/IWBEP/CATALOGSERVICE;v=2/ServiceCollection"
        with httpx.Client(verify=False, timeout=30) as c:
            r = c.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                      params=params)
            r.raise_for_status()
            results = r.json().get("d", {}).get("results", [])
        return json.dumps({"count": len(results),
                           "services": [{"name": s.get("TechnicalServiceName", ""),
                                         "description": s.get("Description", ""),
                                         "type": s.get("ServiceType", "")} for s in results]}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── ADT Discovery ─────────────────────────────────────────────────────────────

@mcp.tool()
def adt_discovery(ctx: Context) -> str:
    """Get all available ADT REST API endpoints on this SAP system.
    Also covers: get_abap_program, get_abap_class, get_function_module,
    get_abap_interface, get_abap_include, search_objects, get_package,
    get_transaction, get_table_definition, get_table_contents, get_type_info,
    syntax_check, run_atc_check, create_transport, release_transport, list_user_transports,
    create_odata_service, activate_odata_service, list_backend_services.
    """
    token = _get_token(ctx)
    try:
        xml_text = _adt_get("/sap/bc/adt/discovery", token, accept="application/xml")
        root = ET.fromstring(xml_text)
        ns = {"app": "http://www.w3.org/2007/app", "atom": "http://www.w3.org/2005/Atom"}
        services = []
        for ws in root.findall("app:workspace", ns):
            ws_title = getattr(ws.find("atom:title", ns), "text", "")
            for col in ws.findall("app:collection", ns):
                col_title = getattr(col.find("atom:title", ns), "text", "")
                services.append({"workspace": ws_title, "title": col_title,
                                  "href": col.get("href", "")})
        return json.dumps({"total": len(services), "services": services}, indent=2)
    except Exception as e: return json.dumps({"error": str(e)})


# ── Strands Agent wrapper (used by parent) ────────────────────────────────────

def create_adt_strands_agent() -> Agent:
    """Return a Strands Agent backed by all ADT tools above."""
    from strands.tools.mcp import MCPClient
    from mcp.client.streamable_http import streamablehttp_client
    client = MCPClient(lambda: streamablehttp_client("http://localhost:8101/mcp"))
    with client:
        return Agent(
            model=BedrockModel(model_id=MODEL_ID, max_tokens=16000),
            tools=client.tools,
            system_prompt=(
                "You are the ADT Agent within the AI Factory MCP Server. "
                "Your role is ABAP development, understanding SAP ABAP objects, and CDS view creation.\n\n"
                "Use this agent when the task involves:\n"
                "- Reading or writing ABAP source code (programs, classes, function modules, includes, interfaces)\n"
                "- Understanding what an ABAP object does or how it is structured\n"
                "- Exploring CDS views and their data models\n"
                "- Running syntax checks or ATC code quality checks\n"
                "- Managing transport requests\n"
                "- Querying DDIC table definitions and contents via SQL\n"
                "- Creating a new OData service via CDS view (always ask user first — they may just want info)\n"
                "- Activating an OData service in SAP Gateway\n\n"
                "IMPORTANT: If an ADT API is not available as a named tool, use your SAP knowledge "
                "to construct the correct ADT REST path and call it via call_adt_api directly.\n\n"
                "If user wants data and no OData exists: generate the SQL SELECT statement and "
                "run it via get_table_contents — do not ask the user to create OData just for a data query.\n\n"
                "If user wants to create OData: follow the flow — "
                "create_odata_service → activate_odata_service. But confirm intent first."
            )
        )


if __name__ == "__main__":
    logger.info("=== SAP ADT Sub-Agent starting on port 8101 ===")
    mcp.run(transport="streamable-http")
