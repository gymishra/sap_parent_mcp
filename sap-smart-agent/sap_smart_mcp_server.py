"""
SAP Smart MCP Server — AI-powered SAP OData + ADT agent.
Token is extracted from the incoming Authorization header or bearer_token parameter.
"""
import os, json, logging, httpx, uuid, boto3, xml.etree.ElementTree as ET
from threading import Thread
from mcp.server.fastmcp import FastMCP, Context

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sap_smart")

SAP_BASE_URL = os.environ.get("SAP_BASE_URL", "https://vhcals4hci.awspoc.club")
CATALOG_URL = "/sap/opu/odata/IWFND/CATALOGSERVICE;v=2/ServiceCollection"

mcp = FastMCP("SAP Smart Agent", host="0.0.0.0", stateless_http=True)

_catalog_cache = {}
_metadata_cache = {}

# ── Enriched in-memory metadata (loaded on startup) ──────────────────────────
# { "SVC_NAME": [ { entity_type, entity_set, keys, properties: [...], nav_properties: [...] } ] }
_entity_cache: dict = {}
# Flat index: [ { service, entity_name, entity_set, property_name, property_type } ]
_field_index: list = []
_cache_loaded = False
_cache_error = ""


def _get_token(ctx: Context) -> str:
    """Get Bearer token from custom AgentCore header, tool parameter fallback, or env var.
    The bridge sends the token via X-Amzn-Bedrock-AgentCore-Runtime-Custom-SapToken header
    which AgentCore always propagates to MCP servers (no allowlist needed).
    """
    # 1. Custom AgentCore header (always propagated, no allowlist needed)
    try:
        req = ctx.request_context.request
        for header_name in ["x-amzn-bedrock-agentcore-runtime-custom-saptoken",
                            "X-Amzn-Bedrock-AgentCore-Runtime-Custom-SapToken",
                            "authorization", "Authorization"]:
            val = req.headers.get(header_name, "")
            if val:
                token = val.replace("Bearer ", "").replace("bearer ", "") if val.lower().startswith("bearer ") else val
                if token:
                    logger.info(f"Token from header: {header_name}")
                    return token
    except Exception as e:
        logger.warning(f"Could not extract token from request context: {e}")
    # 2. Explicit parameter (legacy bridge fallback)
    if bearer_token:
        logger.info("Token from bearer_token parameter")
        return bearer_token
    # 3. Env var fallback
    env_token = os.environ.get("SAP_BEARER_TOKEN", "")
    if env_token:
        logger.info("Token from SAP_BEARER_TOKEN env var")
    else:
        logger.error("No token found in header or env var!")
    return env_token


def _sap_get(path, token, params=None):
    url = f"{SAP_BASE_URL}{path}"
    with httpx.Client(verify=False, timeout=30.0) as c:
        r = c.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                  params=params or {})
        r.raise_for_status()
        return r.json()


def _sap_get_xml(path, token):
    url = f"{SAP_BASE_URL}{path}"
    with httpx.Client(verify=False, timeout=30.0) as c:
        r = c.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/xml"})
        r.raise_for_status()
        return r.text


# ── Metadata parser & startup loader ─────────────────────────────────────────
def _parse_metadata_xml(xml_text):
    """Parse OData $metadata XML into list of entity dicts with properties, keys, nav properties, and entity set names."""
    root = ET.fromstring(xml_text)
    entities = []
    for ns_uri in ["http://schemas.microsoft.com/ado/2008/09/edm",
                   "http://schemas.microsoft.com/ado/2009/11/edm"]:
        assoc_map = {}
        for assoc in root.iter(f"{{{ns_uri}}}Association"):
            aname = assoc.get("Name", "")
            ends = assoc.findall(f"{{{ns_uri}}}End")
            if len(ends) == 2:
                assoc_map[aname] = {
                    ends[0].get("Role", ""): {"type": ends[0].get("Type", ""), "mult": ends[0].get("Multiplicity", "")},
                    ends[1].get("Role", ""): {"type": ends[1].get("Type", ""), "mult": ends[1].get("Multiplicity", "")},
                }
        entity_set_map = {}
        for ec in root.iter(f"{{{ns_uri}}}EntityContainer"):
            for es in ec.findall(f"{{{ns_uri}}}EntitySet"):
                es_type = es.get("EntityType", "").split(".")[-1]
                entity_set_map[es_type] = es.get("Name", "")
        for et in root.iter(f"{{{ns_uri}}}EntityType"):
            ename = et.get("Name", "")
            props = [{"name": p.get("Name", ""), "type": p.get("Type", ""), "nullable": p.get("Nullable", "true")}
                     for p in et.findall(f"{{{ns_uri}}}Property")]
            nav_props = []
            for nav in et.findall(f"{{{ns_uri}}}NavigationProperty"):
                to_role = nav.get("ToRole", "")
                rel = nav.get("Relationship", "").split(".")[-1]
                target, mult = "", ""
                if rel in assoc_map and to_role in assoc_map[rel]:
                    target = assoc_map[rel][to_role]["type"].split(".")[-1]
                    mult = assoc_map[rel][to_role]["mult"]
                nav_props.append({"name": nav.get("Name", ""), "target_entity": target, "multiplicity": mult})
            key_el = et.find(f"{{{ns_uri}}}Key")
            keys = [kr.get("Name", "") for kr in key_el.findall(f"{{{ns_uri}}}PropertyRef")] if key_el is not None else []
            entities.append({"entity_type": ename, "entity_set": entity_set_map.get(ename, ""),
                             "keys": keys, "properties": props, "nav_properties": nav_props})
    return entities


def _load_all_metadata():
    """Background: fetch full catalog + metadata for every service on container startup."""
    global _catalog_cache, _entity_cache, _field_index, _cache_loaded, _cache_error

    logger.info("=== Starting full SAP metadata load ===")
    token = os.environ.get("SAP_BEARER_TOKEN", "")
    if not token:
        _cache_error = "No SAP_BEARER_TOKEN env var for startup load — cache will populate on first request"
        logger.warning(_cache_error)
        return

    # 1. Catalog
    try:
        data = _sap_get_json_raw(CATALOG_URL, token, {"$format": "json", "$top": "5000"})
        for svc in data.get("d", {}).get("results", []):
            title = svc.get("Title", "")
            _catalog_cache[title] = {
                "Title": title,
                "TechnicalServiceName": svc.get("TechnicalServiceName", ""),
                "ServiceUrl": svc.get("ServiceUrl", ""),
                "Description": svc.get("Description", ""),
                "TechnicalServiceVersion": svc.get("TechnicalServiceVersion", ""),
            }
        logger.info(f"Catalog loaded: {len(_catalog_cache)} services")
    except Exception as e:
        _cache_error = f"Catalog fetch failed: {e}"
        logger.error(_cache_error)
        return

    # 2. Metadata for each service
    total = len(_catalog_cache)
    success = skipped = 0
    for i, title in enumerate(_catalog_cache):
        try:
            xml_text = _sap_get_xml_raw(f"/sap/opu/odata/sap/{title}/$metadata", token)
            entities = _parse_metadata_xml(xml_text)
            _entity_cache[title] = entities
            for ent in entities:
                for prop in ent["properties"]:
                    _field_index.append({"service": title, "entity_name": ent["entity_type"],
                                         "entity_set": ent.get("entity_set", ""),
                                         "property_name": prop["name"], "property_type": prop["type"]})
            success += 1
        except Exception:
            skipped += 1
        if (i + 1) % 50 == 0:
            logger.info(f"  Metadata progress: {i+1}/{total} (ok={success}, skip={skipped})")

    _cache_loaded = True
    logger.info(f"Metadata load complete: {success}/{total} services, {len(_field_index)} fields indexed")


def _sap_get_json_raw(path, token, params=None):
    """Direct SAP GET (no Context needed) — used by startup loader."""
    url = f"{SAP_BASE_URL}{path}"
    with httpx.Client(verify=False, timeout=60) as c:
        r = c.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                  params=params or {})
        r.raise_for_status()
        return r.json()


