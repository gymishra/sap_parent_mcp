"""
SAP Cache MCP Server — In-memory metadata cache.
On startup, fetches the full SAP service catalog + entity metadata from SAP OData,
holds it all in memory. No SQLite, no local files.
"""
import os, sys, json, asyncio, logging, time, xml.etree.ElementTree as ET
from threading import Thread

# Add parent for shared auth
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sap-smart-agent"))

from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sap_cache")

# ── Config ────────────────────────────────────────────────────────────────────
SAP_BASE_URL = os.environ.get("SAP_BASE_URL", "https://vhcals4hci.awspoc.club")
CATALOG_URL = "/sap/opu/odata/IWFND/CATALOGSERVICE;v=2/ServiceCollection"

# ── In-memory stores ─────────────────────────────────────────────────────────
# services: { "API_SALES_ORDER_SRV": { title, technical_name, description, version } }
_services: dict = {}
# entities: { "API_SALES_ORDER_SRV": [ { entity_name, properties: [...], nav_properties: [...] } ] }
_entities: dict = {}
# Flat index for field search: [ { service, entity_name, property_name, property_type } ]
_field_index: list = []

_loaded = False
_load_error = ""

server = Server("sap-cache")


# ── SAP HTTP helpers ──────────────────────────────────────────────────────────
def _get_token():
    """Get a valid Okta token for SAP. Uses cached token if still valid."""
    try:
        from kiro_bridge import get_okta_token
        return get_okta_token()
    except Exception as e:
        logger.error(f"Failed to get Okta token: {e}")
        return os.environ.get("SAP_BEARER_TOKEN", "")


def _sap_get_json(path, token, params=None):
    import httpx
    url = f"{SAP_BASE_URL}{path}"
    with httpx.Client(verify=False, timeout=60) as c:
        r = c.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                  params=params or {})
        r.raise_for_status()
        return r.json()


