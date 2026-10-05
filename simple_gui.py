# -*- coding: utf-8 -*-
"""
Minecraft 整合包更新器 - AE 风格 GUI
增量更新 · 配置保留 · 一键回退
"""

import os
import sys
import json
import ctypes
import threading
import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext
from pathlib import Path
from PIL import Image, ImageDraw, ImageTk

from simple_updater import SimpleUpdater, format_size, cleanup_zip_cache


# ==================== DPI 与分辨率自适应 ====================

def _setup_dpi_awareness():
    """设置 Windows DPI 感知，避免高分屏模糊和布局错乱"""
    if sys.platform != "win32":
        return 1.0
    try:
        # 尝试设置 Per-Monitor DPI 感知（Win10+）
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            # 回退到系统 DPI 感知
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass
    return _get_dpi_scale()


def _get_dpi_scale():
    """获取当前 DPI 缩放比例"""
    if sys.platform != "win32":
        return 1.0
    try:
        hdc = ctypes.windll.user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
        ctypes.windll.user32.ReleaseDC(0, hdc)
        return dpi / 96.0
    except (AttributeError, OSError):
        return 1.0


def _calc_window_size(screen_w, screen_h, dpi_scale=1.0):
    """
    根据屏幕分辨率和DPI缩放，自动计算合适的窗口尺寸。
    目标：窗口占屏幕约 70%~80% 高度，宽度按比例。
    最小 780x680，最大 1000x900。
    """
    # 可用高度（减去任务栏等）
    avail_h = int(screen_h * 0.88)
    avail_w = int(screen_w * 0.70)

    # 基础尺寸（96 DPI 下的设计尺寸）
    base_w, base_h = 860, 880

    # 按 DPI 缩放
    scaled_w = int(base_w * dpi_scale)
    scaled_h = int(base_h * dpi_scale)

    # 如果缩放后超出屏幕，按屏幕比例缩小
    if scaled_h > avail_h:
        ratio = avail_h / scaled_h
        scaled_h = avail_h
        scaled_w = int(scaled_w * ratio)
    if scaled_w > avail_w:
        ratio = avail_w / scaled_w
        scaled_w = avail_w
        scaled_h = int(scaled_h * ratio)

    # 限制范围
    min_w, min_h = 720, 680
    max_w, max_h = 1000, 950
    scaled_w = max(min_w, min(max_w, scaled_w))
    scaled_h = max(min_h, min(max_h, scaled_h))

    return scaled_w, scaled_h, min_w, min_h


# ==================== AE 风格配色（亮色版） ====================
class AETheme:
    BG_DEEP = "#f5f7fa"          # 最深背景（内容区）
    BG_MAIN = "#fafbfc"          # 主背景
    BG_PANEL = "#ffffff"         # 面板背景
    BG_PANEL_2 = "#f0f2f7"       # 次级面板
    BG_INPUT = "#ffffff"         # 输入框背景

    BORDER = "#d0d5e0"           # 边框
    BORDER_GLOW = "#a0b0cc"      # 发光边框（hover时）

    NEON_CYAN = "#0088cc"        # 霓虹青 - 主色（加深适配亮色）
    NEON_PURPLE = "#8b3dff"      # 霓虹紫 - 辅助
    NEON_PINK = "#e02c7a"        # 霓虹粉 - 强调
    NEON_GREEN = "#00a86b"       # 霓虹绿 - 成功
    NEON_RED = "#e63946"         # 霓虹红 - 错误
    NEON_YELLOW = "#d4a017"      # 霓虹黄 - 警告

    TEXT_PRIMARY = "#1a1a2e"     # 主文字
    TEXT_SECONDARY = "#4a4a60"   # 次要文字
    TEXT_MUTED = "#8a8aa0"       # 弱化文字

    FONT_TITLE = ("Microsoft YaHei UI", 18, "bold")
    FONT_SUBTITLE = ("Microsoft YaHei UI", 11)
    FONT_BODY = ("Microsoft YaHei UI", 10)
    FONT_SMALL = ("Microsoft YaHei UI", 9)
    FONT_MONO = ("Consolas", 9)
    FONT_BOLD = ("Microsoft YaHei UI", 10, "bold")
    FONT_BUTTON = ("Microsoft YaHei UI", 11, "bold")


def _glow_color(hex_color: str, factor: float = 0.4) -> str:
    """计算颜色的发光版本（亮色模式下增加亮度/减淡）"""
    hex_color = hex_color.lstrip("#")
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    # 亮色模式：与白色混合，得到更淡的发光效果
    r = int(r + (255 - r) * factor)
    g = int(g + (255 - g) * factor)
    b = int(b + (255 - b) * factor)
    return f"#{r:02x}{g:02x}{b:02x}"


# ==================== 自定义控件 ====================

class NeonButton(tk.Canvas):
    """霓虹发光按钮（PIL渲染，抗锯齿文字）"""

    def __init__(self, master, text, command=None, color=AETheme.NEON_CYAN,
                 width=120, height=36, **kwargs):
        bg = kwargs.pop("bg", AETheme.BG_PANEL)
        super().__init__(master, width=width, height=height,
                         bg=bg, highlightthickness=0, bd=0, **kwargs)
        self.text = text
        self.command = command
        self.color = color
        self.btn_width = width
        self.btn_height = height
        self.bg_color = bg
        self._hover = False
        self._disabled = False
        self._normal_img = None
        self._hover_img = None
        self._disabled_img = None
        self._current_img = None

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)
        self.bind("<ButtonRelease-1>", self._on_release)

        self._render_images()
        self._draw()

    def _hex_to_rgb(self, hex_color):
        hex_color = hex_color.lstrip("#")
        return (int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16))

    def _mix_color(self, c1, c2, alpha):
        """alpha=0返回c1, alpha=1返回c2"""
        r = int(c1[0] * (1 - alpha) + c2[0] * alpha)
        g = int(c1[1] * (1 - alpha) + c2[1] * alpha)
        b = int(c1[2] * (1 - alpha) + c2[2] * alpha)
        return (r, g, b)

    def _render_button(self, border_color, text_color, glow=False):
        """用 PIL 渲染一张按钮图"""
        from PIL import ImageDraw, ImageFont, ImageFilter

        w, h = self.btn_width, self.btn_height
        # 留边给发光效果
        pad = 6 if glow else 2
        img_w, img_h = w + pad * 2, h + pad * 2
        img = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        bg_rgb = self._hex_to_rgb(self.bg_color)
        border_rgb = self._hex_to_rgb(border_color)
        text_rgb = self._hex_to_rgb(text_color)

        # 发光效果
        if glow:
            # 画一个稍大的边框，然后模糊
            glow_layer = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
            glow_draw = ImageDraw.Draw(glow_layer)
            glow_rgb = border_rgb + (180,)
            glow_draw.rounded_rectangle(
                [pad - 1, pad - 1, img_w - pad, img_h - pad],
                radius=4, outline=glow_rgb, width=3
            )
            glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=3))
            img = Image.alpha_composite(img, glow_layer)
            draw = ImageDraw.Draw(img)

        # 主背景（填充背景色，覆盖发光在按钮内部的部分）
        draw.rounded_rectangle(
            [pad + 1, pad + 1, img_w - pad - 2, img_h - pad - 2],
            radius=3, fill=bg_rgb + (255,)
        )

        # 主边框
        draw.rounded_rectangle(
            [pad, pad, img_w - pad - 1, img_h - pad - 1],
            radius=4, outline=border_rgb + (255,), width=2
        )

        # 内边框细线
        inner_color = self._mix_color(border_rgb, bg_rgb, 0.6)
        draw.rounded_rectangle(
            [pad + 3, pad + 3, img_w - pad - 4, img_h - pad - 4],
            radius=2, outline=inner_color + (200,), width=1
        )

        # 文字（抗锯齿，加粗）
        try:
            font = ImageFont.truetype("msyhbd.ttc", 13)
        except (IOError, OSError):
            try:
                font = ImageFont.truetype("msyh.ttc", 13)
            except (IOError, OSError):
                try:
                    font = ImageFont.truetype("arialbd.ttf", 13)
                except (IOError, OSError):
                    font = ImageFont.load_default()

        # 计算文字位置
        bbox = draw.textbbox((0, 0), self.text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        text_x = (img_w - text_w) // 2 - bbox[0]
        text_y = (img_h - text_h) // 2 - bbox[1]

        # 发光文字（hover时）
        if glow:
            text_glow = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
            glow_draw = ImageDraw.Draw(text_glow)
            glow_draw.text((text_x, text_y), self.text, font=font, fill=border_rgb + (200,))
            text_glow = text_glow.filter(ImageFilter.GaussianBlur(radius=1.5))
            img = Image.alpha_composite(img, text_glow)
            draw = ImageDraw.Draw(img)

        draw.text((text_x, text_y), self.text, font=font, fill=text_rgb + (255,))

        # 裁剪到实际按钮大小（去掉pad偏移）
        return img.crop([pad, pad, pad + w, pad + h])

    def _render_images(self):
        """预渲染三种状态的按钮图"""
        # 正常状态
        self._normal_img = ImageTk.PhotoImage(
            self._render_button(
                border_color=AETheme.BORDER_GLOW,
                text_color=AETheme.TEXT_PRIMARY,
                glow=False
            )
        )
        # hover状态
        self._hover_img = ImageTk.PhotoImage(
            self._render_button(
                border_color=self.color,
                text_color=self.color,
                glow=True
            )
        )
        # 禁用状态
        self._disabled_img = ImageTk.PhotoImage(
            self._render_button(
                border_color=AETheme.BORDER,
                text_color=AETheme.TEXT_MUTED,
                glow=False
            )
        )

    def _draw(self):
        self.delete("all")
        if self._disabled:
            img = self._disabled_img
        elif self._hover:
            img = self._hover_img
        else:
            img = self._normal_img
        self._current_img = img
        self.create_image(0, 0, anchor=tk.NW, image=img)

    def _on_enter(self, event):
        if not self._disabled:
            self._hover = True
            self._draw()
            self.configure(cursor="hand2")

    def _on_leave(self, event):
        self._hover = False
        self._draw()
        self.configure(cursor="")

    def _on_click(self, event):
        if not self._disabled:
            pass

    def _on_release(self, event):
        if not self._disabled and self._hover and self.command:
            self.command()

    def configure(self, **kwargs):
        if "state" in kwargs:
            state = kwargs.pop("state")
            self._disabled = (state == tk.DISABLED)
        if "text" in kwargs:
            self.text = kwargs.pop("text")
            self._render_images()
        if kwargs:
            super().configure(**kwargs)
        self._draw()

    def config(self, **kwargs):
        self.configure(**kwargs)


class NeonLabelFrame(tk.Frame):
    """霓虹边框的面板"""

    def __init__(self, master, title="", color=AETheme.NEON_CYAN, **kwargs):
        bg = kwargs.pop("bg", AETheme.BG_MAIN)
        super().__init__(master, bg=bg, **kwargs)
        self.title_text = title
        self.glow_color = color

        # 外层发光边框容器
        self.border_frame = tk.Frame(self, bg=bg)
        self.border_frame.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        # 标题
        self._build_title()

        # 内部内容区
        self.inner = tk.Frame(self.border_frame, bg=AETheme.BG_PANEL)
        self.inner.pack(fill=tk.BOTH, expand=True, padx=2, pady=(0, 2))

    def _build_title(self):
        title_bar = tk.Frame(self.border_frame, bg=AETheme.BG_PANEL)
        title_bar.pack(fill=tk.X, padx=2, pady=(2, 0))

        # 左侧装饰线
        tk.Frame(title_bar, bg=self.glow_color, width=3, height=16).pack(side=tk.LEFT, padx=(8, 6))

        tk.Label(
            title_bar, text=self.title_text,
            bg=AETheme.BG_PANEL, fg=self.glow_color,
            font=AETheme.FONT_BOLD
        ).pack(side=tk.LEFT, pady=4)

        # 右侧横线装饰
        line = tk.Frame(title_bar, bg=AETheme.BORDER, height=1)
        line.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(10, 8))
        title_bar.update_idletasks()

    def pack(self, **kwargs):
        super().pack(**kwargs)


