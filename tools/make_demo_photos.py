"""生成课堂演示用的"手机拍照题图"（3 张）。

为什么需要：图片识别是阶段三之后新增的能力，现场演示必须有一张**像真照片**的题图。
原先 tests/fixtures/题目样例.png 只有 0.3 KB（代码画的三条黑线），无法用于演示。

每张图都刻意埋了一个**真实学生易错点**，这样演示流程可以自然接上
「识别图片 → 判断题型 → 定位错误 → 按精细提示讲解」的完整闭环：

  01 高等数学：洛必达连用两次，忽略了「每步都要验证 0/0 型」
  02 分析化学：EDTA 标液用 mL 代入公式，单位换算差 1000 倍（mg / μg）
  03 线性代数：把 |2A| 当成 2|A|，漏了 n 阶方阵要乘 2³

成像风格：米白纸面 + 台灯光照不均 + 轻微透视倾斜 + 相机噪声，
手写用楷体（simkai），印刷用宋体（simsun），红笔用微软雅黑粗体。

运行：python tools/make_demo_photos.py [输出目录]
"""

from __future__ import annotations

import math
import pathlib
import random
import sys
from typing import List, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "deliverables" / "演示素材"

F_SONG = "C:/Windows/Fonts/simsun.ttc"          # 印刷正文
F_SONG_B = "C:/Windows/Fonts/msyhbd.ttc"        # 印刷标题
F_HAND = "C:/Windows/Fonts/simkai.ttf"          # 手写（楷体）
F_RED = "C:/Windows/Fonts/msyhbd.ttc"           # 红笔批注

PAPER = (252, 250, 243)
INK = (28, 30, 34)
HAND_INK = (36, 42, 88)      # 蓝色圆珠笔
PENCIL = (92, 92, 96)
RED = (196, 42, 42)
RULE = (196, 206, 216)


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


