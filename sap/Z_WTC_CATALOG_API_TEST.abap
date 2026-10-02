*&---------------------------------------------------------------------*
*& Report Z_WTC_CATALOG_API_TEST
*&---------------------------------------------------------------------*
*& Temporary test program ($TMP, not for transport).
*& Proves the round trip from SAP to the Wage Type Catalog Service:
*&   1. read the Payslip Master Data workbook (.xlsx) from the PC
*&   2. POST it as multipart/form-data to /v1/catalog/jobs
*&   3. GET the one-time download_url of the returned job
*&   4. save the generated wage type catalog workbook on the PC
*& The download works once only: the service deletes the uploaded and
*& generated files after it. If the download fails, the job is
*& discarded so reruns do not pile up pending jobs (limit: 5, HTTP 429).
*&
*& Needs outbound HTTPS from the application server to the service
*& host, and the service's root CA (ISRG Root X1, Let's Encrypt) in
*& the STRUST SSL client PSE named in P_SSLID.
*& Do not save selection variants: the API key would be stored in them.
*&---------------------------------------------------------------------*
REPORT z_wtc_catalog_api_test LINE-SIZE 255.

TYPES: BEGIN OF ty_input_file,
         name  TYPE string,
         bytes TYPE i,
       END OF ty_input_file,
       ty_input_files TYPE STANDARD TABLE OF ty_input_file WITH DEFAULT KEY.

TYPES: BEGIN OF ty_summary,
         payslips          TYPE i,
         line_items        TYPE i,
         wage_types        TYPE i,
         provisional_codes TYPE i,
         molgas            TYPE string_table,
       END OF ty_summary.

" Job resource as returned by POST /v1/catalog/jobs (OpenAPI schema Job)
TYPES: BEGIN OF ty_job,
         job_id          TYPE string,
         status          TYPE string,
         created_at      TYPE string,
         expires_at      TYPE string,
         download_url    TYPE string,
         output_filename TYPE string,
         output_bytes    TYPE i,
         input_files     TYPE ty_input_files,
         summary         TYPE ty_summary,
         warnings        TYPE string_table,
       END OF ty_job.

*----------------------------------------------------------------------*
* Error raised by every step; carries the HTTP response body if any
*----------------------------------------------------------------------*
CLASS lcx_wtc_error DEFINITION INHERITING FROM cx_static_check FINAL.
  PUBLIC SECTION.
    DATA mv_text          TYPE string READ-ONLY.
    DATA mv_response_body TYPE string READ-ONLY.
    METHODS constructor
      IMPORTING iv_text          TYPE string
                iv_response_body TYPE string OPTIONAL.
ENDCLASS.

CLASS lcx_wtc_error IMPLEMENTATION.
  METHOD constructor.
    super->constructor( ).
    mv_text          = iv_text.
    mv_response_body = iv_response_body.
  ENDMETHOD.
ENDCLASS.

*----------------------------------------------------------------------*
* Ports: HTTP calls to the service, and files on the PC
*----------------------------------------------------------------------*
INTERFACE lif_catalog_client.
  METHODS upload_workbook
    IMPORTING iv_url         TYPE string
              iv_file_name   TYPE string
              iv_content     TYPE xstring
    RETURNING VALUE(rv_json) TYPE string
    RAISING   lcx_wtc_error.
  METHODS download
    IMPORTING iv_url            TYPE string
    RETURNING VALUE(rv_content) TYPE xstring
    RAISING   lcx_wtc_error.
  " Best effort: never raises; used to free a job whose download failed
  METHODS discard
    IMPORTING iv_url TYPE string.
ENDINTERFACE.

INTERFACE lif_file_store.
  METHODS read
    IMPORTING iv_path           TYPE string
    RETURNING VALUE(rv_content) TYPE xstring
    RAISING   lcx_wtc_error.
  METHODS write
    IMPORTING iv_path    TYPE string
              iv_content TYPE xstring
    RAISING   lcx_wtc_error.
ENDINTERFACE.

*----------------------------------------------------------------------*
* Service URLs
*----------------------------------------------------------------------*
CLASS lcl_endpoint DEFINITION FINAL.
  PUBLIC SECTION.
    CONSTANTS c_jobs_path TYPE string VALUE `/v1/catalog/jobs`.
    METHODS constructor
      IMPORTING iv_base_url TYPE string.
    METHODS jobs_url
      RETURNING VALUE(rv_url) TYPE string.
    METHODS job_url
      IMPORTING is_job        TYPE ty_job
      RETURNING VALUE(rv_url) TYPE string.
    METHODS download_url
      IMPORTING is_job        TYPE ty_job
      RETURNING VALUE(rv_url) TYPE string.
  PRIVATE SECTION.
    DATA mv_base_url TYPE string.
ENDCLASS.

CLASS lcl_endpoint IMPLEMENTATION.
  METHOD constructor.
    mv_base_url = iv_base_url.
    CONDENSE mv_base_url NO-GAPS.
    REPLACE REGEX `/+$` IN mv_base_url WITH ``.
  ENDMETHOD.

  METHOD jobs_url.
    rv_url = mv_base_url && c_jobs_path.
  ENDMETHOD.

  METHOD job_url.
    rv_url = mv_base_url && c_jobs_path && `/` && is_job-job_id.
  ENDMETHOD.

  METHOD download_url.
    " The service may return download_url as a full URL or as a path
    IF is_job-download_url IS INITIAL.
      rv_url = job_url( is_job ) && `/download`.
    ELSEIF is_job-download_url CP 'http://*' OR is_job-download_url CP 'https://*'.
      rv_url = is_job-download_url.
    ELSEIF is_job-download_url CP '/*'.
      rv_url = mv_base_url && is_job-download_url.
    ELSE.
      rv_url = mv_base_url && `/` && is_job-download_url.
    ENDIF.
  ENDMETHOD.
ENDCLASS.

*----------------------------------------------------------------------*
* Job JSON to ABAP
*----------------------------------------------------------------------*
CLASS lcl_job_parser DEFINITION FINAL.
  PUBLIC SECTION.
    METHODS parse
      IMPORTING iv_json       TYPE string
      RETURNING VALUE(rs_job) TYPE ty_job
      RAISING   lcx_wtc_error.
ENDCLASS.

CLASS lcl_job_parser IMPLEMENTATION.
  METHOD parse.
    " Unknown members (currencies, code_sources) are skipped
    /ui2/cl_json=>deserialize( EXPORTING json = iv_json
                               CHANGING  data = rs_job ).
    IF rs_job-job_id IS INITIAL.
      RAISE EXCEPTION TYPE lcx_wtc_error
        EXPORTING
          iv_text          = `The service response has no job_id`
          iv_response_body = iv_json.
    ENDIF.
  ENDMETHOD.
ENDCLASS.

