-- Existing installations: connect as BRK_COD in Toad and execute as script (F5).
-- Adds only missing columns; existing rows and column values are preserved.
-- DDL commits implicitly. Stop workers before applying, then restart them.
WHENEVER SQLERROR EXIT SQL.SQLCODE
SET SERVEROUTPUT ON

DECLARE
  PROCEDURE add_if_missing(column_name_in VARCHAR2, definition_in VARCHAR2) IS
    column_count NUMBER;
  BEGIN
    SELECT COUNT(*) INTO column_count FROM USER_TAB_COLUMNS
      WHERE TABLE_NAME = 'CLIENT_IMAGE_OCR_JOB' AND COLUMN_NAME = column_name_in;
    IF column_count = 0 THEN
      EXECUTE IMMEDIATE 'ALTER TABLE BRK_COD.CLIENT_IMAGE_OCR_JOB ADD ('
        || column_name_in || ' ' || definition_in || ')';
      DBMS_OUTPUT.PUT_LINE('Added ' || column_name_in);
    ELSE
      DBMS_OUTPUT.PUT_LINE('Already exists: ' || column_name_in);
    END IF;
  END;
BEGIN
  IF USER <> 'BRK_COD' THEN
    RAISE_APPLICATION_ERROR(-20001, 'Connect as BRK_COD before running this script');
  END IF;
  add_if_missing('MARITAL_STATUS', 'VARCHAR2(100 BYTE)');
  add_if_missing('RELIGION', 'VARCHAR2(100 BYTE)');
  add_if_missing('EXTRACTED_JSON', 'CLOB');
END;
/

-- Verify VARCHAR2 / 100 BYTE for the labels and CLOB for JSON.
-- If an existing column has another type, report it before starting workers.
SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH, CHAR_USED
FROM USER_TAB_COLUMNS
WHERE TABLE_NAME = 'CLIENT_IMAGE_OCR_JOB'
  AND COLUMN_NAME IN ('MARITAL_STATUS', 'RELIGION', 'EXTRACTED_JSON')
ORDER BY COLUMN_NAME;