def _sap_get_xml_raw(path, token):
    """Direct SAP GET XML (no Context needed) — used by startup loader."""
    url = f"{SAP_BASE_URL}{path}"
    with httpx.Client(verify=False, timeout=30) as c:
        r = c.get(url, headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return r.text


# ── Tool 0: ADT API ───────────────────────────────────────────────────────────

@mcp.tool()
def call_adt_api(ctx: Context, adt_path: str, method: str = "GET", query_params: str = "",
                 body: str = "", content_type: str = "", accept: str = "") -> str:
    """Call SAP ADT REST API. Single tool for all ABAP development operations.
    Supports GET, POST, PUT, DELETE with automatic CSRF token handling.
    Args:
        adt_path: ADT REST path (e.g. '/sap/bc/adt/programs/programs/SAPMV45A/source/main')
        method: HTTP method - GET, POST, PUT, DELETE (default GET)
        query_params: URL query string (e.g. 'operation=quickSearch&query=Z*&maxResults=10')
        body: Request body for POST/PUT (source code, XML payload, SQL query etc.)
        content_type: Content-Type header override
        accept: Accept header override (e.g. 'application/vnd.sap.as+xml;charset=UTF-8;dataname=com.sap.adt.lock.result')
    """
    token = _get_token(ctx)
    if not token:
        return json.dumps({"error": "No bearer token in request"})
    try:
        params = {}
        if query_params:
            for pair in query_params.split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    params[k] = v

        url = f"{SAP_BASE_URL}{adt_path}"
        http_method = method.upper()

        with httpx.Client(verify=False, timeout=60.0) as c:
            base_headers = {"Authorization": f"Bearer {token}"}

            if http_method == "GET":
                default_accept = "text/plain" if "/source/main" in adt_path else "application/xml"
                r = c.get(url, headers={**base_headers, "Accept": accept or default_accept}, params=params)
                if r.status_code == 405:
                    csrf_resp = c.get(f"{SAP_BASE_URL}/sap/bc/adt/discovery",
                                     headers={**base_headers, "x-csrf-token": "Fetch", "Accept": "*/*"})
                    csrf = csrf_resp.headers.get("x-csrf-token", "")
                    r = c.post(url, headers={**base_headers, "Accept": accept or "application/xml",
                               "x-csrf-token": csrf, "Content-Type": "application/xml"}, params=params, content=body or "")
            else:
                csrf_resp = c.get(f"{SAP_BASE_URL}/sap/bc/adt/discovery",
                                 headers={**base_headers, "x-csrf-token": "Fetch", "Accept": "*/*"})
                csrf = csrf_resp.headers.get("x-csrf-token", "")
                ct = content_type or ("text/plain" if "/source/main" in adt_path and http_method == "PUT" else "application/xml")
                req_headers = {**base_headers, "x-csrf-token": csrf, "Content-Type": ct, "Accept": accept or "application/xml"}
                if http_method == "POST":
                    r = c.post(url, headers=req_headers, params=params, content=body or "")
                elif http_method == "PUT":
                    r = c.put(url, headers=req_headers, params=params, content=body or "")
                elif http_method == "DELETE":
                    r = c.delete(url, headers=req_headers, params=params)
                else:
                    return json.dumps({"error": f"Unsupported method: {method}"})

            r.raise_for_status()
            resp_text = r.text

        # Parse response
        import xml.etree.ElementTree as ET
        try:
            root = ET.fromstring(resp_text)

            # Special handling for ADT data preview (freestyle SQL results)
            dp_ns = "http://www.sap.com/adt/dataPreview"
            columns = root.findall(f".//{{{dp_ns}}}columns")
            if not columns:
                columns = root.findall(f".//{{{dp_ns}}}column")
            if columns:
                # Extract column names from metadata and data values
                col_names = []
                col_data = []
                for col in columns:
                    meta = col.find(f"{{{dp_ns}}}metadata")
                    if meta is not None:
                        # Try namespaced attribute first, then plain
                        name = meta.get(f"{{{dp_ns}}}name", "") or meta.get("name", "")
                        col_names.append(name)
                    dataset = col.find(f"{{{dp_ns}}}dataSet")
                    if dataset is not None:
                        # Data elements may be namespaced or plain
                        values = [d.text or "" for d in dataset.findall(f"{{{dp_ns}}}data")]
                        if not values:
                            values = [d.text or "" for d in dataset]
                        col_data.append(values)
                    else:
                        col_data.append([])
                # Transpose columns to rows
                if col_data and any(col_data):
                    num_rows = max(len(cd) for cd in col_data) if col_data else 0
                    rows = []
                    for i in range(num_rows):
                        row = {}
                        for j, name in enumerate(col_names):
                            row[name] = col_data[j][i] if j < len(col_data) and i < len(col_data[j]) else ""
                        rows.append(row)
                    return json.dumps({"count": len(rows), "columns": col_names, "results": rows}, indent=2)
                return json.dumps({"columns": col_names, "count": 0, "results": []}, indent=2)

            # Special handling for ADT lock responses (deeply nested LOCK_HANDLE)
            lock_handle_el = root.find(".//{http://www.sap.com/abapxml}values")
            if lock_handle_el is not None:
                lock_data = {}
                for data_el in lock_handle_el:
                    for field in data_el:
                        tag = field.tag.split("}")[-1] if "}" in field.tag else field.tag
                        lock_data[tag] = field.text or ""
                if lock_data:
                    return json.dumps({"lock_data": lock_data}, indent=2)

            # Generic XML parsing for other ADT responses
            results = []
            for child in root:
                item = dict(child.attrib)
                for sub in child:
                    tag = sub.tag.split("}")[-1] if "}" in sub.tag else sub.tag
                    item[tag] = sub.text or dict(sub.attrib)
                results.append(item)
            if results:
                return json.dumps({"count": len(results), "results": results}, indent=2)
            if root.attrib:
                return json.dumps({"result": dict(root.attrib)}, indent=2)
        except ET.ParseError:
            pass
        if len(resp_text) > 5000:
            return json.dumps({"source": resp_text[:5000], "truncated": True, "total_length": len(resp_text)})
        return json.dumps({"response": resp_text}) if resp_text else json.dumps({"status": "success", "http_status": r.status_code})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Tool 0b: Upload ABAP Source (lock → write → unlock → activate in one session) ──

@mcp.tool()
def upload_abap_source(ctx: Context, program_name: str, source_code: str,
                       description: str = "", activate: bool = True) -> str:
    """Upload ABAP source code to SAP: creates program if needed, then lock → write → unlock → activate
    all in a single HTTP session. Solves the lock-handle problem across separate API calls.
    Args:
        program_name: ABAP program name (e.g. 'Z_INVOICE_3WAY_MATCH'). Will be lowercased.
        source_code: Full ABAP source code to upload.
        description: Program description (used only when creating new program).
        activate: Whether to activate after upload (default True).
    """
    token = _get_token(ctx)
    if not token:
        return json.dumps({"error": "No bearer token in request"})

    prog = program_name.lower().strip()
    prog_path = f"/sap/bc/adt/programs/programs/{prog}"
    steps_log = []

    try:
        with httpx.Client(verify=False, timeout=120.0) as c:
            base = {"Authorization": f"Bearer {token}", "X-sap-adt-sessiontype": "stateful"}

            # Step 0: Fetch CSRF token
            csrf_resp = c.get(f"{SAP_BASE_URL}/sap/bc/adt/discovery",
                              headers={**base, "x-csrf-token": "Fetch", "Accept": "*/*"})
            csrf = csrf_resp.headers.get("x-csrf-token", "")
            if not csrf:
                return json.dumps({"error": "Could not fetch CSRF token"})
            steps_log.append("CSRF token fetched")

            # Step 1: Check if program exists
            check = c.get(f"{SAP_BASE_URL}{prog_path}",
                          headers={**base, "Accept": "application/xml"})
            if check.status_code == 404:
                # Create the program
                create_xml = (
                    f'<?xml version="1.0" encoding="UTF-8"?>'
                    f'<program:abapProgram xmlns:program="http://www.sap.com/adt/programs/programs" '
                    f'xmlns:adtcore="http://www.sap.com/adt/core" '
                    f'adtcore:type="PROG/P" adtcore:description="{description or prog}" '
                    f'adtcore:language="EN" adtcore:name="{prog.upper()}" '
                    f'adtcore:masterLanguage="EN" adtcore:responsible="DEVELOPER">'
                    f'<adtcore:packageRef adtcore:name="$TMP"/>'
                    f'</program:abapProgram>'
                )
                cr = c.post(f"{SAP_BASE_URL}{prog_path}",
                            headers={**base, "x-csrf-token": csrf,
                                     "Content-Type": "application/vnd.sap.adt.programs.programs.v2+xml"},
                            content=create_xml)
                if cr.status_code >= 400 and cr.status_code != 409:
                    return json.dumps({"error": f"Create failed: {cr.status_code}", "details": cr.text[:500]})
                steps_log.append(f"Program created ({cr.status_code})")
            else:
                steps_log.append("Program already exists")

            # Step 2: Lock
            lock_resp = c.post(f"{SAP_BASE_URL}{prog_path}",
                               headers={**base, "x-csrf-token": csrf,
                                        "Accept": "application/vnd.sap.as+xml;charset=UTF-8;dataname=com.sap.adt.lock.result"},
                               params={"_action": "LOCK", "accessMode": "MODIFY"})
            if lock_resp.status_code >= 400:
                return json.dumps({"error": f"Lock failed: {lock_resp.status_code}", "details": lock_resp.text[:500]})

            # Capture SAP context ID for session continuity (critical for OAuth/Bearer auth)
            sap_context_id = lock_resp.headers.get("sap-contextid", "")
            context_headers = {}
            if sap_context_id:
                context_headers["sap-contextid"] = sap_context_id

            # Extract lock handle from XML
            lock_handle = ""
            try:
                lock_root = ET.fromstring(lock_resp.text)
                for el in lock_root.iter():
                    tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
                    if tag == "LOCK_HANDLE" and el.text:
                        lock_handle = el.text
                        break
            except ET.ParseError:
                pass
            steps_log.append(f"Locked (handle={'found' if lock_handle else 'empty'}, ctx={'yes' if sap_context_id else 'no'})")

            # Step 3: Write source code
            write_params = {"lockHandle": lock_handle} if lock_handle else {}
            write_resp = c.put(f"{SAP_BASE_URL}{prog_path}/source/main",
                               headers={**base, **context_headers, "x-csrf-token": csrf,
                                        "Content-Type": "text/plain; charset=utf-8"},
                               params=write_params,
                               content=source_code.encode("utf-8"))
            if write_resp.status_code >= 400:
                # Try unlock before returning error
                c.post(f"{SAP_BASE_URL}{prog_path}",
                       headers={**base, "x-csrf-token": csrf},
                       params={"_action": "UNLOCK", "lockHandle": lock_handle})
                return json.dumps({"error": f"Write failed: {write_resp.status_code}",
                                   "details": write_resp.text[:500], "steps": steps_log})
            steps_log.append(f"Source written ({write_resp.status_code})")

            # Step 4: Unlock
            unlock_resp = c.post(f"{SAP_BASE_URL}{prog_path}",
                                 headers={**base, "x-csrf-token": csrf},
                                 params={"_action": "UNLOCK", "lockHandle": lock_handle})
            steps_log.append(f"Unlocked ({unlock_resp.status_code})")

            # Step 5: Activate
            activate_result = ""
            if activate:
                activate_body = (
                    f'<?xml version="1.0" encoding="UTF-8"?>'
                    f'<adtcore:objectReferences xmlns:adtcore="http://www.sap.com/adt/core">'
                    f'<adtcore:objectReference adtcore:uri="{prog_path}" adtcore:name="{prog.upper()}"/>'
                    f'</adtcore:objectReferences>'
                )
                act_resp = c.post(f"{SAP_BASE_URL}/sap/bc/adt/activation",
                                  headers={**base, "x-csrf-token": csrf,
                                           "Content-Type": "application/xml",
                                           "Accept": "application/xml"},
                                  params={"method": "activate", "preauditRequested": "true"},
                                  content=activate_body)
                activate_result = f"status={act_resp.status_code}"
                if act_resp.status_code < 400:
                    # Check for activation errors in response
                    try:
                        act_root = ET.fromstring(act_resp.text)
                        msgs = []
                        for el in act_root.iter():
                            tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
                            if tag == "msg" or tag == "message":
                                severity = el.get("severity", el.get("type", ""))
                                txt = el.get("shortText", el.text or "")
                                if txt:
                                    msgs.append(f"{severity}: {txt}")
                        if msgs:
                            activate_result += f", messages={msgs}"
                    except ET.ParseError:
                        pass
                steps_log.append(f"Activated ({activate_result})")

        return json.dumps({
            "status": "success",
            "program": prog.upper(),
            "steps": steps_log,
            "message": f"Program {prog.upper()} uploaded and {'activated' if activate else 'saved (not activated)'} in SAP."
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e), "steps": steps_log})


# ── Tool 1: Discover SAP Services ─────────────────────────────────────────────

