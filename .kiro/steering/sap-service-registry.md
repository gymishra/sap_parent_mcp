---
inclusion: auto
---

# SAP OData Service Registry

When the user asks SAP-related questions, use this registry to determine the correct OData service, entity set, and fields. Do NOT call discover_sap_services or get_service_metadata — go directly to query_sap_odata with the correct parameters.

SAP Base URL: `https://vhcals4hci.awspoc.club`

## Sales & Distribution (SD)

### Sales Orders
- Service: `/sap/opu/odata/sap/API_SALES_ORDER_SRV`
- Entity: `A_SalesOrder`
- Key: `SalesOrder` (e.g. `'0000000002'`)
- Common fields: `SalesOrder,SalesOrderType,SoldToParty,CreationDate,TotalNetAmount,TransactionCurrency,OverallSDProcessStatus,HeaderBillingBlockReason`
- Items entity: `A_SalesOrderItem` — fields: `SalesOrder,SalesOrderItem,Material,RequestedQuantity,NetAmount,TransactionCurrency`
- Filter for blocked: `HeaderBillingBlockReason ne ''`
- Filter for recent: `CreationDate ge datetime'2021-01-01T00:00:00'`

### Sales Quotations
- Service: `/sap/opu/odata/sap/API_SALES_ORDER_SRV`
- Entity: `A_SalesQuotation` (same service, different entity)
- Worklist service: `/sap/opu/odata/sap/SD_F1852_QUOT_WL_SRV`

### Sales Contracts
- Worklist: `/sap/opu/odata/sap/SD_F1851_CONTR_WL_SRV`

### Billing Documents / Invoices (Customer)
- Service: `/sap/opu/odata/sap/API_BILLING_DOCUMENT_SRV`
- Entity: `A_BillingDocument`
- Key: `BillingDocument`
- Common fields: `BillingDocument,BillingDocumentType,SalesOrganization,BillingDocumentDate,CreationDate,BillingDocumentIsCancelled`
- Items: `A_BillingDocumentItem`

### Customer Returns
- Service: `/sap/opu/odata/sap/API_CUSTOMER_RETURN_SRV`
- Worklist: `/sap/opu/odata/sap/SD_F1708_CRT_WL_SRV`

### Credit Memo Requests
- Service: `/sap/opu/odata/sap/API_CREDIT_MEMO_REQUEST_SRV`

### Sales Order Fulfillment Monitor
- Service: `/sap/opu/odata/sap/SD_SOFM_SRV`
- Credit blocks: `/sap/opu/odata/sap/SD_SOFM_CREDIT_BLOCK_SRV`
- Invoice issues: `/sap/opu/odata/sap/SD_SOFM_INVOICE_SRV`

### Customer 360
- Service: `/sap/opu/odata/sap/SD_F2187_CUST360_SRV`

## Materials Management (MM) — Purchasing

### Purchase Orders
- Service (working): `/sap/opu/odata/sap/MM_PUR_PO_MAINT_V2_SRV`
- Entity: `C_PurchaseOrderTP`
- Key: `PurchaseOrder` (e.g. `'4500000001'`), also needs `DraftUUID=guid'00000000-0000-0000-0000-000000000000',IsActiveEntity=true`
- Common fields: `PurchaseOrder,PurchaseOrderType,Supplier,CompanyCode,PurchasingOrganization,DocumentCurrency`
- Monitor service (404 on this system): `/sap/opu/odata/sap/MM_PUR_POITEMS_MONI_SRV`
- API service (403 on this system): `/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV`

### Purchase Requisitions
- Monitor: `/sap/opu/odata/sap/MM_PUR_PRITEM_MNTR_SRV`
- Manage: `/sap/opu/odata/sap/MM_PUR_PR_PROCESS_SRV`

### Purchase Contracts
- Monitor: `/sap/opu/odata/sap/MM_PUR_CTRITEM_MNTR_SRV_01`
- Manage: `/sap/opu/odata/sap/MM_PUR_OA_MAINTAIN_SRV`

