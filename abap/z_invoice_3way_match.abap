*&---------------------------------------------------------------------*
*& Report Z_INVOICE_3WAY_MATCH
*& Description: 3-Way Match - Invoice → PO → GRN Status Check
*&---------------------------------------------------------------------*
REPORT z_invoice_3way_match.

*----------------------------------------------------------------------*
* Selection Screen
*----------------------------------------------------------------------*
SELECTION-SCREEN BEGIN OF BLOCK b1 WITH FRAME TITLE TEXT-001.
  PARAMETERS: p_belnr TYPE re_belnr OBLIGATORY,          "Invoice Doc Number
              p_gjahr TYPE gjahr DEFAULT sy-datum+0(4).   "Fiscal Year
SELECTION-SCREEN END OF BLOCK b1.

*----------------------------------------------------------------------*
* Types
*----------------------------------------------------------------------*
TYPES: BEGIN OF ty_invoice_header,
         belnr      TYPE re_belnr,
         gjahr      TYPE gjahr,
         bldat      TYPE bldat,
         budat      TYPE budat,
         xblnr      TYPE xblnr1,
         lifnr      TYPE lifnr,
         name1      TYPE name1_gp,
         bukrs      TYPE bukrs,
         waers      TYPE waers,
         wrbtr      TYPE wrbtr,
         zlspr      TYPE dzlspr,
         zfbdt      TYPE dzfbdt,
         bstat      TYPE bstat,
       END OF ty_invoice_header,

       BEGIN OF ty_invoice_item,
         belnr      TYPE re_belnr,
         gjahr      TYPE gjahr,
         buzei      TYPE rblgp,
         ebeln      TYPE ebeln,
         ebelp      TYPE ebelp,
         matnr      TYPE matnr,
         txz01      TYPE txz01,
         menge      TYPE menge_d,
         meins      TYPE bstme,
         wrbtr      TYPE wrbtr,
         waers      TYPE waers,
       END OF ty_invoice_item,

       BEGIN OF ty_po_header,
         ebeln      TYPE ebeln,
         bsart      TYPE esart,
         aedat      TYPE erdat,
         lifnr      TYPE elifn,
         ekorg      TYPE ekorg,
         ekgrp      TYPE bkgrp,
         waers      TYPE waers,
         frgke      TYPE frgke,
       END OF ty_po_header,

       BEGIN OF ty_po_item,
         ebeln      TYPE ebeln,
         ebelp      TYPE ebelp,
         matnr      TYPE matnr,
         txz01      TYPE txz01,
         menge      TYPE bstmg,
         meins      TYPE bstme,
         netpr      TYPE bprei,
         netwr      TYPE bwert,
         elikz      TYPE elikz,    "Delivery Completed
         erekz      TYPE erekz,    "Final Invoice
       END OF ty_po_item,

       BEGIN OF ty_grn,
         ebeln      TYPE ebeln,
         ebelp      TYPE ebelp,
         belnr      TYPE mblnr,
         gjahr      TYPE mjahr,
         zeile      TYPE mblpo,
         budat      TYPE budat,
         menge      TYPE menge_d,
         meins      TYPE bstme,
         bwart      TYPE bwart,
       END OF ty_grn,

       BEGIN OF ty_match_result,
         ebeln      TYPE ebeln,
         ebelp      TYPE ebelp,
         matnr      TYPE matnr,
         txz01      TYPE txz01,
         po_qty     TYPE menge_d,
         po_amt     TYPE bwert,
         grn_qty    TYPE menge_d,
         grn_docs   TYPE i,
         inv_qty    TYPE menge_d,
         inv_amt    TYPE wrbtr,
         del_compl  TYPE elikz,
         fin_inv    TYPE erekz,
         match_icon TYPE icon_d,
         match_text TYPE char30,
       END OF ty_match_result.