@mcp.tool()
def discover_sap_services(ctx: Context) -> str:
    """Discover all available OData services from SAP catalog.
    Caches results in memory. Returns count + first 50 services. Use search_sap_services for targeted lookups."""
    global _catalog_cache
    if _catalog_cache:
        return json.dumps({"service_count": len(_catalog_cache), "message": "Catalog already cached. Use search_sap_services to find specific services.", "sample": list(_catalog_cache.keys())[:10]}, indent=2)
    token = _get_token(ctx)
    if not token:
        return json.dumps({"error": "No bearer token in request"})
    try:
        data = _sap_get(CATALOG_URL, token, {"$format": "json"})
        for svc in data.get("d", {}).get("results", []):
            entry = {
                "Title": svc.get("Title", ""),
                "TechnicalServiceName": svc.get("TechnicalServiceName", ""),
                "ServiceUrl": svc.get("ServiceUrl", ""),
                "Description": svc.get("Description", ""),
                "TechnicalServiceVersion": svc.get("TechnicalServiceVersion", ""),
            }
            _catalog_cache[entry["TechnicalServiceName"]] = entry
        logger.info(f"Discovered and cached {len(_catalog_cache)} SAP services")
        return json.dumps({"service_count": len(_catalog_cache), "message": "Catalog cached. Use search_sap_services to find specific services."}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Tool 2: Get Service Metadata ──────────────────────────────────────────────

@mcp.tool()
def get_service_metadata(ctx: Context, service_path: str) -> str:
    """Get entity types, properties, and navigation properties (relationships) for a SAP OData service.
    Returns from in-memory cache if already fetched.
    Args:
        service_path: OData service path (e.g. '/sap/opu/odata/sap/API_SALES_ORDER_SRV')
    """
    global _metadata_cache
    if service_path in _metadata_cache:
        logger.info(f"Metadata cache hit for {service_path}")
        return json.dumps({"service": service_path, "entity_types": _metadata_cache[service_path], "cached": True}, indent=2)
    token = _get_token(ctx)
    if not token:
        return json.dumps({"error": "No bearer token in request"})
    try:
        metadata_xml = _sap_get_xml(f"{service_path}/$metadata", token)
        entities = []
        import xml.etree.ElementTree as ET
        root = ET.fromstring(metadata_xml)

        # Build association lookup: association name -> target entity type
        assoc_map = {}
        for ns_uri in ["http://schemas.microsoft.com/ado/2008/09/edm",
                       "http://schemas.microsoft.com/ado/2009/11/edm"]:
            for assoc in root.iter(f"{{{ns_uri}}}Association"):
                aname = assoc.get("Name", "")
                ends = assoc.findall(f"{{{ns_uri}}}End")
                if len(ends) == 2:
                    assoc_map[aname] = {
                        "end1": {"role": ends[0].get("Role",""), "type": ends[0].get("Type",""), "multiplicity": ends[0].get("Multiplicity","")},
                        "end2": {"role": ends[1].get("Role",""), "type": ends[1].get("Type",""), "multiplicity": ends[1].get("Multiplicity","")},
                    }

            for entity_type in root.iter(f"{{{ns_uri}}}EntityType"):
                name = entity_type.get("Name", "")
                props = [{"Name": p.get("Name"), "Type": p.get("Type")}
                         for p in entity_type.iter(f"{{{ns_uri}}}Property")]

                # Parse navigation properties (relationships)
                nav_props = []
                for nav in entity_type.iter(f"{{{ns_uri}}}NavigationProperty"):
                    nav_name = nav.get("Name", "")
                    relationship = nav.get("Relationship", "")
                    to_role = nav.get("ToRole", "")
                    # Resolve target entity from association map
                    assoc_short = relationship.split(".")[-1] if "." in relationship else relationship
                    target_type = ""
                    multiplicity = ""
                    if assoc_short in assoc_map:
                        a = assoc_map[assoc_short]
                        target_end = a["end2"] if a["end1"]["role"] != to_role else a["end1"]
                        if target_end["role"] == to_role:
                            target_type = target_end["type"].split(".")[-1]
                            multiplicity = target_end["multiplicity"]
                    nav_props.append({"Name": nav_name, "TargetEntity": target_type, "Multiplicity": multiplicity})

                entities.append({
                    "EntityType": name,
                    "Properties": props[:20],
                    "NavigationProperties": nav_props
                })

        _metadata_cache[service_path] = entities
        logger.info(f"Metadata for {service_path}: {len(entities)} entity types")
        return json.dumps({"service": service_path, "entity_types": entities}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Tool 3: Query OData entity set ────────────────────────────────────────────

@mcp.tool()
def query_sap_odata(ctx: Context, service_path: str, entity_set: str, top: int = 10,
                    skip: int = 0, filter_expr: str = "", select_fields: str = "") -> str:
    """Query any SAP OData entity set.
    Args:
        service_path: OData service path (e.g. '/sap/opu/odata/sap/API_SALES_ORDER_SRV')
        entity_set: Entity set name (e.g. 'A_SalesOrder')
        top: Max records to return (default 10)
        skip: Records to skip for pagination (default 0)
        filter_expr: OData $filter expression (e.g. "SalesOrder eq '4'")
        select_fields: Comma-separated fields to return
    """
    token = _get_token(ctx)
    if not token:
        return json.dumps({"error": "No bearer token in request"})
    try:
        params = {"$format": "json", "$top": str(top), "$skip": str(skip)}
        if filter_expr:   params["$filter"] = filter_expr
        if select_fields: params["$select"] = select_fields
        path = f"{service_path}/{entity_set}"
        data = _sap_get(path, token, params)
        results = data.get("d", {}).get("results", [])
        if not results and "d" in data:
            d = data["d"]
            if isinstance(d, dict) and "results" not in d:
                results = [d]
        logger.info(f"Query {path}: {len(results)} results")
        return json.dumps({"count": len(results), "results": results}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Tool 4: Get single entity by key ──────────────────────────────────────────

@mcp.tool()
def get_sap_entity(ctx: Context, service_path: str, entity_set: str, key: str) -> str:
    """Get a single SAP entity by its key value.
    Args:
        service_path: OData service path
        entity_set: Entity set name
        key: Entity key value (e.g. '0000000004')
    """
    token = _get_token(ctx)
    if not token:
        return json.dumps({"error": "No bearer token in request"})
    try:
        path = f"{service_path}/{entity_set}('{key}')"
        data = _sap_get(path, token, {"$format": "json"})
        return json.dumps(data.get("d", {}), indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Tool 5: Update SAP entity (PATCH) ─────────────────────────────────────────

@mcp.tool()
def update_sap_entity(ctx: Context, service_path: str, entity_set: str,
                      key: str, payload: str) -> str:
    """Update a SAP entity via PATCH. Handles CSRF token and ETag automatically.
    Args:
        service_path: OData service path
        entity_set: Entity set name
        key: Entity key expression (e.g. "SalesOrder='2',SalesOrderItem='10'")
        payload: JSON string with fields to update (e.g. '{"RequestedQuantity": "5"}')
    """
    token = _get_token(ctx)
    if not token:
        return json.dumps({"error": "No bearer token in request"})
    try:
        update_data = json.loads(payload) if isinstance(payload, str) else payload
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON payload: {e}"})

    entity_url = f"{SAP_BASE_URL}{service_path}/{entity_set}({key})"
    try:
        with httpx.Client(verify=False, timeout=30.0) as c:
            fetch_resp = c.get(entity_url, headers={
                "Authorization": f"Bearer {token}", "Accept": "application/json", "x-csrf-token": "Fetch"
            }, params={"$format": "json"})
            fetch_resp.raise_for_status()
            csrf_token = fetch_resp.headers.get("x-csrf-token", "")
            etag = fetch_resp.headers.get("etag", "*")
            if not csrf_token:
                return json.dumps({"error": "SAP did not return a CSRF token"})

            patch_resp = c.patch(entity_url, headers={
                "Authorization": f"Bearer {token}", "Content-Type": "application/json",
                "Accept": "application/json", "x-csrf-token": csrf_token, "If-Match": etag,
            }, content=json.dumps(update_data))

            if patch_resp.status_code in (200, 204):
                verify_resp = c.get(entity_url, headers={
                    "Authorization": f"Bearer {token}", "Accept": "application/json"
                }, params={"$format": "json"})
                if verify_resp.status_code == 200:
                    return json.dumps({"status": "success", "updated_entity": verify_resp.json().get("d", {})}, indent=2)
                return json.dumps({"status": "success", "message": f"Updated {entity_url}"})
            else:
                return json.dumps({"error": f"PATCH returned {patch_resp.status_code}", "details": patch_resp.text[:500]})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Tool 6: Create SAP entity (POST) ──────────────────────────────────────────

@mcp.tool()
def create_sap_entity(ctx: Context, service_path: str, entity_set: str, payload: str) -> str:
    """Create a new SAP entity via POST. Handles CSRF token automatically.
    Args:
        service_path: OData service path
        entity_set: Entity set name
        payload: JSON string with entity data to create
    """
    token = _get_token(ctx)
    if not token:
        return json.dumps({"error": "No bearer token in request"})
    try:
        create_data = json.loads(payload) if isinstance(payload, str) else payload
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON payload: {e}"})

    entity_url = f"{SAP_BASE_URL}{service_path}/{entity_set}"
    try:
        with httpx.Client(verify=False, timeout=30.0) as c:
            fetch_resp = c.get(f"{SAP_BASE_URL}{service_path}/", headers={
                "Authorization": f"Bearer {token}", "x-csrf-token": "Fetch"
            })
            csrf_token = fetch_resp.headers.get("x-csrf-token", "")
            if not csrf_token:
                return json.dumps({"error": "SAP did not return a CSRF token"})

            post_resp = c.post(entity_url, headers={
                "Authorization": f"Bearer {token}", "Content-Type": "application/json",
                "Accept": "application/json", "x-csrf-token": csrf_token,
            }, content=json.dumps(create_data))

            if post_resp.status_code in (200, 201):
                return json.dumps({"status": "created", "entity": post_resp.json().get("d", {})}, indent=2)
            else:
                return json.dumps({"error": f"POST returned {post_resp.status_code}", "details": post_resp.text[:500]})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Resources ─────────────────────────────────────────────────────────────────

@mcp.resource("sap://catalog")
def get_cached_catalog() -> str:
    """Returns the cached SAP service catalog."""
    if not _catalog_cache:
        return json.dumps({"message": "Catalog not loaded. Call discover_sap_services first."})
    return json.dumps(list(_catalog_cache.values()), indent=2)


@mcp.resource("sap://metadata/{service_name}")
def get_cached_metadata(service_name: str) -> str:
    """Returns cached metadata for a specific service."""
    for key, val in _metadata_cache.items():
        if service_name in key:
            return json.dumps(val, indent=2)
    return json.dumps({"message": f"No cached metadata for {service_name}."})


@mcp.tool()
def search_sap_services(ctx: Context, keyword: str, limit: int = 20) -> str:
    """Search SAP services by keyword. Returns only matching services (not the full catalog).
    Automatically loads catalog into cache on first call.

    Args:
        keyword: Search term (e.g. 'sales', 'material', 'purchase', 'maintenance')
        limit: Max results to return (default 20)
    """
    global _catalog_cache
    # Auto-populate cache if empty
    if not _catalog_cache:
        token = _get_token(ctx)
        if token:
            try:
                data = _sap_get(CATALOG_URL, token, {"$format": "json"})
                for svc in data.get("d", {}).get("results", []):
                    entry = {
                        "Title": svc.get("Title", ""),
                        "TechnicalServiceName": svc.get("TechnicalServiceName", ""),
                        "ServiceUrl": svc.get("ServiceUrl", ""),
                        "Description": svc.get("Description", ""),
                        "TechnicalServiceVersion": svc.get("TechnicalServiceVersion", ""),
                    }
                    _catalog_cache[entry["TechnicalServiceName"]] = entry
                logger.info(f"Auto-cached {len(_catalog_cache)} services")
            except Exception as e:
                return json.dumps({"error": f"Failed to load catalog: {e}"})

    kw = keyword.lower()
    matches = [v for v in _catalog_cache.values()
               if kw in v.get("Title", "").lower() or kw in v.get("Description", "").lower()]
    return json.dumps({"keyword": keyword, "match_count": len(matches), "results": matches[:limit]}, indent=2)


# ── Enriched cache tools (served from startup-loaded metadata) ────────────────

@mcp.tool()
def get_service_entities(ctx: Context, service: str) -> str:
    """Get entity types for a SAP OData service from cache. Returns entity names, entity set names
    (for OData queries), key fields, and navigation properties. No SAP call needed.
    Args:
        service: Service title (e.g. API_SALES_ORDER_SRV, MM_PUR_RFQITEM_MNTR_SRV)
    """
    # Try enriched cache first
    entities = _entity_cache.get(service, [])
    if entities:
        summary = [{"entity_type": e["entity_type"], "entity_set": e.get("entity_set", ""),
                     "keys": e.get("keys", []), "property_count": len(e["properties"]),
                     "nav_properties": [{"name": n["name"], "target": n["target_entity"],
                                         "multiplicity": n["multiplicity"]} for n in e.get("nav_properties", [])]}
                    for e in entities]
        return json.dumps(summary, indent=2)
    # Fallback: fetch live if cache not loaded yet
    token = _get_token(ctx)
    if not token:
        return json.dumps({"error": "No token and cache not loaded"})
    try:
        xml_text = _sap_get_xml(f"/sap/opu/odata/sap/{service}/$metadata", token)
        entities = _parse_metadata_xml(xml_text)
        _entity_cache[service] = entities
        summary = [{"entity_type": e["entity_type"], "entity_set": e.get("entity_set", ""),
                     "keys": e.get("keys", []), "property_count": len(e["properties"]),
                     "nav_properties": [{"name": n["name"], "target": n["target_entity"],
                                         "multiplicity": n["multiplicity"]} for n in e.get("nav_properties", [])]}
                    for e in entities]
        return json.dumps(summary, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_entity_properties(ctx: Context, service: str, entity: str) -> str:
    """Get properties/fields for a specific entity type from cache.
    Args:
        service: Service title (e.g. API_SALES_ORDER_SRV)
        entity: Entity type name (e.g. A_SalesOrderType)
    """
    entities = _entity_cache.get(service, [])
    if not entities:
        # Try fetching live
        token = _get_token(ctx)
        if token:
            try:
                xml_text = _sap_get_xml(f"/sap/opu/odata/sap/{service}/$metadata", token)
                entities = _parse_metadata_xml(xml_text)
                _entity_cache[service] = entities
            except Exception as e:
                return json.dumps({"error": str(e)})
    for e in entities:
        if e["entity_type"] == entity:
            return json.dumps({"entity_type": e["entity_type"], "entity_set": e.get("entity_set", ""),
                               "keys": e.get("keys", []), "properties": e["properties"],
                               "nav_properties": e.get("nav_properties", [])}, indent=2)
    return json.dumps({"error": f"Entity {entity} not found in {service}"})


@mcp.tool()
def find_entity_by_field(ctx: Context, field_name: str, limit: int = 15) -> str:
    """Find which SAP services/entities contain a specific field name. Searches across all cached metadata.
    Args:
        field_name: Field name to search for (e.g. 'SalesOrder', 'Material', 'PurchaseRequisition')
        limit: Max results (default 15)
    """
    kw = field_name.lower()
    results = [entry for entry in _field_index if kw in entry["property_name"].lower()][:limit]
    return json.dumps(results, indent=2)


@mcp.tool()
def cache_stats(ctx: Context) -> str:
    """Show cache statistics — how many services, entities, and properties are cached."""
    total_entities = sum(len(v) for v in _entity_cache.values())
    return json.dumps({
        "status": "loaded" if _cache_loaded else ("error" if _cache_error else "loading"),
        "services_in_catalog": len(_catalog_cache),
        "services_with_metadata": len(_entity_cache),
        "total_entities": total_entities,
        "total_fields_indexed": len(_field_index),
        "error": _cache_error or None
    }, indent=2)


# ── Tool 7: Generate & Deploy a new MCP Server ───────────────────────────────

_SERVER_TEMPLATE = '''"""
{description}
Auto-generated by SAP Smart Agent.
"""
import os, json, logging, httpx
from mcp.server.fastmcp import FastMCP, Context

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("{agent_name}")

SAP_BASE_URL = os.environ.get("SAP_BASE_URL", "https://vhcals4hci.awspoc.club")
mcp = FastMCP("{agent_name}", host="0.0.0.0", stateless_http=True)

def _get_token(ctx: Context) -> str:
    try:
        auth = ctx.request_context.request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            t = auth[7:]
            if t: return t
    except: pass
    if bearer_token: return bearer_token
    return os.environ.get("SAP_BEARER_TOKEN", "")

def _sap_get(path, token, params=None):
    url = f"{{SAP_BASE_URL}}{{path}}"
    with httpx.Client(verify=False, timeout=30.0) as c:
        r = c.get(url, headers={{"Authorization": f"Bearer {{token}}", "Accept": "application/json"}}, params=params or {{}})
        r.raise_for_status()
        return r.json()

{tools}

if __name__ == "__main__":
    logger.info("=== {agent_name} Starting ===")
    mcp.run(transport="streamable-http")
'''

_BUILDSPEC_INLINE = """version: 0.2
env:
  parameter-store:
    OKTA_CLIENT_ID: "/sap_smart_agent/okta_client_id"
    OKTA_DOMAIN: "/sap_smart_agent/okta_domain"
phases:
  install:
    runtime-versions:
      python: 3.11
    commands:
      - pip install bedrock-agentcore-starter-toolkit boto3 --quiet
  pre_build:
    commands:
      - aws s3 cp s3://${STAGING_BUCKET}/generated/${BUILD_ID}/server.py ./sap_generated_mcp_server.py
      - aws s3 cp s3://${STAGING_BUCKET}/generated/${BUILD_ID}/requirements.txt ./requirements_generated.txt
      - aws s3 cp s3://${STAGING_BUCKET}/generated/${BUILD_ID}/meta.json ./meta.json
      - AGENT_NAME=$(python -c "import json; print(json.load(open('meta.json'))['agent_name'])")
  build:
    commands:
      - |
        python - <<'EOF'
        import os, json, time, boto3
        from bedrock_agentcore_starter_toolkit import Runtime
        from boto3.session import Session
        agent_name = json.load(open("meta.json"))["agent_name"]
        okta_domain = os.environ["OKTA_DOMAIN"]
        okta_client_id = os.environ["OKTA_CLIENT_ID"]
        region = Session().region_name
        runtime = Runtime()
        auth_config = {"customJWTAuthorizer": {"allowedClients": [okta_client_id],
            "discoveryUrl": f"https://{okta_domain}/oauth2/default/.well-known/openid-configuration"}}
        runtime.configure(entrypoint="sap_generated_mcp_server.py", auto_create_execution_role=True,
            auto_create_ecr=True, requirements_file="requirements_generated.txt", region=region,
            authorizer_configuration=auth_config, protocol="MCP", agent_name=agent_name)
        result = runtime.launch(auto_update_on_conflict=True)
        while True:
            status = runtime.status().endpoint["status"]
            print(f"Status: {status}")
            if status in ["READY", "CREATE_FAILED", "UPDATE_FAILED"]: break
            time.sleep(15)
        if status == "READY":
            ssm = boto3.client("ssm", region_name=region)
            ssm.put_parameter(Name=f"/sap_generated/{agent_name}/agent_arn",
                Value=result.agent_arn, Type="String", Overwrite=True)
            print(f"DEPLOY_SUCCESS:{result.agent_arn}")
        else:
            exit(1)
        EOF
"""

_TOOL_TEMPLATE = '''
def {func_name}(ctx: Context, top: int = 10, skip: int = 0, filter_expr: str = "") -> str:
    """{docstring}"""
    token = _get_token(ctx)
    if not token:
        return json.dumps({{"error": "No bearer token"}})
    try:
        params = {{"$format": "json", "$top": str(top), "$skip": str(skip)}}
        if filter_expr: params["$filter"] = filter_expr
        data = _sap_get("{service_path}/{entity_set}", token, params)
        results = data.get("d", {{}}).get("results", [])
        return json.dumps({{"count": len(results), "results": results}}, indent=2)
    except Exception as e:
        return json.dumps({{"error": str(e)}})
'''


def _discover_relevant_services(token: str, domain_keywords: list) -> list:
    """Query SAP catalog and filter services matching domain keywords."""
    try:
        data = _sap_get(CATALOG_URL, token, {"$format": "json"})
        services = data.get("d", {}).get("results", [])
        logger.info(f"Catalog returned {len(services)} total services, filtering with keywords: {domain_keywords}")
        matched = []
        for svc in services:
            title = (svc.get("Title", "") + svc.get("TechnicalServiceName", "") +
                     svc.get("Description", "")).lower()
            if any(kw.lower() in title for kw in domain_keywords):
                matched.append({
                    "Title": svc.get("Title", ""),
                    "TechnicalServiceName": svc.get("TechnicalServiceName", ""),
                    "ServiceUrl": svc.get("ServiceUrl", ""),
                })
        # Prioritize standard SAP API services over custom Z-services
        api_services = [s for s in matched if s["Title"].startswith("API_")]
        other_std = [s for s in matched if not s["TechnicalServiceName"].startswith("Z")
                     and not s["Title"].startswith("API_")]
        z_services = [s for s in matched if s["TechnicalServiceName"].startswith("Z")
                      and not s["Title"].startswith("API_")]
        prioritized = api_services + other_std + z_services
        logger.info(f"Matched {len(matched)} services ({len(api_services)} API_, {len(other_std)} other std, {len(z_services)} Z-custom)")
        logger.info(f"Top 10: {[s['TechnicalServiceName'] for s in prioritized[:10]]}")
        return prioritized[:10]
    except Exception as e:
        logger.error(f"Service discovery failed: {e}")
        return []


def _get_entities_for_service(service_path: str, token: str) -> list:
    """Get top entity types for a service."""
    try:
        import xml.etree.ElementTree as ET
        logger.info(f"Fetching metadata for: {service_path}")
        xml_text = _sap_get_xml(f"{service_path}/$metadata", token)
        logger.info(f"Metadata response length: {len(xml_text)} chars")
        root = ET.fromstring(xml_text)
        entities = []
        for ns_uri in ["http://schemas.microsoft.com/ado/2008/09/edm",
                       "http://schemas.microsoft.com/ado/2009/11/edm",
                       "http://docs.oasis-open.org/odata/ns/edm"]:
            for et in root.iter(f"{{{ns_uri}}}EntityType"):
                name = et.get("Name", "")
                if name and not name.startswith("I_") and not name.startswith("SAP__"):
                    entities.append(name)
        logger.info(f"Found {len(entities)} entities for {service_path}")
        return entities[:5]
    except Exception as e:
        logger.error(f"_get_entities_for_service failed for {service_path}: {e}")
        return []


def _generate_server_code_with_bedrock(prompt: str, services_with_entities: list,
                                        agent_name: str) -> str:
    """Use Claude via Bedrock to generate the MCP server tool functions."""
    bedrock = boto3.client("bedrock-runtime", region_name=boto3.session.Session().region_name)

    services_summary = json.dumps(services_with_entities, indent=2)
    system = (
        "You are an expert SAP developer. Generate Python MCP tool functions for a FastMCP server. "
        "Each tool must query one SAP OData entity set. "
        "\n\nCRITICAL: The entity names in the metadata are ENTITY TYPES (e.g. A_SalesOrderType, "
        "A_SalesOrderHeaderPartnerType). In the OData URL path you must use the ENTITY SET name, "
        "which is the type name WITHOUT the 'Type' suffix. Examples:\n"
        "  - A_SalesOrderType -> use A_SalesOrder in the URL\n"
        "  - A_SalesOrderHeaderPartnerType -> use A_SalesOrderHeaderPartner in the URL\n"
        "  - A_MaintenanceOrderType -> use A_MaintenanceOrder in the URL\n"
        "NEVER use the 'Type' suffix in OData URL paths.\n\n"
        "Use ONLY this exact function signature pattern:\n"
        "@mcp.tool()\n"
        "def func_name(ctx: Context, top: int = 10, skip: int = 0, filter_expr: str = \"\", bearer_token: str = \"\") -> str:\n"
        "    \"\"\"docstring\"\"\"\n"
        "    token = _get_token(ctx)\n"
        "    if not token: return json.dumps({\"error\": \"No bearer token\"})\n"
        "    try:\n"
        "        params = {\"$format\": \"json\", \"$top\": str(top), \"$skip\": str(skip)}\n"
        "        if filter_expr: params[\"$filter\"] = filter_expr\n"
        "        data = _sap_get(\"<service_path>/<entity_set_without_Type_suffix>\", token, params)\n"
        "        results = data.get(\"d\", {}).get(\"results\", [])\n"
        "        return json.dumps({\"count\": len(results), \"results\": results}, indent=2)\n"
        "    except Exception as e:\n"
        "        return json.dumps({\"error\": str(e)})\n\n"
        "Return ONLY the tool function definitions, no imports, no main block."
    )
    user = (
        f"User request: {prompt}\n\n"
        f"Available SAP services and entities:\n{services_summary}\n\n"
        f"Generate 3-7 MCP tool functions covering the most useful operations for this domain. "
        f"Use snake_case function names that describe what the tool does (e.g. get_maintenance_orders, list_notifications)."
    )

    response = bedrock.invoke_model(
        modelId="us.anthropic.claude-sonnet-4-20250514-v1:0",
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 4096,
            "system": system,
            "messages": [{"role": "user", "content": user}]
        }),
        contentType="application/json",
        accept="application/json"
    )
    result = json.loads(response["body"].read())
    return result["content"][0]["text"]


def _ensure_generator_infrastructure(region, ssm, cb, s3) -> tuple:
    """Auto-provision S3 bucket + CodeBuild project on first use. Idempotent."""
    CODEBUILD_PROJECT = "sap-mcp-generator"
    account_id = boto3.client("sts", region_name=region).get_caller_identity()["Account"]
    STAGING_BUCKET = f"sap-mcp-generator-{account_id}-{region}"

    # Check if already set up
    try:
        staging_bucket = ssm.get_parameter(Name="/sap_smart_agent/staging_bucket")["Parameter"]["Value"]
        codebuild_project = ssm.get_parameter(Name="/sap_smart_agent/codebuild_project")["Parameter"]["Value"]
        logger.info("Generator infrastructure already provisioned")
        return staging_bucket, codebuild_project
    except ssm.exceptions.ParameterNotFound:
        logger.info("First-time setup: provisioning generator infrastructure...")

    iam = boto3.client("iam", region_name=region)
    CODEBUILD_ROLE = "sap-mcp-generator-codebuild-role"

    # 1. S3 bucket
    try:
        if region == "us-east-1":
            s3.create_bucket(Bucket=STAGING_BUCKET)
        else:
            s3.create_bucket(Bucket=STAGING_BUCKET,
                             CreateBucketConfiguration={"LocationConstraint": region})
        logger.info(f"Created S3 bucket: {STAGING_BUCKET}")
    except Exception:
        logger.info(f"S3 bucket already exists: {STAGING_BUCKET}")

    # 2. CodeBuild IAM role
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
        "Principal": {"Service": "codebuild.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
    try:
        role_arn = iam.create_role(RoleName=CODEBUILD_ROLE,
            AssumeRolePolicyDocument=json.dumps(trust))["Role"]["Arn"]
        logger.info(f"Created CodeBuild role: {role_arn}")
    except iam.exceptions.EntityAlreadyExistsException:
        role_arn = iam.get_role(RoleName=CODEBUILD_ROLE)["Role"]["Arn"]

    cb_policy = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": f"arn:aws:logs:{region}:{account_id}:log-group:/aws/codebuild/{CODEBUILD_PROJECT}*"},
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
         "Resource": [f"arn:aws:s3:::{STAGING_BUCKET}", f"arn:aws:s3:::{STAGING_BUCKET}/*"]},
        {"Effect": "Allow", "Action": ["ssm:GetParameter", "ssm:PutParameter"],
         "Resource": f"arn:aws:ssm:{region}:{account_id}:parameter/sap_*"},
        {"Effect": "Allow", "Action": ["ecr:*"], "Resource": "*"},
        {"Effect": "Allow", "Action": ["bedrock-agentcore:CreateAgentRuntime",
         "bedrock-agentcore:UpdateAgentRuntime", "bedrock-agentcore:GetAgentRuntime"],
         "Resource": "*"},
        {"Effect": "Allow", "Action": ["iam:CreateRole", "iam:AttachRolePolicy",
         "iam:PassRole", "iam:GetRole", "iam:PutRolePolicy"],
         "Resource": f"arn:aws:iam::{account_id}:role/sap-generated-*"},
    ]}
    iam.put_role_policy(RoleName=CODEBUILD_ROLE, PolicyName="sap-mcp-generator-policy",
                        PolicyDocument=json.dumps(cb_policy))

    # 3. CodeBuild project — buildspec inline
    import time as _time
    _time.sleep(10)  # IAM propagation

    buildspec_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "buildspec.yml")
    if os.path.exists(buildspec_path):
        with open(buildspec_path) as f:
            buildspec = f.read()
    else:
        # Inline fallback buildspec
        buildspec = _BUILDSPEC_INLINE

    try:
        cb.create_project(
            name=CODEBUILD_PROJECT,
            description="Generates and deploys SAP MCP servers to AgentCore",
            source={"type": "NO_SOURCE", "buildspec": buildspec},
            artifacts={"type": "NO_ARTIFACTS"},
            environment={"type": "LINUX_CONTAINER", "image": "aws/codebuild/standard:7.0",
                         "computeType": "BUILD_GENERAL1_SMALL", "privilegedMode": True,
                         "environmentVariables": [
                             {"name": "STAGING_BUCKET", "value": STAGING_BUCKET, "type": "PLAINTEXT"}]},
            serviceRole=role_arn, timeoutInMinutes=30,
        )
        logger.info(f"Created CodeBuild project: {CODEBUILD_PROJECT}")
    except cb.exceptions.ResourceAlreadyExistsException:
        logger.info(f"CodeBuild project already exists: {CODEBUILD_PROJECT}")

    # 4. Store in SSM
    okta_domain = os.environ.get("OKTA_DOMAIN", "trial-1053860.okta.com")
    okta_client_id = os.environ.get("OKTA_CLIENT_ID", "0oa10vth79kZAuXGt698")
    for key, val in {
        "/sap_smart_agent/staging_bucket": STAGING_BUCKET,
        "/sap_smart_agent/codebuild_project": CODEBUILD_PROJECT,
        "/sap_smart_agent/okta_domain": okta_domain,
        "/sap_smart_agent/okta_client_id": okta_client_id,
    }.items():
        ssm.put_parameter(Name=key, Value=val, Type="String", Overwrite=True)

    logger.info("Generator infrastructure provisioned successfully")
    return STAGING_BUCKET, CODEBUILD_PROJECT


