"""Graphical window for the Jibo DFU Mod Toolkit (tkinter/ttk, standard library only).

The window is only a view. ``jibo_gui_core.GuiController`` runs the text
menu's own flows (``jibo_tui.execute_action``) and hands each question to
this window, so the actions, review texts, confirmation choices, and result
summaries are the text menu's. Every robot operation is a ``jibo_dfu``
function. This module imports tkinter at the top on purpose: the launcher
imports it lazily and reports a missing Tk with a pointer to the text menu.
"""

import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import re
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import tkinter.font as tkfont

import jibo_gui_core as core


class DisplayUnavailable(RuntimeError):
    """Tk is installed, but no graphical display could be opened."""


P = {
    "bg": "#eef1f5", "surface": "#ffffff", "border": "#d7dde5", "border_strong": "#c3ccd7",
    "text": "#17202b", "muted": "#566273", "faint": "#8893a1", "disabled": "#a0aab6",
    "hover": "#f5f8fc", "pressed": "#e3e9f1", "trough": "#dfe5ec",
    "accent": "#2563c9", "accent_active": "#1c4fa6", "accent_disabled": "#a8bde3",
    "accent_soft": "#e8effc", "accent_text": "#1d4ea3",
    "danger": "#c4302b", "danger_active": "#a2231f", "danger_disabled": "#e3a8a5",
    "danger_soft": "#fcebea", "danger_text": "#a2231f",
    "warning": "#b45309", "warning_soft": "#fdf3e1", "warning_text": "#8a3f06",
    "success": "#17804f", "success_soft": "#e4f5ec", "success_text": "#11663e",
    "neutral_soft": "#eceff3",
    "sidebar": "#141c26", "sidebar_card": "#1c2632", "sidebar_line": "#2f3b49",
    "sidebar_text": "#eef2f6", "sidebar_muted": "#97a5b6", "sidebar_hover": "#253140",
}
UI_FONTS = ("Segoe UI", "Inter", "Cantarell", "Ubuntu", "Noto Sans", "DejaVu Sans",
            "Liberation Sans", "Helvetica Neue", "Arial", "Helvetica")
MONO_FONTS = ("Cascadia Mono", "Consolas", "SF Mono", "Menlo", "DejaVu Sans Mono",
              "Noto Sans Mono", "Liberation Mono", "Ubuntu Mono", "Courier New")
_ASCII_BAR = re.compile(r"^\[[#\-]+\]\s*")
_CONFIRM_ARM_MS = 700
PHOENIX_CARD_TITLE = "Next: connect him to Phoenix"
PHOENIX_INTRO = "Optional. Phoenix setup happens at jibo.io, not in this toolkit."
PHOENIX_PREREQUISITE = (
    "He needs int-developer mode and your Wi-Fi first. "
    "Use Set mode and configure Wi-Fi here if he isn't set up yet."
)
PHOENIX_STEPS = (
    "1. Restart Jibo normally, out of recovery mode.",
    "2. Note the IP on his boot screen (or in your router's device list).",
    "3. At jibo.io, sign in and choose Robots \u2192 Add a Jibo. "
    "Run the one-line command it gives you; it asks for his IP.",
)
RCM_PHOTO_SIZE = 360  # assets/help/rcm-buttons.png is 360 x 360
_TONES = {
    "info": ("accent_soft", "accent_text"),
    "warning": ("warning_soft", "warning_text"),
    "danger": ("danger_soft", "danger_text"),
    "success": ("success_soft", "success_text"),
    "neutral": ("neutral_soft", "muted"),
}


def release_title():
    """Keep a release build identifiable when it has no command window."""
    folders = (Path(__file__).resolve().parent,
               Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)) / 'runtime')
    for folder in folders:
        try:
            revision = json.loads((folder / 'release-manifest.json').read_text())['revision']
            if isinstance(revision, str) and re.fullmatch(r'[0-9a-f]{40}', revision):
                return 'Jibo DFU Toolkit (build ' + revision[:7] + ')'
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return 'Jibo DFU Mod Toolkit'


def display_label(label):
    # The shared menu reports quick-file capability, not the transfer chosen
    # later (boot diagnostics and unsupported layouts use full-var instead).
    return str(label).removesuffix(" (quick edit)")


def rcm_help_photo(root, flows):
    """Load the owner's RCM photo only if its bytes match the pinned hash.

    Source checkouts, the .pyz and the frozen bundles all keep it next to the
    modules at assets/help/. A missing or altered file shows a text pointer.
    """
    path = Path(__file__).resolve().parent / flows.RCM_HELP_IMAGE
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if hashlib.sha256(data).hexdigest() != flows.RCM_HELP_IMAGE_SHA256:
        return None
    try:
        return tk.PhotoImage(master=root, data=base64.b64encode(data).decode("ascii"),
                             format="png")
    except tk.TclError:
        return None


def _family(root, candidates):
    available = {name.lower(): name for name in tkfont.families(root)}
    for name in candidates:
        if name.lower() in available:
            return available[name.lower()]
    return None


class Theme:
    """Fonts and ttk styles. Clam everywhere gives one look on all platforms."""

    def __init__(self, root):
        self.root = root
        aqua = root.tk.call("tk", "windowingsystem") == "aqua"
        default = tkfont.nametofont("TkDefaultFont")
        family = (None if aqua else _family(root, UI_FONTS)) or default.actual("family")
        mono = _family(root, MONO_FONTS) or tkfont.nametofont("TkFixedFont").actual("family")
        size = 13 if aqua else 10
        self.family, self.size = family, size
        self.base = tkfont.Font(root, family=family, size=size)
        self.bold = tkfont.Font(root, family=family, size=size, weight="bold")
        self.small = tkfont.Font(root, family=family, size=size - 1)
        self.small_bold = tkfont.Font(root, family=family, size=size - 1, weight="bold")
        self.caps = tkfont.Font(root, family=family, size=size - 2, weight="bold")
        self.heading = tkfont.Font(root, family=family, size=size + 2, weight="bold")
        self.title = tkfont.Font(root, family=family, size=size + 7, weight="bold")
        self.brand = tkfont.Font(root, family=family, size=size + 5, weight="bold")
        self.mono = tkfont.Font(root, family=mono, size=size - 1)
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont",
                     "TkCaptionFont", "TkSmallCaptionFont", "TkIconFont", "TkTooltipFont"):
            try:
                tkfont.nametofont(name).configure(family=family, size=size)
            except tk.TclError:
                pass
        self.style = ttk.Style(root)
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass
        self._configure()

    def _configure(self):
        style = self.style
        style.configure(".", background=P["bg"], foreground=P["text"], font=self.base,
                        bordercolor=P["border"], lightcolor=P["bg"], darkcolor=P["bg"],
                        troughcolor=P["trough"], focuscolor=P["accent"],
                        selectbackground=P["accent"], selectforeground="#ffffff",
                        insertcolor=P["text"])
        style.configure("TButton", background=P["surface"], foreground=P["text"],
                        bordercolor=P["border_strong"], lightcolor=P["surface"],
                        darkcolor=P["surface"], padding=(16, 7), focusthickness=1,
                        focuscolor=P["accent"], anchor="center")
        style.map("TButton",
                  background=[("disabled", P["bg"]), ("pressed", P["pressed"]),
                              ("active", P["hover"])],
                  foreground=[("disabled", P["disabled"])],
                  bordercolor=[("disabled", P["border"]), ("focus", P["accent"])],
                  lightcolor=[("pressed", P["pressed"]), ("active", P["hover"])],
                  darkcolor=[("pressed", P["pressed"]), ("active", P["hover"])])
        for name in ("accent", "danger"):
            color, active, disabled = P[name], P[name + "_active"], P[name + "_disabled"]
            style_name = name.capitalize() + ".TButton"
            style.configure(style_name, background=color, foreground="#ffffff",
                            bordercolor=color, lightcolor=color, darkcolor=color,
                            font=self.bold, focuscolor="#ffffff")
            style.map(style_name,
                      background=[("disabled", disabled), ("pressed", active), ("active", active)],
                      foreground=[("disabled", "#ffffff")],
                      bordercolor=[("disabled", disabled), ("focus", P["text"])],
                      lightcolor=[("disabled", disabled), ("pressed", active), ("active", active)],
                      darkcolor=[("disabled", disabled), ("pressed", active), ("active", active)])
        style.configure("Link.TButton", background=P["bg"], foreground=P["accent_text"],
                        bordercolor=P["bg"], lightcolor=P["bg"], darkcolor=P["bg"],
                        padding=(4, 2), font=self.small_bold)
        style.map("Link.TButton", background=[("active", P["accent_soft"])],
                  foreground=[("disabled", P["disabled"])],
                  bordercolor=[("focus", P["accent"])],
                  lightcolor=[("active", P["accent_soft"])],
                  darkcolor=[("active", P["accent_soft"])])
        style.configure("Sidebar.TButton", background=P["sidebar_card"],
                        foreground=P["sidebar_text"], bordercolor=P["sidebar_line"],
                        lightcolor=P["sidebar_card"], darkcolor=P["sidebar_card"],
                        padding=(14, 6), focuscolor=P["sidebar_text"])
        style.map("Sidebar.TButton",
                  background=[("disabled", P["sidebar"]), ("pressed", P["sidebar_line"]),
                              ("active", P["sidebar_hover"])],
                  foreground=[("disabled", P["sidebar_muted"])],
                  lightcolor=[("active", P["sidebar_hover"])],
                  darkcolor=[("active", P["sidebar_hover"])])
        style.configure("SidebarLink.TButton", background=P["sidebar"],
                        foreground=P["sidebar_muted"], bordercolor=P["sidebar"],
                        lightcolor=P["sidebar"], darkcolor=P["sidebar"],
                        padding=(0, 3), font=self.small)
        style.map("SidebarLink.TButton",
                  background=[("active", P["sidebar_hover"])],
                  foreground=[("disabled", P["faint"]), ("active", P["sidebar_text"])],
                  bordercolor=[("focus", P["sidebar_muted"])],
                  lightcolor=[("active", P["sidebar_hover"])],
                  darkcolor=[("active", P["sidebar_hover"])])
        style.configure("TEntry", fieldbackground=P["surface"], foreground=P["text"],
                        bordercolor=P["border_strong"], lightcolor=P["surface"],
                        darkcolor=P["surface"], padding=(10, 7), insertcolor=P["text"])
        style.map("TEntry", bordercolor=[("focus", P["accent"])],
                  lightcolor=[("focus", P["accent"])], darkcolor=[("focus", P["accent"])])
        style.configure("Horizontal.TProgressbar", background=P["accent"],
                        troughcolor=P["trough"], bordercolor=P["trough"],
                        lightcolor=P["accent"], darkcolor=P["accent"], thickness=10)
        style.configure("Vertical.TScrollbar", background=P["border"], troughcolor=P["bg"],
                        bordercolor=P["bg"], arrowcolor=P["muted"], lightcolor=P["border"],
                        darkcolor=P["border"], gripcount=0, arrowsize=12)
        style.map("Vertical.TScrollbar", background=[("active", P["border_strong"])])