*----------------------------------------------------------------------*
* HTTP adapter over IF_HTTP_CLIENT
*----------------------------------------------------------------------*
CLASS lcl_catalog_client DEFINITION FINAL.
  PUBLIC SECTION.
    INTERFACES lif_catalog_client.
    METHODS constructor
      IMPORTING iv_api_key       TYPE string
                iv_ssl_id        TYPE ssfapplssl
                iv_proxy_host    TYPE string
                iv_proxy_service TYPE string.
    " Keys look like wtc_<12 hex>_<secret>; anything else is a paste mistake
    CLASS-METHODS is_valid_api_key
      IMPORTING iv_key          TYPE string
      RETURNING VALUE(rv_valid) TYPE abap_bool.
    CLASS-METHODS normalize_api_key
      IMPORTING iv_key        TYPE csequence
      RETURNING VALUE(rv_key) TYPE string.
  PRIVATE SECTION.
    CONSTANTS c_xlsx_mime TYPE string
      VALUE `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`.
    CONSTANTS c_timeout_seconds TYPE i VALUE 120.
    TYPES: BEGIN OF ty_problem,
             code       TYPE string,
             detail     TYPE string,
             request_id TYPE string,
           END OF ty_problem.
    DATA mv_api_key       TYPE string.
    DATA mv_ssl_id        TYPE ssfapplssl.
    DATA mv_proxy_host    TYPE string.
    DATA mv_proxy_service TYPE string.
    METHODS create_client
      IMPORTING iv_url           TYPE string
      RETURNING VALUE(ro_client) TYPE REF TO if_http_client
      RAISING   lcx_wtc_error.
    METHODS send_and_receive
      IMPORTING io_client TYPE REF TO if_http_client
      RAISING   lcx_wtc_error.
    METHODS raise_transport_error
      IMPORTING io_client TYPE REF TO if_http_client
                iv_step   TYPE string
      RAISING   lcx_wtc_error.
    METHODS check_status
      IMPORTING io_client TYPE REF TO if_http_client
      RAISING   lcx_wtc_error.
    METHODS hint_for_status
      IMPORTING iv_status      TYPE i
      RETURNING VALUE(rv_hint) TYPE string.
    METHODS problem_summary
      IMPORTING iv_body        TYPE string
      RETURNING VALUE(rv_text) TYPE string.
    METHODS body_as_text
      IMPORTING io_client      TYPE REF TO if_http_client
      RETURNING VALUE(rv_text) TYPE string.
ENDCLASS.

CLASS lcl_catalog_client IMPLEMENTATION.
  METHOD constructor.
    mv_api_key       = normalize_api_key( iv_api_key ).
    mv_ssl_id        = iv_ssl_id.
    mv_proxy_host    = iv_proxy_host.
    mv_proxy_service = iv_proxy_service.
  ENDMETHOD.

  METHOD is_valid_api_key.
    rv_valid = xsdbool( matches( val   = iv_key
                                 regex = `wtc_[0-9a-f]{12}_[A-Za-z0-9_-]{20,}` ) ).
  ENDMETHOD.

  METHOD normalize_api_key.
    " A pasted key often carries spaces or a line break
    rv_key = iv_key.
    REPLACE ALL OCCURRENCES OF cl_abap_char_utilities=>cr_lf(1) IN rv_key WITH ``.
    REPLACE ALL OCCURRENCES OF cl_abap_char_utilities=>newline IN rv_key WITH ``.
    CONDENSE rv_key NO-GAPS.
  ENDMETHOD.

  METHOD lif_catalog_client~upload_workbook.
    DATA lo_client      TYPE REF TO if_http_client.
    DATA lo_part        TYPE REF TO if_http_entity.
    DATA lv_disposition TYPE string.

    lo_client = create_client( iv_url ).
    lo_client->request->set_method( if_http_request=>co_request_method_post ).
    lo_client->request->set_content_type( `multipart/form-data` ).
    lo_client->request->set_header_field( name  = `Accept`
                                          value = `application/json` ).

    " One part, form field "file", as the service's OpenAPI spec defines
    lo_part = lo_client->request->add_multipart( ).
    lv_disposition = `form-data; name="file"; filename="` && iv_file_name && `"`.
    lo_part->set_header_field( name  = `Content-Disposition`
                               value = lv_disposition ).
    lo_part->set_content_type( c_xlsx_mime ).
    lo_part->set_data( iv_content ).

    send_and_receive( lo_client ).
    rv_json = body_as_text( lo_client ).
    lo_client->close( EXCEPTIONS OTHERS = 1 ).
  ENDMETHOD.

  METHOD lif_catalog_client~download.
    DATA lo_client TYPE REF TO if_http_client.

    lo_client = create_client( iv_url ).
    lo_client->request->set_method( if_http_request=>co_request_method_get ).
    send_and_receive( lo_client ).
    rv_content = lo_client->response->get_data( ).
    lo_client->close( EXCEPTIONS OTHERS = 1 ).
  ENDMETHOD.

  METHOD lif_catalog_client~discard.
    DATA lo_client TYPE REF TO if_http_client.

    TRY.
        lo_client = create_client( iv_url ).
      CATCH lcx_wtc_error.
        RETURN.
    ENDTRY.
    lo_client->request->set_method( `DELETE` ).
    lo_client->send( EXPORTING  timeout = c_timeout_seconds
                     EXCEPTIONS OTHERS  = 1 ).
    IF sy-subrc = 0.
      " 204 = discarded, 404 = already gone; both are fine here
      lo_client->receive( EXCEPTIONS OTHERS = 1 ).
    ENDIF.
    lo_client->close( EXCEPTIONS OTHERS = 1 ).
  ENDMETHOD.

  METHOD create_client.
    DATA lv_subrc TYPE sysubrc.
    DATA lv_text  TYPE string.

    " Fail fast with a clear message instead of a 401 from the service
    IF is_valid_api_key( mv_api_key ) = abap_false.
      RAISE EXCEPTION TYPE lcx_wtc_error
        EXPORTING
          iv_text = `The API key is not a valid key (expected wtc_<12 hex>_<secret>). `
                 && `Copy it again; keys from another server will not work here.`.
    ENDIF.

    " An empty proxy host keeps the global SICF proxy settings
    cl_http_client=>create_by_url(
      EXPORTING
        url                = iv_url
        proxy_host         = mv_proxy_host
        proxy_service      = mv_proxy_service
        ssl_id             = mv_ssl_id
      IMPORTING
        client             = ro_client
      EXCEPTIONS
        argument_not_found = 1
        plugin_not_active  = 2
        internal_error     = 3
        OTHERS             = 4 ).
    lv_subrc = sy-subrc.
    IF lv_subrc <> 0.
      CASE lv_subrc.
        WHEN 1.
          lv_text = `Invalid URL: ` && iv_url.
        WHEN 2.
          lv_text = `HTTPS is not active in the ICM (check SMICM services and the SSL setup)`.
        WHEN OTHERS.
          lv_text = `CL_HTTP_CLIENT=>CREATE_BY_URL failed for ` && iv_url.
      ENDCASE.
      RAISE EXCEPTION TYPE lcx_wtc_error
        EXPORTING
          iv_text = lv_text.
    ENDIF.

    ro_client->propertytype_logon_popup = if_http_client=>co_disabled.
    ro_client->request->set_header_field( name  = `X-API-Key`
                                          value = mv_api_key ).
  ENDMETHOD.

  METHOD send_and_receive.
    io_client->send(
      EXPORTING
        timeout                    = c_timeout_seconds
      EXCEPTIONS
        http_communication_failure = 1
        http_invalid_state         = 2
        http_processing_failed     = 3
        http_invalid_timeout       = 4
        OTHERS                     = 5 ).
    IF sy-subrc <> 0.
      raise_transport_error( io_client = io_client
                             iv_step   = `send` ).
    ENDIF.

    io_client->receive(
      EXCEPTIONS
        http_communication_failure = 1
        http_invalid_state         = 2
        http_processing_failed     = 3
        OTHERS                     = 4 ).
    IF sy-subrc <> 0.
      raise_transport_error( io_client = io_client
                             iv_step   = `receive` ).
    ENDIF.

    check_status( io_client ).
  ENDMETHOD.

  METHOD raise_transport_error.
    DATA lv_code    TYPE sysubrc.
    DATA lv_message TYPE string.
    DATA lv_text    TYPE string.

    io_client->get_last_error( IMPORTING code    = lv_code
                                         message = lv_message ).
    io_client->close( EXCEPTIONS OTHERS = 1 ).
    lv_text = `HTTP ` && iv_step && ` failed: ` && lv_message
           && `. For an SSL error, check the STRUST SSL client PSE (it must trust ISRG Root X1)`
           && ` and the SMICM trace; for a timeout or refused connection, check the firewall and proxy.`.
    RAISE EXCEPTION TYPE lcx_wtc_error
      EXPORTING
        iv_text = lv_text.
  ENDMETHOD.

  METHOD check_status.
    DATA lv_status      TYPE i.
    DATA lv_reason      TYPE string.
    DATA lv_status_text TYPE string.
    DATA lv_hint        TYPE string.
    DATA lv_body        TYPE string.
    DATA lv_text        TYPE string.

    io_client->response->get_status( IMPORTING code   = lv_status
                                               reason = lv_reason ).
    IF lv_status >= 200 AND lv_status < 300.
      RETURN.
    ENDIF.

    lv_body = body_as_text( io_client ).
    io_client->close( EXCEPTIONS OTHERS = 1 ).
    lv_status_text = lv_status.
    CONDENSE lv_status_text.
    lv_hint = hint_for_status( lv_status ).
    lv_text = `HTTP ` && lv_status_text && ` ` && lv_reason && lv_hint && problem_summary( lv_body ).
    RAISE EXCEPTION TYPE lcx_wtc_error
      EXPORTING
        iv_text          = lv_text
        iv_response_body = lv_body.
  ENDMETHOD.

  METHOD hint_for_status.
    CASE iv_status.
      WHEN 400.
        rv_hint = `: the request was malformed (for a multipart upload, check the boundary)`.
      WHEN 401 OR 403.
        rv_hint = `: the API key was rejected (wrong server, revoked or expired; keys start with wtc_)`.
      WHEN 404.
        rv_hint = `: unknown job, or it was already downloaded or has expired`.
      WHEN 413.
        rv_hint = `: the file is too large`.
      WHEN 415.
        rv_hint = `: the file is not .xlsx or .csv`.
      WHEN 422.
        rv_hint = `: the file does not match the template (see the response body)`.
      WHEN 429.
        rv_hint = `: too many jobs are waiting to be downloaded, or too many failed logins`.
      WHEN 503.
        rv_hint = `: the service is busy, try again`.
      WHEN OTHERS.
        CLEAR rv_hint.
    ENDCASE.
  ENDMETHOD.

  METHOD problem_summary.
    " Errors from the service are problem+json: {"code":...,"detail":...,"request_id":...}
    DATA ls_problem TYPE ty_problem.

    TRY.
        /ui2/cl_json=>deserialize( EXPORTING json = iv_body
                                   CHANGING  data = ls_problem ).
      CATCH cx_root.
        RETURN.
    ENDTRY.
    IF ls_problem-detail IS NOT INITIAL.
      rv_text = `. ` && ls_problem-detail
             && ` [code ` && ls_problem-code
             && `, request_id ` && ls_problem-request_id && `]`.
    ENDIF.
  ENDMETHOD.

  METHOD body_as_text.
    DATA lv_raw TYPE xstring.

    lv_raw = io_client->response->get_data( ).
    TRY.
        rv_text = cl_abap_codepage=>convert_from( lv_raw ).
      CATCH cx_parameter_invalid_range
            cx_sy_codepage_converter_init
            cx_sy_conversion_codepage
            cx_parameter_invalid_type.
        rv_text = io_client->response->get_cdata( ).
    ENDTRY.
  ENDMETHOD.