@mcp.tool()
def generate_and_deploy_mcp_server(ctx: Context, prompt: str, agent_name: str) -> str:
    """Generate a new focused SAP MCP server and deploy it to AgentCore.

    The parent agent discovers relevant SAP OData services automatically based on the prompt,
    generates tool code using Claude, and deploys a new AgentCore runtime with the same Okta auth.

    Args:
        prompt: Description of what the MCP server should do (e.g. 'Plant Maintenance tools for orders and notifications')
        agent_name: Short identifier for the new server (e.g. 'sap_pm_agent'). Must be lowercase with underscores.
    """
    token = _get_token(ctx)
    if not token:
        return json.dumps({"error": "No bearer token"})

    try:
        boto_session = boto3.session.Session()
        region = boto_session.region_name
        ssm = boto3.client("ssm", region_name=region)
        s3 = boto3.client("s3", region_name=region)
        cb = boto3.client("codebuild", region_name=region)

        # Load config from SSM — auto-provision if first time
        staging_bucket, codebuild_project = _ensure_generator_infrastructure(region, ssm, cb, s3)

        logger.info(f"Generating MCP server '{agent_name}' for: {prompt}")

        # Step 1: Extract domain keywords from prompt and discover services
        stop_words = {"with", "that", "this", "from", "have", "will", "should", "tools", "tool",
                      "details", "including", "information", "related", "help", "about", "also",
                      "their", "them", "these", "those", "into", "like", "such", "some", "other",
                      "more", "most", "very", "just", "only", "also", "both", "each", "every"}
        keywords = [w.strip(",.;:!?") for w in prompt.lower().split()
                    if len(w.strip(",.;:!?")) > 3 and w.strip(",.;:!?") not in stop_words]
        # Deduplicate while preserving order
        seen = set()
        keywords = [k for k in keywords if not (k in seen or seen.add(k))]
        logger.info(f"Discovering services for keywords: {keywords}")
        matched_services = _discover_relevant_services(token, keywords)

        if not matched_services:
            return json.dumps({"error": f"No SAP services found matching: {prompt}. Try more specific keywords."})

        logger.info(f"Found {len(matched_services)} matching services")

        # Step 2: Get entity types for each matched service
        services_with_entities = []
        for svc in matched_services[:5]:
            svc_title = svc["Title"]
            svc_path = f"/sap/opu/odata/sap/{svc_title}"
            entities = _get_entities_for_service(svc_path, token)
            if entities:
                services_with_entities.append({
                    "service_path": svc_path,
                    "title": svc["Title"],
                    "entities": entities
                })

        if not services_with_entities:
            return json.dumps({"error": "Could not retrieve metadata for matched services."})

        # Step 3: Generate tool code with Bedrock
        logger.info("Generating tool code with Claude...")
        generated_tools = _generate_server_code_with_bedrock(prompt, services_with_entities, agent_name)

        # Step 4: Assemble full server file
        description = f"SAP MCP Server for: {prompt}"
        server_code = _SERVER_TEMPLATE.format(
            description=description,
            agent_name=agent_name,
            tools=generated_tools
        )

        requirements = "fastmcp\nhttpx\nbedrock-agentcore-starter-toolkit\nboto3\n"
        meta = {"agent_name": agent_name, "prompt": prompt, "services": services_with_entities}

        # Step 5: Upload to S3
        build_id = str(uuid.uuid4())[:8]
        prefix = f"generated/{build_id}"
        logger.info(f"Uploading to S3: s3://{staging_bucket}/{prefix}/")
        s3.put_object(Bucket=staging_bucket, Key=f"{prefix}/server.py", Body=server_code.encode())
        s3.put_object(Bucket=staging_bucket, Key=f"{prefix}/requirements.txt", Body=requirements.encode())
        s3.put_object(Bucket=staging_bucket, Key=f"{prefix}/meta.json", Body=json.dumps(meta).encode())

        # Step 6: Trigger CodeBuild
        logger.info(f"Triggering CodeBuild project: {codebuild_project}")
        build_resp = cb.start_build(
            projectName=codebuild_project,
            environmentVariablesOverride=[
                {"name": "BUILD_ID", "value": build_id, "type": "PLAINTEXT"},
                {"name": "STAGING_BUCKET", "value": staging_bucket, "type": "PLAINTEXT"},
            ]
        )
        cb_build_id = build_resp["build"]["id"]
        logger.info(f"CodeBuild started: {cb_build_id}")

        return json.dumps({
            "status": "deploying",
            "agent_name": agent_name,
            "codebuild_build_id": cb_build_id,
            "services_used": [s["service_path"] for s in services_with_entities],
            "message": (
                f"Deployment started. CodeBuild is building and deploying '{agent_name}' to AgentCore. "
                f"This takes ~10-15 minutes. Once complete, the ARN will be stored at SSM key: "
                f"/sap_generated/{agent_name}/agent_arn. "
                f"Track progress: aws codebuild batch-get-builds --ids '{cb_build_id}'"
            )
        }, indent=2)

    except Exception as e:
        logger.error(f"generate_and_deploy_mcp_server failed: {e}")
        return json.dumps({"error": str(e)})


