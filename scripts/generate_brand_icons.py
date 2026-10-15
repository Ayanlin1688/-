"""Rebuild the LIN icon assets from the tracked original with Pillow."""
from pathlib import Path
import argparse
import shutil

from PIL import Image, ImageDraw, ImageFont, ImageOps


ROOT = Path(__file__).resolve().parent.parent
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def _rounded(image):
    mask = Image.new('L', image.size)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, image.width - 1, image.height - 1), radius=round(image.width * 0.22), fill=255)
    image.putalpha(mask)
    return image


def _small_icon():
    size = 1024
    image = Image.new('RGBA', (size, size))
    drawing = ImageDraw.Draw(image)
    for row in range(size):
        fraction = row / (size - 1)
        color = tuple(round(start + (end - start) * fraction)
                      for start, end in zip((92, 211, 115), (8, 145, 67)))
        drawing.line((0, row, size, row), fill=color)
    drawing.rounded_rectangle((80, 195, 944, 757), radius=170, fill=(245, 255, 247))
    drawing.polygon(((270, 720), (270, 855), (445, 720)), fill=(245, 255, 247))
    font_paths = (Path('C:/Windows/Fonts/arialbd.ttf'), Path('C:/Windows/Fonts/segoeuib.ttf'))
    font_path = next((path for path in font_paths if path.is_file()), 'DejaVuSans-Bold.ttf')
    font = ImageFont.truetype(str(font_path), 360)
    drawing.text((512, 481), 'LIN', font=font, anchor='mm', fill=(13, 137, 53), stroke_width=1)
    return _rounded(image)


def _resize(image, size):
    resized = image.resize((size, size), Image.Resampling.LANCZOS)
    resized.putalpha(resized.getchannel('A').point(lambda alpha: 0 if alpha < 8 else alpha))
    return resized


def generate(source=None):
    assets = ROOT / 'assets'
    original = assets / 'source' / 'icon-lin.png'
    original.parent.mkdir(parents=True, exist_ok=True)
    if source is not None and Path(source).resolve() != original.resolve():
        shutil.copyfile(source, original)
    with Image.open(original) as source_image:
        square = ImageOps.fit(source_image.convert('RGBA'), (2880, 2880), Image.Resampling.LANCZOS)
        tile = ImageOps.fit(square.crop((244, 220, 2620, 2552)), (2048, 2048), Image.Resampling.LANCZOS)
    tile = _rounded(tile)
    _resize(tile, 256).save(assets / 'app.png')
    _resize(tile, 512).save(assets / 'app-512.png')
    small = _small_icon()
    _resize(small, 256).save(assets / 'app-small.png')
    frames = [
        _resize(small if size <= 40 else tile, size)
        for size in SIZES
    ]
    frames[-1].save(assets / 'app.ico', format='ICO', sizes=[(size, size) for size in SIZES],
                    append_images=frames[:-1])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', nargs='?', type=Path)
    generate(parser.parse_args().source)