def _wrapping(label, minimum=120, margin=0):
    """Rewrap a label to its current width (keeps paragraphs readable on resize)."""
    def update(event):
        width = max(minimum, event.width - margin)
        if int(str(label.cget("wraplength") or 0)) != width:
            label.configure(wraplength=width)
    label.bind("<Configure>", update, add="+")
    return label


class TextBox(tk.Frame):
    """Read-only text with a scrollbar that appears only when needed."""

    def __init__(self, parent, theme, font=None, height=8, wrap="word", bg=None):
        bg = bg or P["surface"]
        super().__init__(parent, bg=bg, highlightthickness=1,
                         highlightbackground=P["border"], highlightcolor=P["border"])
        self.text = tk.Text(self, font=font or theme.base, bg=bg, fg=P["text"], wrap=wrap,
                            height=height, relief="flat", borderwidth=0, highlightthickness=0,
                            padx=16, pady=12, cursor="arrow", insertwidth=0, spacing1=1,
                            spacing3=3, selectbackground=P["accent_soft"],
                            selectforeground=P["text"], inactiveselectbackground=P["accent_soft"])
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=self._scrolled, state="disabled")
        self.text.grid(row=0, column=0, sticky="nsew")
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

    def _scrolled(self, first, last):
        if float(first) <= 0.0 and float(last) >= 1.0:
            self.bar.grid_remove()
        else:
            self.bar.grid(row=0, column=1, sticky="ns")
        self.bar.set(first, last)

    def set(self, content, see_end=False):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", content)
        self.text.configure(state="disabled")
        if see_end:
            self.text.see("end")

    def get(self):
        return self.text.get("1.0", "end-1c")


class ScrollArea(tk.Frame):
    """A vertically scrollable column for option lists and action lists."""

    def __init__(self, parent, bg):
        super().__init__(parent, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, borderwidth=0)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.inner.bind("<Configure>", lambda _event: self._sync())
        self.canvas.bind("<Configure>", self._resize)

    def _resize(self, event):
        self.canvas.itemconfigure(self._window, width=event.width)
        self._sync()

    def _sync(self):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        needed = self.inner.winfo_reqheight()
        visible = self.canvas.winfo_height()
        if visible > 1 and needed > visible:
            if not self.bar.winfo_ismapped():
                self.bar.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        elif self.bar.winfo_ismapped() and needed < visible - 24:
            self.bar.grid_remove()
            self.canvas.yview_moveto(0)

    def scroll(self, steps):
        if self.inner.winfo_reqheight() > self.canvas.winfo_height():
            self.canvas.yview_scroll(steps, "units")

    def show(self, widget):
        """Scroll so a child row is visible (keyboard navigation)."""
        self.update_idletasks()
        total = max(1, self.inner.winfo_reqheight())
        top, bottom = widget.winfo_y(), widget.winfo_y() + widget.winfo_height()
        first, last = self.canvas.yview()
        if top < first * total:
            self.canvas.yview_moveto(top / total)
        elif bottom > last * total:
            self.canvas.yview_moveto(max(0.0, (bottom - self.canvas.winfo_height()) / total))


class ActionRow(tk.Frame):
    """One main-menu action: the text menu's label, hint or disabled reason."""

    def __init__(self, parent, theme, item, hint, on_open, badge=""):
        self.item = item
        self.on_open = on_open
        self._hover = False
        bg = P["surface"]
        super().__init__(parent, bg=bg, highlightthickness=1, highlightbackground=P["border"],
                         highlightcolor=P["accent"], takefocus=1 if item.enabled else 0,
                         cursor="hand2" if item.enabled else "arrow")
        self.body = tk.Frame(self, bg=bg)
        self.body.pack(side="left", fill="both", expand=True, padx=(18, 10), pady=(11, 12))
        self.title = tk.Label(self.body, text=display_label(item.label), font=theme.bold, bg=bg, anchor="w",
                              justify="left",
                              fg=P["text"] if item.enabled else P["disabled"])
        self.title.pack(fill="x")
        self.hint = tk.Label(self.body, text=hint, font=theme.small, bg=bg, anchor="w",
                             justify="left", fg=P["muted"] if item.enabled else P["faint"])
        self.hint.pack(fill="x", pady=(2, 0))
        side_text = "Open \u203a" if item.enabled else "Unavailable"
        self.side = tk.Label(self, text=side_text, font=theme.small_bold, bg=bg,
                             fg=P["accent_text"] if item.enabled else P["faint"])
        self.side.pack(side="right", padx=(6, 18))
        self.badge = None
        if badge and item.enabled:
            self.badge = tk.Label(self, text=badge, font=theme.caps, bg=P["accent"],
                                  fg="#ffffff", padx=8, pady=2)
            self.badge.pack(side="right")
        self._widgets = [self, self.body, self.title, self.hint, self.side]
        for widget in self._widgets + ([self.badge] if self.badge else []):
            widget.bind("<Button-1>", self.activate)
            widget.bind("<Enter>", self._enter)
            widget.bind("<Leave>", self._leave)
        self.bind("<Return>", self.activate)
        self.bind("<KP_Enter>", self.activate)
        self.bind("<space>", self.activate)
        self.bind("<FocusIn>", lambda _event: self._paint())
        self.bind("<FocusOut>", lambda _event: self._paint())
        self.bind("<Configure>", self._rewrap)

    def _rewrap(self, event):
        self.hint.configure(wraplength=max(200, event.width - 190))

    def _enter(self, _event):
        self._hover = True
        self._paint()

    def _leave(self, event):
        inside = self.winfo_containing(event.x_root, event.y_root)
        while inside is not None and inside is not self:
            inside = getattr(inside, "master", None)
        if inside is None:
            self._hover = False
            self._paint()

    def _paint(self):
        active = self.item.enabled and (self._hover or self.focus_get() is self)
        bg = P["hover"] if active else P["surface"]
        self.configure(highlightbackground=P["accent"] if active else P["border"])
        for widget in self._widgets:
            widget.configure(bg=bg)

    def activate(self, _event=None):
        self.on_open(self.item.key)
        return "break"


