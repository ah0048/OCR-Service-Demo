"""Oracle adapter checks without Oracle libraries or ML dependencies."""
from pathlib import Path
import sys
import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import oracle_worker as worker
from inference import OcrOutput


DRIVER = SimpleNamespace(DB_TYPE_NUMBER='number', DB_TYPE_CLOB='clob', DB_TYPE_DATE='date',
                         DB_TYPE_NVARCHAR='nvarchar', DB_TYPE_NCLOB='nclob')


class OracleWorkerTests(unittest.TestCase):
    def test_thin_skips_client_loading_and_keeps_connection_settings(self):
        driver = MagicMock()
        args = SimpleNamespace(thin=True, client_lib_dir='unused', user='BRK_COD',
                               password='test-only', dsn='example:1521/service')
        with patch.dict(sys.modules, {'oracledb': driver}):
            conn, actual_driver = worker.connect(args)
        driver.init_oracle_client.assert_not_called()
        driver.connect.assert_called_once_with(user=args.user, password=args.password, dsn=args.dsn)
        self.assertIs(actual_driver, driver)
        self.assertFalse(conn.autocommit)

    def test_thick_loads_configured_client(self):
        driver = MagicMock()
        args = SimpleNamespace(thin=False, client_lib_dir='client-folder', user='BRK_COD',
                               password='test-only', dsn='example:1521/service')
        with patch.dict(sys.modules, {'oracledb': driver}):
            worker.connect(args)
        driver.init_oracle_client.assert_called_once_with(lib_dir='client-folder')

    def connection(self):
        conn = MagicMock()
        return conn, conn.cursor.return_value.__enter__.return_value

    def test_gender_codes_and_dates(self):
        for label, code in [('ذكر', '1'), ('انثى', '2'), ('أنثى', '2')]:
            values, errors = worker.mapped_result(OcrOutput('Success', .9, {
                'gender': label, 'fullName': 'اسم', 'birthDate': '2000-01-02'}), .75)
            self.assertEqual(values['CLIENT_GENDER'], code)
            self.assertEqual(values['DATE_OF_BIRTH'].isoformat(), '2000-01-02')
            self.assertEqual(values['STATUS'], 'OCR_SUCCESS')
            self.assertEqual(errors, [])

    def test_errors_fail_even_with_high_confidence(self):
        for outcome in ['Rejected', 'Unreadable', 'TechnicalError']:
            values, errors = worker.mapped_result(OcrOutput(outcome, .99, {'fullName': 'x'}), .75)
            self.assertEqual(values['STATUS'], 'OCR_FAIL')
            self.assertTrue(errors)

    def test_review_flags_use_overall_fifty_percent_boundary(self):
        for confidence, expected in [(.499999, 'OCR_FAIL'), (.5, 'OCR_SUCCESS'),
                                     (.571199, 'OCR_SUCCESS')]:
            out = OcrOutput('LowConfidence', confidence, {'fullName': 'test'},
                            {'review_required': True, 'status': 'partial'})
            values, errors = worker.mapped_result(out, .5)
            self.assertEqual(values['STATUS'], expected)
            self.assertEqual(json.loads(values['OCR_DETAILS_JSON'])['review_required'], True)
            if expected == 'OCR_SUCCESS':
                self.assertEqual(errors, [])

    def test_mapping_warnings_do_not_override_overall_score(self):
        out = OcrOutput('LowConfidence', .6, {'gender': 'unknown', 'expiryDate': 'invalid'})
        values, errors = worker.mapped_result(out, .5)
        self.assertEqual(values['STATUS'], 'OCR_SUCCESS')
        self.assertIn('UNRECOGNIZED_GENDER', errors)
        self.assertIn('INVALID_ID_EXPIRE_DATE', errors)

    def test_invalid_date_gender_and_confidence_preserve_original_json(self):
        out = OcrOutput('Success', float('nan'), {'gender': 'unknown', 'expiryDate': 'bad'})
        values, errors = worker.mapped_result(out, .75)
        self.assertEqual(values['STATUS'], 'OCR_FAIL')
        self.assertIsNone(values['CONFIDENCE'])
        self.assertIsNone(values['CLIENT_GENDER'])
        self.assertIsNone(values['ID_EXPIRE_DATE'])
        self.assertIn('unknown', values['EXTRACTED_JSON'])
        self.assertIn('INVALID_ID_EXPIRE_DATE', errors)

    def test_missing_low_confidence_or_no_fields_cannot_succeed(self):
        for confidence, fields in [(None, {'fullName': 'x'}), (.5, {'fullName': 'x'}), (.9, {})]:
            values, _ = worker.mapped_result(OcrOutput('Success', confidence, fields), .75)
            self.assertEqual(values['STATUS'], 'OCR_FAIL')

    def test_claim_commits_and_returns_numeric_nin_as_text(self):
        conn, cur = self.connection()
        cur.var.side_effect = [SimpleNamespace(getvalue=lambda: 12), SimpleNamespace(getvalue=lambda: 12345)]
        self.assertEqual(worker.claim_job(conn, DRIVER), (12, '12345'))
        conn.commit.assert_called_once()
        sql = cur.execute.call_args.args[0]
        self.assertTrue(sql.startswith('DECLARE'))
        self.assertIn('FOR UPDATE SKIP LOCKED', sql)
        self.assertEqual(sql.count('FETCH jobs INTO'), 1)

    def test_empty_claim_and_failed_claim(self):
        conn, cur = self.connection()
        cur.var.return_value.getvalue.return_value = None
        self.assertIsNone(worker.claim_job(conn, DRIVER))
        cur.execute.side_effect = RuntimeError('database unavailable')
        with self.assertRaises(RuntimeError):
            worker.claim_job(conn, DRIVER)
        conn.rollback.assert_called_once()

    def test_blob_pair_is_copied_and_read_transaction_ends(self):
        conn, cur = self.connection()
        def blob(data):
            return SimpleNamespace(size=lambda: len(data), read=lambda offset, amount: data[offset-1:offset-1+amount])
        cur.__iter__.return_value = iter([(2, blob(b'back')), (1, blob(b'front'))])
        with tempfile.TemporaryDirectory() as directory:
            front, back = worker.read_images(conn, '123', Path(directory))
            self.assertEqual(front.read_bytes(), b'front')
            self.assertEqual(back.read_bytes(), b'back')
        conn.rollback.assert_called_once()
        self.assertEqual(cur.execute.call_args.kwargs, {'nin': '123'})

    def test_missing_duplicate_and_oversized_images_fail(self):
        blob = SimpleNamespace(size=lambda: 1, read=lambda *_: b'x')
        oversized = SimpleNamespace(size=lambda: worker.MAX_IMAGE_BYTES + 1)
        for rows in [[], [(1, blob)], [(1, blob), (1, blob)], [(1, oversized)], [(1, None)]]:
            conn, cur = self.connection()
            cur.__iter__.return_value = iter(rows)
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ValueError):
                    worker.read_images(conn, '123', Path(directory))
            conn.rollback.assert_called_once()

    def test_byte_overflow_keeps_json_and_warning_without_overriding_score(self):
        conn, cur = self.connection()
        cur.fetchone.return_value = (101, 14, 10, 10, None, None, None)
        cur.rowcount = 1
        out = OcrOutput('Success', .9, {'fullName': 'original name', 'nationalId': '12345678901234'})
        self.assertEqual(worker.save_result(conn, DRIVER, 12, out, .75), 'OCR_SUCCESS')
        sql, binds = cur.execute.call_args.args
        self.assertIsNone(binds['CLIENT_NAME'])
        self.assertIn('original name', binds['EXTRACTED_JSON'])
        self.assertIn('FIELD_TOO_LONG_CLIENT_NAME', binds['ERROR_MESSAGE'])
        self.assertIn("STATUS = 'UNDER_PROCESSING'", sql)
        self.assertEqual(binds['job_id'], 12)
        cur.setinputsizes.assert_called_once()
        conn.commit.assert_called_once()

    def test_stale_status_or_db_error_rolls_back_result(self):
        for db_error in [False, True]:
            conn, cur = self.connection()
            cur.fetchone.return_value = (None,) * len(worker.TEXT_FIELDS)
            cur.rowcount = 0
            if db_error:
                cur.execute.side_effect = RuntimeError('DB error')
            with self.assertRaises(RuntimeError):
                worker.save_result(conn, DRIVER, 12, OcrOutput('TechnicalError'), .75)
            conn.rollback.assert_called_once()
            conn.commit.assert_not_called()

    def test_large_raw_text_is_only_bound_as_clob(self):
        conn, cur = self.connection()
        cur.fetchone.return_value = (None,) * len(worker.TEXT_FIELDS)
        cur.rowcount = 1
        raw = 'x' * 50000
        worker.save_result(conn, DRIVER, 1, OcrOutput('Success', .9, {'fullName': raw}), .75)
        length_binds = cur.execute.call_args_list[0].args[1]
        result_binds = cur.execute.call_args.args[1]
        self.assertIsNone(length_binds['CLIENT_NAME'])
        self.assertIn(raw, result_binds['EXTRACTED_JSON'])
        self.assertEqual(result_binds['STATUS'], 'OCR_SUCCESS')

    def test_marital_status_religion_and_json_are_saved_together(self):
        conn, cur = self.connection()
        cur.fetchone.return_value = (None,) * len(worker.TEXT_FIELDS)
        cur.rowcount = 1
        fields = {'maritalStatus': '\u0645\u062a\u0632\u0648\u062c',
                  'religion': '\u0645\u0633\u0644\u0645', 'gender': '\u0630\u0643\u0631'}
        worker.save_result(conn, DRIVER, 12, OcrOutput('Success', .8, fields), .5)
        sql, binds = cur.execute.call_args.args
        self.assertEqual(binds['MARITAL_STATUS'], fields['maritalStatus'])
        self.assertEqual(binds['RELIGION'], fields['religion'])
        self.assertEqual(json.loads(binds['EXTRACTED_JSON']), fields)
        self.assertIn('MARITAL_STATUS = :MARITAL_STATUS', sql)
        self.assertIn('RELIGION = :RELIGION', sql)
        self.assertEqual(binds['STATUS'], 'OCR_SUCCESS')

    def test_multibyte_overflow_in_new_fields_preserves_json(self):
        conn, cur = self.connection()
        cur.fetchone.return_value = (None, None, None, None, None, 110, 110)
        cur.rowcount = 1
        fields = {'maritalStatus': '\u0645' * 55, 'religion': '\u0645' * 55}
        worker.save_result(conn, DRIVER, 12, OcrOutput('Success', .8, fields), .5)
        binds = cur.execute.call_args.args[1]
        self.assertIsNone(binds['MARITAL_STATUS'])
        self.assertIsNone(binds['RELIGION'])
        self.assertEqual(json.loads(binds['EXTRACTED_JSON']), fields)
        self.assertEqual(binds['STATUS'], 'OCR_SUCCESS')

    def test_detailed_report_is_saved_intact_as_clob_with_fields(self):
        conn, cur = self.connection()
        cur.fetchone.return_value = (None,) * len(worker.TEXT_FIELDS)
        cur.rowcount = 1
        details = {'national_id': {'status': 'agreed'},
                   'sides': {'front': {'fields': {'name': {'text': '\u0627\u0633\u0645',
                              'needs_review': True, 'ocr': {'score': .6}}}}},
                   'confidenceCalculation': {'calibrated': False},
                   'largeEvidence': 'x' * 50000}
        out = OcrOutput('LowConfidence', .6, {'fullName': '\u0627\u0633\u0645'}, details)
        worker.save_result(conn, DRIVER, 12, out, .5)
        sql, binds = cur.execute.call_args.args
        self.assertEqual(json.loads(binds['OCR_DETAILS_JSON']), details)
        self.assertEqual(json.loads(binds['EXTRACTED_JSON']), out.fields)
        self.assertIn('OCR_DETAILS_JSON = :OCR_DETAILS_JSON', sql)
        self.assertEqual(cur.setinputsizes.call_args.kwargs['OCR_DETAILS_JSON'], DRIVER.DB_TYPE_CLOB)
        self.assertEqual(binds['STATUS'], 'OCR_SUCCESS')
        conn.commit.assert_called_once()

    def test_error_details_are_preserved_and_absent_details_are_null(self):
        for details in [None, {'errorCode': 'INVALID_IMAGE_PAIR'}]:
            values, _ = worker.mapped_result(OcrOutput('Unreadable', 0, details=details), .5)
            if details is None:
                self.assertIsNone(values['OCR_DETAILS_JSON'])
            else:
                self.assertEqual(json.loads(values['OCR_DETAILS_JSON']), details)

    def test_file_inference_uses_shared_gate_without_http(self):
        import inference
        with patch.object(inference, 'validate_image') as validate, \
             patch.object(inference, '_infer_pair', return_value=OcrOutput('Success')) as infer:
            # The normal real PIL package need not be installed for this adapter test.
            image = SimpleNamespace(DecompressionBombError=RuntimeError, DecompressionBombWarning=Warning)
            with patch.dict(sys.modules, {'PIL': SimpleNamespace(Image=image, UnidentifiedImageError=OSError)}):
                out = inference.run_ocr_paths(Path('front'), Path('back'))
        self.assertEqual(out.outcome, 'Success')
        self.assertEqual(validate.call_count, 2)
        infer.assert_called_once()

    def test_job_description_uses_enlarged_400_byte_column(self):
        conn, cur = self.connection()
        cur.fetchone.return_value = (None, None, None, 64, None, None, None)
        cur.rowcount = 1
        text = 'x' * 64
        status = worker.save_result(conn, DRIVER, 12,
                                    OcrOutput('Success', .6, {'occupation': text}), .5)
        binds = cur.execute.call_args.args[1]
        self.assertEqual(status, 'OCR_SUCCESS')
        self.assertEqual(binds['JOB_DESC'], text)
        self.assertIsNone(binds['ERROR_MESSAGE'])

    def test_json_unicode_survives_legacy_charset(self):
        original = '\u0645\u0647\u0646\u0629 \u0661\u0662\u0663'
        out = OcrOutput('LowConfidence', .6, {'occupation': original},
                        {'field': {'text': original}})
        values, _ = worker.mapped_result(out, .5)
        for key in ['EXTRACTED_JSON', 'OCR_DETAILS_JSON']:
            stored = values[key].encode('cp1256', errors='replace').decode('cp1256')
            self.assertEqual(stored, values[key])
            self.assertNotIn('?', stored)
        self.assertEqual(json.loads(values['EXTRACTED_JSON'])['occupation'], original)
        self.assertEqual(json.loads(values['OCR_DETAILS_JSON'])['field']['text'], original)

    def test_unicode_columns_use_national_binds_and_readable_json(self):
        conn, cur = self.connection()
        cur.fetchone.return_value = (None, None, 9, 3, None, None, None)
        cur.rowcount = 1
        address = '\u0639\u0645\u0627\u0631\u0629 \u0662\u0665'
        fields = {'address': address, 'occupation': '\u0661\u0662\u0663'}
        storage = {'CLIENT_ADDRESS': ('NVARCHAR2', 100), 'JOB_DESC': ('NVARCHAR2', 400),
                   'EXTRACTED_JSON': ('NCLOB', None), 'OCR_DETAILS_JSON': ('NCLOB', None)}
        worker.save_result(conn, DRIVER, 12, OcrOutput('LowConfidence', .7, fields,
                           {'text': address}), .5, storage)
        sql, binds = cur.execute.call_args.args
        self.assertEqual(binds['CLIENT_ADDRESS'], address)
        self.assertIn(address, binds['EXTRACTED_JSON'])
        self.assertIn(address, binds['OCR_DETAILS_JSON'])
        self.assertEqual(json.loads(binds['EXTRACTED_JSON']), fields)
        self.assertIn('LENGTH(:CLIENT_ADDRESS)', cur.execute.call_args_list[0].args[0])
        types = cur.setinputsizes.call_args.kwargs
        self.assertEqual(types['CLIENT_ADDRESS'], DRIVER.DB_TYPE_NVARCHAR)
        self.assertEqual(types['JOB_DESC'], DRIVER.DB_TYPE_NVARCHAR)
        self.assertEqual(types['EXTRACTED_JSON'], DRIVER.DB_TYPE_NCLOB)
        self.assertEqual(types['OCR_DETAILS_JSON'], DRIVER.DB_TYPE_NCLOB)

    def test_storage_metadata_distinguishes_bytes_and_characters(self):
        conn, cur = self.connection()
        cur.__iter__.return_value = iter([
            ('CLIENT_NAME', 'VARCHAR2', 100, 100, 'B'),
            ('CLIENT_ADDRESS', 'NVARCHAR2', 100, 200, 'C'),
            ('JOB_DESC', 'NVARCHAR2', 400, 800, 'C'),
            ('EXTRACTED_JSON', 'NCLOB', 0, 4000, None)])
        storage = worker.load_column_storage(conn)
        self.assertEqual(storage['CLIENT_ADDRESS'], ('NVARCHAR2', 100))
        self.assertEqual(storage['JOB_DESC'], ('NVARCHAR2', 400))
        self.assertEqual(storage['CLIENT_NAME'], ('VARCHAR2', 100))


if __name__ == '__main__':
    unittest.main()