ENDCLASS.

*----------------------------------------------------------------------*
* File adapter over CL_GUI_FRONTEND_SERVICES (needs SAP GUI, no batch)
*----------------------------------------------------------------------*
CLASS lcl_gui_file_store DEFINITION FINAL.
  PUBLIC SECTION.
    INTERFACES lif_file_store.
    CLASS-METHODS pick_input_file
      CHANGING cv_path TYPE c.
    CLASS-METHODS pick_output_folder
      CHANGING cv_folder TYPE c.
ENDCLASS.

CLASS lcl_gui_file_store IMPLEMENTATION.
  METHOD lif_file_store~read.
    DATA lt_data   TYPE solix_tab.
    DATA lv_length TYPE i.
    DATA lv_text   TYPE string.

    cl_gui_frontend_services=>gui_upload(
      EXPORTING
        filename   = iv_path
        filetype   = 'BIN'
      IMPORTING
        filelength = lv_length
      CHANGING
        data_tab   = lt_data
      EXCEPTIONS
        OTHERS     = 1 ).
    IF sy-subrc <> 0.
      lv_text = `Could not read the file ` && iv_path.
      RAISE EXCEPTION TYPE lcx_wtc_error
        EXPORTING
          iv_text = lv_text.
    ENDIF.
    rv_content = cl_bcs_convert=>solix_to_xstring( it_solix = lt_data
                                                   iv_size  = lv_length ).
  ENDMETHOD.

  METHOD lif_file_store~write.
    DATA lt_data TYPE solix_tab.
    DATA lv_size TYPE i.
    DATA lv_text TYPE string.

    lt_data = cl_bcs_convert=>xstring_to_solix( iv_content ).
    lv_size = xstrlen( iv_content ).
    cl_gui_frontend_services=>gui_download(
      EXPORTING
        bin_filesize = lv_size
        filename     = iv_path
        filetype     = 'BIN'
      CHANGING
        data_tab     = lt_data
      EXCEPTIONS
        OTHERS       = 1 ).
    IF sy-subrc <> 0.
      lv_text = `Could not write the file ` && iv_path.
      RAISE EXCEPTION TYPE lcx_wtc_error
        EXPORTING
          iv_text = lv_text.
    ENDIF.
  ENDMETHOD.

  METHOD pick_input_file.
    DATA lt_files  TYPE filetable.
    DATA ls_file   TYPE file_table.
    DATA lv_rc     TYPE i.
    DATA lv_action TYPE i.

    cl_gui_frontend_services=>file_open_dialog(
      EXPORTING
        window_title = `Payslip Master Data workbook`
        file_filter  = `Excel workbook (*.xlsx)|*.xlsx`
      CHANGING
        file_table   = lt_files
        rc           = lv_rc
        user_action  = lv_action
      EXCEPTIONS
        OTHERS       = 1 ).
    IF sy-subrc <> 0 OR lv_action <> cl_gui_frontend_services=>action_ok.
      RETURN.
    ENDIF.
    READ TABLE lt_files INTO ls_file INDEX 1.
    IF sy-subrc = 0.
      cv_path = ls_file-filename.
    ENDIF.
  ENDMETHOD.

  METHOD pick_output_folder.
    DATA lv_folder TYPE string.

    cl_gui_frontend_services=>directory_browse(
      EXPORTING
        window_title    = `Folder for the generated catalog`
      CHANGING
        selected_folder = lv_folder
      EXCEPTIONS
        OTHERS          = 1 ).
    IF sy-subrc = 0 AND lv_folder IS NOT INITIAL.
      cv_folder = lv_folder.
    ENDIF.
  ENDMETHOD.
