"""Qari batching boundaries; fake processor/model, no ML installation required."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'try1'))
from core.ocr_old import reader


class TokenRow(list):
    def __getitem__(self, index):
        value = super().__getitem__(index)
        return TokenRow(value) if isinstance(index, slice) else value

    def tolist(self):
        return list(self)


def make_reader(batch_size=1, engine='qwen2_vl'):
    with patch.dict(os.environ, {'QARI_BATCH_SIZE': str(batch_size)}), \
         patch.object(reader, 'check_model_files', return_value=(Path('fixture'), None, engine)):
        return reader.QariOCR(device='cpu')


class QariBatchTests(unittest.TestCase):
    def test_default_uses_existing_single_crop_path(self):
        with patch.dict(os.environ):
            os.environ.pop('QARI_BATCH_SIZE', None)
            with patch.object(reader, 'check_model_files', return_value=(Path('fixture'), None, 'qwen2_vl')):
                ocr = reader.QariOCR()
        self.assertEqual(ocr.batch_size, 1)
        ocr.extract = Mock(side_effect=lambda path: {'text': path})
        ocr._extract_batch = Mock()
        self.assertEqual(ocr.extract_many(['name', 'address']), [{'text': 'name'}, {'text': 'address'}])
        ocr._extract_batch.assert_not_called()

    def test_batches_are_bounded_ordered_and_single_remainder_uses_extract(self):
        ocr = make_reader(2)
        ocr._extract_batch = Mock(side_effect=lambda paths: [{'text': path} for path in paths])
        ocr.extract = Mock(side_effect=lambda path: {'text': path})
        paths = ['name', 'address', 'job', 'spouse', 'retry']
        self.assertEqual(ocr.extract_many(paths), [{'text': path} for path in paths])
        self.assertEqual(ocr._extract_batch.call_args_list,
                         [call(['name', 'address']), call(['job', 'spouse'])])
        ocr.extract.assert_called_once_with('retry')

    def test_failed_batch_retries_individually_and_isolates_bad_crop(self):
        ocr = make_reader(2)
        ocr._extract_batch = Mock(side_effect=RuntimeError('batch allocation failed'))
        def extract(path):
            if path == 'bad':
                raise ValueError('invalid image')
            return {'text': path}
        ocr.extract = Mock(side_effect=extract)
        with self.assertLogs(reader.__name__, level='WARNING'):
            outputs = ocr.extract_many(['bad', 'name'])
        self.assertEqual(outputs[0]['status'], 'ocr_error')
        self.assertEqual(outputs[1], {'text': 'name'})
        self.assertEqual(ocr.extract.call_args_list, [call('bad'), call('name')])

    def test_v4_compatibility_and_empty_inputs_never_batch(self):
        ocr = make_reader(2, engine='qwen3_vl')
        ocr._extract_batch = Mock()
        ocr.extract = Mock(return_value={'text': 'ok'})
        self.assertEqual(ocr.extract_many([]), [])
        ocr.extract.assert_not_called()
        self.assertEqual(len(ocr.extract_many(['a', 'b'])), 2)
        ocr._extract_batch.assert_not_called()

    def test_nonpositive_batch_size_rejected(self):
        for size in (0, -1):
            with self.subTest(size=size), self.assertRaisesRegex(ValueError, 'QARI_BATCH_SIZE'):
                make_reader(size)

    def test_batch_generation_removes_padded_prompt_and_counts_tokens_per_crop(self):
        ocr = make_reader(2)
        ocr.max_tokens = 4
        input_ids = SimpleNamespace(shape=(2, 5))
        ocr.processor = Mock(return_value={'input_ids': input_ids})
        ocr.processor.apply_chat_template.side_effect = ['prompt-a', 'prompt-b']
        ocr.processor.decode.side_effect = [' name ', 'address']
        ocr.model = Mock()
        ocr.model.generation_config = SimpleNamespace(eos_token_id=[2, 3], pad_token_id=0)
        ocr.model.generate.return_value = [TokenRow([9] * 5 + [101, 2, 0, 0]),
                                           TokenRow([8] * 5 + [201, 202, 203, 204])]
        torch = SimpleNamespace(inference_mode=nullcontext)
        with patch.dict(sys.modules, {'torch': torch}):
            outputs = ocr._generate_many(['image-a', 'image-b'])
        self.assertEqual(outputs, [('name', 2), ('address', 4)])
        ocr.processor.assert_called_once_with(text=['prompt-a', 'prompt-b'],
                                             images=['image-a', 'image-b'], padding=True, return_tensors='pt')
        self.assertEqual([c.args[0] for c in ocr.processor.decode.call_args_list],
                         [[101, 2], [201, 202, 203, 204]])
        self.assertFalse(ocr._result(*outputs[0], seconds=1)['truncated'])
        self.assertTrue(ocr._result(*outputs[1], seconds=1)['truncated'])
        ocr.model.generate.assert_called_once()

    def test_left_padding_enabled_only_for_opted_in_merged_model(self):
        for size in (1, 2):
            with self.subTest(size=size):
                ocr = make_reader(size)
                model = Mock()
                model.to.return_value = model
                model.eval.return_value = model
                processor = Mock()
                processor.tokenizer.padding_side = 'right'
                transformers = SimpleNamespace(
                    AutoProcessor=SimpleNamespace(from_pretrained=Mock(return_value=processor)),
                    Qwen2VLForConditionalGeneration=SimpleNamespace(from_pretrained=Mock(return_value=model)))
                torch = SimpleNamespace(set_num_threads=Mock(), bfloat16='bf16')
                with patch.dict(sys.modules, {'torch': torch, 'transformers': transformers}):
                    ocr._load()
                self.assertEqual(processor.tokenizer.padding_side, 'left' if size > 1 else 'right')

    def test_batch_calls_share_lock_and_close_prepared_images(self):
        ocr = make_reader(2)
        ocr._load = Mock()
        images = []
        def prepare(path):
            image = Mock()
            images.append(image)
            return image
        ocr._prepare = prepare
        active = peak = 0
        counter_lock = threading.Lock()
        def generate(batch):
            nonlocal active, peak
            with counter_lock:
                active += 1
                peak = max(peak, active)
            time.sleep(.03)
            with counter_lock:
                active -= 1
            return [('ok', 2)] * len(batch)
        ocr._generate_many = generate
        with ThreadPoolExecutor(max_workers=2) as pool:
            outputs = list(pool.map(ocr._extract_batch, [['a', 'b'], ['c', 'd']]))
        self.assertEqual(peak, 1)
        self.assertTrue(all(result['batch_size'] == 2 for batch in outputs for result in batch))
        for image in images:
            image.close.assert_called_once_with()

    def test_prepared_images_close_when_generation_fails(self):
        ocr = make_reader(2)
        ocr._load = Mock()
        images = [Mock(), Mock()]
        ocr._prepare = Mock(side_effect=images)
        ocr._generate_many = Mock(side_effect=RuntimeError('failed'))
        with self.assertRaises(RuntimeError):
            ocr._extract_batch(['a', 'b'])
        self.assertTrue(ocr.lock.acquire(blocking=False))
        ocr.lock.release()
        for image in images:
            image.close.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