class Sheet:
    """一张"白纸"，负责排版、手写、红笔批注与拍照效果。"""

    def __init__(self, w: int = 1240, h: int = 1650) -> None:
        self.w, self.h = w, h
        self.img = Image.new("RGB", (w, h), PAPER)
        self.d = ImageDraw.Draw(self.img)
        self.y = 96            # 当前书写纵坐标
        self.margin = 92
        self.rng = random.Random(20261013)

    # ---------------------------------------------------------------- 印刷
    def title(self, text: str, size: int = 34) -> None:
        f = _font(F_SONG_B, size)
        self.d.text((self.margin, self.y), text, font=f, fill=INK)
        self.y += size + 18
        self.d.line((self.margin, self.y, self.w - self.margin, self.y), fill=INK, width=2)
        self.y += 30

    def section(self, text: str, size: int = 26) -> None:
        f = _font(F_SONG_B, size)
        self.d.text((self.margin, self.y), text, font=f, fill=INK)
        self.y += size + 14

    def line(self, text: str, size: int = 24, indent: int = 0, gap: int = 14,
             font_path: str = F_SONG, color: Tuple[int, int, int] = INK) -> None:
        f = _font(font_path, size)
        self.d.text((self.margin + indent, self.y), text, font=f, fill=color)
        self.y += size + gap

    def rule_box(self, height: int, label: str = "") -> None:
        """画一个"解答区"方框（模拟答题纸的框线）。"""
        top = self.y
        if label:
            f = _font(F_SONG, 20)
            self.d.text((self.margin + 6, top + 6), label, font=f, fill=(150, 150, 150))
            top += 28
        self.d.rounded_rectangle(
            (self.margin, top, self.w - self.margin, top + height),
            radius=6, outline=RULE, width=2)
        self.y = top + height + 26

    # ---------------------------------------------------------------- 手写
    def hand(self, text: str, size: int = 30, indent: int = 40, gap: int = 16,
             color: Tuple[int, int, int] = HAND_INK, jitter: int = 2) -> None:
        """逐字绘制并加微小抖动/旋转，避免看起来像印刷体。"""
        f = _font(F_HAND, size)
        x = self.margin + indent
        for ch in text:
            dy = self.rng.randint(-jitter, jitter)
            dx = self.rng.randint(-1, 1)
            self.d.text((x + dx, self.y + dy), ch, font=f, fill=color)
            x += self.d.textlength(ch, font=f)
        self.y += size + gap

    def red(self, text: str, size: int = 32, x: int | None = None,
            y: int | None = None) -> None:
        f = _font(F_RED, size)
        self.d.text((x if x is not None else self.w - 380,
                     y if y is not None else self.y - 40), text, font=f, fill=RED)

    def red_circle(self, x: int, y: int, rx: int, ry: int) -> None:
        self.d.ellipse((x - rx, y - ry, x + rx, y + ry), outline=RED, width=4)

    def red_tick(self, x: int, y: int) -> None:
        self.d.line((x, y, x + 16, y + 20), fill=RED, width=5)
        self.d.line((x + 16, y + 20, x + 46, y - 26), fill=RED, width=5)

    # ---------------------------------------------------------------- 拍照
    def shoot(self, out: pathlib.Path, *, tilt: float = -0.9, blur: float = 0.6,
              shade: float = 0.34, seed: int = 7) -> pathlib.Path:
        """模拟手机拍照：光照渐变 + 轻微倾斜 + 失焦 + 颗粒噪声 + 暗角。

        注意：不要用 Image.QUAD 做透视——参数顺序写错会让整张图被旋转 90°，
        实测很容易踩坑。小幅 rotate 就足够有"手持拍照"的感觉。
        """
        rng = random.Random(seed)
        img = self.img.convert("L").convert("RGB")   # 略微去色，更像室内灯光

        # 1) 台灯光照：左上亮、右下暗
        shade_layer = Image.new("L", (self.w, self.h), 0)
        sd = ImageDraw.Draw(shade_layer)
        for i in range(90):
            t = i / 89
            margin = t * min(self.w, self.h) * 0.62
            sd.ellipse((margin * 0.4, margin * 0.2,
                        self.w - margin * 0.2, self.h - margin * 0.35),
                       fill=int(232 + 23 * (1 - t)))
        shade_layer = shade_layer.filter(ImageFilter.GaussianBlur(70))
        dark = Image.new("RGB", (self.w, self.h), (int(255 * (1 - shade)),) * 3)
        img = Image.composite(img, Image.blend(img, dark, 0.8), shade_layer)

        # 2) 轻微倾斜（模拟手持角度，不改变内容方向）
        img = img.rotate(tilt, resample=Image.BICUBIC, expand=False,
                         fillcolor=(232, 230, 224))

        # 3) 轻微失焦 + 颗粒噪声
        img = img.filter(ImageFilter.GaussianBlur(blur))
        px = img.load()
        for _ in range(int(self.w * self.h * 0.010)):
            x, y = rng.randrange(self.w), rng.randrange(self.h)
            r, g, b = px[x, y]
            n = rng.randint(-8, 8)
            px[x, y] = (max(0, min(255, r + n)), max(0, min(255, g + n)),
                        max(0, min(255, b + n)))

        # 4) 暗角
        vig = Image.new("L", (self.w, self.h), 0)
        vd = ImageDraw.Draw(vig)
        vd.ellipse((-self.w * 0.16, -self.h * 0.10, self.w * 1.16, self.h * 1.10), fill=255)
        vig = vig.filter(ImageFilter.GaussianBlur(self.w * 0.09))
        img = Image.composite(img, Image.blend(img, Image.new("RGB", img.size, (0, 0, 0)), 0.5), vig)

        out.parent.mkdir(parents=True, exist_ok=True)
        img.save(out, "JPEG", quality=88, dpi=(300, 300), optimize=True)
        return out


