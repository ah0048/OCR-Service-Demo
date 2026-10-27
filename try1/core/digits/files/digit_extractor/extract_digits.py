"""
Default API: strict 14-digit ID recognition using the existing trained CNN.
Tries Otsu and adaptive thresholding; rejects missing/conflicting readings.
The historical template algorithm below remains available explicitly through
extract_digits_unconstrained() for non-ID uses and template-building tools.

Extract a run of Eastern-Arabic (Arabic-Indic) digits from a photographed /
scanned image like:
    ٢٨٠  ٠٣  ٤٨٨  ٠٠٠  ٨٩
and return them concatenated as a plain digit string, e.g. "28003048800089".

This version is built to tolerate real-world capture conditions: phone
photos at a slight angle, uneven lighting, JPEG noise, and moderate
scale differences between shots. It does NOT require the new photo to
line up pixel-for-pixel with a stored template.

Pipeline
--------
1. Adaptive threshold (not a single global threshold) so uneven lighting
   and camera exposure don't wreck the binarization.
2. Deskew: estimate the in-plane tilt of the whole digit strip and rotate
   it level. Handles the common "phone wasn't perfectly parallel to the
   paper" case.
3. Light denoising (morphological opening) to remove JPEG/camera speckle.
4. Connected components -> one box per glyph, sorted left-to-right.
5. Each glyph is classified with a k-NN vote over a feature vector that is
   inherently robust to small changes in rotation, scale, and pixel-level
   noise:
       - Hu moments (7 values, log-scaled) - invariant to translation,
         scale, and rotation.
       - aspect ratio, extent, solidity - coarse shape descriptors that
         are stable across camera angle/quality.
   This replaces raw pixel-to-pixel template matching, which breaks as
   soon as a glyph is shifted, rotated, or resized relative to the stored
   template.
6. Templates are augmented (small synthetic rotations/scales) when they're
   built, so the k-NN reference set already contains some tolerance to
   the kind of jitter a handheld photo introduces.

Honest limitation: this corrects in-plane rotation (tilt) and is tolerant
of scale/lighting/noise, but it does not undo a strong keystone/perspective
warp (e.g. photographing the strip at a steep angle so one end is much
closer to the camera than the other). For that, crop/warp the strip so its
top and bottom edges are roughly parallel before calling extract_digits,
or capture more top-down.

Usage
-----
    python3 extract_digits.py path/to/image.png

To teach it a new capture style/font once you know the correct digit
string it contains:
    from extract_digits import build_templates
    build_templates("new_photo.jpg", "30206281800517")
"""

import sys
import os
import glob
import json
import cv2
import numpy as np

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
FEATURES_PATH = os.path.join(TEMPLATE_DIR, "_features.json")
DIGIT_SIZE = (28, 40)  # (w, h) glyphs are normalized to this before feature extraction
K_NEIGHBORS = 5


# --------------------------------------------------------------------------
# Preprocessing: adaptive threshold + deskew + denoise
# --------------------------------------------------------------------------

def _adaptive_binarize(gray: np.ndarray) -> np.ndarray:
    """Dark glyphs -> white(255) on black(0), robust to uneven lighting."""
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    # adaptiveThreshold handles vignetting / uneven exposure much better
    # than a single global Otsu threshold across the whole strip.
    th = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV,
        blockSize=31, C=10,
    )
    # remove small speckle noise from camera sensors / JPEG artifacts
    kernel = np.ones((2, 2), np.uint8)
    th = cv2.morphologyEx(th, cv2.MORPH_OPEN, kernel)
    return th