class NeonEntry(tk.Entry):
    """霓虹风格输入框"""

    def __init__(self, master, **kwargs):
        kwargs.setdefault("bg", AETheme.BG_INPUT)
        kwargs.setdefault("fg", AETheme.TEXT_PRIMARY)
        kwargs.setdefault("insertbackground", AETheme.NEON_CYAN)
        kwargs.setdefault("relief", tk.FLAT)
        kwargs.setdefault("bd", 0)
        kwargs.setdefault("highlightthickness", 2)
        kwargs.setdefault("highlightbackground", AETheme.BORDER)
        kwargs.setdefault("highlightcolor", AETheme.NEON_CYAN)
        kwargs.setdefault("font", AETheme.FONT_BODY)
        super().__init__(master, **kwargs)


class NeonCheckbutton(tk.Checkbutton):
    """霓虹风格复选框"""

    def __init__(self, master, **kwargs):
        kwargs.setdefault("bg", AETheme.BG_PANEL)
        kwargs.setdefault("fg", AETheme.TEXT_PRIMARY)
        kwargs.setdefault("selectcolor", AETheme.BG_INPUT)
        kwargs.setdefault("activebackground", AETheme.BG_PANEL_2)
        kwargs.setdefault("activeforeground", AETheme.NEON_CYAN)
        kwargs.setdefault("font", AETheme.FONT_BODY)
        kwargs.setdefault("bd", 0)
        kwargs.setdefault("highlightthickness", 0)
        super().__init__(master, **kwargs)


# ==================== 自定义对话框 ====================

class AEDialog:
    """AE风格的消息对话框（替换原生messagebox）"""

    @staticmethod
    def _center_dialog(dialog, parent, width, height):
        dialog.update_idletasks()
        if parent and parent.winfo_exists():
            x = parent.winfo_x() + (parent.winfo_width() - width) // 2
            y = parent.winfo_y() + (parent.winfo_height() - height) // 2
        else:
            x = (dialog.winfo_screenwidth() - width) // 2
            y = (dialog.winfo_screenheight() - height) // 2
        dialog.geometry(f"{width}x{height}+{x}+{y}")

    @staticmethod
    def _build_top_bar(dialog):
        """顶部霓虹装饰条"""
        top = tk.Frame(dialog, height=3, bg=AETheme.BG_MAIN)
        top.pack(fill=tk.X)
        tk.Frame(top, bg=AETheme.NEON_CYAN, height=2).pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Frame(top, bg=AETheme.NEON_PURPLE, height=2).pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Frame(top, bg=AETheme.NEON_PINK, height=2).pack(side=tk.LEFT, fill=tk.X, expand=True)

    @classmethod
    def show_info(cls, parent, title, message):
        """信息提示框"""
        return cls._show(parent, title, message, "info", ["确定"])

    @classmethod
    def show_warning(cls, parent, title, message):
        """警告提示框"""
        return cls._show(parent, title, message, "warning", ["确定"])

    @classmethod
    def show_error(cls, parent, title, message):
        """错误提示框"""
        return cls._show(parent, title, message, "error", ["确定"])

    @classmethod
    def ask_yesno(cls, parent, title, message):
        """确认对话框，返回 True/False"""
        result = cls._show(parent, title, message, "question", ["是", "否"])
        return result == "是"

    @classmethod
    def _show(cls, parent, title, message, icon_type, buttons):
        dialog = tk.Toplevel(parent)
        dialog.title(title)
        dialog.configure(bg=AETheme.BG_MAIN)
        dialog.transient(parent)
        dialog.resizable(False, False)
        # 先隐藏，构建完再显示，避免闪烁
        dialog.withdraw()

        # 计算合适的宽度
        lines = message.split("\n")
        max_len = max(len(line) for line in lines)
        width = max(360, min(520, max_len * 12 + 120))
        height = max(180, 120 + len(lines) * 22 + 80)

        cls._center_dialog(dialog, parent, width, height)
        cls._build_top_bar(dialog)

        content = tk.Frame(dialog, bg=AETheme.BG_MAIN, padx=24, pady=20)
        content.pack(fill=tk.BOTH, expand=True)

        # 图标
        icon_map = {
            "info": ("ℹ", AETheme.NEON_CYAN),
            "warning": ("⚠", AETheme.NEON_YELLOW),
            "error": ("✕", AETheme.NEON_RED),
            "question": ("?", AETheme.NEON_PURPLE),
        }
        icon_text, icon_color = icon_map.get(icon_type, ("ℹ", AETheme.NEON_CYAN))

        icon_label = tk.Label(
            content, text=icon_text,
            font=("Segoe UI Emoji", 28),
            fg=icon_color, bg=AETheme.BG_MAIN
        )
        icon_label.pack(side=tk.LEFT, padx=(0, 16))

        # 消息文本
        msg_label = tk.Label(
            content, text=message,
            bg=AETheme.BG_MAIN, fg=AETheme.TEXT_PRIMARY,
            font=AETheme.FONT_BODY,
            justify=tk.LEFT, wraplength=width - 120,
            anchor="w"
        )
        msg_label.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # 按钮区
        btn_frame = tk.Frame(dialog, bg=AETheme.BG_MAIN, padx=20, pady=16)
        btn_frame.pack(fill=tk.X, side=tk.BOTTOM)

        result_var = tk.StringVar(value="")

        def on_click(val):
            result_var.set(val)
            dialog.destroy()

        # 按钮颜色映射
        btn_colors = {
            "确定": AETheme.NEON_CYAN,
            "是": AETheme.NEON_GREEN,
            "否": AETheme.TEXT_SECONDARY,
            "取消": AETheme.TEXT_SECONDARY,
        }

        for btn_text in reversed(buttons):
            color = btn_colors.get(btn_text, AETheme.NEON_CYAN)
            NeonButton(
                btn_frame, text=btn_text,
                command=lambda t=btn_text: on_click(t),
                color=color, width=90, height=34,
                bg=AETheme.BG_MAIN
            ).pack(side=tk.RIGHT, padx=(6, 0))

        # UI 构建完成后再显示并获取焦点，避免闪烁
        dialog.update_idletasks()
        dialog.deiconify()
        dialog.grab_set()
        dialog.lift()
        dialog.focus_force()

        dialog.wait_window()
        return result_var.get()


# ==================== 圆形头像 ====================

def make_circle_avatar(image_path: str, size: int = 64, glow_color: str = AETheme.NEON_CYAN) -> ImageTk.PhotoImage:
    """生成圆形头像，带发光边框"""
    try:
        img = Image.open(image_path).convert("RGBA")
    except Exception:
        # 加载失败，生成一个占位的渐变圆
        img = Image.new("RGBA", (size, size), (30, 30, 60, 255))

    # 调整大小
    img = img.resize((size - 8, size - 8), Image.LANCZOS)

    # 创建圆形遮罩
    mask = Image.new("L", (size - 8, size - 8), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, size - 9, size - 9), fill=255)

    # 创建最终图像（带发光效果空间）
    result = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    # 发光效果
    glow_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow_layer)
    hex_c = glow_color.lstrip("#")
    r, g, b = int(hex_c[0:2], 16), int(hex_c[2:4], 16), int(hex_c[4:6], 16)
    for i in range(4, 0, -1):
        alpha = int(60 * (i / 4))
        glow_draw.ellipse(
            (4 - i, 4 - i, size - 4 + i, size - 4 + i),
            fill=(r, g, b, alpha)
        )
    result = Image.alpha_composite(result, glow_layer)

    # 圆形头像
    circular = Image.new("RGBA", (size - 8, size - 8), (0, 0, 0, 0))
    circular.paste(img, (0, 0), mask)

    # 边框
    border_img = Image.new("RGBA", (size - 8, size - 8), (0, 0, 0, 0))
    border_draw = ImageDraw.Draw(border_img)
    border_draw.ellipse((0, 0, size - 9, size - 9), outline=(r, g, b, 255), width=2)
    circular = Image.alpha_composite(circular, border_img)

    result.paste(circular, (4, 4), circular)
    return ImageTk.PhotoImage(result)


# ==================== 主应用 ====================