def _sap_get_xml(path, token):
    import httpx
    url = f"{SAP_BASE_URL}{path}"
    with httpx.Client(verify=False, timeout=30) as c:
        r = c.get(url, headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return r.text


# ── Metadata parser ───────────────────────────────────────────────────────────
def _parse_metadata_xml(xml_text):
    """Parse OData $metadata XML into list of entity dicts with properties and nav properties."""
    root = ET.fromstring(xml_text)
    entities = []

    for ns_uri in ["http://schemas.microsoft.com/ado/2008/09/edm",
                   "http://schemas.microsoft.com/ado/2009/11/edm"]:
        # Build association map
        assoc_map = {}
        for assoc in root.iter(f"{{{ns_uri}}}Association"):
            aname = assoc.get("Name", "")
            ends = assoc.findall(f"{{{ns_uri}}}End")
            if len(ends) == 2:
                assoc_map[aname] = {
                    ends[0].get("Role", ""): {"type": ends[0].get("Type", ""), "mult": ends[0].get("Multiplicity", "")},
                    ends[1].get("Role", ""): {"type": ends[1].get("Type", ""), "mult": ends[1].get("Multiplicity", "")},
                }

        # Build entity set map (EntitySet Name -> EntityType Name)
        entity_set_map = {}
        for ec in root.iter(f"{{{ns_uri}}}EntityContainer"):
            for es in ec.findall(f"{{{ns_uri}}}EntitySet"):
                es_name = es.get("Name", "")
                es_type = es.get("EntityType", "").split(".")[-1]
                entity_set_map[es_type] = es_name

        for et in root.iter(f"{{{ns_uri}}}EntityType"):
            ename = et.get("Name", "")
            props = []
            for p in et.findall(f"{{{ns_uri}}}Property"):
                props.append({
                    "name": p.get("Name", ""),
                    "type": p.get("Type", ""),
                    "nullable": p.get("Nullable", "true")
                })

            nav_props = []
            for nav in et.findall(f"{{{ns_uri}}}NavigationProperty"):
                nav_name = nav.get("Name", "")
                to_role = nav.get("ToRole", "")
                rel = nav.get("Relationship", "").split(".")[-1]
                target = ""
                mult = ""
                if rel in assoc_map and to_role in assoc_map[rel]:
                    target = assoc_map[rel][to_role]["type"].split(".")[-1]
                    mult = assoc_map[rel][to_role]["mult"]
                nav_props.append({"name": nav_name, "target_entity": target, "multiplicity": mult})

            # Key fields
            key_el = et.find(f"{{{ns_uri}}}Key")
            keys = []
            if key_el is not None:
                for kr in key_el.findall(f"{{{ns_uri}}}PropertyRef"):
                    keys.append(kr.get("Name", ""))

            entities.append({
                "entity_type": ename,
                "entity_set": entity_set_map.get(ename, ""),
                "keys": keys,
                "properties": props,
                "nav_properties": nav_props
            })

    return entities


# ── Startup loader ────────────────────────────────────────────────────────────
def _load_all():
    """Fetch full catalog + metadata for all services on startup."""
    global _services, _entities, _field_index, _loaded, _load_error

    logger.info("Starting full SAP metadata load...")
    token = _get_token()
    if not token:
        _load_error = "No SAP token available"
        logger.error(_load_error)
        return

    # 1. Fetch service catalog
    try:
        data = _sap_get_json(CATALOG_URL, token, {"$format": "json", "$top": "5000"})
        svc_list = data.get("d", {}).get("results", [])
        for svc in svc_list:
            title = svc.get("Title", "")
            _services[title] = {
                "title": title,
                "technical_name": svc.get("TechnicalServiceName", ""),
                "description": svc.get("Description", ""),
                "version": svc.get("TechnicalServiceVersion", 1),
                "service_url": svc.get("ServiceUrl", "")
            }
        logger.info(f"Catalog loaded: {len(_services)} services")
    except Exception as e:
        _load_error = f"Catalog fetch failed: {e}"
        logger.error(_load_error)
        return

    # 2. Fetch metadata for each service
    total = len(_services)
    success = 0
    skipped = 0
    for i, (title, svc) in enumerate(_services.items()):
        try:
            xml_text = _sap_get_xml(f"/sap/opu/odata/sap/{title}/$metadata", token)
            entities = _parse_metadata_xml(xml_text)
            _entities[title] = entities

            # Build field index
            for ent in entities:
                for prop in ent["properties"]:
                    _field_index.append({
                        "service": title,
                        "entity_name": ent["entity_type"],
                        "entity_set": ent.get("entity_set", ""),
                        "property_name": prop["name"],
                        "property_type": prop["type"]
                    })
            success += 1
        except Exception:
            skipped += 1

        if (i + 1) % 50 == 0:
            logger.info(f"  Metadata progress: {i+1}/{total} (ok={success}, skip={skipped})")

    _loaded = True
    logger.info(f"Metadata load complete: {success} services loaded, {skipped} skipped, "
                f"{len(_field_index)} fields indexed")


# ── MCP Tools ─────────────────────────────────────────────────────────────────
@server.list_tools()
async def list_tools():
    return [
        types.Tool(name="search_sap_services",
            description="Search SAP OData services by keyword. Returns matching service names and descriptions. Use this INSTEAD of discover_sap_services.",
            inputSchema={"type": "object", "properties": {
                "keyword": {"type": "string", "description": "Search term (e.g. 'sales', 'material', 'purchase')"},
                "limit": {"type": "integer", "default": 20}
            }, "required": ["keyword"]}),

        types.Tool(name="get_service_entities",
            description="Get entity types for a specific SAP OData service from cache. Returns entity names, their entity set names (for OData queries), key fields, and navigation properties.",
            inputSchema={"type": "object", "properties": {
                "service": {"type": "string", "description": "Service title (e.g. API_SALES_ORDER_SRV)"}
            }, "required": ["service"]}),

        types.Tool(name="get_entity_properties",
            description="Get properties/fields for a specific entity type from cache.",
            inputSchema={"type": "object", "properties": {
                "service": {"type": "string", "description": "Service title"},
                "entity": {"type": "string", "description": "Entity type name"}
            }, "required": ["service", "entity"]}),

        types.Tool(name="find_entity_by_field",
            description="Find which SAP services/entities contain a specific field name.",
            inputSchema={"type": "object", "properties": {
                "field_name": {"type": "string", "description": "Field name to search for (e.g. 'SalesOrder', 'Material')"},
                "limit": {"type": "integer", "default": 15}
            }, "required": ["field_name"]}),

        types.Tool(name="cache_stats",
            description="Show cache statistics - how many services, entities, and properties are cached.",
            inputSchema={"type": "object", "properties": {}}),
    ]


@server.call_tool()
async def call_tool(name, arguments):
    if not _loaded:
        msg = f"Cache not loaded yet. {_load_error}" if _load_error else "Cache is still loading, please wait..."
        return [types.TextContent(type="text", text=json.dumps({"error": msg}))]

    if name == "search_sap_services":
        kw = arguments["keyword"].lower()
        limit = arguments.get("limit", 20)
        results = []
        for title, svc in _services.items():
            if kw in title.lower() or kw in svc.get("description", "").lower():
                results.append({"title": title, "description": svc.get("description", "")})
                if len(results) >= limit:
                    break
        return [types.TextContent(type="text", text=json.dumps(results, indent=2))]

    if name == "get_service_entities":
        svc_name = arguments["service"]
        entities = _entities.get(svc_name, [])
        if not entities:
            return [types.TextContent(type="text", text=json.dumps([]))]
        # Return summary: entity_type, entity_set, keys, nav_property count
        summary = []
        for e in entities:
            summary.append({
                "entity_type": e["entity_type"],
                "entity_set": e.get("entity_set", ""),
                "keys": e.get("keys", []),
                "property_count": len(e["properties"]),
                "nav_properties": [{"name": n["name"], "target": n["target_entity"], "multiplicity": n["multiplicity"]}
                                   for n in e.get("nav_properties", [])]
            })
        return [types.TextContent(type="text", text=json.dumps(summary, indent=2))]

    if name == "get_entity_properties":
        svc_name = arguments["service"]
        entity_name = arguments["entity"]
        entities = _entities.get(svc_name, [])
        for e in entities:
            if e["entity_type"] == entity_name:
                return [types.TextContent(type="text", text=json.dumps({
                    "entity_type": e["entity_type"],
                    "entity_set": e.get("entity_set", ""),
                    "keys": e.get("keys", []),
                    "properties": e["properties"],
                    "nav_properties": e.get("nav_properties", [])
                }, indent=2))]
        return [types.TextContent(type="text", text=json.dumps({"error": f"Entity {entity_name} not found in {svc_name}"}))]

    if name == "find_entity_by_field":
        kw = arguments["field_name"].lower()
        limit = arguments.get("limit", 15)
        results = []
        for entry in _field_index:
            if kw in entry["property_name"].lower():
                results.append(entry)
                if len(results) >= limit:
                    break
        return [types.TextContent(type="text", text=json.dumps(results, indent=2))]

    if name == "cache_stats":
        total_entities = sum(len(v) for v in _entities.values())
        stats = {
            "status": "loaded" if _loaded else "loading",
            "services": len(_services),
            "services_with_metadata": len(_entities),
            "total_entities": total_entities,
            "total_fields_indexed": len(_field_index),
            "error": _load_error or None
        }
        return [types.TextContent(type="text", text=json.dumps(stats, indent=2))]

    return [types.TextContent(type="text", text=f"Unknown tool: {name}")]


# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    # Start metadata load in background thread so MCP server starts immediately
    loader = Thread(target=_load_all, daemon=True)
    loader.start()

    async with stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
