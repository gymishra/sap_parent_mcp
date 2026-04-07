# Hybrid Data Research Agent — Requirements

## Problem
Current AI Factory has separate OData and ADT tools that don't collaborate. Users get partial data from OData (limited fields, inactive services) with no fallback. Generated agents only use OData, missing the full picture.

## Requirements

### R1: Hybrid Data Research Tool
- Replace single-source `odata_agent_tool` with a hybrid research agent
- Agent tries OData first for structured business data
- Detects gaps: inactive services (403), missing fields, partial results
- Auto-generates SQL via ADT `/sap/bc/adt/datapreview/freestyle` for gaps
- Merges OData + SQL results into unified response
- Tracks "research history" — which APIs, SQL statements, tables, fields worked

### R2: Service Activation Detection
- When catalog shows a service but query returns 403 (`/IWFND/MED/170`)
- Inform user: "Service X exists but is not activated. Activate via /IWFND/MAINT_SERVICE"
- Fall back to SQL automatically while informing user
- Future: auto-activate via ADT API

### R3: Conversation-Driven Agent Generation
- After research phase, ask user: "Satisfied? Want to create a dedicated agent?"
- If yes, feed research history to `generator_agent_tool`
- History includes: OData services used, SQL statements, tables, field mappings
- Generator creates MCP server with hybrid tools (OData + SQL combined)
- Strands agent decides tool count and strategy based on complexity

### R4: Smart Tool Generation
- Generated tools use optimal data path per operation
- Some tools: pure OData (when service is active and complete)
- Some tools: pure SQL (when no OData service exists)
- Some tools: hybrid (OData for header, SQL for details/enrichment)
- Generator includes `_sap_get()` AND `_adt_sql()` helpers in template

## Example Flow
1. User: "Show me POs with invoice details"
2. Agent → OData: searches for PO service → finds `MM_PUR_PO_MAINT_V2_SRV` → gets PO headers
3. Agent → OData: searches for invoice service → not activated (403)
4. Agent → SQL fallback: `SELECT * FROM rseg WHERE ebeln = '4500000001'` → gets invoice items
5. Agent → presents combined result to user
6. Agent: "The invoice data came from SQL (no OData service active). Want me to create a CDS view and expose it as OData?"
7. User: "Yes"
8. Agent → calls `create_odata_service` → CDS view created with @OData.publish
9. Agent: "CDS view created. Please activate service `ZI_INVOICE_ITEMS_CDS` in /IWFND/MAINT_SERVICE → Add Service → LOCAL"
10. User: "Done, activated"
11. Agent → tests the new OData endpoint → confirms it works
12. Agent: "Service is live. Want me to create a dedicated MCP agent that uses this OData service?"
13. User: "Yes"
14. Agent → feeds research history (OData services + new CDS service) to generator_agent_tool
15. Generator creates MCP server using only OData (production-safe, no ADT needed)
