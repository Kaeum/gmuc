import base64
import io
import re
import sys
from collections import Counter
from typing import Iterable, Tuple

from PIL import Image, ImageOps, ImageFilter
import pytesseract


def _strip_data_uri_prefix(b64_data: str) -> str:
    """Remove data URI prefix if provided (e.g., data:image/png;base64,XXX)."""
    prefix_sep = b64_data.find(",")
    if prefix_sep != -1 and "base64" in b64_data[:prefix_sep].lower():
        return b64_data[prefix_sep + 1 :]
    return b64_data


def _crop_to_content(img: Image.Image) -> Image.Image:
    """Crop to bounding box of non-white content after inversion."""
    inv = ImageOps.invert(img.convert("L"))
    bbox = inv.getbbox()
    if bbox:
        return img.crop(bbox).convert("L")
    return img.convert("L")


def _adaptive_threshold(im: Image.Image, factor: float) -> Image.Image:
    """Threshold based on image intensity percent to adapt to varied contrast."""
    gray = im.convert("L")
    lo, hi = gray.getextrema()
    cutoff = int(lo + (hi - lo) * factor)
    return gray.point(lambda x: 0 if x < cutoff else 255, "1")


def _preprocess_variants(img: Image.Image) -> Iterable[Tuple[str, Image.Image]]:
    """Yield multiple preprocessing variants to maximize OCR success.

    축소된 세트지만 가독성에 효과적인 조합(업스케일+적응 임계+노이즈 제거)에 집중한다.
    """
    gray = img.convert("L")
    thresholds = [120, 140, 160, 180]
    scales = [1, 2, 3]  # 지나친 확대는 노이즈를 늘리므로 3배까지 제한

    def thresh(im: Image.Image, t: int) -> Image.Image:
        return im.point(lambda x: 0 if x < t else 255, "1")

    # Cropping helps cut background noise before scaling
    base_images = [("gray", gray), ("gray_cropped", _crop_to_content(img))]

    for base_name, base_img in base_images:
        # Adaptive threshold + upscale + light denoise
        for factor in (0.45, 0.55, 0.65):
            adaptive = _adaptive_threshold(base_img, factor)
            denoised = adaptive.filter(ImageFilter.MedianFilter(size=3))
            for scale in scales:
                scaled = denoised if scale == 1 else denoised.resize(
                    (denoised.width * scale, denoised.height * scale), Image.LANCZOS
                )
                yield f"{base_name}_adaptive{int(factor*100)}_scale{scale}x", scaled

        # Basic threshold sweeps with scaling
        for scale in scales:
            scaled = base_img if scale == 1 else base_img.resize((base_img.width * scale, base_img.height * scale), Image.LANCZOS)
            for t in thresholds:
                yield f"{base_name}_thresh{t}_scale{scale}x", thresh(scaled, t)

        # Invert + autocontrast + median filter variants
        inv = ImageOps.invert(base_img)
        auto = ImageOps.autocontrast(inv)
        median = auto.filter(ImageFilter.MedianFilter(size=3))
        for scale in scales:
            scaled = median if scale == 1 else median.resize((median.width * scale, median.height * scale), Image.LANCZOS)
            for t in thresholds:
                yield f"{base_name}_invert_auto_median_thresh{t}_scale{scale}x", thresh(scaled, t)

        # Gaussian blur to smooth jagged noise before threshold
        blur = base_img.filter(ImageFilter.GaussianBlur(radius=1))
        for scale in scales:
            scaled = blur if scale == 1 else blur.resize((blur.width * scale, blur.height * scale), Image.LANCZOS)
            for t in thresholds:
                yield f"{base_name}_gaussian_thresh{t}_scale{scale}x", thresh(scaled, t)

        # Min/Max filter combo to reinforce strokes
        minmax = base_img.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))
        for scale in scales:
            scaled = minmax if scale == 1 else minmax.resize((minmax.width * scale, minmax.height * scale), Image.LANCZOS)
            for t in thresholds:
                yield f"{base_name}_minmax_thresh{t}_scale{scale}x", thresh(scaled, t)

        # Morphology: 열림/닫힘으로 노이즈 제거 및 획 보강
        opened = base_img.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))
        closed = base_img.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.MinFilter(3))
        for variant_name, variant in (("opened", opened), ("closed", closed)):
            for scale in scales:
                scaled = variant if scale == 1 else variant.resize((variant.width * scale, variant.height * scale), Image.LANCZOS)
                for t in thresholds:
                    yield f"{base_name}_{variant_name}_thresh{t}_scale{scale}x", thresh(scaled, t)


def _normalize_text(raw_text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", raw_text.lower())


def _apply_confusion_map(text: str) -> str:
    """Map commonly confused glyphs to improve hit rate."""
    m = {"o": "0", "l": "1", "i": "1", "s": "5", "z": "2"}
    return "".join(m.get(c, c) for c in text)


def extract_code_from_base64_png(b64_png: str) -> str:
    """
    Decode a base64-encoded PNG, run OCR, and return a 5-character string
    consisting of lowercase letters and digits.
    """
    if not b64_png:
        raise ValueError("base64 PNG 데이터가 비어 있습니다.")

    cleaned = _strip_data_uri_prefix(b64_png.strip())
    try:
        img_bytes = base64.b64decode(cleaned, validate=True)
    except (base64.binascii.Error, ValueError) as e:
        raise ValueError("유효한 base64 문자열이 아닙니다.") from e

    try:
        with Image.open(io.BytesIO(img_bytes)) as img:
            base_image = img.copy()
    except Exception as e:
        raise ValueError("PNG 이미지를 열 수 없습니다.") from e

    psm_options = [7, 8, 6]
    candidates: Counter[str] = Counter()
    last_raw = ""

    for variant_name, processed in _preprocess_variants(base_image):
        for psm in psm_options:
            config = (
                f"--dpi 300 --oem 1 --psm {psm} "
                f"-c tessedit_char_whitelist=abcdefghijklmnopqrstuvwxyz0123456789"
            )
            raw_text: str = pytesseract.image_to_string(processed, config=config)
            last_raw = raw_text
            norm = _normalize_text(raw_text)
            fixed = _apply_confusion_map(norm)
            for candidate in (norm, fixed):
                if len(candidate) == 5:
                    candidates[candidate] += 1

    if candidates:
        return candidates.most_common(1)[0][0]

    # 실패 시에도 예외를 던지지 않고, best-effort 결과를 반환
    fallback = _normalize_text(last_raw)
    fallback = _apply_confusion_map(fallback)
    return fallback[:5] if fallback else ""


def main():
    """
    CLI entry: accepts base64 PNG from stdin (pipeline) or interactive prompt.
    Outputs the 5-character OCR result to stdout, exits non-zero on error.
    """
    data = ""
    if not sys.stdin.isatty():
        data = sys.stdin.read()

    if not data.strip():
        try:
            data = input("base64 PNG 입력: ")
        except EOFError:
            sys.exit("입력이 제공되지 않았습니다.")

    data = data.strip()
    if not data:
        sys.exit("입력이 비어 있습니다.")

    try:
        code = extract_code_from_base64_png(data)
    except Exception as e:
        print(f"오류: {e}")
        sys.exit(1)



if __name__ == "__main__":
    main()