*----------------------------------------------------------------------*
* Data Declarations
*----------------------------------------------------------------------*
DATA: gs_inv_header  TYPE ty_invoice_header,
      gt_inv_items   TYPE TABLE OF ty_invoice_item,
      gt_po_headers  TYPE TABLE OF ty_po_header,
      gt_po_items    TYPE TABLE OF ty_po_item,
      gt_grn         TYPE TABLE OF ty_grn,
      gt_match       TYPE TABLE OF ty_match_result,
      gv_po_list     TYPE RANGE OF ebeln,
      gv_total_inv   TYPE wrbtr,
      gv_total_grn   TYPE menge_d.

*----------------------------------------------------------------------*
* START-OF-SELECTION
*----------------------------------------------------------------------*
START-OF-SELECTION.

  PERFORM get_invoice_header.
  PERFORM get_invoice_items.
  PERFORM get_po_data.
  PERFORM get_grn_data.
  PERFORM build_match_results.
  PERFORM display_output.

*&---------------------------------------------------------------------*
*& Form GET_INVOICE_HEADER
*&---------------------------------------------------------------------*
FORM get_invoice_header.

  " Get invoice header from RBKP
  SELECT SINGLE
         rbkp~belnr rbkp~gjahr rbkp~bldat rbkp~budat
         rbkp~xblnr rbkp~lifnr rbkp~bukrs rbkp~waers
         rbkp~wrbtr rbkp~zlspr rbkp~zfbdt rbkp~bstat
         lfa1~name1
    INTO (gs_inv_header-belnr, gs_inv_header-gjahr,
          gs_inv_header-bldat, gs_inv_header-budat,
          gs_inv_header-xblnr, gs_inv_header-lifnr,
          gs_inv_header-bukrs, gs_inv_header-waers,
          gs_inv_header-wrbtr, gs_inv_header-zlspr,
          gs_inv_header-zfbdt, gs_inv_header-bstat,
          gs_inv_header-name1)
    FROM rbkp
    INNER JOIN lfa1 ON lfa1~lifnr = rbkp~lifnr
    WHERE rbkp~belnr = p_belnr
      AND rbkp~gjahr = p_gjahr.

  IF sy-subrc <> 0.
    MESSAGE 'Invoice not found' TYPE 'E'.
  ENDIF.

ENDFORM.

*&---------------------------------------------------------------------*
*& Form GET_INVOICE_ITEMS
*&---------------------------------------------------------------------*
FORM get_invoice_items.

  " Get invoice items from RSEG with PO reference
  SELECT rseg~belnr rseg~gjahr rseg~buzei
         rseg~ebeln rseg~ebelp rseg~matnr
         rseg~txz01 rseg~menge rseg~meins
         rseg~wrbtr rseg~waers
    INTO TABLE gt_inv_items
    FROM rseg
    WHERE belnr = p_belnr
      AND gjahr = p_gjahr.

  IF gt_inv_items IS INITIAL.
    MESSAGE 'No invoice items found' TYPE 'E'.
  ENDIF.

  " Build PO number range for subsequent selects
  LOOP AT gt_inv_items ASSIGNING FIELD-SYMBOL(<inv>).
    IF <inv>-ebeln IS NOT INITIAL.
      APPEND VALUE #( sign = 'I' option = 'EQ' low = <inv>-ebeln )
        TO gv_po_list.
    ENDIF.
  ENDLOOP.
  SORT gv_po_list BY low.
  DELETE ADJACENT DUPLICATES FROM gv_po_list COMPARING low.

ENDFORM.

*&---------------------------------------------------------------------*
*& Form GET_PO_DATA
*&---------------------------------------------------------------------*
FORM get_po_data.

  CHECK gv_po_list IS NOT INITIAL.

  " PO Headers from EKKO
  SELECT ebeln bsart aedat lifnr ekorg ekgrp waers frgke
    INTO TABLE gt_po_headers
    FROM ekko
    WHERE ebeln IN gv_po_list.

  " PO Items from EKPO
  SELECT ebeln ebelp matnr txz01 menge meins netpr netwr elikz erekz
    INTO TABLE gt_po_items
    FROM ekpo
    WHERE ebeln IN gv_po_list.

ENDFORM.