### Supplier Quotations & RFQs
- RFQ facets: `/sap/opu/odata/sap/MM_PUR_QTN_MAINTAIN_SRV` → entity `C_RequestForQuotationFacet`
- Quotation status per RFQ: `/sap/opu/odata/sap/MM_PUR_RFQITEM_MNTR_SRV` → entity `C_RFQSuppQuotationStatus`
- Filter: `RequestForQuotation eq '7000000000'`
- Fields: `RequestForQuotation,RequestForQuotationItem,SupplierQuotation,SupplierQuotationItem,QtnLifecycleStatus,QtnLifecycleStatusName,Supplier`

### Supplier Invoices
- List: `/sap/opu/odata/sap/MM_SUPPLIER_INVOICE_LIST_SRV` → entity `SupInvoice`
- Fields: `SupplierInvoice,FiscalYear,CompanyCode,CompanyCodeName,DocumentCurrency,InvoiceGrossAmount,Status,StatusDescription,DocumentDate,CreatedByUser,PaymentBlockingReason,DocumentReferenceID`
- Filter for blocked: `PaymentBlockingReason eq true`
- API: `/sap/opu/odata/sap/API_SUPPLIERINVOICE_PROCESS_SRV` → entity `A_SupplierInvoice`

### Purchasing Info Records
- Manage: `/sap/opu/odata/sap/MM_PUR_INFO_RECORDS_MANAGE_SRV`
- Monitor price history: `/sap/opu/odata/sap/MM_PUR_INFORECPRICEH_MNTR_SRV`

### Supplier Evaluation
- Overall: `/sap/opu/odata/sap/MM_PUR_ANA_SUPPLEVALOVERALL_SRV`
- By quality: `/sap/opu/odata/sap/MM_PUR_SEV_CONFIG`

## Inventory Management (MM-IM)

### Material Documents / Goods Movements
- Service: `/sap/opu/odata/sap/MMIM_MATDOC_SRV`
- Overview: `/sap/opu/odata/sap/MMIM_MATDOC_OV_SRV`

### Goods Receipt for PO
- Service: `/sap/opu/odata/sap/MMIM_GR4PO_DL_SRV`

### Physical Inventory
- Service: `/sap/opu/odata/sap/MM_IM_PHYS_INV_DOC_SRV`

### Stock Overview
- Current stock: entity `C_StockQtyCurrentValue` in various stock CDS services
- Dead stock: `/sap/opu/odata/sap/MMIM_DEADSTOCKMATERIAL_SRV`
- Slow moving: `/sap/opu/odata/sap/MMIM_SLOWORNONMOVINGMATERIAL_SRV`

## Finance (FI)

### Journal Entries / G/L Line Items
- Service: `/sap/opu/odata/sap/API_GLACCOUNTLINEITEM`
- Entity: `A_GLAccountLineItem`
- G/L Account Balance: `/sap/opu/odata/sap/FAC_GL_ACCOUNT_BALANCE_SRV`

### Trial Balance
- Service: `/sap/opu/odata/sap/C_TRIALBALANCE_CDS`

### Financial Statement
- Service: `/sap/opu/odata/sap/FAC_FINANCIAL_STATEMENT_SRV`

### Accounts Payable
- Vendor line items: `/sap/opu/odata/sap/FAP_VENDOR_LINE_ITEMS_SRV`
- Vendor balances: `/sap/opu/odata/sap/FAP_VENDOR_BALANCE_SRV`
- Payment blocks: `/sap/opu/odata/sap/FAP_MANAGE_PAYMENT_BLOCKS_SRV`
- AP Overview: `/sap/opu/odata/sap/FAP_APOVERVIEWPAGE_SRV`

