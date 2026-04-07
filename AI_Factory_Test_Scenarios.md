# AI Factory MCP Server — Test Scenarios

## Real-World SAP Business Use Cases for Demo & Validation

---

## 1. OData Agent — Cross-Domain Procurement Analysis

**Prompt:**
> "Show me purchase orders from the last 30 days where the goods receipt is done but invoice is still pending. Include the PO number, vendor, GR date, and outstanding amount."

**What it tests:**
- Service discovery (PO + GR/IR services)
- Multi-entity querying (PO headers, items, history)
- Cross-referencing GR status vs Invoice status
- Date filtering and business logic reasoning

**Expected agent behavior:**
1. Search for purchase order OData service
2. Get metadata to find GR/IR status fields
3. Query PO items where GoodsReceiptStatus != '' AND InvoiceReceiptStatus = ''
4. Cross-validate via PO history (HistoryCategory E vs R)
5. Present results with vendor details and outstanding amounts

---

## 2. OData Agent — Financial Close Readiness

**Prompt:**
> "I need to check month-end close readiness. Show me all open vendor invoices that are past due, grouped by company code, with total exposure per company code and aging buckets (0-30, 31-60, 60+ days)."

**What it tests:**
- Accounts payable open items querying
- Date arithmetic (days past due calculation)
- Grouping and aggregation logic
- Multi-company code analysis
- Executive summary generation

**Expected agent behavior:**
1. Search for AP open items or supplier invoice service
2. Query all open items where NetDueDate < today
3. Calculate days past due for each invoice
4. Group by company code and aging bucket
5. Generate summary dashboard with totals and recommendations

---

## 3. ADT Agent — Code Investigation

**Prompt:**
> "Find all custom Z programs related to invoice verification in our system. Show me the source code of the most recently modified one and check if it has any ATC findings."

**What it tests:**
- SAP repository object search (wildcard Z* + keyword)
- Source code retrieval via ADT
- ATC quality check execution
- Multi-step reasoning (search → read → check)

**Expected agent behavior:**
1. Use search_objects with query "Z*INVOICE*" or "Z*LIV*" or "Z*MIRO*"
2. Identify the most recently modified program
3. Read its source code via get_abap_program
4. Run run_atc_check on the program
5. Present source code + ATC findings together

---

## 4. Multi-Agent (OData + ADT) — 3-Way Match Investigation

**Prompt:**
> "We have a 3-way match issue. Show me purchase orders where the GR quantity doesn't match the invoice quantity. If no OData service covers this, write a SQL query joining EKKO, EKBE, and RBKP tables to find mismatches."

**What it tests:**
- OData-first approach with ADT fallback
- Complex business logic (3-way match: PO vs GR vs Invoice)
- SQL generation for SAP tables
- Cross-agent routing (OData agent tries first, ADT agent as fallback)

**Expected agent behavior:**
1. OData agent searches for GR/IR reconciliation service
2. If found: query for quantity mismatches
3. If not found: ADT agent generates SQL:
   ```sql
   SELECT e.EBELN, e.EBELP, e.MENGE as PO_QTY,
          b.MENGE as GR_QTY, r.MENGE as IV_QTY
   FROM EKPO e
   INNER JOIN EKBE b ON e.EBELN = b.EBELN AND e.EBELP = b.EBELP AND b.BEWTP = 'E'
   INNER JOIN EKBE r ON e.EBELN = r.EBELN AND e.EBELP = r.EBELP AND r.BEWTP = 'R'
   WHERE b.MENGE <> r.MENGE
   ```
4. Execute via get_table_contents and present mismatches

---

## 5. Generator Agent — Deploy Cloud ALM Monitoring Agent

**Prompt:**
> "Create and deploy a new Cloud ALM monitoring agent that tracks process exceptions, alerts, and monitoring events. Name it calm_ops_monitor."

**What it tests:**
- Domain detection (Cloud ALM keywords)
- Code generation via Claude/Bedrock
- S3 upload + CodeBuild trigger
- End-to-end deployment pipeline

**Expected agent behavior:**
1. Detect domain = "calm"
2. Generate Python FastMCP tools for monitoring events, alerts, analytics
3. Package with CALM server template
4. Upload to S3 staging bucket
5. Trigger CodeBuild → deploy to AgentCore
6. Return build ID and SSM key for tracking

---

## 6. Cross-Domain (OData + ADT) — Duplicate Invoice Detection

**Prompt:**
> "Our AP team reports duplicate invoices. Search for any custom ABAP programs that handle duplicate invoice detection, and also query the supplier invoice OData service to find invoices from the same vendor with the same amount posted within 7 days of each other."

**What it tests:**
- Parallel multi-agent execution (ADT + OData)
- Custom code discovery in SAP
- Complex OData filtering (same vendor + same amount + date range)
- Business process understanding (AP duplicate detection)

**Expected agent behavior:**
1. ADT agent: search_objects for Z*DUPL*INVOICE* or Z*DUP*INV*
2. If found: read source code to understand detection logic
3. OData agent: query supplier invoices grouped by vendor + amount
4. Filter for invoices where same vendor + same amount within 7 days
5. Present both: existing custom programs AND detected potential duplicates

---

## 7. OData Agent — Procure-to-Pay Full Chain

**Prompt:**
> "Show me 2 purchase requisitions, their RFQs, and their quotations."

**What it tests:**
- Multi-service chaining (PR → RFQ → Quotation)
- Three separate OData service discoveries
- Cross-document linking via reference fields
- End-to-end procurement flow visualization

**Expected agent behavior:**
1. Query API_PURCHASEREQ_PROCESS_SRV for 2 PRs
2. Query API_RFQ_PROCESS_SRV for linked RFQs
3. Query API_QUOTATION_PROCESS_SRV for supplier responses
4. Link documents via PurchaseRequisition reference fields
5. Present full PR → RFQ → Quotation chain

---

## 8. OData Agent — Blocked Invoice Analysis

**Prompt:**
> "Show me invoices that are blocked for payment."

**What it tests:**
- Supplier invoice service discovery
- Payment blocking reason filtering
- SAP MM domain knowledge (block codes R, B, A, I, Z)
- Actionable recommendations (MRBR, FB02)

**Expected agent behavior:**
1. Search for supplier invoice OData service
2. Query where PaymentBlockingReason != ''
3. Decode block reason codes
4. Group by block type with totals
5. Recommend resolution actions per block type

---

## Architecture Reference

```
Kiro → kiro_bridge.py (Okta 3LO) → AgentCore Runtime
  └── AI Factory MCP Server (5 router tools)
        ├── odata_agent_tool  → Strands → S/4 OData tools (12)
        ├── adt_agent_tool    → Strands → ADT/ABAP tools (22)
        ├── calm_agent_tool   → Strands → Cloud ALM tools (46)
        ├── sf_agent_tool     → Strands → SuccessFactors tools (11)
        └── generator_agent_tool → Strands → deploy new agents (1)
```

**Model:** us.anthropic.claude-sonnet-4-6 (1M context)
**Runtime:** AWS Bedrock AgentCore
**Auth:** Okta 3LO → SAP Bearer Token
