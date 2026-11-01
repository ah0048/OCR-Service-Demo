"""Offline Arabic OCR inference, shared by the CLI pipeline and local API.

Two engines are supported and auto-detected from the model folder's
``config.json``:

``qwen3_vl``
    KARI_V4 kit - ``Qwen3-VL-4B-Instruct`` base plus the Qari-OCR 0.4.0 LoRA
    adapter. The stronger model on paper (4.44B, reported CER 0.1222 vs ~0.64),
    but it does **not** run correctly in this environment - see
    ``ocr_benchmark/KARI_V4_TEST.md``. Opt in with ``QARI_ENGINE=v4``.

``qwen2_vl``
    The older merged ``Qari-OCR-merged`` 2B package. This is the default because
    it is the only engine verified to produce correct text on this machine.

The active application always defaults to the old merged model. QARI_ENGINE
is ignored after the 2026-09-27 reorganization. V4 compatibility code is retained
only for explicit diagnostic calls; its weight paths point to the D: archive.
"""
import json
import logging
import os
from contextlib import ExitStack
from pathlib import Path
import threading
import time

os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'
os.environ.setdefault('TOKENIZERS_PARALLELISM', 'false')

from project_config import ROOT, OCR_MODEL, ARCHIVE
from core.runtime import resolve_device
V4_KIT = ARCHIVE / 'models/qari_v4/USB_A_CPU_only'
DEFAULT_QARI_V4_BASE = V4_KIT / 'models/Qwen3-VL-4B-Instruct'
DEFAULT_QARI_V4_ADAPTER = V4_KIT / 'models/qari-ocr-0.4.0-adapter'
DEFAULT_QARI = OCR_MODEL

# 'merged' is the default: it is the only engine that produces correct output on
# this machine. The V4 kit's qwen3_vl forward pass returns exactly-zero logits
# here, which decodes to '!!!!!!!!'. Do not flip this to 'v4' without first
# re-running benchmark_v4_ocr.py and confirming real Arabic text.
ENGINE_CHOICE = 'merged'  # Active application is fixed to old OCR; V4 files are archived.
if ENGINE_CHOICE not in ('merged', 'v4', 'auto'):
    raise ValueError(f'QARI_ENGINE must be merged, v4 or auto; got {ENGINE_CHOICE!r}')

# The Qwen2-VL merged model was trained with a long instruction prompt.
PROMPT = ('Please extract all the text present in the image without adding any additional comments, '
          'explanations, or corrections. It is strictly prohibited to add any text that is not present '
          'in the image, and to correct or complete any words. Your response should only contain the extracted text.')
# The Qari-OCR 0.4.0 LoRA adapter was trained with this exact short prompt.
V4_PROMPT = 'Free OCR.'

V4_REQUIRED = ('config.json', 'model.safetensors.index.json', 'tokenizer_config.json',
               'preprocessor_config.json')
MERGED_REQUIRED = ('config.json', 'model.safetensors', 'tokenizer_config.json',
                   'preprocessor_config.json')
ADAPTER_REQUIRED = ('adapter_config.json', 'adapter_model.safetensors')


def _missing(directory, names):
    return [name for name in names if not (Path(directory) / name).is_file()]


def detect_engine(model_dir):
    """Return 'qwen3_vl' or 'qwen2_vl' based on the folder's config.json."""
    config = Path(model_dir) / 'config.json'
    if not config.is_file():
        raise FileNotFoundError(f'Qari file missing: {config}. Restore the local merged model folder.')
    try:
        model_type = json.loads(config.read_text(encoding='utf-8')).get('model_type', '')
    except (OSError, ValueError) as exc:
        raise ValueError(f'Cannot read {config}: {exc}') from exc
    return 'qwen3_vl' if model_type == 'qwen3_vl' else 'qwen2_vl'


def _default_locations():
    """Always select the verified old OCR for the active application."""
    return DEFAULT_QARI, None


def check_model_files(model_dir=None, adapter_dir=None):
    """Validate a model folder (and adapter, for the V4 engine).

    With no arguments this checks the default engine, which is what the API
    health check uses. Raises FileNotFoundError listing anything missing.
    """
    if model_dir is None:
        model_dir, adapter_dir = _default_locations()
    directory = Path(model_dir).resolve()
    engine = detect_engine(directory)
    required = V4_REQUIRED if engine == 'qwen3_vl' else MERGED_REQUIRED
    if missing := _missing(directory, required):
        raise FileNotFoundError(
            f'Qari file missing: {directory / missing[0]}. '
            'Restore the local merged model folder or the KARI_V4 base + adapter folders.')
    if engine == 'qwen3_vl':
        if adapter_dir is None:
            adapter_dir = DEFAULT_QARI_V4_ADAPTER
        if missing := _missing(adapter_dir, ADAPTER_REQUIRED):
            raise FileNotFoundError(
                f'Qari LoRA adapter file missing: {Path(adapter_dir) / missing[0]}. '
                'Restore the KARI_V4 adapter folder; the Qwen3-VL kit is a base model plus adapter.')
        return directory, Path(adapter_dir).resolve(), engine
    return directory, None, engine