# ── Smart Query: Natural Language → Open SQL → Results ────────────────────────

# Common SAP table reference for the LLM to generate accurate SQL
_SAP_TABLE_REFERENCE = """
KEY SAP TABLES BY DOMAIN:

SALES & DISTRIBUTION (SD):
  VBAK - Sales Order Header (VBELN, ERDAT, ERZET, ERNAM, AUART, VKORG, VTWEG, SPART, KUNNR, NETWR, WAERK)
  VBAP - Sales Order Item (VBELN, POSNR, MATNR, WERKS, LGORT, KWMENG, MEINS, NETWR, WAERK)
  VBFA - Sales Document Flow (VBELV, POSNV, VBELN, POSNN, VBTYP_N, RFMNG, ERDAT)
  LIKP - Delivery Header (VBELN, ERDAT, LFART, WADAT, KUNNR, VSTEL)
  LIPS - Delivery Item (VBELN, POSNR, MATNR, WERKS, LFIMG, MEINS, VGBEL, VGPOS)
  VBRK - Billing Header (VBELN, FKART, FKDAT, KUNAG, NETWR, WAERK, BUKRS)
  VBRP - Billing Item (VBELN, POSNR, MATNR, FKIMG, NETWR, AUBEL, AUPOS)
  KNA1 - Customer Master (KUNNR, NAME1, NAME2, ORT01, LAND1, PSTLZ, STRAS)

MATERIALS MANAGEMENT (MM):
  EKKO - Purchase Order Header (EBELN, BUKRS, BSTYP, BSART, ERNAM, AEDAT, LIFNR, EKORG, EKGRP, WAERS, NETWR)
  EKPO - Purchase Order Item (EBELN, EBELP, MATNR, WERKS, LGORT, MENGE, MEINS, NETPR, NETWR)
  EBAN - Purchase Requisition (BANFN, BNFPO, BSART, ERNAM, BADAT, MATNR, WERKS, MENGE, MEINS)
  EKBE - PO History / GR (EBELN, EBELP, ZEESSION, VGABE, GJAHR, BELNR, BUZEI, MENGE, WRBTR, WAERS, BUDAT)
  MARA - Material Master General (MATNR, ERNAM, ERSDA, MTART, MBRSH, MATKL, MEINS, BRGEW, GEWEI)
  MAKT - Material Description (MATNR, SPRAS, MAKTX)
  MARC - Plant Data for Material (MATNR, WERKS, EKGRP, DISMM, DISPO, PLIFZ, WEBAZ)
  MARD - Storage Location Data (MATNR, WERKS, LGORT, LABST, INSME, SPEME)
  LFA1 - Vendor Master (LIFNR, NAME1, NAME2, ORT01, LAND1, PSTLZ, STRAS)

FINANCE (FI):
  BKPF - Accounting Document Header (BUKRS, BELNR, GJAHR, BLART, BUDAT, BLDAT, MONAT, USNAM, WAERS, XBLNR)
  BSEG - Accounting Document Item (BUKRS, BELNR, GJAHR, BUZEI, BSCHL, KOART, KONTO, DMBTR, WRBTR, SHKZG)
  BSID - Customer Open Items (BUKRS, KUNNR, UMSKS, UMSKZ, AUGDT, AUGBL, ZUONR, GJAHR, BELNR, BUZEI, BUDAT, BLDAT, WAERS, SHKZG, DMBTR, WRBTR)
  BSIK - Vendor Open Items (BUKRS, LIFNR, UMSKS, UMSKZ, AUGDT, AUGBL, ZUONR, GJAHR, BELNR, BUZEI, BUDAT, BLDAT, WAERS, SHKZG, DMBTR, WRBTR)
  BSAD - Customer Cleared Items (same fields as BSID)
  BSAK - Vendor Cleared Items (same fields as BSIK)
  SKA1 - GL Account Master (KTOPL, SAKNR, XBILK, GVTYP, KTOKS)
  SKAT - GL Account Text (SPRAS, KTOPL, SAKNR, TXT20, TXT50)
  RBKP - Invoice Document Header (BELNR, GJAHR, BUKRS, LIFNR, BLDAT, BUDAT, WAERS, RMWWR, XBLNR)
  RSEG - Invoice Document Item (BELNR, GJAHR, BUZEI, EBELN, EBELP, MATNR, MENGE, WRBTR, WAERS)

PLANT MAINTENANCE (PM):
  AUFK - Order Master (AUFNR, AUART, AUTYP, ERNAM, ERDAT, BUKRS, WERKS, KTEXT)
  AFIH - Maintenance Order Header (AUFNR, EQUNR, TPLNR, IWERK, ILART, QMNUM)
  EQUI - Equipment Master (EQUNR, ERDAT, ERNAM, EQART, EQTYP, HERST, TYPBZ)
  TPLNR - Functional Location (TPLNR, ERDAT, ERNAM, PLTXT)
  QMEL - Quality Notification (QMNUM, QMART, ERNAM, ERDAT, QMTXT, EQUNR, TPLNR)

WAREHOUSE / INVENTORY:
  MSEG - Material Document Item (MBLNR, MJAHR, ZEILE, BWART, MATNR, WERKS, LGORT, MENGE, MEINS, EBELN, EBELP)
  MKPF - Material Document Header (MBLNR, MJAHR, BLDAT, BUDAT, USNAM, BKTXT, XBLNR)

CROSS-MODULE:
  T001 - Company Codes (BUKRS, BUTXT, ORT01, LAND1, WAERS)
  T001W - Plants (WERKS, NAME1, BWKEY, BUKRS, LAND1)
  T001L - Storage Locations (WERKS, LGORT, LGOBE)
  USR02 - User Master (BNAME, USTYP, CLASS, GLTGV, GLTGB, TRDAT)
  DD03L - Table Fields (TABNAME, FIELDNAME, POSITION, KEYFLAG, DATATYPE, LENG, DECIMALS, ROLLNAME)

IMPORTANT SQL RULES FOR SAP ADT DATA PREVIEW:
- Use standard SQL SELECT syntax
- Always qualify with table name if joining: SELECT a~FIELD, b~FIELD
- String literals use single quotes: WHERE MATNR = 'TG14'
- Date format: WHERE ERDAT > '20240101' (YYYYMMDD as string)
- JOINs: ONLY INNER JOIN is supported. Do NOT use LEFT JOIN, RIGHT JOIN, or OUTER JOIN.
- JOIN syntax: SELECT a~VBELN, b~MATNR FROM VBAK AS a INNER JOIN VBAP AS b ON a~VBELN = b~VBELN
- Use tilde (~) for field qualification in JOINs: a~FIELD not a.FIELD
- Do NOT use ORDER BY — it is not supported
- Do NOT use COUNT(DISTINCT ...) — use COUNT(*) instead
- Do NOT use UP TO N ROWS — the system handles row limits separately
- Do NOT add semicolons at the end
- Aggregation: SELECT COUNT(*) AS CNT, SUM(NETWR) AS TOTAL FROM VBAK GROUP BY AUART
"""