def photo_1_limit(out: pathlib.Path) -> pathlib.Path:
    """高数：洛必达连用，学生漏了"每步验证 0/0 型"。"""
    s = Sheet()
    s.title("高等数学 · 第一章 习题 1-7")
    s.line("姓名：李明轩        班级：计科 2302        日期：2026.10.12", 20, gap=24,
           font_path=F_SONG)
    s.section("三、计算下列极限（每题 10 分）")
    s.line("3.  lim(x→0)  (sin x - x) / x^3", 30, gap=22, font_path=F_SONG_B)
    s.line("4.  lim(x→0)  (e^x - 1 - x) / x^2", 30, gap=22, font_path=F_SONG_B)
    s.rule_box(560, "解答（请写出完整步骤）")

    s.y = 560
    s.hand("解：原式属于 0/0 型，可用洛必达法则。", 30)          # 基线 ≈ 590
    s.hand("第一次：lim (cos x - 1) / (3x^2)    仍为 0/0", 30)   # 基线 ≈ 636
    s.hand("第二次：lim (-sin x) / (6x)", 30)                    # 基线 ≈ 682 ← 错误在此行
    s.hand("第三次：lim (-cos x) / 6 = -1/6", 30)                # 基线 ≈ 728
    s.y += 8
    s.hand("所以原式 = -1/6", 32)
    # 圈住"第二次"那行（错误发生处），批注写在右侧空白
    s.red_circle(430, 700, 250, 44)
    s.red("第二次后已不是 0/0", 30, x=700, y=664)
    s.red("不能再用洛必达", 30, x=700, y=708)
    return s.shoot(out, tilt=-1.1, seed=11)


def photo_2_edta(out: pathlib.Path) -> pathlib.Path:
    """分析化学：EDTA 标液用 mL 代入公式，单位差 1000 倍。"""
    s = Sheet()
    s.title("分析化学实验报告 · 水的总硬度测定")
    s.line("实验者：郑志        同组：韩武晋        实验日期：2026.10.08", 20, gap=24)
    s.section("【数据处理】")
    s.line("已知：EDTA 标准溶液浓度 c = 0.01000 mol/L", 24)
    s.line("      水样体积 V(水样) = 50.00 mL", 24)
    s.line("      M(CaCO3) = 100.09 g/mol", 24, gap=22)
    s.line("      V(EDTA) 三次平行测定：12.35 mL、12.40 mL、12.38 mL", 24, gap=26)
    s.rule_box(500, "计算过程")

    s.y = 470
    s.hand("平均消耗 V(EDTA) = (12.35 + 12.40 + 12.38) / 3 = 12.38 mL", 30)
    s.y += 6
    s.hand("由 c(EDTA)·V(EDTA) = c(CaCO3)·V(水样) 得", 30)
    s.y += 6
    s.hand("总硬度 = (0.01000 × 12.38 × 100.09) / 50.00", 30)
    s.y += 6
    s.hand("      = 0.2478 mg/L", 32)
    s.red_circle(560, 800, 210, 40)
    s.red("单位换算错了", 30, x=770, y=772)
    s.red("mL 要换成 L（÷1000）", 28, x=750, y=812)
    return s.shoot(out, tilt=0.8, seed=23)


def photo_3_matrix(out: pathlib.Path) -> pathlib.Path:
    """线性代数：|2A| 当成 2|A|，漏了 n 阶方阵要乘 2³。"""
    s = Sheet()
    s.title("线性代数 · 第二章 行列式 作业")
    s.line("学号：2023110245        姓名：韩武晋", 20, gap=24)
    s.section("四、填空与计算")
    s.line("7. 设 A 为 3 阶方阵，已知 |A| = 2，求 |2A| 与 |A*|。", 26, gap=22)
    s.line("8. 设 A 为 4 阶方阵，|A| = -1，求 |A²| 与 |3A|。", 26, gap=26)
    s.rule_box(470, "解答")

    s.y = 450
    s.hand("解：|2A| = 2|A| = 2 × 2 = 4", 32)
    s.y += 8
    s.hand("    |A*| = |A|^(n-1) = |A|^2 = 4", 32)
    s.y += 12
    s.hand("所以 |2A| = 4，|A*| = 4", 32)
    s.red_circle(430, 528, 200, 38)
    s.red("漏了阶数", 30, x=640, y=498)
    s.red("|kA| = k^n |A|", 30, x=640, y=540)
    return s.shoot(out, tilt=-0.6, seed=31)


def main() -> int:
    out_dir = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    out_dir.mkdir(parents=True, exist_ok=True)
    jobs = [
        ("01_高数_洛必达题.jpg", photo_1_limit),
        ("02_分析化学_EDTA硬度题.jpg", photo_2_edta),
        ("03_线性代数_行列式题.jpg", photo_3_matrix),
    ]
    for name, fn in jobs:
        path = fn(out_dir / name)
        kb = path.stat().st_size / 1024
        print(f"  已生成 {name}  ({kb:.0f} KB)")
    print(f"\n输出目录：{out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