ENDCLASS.

*----------------------------------------------------------------------*
* Model: upload the workbook, then fetch and save the catalog
*----------------------------------------------------------------------*
CLASS lcl_catalog_flow DEFINITION FINAL.
  PUBLIC SECTION.
    TYPES: BEGIN OF ty_submission,
             input_bytes TYPE i,
             raw_json    TYPE string,
             job         TYPE ty_job,
           END OF ty_submission.
    TYPES: BEGIN OF ty_saved_file,
             path  TYPE string,
             bytes TYPE i,
           END OF ty_saved_file.
    METHODS constructor
      IMPORTING io_client   TYPE REF TO lif_catalog_client
                io_files    TYPE REF TO lif_file_store
                iv_base_url TYPE string.
    METHODS submit
      IMPORTING iv_input_path        TYPE string
      RETURNING VALUE(rs_submission) TYPE ty_submission
      RAISING   lcx_wtc_error.
    METHODS fetch_catalog
      IMPORTING is_job           TYPE ty_job
                iv_output_folder TYPE string
      RETURNING VALUE(rs_saved)  TYPE ty_saved_file
      RAISING   lcx_wtc_error.
    METHODS discard
      IMPORTING is_job TYPE ty_job.
    CLASS-METHODS file_name_of
      IMPORTING iv_path             TYPE string
      RETURNING VALUE(rv_file_name) TYPE string.
    CLASS-METHODS is_workbook
      IMPORTING iv_path        TYPE csequence
      RETURNING VALUE(rv_ok)   TYPE abap_bool.
  PRIVATE SECTION.
    CONSTANTS c_default_output TYPE string VALUE `wage_type_catalog.xlsx`.
    CONSTANTS c_status_ready   TYPE string VALUE `ready`.
    DATA mo_client   TYPE REF TO lif_catalog_client.
    DATA mo_files    TYPE REF TO lif_file_store.
    DATA mo_endpoint TYPE REF TO lcl_endpoint.
    DATA mo_parser   TYPE REF TO lcl_job_parser.
    METHODS join_path
      IMPORTING iv_folder      TYPE string
                iv_file_name   TYPE string
      RETURNING VALUE(rv_path) TYPE string.
ENDCLASS.