class OptionRow(tk.Frame):
    """A selectable card with a drawn radio or check indicator."""

    def __init__(self, parent, theme, item, selected, on_toggle, on_activate=None,
                 multi=False):
        self.item = item
        self.multi = multi
        self.on_toggle = on_toggle
        self.on_activate = on_activate
        self.selected = selected
        super().__init__(parent, highlightthickness=1, takefocus=0,
                         cursor="hand2" if item.enabled else "arrow")
        self.indicator = tk.Canvas(self, width=20, height=20, highlightthickness=0,
                                   borderwidth=0)
        self.indicator.pack(side="left", padx=(16, 12), pady=12, anchor="n")
        body = tk.Frame(self)
        body.pack(side="left", fill="both", expand=True, pady=(11, 11), padx=(0, 16))
        self.label = tk.Label(body, text=item.label, anchor="w", justify="left",
                              font=theme.bold if getattr(item, "tone", "") == "recommended"
                              else theme.base)
        self.label.pack(fill="x")
        self.reason = None
        if item.reason:
            self.reason = tk.Label(body, text=item.reason, font=theme.small, anchor="w",
                                   justify="left")
            self.reason.pack(fill="x", pady=(2, 0))
            _wrapping(self.reason)
        _wrapping(self.label)
        self._parts = [self, body, self.label, self.indicator] + (
            [self.reason] if self.reason else [])
        for widget in self._parts:
            widget.bind("<Button-1>", self._click)
            if on_activate is not None:
                widget.bind("<Double-Button-1>", self._double)
        self.paint()

    def _click(self, _event=None):
        if self.item.enabled:
            self.on_toggle(self.item.key)
        return "break"

    def _double(self, _event=None):
        if self.item.enabled and self.on_activate is not None:
            self.on_toggle(self.item.key)
            self.on_activate()
        return "break"

    def set_selected(self, value):
        self.selected = bool(value)
        self.paint()

    def paint(self):
        enabled, chosen = self.item.enabled, self.selected
        danger = getattr(self.item, "tone", "") == "danger"
        bg = P["danger_soft"] if danger else (P["accent_soft"] if chosen else P["surface"])
        border = P["danger"] if danger else (P["accent"] if chosen else P["border"])
        self.configure(bg=bg, highlightbackground=border, highlightcolor=border)
        for widget in self._parts[1:]:
            widget.configure(bg=bg)
        self.label.configure(fg=P["danger_text"] if danger else P["text"] if enabled else P["disabled"])
        if self.reason is not None:
            self.reason.configure(fg=P["danger_text"] if danger else P["muted"])
        canvas = self.indicator
        canvas.delete("all")
        ring = P["accent"] if chosen else (P["border_strong"] if enabled else P["border"])
        if self.multi:
            fill = P["accent"] if chosen else P["surface"]
            canvas.create_rectangle(2, 2, 18, 18, outline=ring, width=2, fill=fill)
            if chosen:
                canvas.create_line(5, 10, 8.5, 13.5, 15, 6, fill="#ffffff", width=2.2,
                                   capstyle="round", joinstyle="round")
        else:
            canvas.create_oval(2, 2, 18, 18, outline=ring, width=2, fill=P["surface"])
            if chosen:
                canvas.create_oval(6, 6, 14, 14, outline=P["accent"], fill=P["accent"])


class Stepper(tk.Canvas):
    """The text menu's USB flow line, drawn as numbered steps."""

    ROW = 40

    def __init__(self, parent, theme, steps):
        self.theme = theme
        self.steps = tuple(steps)
        super().__init__(parent, bg=P["sidebar"], highlightthickness=0, borderwidth=0,
                         width=240, height=self.ROW * len(self.steps) - 4)

    def render(self, current, warning=False, checked=True):
        self.delete("all")
        x = 14
        for index, name in enumerate(self.steps):
            y = 14 + index * self.ROW
            done = checked and index < current
            now = checked and index == current
            if index < len(self.steps) - 1:
                self.create_line(x, y + 14, x, y + self.ROW - 14, width=2,
                                 fill=P["accent"] if done else P["sidebar_line"])
            if done:
                self.create_oval(x - 12, y - 12, x + 12, y + 12, fill=P["accent"],
                                 outline=P["accent"])
                self.create_line(x - 5, y, x - 1.5, y + 4, x + 5.5, y - 4, fill="#ffffff",
                                 width=2.2, capstyle="round", joinstyle="round")
            elif now:
                color = P["warning"] if warning else P["accent"]
                self.create_oval(x - 12, y - 12, x + 12, y + 12, fill=color, outline=color)
                self.create_text(x, y, text=str(index + 1), fill="#ffffff",
                                 font=self.theme.small_bold)
            else:
                self.create_oval(x - 12, y - 12, x + 12, y + 12, fill=P["sidebar"],
                                 outline=P["sidebar_line"], width=2)
                self.create_text(x, y, text=str(index + 1), fill=P["sidebar_muted"],
                                 font=self.theme.small_bold)
            self.create_text(x + 26, y, text=name, anchor="w",
                             font=self.theme.bold if now else self.theme.base,
                             fill=P["sidebar_text"] if (done or now) else P["sidebar_muted"])


