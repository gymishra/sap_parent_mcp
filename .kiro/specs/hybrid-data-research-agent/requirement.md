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
2. Agent → OData: `MM_PUR_PO_MAINT_V2_SRV` → gets PO headers
3. Agent → detects: no invoice details in OData response
4. Agent → SQL: `SELECT * FROM rseg WHERE ebeln = '4500000001'` → gets invoice items
5. Agent → merges, presents combined result
6. Agent: "Want me to create a dedicated PO-Invoice agent?"
7. User: "Yes"
8. Generator receives history → creates MCP server with `get_po_with_invoices()` tool that does both internally