CLASS lcl_catalog_flow IMPLEMENTATION.
  METHOD constructor.
    mo_client = io_client.
    mo_files  = io_files.
    CREATE OBJECT mo_endpoint
      EXPORTING
        iv_base_url = iv_base_url.
    CREATE OBJECT mo_parser.
  ENDMETHOD.

  METHOD submit.
    DATA lv_content   TYPE xstring.
    DATA lv_file_name TYPE string.
    DATA lv_url       TYPE string.
    DATA lv_text      TYPE string.

    lv_file_name = file_name_of( iv_input_path ).
    " This program supports workbook mode only; CSV needs a second Master Data file
    IF is_workbook( lv_file_name ) = abap_false.
      RAISE EXCEPTION TYPE lcx_wtc_error
        EXPORTING
          iv_text = `Select the Payslip Master Data workbook (.xlsx). `
                 && `CSV needs two files and is not supported by this program.`.
    ENDIF.

    lv_content = mo_files->read( iv_input_path ).
    rs_submission-input_bytes = xstrlen( lv_content ).
    lv_url = mo_endpoint->jobs_url( ).
    rs_submission-raw_json = mo_client->upload_workbook( iv_url       = lv_url
                                                         iv_file_name = lv_file_name
                                                         iv_content   = lv_content ).
    rs_submission-job = mo_parser->parse( rs_submission-raw_json ).

    " Status defaults to ready in the schema, so a missing status is fine
    IF rs_submission-job-status IS NOT INITIAL
       AND rs_submission-job-status <> c_status_ready.
      lv_text = `Job ` && rs_submission-job-job_id && ` has status "`
             && rs_submission-job-status && `", expected "ready"`.
      RAISE EXCEPTION TYPE lcx_wtc_error
        EXPORTING
          iv_text          = lv_text
          iv_response_body = rs_submission-raw_json.
    ENDIF.
  ENDMETHOD.

  METHOD fetch_catalog.
    DATA lv_url       TYPE string.
    DATA lv_content   TYPE xstring.
    DATA lv_file_name TYPE string.

    lv_url = mo_endpoint->download_url( is_job ).
    lv_content = mo_client->download( lv_url ).
    IF lv_content IS INITIAL.
      RAISE EXCEPTION TYPE lcx_wtc_error
        EXPORTING
          iv_text = `The download returned an empty file`.
    ENDIF.

    " Keep only the file name the service suggests, never a path
    lv_file_name = file_name_of( is_job-output_filename ).
    IF lv_file_name IS INITIAL.
      lv_file_name = c_default_output.
    ENDIF.
    rs_saved-path  = join_path( iv_folder    = iv_output_folder
                                iv_file_name = lv_file_name ).
    rs_saved-bytes = xstrlen( lv_content ).
    mo_files->write( iv_path    = rs_saved-path
                     iv_content = lv_content ).
  ENDMETHOD.

  METHOD discard.
    mo_client->discard( mo_endpoint->job_url( is_job ) ).
  ENDMETHOD.

  METHOD file_name_of.
    DATA lv_path  TYPE string.
    DATA lt_parts TYPE string_table.
    DATA lv_count TYPE i.

    lv_path = iv_path.
    REPLACE ALL OCCURRENCES OF `/` IN lv_path WITH `\`.
    SPLIT lv_path AT `\` INTO TABLE lt_parts.
    lv_count = lines( lt_parts ).
    IF lv_count > 0.
      READ TABLE lt_parts INTO rv_file_name INDEX lv_count.
    ENDIF.
  ENDMETHOD.

  METHOD is_workbook.
    DATA lv_path TYPE string.

    lv_path = iv_path.                     " c -> string drops the trailing blanks of p_file
    " CP is case-insensitive, so .XLSX is accepted too
    rv_ok = xsdbool( lv_path CP '*.xlsx' ).
  ENDMETHOD.

  METHOD join_path.
    DATA lv_length TYPE i.
    DATA lv_offset TYPE i.
    DATA lv_last   TYPE c LENGTH 1.

    lv_length = strlen( iv_folder ).
    IF lv_length = 0.
      rv_path = iv_file_name.
      RETURN.
    ENDIF.
    lv_offset = lv_length - 1.
    lv_last = iv_folder+lv_offset(1).
    IF lv_last = '\' OR lv_last = '/'.
      rv_path = iv_folder && iv_file_name.
    ELSE.
      rv_path = iv_folder && `\` && iv_file_name.
    ENDIF.
  ENDMETHOD.
ENDCLASS.

*----------------------------------------------------------------------*
* View: classic list output
*----------------------------------------------------------------------*
CLASS lcl_list_view DEFINITION FINAL.
  PUBLIC SECTION.
    METHODS show_submission
      IMPORTING is_submission TYPE lcl_catalog_flow=>ty_submission.
    METHODS show_saved
      IMPORTING is_saved TYPE lcl_catalog_flow=>ty_saved_file.
    METHODS show_error
      IMPORTING io_error TYPE REF TO lcx_wtc_error.
  PRIVATE SECTION.
    CONSTANTS c_chunk_length TYPE i VALUE 200.
    METHODS write_heading
      IMPORTING iv_text TYPE string.
    METHODS write_value
      IMPORTING iv_label TYPE string
                iv_value TYPE string.
    METHODS write_number
      IMPORTING iv_label TYPE string
                iv_value TYPE i.
    METHODS write_long
      IMPORTING iv_text TYPE string.
ENDCLASS.

CLASS lcl_list_view IMPLEMENTATION.
  METHOD show_submission.
    DATA lv_molgas   TYPE string.
    DATA lv_warning  TYPE string.
    DATA lv_warnings TYPE i.

    write_heading( `1. Upload: POST /v1/catalog/jobs` ).
    write_number( iv_label = `Input bytes`
                  iv_value = is_submission-input_bytes ).
    write_value( iv_label = `Job ID`
                 iv_value = is_submission-job-job_id ).
    write_value( iv_label = `Status`
                 iv_value = is_submission-job-status ).
    write_value( iv_label = `Created at`
                 iv_value = is_submission-job-created_at ).
    write_value( iv_label = `Expires at`
                 iv_value = is_submission-job-expires_at ).
    write_value( iv_label = `Download URL`
                 iv_value = is_submission-job-download_url ).
    write_value( iv_label = `Output file`
                 iv_value = is_submission-job-output_filename ).
    write_number( iv_label = `Output bytes`
                  iv_value = is_submission-job-output_bytes ).
    write_number( iv_label = `Payslips`
                  iv_value = is_submission-job-summary-payslips ).
    write_number( iv_label = `Line items`
                  iv_value = is_submission-job-summary-line_items ).
    write_number( iv_label = `Wage types`
                  iv_value = is_submission-job-summary-wage_types ).
    write_number( iv_label = `Provisional codes`
                  iv_value = is_submission-job-summary-provisional_codes ).
    CONCATENATE LINES OF is_submission-job-summary-molgas INTO lv_molgas SEPARATED BY `, `.
    write_value( iv_label = `MOLGAs`
                 iv_value = lv_molgas ).
    lv_warnings = lines( is_submission-job-warnings ).
    write_number( iv_label = `Warnings`
                  iv_value = lv_warnings ).
    LOOP AT is_submission-job-warnings INTO lv_warning.
      write_long( lv_warning ).
    ENDLOOP.
    SKIP.
    WRITE / 'Raw response:'.
    write_long( is_submission-raw_json ).
    SKIP.
  ENDMETHOD.

  METHOD show_saved.
    write_heading( `2. Download: GET download_url` ).
    write_value( iv_label = `Saved to`
                 iv_value = is_saved-path ).
    write_number( iv_label = `Bytes`
                  iv_value = is_saved-bytes ).
    SKIP.
    FORMAT COLOR COL_POSITIVE.
    WRITE / 'Round trip complete.'.
    FORMAT COLOR OFF.
  ENDMETHOD.

  METHOD show_error.
    SKIP.
    FORMAT COLOR COL_NEGATIVE.
    WRITE / 'Error'.
    FORMAT COLOR OFF.
    write_long( io_error->mv_text ).
    IF io_error->mv_response_body IS NOT INITIAL.
      SKIP.
      WRITE / 'Response body:'.
      write_long( io_error->mv_response_body ).
    ENDIF.
  ENDMETHOD.

  METHOD write_heading.
    FORMAT COLOR COL_HEADING.
    WRITE / iv_text.
    FORMAT COLOR OFF.
  ENDMETHOD.

  METHOD write_value.
    DATA lv_label TYPE c LENGTH 20.

    lv_label = iv_label.
    WRITE: / lv_label, iv_value.
  ENDMETHOD.

  METHOD write_number.
    DATA lv_text TYPE string.

    lv_text = iv_value.
    CONDENSE lv_text.
    write_value( iv_label = iv_label
                 iv_value = lv_text ).
  ENDMETHOD.

  METHOD write_long.
    DATA lv_length TYPE i.
    DATA lv_offset TYPE i.
    DATA lv_take   TYPE i.
    DATA lv_chunk  TYPE string.

    lv_length = strlen( iv_text ).
    WHILE lv_offset < lv_length.
      lv_take = lv_length - lv_offset.
      IF lv_take > c_chunk_length.
        lv_take = c_chunk_length.
      ENDIF.
      lv_chunk = iv_text+lv_offset(lv_take).
      WRITE / lv_chunk.
      lv_offset = lv_offset + lv_take.
    ENDWHILE.
  ENDMETHOD.
ENDCLASS.

*----------------------------------------------------------------------*
* Controller: wires the objects and runs the two steps
*----------------------------------------------------------------------*
CLASS lcl_controller DEFINITION FINAL.
  PUBLIC SECTION.
    CLASS-METHODS create
      IMPORTING iv_base_url          TYPE csequence
                iv_api_key           TYPE csequence
                iv_ssl_id            TYPE ssfapplssl
                iv_proxy_host        TYPE csequence
                iv_proxy_service     TYPE csequence
      RETURNING VALUE(ro_controller) TYPE REF TO lcl_controller.
    METHODS constructor
      IMPORTING io_flow TYPE REF TO lcl_catalog_flow
                io_view TYPE REF TO lcl_list_view.
    METHODS run
      IMPORTING iv_input_path    TYPE csequence
                iv_output_folder TYPE csequence.
  PRIVATE SECTION.
    DATA mo_flow TYPE REF TO lcl_catalog_flow.
    DATA mo_view TYPE REF TO lcl_list_view.
ENDCLASS.

CLASS lcl_controller IMPLEMENTATION.
  METHOD create.
    DATA lo_client        TYPE REF TO lcl_catalog_client.
    DATA lo_files         TYPE REF TO lcl_gui_file_store.
    DATA lo_flow          TYPE REF TO lcl_catalog_flow.
    DATA lo_view          TYPE REF TO lcl_list_view.
    DATA lv_base_url      TYPE string.
    DATA lv_api_key       TYPE string.
    DATA lv_proxy_host    TYPE string.
    DATA lv_proxy_service TYPE string.

    lv_base_url      = iv_base_url.
    lv_api_key       = iv_api_key.
    lv_proxy_host    = iv_proxy_host.
    lv_proxy_service = iv_proxy_service.

    CREATE OBJECT lo_client
      EXPORTING
        iv_api_key       = lv_api_key
        iv_ssl_id        = iv_ssl_id
        iv_proxy_host    = lv_proxy_host
        iv_proxy_service = lv_proxy_service.
    CREATE OBJECT lo_files.
    CREATE OBJECT lo_flow
      EXPORTING
        io_client   = lo_client
        io_files    = lo_files
        iv_base_url = lv_base_url.
    CREATE OBJECT lo_view.
    CREATE OBJECT ro_controller
      EXPORTING
        io_flow = lo_flow
        io_view = lo_view.
  ENDMETHOD.

  METHOD constructor.
    mo_flow = io_flow.
    mo_view = io_view.
  ENDMETHOD.

  METHOD run.
    DATA lv_input_path    TYPE string.
    DATA lv_output_folder TYPE string.
    DATA ls_submission    TYPE lcl_catalog_flow=>ty_submission.
    DATA ls_saved         TYPE lcl_catalog_flow=>ty_saved_file.
    DATA lx_error         TYPE REF TO lcx_wtc_error.

    lv_input_path    = iv_input_path.
    lv_output_folder = iv_output_folder.
    TRY.
        ls_submission = mo_flow->submit( lv_input_path ).
        mo_view->show_submission( ls_submission ).
        TRY.
            ls_saved = mo_flow->fetch_catalog( is_job           = ls_submission-job
                                               iv_output_folder = lv_output_folder ).
          CATCH lcx_wtc_error INTO lx_error.
            " Free the server-side job so reruns do not hit the pending-jobs limit (HTTP 429)
            mo_flow->discard( ls_submission-job ).
            RAISE EXCEPTION lx_error.
        ENDTRY.
        mo_view->show_saved( ls_saved ).
      CATCH lcx_wtc_error INTO lx_error.
        mo_view->show_error( lx_error ).
    ENDTRY.
  ENDMETHOD.
ENDCLASS.

*----------------------------------------------------------------------*
* Selection screen
*----------------------------------------------------------------------*
SELECTION-SCREEN BEGIN OF BLOCK b_api WITH FRAME.
PARAMETERS: p_url    TYPE c LENGTH 255 VISIBLE LENGTH 60 LOWER CASE OBLIGATORY
                     DEFAULT 'https://13-62-135-9.sslip.io',
            p_apikey TYPE c LENGTH 128 VISIBLE LENGTH 60 LOWER CASE OBLIGATORY,
            p_sslid  TYPE ssfapplssl DEFAULT 'ANONYM',
            p_phost  TYPE c LENGTH 128 VISIBLE LENGTH 40 LOWER CASE,
            p_pserv  TYPE c LENGTH 5.
SELECTION-SCREEN END OF BLOCK b_api.

SELECTION-SCREEN BEGIN OF BLOCK b_file WITH FRAME.
PARAMETERS: p_file   TYPE c LENGTH 255 VISIBLE LENGTH 60 LOWER CASE OBLIGATORY,
            p_outdir TYPE c LENGTH 255 VISIBLE LENGTH 60 LOWER CASE OBLIGATORY.
SELECTION-SCREEN END OF BLOCK b_file.

DATA go_controller TYPE REF TO lcl_controller.
DATA gv_api_key    TYPE string.

INITIALIZATION.
  " Labels set here so the program needs no text elements after a paste
  %_p_url_%_app_%-text    = 'API base URL'.
  %_p_apikey_%_app_%-text = 'API key (X-API-Key)'.
  %_p_sslid_%_app_%-text  = 'STRUST SSL client ID'.
  %_p_phost_%_app_%-text  = 'Proxy host (optional)'.
  %_p_pserv_%_app_%-text  = 'Proxy port (optional)'.
  %_p_file_%_app_%-text   = 'Payslip Master Data'.
  %_p_outdir_%_app_%-text = 'Output folder'.

AT SELECTION-SCREEN OUTPUT.
  " Show the API key as asterisks
  LOOP AT SCREEN.
    IF screen-name = 'P_APIKEY'.
      screen-invisible = '1'.
      MODIFY SCREEN.
    ENDIF.
  ENDLOOP.

AT SELECTION-SCREEN ON p_apikey.
  " Check the key format on the screen, before any HTTP call
  gv_api_key = lcl_catalog_client=>normalize_api_key( p_apikey ).
  IF lcl_catalog_client=>is_valid_api_key( gv_api_key ) = abap_false.
    MESSAGE `The API key is not valid: it must look like wtc_<12 hex>_<secret>` TYPE 'E'.
  ENDIF.

AT SELECTION-SCREEN ON p_file.
  IF lcl_catalog_flow=>is_workbook( p_file ) = abap_false.
    MESSAGE `Select the Payslip Master Data workbook (.xlsx)` TYPE 'E'.
  ENDIF.

AT SELECTION-SCREEN ON VALUE-REQUEST FOR p_file.
  lcl_gui_file_store=>pick_input_file( CHANGING cv_path = p_file ).

AT SELECTION-SCREEN ON VALUE-REQUEST FOR p_outdir.
  lcl_gui_file_store=>pick_output_folder( CHANGING cv_folder = p_outdir ).

START-OF-SELECTION.
  go_controller = lcl_controller=>create( iv_base_url      = p_url
                                          iv_api_key       = p_apikey
                                          iv_ssl_id        = p_sslid
                                          iv_proxy_host    = p_phost
                                          iv_proxy_service = p_pserv ).
  go_controller->run( iv_input_path    = p_file
                      iv_output_folder = p_outdir ).

*----------------------------------------------------------------------*
* Unit tests: JSON mapping, URL handling, input checks and the flow
*----------------------------------------------------------------------*
CLASS lth_fixture DEFINITION FOR TESTING FINAL.
  PUBLIC SECTION.
    " Well-formed but fake: never put a real key in source code
    CONSTANTS c_fake_api_key TYPE string
      VALUE `wtc_0123456789ab_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_AbCdE`.
    CLASS-METHODS job_json
      IMPORTING iv_download_url TYPE string
      RETURNING VALUE(rv_json)  TYPE string.
ENDCLASS.

CLASS lth_fixture IMPLEMENTATION.
  METHOD job_json.
    " Mirrors a real POST /v1/catalog/jobs response
    rv_json = `{"job_id":"j-123","status":"ready",`
           && `"created_at":"2026-09-30T08:00:00Z","expires_at":"2026-09-30T09:00:00Z",`
           && `"download_url":"` && iv_download_url && `",`
           && `"input_files":[{"name":"Payslip_Demo_Master_Data_updated.xlsx","bytes":32184}],`
           && `"output_filename":"wage_type_catalog_j-123.xlsx","output_bytes":2048,`
           && `"summary":{"payslips":10,"line_items":164,"wage_types":20,"provisional_codes":0,`
           && `"molgas":["10"],"currencies":{"10":"USD"},`
           && `"code_sources":{"input":164,"mapping":0,"provisional":0}},`
           && `"warnings":["first warning","second warning"]}`.
  ENDMETHOD.
ENDCLASS.

CLASS ltd_catalog_client DEFINITION FOR TESTING FINAL.
  PUBLIC SECTION.
    INTERFACES lif_catalog_client.
    DATA mv_job_json     TYPE string.
    DATA mv_catalog      TYPE xstring.
    DATA mv_upload_url   TYPE string.
    DATA mv_file_name    TYPE string.
    DATA mv_download_url TYPE string.
    DATA mv_discard_url  TYPE string.
ENDCLASS.

CLASS ltd_catalog_client IMPLEMENTATION.
  METHOD lif_catalog_client~upload_workbook.
    mv_upload_url = iv_url.
    mv_file_name  = iv_file_name.
    rv_json       = mv_job_json.
  ENDMETHOD.

  METHOD lif_catalog_client~download.
    mv_download_url = iv_url.
    rv_content      = mv_catalog.
  ENDMETHOD.

  METHOD lif_catalog_client~discard.
    mv_discard_url = iv_url.
  ENDMETHOD.
ENDCLASS.

CLASS ltd_file_store DEFINITION FOR TESTING FINAL.
  PUBLIC SECTION.
    INTERFACES lif_file_store.
    DATA mv_input           TYPE xstring.
    DATA mv_read_called     TYPE abap_bool.
    DATA mv_written_path    TYPE string.
    DATA mv_written_content TYPE xstring.
ENDCLASS.

CLASS ltd_file_store IMPLEMENTATION.
  METHOD lif_file_store~read.
    mv_read_called = abap_true.
    rv_content     = mv_input.
  ENDMETHOD.

  METHOD lif_file_store~write.
    mv_written_path    = iv_path.
    mv_written_content = iv_content.
  ENDMETHOD.
ENDCLASS.

CLASS ltc_job_parser DEFINITION FOR TESTING RISK LEVEL HARMLESS DURATION SHORT FINAL.
  PRIVATE SECTION.
    METHODS parse_reads_job_fields FOR TESTING RAISING lcx_wtc_error.
    METHODS parse_without_job_id_raises FOR TESTING.
ENDCLASS.

CLASS ltc_job_parser IMPLEMENTATION.
  METHOD parse_reads_job_fields.
    DATA lo_parser   TYPE REF TO lcl_job_parser.
    DATA lv_json     TYPE string.
    DATA ls_job      TYPE ty_job.
    DATA lv_warnings TYPE i.
    DATA lv_molga    TYPE string.

    CREATE OBJECT lo_parser.
    lv_json = lth_fixture=>job_json( `/v1/catalog/jobs/j-123/download` ).
    ls_job = lo_parser->parse( lv_json ).

    cl_abap_unit_assert=>assert_equals( act = ls_job-job_id
                                        exp = `j-123` ).
    cl_abap_unit_assert=>assert_equals( act = ls_job-status
                                        exp = `ready` ).
    cl_abap_unit_assert=>assert_equals( act = ls_job-download_url
                                        exp = `/v1/catalog/jobs/j-123/download` ).
    cl_abap_unit_assert=>assert_equals( act = ls_job-output_filename
                                        exp = `wage_type_catalog_j-123.xlsx` ).
    cl_abap_unit_assert=>assert_equals( act = ls_job-output_bytes
                                        exp = 2048 ).
    cl_abap_unit_assert=>assert_equals( act = ls_job-summary-wage_types
                                        exp = 20 ).
    lv_warnings = lines( ls_job-warnings ).
    cl_abap_unit_assert=>assert_equals( act = lv_warnings
                                        exp = 2 ).
    READ TABLE ls_job-summary-molgas INTO lv_molga INDEX 1.
    cl_abap_unit_assert=>assert_equals( act = lv_molga
                                        exp = `10` ).
  ENDMETHOD.

  METHOD parse_without_job_id_raises.
    DATA lo_parser TYPE REF TO lcl_job_parser.
    DATA ls_job    TYPE ty_job.

    CREATE OBJECT lo_parser.
    TRY.
        ls_job = lo_parser->parse( `{"status":"ready"}` ).
        cl_abap_unit_assert=>fail( msg = `A response without job_id must raise` ).
      CATCH lcx_wtc_error ##NO_HANDLER.
    ENDTRY.
  ENDMETHOD.
ENDCLASS.

CLASS ltc_endpoint DEFINITION FOR TESTING RISK LEVEL HARMLESS DURATION SHORT FINAL.
  PRIVATE SECTION.
    DATA mo_cut TYPE REF TO lcl_endpoint.
    METHODS setup.
    METHODS trailing_slash_is_dropped FOR TESTING.
    METHODS relative_download_url FOR TESTING.
    METHODS absolute_download_url_kept FOR TESTING.
    METHODS missing_download_url_built FOR TESTING.
    METHODS job_url_for_discard FOR TESTING.
ENDCLASS.

CLASS ltc_endpoint IMPLEMENTATION.
  METHOD setup.
    CREATE OBJECT mo_cut
      EXPORTING
        iv_base_url = `https://wtc.example//`.
  ENDMETHOD.

  METHOD trailing_slash_is_dropped.
    DATA lv_url TYPE string.

    lv_url = mo_cut->jobs_url( ).
    cl_abap_unit_assert=>assert_equals( act = lv_url
                                        exp = `https://wtc.example/v1/catalog/jobs` ).
  ENDMETHOD.

  METHOD relative_download_url.
    DATA ls_job TYPE ty_job.
    DATA lv_url TYPE string.

    ls_job-download_url = `/v1/catalog/jobs/j-123/download`.
    lv_url = mo_cut->download_url( ls_job ).
    cl_abap_unit_assert=>assert_equals( act = lv_url
                                        exp = `https://wtc.example/v1/catalog/jobs/j-123/download` ).
  ENDMETHOD.

  METHOD absolute_download_url_kept.
    DATA ls_job TYPE ty_job.
    DATA lv_url TYPE string.

    ls_job-download_url = `https://other.example/v1/catalog/jobs/j-123/download`.
    lv_url = mo_cut->download_url( ls_job ).
    cl_abap_unit_assert=>assert_equals( act = lv_url
                                        exp = ls_job-download_url ).
  ENDMETHOD.

  METHOD missing_download_url_built.
    DATA ls_job TYPE ty_job.
    DATA lv_url TYPE string.

    ls_job-job_id = `j-123`.
    lv_url = mo_cut->download_url( ls_job ).
    cl_abap_unit_assert=>assert_equals( act = lv_url
                                        exp = `https://wtc.example/v1/catalog/jobs/j-123/download` ).
  ENDMETHOD.

  METHOD job_url_for_discard.
    DATA ls_job TYPE ty_job.
    DATA lv_url TYPE string.

    ls_job-job_id = `j-123`.
    lv_url = mo_cut->job_url( ls_job ).
    cl_abap_unit_assert=>assert_equals( act = lv_url
                                        exp = `https://wtc.example/v1/catalog/jobs/j-123` ).
  ENDMETHOD.