class SimpleUpdaterApp:
    """AE风格整合包更新器"""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("MC 整合包更新器")
        self.root.configure(bg=AETheme.BG_DEEP)

        # 先隐藏窗口，设置好位置大小后再显示，避免闪烁
        self.root.withdraw()

        # 根据屏幕分辨率和 DPI 自动计算窗口大小并居中
        self.root.update_idletasks()
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        dpi_scale = _get_dpi_scale()
        win_w, win_h, min_w, min_h = _calc_window_size(screen_w, screen_h, dpi_scale)
        x = (screen_w - win_w) // 2
        y = max(30, (screen_h - win_h) // 2 - 20)  # 稍微偏上
        self.root.geometry(f"{win_w}x{win_h}+{x}+{y}")
        self.root.minsize(min_w, min_h)

        # 配置
        self.config_dir = Path(os.path.dirname(os.path.abspath(__file__)))
        self.config_file = self.config_dir / "updater_config.json"
        self.config = self._load_config()

        self.updater = None
        self.current_changes = None
        self._avatar_img = None  # 保持引用防止被GC

        self._build_ui()
        self._log("程序就绪，请选择整合包目录", "info")
        # UI 构建完成后再显示窗口，避免闪烁
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _load_config(self) -> dict:
        default = {
            "old_dir": "",
            "new_dir": "",
            "delete_removed": True
        }
        if self.config_file.exists():
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    default.update(loaded)
            except (json.JSONDecodeError, IOError):
                pass
        return default

    def _save_config(self):
        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
        except IOError:
            pass

    # ==================== UI 构建 ====================

    def _build_ui(self):
        # 顶部装饰条
        top_bar = tk.Frame(self.root, bg=AETheme.BG_PANEL, height=2)
        top_bar.pack(fill=tk.X)
        # 渐变效果（用多个Frame模拟）
        self._draw_gradient_bar(top_bar)

        # 主容器
        main_container = tk.Frame(self.root, bg=AETheme.BG_MAIN)
        main_container.pack(fill=tk.BOTH, expand=True, padx=2, pady=(0, 2))

        # 标题区域
        self._build_header(main_container)

        # 内容区域
        content = tk.Frame(main_container, bg=AETheme.BG_MAIN)
        content.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 12))

        # 步骤1
        self._build_step1(content)

        # 步骤2
        self._build_step2(content)

        # 步骤3（包含详情、按钮、日志、进度条）
        self._build_step3(content)

    def _draw_gradient_bar(self, parent):
        """顶部霓虹渐变条"""
        colors = [AETheme.NEON_CYAN, AETheme.NEON_PURPLE, AETheme.NEON_PINK]
        # 用三个方块模拟渐变
        bar = tk.Frame(parent, height=3, bg=AETheme.BG_PANEL)
        bar.pack(fill=tk.X)

        # 发光点
        for i, c in enumerate(colors):
            f = tk.Frame(bar, bg=c, height=2)
            f.place(relx=i / 3, rely=0, relwidth=1 / 3, relheight=1)

    def _build_header(self, parent):
        """构建头部（标题+头像）"""
        header = tk.Frame(parent, bg=AETheme.BG_MAIN)
        header.pack(fill=tk.X, padx=16, pady=(16, 12))

        # 左侧：头像
        avatar_path = str(self.config_dir / "avatar.png")
        self._avatar_img = make_circle_avatar(avatar_path, size=64, glow_color=AETheme.NEON_CYAN)

        avatar_frame = tk.Frame(header, bg=AETheme.BG_MAIN)
        avatar_frame.pack(side=tk.LEFT, padx=(0, 14))

        avatar_label = tk.Label(avatar_frame, image=self._avatar_img, bg=AETheme.BG_MAIN)
        avatar_label.pack()

        # 中间：标题和副标题
        title_frame = tk.Frame(header, bg=AETheme.BG_MAIN)
        title_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)

        tk.Label(
            title_frame, text="MC 整合包更新器",
            bg=AETheme.BG_MAIN, fg=AETheme.TEXT_PRIMARY,
            font=("Microsoft YaHei UI", 20, "bold")
        ).pack(anchor=tk.W)

        subtitle = tk.Label(
            title_frame, text="INCREMENTAL UPDATER  ·  增量更新 · 配置保留 · 一键回退",
            bg=AETheme.BG_MAIN, fg=AETheme.TEXT_SECONDARY,
            font=("Consolas", 9)
        )
        subtitle.pack(anchor=tk.W, pady=(2, 0))

        # 装饰线
        deco_line = tk.Frame(title_frame, height=1, bg=AETheme.BORDER)
        deco_line.pack(fill=tk.X, pady=(8, 0))
        # 霓虹点缀
        tk.Frame(deco_line, bg=AETheme.NEON_CYAN, width=40, height=1).place(x=0, y=0)

        # 右侧：关于按钮
        self.btn_about = NeonButton(
            header, text="ℹ  关于",
            command=self._show_about,
            color=AETheme.NEON_PURPLE,
            width=90, height=34,
            bg=AETheme.BG_MAIN
        )
        self.btn_about.pack(side=tk.RIGHT, pady=(12, 0))

    def _build_step1(self, parent):
        """第一步：选择目录"""
        panel = NeonLabelFrame(parent, title=" STEP 01  ·  选择整合包目录",
                               color=AETheme.NEON_CYAN)
        panel.pack(fill=tk.X)

        body = panel.inner

        # 旧整合包
        row = tk.Frame(body, bg=AETheme.BG_PANEL)
        row.pack(fill=tk.X, padx=14, pady=(12, 4))

        tk.Label(row, text="本地旧整合包", bg=AETheme.BG_PANEL,
                 fg=AETheme.TEXT_SECONDARY, font=AETheme.FONT_BODY,
                 width=14, anchor="w").pack(side=tk.LEFT)

        self.old_dir_var = tk.StringVar(value=self.config.get("old_dir", ""))
        self.entry_old = NeonEntry(row, textvariable=self.old_dir_var)
        self.entry_old.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        NeonButton(row, text="浏览", command=self._browse_old,
                   color=AETheme.NEON_CYAN, width=70, height=28,
                   bg=AETheme.BG_PANEL).pack(side=tk.LEFT)

        # 新整合包
        row2 = tk.Frame(body, bg=AETheme.BG_PANEL)
        row2.pack(fill=tk.X, padx=14, pady=(8, 4))

        tk.Label(row2, text="新版本整合包", bg=AETheme.BG_PANEL,
                 fg=AETheme.TEXT_SECONDARY, font=AETheme.FONT_BODY,
                 width=14, anchor="w").pack(side=tk.LEFT)

        self.new_dir_var = tk.StringVar(value=self.config.get("new_dir", ""))
        self.entry_new = NeonEntry(row2, textvariable=self.new_dir_var)
        self.entry_new.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        NeonButton(row2, text="浏览文件夹", command=self._browse_new,
                   color=AETheme.NEON_CYAN, width=90, height=28,
                   bg=AETheme.BG_PANEL).pack(side=tk.LEFT)

        NeonButton(row2, text="选压缩包", command=self._browse_new_zip,
                   color=AETheme.NEON_PURPLE, width=100, height=28,
                   bg=AETheme.BG_PANEL).pack(side=tk.LEFT, padx=(6, 0))

        # 选项行
        opt_row = tk.Frame(body, bg=AETheme.BG_PANEL)
        opt_row.pack(fill=tk.X, padx=14, pady=(10, 4))

        self.preserve_config_var = tk.BooleanVar(value=self.config.get("preserve_config", False))
        NeonCheckbutton(
            opt_row, text="  保留配置文件（config/、options.txt 等不更新）",
            variable=self.preserve_config_var
        ).pack(side=tk.LEFT)

        opt_row2 = tk.Frame(body, bg=AETheme.BG_PANEL)
        opt_row2.pack(fill=tk.X, padx=14, pady=(2, 4))

        self.delete_removed_var = tk.BooleanVar(value=self.config.get("delete_removed", True))
        NeonCheckbutton(
            opt_row2, text="  删除新版本中没有的旧文件（推荐开启，避免残留无用文件）",
            variable=self.delete_removed_var
        ).pack(side=tk.LEFT)

        # 提示
        tip_frame = tk.Frame(body, bg=AETheme.BG_PANEL)
        tip_frame.pack(fill=tk.X, padx=14, pady=(8, 12))

        tk.Label(tip_frame, text="💡", bg=AETheme.BG_PANEL,
                 font=("Segoe UI Emoji", 11)).pack(side=tk.LEFT, padx=(0, 6))
        tk.Label(
            tip_frame,
            text="新版本可选：文件夹 / .zip / Modrinth 整合包（.mrpack）——程序自动解压并下载所需模组；"
                 "存档(saves)、资源包等用户数据永远保留",
            bg=AETheme.BG_PANEL, fg=AETheme.TEXT_MUTED,
            font=AETheme.FONT_SMALL, wraplength=620, justify="left"
        ).pack(side=tk.LEFT)

    def _build_step2(self, parent):
        """第二步：检测更新"""
        panel = NeonLabelFrame(parent, title=" STEP 02  ·  检测更新差异",
                               color=AETheme.NEON_PURPLE)
        panel.pack(fill=tk.X, pady=(10, 0))

        body = panel.inner

        btn_row = tk.Frame(body, bg=AETheme.BG_PANEL)
        btn_row.pack(fill=tk.X, padx=14, pady=12)

        self.btn_check = NeonButton(
            btn_row, text="🔍  检测差异",
            command=self._check_update_threaded,
            color=AETheme.NEON_PURPLE,
            width=140, height=36,
            bg=AETheme.BG_PANEL
        )
        self.btn_check.pack(side=tk.LEFT)

        # 统计信息
        self.stat_added_var = tk.StringVar(value="新增: --")
        self.stat_modified_var = tk.StringVar(value="修改: --")
        self.stat_removed_var = tk.StringVar(value="删除: --")
        self.stat_size_var = tk.StringVar(value="大小: --")
        self.stat_preserve_var = tk.StringVar(value="保留: --")

        stats = [
            (self.stat_added_var, AETheme.NEON_GREEN),
            (self.stat_modified_var, AETheme.NEON_CYAN),
            (self.stat_removed_var, AETheme.NEON_RED),
            (self.stat_size_var, AETheme.NEON_PURPLE),
            (self.stat_preserve_var, AETheme.NEON_YELLOW),
        ]

        for var, color in stats:
            stat_frame = tk.Frame(btn_row, bg=AETheme.BG_PANEL)
            stat_frame.pack(side=tk.LEFT, padx=(18, 0))
            # 前面的小色点
            tk.Frame(stat_frame, bg=color, width=6, height=6).pack(side=tk.LEFT, pady=7)
            tk.Label(stat_frame, textvariable=var,
                     bg=AETheme.BG_PANEL, fg=color,
                     font=AETheme.FONT_BOLD).pack(side=tk.LEFT, padx=(5, 0))

    def _build_step3(self, parent):
        """第三步：更新详情 + 操作按钮 + 运行日志（合并在一个面板里）"""
        panel = NeonLabelFrame(parent, title=" STEP 03  ·  更新详情与操作",
                               color=AETheme.NEON_PINK)
        panel.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        body = panel.inner

        # 上半部分：详情文本（占主要空间，可伸缩）
        self.detail_text = scrolledtext.ScrolledText(
            body, height=6, wrap=tk.WORD,
            font=AETheme.FONT_MONO,
            bg=AETheme.BG_DEEP, fg=AETheme.TEXT_PRIMARY,
            insertbackground=AETheme.NEON_CYAN,
            bd=0, highlightthickness=1,
            highlightbackground=AETheme.BORDER,
            relief=tk.FLAT, padx=10, pady=8
        )
        self.detail_text.pack(fill=tk.BOTH, expand=True, padx=12, pady=(10, 0))
        self.detail_text.insert(tk.END, "// 请先选择整合包目录，然后点击「检测差异」查看更新内容\n\n")
        self.detail_text.insert(tk.END, "// 👁 小提示：检测差异后可以先点「模拟更新」预览效果\n")
        self.detail_text.insert(tk.END, "//    全程只读不修改文件，确认无误再执行更新\n\n")
        self.detail_text.insert(tk.END, "// 💡 其他提示：\n")
        self.detail_text.insert(tk.END, "//   - config/ 目录下的配置文件会自动保留你的修改\n")
        self.detail_text.insert(tk.END, "//   - saves/ 存档、resourcepacks/ 资源包不会被覆盖\n")
        self.detail_text.insert(tk.END, "//   - 更新前会自动备份，出问题可以一键回退\n")
        self.detail_text.insert(tk.END, "//   - 游戏开着的时候会提醒你先关闭再更新\n")
        self.detail_text.configure(state=tk.DISABLED)

        # 配置样式标签
        self.detail_text.tag_config("title", font=("Microsoft YaHei UI", 11, "bold"), foreground=AETheme.NEON_CYAN)
        self.detail_text.tag_config("section", font=("Microsoft YaHei UI", 10, "bold"), foreground=AETheme.TEXT_PRIMARY)
        self.detail_text.tag_config("preserve", font=("Microsoft YaHei UI", 10, "bold"), foreground=AETheme.NEON_YELLOW)
        self.detail_text.tag_config("warn", font=("Microsoft YaHei UI", 10, "bold"), foreground=AETheme.NEON_RED)

        # 操作按钮
        btn_row = tk.Frame(body, bg=AETheme.BG_PANEL)
        btn_row.pack(fill=tk.X, padx=12, pady=10)

        self.btn_update = NeonButton(
            btn_row, text="⬆  立即更新",
            command=self._do_update_threaded,
            color=AETheme.NEON_GREEN,
            width=150, height=40,
            bg=AETheme.BG_PANEL
        )
        self.btn_update.pack(side=tk.LEFT)
        self.btn_update.configure(state=tk.DISABLED)

        self.btn_simulate = NeonButton(
            btn_row, text="👁  模拟更新",
            command=self._do_simulate,
            color=AETheme.NEON_CYAN,
            width=130, height=40,
            bg=AETheme.BG_PANEL
        )
        self.btn_simulate.pack(side=tk.LEFT, padx=(10, 0))
        self.btn_simulate.configure(state=tk.DISABLED)

        self.btn_rollback = NeonButton(
            btn_row, text="↩  版本回退",
            command=self._show_rollback_dialog,
            color=AETheme.NEON_YELLOW,
            width=130, height=40,
            bg=AETheme.BG_PANEL
        )
        self.btn_rollback.pack(side=tk.LEFT, padx=(10, 0))

        # 下半部分：运行日志（固定高度，不参与上方伸缩）
        log_header = tk.Frame(body, bg=AETheme.BG_PANEL)
        log_header.pack(fill=tk.X, padx=12, pady=(0, 4))
        tk.Label(
            log_header, text="📋 运行日志",
            bg=AETheme.BG_PANEL, fg=AETheme.TEXT_SECONDARY,
            font=("Microsoft YaHei UI", 9, "bold")
        ).pack(side=tk.LEFT)

        self.log_text = scrolledtext.ScrolledText(
            body, height=5, wrap=tk.WORD,
            font=("Consolas", 9),
            bg=AETheme.BG_DEEP, fg="#707888",
            insertbackground=AETheme.TEXT_MUTED,
            bd=0, highlightthickness=1,
            highlightbackground=AETheme.BORDER,
            relief=tk.FLAT, padx=8, pady=6
        )
        self.log_text.pack(fill=tk.X, padx=12, pady=(0, 10))
        self.log_text.configure(state=tk.DISABLED)

        # 进度条（放在日志下面）
        progress_frame = tk.Frame(body, bg=AETheme.BG_PANEL)
        progress_frame.pack(fill=tk.X, padx=12, pady=(0, 12))

        self.progress_label_var = tk.StringVar(value="就绪")
        tk.Label(progress_frame, textvariable=self.progress_label_var,
                 bg=AETheme.BG_PANEL, fg=AETheme.TEXT_SECONDARY,
                 font=AETheme.FONT_SMALL).pack(side=tk.LEFT)

        self._progress_canvas = tk.Canvas(
            progress_frame, height=8, bg=AETheme.BG_DEEP,
            highlightthickness=1, highlightbackground=AETheme.BORDER,
            bd=0
        )
        self._progress_canvas.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(10, 0))
        self.progress_var = tk.DoubleVar()
        self._draw_progress(0)

    def _draw_progress(self, value):
        """绘制自定义进度条"""
        self._progress_canvas.delete("all")
        w = self._progress_canvas.winfo_width()
        if w < 10:
            w = 200
        h = 10

        # 背景
        self._progress_canvas.create_rectangle(0, 0, w, h, fill=AETheme.BG_DEEP, outline="")

        # 进度条（霓虹渐变效果）
        fill_w = int(w * value / 100)
        if fill_w > 0:
            # 主体
            self._progress_canvas.create_rectangle(
                0, 0, fill_w, h,
                fill=AETheme.NEON_CYAN, outline=""
            )
            # 高光
            self._progress_canvas.create_rectangle(
                0, 0, fill_w, 3,
                fill=_glow_color(AETheme.NEON_CYAN, 0.4), outline=""
            )
            # 末端发光
            if fill_w > 5:
                for i in range(3, 0, -1):
                    alpha_color = self._fade_canvas_color(AETheme.NEON_CYAN, 0.3 * i)
                    self._progress_canvas.create_line(
                        fill_w - i, 1, fill_w - i, h - 1,
                        fill=alpha_color
                    )

    def _fade_canvas_color(self, hex_color, alpha):
        """与背景混合模拟透明度"""
        hex_color = hex_color.lstrip("#")
        bg = AETheme.BG_DEEP.lstrip("#")
        r1, g1, b1 = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
        r2, g2, b2 = int(bg[0:2], 16), int(bg[2:4], 16), int(bg[4:6], 16)
        r = int(r1 * alpha + r2 * (1 - alpha))
        g = int(g1 * alpha + g2 * (1 - alpha))
        b = int(b1 * alpha + b2 * (1 - alpha))
        return f"#{r:02x}{g:02x}{b:02x}"

    # ==================== 事件处理 ====================

    def _browse_old(self):
        path = filedialog.askdirectory(title="选择本地旧整合包目录")
        if path:
            self.old_dir_var.set(path)
            self._save_dirs()

    def _browse_new(self):
        path = filedialog.askdirectory(title="选择新版本整合包目录")
        if path:
            self.new_dir_var.set(path)
            self._save_dirs()

    def _browse_new_zip(self):
        path = filedialog.askopenfilename(
            title="选择新版本整合包压缩包",
            filetypes=[
                ("整合包（zip / mrpack）", "*.zip *.mrpack"),
                ("普通压缩包 zip", "*.zip"),
                ("Modrinth 整合包 mrpack", "*.mrpack"),
                ("所有文件", "*.*"),
            ]
        )
        if path:
            self.new_dir_var.set(path)
            self._save_dirs()

    def _save_dirs(self):
        self.config["old_dir"] = self.old_dir_var.get().strip()
        self.config["new_dir"] = self.new_dir_var.get().strip()
        self.config["delete_removed"] = self.delete_removed_var.get()
        self.config["preserve_config"] = self.preserve_config_var.get()
        self._save_config()

    def _check_update_threaded(self):
        """检测更新（线程）"""
        old_dir = self.old_dir_var.get().strip()
        new_dir = self.new_dir_var.get().strip()

        if not old_dir or not new_dir:
            AEDialog.show_warning(self.root, "提示", "请先选择旧整合包和新整合包的目录！")
            return
        if not Path(old_dir).exists():
            AEDialog.show_error(self.root, "错误", f"旧整合包目录不存在：\n{old_dir}")
            return
        new_path = Path(new_dir)
        if not new_path.exists():
            AEDialog.show_error(self.root, "错误", f"新版本路径不存在:\n{new_dir}")
            return
        if new_path.is_file() and new_path.suffix.lower() not in (".zip", ".mrpack"):
            AEDialog.show_error(
                self.root,
                "错误",
                "新版本请选择「文件夹」、「.zip」或 Modrinth 整合包「.mrpack」！\n"
                "（.7z / .rar 格式请先解压，再选择解压出来的文件夹）"
            )
            return

        self._save_dirs()

        self.btn_check.configure(state=tk.DISABLED)
        self.btn_update.configure(state=tk.DISABLED)
        self._log("开始检测差异...", "info")

        preserve_config = self.preserve_config_var.get()
        delete_removed = self.delete_removed_var.get()

        threading.Thread(
            target=self._check_update_worker,
            args=(old_dir, new_dir, preserve_config, delete_removed),
            daemon=True
        ).start()

    def _check_update_worker(self, old_dir, new_dir, preserve_config, delete_removed):
        try:
            updater = SimpleUpdater(
                old_dir=old_dir,
                new_dir=new_dir,
                progress_callback=self._on_progress,
                log_callback=self._on_log,
                preserve_config=preserve_config,
                delete_removed=delete_removed
            )
            changes = updater.compare()
            self.updater = updater
            self.root.after(0, lambda: self._on_check_done(changes))
        except Exception as e:
            self.root.after(0, lambda: self._on_check_error(e))

    def _on_check_done(self, changes: dict):
        self.current_changes = changes

        added = len(changes.get("added", []))
        modified = len(changes.get("modified", []))
        removed = len(changes.get("removed", []))
        size = format_size(changes.get("total_size", 0))
        preserve = len(changes.get("preserve_modified", []))

        self.stat_added_var.set(f"新增: {added}")
        self.stat_modified_var.set(f"修改: {modified}")
        self.stat_removed_var.set(f"删除: {removed}")
        self.stat_size_var.set(f"下载大小: {size}")
        self.stat_preserve_var.set(f"保留配置: {preserve}")

        self._show_details(changes)

        # Modrinth 整合包信息
        if changes.get("mrpack"):
            mp = changes["mrpack"]
            self._log(
                f"✓ Modrinth 整合包：{mp.get('name', '')} {mp.get('version_id', '')}"
                f"，已自动获取 {mp.get('file_count', 0)} 个清单资源",
                "info"
            )

        # NeoForge 版本检测：根据状态给出对应提示
        nf = changes.get("neoforge", {})
        if nf and nf.get("status"):
            status = nf.get("status", "ok")
            msg = nf.get("message", "")
            old_ver = nf.get("old_version", "未检测到")
            req_ver = nf.get("new_required", "未检测到")
            demanding = nf.get("demanding_mods", [])

            # 记录日志
            if status in ("too_low", "too_high", "mismatch"):
                self._log(f"⚠ {msg}", "warn")
            elif status.startswith("unknown") or status == "error":
                self._log(f"ℹ {msg}", "info")
            else:
                self._log(f"✓ {msg}", "info")

            # 需要弹窗警告的情况
            if status in ("too_low", "too_high", "mismatch"):
                # 构造详细的模组列表
                mod_list_text = ""
                if demanding:
                    mod_list_text = "\n\n要求较高的模组：\n"
                    for name, ver in demanding[:8]:
                        mod_list_text += f"  · {name}  →  {ver}+\n"
                    if len(demanding) > 8:
                        mod_list_text += f"  ... 还有 {len(demanding)-8} 个模组"

                if status == "too_low":
                    warn_title = "⚠ NeoForge 版本不足"
                    warn_msg = (
                        f"当前安装: NeoForge {old_ver}\n"
                        f"模组最低要求: NeoForge {req_ver}\n\n"
                        f"更新模组后可能无法进入游戏！\n"
                        f"请先在启动器中升级 NeoForge 到 {req_ver} 以上。"
                        f"{mod_list_text}"
                    )
                    self.root.after(100, lambda t=warn_title, m=warn_msg: 
                        AEDialog.show_warning(self.root, t, m))
                elif status == "too_high":
                    warn_title = "⚠ NeoForge 版本过高"
                    warn_msg = (
                        f"当前安装: NeoForge {old_ver}\n"
                        f"模组适配版本: {req_ver} 系列\n\n"
                        f"当前 NeoForge 主版本高于模组要求，\n"
                        f"部分旧模组可能不兼容高版本加载器。\n"
                        f"建议确认模组是否支持当前 NeoForge 版本。"
                        f"{mod_list_text}"
                    )
                    self.root.after(100, lambda t=warn_title, m=warn_msg: 
                        AEDialog.show_warning(self.root, t, m))

        # Fabric 版本检测：根据状态给出对应提示
        fb = changes.get("fabric", {})
        if fb and fb.get("status") and fb.get("status") != "unknown":
            status_f = fb.get("status", "ok")
            msg_f = fb.get("message", "")
            old_ver_f = fb.get("old_version", "未检测到")
            req_ver_f = fb.get("new_required", "未检测到")
            demanding_f = fb.get("demanding_mods", [])

            # 记录日志
            if status_f in ("too_low", "too_high", "mismatch"):
                self._log(f"⚠ {msg_f}", "warn")
            elif status_f.startswith("unknown") or status_f == "error":
                self._log(f"ℹ {msg_f}", "info")
            else:
                self._log(f"✓ {msg_f}", "info")

            # 需要弹窗警告的情况
            if status_f in ("too_low", "too_high"):
                mod_list_text = ""
                if demanding_f:
                    mod_list_text = "\n\n要求较高的模组：\n"
                    for name, ver in demanding_f[:8]:
                        mod_list_text += f"  · {name}  →  {ver}+\n"
                    if len(demanding_f) > 8:
                        mod_list_text += f"  ... 还有 {len(demanding_f)-8} 个模组"

                if status_f == "too_low":
                    warn_title = "⚠ Fabric Loader 版本不足"
                    warn_msg = (
                        f"当前安装: Fabric Loader {old_ver_f}\n"
                        f"模组最低要求: Fabric Loader {req_ver_f}\n\n"
                        f"更新模组后可能无法进入游戏！\n"
                        f"请先在启动器中升级 Fabric Loader 到 {req_ver_f} 以上。"
                        f"{mod_list_text}"
                    )
                    self.root.after(200, lambda t=warn_title, m=warn_msg: 
                        AEDialog.show_warning(self.root, t, m))
                elif status_f == "too_high":
                    warn_title = "⚠ Fabric Loader 版本过高"
                    warn_msg = (
                        f"当前安装: Fabric Loader {old_ver_f}\n"
                        f"模组适配版本: {req_ver_f} 系列\n\n"
                        f"当前 Fabric Loader 主版本高于模组要求，\n"
                        f"部分旧模组可能不兼容高版本加载器。\n"
                        f"建议确认模组是否支持当前 Fabric Loader 版本。"
                        f"{mod_list_text}"
                    )
                    self.root.after(200, lambda t=warn_title, m=warn_msg: 
                        AEDialog.show_warning(self.root, t, m))

        total = added + modified + removed
        if total == 0:
            self.btn_update.configure(state=tk.DISABLED)
            self.btn_simulate.configure(state=tk.DISABLED)
            self._log("检测完成，两个整合包文件一致，无需更新", "info")
        else:
            self.btn_update.configure(state=tk.NORMAL)
            self.btn_simulate.configure(state=tk.NORMAL)
            self._log(f"检测完成: 新增{added} 修改{modified} 删除{removed}", "info")

        self.btn_check.configure(state=tk.NORMAL)

    def _on_check_error(self, error: Exception):
        self.btn_check.configure(state=tk.NORMAL)
        self._log(f"检测失败: {error}", "error")
        AEDialog.show_error(self.root, "检测失败", str(error))

    def _show_details(self, changes: dict):
        """显示更新详情"""
        self.detail_text.configure(state=tk.NORMAL)
        self.detail_text.delete("1.0", tk.END)

        added = changes.get("added", [])
        modified = changes.get("modified", [])
        removed = changes.get("removed", [])
        preserve_modified = changes.get("preserve_modified", [])

        self.detail_text.insert(tk.END, "// ======== 更新概览 ========\n", "title")
        self.detail_text.insert(tk.END, f"   旧版本文件数: {changes.get('old_count', 0)}\n")
        self.detail_text.insert(tk.END, f"   新版本文件数: {changes.get('new_count', 0)}\n")
        self.detail_text.insert(tk.END, f"   下载大小: {format_size(changes.get('total_size', 0))}\n")

        # Modrinth 整合包信息
        mp = changes.get("mrpack")
        if mp:
            loaders = mp.get("loaders") or {}
            self.detail_text.insert(tk.END, "\n// ======== Modrinth 整合包 ========\n", "title")
            self.detail_text.insert(tk.END, f"   名称: {mp.get('name', '')}\n")
            self.detail_text.insert(tk.END, f"   版本: {mp.get('version_id', '')}\n")
            self.detail_text.insert(tk.END, f"   游戏版本: {mp.get('minecraft', '')}\n")
            if loaders:
                self.detail_text.insert(
                    tk.END,
                    "   加载器: " + "、".join(f"{k} {v}" for k, v in loaders.items()) + "\n"
                )
            self.detail_text.insert(
                tk.END,
                f"   ✓ 已自动下载清单资源 {mp.get('file_count', 0)} 个\n",
                "preserve"
            )

        # NeoForge 版本信息
        nf = changes.get("neoforge", {})
        if nf:
            old_ver = nf.get("old_version", "未检测到")
            req_ver = nf.get("new_required", "未检测到")
            status = nf.get("status", "unknown")
            msg = nf.get("message", "")
            demanding = nf.get("demanding_mods", [])

            self.detail_text.insert(tk.END, f"\n// ======== NeoForge 版本检测 ========\n", "title")
            self.detail_text.insert(tk.END, f"   当前安装: NeoForge {old_ver}\n")
            self.detail_text.insert(tk.END, f"   模组要求: {req_ver}+\n")

            status_icon = {"too_low": "⚠", "too_high": "⚠", "mismatch": "✕",
                           "ok": "✓", "unknown": "ℹ", "unknown_old": "ℹ",
                           "unknown_new": "ℹ", "error": "✕"}.get(status, "ℹ")

            status_tag = "warn" if status in ("too_low", "too_high", "mismatch", "error") else "preserve"
            self.detail_text.insert(tk.END, f"   {status_icon} {msg}\n", status_tag)

            # 显示要求较高的模组列表
            if demanding:
                self.detail_text.insert(tk.END, f"\n   要求较高的模组（前8个）：\n")
                for name, ver in demanding[:8]:
                    self.detail_text.insert(tk.END, f"     · {name}  →  NeoForge {ver}+\n")
                if len(demanding) > 8:
                    self.detail_text.insert(tk.END, f"     ... 还有 {len(demanding)-8} 个模组\n")

        # Fabric 版本信息
        fb = changes.get("fabric", {})
        if fb and fb.get("status") and fb.get("status") != "unknown":
            old_ver_f = fb.get("old_version", "未检测到")
            req_ver_f = fb.get("new_required", "未检测到")
            status_f = fb.get("status", "unknown")
            msg_f = fb.get("message", "")
            demanding_f = fb.get("demanding_mods", [])

            self.detail_text.insert(tk.END, f"\n// ======== Fabric Loader 版本检测 ========\n", "title")
            self.detail_text.insert(tk.END, f"   当前安装: Fabric Loader {old_ver_f}\n")
            self.detail_text.insert(tk.END, f"   模组要求: {req_ver_f}+\n")

            status_icon_f = {"too_low": "⚠", "too_high": "⚠", "mismatch": "✕",
                             "ok": "✓", "unknown": "ℹ", "unknown_old": "ℹ",
                             "unknown_new": "ℹ", "error": "✕"}.get(status_f, "ℹ")
            status_tag_f = "warn" if status_f in ("too_low", "too_high", "mismatch", "error") else "preserve"
            self.detail_text.insert(tk.END, f"   {status_icon_f} {msg_f}\n", status_tag_f)

            if demanding_f:
                self.detail_text.insert(tk.END, f"\n   要求较高的模组（前8个）：\n")
                for name, ver in demanding_f[:8]:
                    self.detail_text.insert(tk.END, f"     · {name}  →  Fabric {ver}+\n")
                if len(demanding_f) > 8:
                    self.detail_text.insert(tk.END, f"     ... 还有 {len(demanding_f)-8} 个模组\n")
        self.detail_text.insert(tk.END, "\n")

        if changes.get("delete_removed", True) and changes.get("risky_removed"):
            self.detail_text.insert(
                tk.END,
                f"// ⚠ 警告：本次将删除 {len(changes.get('risky_removed', []))} 个"
                f"模组/脚本/配置文件，请确认新版本目录选择正确！\n\n",
                "warn"
            )

        if added:
            self.detail_text.insert(tk.END, f"// [+] 新增文件 ({len(added)} 个)\n", "section")
            for f in added[:20]:
                self.detail_text.insert(tk.END, f"     {f}\n")
            if len(added) > 20:
                self.detail_text.insert(tk.END, f"     ... 还有 {len(added)-20} 个文件\n")
            self.detail_text.insert(tk.END, "\n")

        if modified:
            self.detail_text.insert(tk.END, f"// [~] 修改文件 ({len(modified)} 个)\n", "section")
            for f in modified[:20]:
                self.detail_text.insert(tk.END, f"     {f}\n")
            if len(modified) > 20:
                self.detail_text.insert(tk.END, f"     ... 还有 {len(modified)-20} 个文件\n")
            self.detail_text.insert(tk.END, "\n")

        if removed:
            self.detail_text.insert(tk.END, f"// [-] 删除文件 ({len(removed)} 个)\n", "section")
            for f in removed[:20]:
                self.detail_text.insert(tk.END, f"     {f}\n")
            if len(removed) > 20:
                self.detail_text.insert(tk.END, f"     ... 还有 {len(removed)-20} 个文件\n")
            self.detail_text.insert(tk.END, "\n")

        preserve_config = changes.get("preserve_config", False)

        if preserve_config:
            if preserve_modified:
                self.detail_text.insert(
                    tk.END,
                    f"// [🛡] 配置文件已保留（{len(preserve_modified)} 个配置不更新）\n",
                    "preserve"
                )
                for f in preserve_modified[:10]:
                    self.detail_text.insert(tk.END, f"     {f}\n")
                if len(preserve_modified) > 10:
                    self.detail_text.insert(tk.END, f"     ... 还有 {len(preserve_modified)-10} 个\n")
                self.detail_text.insert(tk.END, "\n")
            else:
                self.detail_text.insert(tk.END, "// [🛡] 配置文件保留（已开启）\n", "preserve")
                self.detail_text.insert(tk.END, "     本次没有需要更新的配置文件\n\n")
        else:
            self.detail_text.insert(tk.END, "// [🛡] 文件保护\n", "preserve")
            self.detail_text.insert(
                tk.END,
                "     以下用户数据永远保留，不会被更新或删除：\n"
                "     • saves/ 存档\n"
                "     • servers.dat 服务器列表\n"
                "     • resourcepacks/ 资源包\n"
                "     • shaderpacks/ 光影包\n"
                "     • screenshots/ 截图\n"
                "     配置文件(config/)将跟随整合包一起更新。\n"
                "     如需保留配置，请勾选上方「保留配置文件」选项。\n\n"
            )

        self.detail_text.configure(state=tk.DISABLED)

    def _do_simulate(self):
        """模拟更新：只展示完整的更新效果预览，不实际修改任何文件"""
        if not self.updater or not self.current_changes:
            AEDialog.show_warning(self.root, "提示", "请先点击「检测差异」！")
            return

        changes = self.current_changes
        added = len(changes.get("added", []))
        modified = len(changes.get("modified", []))
        removed = len(changes.get("removed", []))
        preserve = len(changes.get("preserve_modified", []))
        size = format_size(changes.get("total_size", 0))

        # 保留文件说明
        preserve_info = ""
        if preserve > 0:
            preserve_info = f"\n  · 保留用户配置: {preserve} 个"

        # 版本检测信息
        ver_info = ""
        nf = changes.get("neoforge", {})
        if nf and nf.get("status") and nf.get("status") not in ("unknown", "unknown_new"):
            nf_status_icon = {"too_low": "⚠", "too_high": "⚠", "ok": "✓",
                              "unknown_old": "ℹ", "error": "✕"}.get(nf.get("status"), "ℹ")
            ver_info += f"\n{nf_status_icon} NeoForge: {nf.get('message', '')}"

        fb = changes.get("fabric", {})
        if fb and fb.get("status") and fb.get("status") not in ("unknown", "unknown_new"):
            fb_status_icon = {"too_low": "⚠", "too_high": "⚠", "ok": "✓",
                              "unknown_old": "ℹ", "error": "✕"}.get(fb.get("status"), "ℹ")
            ver_info += f"\n{fb_status_icon} Fabric: {fb.get('message', '')}"

        msg = (
            f"【👁 模拟更新预览】\n\n"
            f"这只是预览，不会实际修改任何文件！\n\n"
            f"如果执行更新，将会发生以下变化：\n\n"
            f"  · 新增文件: {added} 个\n"
            f"  · 修改文件: {modified} 个\n"
            f"  · 删除文件: {removed} 个"
            f"{preserve_info}\n"
            f"  · 需要复制: {size}\n"
            f"{ver_info}\n\n"
            f"💡 小贴士：\n"
            f"  第一次使用 / 不确定目录对不对时，\n"
            f"  先模拟更新确认一下，放心了再点「立即更新」。\n"
            f"  更新前会自动备份，出问题也可以一键回退。"
        )
        AEDialog.show_info(self.root, "👁 模拟更新预览", msg)
        self._log("模拟更新：已展示更新预览（未实际修改文件）", "info")

    def _do_update_threaded(self):
        """执行更新（线程）"""
        if not self.updater or not self.current_changes:
            AEDialog.show_warning(self.root, "提示", "请先点击「检测差异」！")
            return

        # ===== 安全检查：游戏是否在运行 =====
        try:
            if SimpleUpdater.is_game_running():
                if not AEDialog.ask_yesno(
                    self.root,
                    "⚠ 检测到游戏正在运行",
                    "检测到 Minecraft 游戏正在运行中！\n\n"
                    "游戏运行时更新可能导致：\n"
                    "  · 文件被占用，更新失败\n"
                    "  · 存档损坏或配置丢失\n"
                    "  · 游戏崩溃\n\n"
                    "强烈建议先关闭游戏再更新。\n"
                    "确定要继续更新吗？"
                ):
                    return
                self._log("⚠ 用户确认游戏运行时继续更新", "warn")
        except Exception:
            pass  # 检测失败就跳过，不阻塞

        changes = self.current_changes
        added = len(changes.get("added", []))
        modified = len(changes.get("modified", []))
        removed = len(changes.get("removed", []))

        # ===== 安全保护：检测高风险的大批量删除（通常是目录选错） =====
        removed_mods = changes.get("removed_mods", [])
        added_mods = changes.get("added_mods", [])
        risky = changes.get("risky_removed", [])

        if changes.get("delete_removed", True) and (
            len(removed_mods) >= 10 or len(risky) >= 100
        ):
            # 最危险：大量删除模组却没有任何新增模组
            if len(removed_mods) >= 20 and len(added_mods) == 0:
                AEDialog.show_error(
                    self.root,
                    "已阻止更新",
                    "⚠ 检测到高风险操作，已自动阻止！\n"
                    f"本次将删除 {len(removed_mods)} 个模组，但没有新增任何模组。\n"
                    "这通常说明「新版本整合包」目录选错了。\n"
                    "请确认新版本选的是整合包解压后的文件夹\n"
                    "（里面有 mods/、kubejs/、config/）。\n"
                    "如果确认无误，请取消勾选「删除新版本中没有的旧文件」后重试。"
                )
                return

            warn = (
                "⚠ 高风险操作确认\n\n"
                f"本次更新将删除 {removed} 个文件，其中：\n"
                f"  · 模组文件：{len(removed_mods)} 个\n"
                f"  · 脚本/配置：{len(risky)} 个\n\n"
                "如果没有选错目录，一般是因为新版本确实移除了这些文件。\n"
                "请再次确认两个目录都选对了！\n\n"
                "确定要继续吗？（更新前会自动备份，可回退）"
            )
            if not AEDialog.ask_yesno(self.root, "⚠ 高风险操作确认", warn):
                return

        msg = (
            f"确认开始更新吗？\n\n"
            f"  新增文件: {added} 个\n"
            f"  修改文件: {modified} 个\n"
            f"  删除文件: {removed} 个\n"
            f"  下载大小: {format_size(changes.get('total_size', 0))}\n\n"
            f"更新前会自动备份，出问题可以回退。"
        )
        if not AEDialog.ask_yesno(self.root, "确认更新", msg):
            return

        self.btn_check.configure(state=tk.DISABLED)
        self.btn_update.configure(state=tk.DISABLED)
        self.btn_simulate.configure(state=tk.DISABLED)
        self.btn_rollback.configure(state=tk.DISABLED)
        self._log("开始更新...", "info")

        threading.Thread(target=self._do_update_worker, daemon=True).start()

    def _do_update_worker(self):
        try:
            success, msg, backup_name = self.updater.do_update(self.current_changes)
            self.root.after(0, lambda: self._on_update_done(success, msg, backup_name))
        except Exception as e:
            self.root.after(0, lambda: self._on_update_error(e))

    def _on_update_done(self, success: bool, msg: str, backup_name: str = ""):
        self.btn_check.configure(state=tk.NORMAL)
        self.btn_rollback.configure(state=tk.NORMAL)
        if success:
            self.btn_update.configure(state=tk.DISABLED)
            self.btn_simulate.configure(state=tk.DISABLED)
            self._log("更新完成！", "info")
            self._show_update_result(self.current_changes, msg, backup_name)
            self._check_update_threaded()
        else:
            self.btn_update.configure(state=tk.NORMAL)
            self.btn_simulate.configure(state=tk.NORMAL)
            self._log(f"更新失败: {msg}", "error")
            AEDialog.show_error(self.root, "更新失败", msg)

    def _show_update_result(self, changes: dict, msg: str, backup_name: str = ""):
        """显示更新结果窗口"""
        dialog = tk.Toplevel(self.root)
        dialog.title("更新完成")
        dialog.geometry("700x680")
        dialog.minsize(600, 550)
        dialog.configure(bg=AETheme.BG_MAIN)
        dialog.transient(self.root)
        # 先隐藏
        dialog.withdraw()

        dialog.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - 650) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - 580) // 2
        dialog.geometry(f"+{x}+{y}")

        # 顶部状态
        top_frame = tk.Frame(dialog, bg=AETheme.BG_MAIN, padx=20, pady=18)
        top_frame.pack(fill=tk.X)

        tk.Label(
            top_frame, text="✅",
            font=("Segoe UI Emoji", 28),
            bg=AETheme.BG_MAIN
        ).pack(side=tk.LEFT, padx=(0, 14))

        info_frame = tk.Frame(top_frame, bg=AETheme.BG_MAIN)
        info_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)

        tk.Label(
            info_frame, text="更新完成！",
            bg=AETheme.BG_MAIN, fg=AETheme.NEON_GREEN,
            font=("Microsoft YaHei UI", 16, "bold")
        ).pack(anchor=tk.W)

        added = len(changes.get("added", []))
        modified = len(changes.get("modified", []))
        removed = len(changes.get("removed", []))
        total_size = changes.get("total_size", 0)

        tk.Label(
            info_frame,
            text=f"新增 {added} 个 · 修改 {modified} 个 · 删除 {removed} 个 · 共 {format_size(total_size)}",
            bg=AETheme.BG_MAIN, fg=AETheme.TEXT_SECONDARY,
            font=AETheme.FONT_BODY
        ).pack(anchor=tk.W, pady=(3, 0))

        # 文件列表区域
        list_frame = tk.Frame(dialog, bg=AETheme.BG_MAIN, padx=20)
        list_frame.pack(fill=tk.BOTH, expand=True)

        list_header = tk.Frame(list_frame, bg=AETheme.BG_MAIN)
        list_header.pack(fill=tk.X, pady=(0, 6))

        tk.Label(
            list_header, text="本次更新的文件（可多选后批量回退）：",
            bg=AETheme.BG_MAIN, fg=AETheme.TEXT_PRIMARY,
            font=AETheme.FONT_BOLD
        ).pack(side=tk.LEFT)

        # 全选/全不选按钮
        def select_all():
            for item in tree.get_children():
                tree.selection_add(item)

        def select_none():
            tree.selection_remove(tree.selection())

        btn_sel_all = tk.Label(
            list_header, text="全选",
            bg=AETheme.BG_MAIN, fg=AETheme.NEON_CYAN,
            font=("Microsoft YaHei UI", 9, "underline"),
            cursor="hand2"
        )
        btn_sel_all.pack(side=tk.RIGHT, padx=(10, 0))
        btn_sel_all.bind("<Button-1>", lambda e: select_all())

        tk.Label(
            list_header, text=" / ",
            bg=AETheme.BG_MAIN, fg=AETheme.TEXT_MUTED,
            font=("Microsoft YaHei UI", 9)
        ).pack(side=tk.RIGHT)

        btn_sel_none = tk.Label(
            list_header, text="全不选",
            bg=AETheme.BG_MAIN, fg=AETheme.TEXT_MUTED,
            font=("Microsoft YaHei UI", 9, "underline"),
            cursor="hand2"
        )
        btn_sel_none.pack(side=tk.RIGHT)
        btn_sel_none.bind("<Button-1>", lambda e: select_none())

        tk.Label(
            list_header, text="（Ctrl/Shift多选）",
            bg=AETheme.BG_MAIN, fg=AETheme.TEXT_MUTED,
            font=("Microsoft YaHei UI", 9)
        ).pack(side=tk.RIGHT, padx=(0, 5))

        # Treeview
        columns = ("action", "path")
        tree = ttk.Treeview(list_frame, columns=columns, show="headings",
                            height=16, selectmode="extended")
        tree.heading("action", text="操作")
        tree.heading("path", text="文件路径")
        tree.column("action", width=60, anchor=tk.CENTER, stretch=False)
        tree.column("path", width=520, anchor=tk.W)

        # 配置暗色主题
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview",
                        background=AETheme.BG_DEEP,
                        foreground=AETheme.TEXT_PRIMARY,
                        fieldbackground=AETheme.BG_DEEP,
                        bordercolor=AETheme.BORDER,
                        borderwidth=1)
        style.configure("Treeview.Heading",
                        background=AETheme.BG_PANEL_2,
                        foreground=AETheme.NEON_CYAN,
                        font=AETheme.FONT_BOLD)
        style.map("Treeview",
                  background=[("selected", AETheme.NEON_CYAN)],
                  foreground=[("selected", "#ffffff")])

        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)

        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        for f in changes.get("added", []):
            tree.insert("", tk.END, iid=f"added::{f}",
                        values=("新增", f), tags=("added",))
        for f in changes.get("modified", []):
            tree.insert("", tk.END, iid=f"modified::{f}",
                        values=("修改", f), tags=("modified",))
        for f in changes.get("removed", []):
            tree.insert("", tk.END, iid=f"removed::{f}",
                        values=("删除", f), tags=("removed",))

        tree.tag_configure("added", foreground=AETheme.NEON_GREEN)
        tree.tag_configure("modified", foreground=AETheme.NEON_CYAN)
        tree.tag_configure("removed", foreground=AETheme.NEON_RED)

        for item in tree.get_children():
            tree.selection_add(item)

        # 底部按钮
        btn_frame = tk.Frame(dialog, bg=AETheme.BG_MAIN, padx=20, pady=18)
        btn_frame.pack(fill=tk.X)

        status_var = tk.StringVar(value="")
        tk.Label(
            btn_frame, textvariable=status_var,
            bg=AETheme.BG_MAIN, fg=AETheme.TEXT_MUTED,
            font=AETheme.FONT_SMALL
        ).pack(side=tk.LEFT)

        def do_rollback_selected():
            selected = tree.selection()
            if not selected:
                AEDialog.show_warning(dialog, "提示", "请先选择要回退的文件！")
                return
            files = []
            for iid in selected:
                parts = iid.split("::", 1)
                if len(parts) == 2:
                    files.append(parts[1])

            if not AEDialog.ask_yesno(
                dialog,
                "确认回退",
                f"确定要回退选中的 {len(files)} 个文件吗？\n"
                f"这些文件将恢复到更新前的状态。"
            ):
                return
            if not backup_name:
                AEDialog.show_error(dialog, "错误", "找不到备份信息，无法回退！")
                return

            btn_rollback.configure(state=tk.DISABLED)
            btn_close.configure(state=tk.DISABLED)
            status_var.set("正在回退...")

            def worker():
                try:
                    ok, msg2, count = self.updater.rollback_files(backup_name, files)
                    self.root.after(0, lambda: on_done(ok, msg2, count))
                except Exception as e:
                    self.root.after(0, lambda: on_done(False, str(e), 0))

            def on_done(ok: bool, msg2: str, count: int):
                btn_rollback.configure(state=tk.NORMAL)
                btn_close.configure(state=tk.NORMAL)
                if ok:
                    status_var.set(f"已回退 {count} 个文件")
                    for iid in selected:
                        if tree.exists(iid):
                            tree.delete(iid)
                    self._log(f"部分回退完成: {count} 个文件", "info")
                    AEDialog.show_info(self.root, "回退完成", msg2, parent=dialog)
                    if not tree.get_children():
                        dialog.destroy()
                else:
                    status_var.set("回退失败")
                    AEDialog.show_error(self.root, "回退失败", msg2, parent=dialog)

            threading.Thread(target=worker, daemon=True).start()

        def do_rollback_all():
            if not AEDialog.ask_yesno(
                dialog,
                "确认回退",
                "确定要回退全部文件吗？\n"
                "将撤销本次更新的所有文件变动，恢复到更新前的状态。"
            ):
                return
            dialog.destroy()
            self._on_rollback_threaded()

        btn_rollback = NeonButton(
            btn_frame, text="↩ 回退选中的文件",
            command=do_rollback_selected,
            color=AETheme.NEON_YELLOW,
            width=150, height=34,
            bg=AETheme.BG_MAIN
        )
        btn_rollback.pack(side=tk.RIGHT)

        NeonButton(
            btn_frame, text="全部回退",
            command=do_rollback_all,
            color=AETheme.NEON_RED,
            width=100, height=34,
            bg=AETheme.BG_MAIN
        ).pack(side=tk.RIGHT, padx=(0, 8))

        btn_close = NeonButton(
            btn_frame, text="完成",
            command=dialog.destroy,
            color=AETheme.NEON_GREEN,
            width=90, height=34,
            bg=AETheme.BG_MAIN
        )
        btn_close.pack(side=tk.RIGHT, padx=(0, 8))

        # UI 构建完成后显示
        dialog.update_idletasks()
        dialog.deiconify()
        dialog.lift()
        dialog.focus_force()

    def _on_update_error(self, error: Exception):
        self.btn_check.configure(state=tk.NORMAL)
        self.btn_update.configure(state=tk.NORMAL)
        self.btn_rollback.configure(state=tk.NORMAL)
        self._log(f"更新出错: {error}", "error")
        AEDialog.show_error(self.root, "更新出错", str(error))

    def _show_rollback_dialog(self):
        """显示回退对话框"""
        old_dir = self.old_dir_var.get().strip()
        if not old_dir or not Path(old_dir).exists():
            AEDialog.show_warning(self.root, "提示", "请先选择旧整合包目录！")
            return

        updater = SimpleUpdater(old_dir, old_dir)
        backups = updater.get_backup_list()

        if not backups:
            AEDialog.show_info(self.root, "提示", "还没有任何备份记录。\n更新过一次后才能回退。")
            return

        dialog = tk.Toplevel(self.root)
        dialog.title("版本回退")
        dialog.geometry("480x400")
        dialog.configure(bg=AETheme.BG_MAIN)
        dialog.transient(self.root)
        # 先隐藏
        dialog.withdraw()

        tk.Label(
            dialog, text="选择要回退到的备份：",
            bg=AETheme.BG_MAIN, fg=AETheme.TEXT_PRIMARY,
            font=AETheme.FONT_BOLD
        ).pack(anchor=tk.W, padx=20, pady=(20, 10))

        list_frame = tk.Frame(dialog, bg=AETheme.BG_MAIN)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=20)

        lb = tk.Listbox(
            list_frame, font=("Consolas", 10),
            bg=AETheme.BG_DEEP, fg=AETheme.TEXT_PRIMARY,
            selectbackground=AETheme.NEON_CYAN,
            selectforeground="#ffffff",
            bd=0, highlightthickness=1,
            highlightbackground=AETheme.BORDER,
            relief=tk.FLAT
        )
        lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        sb = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=lb.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        lb.config(yscrollcommand=sb.set)

        for b in backups:
            lb.insert(tk.END, f"{b['time']}  (用户配置:{b['user_files']}  删除文件:{b['removed_files']})")

        btn_frame = tk.Frame(dialog, bg=AETheme.BG_MAIN)
        btn_frame.pack(fill=tk.X, padx=20, pady=20)

        def do_rollback():
            selection = lb.curselection()
            if not selection:
                AEDialog.show_warning(dialog, "提示", "请选择一个备份！")
                return
            backup_name = backups[selection[0]]["name"]
            if not AEDialog.ask_yesno(dialog, "确认回退", f"确定要回退到 {backups[selection[0]]['time']} 吗？"):
                return
            dialog.destroy()
            self._do_rollback_threaded(backup_name)

        NeonButton(
            btn_frame, text="↩  回退",
            command=do_rollback,
            color=AETheme.NEON_YELLOW,
            width=120, height=36,
            bg=AETheme.BG_MAIN
        ).pack(side=tk.LEFT)

        NeonButton(
            btn_frame, text="取消",
            command=dialog.destroy,
            color=AETheme.TEXT_SECONDARY,
            width=100, height=36,
            bg=AETheme.BG_MAIN
        ).pack(side=tk.RIGHT)

        # UI 构建完成后显示
        dialog.update_idletasks()
        # 居中
        x = self.root.winfo_x() + (self.root.winfo_width() - 480) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - 400) // 2
        dialog.geometry(f"+{x}+{y}")
        dialog.deiconify()
        dialog.grab_set()
        dialog.lift()
        dialog.focus_force()

    def _do_rollback_threaded(self, backup_name: str):
        old_dir = self.old_dir_var.get().strip()
        self.updater = SimpleUpdater(old_dir, old_dir)

        self.btn_check.configure(state=tk.DISABLED)
        self.btn_update.configure(state=tk.DISABLED)
        self.btn_rollback.configure(state=tk.DISABLED)
        self._log(f"开始回退到 {backup_name}...", "info")

        threading.Thread(target=self._do_rollback_worker, args=(backup_name,), daemon=True).start()

    def _do_rollback_worker(self, backup_name: str):
        try:
            success, msg = self.updater.rollback(backup_name)
            self.root.after(0, lambda: self._on_rollback_done(success, msg))
        except Exception as e:
            self.root.after(0, lambda: self._on_rollback_error(e))

    def _on_rollback_done(self, success: bool, msg: str):
        self.btn_check.configure(state=tk.NORMAL)
        self.btn_rollback.configure(state=tk.NORMAL)
        if success:
            self._log("回退完成！", "info")
            AEDialog.show_info(self.root, "回退完成", msg)
        else:
            self._log(f"回退失败: {msg}", "error")
            AEDialog.show_error(self.root, "回退失败", msg)

    def _on_rollback_error(self, error: Exception):
        self.btn_check.configure(state=tk.NORMAL)
        self.btn_update.configure(state=tk.NORMAL)
        self.btn_rollback.configure(state=tk.NORMAL)
        self._log(f"回退出错: {error}", "error")
        AEDialog.show_error(self.root, "回退出错", str(error))

    # ==================== 关于对话框 ====================

    def _show_about(self):
        """关于对话框 - AE风格"""
        dialog = tk.Toplevel(self.root)
        dialog.title("关于")
        dialog.geometry("420x420")
        dialog.resizable(False, False)
        dialog.configure(bg=AETheme.BG_MAIN)
        dialog.transient(self.root)
        # 先隐藏，构建完再显示
        dialog.withdraw()

        dialog.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - 420) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - 420) // 2
        dialog.geometry(f"+{x}+{y}")

        # 顶部装饰
        top_deco = tk.Frame(dialog, height=3, bg=AETheme.BG_MAIN)
        top_deco.pack(fill=tk.X)
        tk.Frame(top_deco, bg=AETheme.NEON_CYAN, height=2).pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Frame(top_deco, bg=AETheme.NEON_PURPLE, height=2).pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Frame(top_deco, bg=AETheme.NEON_PINK, height=2).pack(side=tk.LEFT, fill=tk.X, expand=True)

        content = tk.Frame(dialog, bg=AETheme.BG_MAIN, padx=30, pady=25)
        content.pack(fill=tk.BOTH, expand=True)

        # 头像（大）
        avatar_path = str(self.config_dir / "avatar.png")
        big_avatar = make_circle_avatar(avatar_path, size=96, glow_color=AETheme.NEON_CYAN)
        self._about_avatar = big_avatar  # 防止GC

        avatar_label = tk.Label(content, image=big_avatar, bg=AETheme.BG_MAIN)
        avatar_label.pack(pady=(5, 12))

        # 标题
        tk.Label(
            content, text="MC 整合包更新器",
            bg=AETheme.BG_MAIN, fg=AETheme.TEXT_PRIMARY,
            font=("Microsoft YaHei UI", 16, "bold")
        ).pack()

        tk.Label(
            content, text="v1.0  ·  AE Edition",
            bg=AETheme.BG_MAIN, fg=AETheme.NEON_CYAN,
            font=("Consolas", 10)
        ).pack(pady=(2, 15))

        # 分隔线
        sep = tk.Frame(content, height=1, bg=AETheme.BORDER)
        sep.pack(fill=tk.X, pady=5)
        tk.Frame(sep, bg=AETheme.NEON_PURPLE, width=60, height=1).place(x=0, y=0)

        # 信息区
        info_frame = tk.Frame(content, bg=AETheme.BG_MAIN)
        info_frame.pack(fill=tk.X, pady=18)

        info_items = [
            ("制作者", "凉寻 LonyaLx", AETheme.NEON_CYAN),
            ("联系QQ", "2287645520", AETheme.NEON_PURPLE),
            ("QQ交流群", "1104413649", AETheme.NEON_PINK),
            ("功能", "增量更新 · 配置保留 · 版本回退", AETheme.NEON_GREEN),
        ]

        for i, (label, value, color) in enumerate(info_items):
            row = tk.Frame(info_frame, bg=AETheme.BG_MAIN)
            row.pack(fill=tk.X, pady=5)

            tk.Label(
                row, text=label,
                bg=AETheme.BG_MAIN, fg=AETheme.TEXT_SECONDARY,
                font=AETheme.FONT_BODY, width=10, anchor="w"
            ).pack(side=tk.LEFT)

            tk.Label(
                row, text=value,
                bg=AETheme.BG_MAIN, fg=color,
                font=AETheme.FONT_BOLD
            ).pack(side=tk.LEFT)

        # 底部
        sep2 = tk.Frame(content, height=1, bg=AETheme.BORDER)
        sep2.pack(fill=tk.X, pady=(10, 15))

        tk.Label(
            content, text="© 2025 凉寻 LonyaLx  ·  All Rights Reserved",
            bg=AETheme.BG_MAIN, fg=AETheme.TEXT_MUTED,
            font=("Consolas", 8)
        ).pack()

        NeonButton(
            content, text="确 定",
            command=dialog.destroy,
            color=AETheme.NEON_CYAN,
            width=120, height=36,
            bg=AETheme.BG_MAIN
        ).pack(pady=(20, 0))

        # UI 构建完成后显示
        dialog.update_idletasks()
        dialog.deiconify()
        dialog.grab_set()
        dialog.lift()
        dialog.focus_force()

    # ==================== 辅助方法 ====================

    def _on_close(self):
        try:
            cleanup_zip_cache()
        except Exception:
            pass
        self.root.destroy()

    def _on_progress(self, current: int, total: int, message: str = ""):
        def update():
            if total > 0:
                pct = current / total * 100
                self.progress_var.set(pct)
                self._draw_progress(pct)
            self.progress_label_var.set(message or f"{current}/{total}")
        self.root.after(0, update)

    def _on_log(self, message: str, level: str = "info"):
        def append():
            self.log_text.configure(state=tk.NORMAL)
            import time
            timestamp = time.strftime("%H:%M:%S")
            prefix = {"info": "[INFO]", "warning": "[WARN]", "error": "[ERROR]"}.get(level, "[INFO]")
            color = {"info": "#b0b0c8", "warning": AETheme.NEON_YELLOW, "error": AETheme.NEON_RED}.get(level, "#b0b0c8")

            self.log_text.insert(tk.END, f"{timestamp} {prefix} {message}\n")
            line_start = self.log_text.index("end-2l linestart")
            tag_name = f"level_{level}"
            self.log_text.tag_config(tag_name, foreground=color)
            self.log_text.tag_add(tag_name, line_start, self.log_text.index("end-1l lineend"))

            self.log_text.see(tk.END)
            lines = int(self.log_text.index("end-1c").split(".")[0])
            if lines > 300:
                self.log_text.delete("1.0", f"{lines-200}.0")
            self.log_text.configure(state=tk.DISABLED)
        self.root.after(0, append)

    def _log(self, message: str, level: str = "info"):
        self._on_log(message, level)


def main():
    # 先设置 DPI 感知（必须在创建 Tk 之前）
    _setup_dpi_awareness()
    root = tk.Tk()
    app = SimpleUpdaterApp(root)
    root.protocol("WM_DELETE_WINDOW", app._on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