### Accounts Receivable
- Customer line items: `/sap/opu/odata/sap/FAR_CUSTOMER_LINE_ITEMS`
- Customer balances: `/sap/opu/odata/sap/FAR_CUST_BALANCE_SRV`
- AR Overview: `/sap/opu/odata/sap/FAR_AR_OVP_SRV`
- Dunning: `/sap/opu/odata/sap/FAR_DUNNING_PROPOSAL_SRV`
- Collection worklist: `/sap/opu/odata/sap/FAP_COLLECTION_WORKLIST_SRV`

### Fixed Assets
- Manage: `/sap/opu/odata/sap/FAA_ASSET_MANAGE_SRV`
- Values overview: `/sap/opu/odata/sap/FAA_ASSET_VALUES_OVERVIEW_SRV`
- Depreciation: `/sap/opu/odata/sap/FAA_DEPRECIATION_RUN_SRV`

### Bank Accounts / Cash Management
- Bank accounts: `/sap/opu/odata/sap/FCLM_BAM_SRV`
- Cash position: `/sap/opu/odata/sap/C_CASHPOSITIONOVERVIEW_CDS`
- Bank transfers: `/sap/opu/odata/sap/FCLM_CASH_BANKTRANSFER_SRV`

## Controlling (CO)

### Cost Centers
- Manage: `/sap/opu/odata/sap/FCO_MANAGE_COST_CENTERS_SRV`
- Master data: `/sap/opu/odata/sap/FCOM_COSTCENTER_SRV`
- Actuals query: `/sap/opu/odata/sap/C_COSTCENTERQ2001_CDS`

### Internal Orders
- Manage: `/sap/opu/odata/sap/FCO_INTERNAL_ORDER_SRV`

### Profit Centers
- Manage: `/sap/opu/odata/sap/FAC_MANAGE_PROFIT_CENTERS_SRV`
- Actuals: `/sap/opu/odata/sap/C_PROFITCENTERQ2701_CDS`

### Profitability Analysis
- Service: `/sap/opu/odata/sap/C_PROFITABILITY_Q0001_CDS`

### Production Costs
- By order: `/sap/opu/odata/sap/C_PRODUCTCOSTBYORDERQUERY_CDS`
- By work center: `/sap/opu/odata/sap/C_WORKCENTERPRODCOSTQUERY_CDS`
- Analysis: `/sap/opu/odata/sap/FCO_PRODUCTION_COST_ANALYSIS_SRV`

## Production Planning (PP)

### Production Orders
- Manage: `/sap/opu/odata/sap/PP_MPE_ORDER_MANAGE`
- Operations: `/sap/opu/odata/sap/PP_MPE_OPER_MANAGE`
- Confirmation: `/sap/opu/odata/sap/PP_PRODORD_CONFIRM`
- Release: `/sap/opu/odata/sap/PP_PRODORDER_RELEASE_SRV`

### Planned Orders
- Manage: `/sap/opu/odata/sap/C_PLANNEDORDERS_CDS`
- Convert: `/sap/opu/odata/sap/C_CONVERTPLANNEDORDER_CDS`

### MRP
- Cockpit: `/sap/opu/odata/sap/PP_MRP_COCKPIT_SRV`
- Material coverage: `/sap/opu/odata/sap/PP_MRP_MATERIAL_COVERAGE_SRV`

### Capacity
- Evaluate: `/sap/opu/odata/sap/PP_CAPPLANS1_SRV`
- Work center schedule: `/sap/opu/odata/sap/PP_MNTR_WRKCTR_SRV`

## Plant Maintenance (PM)

### Maintenance Orders
- Object page: `/sap/opu/odata/sap/EAM_OBJPG_MAINTENANCEORDER_SRV`
- API: `/sap/opu/odata/sap/API_MAINTENANCEORDER`
- Backlog: `/sap/opu/odata/sap/EAM_BACKLOG_MANAGE`

### Maintenance Notifications
- API: `/sap/opu/odata/sap/API_MAINTNOTIFICATION`
- Object page: `/sap/opu/odata/sap/EAM_OBJPG_MAINTNOTIFICATION_SRV`