*&---------------------------------------------------------------------*
*& Form GET_GRN_DATA
*&---------------------------------------------------------------------*
FORM get_grn_data.

  CHECK gv_po_list IS NOT INITIAL.

  " Goods Receipt from EKBE (PO History) - movement type 101 = GR
  SELECT ebeln ebelp belnr gjahr buzei budat menge meins bwart
    INTO TABLE gt_grn
    FROM ekbe
    WHERE ebeln IN gv_po_list
      AND vgabe = '1'.          "Goods Receipt

  SORT gt_grn BY ebeln ebelp.

ENDFORM.

*&---------------------------------------------------------------------*
*& Form BUILD_MATCH_RESULTS
*&---------------------------------------------------------------------*
FORM build_match_results.

  DATA: ls_match TYPE ty_match_result.

  LOOP AT gt_inv_items ASSIGNING FIELD-SYMBOL(<inv>).
    CLEAR ls_match.

    ls_match-ebeln   = <inv>-ebeln.
    ls_match-ebelp   = <inv>-ebelp.
    ls_match-matnr   = <inv>-matnr.
    ls_match-txz01   = <inv>-txz01.
    ls_match-inv_qty = <inv>-menge.
    ls_match-inv_amt = <inv>-wrbtr.

    " Get PO item data
    READ TABLE gt_po_items ASSIGNING FIELD-SYMBOL(<po>)
      WITH KEY ebeln = <inv>-ebeln
               ebelp = <inv>-ebelp.
    IF sy-subrc = 0.
      ls_match-po_qty    = <po>-menge.
      ls_match-po_amt    = <po>-netwr.
      ls_match-del_compl = <po>-elikz.
      ls_match-fin_inv   = <po>-erekz.
    ENDIF.

    " Sum GRN quantities for this PO item
    LOOP AT gt_grn ASSIGNING FIELD-SYMBOL(<grn>)
      WHERE ebeln = <inv>-ebeln
        AND ebelp = <inv>-ebelp.
      ls_match-grn_qty = ls_match-grn_qty + <grn>-menge.
      ls_match-grn_docs = ls_match-grn_docs + 1.
    ENDLOOP.

    " Determine 3-way match status
    IF ls_match-po_qty IS INITIAL.
      ls_match-match_icon = icon_led_red.
      ls_match-match_text = 'No PO Reference'.
    ELSEIF ls_match-grn_qty IS INITIAL.
      ls_match-match_icon = icon_led_red.
      ls_match-match_text = 'No GRN Found'.
    ELSEIF ls_match-grn_qty < ls_match-inv_qty.
      ls_match-match_icon = icon_led_yellow.
      ls_match-match_text = 'GRN Qty < Invoice Qty'.
    ELSEIF ls_match-grn_qty >= ls_match-inv_qty
       AND ls_match-del_compl = 'X'.
      ls_match-match_icon = icon_led_green.
      ls_match-match_text = '3-Way Match OK'.
    ELSE.
      ls_match-match_icon = icon_led_yellow.
      ls_match-match_text = 'Partial Match'.
    ENDIF.

    APPEND ls_match TO gt_match.
  ENDLOOP.

ENDFORM.