def _deskew(th: np.ndarray) -> np.ndarray:
    """Rotate the strip so the digit baseline is level.

    Uses the centroids of each connected component (glyph) rather than raw
    pixel minAreaRect: raw pixels are noisy (glyph shapes aren't
    rectangular and vary a lot in width), but glyph centroids sit right on
    the strip's baseline and fitting a line through them is a much more
    stable estimate of the true tilt.
    """
    contours, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = [cv2.boundingRect(c) for c in contours if cv2.contourArea(c) > 20]
    if len(boxes) < 4:
        return th
    centers = np.array([(x + w / 2.0, y + h / 2.0) for (x, y, w, h) in boxes])
    xs, ys = centers[:, 0], centers[:, 1]
    # robust line fit (least squares is fine here - centroids are clean)
    m, b = np.polyfit(xs, ys, 1)
    angle = np.degrees(np.arctan(m))
    if abs(angle) < 0.3 or abs(angle) > 20:
        return th
    h, w = th.shape
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    rotated = cv2.warpAffine(th, M, (w, h), flags=cv2.INTER_NEAREST,
                              borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    return rotated


def _preprocess(image_path: str) -> np.ndarray:
    gray = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
    if gray is None:
        raise FileNotFoundError(image_path)
    th = _adaptive_binarize(gray)
    th = _deskew(th)
    return th


# --------------------------------------------------------------------------
# Glyph segmentation
# --------------------------------------------------------------------------

def _segment_glyphs(th: np.ndarray, noise_floor: int = 12, relative_cutoff: float = 0.12):
    """Return list of (x, y, w, h) boxes, sorted left-to-right.

    Filtering is done in a few passes so it adapts to the image's own scale
    instead of relying on a fixed pixel-count threshold (which breaks the
    moment the input photo is a different resolution/zoom level):
      1. Drop anything under an absolute noise floor (a handful of stray
         pixels from JPEG artifacts / camera sensor noise).
      2. Drop components that hug the very top/bottom image border - a
         common artifact of adaptive thresholding at frame edges, not a
         real glyph (real glyphs sit inset from the frame).
      3. Among what's left, drop anything much smaller than this image's
         own median component (relative_cutoff) - that's noise speckle,
         not a genuine (if small) zero glyph.
    """
    img_h = th.shape[0]
    # a small dilation reconnects strokes that rotation/interpolation can
    # fragment into 2 components, without merging genuinely separate glyphs
    # (there's a real gap between digits in this font).
    reconnected = cv2.dilate(th, np.ones((2, 2), np.uint8), iterations=1)
    contours, _ = cv2.findContours(reconnected, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = [cv2.boundingRect(c) for c in contours]
    boxes = [b for b in boxes if b[2] * b[3] > noise_floor]
    boxes = [b for b in boxes if not (b[1] <= 0 or b[1] + b[3] >= img_h - 1)]
    if not boxes:
        return []
    areas = np.array([w * h for (_, _, w, h) in boxes], dtype=np.float64)
    median_area = float(np.median(areas))
    boxes = [b for b, a in zip(boxes, areas) if a > median_area * relative_cutoff]
    boxes.sort(key=lambda b: b[0])
    return boxes


# --------------------------------------------------------------------------
# Rotation/scale-robust feature extraction
# --------------------------------------------------------------------------

def _glyph_features(crop: np.ndarray) -> np.ndarray:
    """
    Build a feature vector that stays roughly stable under small rotation,
    scale changes, and pixel noise - unlike raw pixel comparison.
    """
    h, w = crop.shape
    resized = cv2.resize(crop, DIGIT_SIZE, interpolation=cv2.INTER_NEAREST)

    moments = cv2.moments(resized)
    hu = cv2.HuMoments(moments).flatten()
    # log-scale Hu moments (standard trick - they span many orders of magnitude)
    hu = np.array([-np.sign(v) * np.log10(abs(v) + 1e-12) for v in hu])

    contours, _ = cv2.findContours(resized, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if contours:
        c = max(contours, key=cv2.contourArea)
        area = cv2.contourArea(c)
        hull = cv2.convexHull(c)
        hull_area = cv2.contourArea(hull) if len(hull) >= 3 else area
        solidity = area / hull_area if hull_area > 0 else 0.0
    else:
        solidity = 0.0

    aspect = w / float(h) if h else 0.0
    extent = float(np.count_nonzero(resized)) / (DIGIT_SIZE[0] * DIGIT_SIZE[1])

    # count interior holes (e.g. helps separate looped digits like 9 from open ones)
    _, hierarchy = cv2.findContours(resized, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    n_holes = 0
    if hierarchy is not None:
        n_holes = sum(1 for h4 in hierarchy[0] if h4[3] != -1)

    extra = np.array([aspect, extent, solidity, n_holes], dtype=np.float64)
    return np.concatenate([hu, extra])


_FEATURE_MEAN = None
_FEATURE_STD = None


def _normalize_features(matrix: np.ndarray) -> np.ndarray:
    global _FEATURE_MEAN, _FEATURE_STD
    if _FEATURE_MEAN is None:
        _FEATURE_MEAN = matrix.mean(axis=0)
        _FEATURE_STD = matrix.std(axis=0)
        _FEATURE_STD[_FEATURE_STD < 1e-6] = 1.0
    return (matrix - _FEATURE_MEAN) / _FEATURE_STD


# --------------------------------------------------------------------------
# Template store: augmented crops -> cached feature vectors
# --------------------------------------------------------------------------

def _augment(crop: np.ndarray):
    """Yield small synthetic variants of a template crop to build tolerance
    to the rotation/scale jitter a handheld photo introduces."""
    yield crop
    h, w = crop.shape
    for angle in (-10, -5, 5, 10):
        M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
        rot = cv2.warpAffine(crop, M, (w, h), flags=cv2.INTER_NEAREST,
                              borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        yield rot
    for scale in (0.85, 1.15):
        sw, sh = max(1, int(w * scale)), max(1, int(h * scale))
        scaled = cv2.resize(crop, (sw, sh), interpolation=cv2.INTER_NEAREST)
        yield scaled


def _load_template_bank():
    """Return (feature_matrix, labels) built from every templates/*.png,
    using a cached _features.json when the template files haven't changed."""
    files = sorted(glob.glob(os.path.join(TEMPLATE_DIR, "*.png")))
    if not files:
        return np.zeros((0, 0)), []

    sig = {os.path.basename(f): os.path.getmtime(f) for f in files}
    if os.path.exists(FEATURES_PATH):
        with open(FEATURES_PATH) as fh:
            cached = json.load(fh)
        if cached.get("sig") == sig:
            return np.array(cached["X"]), cached["y"]

    X, y = [], []
    for f in files:
        digit = os.path.basename(f).split("_")[0]
        crop = cv2.imread(f, cv2.IMREAD_GRAYSCALE)
        for variant in _augment(crop):
            X.append(_glyph_features(variant))
            y.append(digit)
    X = np.array(X)

    with open(FEATURES_PATH, "w") as fh:
        json.dump({"sig": sig, "X": X.tolist(), "y": y}, fh)
    return X, y


def _knn_classify(feat: np.ndarray, X: np.ndarray, y, k: int = K_NEIGHBORS) -> str:
    if len(y) == 0:
        return "?"
    dists = np.linalg.norm(X - feat, axis=1)
    idx = np.argsort(dists)[:k]
    votes = {}
    for i in idx:
        w = 1.0 / (dists[i] + 1e-6)
        votes[y[i]] = votes.get(y[i], 0.0) + w
    return max(votes, key=votes.get)


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def extract_digits_unconstrained(image_path: str, zero_area_ratio: float = 0.32) -> str:
    """
    Extract the digit string from a photo/scan of spaced Eastern-Arabic
    numerals. Robust to moderate rotation, scale, lighting, and noise
    differences between the input photo and the stored templates.

    zero_area_ratio: glyphs whose filled pixel area is well below the
    median glyph area are fast-tracked as '0' (the zero glyph in this
    font family is a visually tiny diamond/dot, categorically smaller
    than every other digit) rather than sent through k-NN, which keeps
    the classifier focused on telling the "real" digits apart.
    """
    th = _preprocess(image_path)
    boxes = _segment_glyphs(th)
    if not boxes:
        return ""

    areas = np.array([w * h for (_, _, w, h) in boxes], dtype=np.float64)
    median_area = float(np.median(areas))

    X, y = _load_template_bank()
    global _FEATURE_MEAN, _FEATURE_STD
    _FEATURE_MEAN = _FEATURE_STD = None  # recompute normalization per call
    if len(X):
        X = _normalize_features(X)

    result = []
    for (x, y0, w, h), area in zip(boxes, areas):
        if area < median_area * zero_area_ratio:
            result.append("0")
            continue
        crop = th[y0:y0 + h, x:x + w]
        feat = _glyph_features(crop)
        if len(X):
            feat_n = (feat - _FEATURE_MEAN) / _FEATURE_STD
            result.append(_knn_classify(feat_n, X, y))
        else:
            result.append("?")
    return "".join(result)


def build_templates(image_path: str, ground_truth: str, source_tag: str = None):
    """
    Teach the classifier a new sample: given an image and the digit string
    it's known to contain (same left-to-right order/length as the glyphs
    found), crop each glyph and save it into templates/ labeled with its
    digit. Invalidates the cached feature bank so the next extract_digits()
    call picks up the new sample automatically.
    """
    if source_tag is None:
        source_tag = os.path.splitext(os.path.basename(image_path))[0]

    th = _preprocess(image_path)
    boxes = _segment_glyphs(th)
    if len(boxes) != len(ground_truth):
        raise ValueError(
            f"Found {len(boxes)} glyphs but ground_truth has {len(ground_truth)} digits."
        )
    os.makedirs(TEMPLATE_DIR, exist_ok=True)
    for (x, y, w, h), digit in zip(boxes, ground_truth):
        crop = th[y:y + h, x:x + w]
        cv2.imwrite(os.path.join(TEMPLATE_DIR, f"{digit}_{source_tag}_{x}.png"), crop)

    if os.path.exists(FEATURES_PATH):
        os.remove(FEATURES_PATH)  # force feature cache rebuild


def extract_id_details(image_path: str) -> dict:
    """Strict ID API: recognize 14 digits or return needs_review with diagnostics."""
    return _strict_module().recognize(image_path, _segment_glyphs, _deskew)


from functools import lru_cache


@lru_cache(maxsize=1)
def _strict_module():
    import importlib.util
    path = os.path.join(os.path.dirname(__file__), 'strict_id.py')
    spec = importlib.util.spec_from_file_location('strict_front_id', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def extract_digits(image_path: str, zero_area_ratio: float = 0.32) -> str:
    """Return exactly 14 recognized digits, or raise ValueError for review.

    zero_area_ratio is retained for call compatibility; the strict CNN route
    does not use the old area-based zero heuristic.
    """
    result = extract_id_details(image_path)
    if result['text'] is None:
        raise ValueError(result['reason'])
    return result['text']


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 extract_digits.py <image_path>")
        sys.exit(1)
    print(extract_digits(sys.argv[1]))