### Maintenance Plans
- API: `/sap/opu/odata/sap/API_MAINTENANCEPLAN`
- Scheduling overview: `/sap/opu/odata/sap/C_MAINTPLANSCHEDGOVWQUERY_CDS`

### Equipment / Functional Locations
- Equipment: `/sap/opu/odata/sap/EAM_OBJPG_TECHNICALOBJECT_SRV`

## Quality Management (QM)

### Inspection Lots
- Manage: `/sap/opu/odata/sap/QM_INSPLOTMNG_SRV`
- Results recording: `/sap/opu/odata/sap/QM_RR_SRV`
- Usage decision: `/sap/opu/odata/sap/QM_INSPUSGDSC_MANAGE_SRV`

### Defects
- Manage: `/sap/opu/odata/sap/QM_DEFECT_MANAGE_SRV`
- Analytics: `/sap/opu/odata/sap/QM_DEFECT_ANALYZE_SRV`

### Quality Notifications
- Analytics: `/sap/opu/odata/sap/QM_NOTIF_ANALYZE_SRV`

## Master Data

### Business Partners / Customers / Suppliers
- API: `/sap/opu/odata/sap/API_BUSINESS_PARTNER`
- Customer master: `/sap/opu/odata/sap/MD_CUSTOMER_MASTER_SRV_01`
- Supplier master: `/sap/opu/odata/sap/MD_SUPPLIER_MASTER_SRV`
- Product master: `/sap/opu/odata/sap/API_PRODUCT_SRV`

### Products / Materials
- API: `/sap/opu/odata/sap/API_PRODUCT_SRV` → entity `A_Product`
- Maintain: `/sap/opu/odata/sap/MD_C_PRODUCT_MAINTAIN_SRV`

## Warehouse Management (EWM)

### Warehouse Orders/Tasks
- Orders: `/sap/opu/odata/sap/C_EWM_WAREHOUSEORDERQ_CDS`
- Tasks: `/sap/opu/odata/sap/C_EWM_WAREHOUSETASKQ_CDS`
- Outbound delivery: `/sap/opu/odata/sap/C_EWM_OUTBDELIVORDQ_CDS`
- Inbound delivery: `/sap/opu/odata/sap/C_EWM_INBOUNDDELIVERYITEMQ_CDS`

## Logistics / Delivery

### Outbound Deliveries
- Worklist: `/sap/opu/odata/sap/LE_SHP_DELIVERY_WORK_LIST`
- Create: `/sap/opu/odata/sap/LE_SHP_DELIVERY_CREATE`
- Fact sheet: `/sap/opu/odata/sap/LE_SHP_OUTBOUND_DELIVERY_FS`

### Inbound Deliveries
- Object page: `/sap/opu/odata/sap/LE_SHP_INBOUND_DELIVERY_OBJPG_SRV`

## Project Systems (PS)

### Projects
- API: `/sap/opu/odata/sap/API_ENTERPRISE_PROJECT_SRV;v=0002`
- Financial report: `/sap/opu/odata/sap/PS_PROJFIN_MNTR_SRV`
- Budget report: `/sap/opu/odata/sap/PS_PROJBDGTRPT_SRV`
- Overview: `/sap/opu/odata/sap/PS_PROJECT_OVERV_SRV`

## Service Management

### Service Orders
- Manage: `/sap/opu/odata/sap/CRMS4_SERV_ORDER_MANAGE_SRV`
- Query: `/sap/opu/odata/sap/C_SERVICEORDERQUERY_CDS`

### Service Contracts
- Manage: `/sap/opu/odata/sap/CRMS4_SERV_CONTRACT_MANAGE_SRV`

## Analytics / KPIs