@mcp.tool()
def smart_query(ctx: Context, question: str, max_rows: int = 100,
                prefer: str = "auto") -> str:
    """Answer a natural language question about SAP data. Auto-decides between OData and SQL.

    ROUTING LOGIC (when prefer='auto'):
    - Checks cached OData metadata for matching services/entities
    - Uses OData when: a matching service exists, query is simple read, no JOINs/aggregations needed
    - Uses SQL when: no OData service covers the data, JOINs needed, aggregations (COUNT/SUM/GROUP BY),
      raw table access (BSEG, MSEG, EKBE), or cross-module queries

    Examples:
        - "Show me all purchase orders for vendor 17300001"
        - "What materials were received in plant 1010 this month?"
        - "Count sales orders by order type"
        - "Join PO header and items for PO 4500001485"

    Args:
        question: Natural language question about SAP data
        max_rows: Maximum rows to return (default 100)
        prefer: 'auto' (let AI decide), 'odata' (force OData), 'sql' (force SQL)
    """
    token = _get_token(ctx)
    if not token:
        return json.dumps({"error": "No bearer token in request"})

    try:
        bedrock = boto3.client("bedrock-runtime",
                               region_name=boto3.session.Session().region_name)

        # Build OData service summary from cache for the router
        odata_summary = ""
        if _entity_cache and prefer != "sql":
            svc_lines = []
            for svc_name, entities in list(_entity_cache.items())[:80]:
                entity_names = [e["entity_type"] for e in entities[:5]]
                svc_lines.append(f"  {svc_name}: {', '.join(entity_names)}")
            odata_summary = "AVAILABLE ODATA SERVICES (service: entities):\n" + "\n".join(svc_lines)

        # Step 1: Route decision — ask Claude to decide OData vs SQL and generate the query
        route_system = (
            "You are an SAP data access expert. Given a user question, decide the best approach "
            "and generate the query.\n\n"
            "DECISION RULES:\n"
            "1. Use OData when:\n"
            "   - A matching OData service/entity exists in the available services list\n"
            "   - The query is a simple read (list, filter, get by key)\n"
            "   - No JOINs or aggregations are needed\n"
            "   - The user wants to read business objects (sales orders, POs, materials, etc.)\n"
            "2. Use SQL when:\n"
            "   - No OData service covers the data (e.g. BSEG, MSEG, EKBE, DD03L, USR02)\n"
            "   - JOINs across tables are needed\n"
            "   - Aggregations needed (COUNT, SUM, AVG, GROUP BY)\n"
            "   - The user asks about raw table data or cross-module queries\n"
            "   - The user explicitly mentions table names\n\n"
            "OUTPUT FORMAT (strict JSON, nothing else):\n"
            '{"method": "odata" or "sql",\n'
            ' "reasoning": "one line why",\n'
            ' "service": "SERVICE_NAME (only for odata)",\n'
            ' "entity_set": "EntitySetName (only for odata)",\n'
            ' "filter": "$filter expression (only for odata, can be empty)",\n'
            ' "select": "$select fields comma-separated (only for odata, can be empty)",\n'
            ' "sql": "full SELECT statement (only for sql)"}\n\n'
            "IMPORTANT: Output ONLY the JSON object. No markdown, no explanation.\n\n"
            f"{odata_summary}\n\n"
            f"{_SAP_TABLE_REFERENCE}"
        )

        if prefer == "odata":
            route_system += "\n\nFORCED: You MUST use OData method."
        elif prefer == "sql":
            route_system += "\n\nFORCED: You MUST use SQL method."

        response = bedrock.invoke_model(
            modelId="us.anthropic.claude-sonnet-4-20250514-v1:0",
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1024,
                "system": route_system,
                "messages": [{"role": "user", "content": question}]
            }),
            contentType="application/json",
            accept="application/json"
        )
        result = json.loads(response["body"].read())
        raw_decision = result["content"][0]["text"].strip()

        # Clean up markdown fencing if present
        if raw_decision.startswith("```"):
            raw_decision = "\n".join(raw_decision.split("\n")[1:])
        if raw_decision.endswith("```"):
            raw_decision = raw_decision.rsplit("```", 1)[0]
        raw_decision = raw_decision.strip()

        decision = json.loads(raw_decision)
        method = decision.get("method", "sql")
        reasoning = decision.get("reasoning", "")

        logger.info(f"smart_query route: {method} — {reasoning}")

        # Step 2: Execute based on decision
        if method == "odata":
            service = decision.get("service", "")
            entity_set = decision.get("entity_set", "")
            filter_expr = decision.get("filter", "")
            select_fields = decision.get("select", "")

            if not service or not entity_set:
                # Fallback to SQL if OData params are missing
                method = "sql"
            else:
                service_path = f"/sap/opu/odata/sap/{service}"
                odata_result = query_sap_odata(ctx, service_path, entity_set,
                                               top=max_rows, filter_expr=filter_expr,
                                               select_fields=select_fields)
                try:
                    parsed = json.loads(odata_result)
                    parsed["method"] = "odata"
                    parsed["service"] = service
                    parsed["entity_set"] = entity_set
                    parsed["filter"] = filter_expr
                    parsed["reasoning"] = reasoning
                    parsed["question"] = question
                    return json.dumps(parsed, indent=2)
                except json.JSONDecodeError:
                    return json.dumps({
                        "method": "odata", "question": question,
                        "reasoning": reasoning, "raw_result": odata_result[:3000]
                    }, indent=2)

        # SQL path (either chosen or fallback)
        sql = decision.get("sql", "")
        if not sql:
            # Generate SQL as fallback
            sql = f"SELECT * FROM EKKO"  # shouldn't happen

        # Clean up SQL
        if sql.startswith("```"):
            sql = "\n".join(sql.split("\n")[1:])
        if sql.endswith("```"):
            sql = sql.rsplit("```", 1)[0]
        sql = sql.strip().rstrip(";")

        logger.info(f"smart_query SQL: {sql}")

        exec_result = call_adt_api(ctx, "/sap/bc/adt/datapreview/freestyle",
                                   method="POST", body=sql,
                                   content_type="text/plain; charset=utf-8",
                                   accept="application/vnd.sap.adt.datapreview.table.v1+xml",
                                   query_params=f"rowNumber={max_rows}")

        try:
            parsed = json.loads(exec_result)
            parsed["method"] = "sql"
            parsed["generated_sql"] = sql
            parsed["reasoning"] = reasoning
            parsed["question"] = question
            return json.dumps(parsed, indent=2)
        except json.JSONDecodeError:
            return json.dumps({
                "method": "sql", "question": question,
                "generated_sql": sql, "reasoning": reasoning,
                "raw_result": exec_result[:3000]
            }, indent=2)

    except Exception as e:
        logger.error(f"smart_query failed: {e}")
        return json.dumps({"error": str(e), "question": question})


# ── Create OData Service (CDS View) ──────────────────────────────────────────

@mcp.tool()
def create_odata_service(ctx: Context, description: str, cds_name: str = "") -> str:
    """Create a new OData service in SAP by generating and deploying a CDS view.
    Uses Bedrock Claude to generate the CDS source from a natural language description,
    then creates it via ADT with @OData.publish: true for auto-exposure.

    The CDS view is created in $TMP (local objects). After creation, the service needs
    to be activated in /IWFND/MAINT_SERVICE to be accessible via OData.

    Args:
        description: What data the service should expose (e.g. 'Purchase orders with vendor info and item details')
        cds_name: Optional CDS view name (auto-generated if empty, must start with Z)
    """
    token = _get_token(ctx)
    if not token:
        return json.dumps({"error": "No bearer token in request"})

    steps = []

    try:
        # Step 1: Generate CDS source with Claude
        bedrock = boto3.client("bedrock-runtime",
                               region_name=boto3.session.Session().region_name)

        system_prompt = (
            "You are an SAP ABAP CDS expert. Generate a CDS view entity source based on the user's description.\n\n"
            "RULES:\n"
            "- Output ONLY the CDS source code, nothing else. No explanation, no markdown.\n"
            "- Use 'define root view entity' syntax (not 'define view')\n"
            "- Include @OData.publish: true annotation for auto OData exposure\n"
            "- Include proper annotations: @AbapCatalog.viewEnhancementCategory, @AccessControl, @EndUserText, @ObjectModel\n"
            "- Use INNER JOIN for related tables (not LEFT JOIN)\n"
            "- Add @Semantics.amount.currencyCode for amount fields\n"
            "- Add @Semantics.quantity.unitOfMeasure for quantity fields\n"
            "- Use meaningful alias names (PascalCase)\n"
            "- Always include key fields\n\n"
            f"{_SAP_TABLE_REFERENCE}\n\n"
            "EXAMPLE:\n"
            "@AbapCatalog.viewEnhancementCategory: [#NONE]\n"
            "@AccessControl.authorizationCheck: #NOT_REQUIRED\n"
            "@EndUserText.label: 'My Service Description'\n"
            "@Metadata.ignorePropagatedAnnotations: true\n"
            "@ObjectModel.usageType:{ serviceQuality: #X, sizeCategory: #L, dataClass: #MIXED }\n"
            "@OData.publish: true\n"
            "define root view entity ZI_MY_VIEW\n"
            "  as select from ekko as Header\n"
            "  inner join ekpo as Item on Header.ebeln = Item.ebeln\n"
            "{\n"
            "  key Header.ebeln as DocumentNumber,\n"
            "  key Item.ebelp   as ItemNumber,\n"
            "      Header.lifnr as Vendor,\n"
            "      Header.waers as Currency,\n"
            "      @Semantics.amount.currencyCode: 'Currency'\n"
            "      Item.netwr    as NetValue\n"
            "}\n"
        )

        # Auto-generate name if not provided
        if not cds_name:
            # Extract keywords from description for naming
            words = [w.strip(",.;:!?").lower() for w in description.split()
                     if len(w.strip(",.;:!?")) > 3 and w.strip(",.;:!?") not in
                     {"with", "that", "this", "from", "show", "list", "details", "including", "their"}]
            name_part = "_".join(words[:3])[:20]
            cds_name = f"zi_{name_part}"

        cds_name = cds_name.lower().strip()
        if not cds_name.startswith("z"):
            cds_name = f"z{cds_name}"

        user_msg = f"Create a CDS view named {cds_name.upper()} that exposes: {description}"

        response = bedrock.invoke_model(
            modelId="us.anthropic.claude-sonnet-4-20250514-v1:0",
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 2048,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_msg}]
            }),
            contentType="application/json",
            accept="application/json"
        )
        result = json.loads(response["body"].read())
        cds_source = result["content"][0]["text"].strip()

        # Clean up markdown fencing
        if cds_source.startswith("```"):
            cds_source = "\n".join(cds_source.split("\n")[1:])
        if cds_source.endswith("```"):
            cds_source = cds_source.rsplit("```", 1)[0]
        cds_source = cds_source.strip()

        steps.append("CDS source generated")
        logger.info(f"Generated CDS for {cds_name}: {len(cds_source)} chars")

        # Step 2: Create CDS view via ADT (stateful session)
        with httpx.Client(verify=False, timeout=120) as c:
            base = {"Authorization": f"Bearer {token}", "X-sap-adt-sessiontype": "stateful"}

            # Fetch CSRF
            csrf_resp = c.get(f"{SAP_BASE_URL}/sap/bc/adt/discovery",
                              headers={**base, "x-csrf-token": "Fetch", "Accept": "application/atomsvc+xml"})
            csrf = csrf_resp.headers.get("x-csrf-token", "")
            if not csrf:
                return json.dumps({"error": "Could not fetch CSRF token"})
            steps.append("CSRF token fetched")

            ddl_path = f"/sap/bc/adt/ddic/ddl/sources"

            # Check if exists
            check = c.get(f"{SAP_BASE_URL}{ddl_path}/{cds_name}/source/main",
                          headers={**base, "Accept": "text/plain"})
            if check.status_code == 200:
                steps.append("CDS view already exists, will overwrite")
            else:
                # Create
                create_xml = (
                    f'<?xml version="1.0" encoding="UTF-8"?>'
                    f'<ddl:ddlSource xmlns:ddl="http://www.sap.com/adt/ddic/ddlsources" '
                    f'xmlns:adtcore="http://www.sap.com/adt/core" '
                    f'adtcore:type="DDLS/DF" adtcore:description="{description[:60]}" '
                    f'adtcore:language="EN" adtcore:name="{cds_name.upper()}" '
                    f'adtcore:masterLanguage="EN" adtcore:responsible="DEVELOPER">'
                    f'<adtcore:packageRef adtcore:name="$TMP"/>'
                    f'</ddl:ddlSource>'
                )
                cr = c.post(f"{SAP_BASE_URL}{ddl_path}",
                            headers={**base, "x-csrf-token": csrf,
                                     "Content-Type": "application/vnd.sap.adt.ddlsource+xml",
                                     "Accept": "*/*"},
                            content=create_xml)
                if cr.status_code >= 400 and cr.status_code != 409:
                    return json.dumps({"error": f"Create failed: {cr.status_code}",
                                       "details": cr.text[:500], "steps": steps})
                steps.append(f"CDS view created ({cr.status_code})")

            # Lock
            lock_resp = c.post(f"{SAP_BASE_URL}{ddl_path}/{cds_name}",
                               headers={**base, "x-csrf-token": csrf,
                                        "Accept": "application/vnd.sap.as+xml;charset=UTF-8;dataname=com.sap.adt.lock.result"},
                               params={"_action": "LOCK", "accessMode": "MODIFY"})
            lock_handle = ""
            try:
                lock_root = ET.fromstring(lock_resp.text)
                for el in lock_root.iter():
                    tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
                    if tag == "LOCK_HANDLE" and el.text:
                        lock_handle = el.text
                        break
            except ET.ParseError:
                pass
            steps.append(f"Locked (handle={'found' if lock_handle else 'empty'})")

            # Write source
            write_resp = c.put(f"{SAP_BASE_URL}{ddl_path}/{cds_name}/source/main",
                               headers={**base, "x-csrf-token": csrf,
                                        "Content-Type": "text/plain; charset=utf-8"},
                               params={"lockHandle": lock_handle},
                               content=cds_source.encode("utf-8"))
            if write_resp.status_code >= 400:
                c.post(f"{SAP_BASE_URL}{ddl_path}/{cds_name}",
                       headers={**base, "x-csrf-token": csrf},
                       params={"_action": "UNLOCK", "lockHandle": lock_handle})
                return json.dumps({"error": f"Write failed: {write_resp.status_code}",
                                   "details": write_resp.text[:500], "steps": steps,
                                   "cds_source": cds_source})
            steps.append("Source written")

            # Unlock
            c.post(f"{SAP_BASE_URL}{ddl_path}/{cds_name}",
                   headers={**base, "x-csrf-token": csrf},
                   params={"_action": "UNLOCK", "lockHandle": lock_handle})
            steps.append("Unlocked")

            # Activate
            activate_body = (
                f'<?xml version="1.0" encoding="UTF-8"?>'
                f'<adtcore:objectReferences xmlns:adtcore="http://www.sap.com/adt/core">'
                f'<adtcore:objectReference adtcore:uri="{ddl_path}/{cds_name}" adtcore:name="{cds_name.upper()}"/>'
                f'</adtcore:objectReferences>'
            )
            act_resp = c.post(f"{SAP_BASE_URL}/sap/bc/adt/activation",
                              headers={**base, "x-csrf-token": csrf,
                                       "Content-Type": "application/xml", "Accept": "application/xml"},
                              params={"method": "activate", "preauditRequested": "true"},
                              content=activate_body)
            steps.append(f"Activated ({act_resp.status_code})")

        svc_name = f"{cds_name.upper()}_CDS"
        return json.dumps({
            "status": "success",
            "cds_view": cds_name.upper(),
            "odata_service_name": svc_name,
            "steps": steps,
            "cds_source": cds_source,
            "message": (
                f"CDS view {cds_name.upper()} created and activated in SAP. "
                f"The @OData.publish annotation auto-generates backend service '{svc_name}'. "
                f"To make it accessible via OData, activate it in /IWFND/MAINT_SERVICE "
                f"or use activate_odata_service('{svc_name}')."
            ),
            "next_steps": [
                f"1. Activate service: activate_odata_service('{svc_name}')",
                f"2. Or manually via tcode /IWFND/MAINT_SERVICE → Add Service → search '{svc_name}'",
                f"3. Once activated, query via: /sap/opu/odata/sap/{svc_name}/"
            ]
        }, indent=2)

    except Exception as e:
        logger.error(f"create_odata_service failed: {e}")
        return json.dumps({"error": str(e), "steps": steps})