*&---------------------------------------------------------------------*
*& Form DISPLAY_OUTPUT
*&---------------------------------------------------------------------*
FORM display_output.

  DATA: lo_alv   TYPE REF TO cl_salv_table,
        lo_cols  TYPE REF TO cl_salv_columns_table,
        lo_col   TYPE REF TO cl_salv_column,
        lo_funcs TYPE REF TO cl_salv_functions_list,
        lo_disp  TYPE REF TO cl_salv_display_settings,
        lv_title TYPE lvc_title.

  " Print invoice header summary
  WRITE: / '═══════════════════════════════════════════════════════'.
  WRITE: / '  3-Way Match Report: Invoice → PO → GRN'.
  WRITE: / '═══════════════════════════════════════════════════════'.
  SKIP.
  WRITE: / 'Invoice Number :', gs_inv_header-belnr,
         / 'Fiscal Year    :', gs_inv_header-gjahr,
         / 'Document Date  :', gs_inv_header-bldat,
         / 'Posting Date   :', gs_inv_header-budat,
         / 'Supplier       :', gs_inv_header-lifnr, gs_inv_header-name1,
         / 'Company Code   :', gs_inv_header-bukrs,
         / 'Gross Amount   :', gs_inv_header-wrbtr, gs_inv_header-waers,
         / 'Reference      :', gs_inv_header-xblnr,
         / 'Payment Block  :', gs_inv_header-zlspr.

  IF gs_inv_header-bstat = '5'.
    WRITE: / 'Invoice Status : Posted'.
  ELSEIF gs_inv_header-bstat = '2'.
    WRITE: / 'Invoice Status : Parked'.
  ELSE.
    WRITE: / 'Invoice Status :', gs_inv_header-bstat.
  ENDIF.

  SKIP.
  WRITE: / '───────────────────────────────────────────────────────'.
  WRITE: / '  Line Item 3-Way Match Details'.
  WRITE: / '───────────────────────────────────────────────────────'.
  SKIP.

  " Build ALV for match results
  TRY.
      cl_salv_table=>factory(
        IMPORTING r_salv_table = lo_alv
        CHANGING  t_table      = gt_match ).

      " Enable toolbar functions
      lo_funcs = lo_alv->get_functions( ).
      lo_funcs->set_all( abap_true ).

      " Set column texts
      lo_cols = lo_alv->get_columns( ).
      lo_cols->set_optimize( abap_true ).

      PERFORM set_col_text USING lo_cols 'EBELN'      'PO Number'.
      PERFORM set_col_text USING lo_cols 'EBELP'      'PO Item'.
      PERFORM set_col_text USING lo_cols 'MATNR'      'Material'.
      PERFORM set_col_text USING lo_cols 'TXZ01'      'Description'.
      PERFORM set_col_text USING lo_cols 'PO_QTY'     'PO Qty'.
      PERFORM set_col_text USING lo_cols 'PO_AMT'     'PO Amount'.
      PERFORM set_col_text USING lo_cols 'GRN_QTY'    'GRN Qty'.
      PERFORM set_col_text USING lo_cols 'GRN_DOCS'   'GRN Docs'.
      PERFORM set_col_text USING lo_cols 'INV_QTY'    'Invoice Qty'.
      PERFORM set_col_text USING lo_cols 'INV_AMT'    'Invoice Amt'.
      PERFORM set_col_text USING lo_cols 'DEL_COMPL'  'Deliv.Compl'.
      PERFORM set_col_text USING lo_cols 'FIN_INV'    'Final Inv'.
      PERFORM set_col_text USING lo_cols 'MATCH_ICON' 'Status'.
      PERFORM set_col_text USING lo_cols 'MATCH_TEXT' 'Match Result'.

      " Display settings
      lo_disp = lo_alv->get_display_settings( ).
      lv_title = |3-Way Match: Invoice { p_belnr } / { p_gjahr }|.
      lo_disp->set_list_header( lv_title ).
      lo_disp->set_striped_pattern( abap_true ).

      lo_alv->display( ).

    CATCH cx_salv_msg cx_salv_not_found.
      " Fallback: simple WRITE output
      LOOP AT gt_match ASSIGNING FIELD-SYMBOL(<m>).
        WRITE: / <m>-ebeln, <m>-ebelp, <m>-matnr,
                 <m>-po_qty, <m>-grn_qty, <m>-inv_qty,
                 <m>-match_text.
      ENDLOOP.
  ENDTRY.

ENDFORM.

*&---------------------------------------------------------------------*
*& Form SET_COL_TEXT
*&---------------------------------------------------------------------*
FORM set_col_text USING io_cols TYPE REF TO cl_salv_columns_table
                        iv_name TYPE lvc_fname
                        iv_text TYPE string.

  DATA: lo_col TYPE REF TO cl_salv_column.

  TRY.
      lo_col = io_cols->get_column( iv_name ).
      lo_col->set_short_text( CONV #( iv_text ) ).
      lo_col->set_medium_text( CONV #( iv_text ) ).
      lo_col->set_long_text( CONV #( iv_text ) ).
    CATCH cx_salv_not_found.
      " Column not found - skip
  ENDTRY.

ENDFORM.