### Sales Analytics
- Sales volume: `/sap/opu/odata/sap/C_SALESVOLUMEANALYTICSQRY_CDS`
- Delivery performance: `/sap/opu/odata/sap/C_SLSORDDELIVPERFANLYTSQRY_CDS`
- Revenue from invoices: `/sap/opu/odata/sap/C_REVENUEFROMINVOICEQRY_CDS`

### Procurement Analytics
- PO spend: `/sap/opu/odata/sap/C_PURCHASEORDERSPENDQUERY_CDS`
- Price variance: `/sap/opu/odata/sap/C_PRICEVARIANCE_CDS`
- Contract leakage: `/sap/opu/odata/sap/C_PURCHASECONTRACTLEAKAGE_CDS`

### Financial Analytics
- Days sales outstanding: `/sap/opu/odata/sap/C_DAYSSALESOUTSTANDING_CDS`
- Days payable outstanding: `/sap/opu/odata/sap/C_DAYSPAYABLESOUTSTANDING_CDS`
- Cash discount utilization: `/sap/opu/odata/sap/C_APCSHDISCUTILIZATION_CDS`

## Common Question → Direct Query Mapping

| Question Pattern | Service | Entity | Filter/Notes |
|---|---|---|---|
| "show sales orders" | API_SALES_ORDER_SRV | A_SalesOrder | top=N |
| "blocked invoices" (customer) | API_SALES_ORDER_SRV | A_SalesOrder | HeaderBillingBlockReason ne '' |
| "blocked supplier invoices" | MM_SUPPLIER_INVOICE_LIST_SRV | SupInvoice | PaymentBlockingReason eq true |
| "purchase orders" | MM_PUR_POITEMS_MONI_SRV | C_PurchaseOrderItemMoni(P_DisplayCurrency='USD')/Results | |
| "RFQs" | MM_PUR_QTN_MAINTAIN_SRV | C_RequestForQuotationFacet | |
| "quotations for RFQ X" | MM_PUR_RFQITEM_MNTR_SRV | C_RFQSuppQuotationStatus | RequestForQuotation eq 'X' |
| "billing documents" | API_BILLING_DOCUMENT_SRV | A_BillingDocument | |
| "products / materials" | API_PRODUCT_SRV | A_Product | |
| "customers" | API_BUSINESS_PARTNER | A_BusinessPartner | BusinessPartnerCategory eq '1' |
| "suppliers" | API_BUSINESS_PARTNER | A_BusinessPartner | BusinessPartnerCategory eq '2' |
| "maintenance orders" | API_MAINTENANCEORDER | MaintenanceOrder | |
| "cost centers" | FCOM_COSTCENTER_SRV | | |
| "G/L account balances" | FAC_GL_ACCOUNT_BALANCE_SRV | | |
| "vendor balances" | FAP_VENDOR_BALANCE_SRV | | |
| "customer balances" | FAR_CUST_BALANCE_SRV | | |
| "trial balance" | C_TRIALBALANCE_CDS | | |

## ADT (ABAP Development Tools) REST APIs

For any ABAP/CDS development artifact questions, use the `call_adt_api` tool instead of OData.

### Common ADT Paths
| Question Pattern | ADT Path | Query Params |
|---|---|---|
| "Z programs" / "custom programs" | /sap/bc/adt/repository/informationsystem/search | operation=quickSearch&query=Z*+type:PROG&maxResults=10 |
| "CDS views" / "Z CDS" | /sap/bc/adt/repository/informationsystem/search | operation=quickSearch&query=Z*+type:DDLS&maxResults=10 |
| "ABAP classes" / "Z classes" | /sap/bc/adt/repository/informationsystem/search | operation=quickSearch&query=Z*+type:CLAS&maxResults=10 |
| "function modules" | /sap/bc/adt/repository/informationsystem/search | operation=quickSearch&query=Z*+type:FUGR&maxResults=10 |
| "packages" | /sap/bc/adt/repository/informationsystem/search | operation=quickSearch&query=Z*+type:DEVC&maxResults=10 |
| "read program source" | /sap/bc/adt/programs/programs/{program_name}/source/main | |
| "read CDS source" | /sap/bc/adt/ddic/ddl/sources/{cds_name}/source/main | |
| "read class source" | /sap/bc/adt/oo/classes/{class_name}/source/main | |
| "transports" / "transport requests" | /sap/bc/adt/cts/transportrequests | |
| "search any object" | /sap/bc/adt/repository/informationsystem/search | operation=quickSearch&query={search_term}&maxResults=N |