class ToolkitWindow:
    """Main window: USB status sidebar plus one page at a time."""

    POLL_MS = 60
    REFRESH_MS = 1500

    def __init__(self, root, controller):
        self.root = root
        self.controller = controller
        self.theme = Theme(root)
        self.closed = False
        self.page = None
        self.page_name = ""
        self.prompt = None
        self.controls = {}
        self.action_rows = {}
        self._rendered_items = None
        self._scroll_areas = []
        self._confirm_job = None
        root.title(release_title())
        root.configure(bg=P["bg"])
        root.geometry("1080x720")
        root.minsize(920, 640)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._build_layout()
        root.bind_all("<MouseWheel>", self._wheel, add="+")
        root.bind_all("<Button-4>", self._wheel, add="+")
        root.bind_all("<Button-5>", self._wheel, add="+")
        root.bind("<F5>", lambda _event: self.refresh())
        root.bind("<Control-r>", lambda _event: self.refresh())
        self.show_home()
        self.render_state()
        controller.request_refresh()
        self._poll_job = root.after(self.POLL_MS, self._poll)
        self._refresh_job = root.after(self.REFRESH_MS, self._auto_refresh)

    # -- layout ------------------------------------------------------------
    def _build_layout(self):
        theme = self.theme
        self.root.grid_columnconfigure(1, weight=1)
        self.root.grid_rowconfigure(0, weight=1)
        side = tk.Frame(self.root, bg=P["sidebar"], width=292)
        side.grid(row=0, column=0, sticky="nsw")
        side.grid_propagate(False)
        side.pack_propagate(False)
        inner = tk.Frame(side, bg=P["sidebar"])
        inner.pack(fill="both", expand=True, padx=24, pady=(26, 20))
        tk.Label(inner, text="Jibo DFU", font=theme.brand, bg=P["sidebar"],
                 fg=P["sidebar_text"], anchor="w").pack(fill="x")
        tk.Label(inner, text="Mod Toolkit", font=theme.base, bg=P["sidebar"],
                 fg=P["sidebar_muted"], anchor="w").pack(fill="x")
        tk.Frame(inner, bg=P["sidebar_line"], height=1).pack(fill="x", pady=(20, 18))
        tk.Label(inner, text="USB FLOW", font=theme.caps, bg=P["sidebar"],
                 fg=P["sidebar_muted"], anchor="w").pack(fill="x", pady=(0, 10))
        self.stepper = Stepper(inner, theme, core.FLOW_STEPS)
        self.stepper.pack(anchor="w")
        card = tk.Frame(inner, bg=P["sidebar_card"])
        card.pack(fill="x", pady=(18, 0))
        self.status_step = tk.Label(card, text="", font=theme.caps, bg=P["sidebar_card"],
                                    fg=P["sidebar_muted"], anchor="w")
        self.status_step.pack(fill="x", padx=16, pady=(14, 2))
        self.status_title = tk.Label(card, text="", font=theme.bold, bg=P["sidebar_card"],
                                     fg=P["sidebar_text"], anchor="w", justify="left",
                                     wraplength=210)
        self.status_title.pack(fill="x", padx=16)
        self.status_detail = tk.Label(card, text="", font=theme.small, bg=P["sidebar_card"],
                                      fg=P["sidebar_muted"], anchor="w", justify="left",
                                      wraplength=210)
        self.status_detail.pack(fill="x", padx=16, pady=(6, 0))
        self.status_port = tk.Label(card, text="", font=theme.small_bold,
                                    bg=P["sidebar_card"], fg=P["sidebar_text"], anchor="w")
        self.status_port.pack(fill="x", padx=16, pady=(8, 0))
        self.refresh_button = ttk.Button(card, text="Refresh USB", style="Sidebar.TButton",
                                         command=self.refresh)
        self.refresh_button.pack(anchor="w", padx=16, pady=(12, 16))
        footer = tk.Frame(inner, bg=P["sidebar"])
        footer.pack(side="bottom", fill="x")
        backups = getattr(self.controller.api, "BACKUP_ROOT", None)
        if backups:
            tk.Label(footer, text="BACKUPS", font=theme.caps, bg=P["sidebar"],
                     fg=P["sidebar_muted"], anchor="w").pack(fill="x")
            tk.Label(footer, text=str(backups), font=theme.small, bg=P["sidebar"],
                     fg=P["sidebar_text"], anchor="w", justify="left",
                     wraplength=240).pack(fill="x", pady=(2, 10))
        self.phoenix_entry = ttk.Button(footer, text="Connect to Phoenix (jibo.io)",
                                       style="SidebarLink.TButton", command=self.show_phoenix)
        self.phoenix_entry.pack(anchor="w")

        main = tk.Frame(self.root, bg=P["bg"])
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(0, weight=1)
        self.content = tk.Frame(main, bg=P["bg"])
        self.content.grid(row=0, column=0, sticky="nsew", padx=36, pady=(28, 12))
        bar = tk.Frame(main, bg=P["bg"])
        bar.grid(row=1, column=0, sticky="ew", padx=36, pady=(0, 14))
        self.note = tk.Label(bar, text="", font=theme.small, bg=P["bg"], fg=P["muted"],
                             anchor="w", justify="left")
        self.note.pack(fill="x")
        _wrapping(self.note)
        self.home = self._build_home()
        self.progress = self._build_progress()

    def _header(self, parent, title, subtitle="", kicker=""):
        if kicker:
            tk.Label(parent, text=kicker.upper(), font=self.theme.caps, bg=P["bg"],
                     fg=P["accent_text"], anchor="w").pack(fill="x", pady=(0, 6))
        title_label = tk.Label(parent, text=title, font=self.theme.title, bg=P["bg"],
                               fg=P["text"], anchor="w", justify="left")
        title_label.pack(fill="x")
        _wrapping(title_label)
        if subtitle:
            detail = tk.Label(parent, text=subtitle, font=self.theme.base, bg=P["bg"],
                              fg=P["muted"], anchor="w", justify="left")
            detail.pack(fill="x", pady=(8, 0))
            _wrapping(detail)
        return title_label

    def _callout(self, parent, text, tone="info"):
        background, foreground = (P[name] for name in _TONES[tone])
        box = tk.Frame(parent, bg=background)
        stripe = tk.Frame(box, bg=foreground, width=4)
        stripe.pack(side="left", fill="y")
        label = tk.Label(box, text=text, font=self.theme.base, bg=background, fg=foreground,
                         anchor="w", justify="left")
        label.pack(side="left", fill="both", expand=True, padx=14, pady=10)
        _wrapping(label)
        return box

    def _button_bar(self, parent):
        bar = tk.Frame(parent, bg=P["bg"])
        bar.pack(side="bottom", fill="x", pady=(18, 0))
        return bar

    # -- pages -------------------------------------------------------------
    def _build_home(self):
        page = tk.Frame(self.content, bg=P["bg"])
        self._header(page, "Actions",
                     "Actions unlock as the robot moves from RCM/APX to DFU. The list follows "
                     "USB changes automatically; nothing is written without a review step.")
        self.actions = ScrollArea(page, P["bg"])
        self.actions.pack(fill="both", expand=True, pady=(20, 0))
        self._scroll_areas.append(self.actions)
        # Shown until the robot reaches DFU; packed above the action list.
        self.rcm_help_link = ttk.Button(page, text=self.controller.flows.RCM_HELP_LINK,
                                        style="Link.TButton", command=self.show_rcm_help)
        self._rcm_help_shown = False
        return page

    def _build_progress(self):
        theme = self.theme
        page = tk.Frame(self.content, bg=P["bg"])
        self.progress_kicker = tk.Label(page, text="", font=theme.caps, bg=P["bg"],
                                        fg=P["accent_text"], anchor="w")
        self.progress_kicker.pack(fill="x", pady=(0, 6))
        self.progress_title = tk.Label(page, text="Working", font=theme.title, bg=P["bg"],
                                       fg=P["text"], anchor="w")
        self.progress_title.pack(fill="x")
        self._callout(page, "Keep the robot connected by USB and leave this window open "
                      "until the operation finishes.", "info").pack(fill="x", pady=(14, 0))
        card = tk.Frame(page, bg=P["surface"], highlightthickness=1,
                        highlightbackground=P["border"])
        card.pack(fill="x", pady=(16, 0))
        self.progress_step = tk.Label(card, text="Starting...", font=theme.bold,
                                      bg=P["surface"], fg=P["text"], anchor="w", justify="left")
        self.progress_step.pack(fill="x", padx=20, pady=(16, 10))
        _wrapping(self.progress_step)
        self.progress_bar = ttk.Progressbar(card, mode="indeterminate", maximum=100)
        self.progress_bar.pack(fill="x", padx=20)
        self.progress_status = tk.Label(card, text="", font=theme.mono, bg=P["surface"],
                                        fg=P["muted"], anchor="w")
        self.progress_status.pack(fill="x", padx=20, pady=(8, 16))
        # Shown only while a Phoenix image list check or download runs.
        self.progress_cancel = ttk.Button(card, text="Cancel download", command=self.cancel_download)
        self._cancel_shown = False
        tk.Label(page, text="OPERATION LOG", font=theme.caps, bg=P["bg"], fg=P["muted"],
                 anchor="w").pack(fill="x", pady=(18, 6))
        self.progress_log = TextBox(page, theme, font=theme.mono, height=8, wrap="word")
        self.progress_log.pack(fill="both", expand=True)
        self._progress_running = False
        return page

    def _show(self, name, frame):
        if self._confirm_job is not None:
            self.root.after_cancel(self._confirm_job)
            self._confirm_job = None
        for persistent in (self.home, self.progress):
            if persistent is not frame:
                persistent.pack_forget()
        if self.page is not None and self.page not in (self.home, self.progress) \
                and self.page is not frame:
            self.page.destroy()
            self._scroll_areas = [area for area in self._scroll_areas if area.winfo_exists()]
        self.page = frame
        self.page_name = name
        frame.pack(fill="both", expand=True)

    def _new_page(self, name):
        frame = tk.Frame(self.content, bg=P["bg"])
        self.controls = {}
        self._show(name, frame)
        return frame

    def show_home(self):
        self.prompt = None
        self.controls = {}
        self._stop_progress()
        self._show("home", self.home)
        snap = self.controller.snapshot()
        self._render_rcm_help(snap)
        self._render_actions(snap)

    def show_progress(self):
        self.prompt = None
        self.controls = {}
        snap = self.controller.snapshot()
        self.progress_kicker.configure(text="RUNNING")
        self.progress_title.configure(text=display_label(snap.active_label) or "Working")
        self._show("progress", self.progress)
        self.render_output()

    def show_phoenix(self):
        """User-requested, local guidance; never interrupt a running robot flow."""
        if self.controller.busy:
            return
        self.prompt = None
        self._stop_progress()
        page = self._new_page("phoenix")
        self._header(page, "Bring your Jibo online with Phoenix")
        buttons = self._button_bar(page)
        back = ttk.Button(buttons, text="Back to actions", command=self.show_home)
        back.pack(side="right")
        area = ScrollArea(page, P["bg"])
        area.pack(fill="both", expand=True)
        self._scroll_areas.append(area)
        self._build_phoenix_card(area.inner, self.show_home, show_prerequisite=True)
        self.controls["primary"] = back
        back.focus_set()

    def show_rcm_help(self):
        """Local how-to with the owner's photo; never interrupts a running robot flow."""
        if self.controller.busy:
            return
        flows = self.controller.flows
        self.prompt = None
        self._stop_progress()
        page = self._new_page("rcm-help")
        self._header(page, flows.RCM_HELP_TITLE, flows.RCM_HELP_INTRO, kicker="Help")
        buttons = self._button_bar(page)
        back = ttk.Button(buttons, text="Back to actions", style="Accent.TButton",
                          command=self.show_home)
        back.pack(side="right")
        area = ScrollArea(page, P["bg"])
        area.pack(fill="both", expand=True, pady=(16, 0))
        self._scroll_areas.append(area)
        card = tk.Frame(area.inner, bg=P["surface"], highlightthickness=1,
                        highlightbackground=P["border"])
        card.pack(fill="x")
        figure = tk.Frame(card, bg=P["surface"])
        steps_box = tk.Frame(card, bg=P["surface"])
        photo = rcm_help_photo(self.root, flows)
        if photo is not None:
            picture = tk.Label(figure, image=photo, bg=P["surface"], borderwidth=0)
            picture.image = photo  # Tk drops an unreferenced PhotoImage.
            picture.pack(anchor="w")
        else:
            picture = tk.Label(figure, text="The photo of the buttons is missing from this copy "
                               "of the toolkit. See the README's \"" + flows.RCM_HELP_TITLE +
                               "\" section.", font=self.theme.small, bg=P["surface"],
                               fg=P["warning_text"], anchor="w", justify="left", wraplength=300)
            picture.pack(anchor="w")
        caption = tk.Label(figure, text=flows.RCM_HELP_CAPTION, font=self.theme.small,
                           bg=P["surface"], fg=P["muted"], anchor="w", justify="left",
                           wraplength=RCM_PHOTO_SIZE)
        caption.pack(anchor="w", pady=(8, 0))
        step_labels = []
        for step in flows.RCM_HELP_STEPS:
            label = tk.Label(steps_box, text=step, font=self.theme.base, bg=P["surface"],
                             fg=P["text"], anchor="w", justify="left")
            label.pack(fill="x", pady=(0, 8))
            step_labels.append(label)
        layout = {"wide": None}

        def arrange(event=None):
            width = event.width if event is not None else card.winfo_width()
            wide = width >= RCM_PHOTO_SIZE + 300
            if wide != layout["wide"]:
                layout["wide"] = wide
                figure.grid_forget()
                steps_box.grid_forget()
                if wide:
                    figure.grid(row=0, column=0, sticky="nw", padx=(18, 0), pady=18)
                    steps_box.grid(row=0, column=1, sticky="new", padx=18, pady=18)
                    card.grid_columnconfigure(0, weight=0)
                    card.grid_columnconfigure(1, weight=1)
                else:
                    figure.grid(row=0, column=0, columnspan=2, sticky="nw", padx=18, pady=(18, 0))
                    steps_box.grid(row=1, column=0, columnspan=2, sticky="new", padx=18, pady=18)
                    card.grid_columnconfigure(0, weight=1)
                    card.grid_columnconfigure(1, weight=0)
            text_width = max(180, width - 36 - ((RCM_PHOTO_SIZE + 18) if wide else 0) - 4)
            for label in step_labels:
                label.configure(wraplength=text_width)

        card.bind("<Configure>", arrange, add="+")
        arrange()
        self.controls.update(primary=back, rcm_card=card, rcm_photo=picture,
                             rcm_caption=caption, rcm_steps=step_labels, rcm_layout=layout)
        back.focus_set()
        back.bind("<Return>", lambda _event: self.show_home())
        back.bind("<Escape>", lambda _event: self.show_home())

    def _render_rcm_help(self, snap):
        """Offer the RCM how-to until the robot's recovery loader is ready in DFU."""
        wanted = snap.readiness.state != "dfu-ready" or not snap.checked
        if wanted and not self._rcm_help_shown:
            self.rcm_help_link.pack(anchor="w", before=self.actions, pady=(12, 0))
            self.actions.pack_configure(pady=(10, 0))
        elif not wanted and self._rcm_help_shown:
            self.rcm_help_link.pack_forget()
            self.actions.pack_configure(pady=(20, 0))
        self._rcm_help_shown = wanted
        self.rcm_help_link.state(["disabled"] if snap.busy else ["!disabled"])

    # -- main actions ------------------------------------------------------
    def _render_actions(self, snap):
        signature = tuple((item.key, item.label, item.enabled, item.reason)
                          for item in snap.items)
        if signature == self._rendered_items:
            return
        self._rendered_items = signature
        focused = self.root.focus_get()
        focused_key = getattr(focused, "item", None)
        focused_key = getattr(focused_key, "key", None)
        for child in self.actions.inner.winfo_children():
            child.destroy()
        self.action_rows = {}
        for item in snap.items:
            badge = ("NEXT STEP" if item.key == "enter-dfu-shofel" and item.enabled else "")
            row = ActionRow(self.actions.inner, self.theme, item, self.controller.hint(item),
                            self.open_action, badge)
            row.pack(fill="x", pady=(0, 10))
            self.action_rows[item.key] = row
        if focused_key in self.action_rows and self.action_rows[focused_key].item.enabled:
            self.action_rows[focused_key].focus_set()

    def open_action(self, key):
        if self.controller.start_action(key):
            self.show_progress()
        else:
            self.render_state()

    def refresh(self):
        if not self.controller.busy:
            self.controller.request_refresh(manual=True)

    # -- state rendering ---------------------------------------------------
    def render_state(self):
        snap = self.controller.snapshot()
        self.stepper.render(snap.step_index, snap.warning, snap.checked)
        self.status_step.configure(text="USB READINESS")
        self.status_title.configure(text=snap.headline)
        self.status_detail.configure(text=snap.detail)
        self.status_port.configure(text=("USB port " + snap.port) if snap.port else "")
        self.refresh_button.state(["disabled"] if snap.busy else ["!disabled"])
        self.phoenix_entry.state(["disabled"] if snap.busy else ["!disabled"])
        self.note.configure(text=snap.note)
        if self.page_name == "home":
            self._render_rcm_help(snap)
            self._render_actions(snap)

    def render_output(self):
        state = self.controller.output_state()
        status = _ASCII_BAR.sub("", state.status)
        self.progress_step.configure(text=state.step or "Starting...")
        self.progress_status.configure(text=status)
        if state.percent is None:
            if not self._progress_running:
                self.progress_bar.configure(mode="indeterminate")
                self.progress_bar.start(14)
                self._progress_running = True
        else:
            if self._progress_running:
                self.progress_bar.stop()
                self._progress_running = False
            self.progress_bar.configure(mode="determinate", value=state.percent)
        self.progress_log.set("\n".join(state.lines), see_end=True)
        self._render_download_controls()

    def _render_download_controls(self):
        active = self.controller.work_active
        self.progress_cancel.configure(text=self.controller.work_cancel_label)
        if active and not self._cancel_shown:
            self.progress_cancel.state(["!disabled"])
            self.progress_cancel.pack(anchor="e", padx=20, pady=(0, 16))
            self._cancel_shown = True
            self.root.bind("<Escape>", lambda _event: self.cancel_download())
        elif not active and self._cancel_shown:
            self.progress_cancel.pack_forget()
            self._cancel_shown = False
            if self.prompt is None:
                self.root.unbind("<Escape>")

    def cancel_download(self):
        if self.controller.cancel_download():
            self.progress_cancel.state(["disabled"])
            self.progress_status.configure(text=self.controller.work_cancel_message)

    def _stop_progress(self):
        if getattr(self, "_progress_running", False):
            self.progress_bar.stop()
            self._progress_running = False

    # -- prompts -----------------------------------------------------------
    def show_prompt(self, prompt):
        if prompt.answered:
            return
        self._stop_progress()
        page = self._new_page("prompt-" + prompt.kind)
        self.prompt = prompt
        builders = {"select": self._build_select, "confirm": self._build_confirm,
                    "multi": self._build_multi, "text": self._build_text,
                    "message": self._build_message,
                    "calibration-consent": self._build_calibration_consent}
        builders.get(prompt.kind, self._build_message)(page, prompt)
        page.bind("<Escape>", lambda _event: self.answer(None))
        self.root.bind("<Escape>", lambda _event: self.answer(None))

    def answer(self, value):
        prompt, self.prompt = self.prompt, None
        self.root.unbind("<Escape>")
        if prompt is None:
            return
        prompt.resolve(value)
        if self.controller.busy:
            self.show_progress()
        else:
            self.show_home()

    def _kicker(self):
        return display_label(self.controller.snapshot().active_label)

    def _build_select(self, page, prompt):
        if (prompt.title == "Boot screen network status" and
                tuple(item.key for item in prompt.items) == ("install", "keep", "remove")):
            return self._build_boot_status(page, prompt)
        # Long details must not consume the action viewport on a small display.
        long_detail = len(prompt.detail.splitlines()) > 4
        self._header(page, prompt.title, "" if long_detail else prompt.detail, self._kicker())
        if prompt.prompt:
            question = tk.Label(page, text=prompt.prompt, font=self.theme.bold, bg=P["bg"],
                                fg=P["text"], anchor="w", justify="left")
            question.pack(fill="x", pady=(18, 0))
            _wrapping(question)
        enabled = [item.key for item in prompt.items if item.enabled]
        current = {"key": prompt.initial if prompt.initial in enabled else
                   (enabled[0] if enabled else None)}
        buttons = self._button_bar(page)
        area = ScrollArea(page, P["bg"])
        area.pack(fill="both", expand=True, pady=(12, 0))
        self._scroll_areas.append(area)
        if long_detail:
            detail = tk.Label(area.inner, text=prompt.detail, font=self.theme.base,
                              bg=P["bg"], fg=P["muted"], anchor="w", justify="left")
            detail.pack(fill="x", pady=(0, 16))
            _wrapping(detail)
        rows = {}

        def choose(key):
            current["key"] = key
            for row_key, row in rows.items():
                row.set_selected(row_key == key)
            area.show(rows[key])
            go.state(["!disabled"] if key in enabled else ["disabled"])

        def submit():
            if current["key"] in enabled:
                self.answer(current["key"])

        for item in prompt.items:
            row = OptionRow(area.inner, self.theme, item, item.key == current["key"],
                            choose, submit)
            row.pack(fill="x", pady=(0, 8))
            rows[item.key] = row
        go = ttk.Button(buttons, text="Continue", style="Accent.TButton", command=submit)
        go.pack(side="right")
        cancel = ttk.Button(buttons, text=prompt.cancel_label, command=lambda: self.answer(None))
        cancel.pack(side="right", padx=(0, 10))
        if current["key"] is None:
            go.state(["disabled"])

        def move(step):
            if not enabled:
                return "break"
            index = enabled.index(current["key"]) if current["key"] in enabled else -1
            choose(enabled[(index + step) % len(enabled)])
            return "break"

        page.bind("<Up>", lambda _event: move(-1))
        page.bind("<Down>", lambda _event: move(1))
        page.bind("<Return>", lambda _event: submit())
        page.bind("<KP_Enter>", lambda _event: submit())
        page.focus_set()
        self.controls = {"options": rows, "primary": go, "cancel": cancel,
                         "selected": lambda: current["key"], "choose": choose}

    def _build_boot_status(self, page, prompt):
        """Present the shared install/keep/remove choices as an ON-by-default checkbox."""
        self._header(page, prompt.title, prompt.detail, self._kicker())
        if prompt.prompt:
            self._callout(page, prompt.prompt).pack(fill="x", pady=(18, 0))
        labels = {item.key: item.label for item in prompt.items}
        enabled = tk.BooleanVar(page, value=prompt.initial == "install")
        checkbox = ttk.Checkbutton(page, text=labels["install"], variable=enabled)
        checkbox.pack(anchor="w", pady=(24, 10))
        note = tk.Label(page, text="Unchecked: " + labels["keep"] + ".\n"
                        "Unchecking does not remove an existing diagnostic.",
                        font=self.theme.small, bg=P["bg"], fg=P["muted"],
                        justify="left", anchor="w")
        note.pack(fill="x")
        _wrapping(note)
        remove = ttk.Button(page, text=labels["remove"],
                            command=lambda: self.answer("remove"))
        remove.pack(anchor="w", pady=(24, 0))
        buttons = self._button_bar(page)
        go = ttk.Button(buttons, text="Continue", style="Accent.TButton",
                        command=lambda: self.answer("install" if enabled.get() else "keep"))
        go.pack(side="right")
        cancel = ttk.Button(buttons, text=prompt.cancel_label, command=lambda: self.answer(None))
        cancel.pack(side="right", padx=(0, 10))
        checkbox.focus_set()
        self.controls = {"boot_enabled": enabled, "checkbox": checkbox, "remove": remove,
                         "primary": go, "cancel": cancel}

    def _build_calibration_consent(self, page, prompt):
        """Owner-approved Share default; showing this page never authorizes upload."""
        self._header(page, prompt.title, kicker=self._kicker())
        buttons = self._button_bar(page)
        disclosure = TextBox(page, self.theme, height=14)
        disclosure.pack(fill="both", expand=True, pady=(18, 0))
        disclosure.set(prompt.detail)
        labels = {item.key: item.label for item in prompt.items}
        share = ttk.Button(buttons, text=labels["share"], style="Accent.TButton",
                           command=lambda: self.answer("share"))
        share.pack(side="right")
        local = ttk.Button(buttons, text=labels["local"], command=lambda: self.answer("local"))
        local.pack(side="right", padx=(0, 10))
        for widget in (share, local):
            widget.bind("<Return>", lambda event: event.widget.invoke() or "break")
            widget.bind("<KP_Enter>", lambda event: event.widget.invoke() or "break")
        share.focus_set()
        self.controls = {"primary": share, "cancel": local, "disclosure": disclosure}

    def _build_confirm(self, page, prompt):
        writes = prompt.title.lower().startswith("confirm")
        self._header(page, prompt.title, kicker=self._kicker())
        self._callout(page, prompt.prompt or "Review the details, then select Confirm to "
                      "continue.", "danger" if "(unsafe)" in prompt.title else "warning" if writes else "info").pack(fill="x", pady=(14, 0))
        buttons = self._button_bar(page)
        review = TextBox(page, self.theme, height=10)
        review.pack(fill="both", expand=True, pady=(14, 0))
        review.set(prompt.detail)
        labels = {item.key: item.label for item in prompt.items}
        confirm = ttk.Button(buttons, text=labels.get("confirm", "Confirm"),
                             style="Danger.TButton" if writes else "Accent.TButton",
                             command=lambda: self.answer("confirm"))
        confirm.pack(side="right")
        cancel = ttk.Button(buttons, text=labels.get("cancel", "Cancel"),
                            command=lambda: self.answer(None))
        cancel.pack(side="right", padx=(0, 10))
        hint = tk.Label(buttons, text="Cancel is the default. Confirm unlocks after a brief safety delay.",
                        font=self.theme.small, bg=P["bg"], fg=P["muted"],
                        anchor="w")
        hint.pack(side="left", fill="x", expand=True, padx=(0, 12))
        _wrapping(hint)
        # A double-click carried over from the previous page must not confirm.
        confirm.state(["disabled"])
        self._confirm_job = self.root.after(_CONFIRM_ARM_MS, lambda: confirm.winfo_exists() and
                        self.prompt is prompt and confirm.state(["!disabled"]))
        for widget in (cancel, confirm):
            widget.bind("<Return>", lambda event: event.widget.invoke() or "break")
            widget.bind("<KP_Enter>", lambda event: event.widget.invoke() or "break")
        cancel.focus_set()
        self.controls = {"primary": confirm, "cancel": cancel, "review": review}

    def _build_multi(self, page, prompt):
        self._header(page, prompt.title, prompt.detail, self._kicker())
        allowed = {item.key for item in prompt.items if item.enabled}
        chosen = set(prompt.initial) & allowed
        tools = tk.Frame(page, bg=P["bg"])
        tools.pack(fill="x", pady=(16, 0))
        buttons = self._button_bar(page)
        area = ScrollArea(page, P["bg"])
        area.pack(fill="both", expand=True, pady=(10, 0))
        self._scroll_areas.append(area)
        rows = {}
        count = tk.Label(tools, text="", font=self.theme.small_bold, bg=P["bg"], fg=P["muted"])

        def update():
            for key, row in rows.items():
                row.set_selected(key in chosen)
            go.configure(text="Continue with {} selected".format(len(chosen)))
            go.state(["!disabled"] if chosen else ["disabled"])
            sized = any(hasattr(item, "size_bytes") for item in prompt.items)
            size = sum(getattr(item, "size_bytes", 0) for item in prompt.items
                       if item.key in chosen)
            count.configure(text=self.controller.flows.selection_summary(len(chosen), size)
                            if sized else "{} of {} selected".format(len(chosen), len(rows)))

        def toggle(key):
            if key in allowed:
                chosen.symmetric_difference_update({key})
            update()

        def everything(selected):
            chosen.clear()
            if selected:
                chosen.update(allowed)
            update()

        def submit():
            if chosen:
                self.answer(tuple(item.key for item in prompt.items if item.key in chosen))

        ttk.Button(tools, text="Select all", style="Link.TButton",
                   command=lambda: everything(True)).pack(side="left")
        ttk.Button(tools, text="Clear", style="Link.TButton",
                   command=lambda: everything(False)).pack(side="left", padx=(6, 0))
        count.pack(side="right")
        for item in prompt.items:
            row = OptionRow(area.inner, self.theme, item, item.key in chosen, toggle,
                            multi=True)
            row.pack(fill="x", pady=(0, 8))
            rows[item.key] = row
        go = ttk.Button(buttons, text="", style="Accent.TButton", command=submit)
        go.pack(side="right")
        cancel = ttk.Button(buttons, text="Cancel", command=lambda: self.answer(None))
        cancel.pack(side="right", padx=(0, 10))
        update()
        page.focus_set()
        focused = {"index": 0}
        keys = [item.key for item in prompt.items if item.enabled]

        def move(step):
            if keys:
                focused["index"] = (focused["index"] + step) % len(keys)
                row = rows[keys[focused["index"]]]
                area.show(row)
                for key, option in rows.items():
                    option.configure(highlightbackground=P["text"] if key == keys[focused["index"]]
                                     else P["accent"] if key in chosen else P["border"])
            return "break"

        def keyboard_toggle():
            if keys:
                toggle(keys[focused["index"]])
            return "break"

        page.bind("<Up>", lambda _event: move(-1))
        page.bind("<Down>", lambda _event: move(1))
        page.bind("<space>", lambda _event: keyboard_toggle())
        page.bind("<a>", lambda _event: everything(True))
        page.bind("<n>", lambda _event: everything(False))
        page.bind("<Return>", lambda _event: submit())
        self.controls = {"options": rows, "primary": go, "cancel": cancel,
                         "toggle": toggle, "all": everything, "chosen": chosen,
                         "selection_summary": count}

    def _build_text(self, page, prompt):
        self._header(page, prompt.title, prompt.detail, self._kicker())
        question = tk.Label(page, text=prompt.prompt, font=self.theme.bold, bg=P["bg"],
                            fg=P["text"], anchor="w", justify="left")
        question.pack(fill="x", pady=(20, 8))
        value = tk.StringVar(page, value=prompt.default)
        row = tk.Frame(page, bg=P["bg"])
        row.pack(fill="x")
        entry = ttk.Entry(row, textvariable=value, font=self.theme.base,
                          show="\u2022" if prompt.password else "")
        entry.pack(side="left", fill="x", expand=True)
        is_path = prompt.prompt.lower().startswith("path to")
        if is_path:
            folder = "folder" in prompt.prompt.lower()
            if folder:
                ttk.Button(row, text="Choose folder...",
                           command=lambda: self._browse(value, folder=True)).pack(
                               side="right", padx=(10, 0))
            ttk.Button(row, text="Choose file...",
                       command=lambda: self._browse(value, json_file=folder)).pack(
                           side="right", padx=(10, 0))
        if prompt.password:
            tk.Label(page, text="Characters are hidden. The password is passed only to the "
                     "toolkit's Wi-Fi edit and is never shown or logged.",
                     font=self.theme.small, bg=P["bg"], fg=P["muted"], anchor="w",
                     justify="left").pack(fill="x", pady=(8, 0))
        buttons = self._button_bar(page)

        def submit(_event=None):
            text = value.get()
            if not (text if prompt.password else text.strip()):
                return "break"
            value.set("")
            self.answer(text)
            return "break"

        def cancel_prompt():
            value.set("")
            self.answer(None)

        go = ttk.Button(buttons, text="Continue", style="Accent.TButton", command=submit)
        go.pack(side="right")
        cancel = ttk.Button(buttons, text="Cancel", command=cancel_prompt)
        cancel.pack(side="right", padx=(0, 10))

        def changed(*_args):
            text = value.get()
            go.state(["!disabled"] if (text if prompt.password else text.strip())
                     else ["disabled"])

        value.trace_add("write", changed)
        changed()
        entry.bind("<Return>", submit)
        entry.bind("<KP_Enter>", submit)
        entry.focus_set()
        entry.icursor("end")
        self.controls = {"entry": entry, "value": value, "primary": go, "cancel": cancel}

    def _browse(self, value, folder=False, json_file=False):
        initial = getattr(self.controller.api, "BACKUP_ROOT", None)
        options = {"parent": self.root}
        if initial:
            options["initialdir"] = str(initial)
        if folder:
            chosen = filedialog.askdirectory(title="Choose a backup set folder", **options)
        elif json_file:
            chosen = filedialog.askopenfilename(
                title="Choose backup-set.json",
                filetypes=(("Backup set", "backup-set.json"), ("JSON", "*.json"),
                           ("All files", "*")), **options)
        else:
            chosen = filedialog.askopenfilename(
                title="Choose an image", filetypes=(("Disk images", "*.img *.bin"),
                                                    ("All files", "*")), **options)
        if chosen:
            value.set(str(chosen))

    def _build_message(self, page, prompt):
        self._header(page, prompt.title, kicker=self._kicker())
        buttons = self._button_bar(page)
        body = TextBox(page, self.theme, height=10)
        body.pack(fill="both", expand=True, pady=(16, 0))
        body.set("\n".join(prompt.lines))
        back = ttk.Button(buttons, text="Back", style="Accent.TButton",
                          command=lambda: self.answer(None))
        back.pack(side="right")
        back.focus_set()
        self.controls = {"primary": back, "body": body}

    def _build_phoenix_card(self, parent, on_dismiss, *, show_prerequisite=False):
        card = tk.Frame(parent, bg=P["surface"], highlightthickness=1,
                        highlightbackground=P["border"])
        card.pack(fill="x", pady=(16, 0))
        body = tk.Frame(card, bg=P["surface"])
        body.pack(fill="x", padx=18, pady=16)
        title = tk.Label(body, text=PHOENIX_CARD_TITLE, font=self.theme.heading,
                         bg=P["surface"], fg=P["text"], anchor="w", justify="left")
        title.pack(fill="x")
        _wrapping(title)
        intro = tk.Label(body, text=PHOENIX_INTRO, font=self.theme.small,
                         bg=P["surface"], fg=P["muted"], anchor="w", justify="left")
        intro.pack(fill="x", pady=(4, 8))
        _wrapping(intro)
        steps = ((PHOENIX_PREREQUISITE,) if show_prerequisite else ()) + PHOENIX_STEPS
        for step in steps:
            label = tk.Label(body, text=step, font=self.theme.base, bg=P["surface"],
                             fg=P["text"], anchor="w", justify="left")
            label.pack(fill="x", pady=(4, 0))
            _wrapping(label)
        buttons = tk.Frame(body, bg=P["surface"])
        buttons.pack(fill="x", pady=(14, 0))
        open_button = ttk.Button(buttons, text="Open jibo.io",
                                 command=self.controller.open_phoenix_guide)
        open_button.pack(side="left")

        def dismiss():
            self.controller.dismiss_phoenix()
            card.destroy()
            for key in ("phoenix_card", "phoenix_title", "phoenix_open", "phoenix_dismiss"):
                self.controls.pop(key, None)
            on_dismiss()

        not_now = ttk.Button(buttons, text="Not now", command=dismiss)
        not_now.pack(side="left", padx=(10, 0))
        self.controls.update(phoenix_card=card, phoenix_title=title,
                             phoenix_open=open_button, phoenix_dismiss=not_now)
        return card

    # -- results -----------------------------------------------------------
    def show_outcome(self, outcome):
        self._stop_progress()
        if outcome is None or outcome.kind == "note":
            self.show_home()
            return
        if outcome.kind == "error":
            tone, badge = "danger", "FAILED"
        elif outcome.status == "cancelled":
            tone, badge = "neutral", "CANCELLED"
        else:
            tone, badge = "success", "DONE"
        background, foreground = (P[name] for name in _TONES[tone])
        page = self._new_page("result")
        tk.Label(page, text=badge, font=self.theme.caps, bg=background, fg=foreground,
                 padx=10, pady=3).pack(anchor="w", pady=(0, 10))
        self._header(page, display_label(outcome.title) or "Finished")
        buttons = self._button_bar(page)
        area = ScrollArea(page, P["bg"])
        area.pack(fill="both", expand=True)
        self._scroll_areas.append(area)
        summary = tk.Frame(area.inner, bg=P["surface"], highlightthickness=1,
                           highlightbackground=P["border"])
        summary.pack(fill="x", pady=(16, 0))
        stripe = tk.Frame(summary, bg=P[{"danger": "danger", "neutral": "border_strong",
                                          "success": "success"}[tone]], width=4)
        stripe.pack(side="left", fill="y")
        lines = tk.Frame(summary, bg=P["surface"])
        lines.pack(side="left", fill="both", expand=True, padx=18, pady=14)
        for index, line in enumerate(outcome.lines or ("Finished.",)):
            label = tk.Label(lines, text=line, font=self.theme.bold if index == 0 else
                             self.theme.base, bg=P["surface"], fg=P["text"], anchor="w",
                             justify="left")
            label.pack(fill="x", pady=(0 if index == 0 else 4, 0))
            _wrapping(label)
        if self.controller.should_show_phoenix(outcome):
            self._build_phoenix_card(area.inner, lambda: None)
        log_lines = self.controller.output_state().lines
        log = None
        if log_lines:
            log = TextBox(area.inner, self.theme, font=self.theme.mono, height=8)
            log.set("\n".join(log_lines), see_end=True)
            toggle = ttk.Button(area.inner, text="Show operation log", style="Link.TButton")

            def flip():
                if log.winfo_ismapped():
                    log.pack_forget()
                    toggle.configure(text="Show operation log")
                else:
                    log.pack(fill="both", expand=True, pady=(8, 0))
                    toggle.configure(text="Hide operation log")

            toggle.configure(command=flip)
            toggle.pack(anchor="w", pady=(14, 0))
            self.controls["log_toggle"] = toggle
        back = ttk.Button(buttons, text="Back to actions", style="Accent.TButton",
                          command=self.show_home)
        back.pack(side="right")
        back.focus_set()
        back.bind("<Return>", lambda _event: self.show_home())
        self.controls.update({"primary": back, "log": log, "tone": tone})

    # -- event loop --------------------------------------------------------
    def _poll(self):
        if self.closed:
            return
        try:
            state = output = False
            for event in self.controller.drain():
                kind = event[0]
                if kind == "state":
                    state = True
                elif kind == "output":
                    output = True
                elif kind == "download":
                    output = True
                elif kind == "started":
                    if self.page_name != "progress":
                        self.show_progress()
                elif kind == "prompt":
                    self.show_prompt(event[1])
                elif kind == "finished":
                    self.show_outcome(event[1])
                    state = True
            if output:
                self.render_output()
            if state:
                self.render_state()
        finally:
            if not self.closed:
                self._poll_job = self.root.after(self.POLL_MS, self._poll)

    def _auto_refresh(self):
        if self.closed:
            return
        if not self.controller.busy:
            self.controller.request_refresh()
        self._refresh_job = self.root.after(self.REFRESH_MS, self._auto_refresh)

    def _wheel(self, event):
        widget = self.root.winfo_containing(event.x_root, event.y_root)
        if widget is None or isinstance(widget, tk.Text):
            return None
        while widget is not None:
            if isinstance(widget, ScrollArea):
                if getattr(event, "num", None) == 4:
                    steps = -1
                elif getattr(event, "num", None) == 5:
                    steps = 1
                else:
                    steps = -1 if event.delta > 0 else 1
                widget.scroll(steps * 2)
                return "break"
            widget = getattr(widget, "master", None)
        return None

    def close(self):
        if self.closed:
            return
        if self.controller.busy:
            pending = self.controller.pending_prompt
            if self.controller.download_active:
                if not messagebox.askyesno(
                        "Stop the download and quit?",
                        "No robot is being changed. What was already downloaded is kept "
                        "and continues next time. Quit now?", icon="warning", default="no",
                        parent=self.root):
                    return
            elif pending is None or pending.answered:
                messagebox.showwarning(
                    "Operation in progress",
                    "A robot operation is still running. Closing the window now could "
                    "interrupt a USB transfer. Wait until it finishes.", parent=self.root)
                return
            elif not messagebox.askyesno(
                    "Cancel and quit?",
                    "The open question will be answered with Cancel, then the window "
                    "closes. Quit now?", icon="warning", default="no", parent=self.root):
                return
        if not self.controller.shutdown_if_safe():
            messagebox.showwarning("Operation in progress",
                                   "The question was already answered. Wait until the operation finishes.",
                                   parent=self.root)
            return
        self.closed = True
        for job in (self._confirm_job, getattr(self, "_poll_job", None),
                    getattr(self, "_refresh_job", None)):
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
        self._stop_progress()
        self.root.destroy()