# ── OData Service Activation ──────────────────────────────────────────────────

@mcp.tool()
def activate_odata_service(ctx: Context, service_name: str, service_version: str = "0001",
                           system_alias: str = "LOCAL") -> str:
    """Activate an OData service in SAP Gateway (equivalent to /IWFND/MAINT_SERVICE).
    Finds the service in the backend catalog and registers it in the frontend hub.
    Uses SQL queries on TADIR to verify service status, then tries the catalog API.

    Args:
        service_name: Technical service name (e.g. 'API_SALES_ORDER_SRV', 'MM_PUR_POITEMS_MONI_SRV')
        service_version: Service version (default '0001')
        system_alias: System alias for the backend (default 'LOCAL' for embedded deployment)
    """
    token = _get_token(ctx)
    if not token:
        return json.dumps({"error": "No bearer token in request"})

    svc = service_name.strip().upper()
    steps = []

    # Step 1: Check if already activated (try to GET $metadata)
    try:
        check_url = f"{SAP_BASE_URL}/sap/opu/odata/sap/{svc}/$metadata"
        with httpx.Client(verify=False, timeout=15) as c:
            r = c.get(check_url, headers={"Authorization": f"Bearer {token}"})
            if r.status_code == 200:
                return json.dumps({
                    "status": "already_active",
                    "service": svc,
                    "message": f"Service {svc} is already activated and accessible."
                }, indent=2)
        steps.append(f"Not yet active (HTTP {r.status_code})")
    except Exception as e:
        steps.append(f"Metadata check failed: {e}")

    # Step 2: Verify service exists in backend via SQL on TADIR (IWSV = backend service)
    backend_check = call_adt_api(ctx, "/sap/bc/adt/datapreview/freestyle",
                                 method="POST",
                                 body=f"SELECT PGMID, OBJECT, OBJ_NAME, DEVCLASS FROM TADIR WHERE OBJECT = 'IWSV' AND OBJ_NAME LIKE '{svc}%'",
                                 content_type="text/plain; charset=utf-8",
                                 accept="application/vnd.sap.adt.datapreview.table.v1+xml",
                                 query_params="rowNumber=5")
    try:
        backend_data = json.loads(backend_check)
        backend_results = backend_data.get("results", [])
        if not backend_results:
            return json.dumps({
                "status": "not_found",
                "service": svc,
                "message": f"Service {svc} not found in backend (TADIR IWSV). It may not be installed on this system.",
                "hint": "Use list_backend_services to search for available services."
            }, indent=2)
        steps.append(f"Found in backend: {backend_results[0].get('OBJ_NAME', '').strip()}")
    except Exception:
        steps.append("Backend check via SQL inconclusive")

    # Step 3: Check if already registered in frontend via SQL (IWSG = frontend service group)
    frontend_check = call_adt_api(ctx, "/sap/bc/adt/datapreview/freestyle",
                                  method="POST",
                                  body=f"SELECT PGMID, OBJECT, OBJ_NAME, DEVCLASS FROM TADIR WHERE OBJECT = 'IWSG' AND OBJ_NAME LIKE '{svc}%'",
                                  content_type="text/plain; charset=utf-8",
                                  accept="application/vnd.sap.adt.datapreview.table.v1+xml",
                                  query_params="rowNumber=5")
    try:
        frontend_data = json.loads(frontend_check)
        frontend_results = frontend_data.get("results", [])
        if frontend_results:
            steps.append(f"Already registered in frontend (IWSG): {frontend_results[0].get('OBJ_NAME', '').strip()}")
        else:
            steps.append("Not yet registered in frontend (no IWSG entry)")
    except Exception:
        steps.append("Frontend check via SQL inconclusive")

    # Step 4: Try catalog API activation (POST AddService)
    try:
        with httpx.Client(verify=False, timeout=60) as c:
            base = {"Authorization": f"Bearer {token}"}

            # Fetch CSRF from catalog service
            csrf_resp = c.get(f"{SAP_BASE_URL}/sap/opu/odata/IWFND/CATALOGSERVICE;v=2/",
                              headers={**base, "x-csrf-token": "Fetch"})
            csrf = csrf_resp.headers.get("x-csrf-token", "")

            if csrf:
                add_url = f"{SAP_BASE_URL}/sap/opu/odata/IWFND/CATALOGSERVICE;v=2/AddService"
                payload = {
                    "TechnicalServiceName": svc,
                    "TechnicalServiceVersion": int(service_version),
                    "SystemAlias": system_alias,
                    "ExternalServiceName": svc,
                }
                r = c.post(add_url,
                           headers={**base, "x-csrf-token": csrf,
                                    "Content-Type": "application/json",
                                    "Accept": "application/json"},
                           content=json.dumps(payload))

                if r.status_code in (200, 201, 204):
                    return json.dumps({
                        "status": "activated",
                        "service": svc,
                        "method": "catalog_api",
                        "steps": steps,
                        "message": f"Service {svc} activated via Gateway catalog API."
                    }, indent=2)
                else:
                    steps.append(f"AddService API returned HTTP {r.status_code}: {r.text[:200]}")
            else:
                steps.append("Could not get CSRF token from catalog service")

    except Exception as e:
        steps.append(f"Catalog API failed: {e}")

    # Step 5: Return diagnostic info with manual activation steps
    return json.dumps({
        "status": "needs_manual_activation",
        "service": svc,
        "steps": steps,
        "message": (
            f"Service {svc} exists in backend but could not be auto-activated. "
            f"This typically requires SAP_ALL or specific Gateway admin authorization."
        ),
        "manual_steps": [
            f"1. Go to tcode /IWFND/MAINT_SERVICE",
            f"2. Click 'Add Service'",
            f"3. System Alias: {system_alias}",
            f"4. Search for: {svc}",
            f"5. Select and activate"
        ]
    }, indent=2)