ENDCLASS.

CLASS ltc_api_key DEFINITION FOR TESTING RISK LEVEL HARMLESS DURATION SHORT FINAL.
  PRIVATE SECTION.
    METHODS valid_key_accepted FOR TESTING.
    METHODS pasted_spaces_removed FOR TESTING.
    METHODS wrong_values_rejected FOR TESTING.
ENDCLASS.

CLASS ltc_api_key IMPLEMENTATION.
  METHOD valid_key_accepted.
    cl_abap_unit_assert=>assert_true(
      lcl_catalog_client=>is_valid_api_key( lth_fixture=>c_fake_api_key ) ).
  ENDMETHOD.

  METHOD pasted_spaces_removed.
    DATA lv_key TYPE string.

    lv_key = lcl_catalog_client=>normalize_api_key( ` ` && lth_fixture=>c_fake_api_key && `  ` ).
    cl_abap_unit_assert=>assert_equals( act = lv_key
                                        exp = lth_fixture=>c_fake_api_key ).
  ENDMETHOD.

  METHOD wrong_values_rejected.
    " The kinds of values that actually reached the service as "API keys"
    cl_abap_unit_assert=>assert_false(
      lcl_catalog_client=>is_valid_api_key( `https://13-62-135-9.sslip.io/docs` ) ).
    cl_abap_unit_assert=>assert_false(
      lcl_catalog_client=>is_valid_api_key( `hCVgTFqfWu8L2W29abcd3ho9` ) ).
    cl_abap_unit_assert=>assert_false(
      lcl_catalog_client=>is_valid_api_key( `` ) ).
  ENDMETHOD.