def create_root():
    """Create the Tk root, translating a missing display into DisplayUnavailable."""
    try:
        return tk.Tk(className="JiboDfuToolkit")
    except tk.TclError as exc:
        raise DisplayUnavailable(str(exc)) from exc


def supervise_windows_startup(root, controller, window, ready_file):
    """A Linux map is only half of the Windows release startup handshake.

    Keep robot actions unavailable until the launcher observes this invocation's
    visible, responsive native window. A failed startup closes only while idle;
    it never answers a robot question or interrupts an operation.
    """
    ready = Path(ready_file)
    title = root.title()
    root.title(title + ' [startup ' + ready.parent.name + ']')
    start_action = controller.start_action
    controller.start_action = lambda *_args, **_kwargs: False

    def signal_ready():
        if root.winfo_viewable():
            ready.write_text('ready\n', encoding='ascii')

    def on_map(event):
        if event.widget is root:
            root.after_idle(signal_ready)

    def poll():
        if Path(str(ready) + '.close').exists():
            if not controller.busy:
                window.close()
                return
        elif Path(str(ready) + '.visible').exists():
            controller.start_action = start_action
            root.title(title)
            return
        root.after(100, poll)

    root.bind('<Map>', on_map, add='+')
    root.after_idle(signal_ready)
    root.after(100, poll)


def main(api):
    """Open the window for the real device layer and run until it closes."""
    root = create_root()
    controller = core.GuiController(api, preferences_path=api.BACKUP_ROOT / ".phoenix-guidance.json")
    window = ToolkitWindow(root, controller)
    # Only the Windows release wrapper supplies these per-launch files.
    ready_file = os.environ.get('JIBO_GUI_READY_FILE')
    if ready_file:
        supervise_windows_startup(root, controller, window, ready_file)
    with core.capture_output(controller):
        root.mainloop()
    return 0
