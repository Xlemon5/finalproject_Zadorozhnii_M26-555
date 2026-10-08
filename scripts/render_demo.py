"""Создание GIF из текстового вывода demo.cast без изменения содержания"""

import argparse
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FONT_SIZE = 14
LINE_HEIGHT = 21
PADDING = 20
HEADER_HEIGHT = 50
BACKGROUND = "#111827"
FOREGROUND = "#e5e7eb"
COMMAND_COLOR = "#86efac"
ERROR_COLOR = "#fca5a5"


def load_font(path: str | None):
    """Выбирает моноширинный шрифт с кириллицей"""
    if path:
        return ImageFont.truetype(path, FONT_SIZE)
    candidates = (
        "/System/Library/Fonts/Menlo.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "DejaVuSansMono.ttf",
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, FONT_SIZE)
        except OSError:
            continue
    raise ValueError("Укажите моноширинный шрифт с кириллицей через --font")


def render(source: Path, target: Path, font) -> None:
    """Воспроизводит текст и временные интервалы записанного сеанса"""
    records = [json.loads(line) for line in source.read_text().splitlines()]
    header, events = records[0], records[1:]
    width, height = header["width"], header["height"]
    cell_width = math.ceil(font.getlength("M"))
    lines = [""]
    column = 0
    frames, timestamps = [], []
    for timestamp, kind, text in events:
        if kind != "o":
            continue
        if "\x1b" in text:
            raise ValueError("Этот рендерер поддерживает текст без ANSI-команд")
        for character in text:
            if character == "\r":
                column = 0
            elif character == "\n":
                lines.append("")
                column = 0
            elif character == "\b":
                column = max(0, column - 1)
            else:
                if column >= width:
                    lines.append("")
                    column = 0
                line = lines[-1].ljust(column)
                lines[-1] = line[:column] + character + line[column + 1 :]
                column += 1
        lines = lines[-height:]
        frame = Image.new(
            "RGB",
            (
                width * cell_width + PADDING * 2,
                height * LINE_HEIGHT + HEADER_HEIGHT + PADDING,
            ),
            BACKGROUND,
        )
        draw = ImageDraw.Draw(frame)
        draw.text(
            (PADDING, 14),
            "ValutaTrade Hub | Реальные API и CLI",
            font=font,
            fill=COMMAND_COLOR,
        )
        for index, line in enumerate(lines):
            color = FOREGROUND
            if line.startswith("> "):
                color = COMMAND_COLOR
            elif line.startswith(
                ("Недостаточно", "Неизвестная", "'amount'", "Сначала выполните")
            ):
                color = ERROR_COLOR
            draw.text(
                (PADDING, HEADER_HEIGHT + index * LINE_HEIGHT),
                line,
                font=font,
                fill=color,
            )
        frame = frame.convert("P", palette=Image.Palette.ADAPTIVE, colors=32)
        if timestamps and timestamp - timestamps[-1] < 0.04:
            frames[-1] = frame
        else:
            frames.append(frame)
            timestamps.append(timestamp)
    if not frames:
        raise ValueError("В записи нет вывода терминала")
    durations = [
        max(40, round((b - a) * 1000)) for a, b in zip(timestamps, timestamps[1:])
    ]
    target.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        target,
        save_all=True,
        append_images=frames[1:],
        duration=[*durations, 2500],
        loop=0,
        optimize=True,
    )
    print(f"GIF: {target}; кадров: {len(frames)}")


def main() -> None:
    """Преобразует asciicast v2 в GIF с помощью необязательного Pillow"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=PROJECT_ROOT / "docs/demo.cast")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "docs/demo.gif")
    parser.add_argument("--font", help="путь к моноширинному TTF/TTC-шрифту")
    args = parser.parse_args()
    render(args.input, args.output, load_font(args.font))


if __name__ == "__main__":
    main()