### ADT Object Type Codes
- PROG = ABAP Program
- CLAS = ABAP Class
- DDLS = CDS Data Definition (DDL Source)
- FUGR = Function Group
- FUNC = Function Module
- DEVC = Package
- TABL = Database Table
- DTEL = Data Element
- DOMA = Domain
- TTYP = Table Type
- MSAG = Message Class

### ADT Operations Reference (all via single `call_adt_api` tool)

| Operation | adt_path | method | body/query_params | content_type |
|---|---|---|---|---|
| Search objects | /sap/bc/adt/repository/informationsystem/search | GET | query_params: operation=quickSearch&query=SAPMV45A&maxResults=10 | |
| Read source code | /sap/bc/adt/programs/programs/{name}/source/main | GET | | (returns text/plain) |
| Read class source | /sap/bc/adt/oo/classes/{name}/source/main | GET | | |
| Read CDS source | /sap/bc/adt/ddic/ddl/sources/{name}/source/main | GET | | |
| Read function group | /sap/bc/adt/functions/groups/{name}/source/main | GET | | |
| Write source code | /sap/bc/adt/programs/programs/{name}/source/main | PUT | body: full ABAP source, query_params: lockHandle={handle}&corrNr={transport} | text/plain |
| Lock object | /sap/bc/adt/programs/programs/{name} | POST | query_params: _action=LOCK&accessMode=MODIFY | |
| Unlock object | /sap/bc/adt/programs/programs/{name} | POST | query_params: _action=UNLOCK&lockHandle={handle} | |
| Activate object | /sap/bc/adt/activation | POST | body: XML with object refs | application/xml |
| Syntax check | /sap/bc/adt/checkruns | POST | body: XML with source URL | application/vnd.sap.adt.checkruns+xml |
| Run unit tests | /sap/bc/adt/abapunit/testruns | POST | body: XML with object URL | application/vnd.sap.adt.abapunit.testruns.result.v1+xml |
| Get table contents | /sap/bc/adt/datapreview/ddic | POST | body: entity name, query_params: rowNumber=100 | |
| Run SQL query | /sap/bc/adt/datapreview/freestyle | POST | body: SQL query string | text/plain |
| Transport info | /sap/bc/adt/cts/transportchecks | POST | body: XML with object URI | |
| Create transport | /sap/bc/adt/cts/transports | POST | body: XML with request text + dev class | |
| Object structure | /sap/bc/adt/programs/programs/{name} | GET | | |
| Package contents | /sap/bc/adt/repository/nodestructure | POST | query_params: parent_name={pkg}&parent_type=DEVC/K | |
| DDIC element info | /sap/bc/adt/ddic/ddl/sources/{name} | GET | | |

### ABAP Code Modification Workflow
1. `call_adt_api` — Search: GET /sap/bc/adt/repository/informationsystem/search
2. `call_adt_api` — Read source: GET /sap/bc/adt/.../source/main
3. `call_adt_api` — Lock: POST /sap/bc/adt/...?_action=LOCK
4. `call_adt_api` — Write source: PUT /sap/bc/adt/.../source/main (with lockHandle)
5. `call_adt_api` — Syntax check: POST /sap/bc/adt/checkruns
6. `call_adt_api` — Activate: POST /sap/bc/adt/activation
7. `call_adt_api` — Unlock: POST /sap/bc/adt/...?_action=UNLOCK
