-- Toad F5 as BRK_COD. Stop ALL writers first; keep them stopped during migration.
-- Preserves existing columns as *_OLD. No table/column is dropped.
-- DDL commits implicitly: inspect partial installations before retrying.
WHENEVER SQLERROR EXIT SQL.SQLCODE
SET SERVEROUTPUT ON

DECLARE
  PROCEDURE convert_column(col VARCHAR2, old_type VARCHAR2,
                           new_type VARCHAR2, definition VARCHAR2,
                           conversion VARCHAR2) IS
    actual_type VARCHAR2(30);
    conflicts NUMBER;
  BEGIN
    SELECT DATA_TYPE INTO actual_type FROM USER_TAB_COLUMNS
      WHERE TABLE_NAME = 'CLIENT_IMAGE_OCR_JOB' AND COLUMN_NAME = col;
    IF actual_type = new_type THEN
      DBMS_OUTPUT.PUT_LINE('Already Unicode: ' || col);
      RETURN;
    END IF;
    IF actual_type <> old_type THEN
      RAISE_APPLICATION_ERROR(-20002, 'Unexpected datatype for ' || col);
    END IF;
    SELECT COUNT(*) INTO conflicts FROM USER_TAB_COLUMNS
      WHERE TABLE_NAME = 'CLIENT_IMAGE_OCR_JOB'
        AND COLUMN_NAME IN (col || '_OLD', col || '_U');
    IF conflicts <> 0 THEN
      RAISE_APPLICATION_ERROR(-20003, 'Inspect partial migration/backup columns for ' || col);
    END IF;
    EXECUTE IMMEDIATE 'ALTER TABLE BRK_COD.CLIENT_IMAGE_OCR_JOB ADD ('
                      || col || '_U ' || definition || ')';
    EXECUTE IMMEDIATE 'UPDATE BRK_COD.CLIENT_IMAGE_OCR_JOB SET '
                      || col || '_U = ' || conversion || '(' || col || ')';
    COMMIT;
    EXECUTE IMMEDIATE 'ALTER TABLE BRK_COD.CLIENT_IMAGE_OCR_JOB RENAME COLUMN '
                      || col || ' TO ' || col || '_OLD';
    EXECUTE IMMEDIATE 'ALTER TABLE BRK_COD.CLIENT_IMAGE_OCR_JOB RENAME COLUMN '
                      || col || '_U TO ' || col;
    DBMS_OUTPUT.PUT_LINE('Converted ' || col || '; previous data retained in ' || col || '_OLD');
  END;
BEGIN
  IF USER <> 'BRK_COD' THEN
    RAISE_APPLICATION_ERROR(-20001, 'Connect as BRK_COD');
  END IF;
  convert_column('CLIENT_ADDRESS', 'VARCHAR2', 'NVARCHAR2', 'NVARCHAR2(100)', 'TO_NCHAR');
  convert_column('JOB_DESC', 'VARCHAR2', 'NVARCHAR2', 'NVARCHAR2(400)', 'TO_NCHAR');
  convert_column('EXTRACTED_JSON', 'CLOB', 'NCLOB', 'NCLOB', 'TO_NCLOB');
  convert_column('OCR_DETAILS_JSON', 'CLOB', 'NCLOB', 'NCLOB', 'TO_NCLOB');
END;
/

SELECT COLUMN_NAME, DATA_TYPE, CHAR_LENGTH FROM USER_TAB_COLUMNS
WHERE TABLE_NAME = 'CLIENT_IMAGE_OCR_JOB'
  AND COLUMN_NAME IN ('CLIENT_ADDRESS', 'JOB_DESC', 'EXTRACTED_JSON', 'OCR_DETAILS_JSON')
ORDER BY COLUMN_NAME;
