*&---------------------------------------------------------------------*
*& Report Z_VENDOR_STMT_RECON
*& Vendor Statement Reconciliation Report for AP Finance Team
*&---------------------------------------------------------------------*
REPORT z_vendor_stmt_recon.

TABLES: lfa1, bsik, bsak.

SELECTION-SCREEN BEGIN OF BLOCK b1 WITH FRAME TITLE TEXT-001.
  SELECT-OPTIONS: s_lifnr FOR lfa1-lifnr,
                  s_bukrs FOR bsik-bukrs DEFAULT '1710',
                  s_budat FOR bsik-budat.
  PARAMETERS:     p_stida TYPE sy-datum DEFAULT sy-datum.
SELECTION-SCREEN END OF BLOCK b1.

TYPES: BEGIN OF ty_vendor_item,
         lifnr   TYPE lifnr,
         name1   TYPE name1_gp,
         bukrs   TYPE bukrs,
         belnr   TYPE belnr_d,
         gjahr   TYPE gjahr,
         buzei   TYPE buzei,
         budat   TYPE budat,
         bldat   TYPE bldat,
         xblnr   TYPE xblnr1,
         blart   TYPE blart,
         dmbtr   TYPE dmbtr,
         wrbtr   TYPE wrbtr,
         shkzg   TYPE shkzg,
         waers   TYPE waers,
         zuonr   TYPE dzuonr,
         sgtxt   TYPE sgtxt,
         augdt   TYPE augdt,
         augbl   TYPE augbl,
         status  TYPE char10,
       END OF ty_vendor_item.

DATA: gt_items   TYPE TABLE OF ty_vendor_item,
      gs_item    TYPE ty_vendor_item,
      gv_open    TYPE wrbtr,
      gv_cleared TYPE wrbtr,
      gv_total   TYPE wrbtr.

START-OF-SELECTION.

  " Fetch open items (BSIK)
  SELECT b~lifnr l~name1 b~bukrs b~belnr b~gjahr b~buzei
         b~budat b~bldat b~xblnr b~blart b~dmbtr b~wrbtr
         b~shkzg b~waers b~zuonr b~sgtxt b~augdt b~augbl
    INTO CORRESPONDING FIELDS OF TABLE gt_items
    FROM bsik AS b
    INNER JOIN lfa1 AS l ON b~lifnr = l~lifnr
    WHERE b~lifnr IN s_lifnr
      AND b~bukrs IN s_bukrs
      AND b~budat IN s_budat
      AND b~budat <= p_stida.

  LOOP AT gt_items ASSIGNING FIELD-SYMBOL(<fs>).
    <fs>-status = 'OPEN'.
    IF <fs>-shkzg = 'H'.
      <fs>-wrbtr = <fs>-wrbtr * -1.
      <fs>-dmbtr = <fs>-dmbtr * -1.
    ENDIF.
  ENDLOOP.

  " Fetch cleared items (BSAK) cleared after key date
  SELECT b~lifnr l~name1 b~bukrs b~belnr b~gjahr b~buzei
         b~budat b~bldat b~xblnr b~blart b~dmbtr b~wrbtr
         b~shkzg b~waers b~zuonr b~sgtxt b~augdt b~augbl
    APPENDING CORRESPONDING FIELDS OF TABLE gt_items
    FROM bsak AS b
    INNER JOIN lfa1 AS l ON b~lifnr = l~lifnr
    WHERE b~lifnr IN s_lifnr
      AND b~bukrs IN s_bukrs
      AND b~budat IN s_budat
      AND b~budat <= p_stida
      AND b~augdt > p_stida.

  LOOP AT gt_items ASSIGNING <fs> WHERE status IS INITIAL.
    <fs>-status = 'CLEARED'.
    IF <fs>-shkzg = 'H'.
      <fs>-wrbtr = <fs>-wrbtr * -1.
      <fs>-dmbtr = <fs>-dmbtr * -1.
    ENDIF.
  ENDLOOP.

  SORT gt_items BY lifnr budat belnr.

END-OF-SELECTION.

  " Output header
  WRITE: / '================================================================'.
  WRITE: / 'VENDOR STATEMENT RECONCILIATION REPORT'.
  WRITE: / 'Key Date:', p_stida, '  Company Code:', s_bukrs-low.
  WRITE: / '================================================================'.
  SKIP.

  DATA: lv_prev_vendor TYPE lifnr,
        lv_vendor_total TYPE wrbtr.

  LOOP AT gt_items INTO gs_item.
    " Vendor header
    IF gs_item-lifnr <> lv_prev_vendor.
      IF lv_prev_vendor IS NOT INITIAL.
        WRITE: / '  -------------------------------------------------------'.
        WRITE: / '  Vendor Balance:', lv_vendor_total, gs_item-waers.
        SKIP.
      ENDIF.
      lv_prev_vendor = gs_item-lifnr.
      lv_vendor_total = 0.
      WRITE: / 'Vendor:', gs_item-lifnr, '-', gs_item-name1.
      WRITE: / '  Doc No     | Post Date  | Ref        | Type | Amount       | Status'.
      WRITE: / '  -------------------------------------------------------'.
    ENDIF.

    lv_vendor_total = lv_vendor_total + gs_item-wrbtr.

    IF gs_item-status = 'OPEN'.
      gv_open = gv_open + gs_item-wrbtr.
    ELSE.
      gv_cleared = gv_cleared + gs_item-wrbtr.
    ENDIF.
    gv_total = gv_total + gs_item-wrbtr.

    WRITE: / '  ', gs_item-belnr, '|', gs_item-budat, '|',
             gs_item-xblnr(10), '|', gs_item-blart, '  |',
             gs_item-wrbtr, '|', gs_item-status.
  ENDLOOP.

  IF lv_prev_vendor IS NOT INITIAL.
    WRITE: / '  -------------------------------------------------------'.
    WRITE: / '  Vendor Balance:', lv_vendor_total.
  ENDIF.

  SKIP 2.
  WRITE: / '================================================================'.
  WRITE: / 'SUMMARY'.
  WRITE: / '================================================================'.
  WRITE: / 'Total Open Items:   ', gv_open.
  WRITE: / 'Total Cleared Items:', gv_cleared.
  WRITE: / 'Grand Total:        ', gv_total.
  WRITE: / 'Number of Items:    ', lines( gt_items ).
  WRITE: / '================================================================'.