ENDCLASS.

CLASS ltc_catalog_flow DEFINITION FOR TESTING RISK LEVEL HARMLESS DURATION SHORT FINAL.
  PRIVATE SECTION.
    DATA mo_client TYPE REF TO ltd_catalog_client.
    DATA mo_files  TYPE REF TO ltd_file_store.
    DATA mo_cut    TYPE REF TO lcl_catalog_flow.
    METHODS setup.
    METHODS round_trip_saves_catalog FOR TESTING RAISING lcx_wtc_error.
    METHODS not_ready_status_raises FOR TESTING.
    METHODS csv_input_rejected FOR TESTING.
    METHODS discard_targets_job FOR TESTING.
    METHODS file_name_of_windows_path FOR TESTING.
ENDCLASS.

CLASS ltc_catalog_flow IMPLEMENTATION.
  METHOD setup.
    CREATE OBJECT mo_client.
    CREATE OBJECT mo_files.
    mo_files->mv_input  = '504B0304'.
    mo_client->mv_catalog = '504B030414'.
    CREATE OBJECT mo_cut
      EXPORTING
        io_client   = mo_client
        io_files    = mo_files
        iv_base_url = `https://wtc.example`.
  ENDMETHOD.

  METHOD round_trip_saves_catalog.
    DATA ls_submission TYPE lcl_catalog_flow=>ty_submission.
    DATA ls_saved      TYPE lcl_catalog_flow=>ty_saved_file.

    mo_client->mv_job_json = lth_fixture=>job_json( `/v1/catalog/jobs/j-123/download` ).
    ls_submission = mo_cut->submit( `C:\data\Payslip_Demo_Master_Data_updated.xlsx` ).
    ls_saved = mo_cut->fetch_catalog( is_job           = ls_submission-job
                                      iv_output_folder = `C:\out` ).

    cl_abap_unit_assert=>assert_equals( act = mo_client->mv_upload_url
                                        exp = `https://wtc.example/v1/catalog/jobs` ).
    cl_abap_unit_assert=>assert_equals( act = mo_client->mv_file_name
                                        exp = `Payslip_Demo_Master_Data_updated.xlsx` ).
    cl_abap_unit_assert=>assert_equals( act = ls_submission-input_bytes
                                        exp = 4 ).
    cl_abap_unit_assert=>assert_equals( act = mo_client->mv_download_url
                                        exp = `https://wtc.example/v1/catalog/jobs/j-123/download` ).
    cl_abap_unit_assert=>assert_equals( act = ls_saved-path
                                        exp = `C:\out\wage_type_catalog_j-123.xlsx` ).
    cl_abap_unit_assert=>assert_equals( act = mo_files->mv_written_path
                                        exp = ls_saved-path ).
    cl_abap_unit_assert=>assert_equals( act = mo_files->mv_written_content
                                        exp = mo_client->mv_catalog ).
    cl_abap_unit_assert=>assert_equals( act = ls_saved-bytes
                                        exp = 5 ).
  ENDMETHOD.

  METHOD not_ready_status_raises.
    DATA ls_submission TYPE lcl_catalog_flow=>ty_submission.

    mo_client->mv_job_json = `{"job_id":"j-9","status":"failed"}`.
    TRY.
        ls_submission = mo_cut->submit( `C:\data\in.xlsx` ).
        cl_abap_unit_assert=>fail( msg = `A job that is not ready must raise` ).
      CATCH lcx_wtc_error ##NO_HANDLER.
    ENDTRY.
  ENDMETHOD.

  METHOD csv_input_rejected.
    DATA ls_submission TYPE lcl_catalog_flow=>ty_submission.

    TRY.
        ls_submission = mo_cut->submit( `C:\data\lines.csv` ).
        cl_abap_unit_assert=>fail( msg = `A CSV input must raise` ).
      CATCH lcx_wtc_error ##NO_HANDLER.
    ENDTRY.
    " Rejected before anything is read or sent
    cl_abap_unit_assert=>assert_false( mo_files->mv_read_called ).
    cl_abap_unit_assert=>assert_initial( mo_client->mv_upload_url ).
  ENDMETHOD.

  METHOD discard_targets_job.
    DATA ls_job TYPE ty_job.

    ls_job-job_id = `j-123`.
    mo_cut->discard( ls_job ).
    cl_abap_unit_assert=>assert_equals( act = mo_client->mv_discard_url
                                        exp = `https://wtc.example/v1/catalog/jobs/j-123` ).
  ENDMETHOD.

  METHOD file_name_of_windows_path.
    DATA lv_name TYPE string.

    lv_name = lcl_catalog_flow=>file_name_of( `C:\Users\me/Downloads\book.xlsx` ).
    cl_abap_unit_assert=>assert_equals( act = lv_name
                                        exp = `book.xlsx` ).
  ENDMETHOD.
ENDCLASS.
