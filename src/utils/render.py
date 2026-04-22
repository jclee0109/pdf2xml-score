import subprocess
from pathlib import Path
from PIL import Image


def render_pdf(pdf_path: str | Path, output_dir: str | Path, dpi: int = 300) -> list[Path]:
    """PDF를 페이지별 PNG로 렌더링. 페이지 순서대로 반환."""
    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    prefix = output_dir / "page"
    subprocess.run(
        ["pdftoppm", "-r", str(dpi), "-png", str(pdf_path), str(prefix)],
        check=True,
        capture_output=True,
    )

    pages = sorted(output_dir.glob("page-*.png"))
    if not pages:
        raise RuntimeError(f"pdftoppm produced no output in {output_dir}")
    return pages


def load_image(path: Path) -> Image.Image:
    return Image.open(path).convert("RGB")


def crop_system(img: Image.Image, y_top_px: int, y_bottom_px: int) -> Image.Image:
    return img.crop((0, y_top_px, img.width, y_bottom_px))


def crop_part(img: Image.Image, system_y_top: int, system_y_bottom: int,
              part_index: int, n_parts: int, extra_top: int = 20, extra_bottom: int = 5) -> Image.Image:
    """
    active_parts 내 part_index 기준으로 crop.
    extra_top: 코드 심볼 여백 포함
    """
    system_h = system_y_bottom - system_y_top
    part_h = system_h / n_parts
    y_top = int(system_y_top + part_h * part_index - extra_top)
    y_bot = int(system_y_top + part_h * (part_index + 1) + extra_bottom)
    y_top = max(0, y_top)
    y_bot = min(img.height, y_bot)
    return img.crop((0, y_top, img.width, y_bot))


def crop_part_range(img: Image.Image, system_y_top: int, system_y_bottom: int,
                    start_index: int, end_index: int, n_parts: int,
                    extra_top: int = 20, extra_bottom: int = 5) -> Image.Image:
    """
    start_index ~ end_index (inclusive) 범위의 파트를 한 번에 crop.
    Piano treble + bass 같이 연속된 두 파트를 묶을 때 사용.
    """
    system_h = system_y_bottom - system_y_top
    part_h = system_h / n_parts
    y_top = int(system_y_top + part_h * start_index - extra_top)
    y_bot = int(system_y_top + part_h * (end_index + 1) + extra_bottom)
    y_top = max(0, y_top)
    y_bot = min(img.height, y_bot)
    return img.crop((0, y_top, img.width, y_bot))