class QariOCR:
    def __init__(self, model_dir=None, device='auto', adapter_dir=None):
        if model_dir is None:
            model_dir, adapter_dir = _default_locations()
        self.directory, self.adapter_dir, self.engine = check_model_files(model_dir, adapter_dir)
        self.device = device
        self.model = self.processor = None
        self.lock = threading.Lock()
        self.max_tokens = int(os.environ.get('QARI_MAX_TOKENS', '128'))
        self.max_pixels = os.environ.get('QARI_MAX_PIXELS', '').strip()
        self.threads = int(os.environ.get('QARI_THREADS', '4'))
        self.batch_size = int(os.environ.get('QARI_BATCH_SIZE', '1'))
        self.merge = os.environ.get('QARI_MERGE', '').strip().lower() in ('1', 'true', 'yes')
        # Disk offload lets the 8.87 GB V4 model run on a small-RAM machine (and
        # lets both models be served at once on a 15 GB box). Much slower.
        self.offload = os.environ.get('QARI_OFFLOAD', '').strip().lower() in ('1', 'true', 'yes')
        self.max_cpu_gb = int(os.environ.get('QARI_MAX_CPU_GB', '6'))
        self.max_pixels_value = int(self.max_pixels) if self.max_pixels else 0
        if self.max_tokens < 1 or self.threads < 1:
            raise ValueError('QARI_MAX_TOKENS and QARI_THREADS must be positive')
        if self.batch_size < 1:
            raise ValueError('QARI_BATCH_SIZE must be positive')
        if self.max_pixels_value and self.max_pixels_value < 64 * 28 * 28:
            raise ValueError('QARI_MAX_PIXELS must be >= 50176 when set')

    @property
    def prompt(self):
        return V4_PROMPT if self.engine == 'qwen3_vl' else PROMPT

    def _load(self):
        if self.model is not None:
            return
        import torch
        from transformers import AutoProcessor
        torch.set_num_threads(self.threads)
        device = resolve_device(self.device)
        dtype = torch.bfloat16 if device == 'cpu' else torch.float16
        kwargs = dict(local_files_only=True, trust_remote_code=False, dtype=dtype,
                      low_cpu_mem_usage=True)
        if self.engine == 'qwen3_vl':
            from transformers.models.qwen3_vl import Qwen3VLForConditionalGeneration
            if self.offload:
                if self.merge:
                    raise ValueError('QARI_MERGE cannot be combined with QARI_OFFLOAD')
                if self.adapter_dir:
                    # PEFT cannot attach a LoRA to disk-offloaded weights; it
                    # raises KeyError on the offload hook. Offload therefore
                    # needs an already-merged V4 model folder (no adapter).
                    raise ValueError(
                        'QARI_OFFLOAD cannot be used with the LoRA adapter - PEFT cannot '
                        'attach LoRA weights to offloaded modules. Either drop QARI_OFFLOAD '
                        '(needs ~9 GB RAM for the base model) or point --qari-model-dir at a '
                        'merged V4 model folder built with the kit scripts/merge_lora.py.')
                offload_dir = self.directory.parent.parent / '.offload'
                offload_dir.mkdir(exist_ok=True)
                kwargs.update(device_map='auto',
                              max_memory={'cpu': f'{self.max_cpu_gb}GiB'},
                              offload_folder=str(offload_dir))
            model = Qwen3VLForConditionalGeneration.from_pretrained(str(self.directory), **kwargs)
            from peft import PeftModel
            model = PeftModel.from_pretrained(model, str(self.adapter_dir), local_files_only=True)
            if self.merge:
                model = model.merge_and_unload()
            processor = AutoProcessor.from_pretrained(str(self.directory), local_files_only=True)
            if not self.offload:
                model = model.to(device)
            model.eval()
            device = str(getattr(model, 'device', device))
        else:
            from transformers import Qwen2VLForConditionalGeneration
            model = Qwen2VLForConditionalGeneration.from_pretrained(
                str(self.directory), attn_implementation='sdpa',
                device_map={'': device}, **kwargs)
            extra = {'min_pixels': 64 * 28 * 28}
            if self.max_pixels_value:
                extra['max_pixels'] = self.max_pixels_value
            else:
                extra['max_pixels'] = 256 * 28 * 28
            processor = AutoProcessor.from_pretrained(str(self.directory), local_files_only=True,
                                                     trust_remote_code=False, **extra)
            if self.batch_size > 1:
                # Decoder-only generation must use left padding for unequal prompts.
                processor.tokenizer.padding_side = 'left'
            model = model.to(device).eval()
        self.model, self.processor = model, processor
        self.device = device

    def warm(self):
        """Load weights now so the first extract() call is not slowed by loading."""
        with self.lock:
            self._load()

    def _prepare(self, image_path):
        from PIL import Image, ImageOps
        with Image.open(image_path) as raw:
            return ImageOps.exif_transpose(raw).convert('RGB')

    def _generate(self, image):
        """Return (decoded text, generated token count) for one prepared image."""
        import torch
        messages = [{'role': 'user', 'content': [{'type': 'image', 'image': image},
                                                 {'type': 'text', 'text': self.prompt}]}]
        if self.engine == 'qwen3_vl':
            inputs = self.processor.apply_chat_template(
                messages, tokenize=True, add_generation_prompt=True,
                return_dict=True, return_tensors='pt')
        else:
            prompt = self.processor.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True)
            inputs = self.processor(text=[prompt], images=[image], return_tensors='pt')
        inputs = {k: (v.to(self.device) if hasattr(v, 'to') else v) for k, v in inputs.items()}
        with torch.inference_mode():
            generated = self.model.generate(**inputs, max_new_tokens=self.max_tokens,
                                           do_sample=False, num_beams=1, use_cache=True)
        ids = generated[0, inputs['input_ids'].shape[1]:]
        text = self.processor.decode(ids, skip_special_tokens=True,
                                     clean_up_tokenization_spaces=False).strip()
        return text, len(ids)

    def extract(self, image_path):
        with self.lock:
            start = time.perf_counter()
            self._load()
            text, tokens = self._generate(self._prepare(image_path))
            return self._result(text, tokens, time.perf_counter() - start)

    def _result(self, text, tokens, seconds):
        truncated = tokens >= self.max_tokens
        engine = 'Qari-OCR-0.4.0-VL-4B' if self.engine == 'qwen3_vl' else 'Qari-OCR-merged'
        result = {'text': text or None,
                  'status': 'needs_review' if truncated or not text else 'extracted',
                  'needs_review': True, 'truncated': truncated, 'generated_tokens': tokens,
                  'seconds': round(seconds, 3), 'engine': engine,
                  'model_dir': str(self.directory), 'device': self.device, 'max_pixels': self.max_pixels_value or None}
        if self.adapter_dir:
            result['adapter_dir'] = str(self.adapter_dir)
        if self.offload:
            result['offload'] = True
        return result

    def _generate_many(self, images):
        """Generate independent Qwen2-VL sequences in one padded model batch."""
        import torch
        prompts = []
        for image in images:
            messages = [{'role': 'user', 'content': [{'type': 'image', 'image': image},
                                                     {'type': 'text', 'text': self.prompt}]}]
            prompts.append(self.processor.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True))
        inputs = self.processor(text=prompts, images=images, padding=True, return_tensors='pt')
        inputs = {k: (v.to(self.device) if hasattr(v, 'to') else v) for k, v in inputs.items()}
        with torch.inference_mode():
            generated = self.model.generate(**inputs, max_new_tokens=self.max_tokens,
                                           do_sample=False, num_beams=1, use_cache=True)
        if len(generated) != len(images):
            raise RuntimeError('Qari batch output count does not match its inputs')
        eos = self.model.generation_config.eos_token_id
        eos = [eos] if isinstance(eos, int) else (eos or [])
        pad = self.model.generation_config.pad_token_id
        if pad is None:
            pad = self.processor.tokenizer.pad_token_id
        outputs = []
        for row in generated:
            ids = row[inputs['input_ids'].shape[1]:].tolist()
            # Finished sequences are padded to the longest output. Do not count
            # those pads as generated tokens or mark short answers truncated.
            for index, token in enumerate(ids):
                if token in eos:
                    ids = ids[:index + 1]
                    break
                if token == pad:
                    ids = ids[:index]
                    break
            text = self.processor.decode(ids, skip_special_tokens=True,
                                         clean_up_tokenization_spaces=False).strip()
            outputs.append((text, len(ids)))
        return outputs

    def _extract_batch(self, paths):
        with self.lock, ExitStack() as images_to_close:
            start = time.perf_counter()
            self._load()
            images = []
            for path in paths:
                image = self._prepare(path)
                images_to_close.callback(image.close)
                images.append(image)
            outputs = self._generate_many(images)
            seconds = time.perf_counter() - start
            return [dict(self._result(text, tokens, seconds), batch_size=len(paths))
                    for text, tokens in outputs]

    def extract_many(self, image_paths):
        """Preserve input order; batch merged-model crops only when opted in."""
        paths = list(image_paths)
        results = []
        for start in range(0, len(paths), self.batch_size):
            chunk = paths[start:start + self.batch_size]
            batch_results = None
            if len(chunk) > 1 and self.engine == 'qwen2_vl':
                try:
                    batch_results = self._extract_batch(chunk)
                except Exception:
                    logging.getLogger(__name__).warning(
                        'Qari batch failed; retrying its crops individually')
            if batch_results is None:
                batch_results = []
                for path in chunk:
                    try:
                        batch_results.append(self.extract(path))
                    except Exception as exc:
                        batch_results.append({'text': None, 'status': 'ocr_error',
                                              'error': f'{type(exc).__name__}: {exc}'})
            results.extend(batch_results)
        return results