@mcp.tool()
def list_backend_services(ctx: Context, search: str = "", max_results: int = 20) -> str:
    """List OData services available in the SAP backend catalog (registered but possibly not activated).
    Use this to find services that can be activated via activate_odata_service.

    Args:
        search: Search term to filter services (e.g. 'SALES', 'MATERIAL', 'PURCHASE')
        max_results: Maximum results (default 20)
    """
    token = _get_token(ctx)
    if not token:
        return json.dumps({"error": "No bearer token in request"})

    try:
        # Query backend catalog (IWBEP)
        params = {"$format": "json", "$top": str(max_results)}
        if search:
            params["$filter"] = f"substringof('{search.upper()}',TechnicalServiceName)"

        url = f"{SAP_BASE_URL}/sap/opu/odata/IWBEP/CATALOGSERVICE;v=2/ServiceCollection"
        with httpx.Client(verify=False, timeout=30) as c:
            r = c.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                      params=params)
            r.raise_for_status()
            data = r.json()

        results = data.get("d", {}).get("results", [])
        services = []
        for svc in results:
            services.append({
                "ID": svc.get("ID", ""),
                "TechnicalServiceName": svc.get("TechnicalServiceName", ""),
                "Description": svc.get("Description", ""),
                "ServiceType": svc.get("ServiceType", ""),
                "IsSapService": svc.get("IsSapService", False),
            })

        # Also check which are already in frontend catalog
        frontend_services = set(_catalog_cache.keys()) if _catalog_cache else set()
        for svc in services:
            svc["activated"] = svc["TechnicalServiceName"] in frontend_services

        return json.dumps({
            "search": search,
            "count": len(services),
            "services": services,
            "hint": "Use activate_odata_service to activate any service that shows activated=false"
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Convenience ADT Tools ─────────────────────────────────────────────────────
# These wrap call_adt_api with the correct ADT paths so the LLM doesn't need to
# know the REST endpoints. Each is a thin wrapper that delegates to the generic
# ADT client but provides a discoverable, named tool.

# ── Object Readers ────────────────────────────────────────────────────────────

@mcp.tool()
def get_abap_program(ctx: Context, program_name: str) -> str:
    """Read ABAP program/report source code from SAP.
    Args:
        program_name: Program name (e.g. 'Z_INVOICE_3WAY_MATCH', 'SAPMV45A')
    """
    prog = program_name.strip().lower()
    return call_adt_api(ctx, f"/sap/bc/adt/programs/programs/{prog}/source/main",
                        accept="text/plain")


@mcp.tool()
def get_abap_class(ctx: Context, class_name: str, include: str = "main") -> str:
    """Read ABAP class source code from SAP.
    Args:
        class_name: Class name (e.g. 'ZCL_MY_CLASS', 'CL_ABAP_TYPEDESCR')
        include: Which part to read — 'main' (definition+implementation), 'definitions' (local types),
                 'implementations' (local implementations), 'testclasses'. Default 'main'.
    """
    cls = class_name.strip().lower()
    valid = {"main", "definitions", "implementations", "testclasses"}
    inc = include.strip().lower() if include.strip().lower() in valid else "main"
    path = f"/sap/bc/adt/oo/classes/{cls}/source/{inc}"
    return call_adt_api(ctx, path, accept="text/plain")


@mcp.tool()
def get_function_module(ctx: Context, function_group: str, function_name: str) -> str:
    """Read function module source code from SAP.
    Args:
        function_group: Function group name (e.g. 'ZFGRP', 'SMOD')
        function_name: Function module name (e.g. 'Z_MY_FUNC', 'BAPI_MATERIAL_GET_DETAIL')
    """
    fg = function_group.strip().lower()
    fn = function_name.strip().lower()
    return call_adt_api(ctx, f"/sap/bc/adt/functions/groups/{fg}/fmodules/{fn}/source/main",
                        accept="text/plain")


@mcp.tool()
def get_abap_interface(ctx: Context, interface_name: str) -> str:
    """Read ABAP interface source code from SAP.
    Args:
        interface_name: Interface name (e.g. 'ZIF_MY_INTERFACE', 'IF_HTTP_CLIENT')
    """
    intf = interface_name.strip().lower()
    return call_adt_api(ctx, f"/sap/bc/adt/oo/interfaces/{intf}/source/main",
                        accept="text/plain")


@mcp.tool()
def get_abap_include(ctx: Context, include_name: str) -> str:
    """Read ABAP include program source code from SAP.
    Args:
        include_name: Include name (e.g. 'MV45AF0B_BELEG_SICHERN')
    """
    inc = include_name.strip().lower()
    return call_adt_api(ctx, f"/sap/bc/adt/programs/includes/{inc}/source/main",
                        accept="text/plain")


@mcp.tool()
def search_objects(ctx: Context, query: str, object_type: str = "",
                   max_results: int = 50) -> str:
    """Search SAP repository objects (programs, classes, tables, function modules, etc.).
    Args:
        query: Search term with wildcards (e.g. 'Z_INVOICE*', 'CL_ABAP*', '*MATERIAL*')
        object_type: Optional filter — PROG, CLAS, INTF, FUGR, FUNC, TABL, DTEL, DOMA, TRAN, DEVC, etc.
        max_results: Max results (default 50)
    """
    qp = f"operation=quickSearch&query={query}&maxResults={max_results}"
    if object_type:
        qp += f"&objectType={object_type.strip().upper()}"
    return call_adt_api(ctx, "/sap/bc/adt/repository/informationsystem/search",
                        query_params=qp)


@mcp.tool()
def get_package(ctx: Context, package_name: str) -> str:
    """Get SAP package (development class) information and contents.
    Args:
        package_name: Package name (e.g. '$TMP', 'ZPACKAGE', 'SAPBC')
    """
    pkg = package_name.strip().upper()
    return call_adt_api(ctx, f"/sap/bc/adt/packages/{pkg}",
                        accept="application/xml")


@mcp.tool()
def get_transaction(ctx: Context, tcode: str) -> str:
    """Look up a SAP transaction code and find the associated program/object.
    Args:
        tcode: Transaction code (e.g. 'VA01', 'MM01', 'SE38')
    """
    return call_adt_api(ctx, "/sap/bc/adt/repository/informationsystem/search",
                        query_params=f"operation=quickSearch&query={tcode.strip().upper()}&objectType=TRAN&maxResults=5")


# ── DDIC Tools ────────────────────────────────────────────────────────────────

@mcp.tool()
def get_table_definition(ctx: Context, table_name: str) -> str:
    """Get DDIC table or structure definition (fields, types, keys) from SAP.
    Args:
        table_name: Table or structure name (e.g. 'MARA', 'VBAK', 'BSEG', 'EKKO')
    """
    tbl = table_name.strip().upper()
    # Try table first, fall back to structure
    result = call_adt_api(ctx, f"/sap/bc/adt/ddic/tables/{tbl.lower()}",
                          accept="application/xml")
    try:
        parsed = json.loads(result)
        if "error" in parsed and "404" in str(parsed.get("error", "")):
            return call_adt_api(ctx, f"/sap/bc/adt/ddic/structures/{tbl.lower()}",
                                accept="application/xml")
    except json.JSONDecodeError:
        pass
    return result


@mcp.tool()
def get_table_contents(ctx: Context, sql_query: str, max_rows: int = 100) -> str:
    """Execute a SQL query on SAP database tables via ADT Data Preview.
    Args:
        sql_query: SQL SELECT statement (e.g. "SELECT * FROM MARA WHERE MATNR = 'TG14'" or
                   "SELECT VBELN, ERDAT, ERNAM FROM VBAK WHERE ERDAT > '20240101'")
        max_rows: Maximum rows to return (default 100)
    """
    body = sql_query.strip()
    if not body.upper().startswith("SELECT"):
        body = f"SELECT * FROM {body}"
    return call_adt_api(ctx, "/sap/bc/adt/datapreview/freestyle",
                        method="POST", body=body,
                        content_type="text/plain; charset=utf-8",
                        accept="application/vnd.sap.adt.datapreview.table.v1+xml",
                        query_params=f"rowNumber={max_rows}")


@mcp.tool()
def get_type_info(ctx: Context, type_name: str) -> str:
    """Get DDIC type information (data element, domain, or type details).
    Args:
        type_name: Type name (e.g. 'MATNR', 'BUKRS', 'VBELN')
    """
    return call_adt_api(ctx, f"/sap/bc/adt/ddic/dataelements/{type_name.strip().lower()}",
                        accept="application/xml")


# ── Transport Management ─────────────────────────────────────────────────────

@mcp.tool()
def create_transport(ctx: Context, description: str, target_system: str = "",
                     transport_type: str = "K") -> str:
    """Create a new transport request in SAP.
    Args:
        description: Transport description (e.g. 'Z_INVOICE_3WAY_MATCH development')
        target_system: Target system SID (e.g. 'QAS'). Leave empty for local ($TMP) objects.
        transport_type: 'K' for workbench, 'W' for customizing (default 'K')
    """
    body = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<tm:root xmlns:tm="http://www.sap.com/cts/adt/tm" tm:useraction="newrequest">'
        f'<tm:request tm:desc="{description}" tm:type="{transport_type}" '
        f'tm:target="{target_system}" tm:cts_project=""/>'
        f'</tm:root>'
    )
    return call_adt_api(ctx, "/sap/bc/adt/cts/transportrequests",
                        method="POST", body=body,
                        content_type="application/vnd.sap.as+xml; charset=UTF-8; dataname=com.sap.adt.cts.model.workbench.request",
                        accept="application/vnd.sap.as+xml; charset=UTF-8; dataname=com.sap.adt.cts.model.workbench.request")


@mcp.tool()
def release_transport(ctx: Context, transport_number: str) -> str:
    """Release a transport request or task in SAP.
    Args:
        transport_number: Transport number (e.g. 'DEVK900123')
    """
    tr = transport_number.strip().upper()
    return call_adt_api(ctx, f"/sap/bc/adt/cts/transportrequests/{tr}/newreleasejobs",
                        method="POST",
                        content_type="application/xml")


@mcp.tool()
def list_user_transports(ctx: Context, user: str = "", status: str = "D") -> str:
    """List transport requests for a user.
    Args:
        user: SAP username (leave empty for current user)
        status: 'D' for modifiable, 'R' for released, 'N' for not released (default 'D')
    """
    qp = f"user={user.strip().upper()}&status={status}" if user else f"status={status}"
    return call_adt_api(ctx, "/sap/bc/adt/cts/transportrequests",
                        query_params=qp,
                        accept="application/xml")


# ── Code Quality ──────────────────────────────────────────────────────────────

@mcp.tool()
def syntax_check(ctx: Context, object_url: str, source_code: str = "") -> str:
    """Run ABAP syntax check on a program or class.
    Args:
        object_url: ADT object URI (e.g. '/sap/bc/adt/programs/programs/z_my_program')
        source_code: Optional source code to check. If empty, checks the saved version.
    """
    body = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<chkrun:checkRunReports xmlns:chkrun="http://www.sap.com/adt/checkruns" '
        f'xmlns:adtcore="http://www.sap.com/adt/core">'
        f'<chkrun:checkReport>'
        f'<adtcore:objectReference adtcore:uri="{object_url}"/>'
        f'</chkrun:checkReport>'
        f'</chkrun:checkRunReports>'
    )
    return call_adt_api(ctx, "/sap/bc/adt/checkruns",
                        method="POST", body=body,
                        content_type="application/vnd.sap.adt.checkruns+xml",
                        accept="application/vnd.sap.adt.checkruns+xml")


@mcp.tool()
def run_atc_check(ctx: Context, object_uri: str, check_variant: str = "DEFAULT") -> str:
    """Run ATC (ABAP Test Cockpit) code quality check on an object.
    Args:
        object_uri: ADT object URI (e.g. '/sap/bc/adt/programs/programs/z_my_program')
        check_variant: ATC check variant (default 'DEFAULT')
    """
    run_body = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<atc:run xmlns:atc="http://www.sap.com/adt/atc">'
        f'<atc:objectSets>'
        f'<atc:objectSet kind="inclusive">'
        f'<atc:adtObjectRefs>'
        f'<atc:adtObjRef adtcore:uri="{object_uri}" '
        f'xmlns:adtcore="http://www.sap.com/adt/core"/>'
        f'</atc:adtObjectRefs>'
        f'</atc:objectSet>'
        f'</atc:objectSets>'
        f'</atc:run>'
    )
    return call_adt_api(ctx, "/sap/bc/adt/atc/runs",
                        method="POST", body=run_body,
                        query_params=f"checkVariant={check_variant}",
                        content_type="application/xml",
                        accept="application/xml")


# ── ADT Discovery / Help ─────────────────────────────────────────────────────

@mcp.tool()
def adt_discovery(ctx: Context) -> str:
    """Get the ADT discovery document — lists all available ADT REST API endpoints and their
    capabilities on this SAP system. Useful for finding what ADT operations are supported.
    Returns a structured summary of available ADT services, their URLs, and supported content types.
    """
    token = _get_token(ctx)
    if not token:
        return json.dumps({"error": "No bearer token in request"})
    try:
        url = f"{SAP_BASE_URL}/sap/bc/adt/discovery"
        with httpx.Client(verify=False, timeout=30.0) as c:
            r = c.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/xml"})
            r.raise_for_status()
            root = ET.fromstring(r.text)
            # Parse the Atom service document
            ns = {"app": "http://www.w3.org/2007/app", "atom": "http://www.w3.org/2005/Atom",
                  "adtcomp": "http://www.sap.com/adt/compatibility"}
            services = []
            for workspace in root.findall("app:workspace", ns):
                ws_title = ""
                title_el = workspace.find("atom:title", ns)
                if title_el is not None:
                    ws_title = title_el.text or ""
                for collection in workspace.findall("app:collection", ns):
                    href = collection.get("href", "")
                    col_title = ""
                    ct = collection.find("atom:title", ns)
                    if ct is not None:
                        col_title = ct.text or ""
                    # Get accepted content types
                    accepts = [a.text for a in collection.findall("app:accept", ns) if a.text]
                    services.append({
                        "workspace": ws_title,
                        "title": col_title,
                        "href": href,
                        "accepts": accepts[:3]  # limit for readability
                    })
            return json.dumps({
                "adt_base": f"{SAP_BASE_URL}/sap/bc/adt",
                "total_endpoints": len(services),
                "services": services,
                "hint": "Use call_adt_api with the href as adt_path to call any of these endpoints"
            }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


if __name__ == "__main__":
    logger.info("=== SAP Smart MCP Server Starting ===")
    # Start background metadata load
    Thread(target=_load_all_metadata, daemon=True).start()
    mcp.run(transport="streamable-http")

# Force rebuild: 20260322012500
