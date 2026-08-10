from __future__ import annotations

import sys
from typing import Any


class SmoothScrollController:
    """One global wheel/trackpad handler for a Tk window.

    Only canvases explicitly registered with ``set_active_canvas`` are scrolled.
    This prevents small canvases used for cover art from consuming wheel events.
    """

    def __init__(self, root: Any, tk: Any, ttk: Any) -> None:
        self.root = root
        self.tk = tk
        self.ttk = ttk
        self.active_canvas: Any | None = None
        self._velocity = 0.0
        self._job: str | None = None

        root.bind_all("<MouseWheel>", self._on_wheel, add="+")
        root.bind_all("<Button-4>", self._on_wheel, add="+")
        root.bind_all("<Button-5>", self._on_wheel, add="+")

    def set_active_canvas(self, canvas: Any | None) -> None:
        self.active_canvas = canvas
        self._velocity = 0.0
        if self._job:
            try:
                self.root.after_cancel(self._job)
            except self.tk.TclError:
                pass
            self._job = None
        if canvas is not None:
            setattr(canvas, "_pudge_page_canvas", True)

    def _widget_under_pointer(self) -> Any | None:
        try:
            return self.root.winfo_containing(
                self.root.winfo_pointerx(), self.root.winfo_pointery()
            )
        except self.tk.TclError:
            return None

    def _nearest_treeview(self, widget: Any | None) -> Any | None:
        current = widget
        while current is not None:
            if isinstance(current, self.ttk.Treeview):
                return current
            try:
                parent_name = current.winfo_parent()
                current = current._nametowidget(parent_name) if parent_name else None
            except (self.tk.TclError, KeyError):
                return None
        return None

    @staticmethod
    def _direction(event: Any) -> float:
        raw = getattr(event, "delta", 0) or 0
        try:
            delta = float(raw)
        except (TypeError, ValueError):
            delta = 0.0
        if delta:
            return delta
        if getattr(event, "num", None) == 4:
            return 1.0
        if getattr(event, "num", None) == 5:
            return -1.0
        return 0.0

    def _on_wheel(self, event: Any) -> str | None:
        delta = self._direction(event)
        if not delta:
            return None

        tree = self._nearest_treeview(self._widget_under_pointer())
        if tree is not None:
            units = -1 if delta > 0 else 1
            tree.yview_scroll(units, "units")
            return "break"

        canvas = self.active_canvas
        if canvas is None:
            return None
        try:
            if not canvas.winfo_exists():
                return None
        except self.tk.TclError:
            return None

        # Cocoa Tk reports trackpad deltas as small signed values, while other
        # Tk builds often report +/-120 for a mouse notch.
        if abs(delta) >= 60:
            pixels = -(delta / 120.0) * 64.0
        elif sys.platform == "darwin":
            pixels = -delta * 30.0
        else:
            pixels = -delta * 42.0

        self._velocity = max(-300.0, min(300.0, self._velocity + pixels))
        if self._job is None:
            self._job = self.root.after(16, self._animate)
        return "break"

    def _animate(self) -> None:
        self._job = None
        canvas = self.active_canvas
        if canvas is None:
            self._velocity = 0.0
            return
        try:
            bbox = canvas.bbox("all")
            if not bbox:
                self._velocity = 0.0
                return
            top, bottom = float(bbox[1]), float(bbox[3])
            content_height = max(1.0, bottom - top)
            viewport = max(1.0, float(canvas.winfo_height()))
            max_scroll = max(0.0, content_height - viewport)
            current = max(0.0, min(max_scroll, float(canvas.canvasy(0)) - top))
            step = self._velocity * 0.30
            new_pos = max(0.0, min(max_scroll, current + step))
            canvas.yview_moveto(new_pos / content_height)
        except self.tk.TclError:
            self._velocity = 0.0
            return

        at_boundary = (new_pos <= 0.0 and self._velocity < 0) or (
            new_pos >= max_scroll and self._velocity > 0
        )
        self._velocity = 0.0 if at_boundary else self._velocity * 0.72
        if abs(self._velocity) >= 0.45:
            self._job = self.root.after(16, self._animate)
        else:
            self._velocity = 0.0


def enable_edit_shortcuts(root: Any, widget: Any, tk: Any) -> None:
    """Install reliable macOS editing shortcuts directly on an Entry widget."""

    def delete_selection() -> None:
        try:
            widget.delete("sel.first", "sel.last")
        except (tk.TclError, AttributeError):
            pass

    def paste(_event: Any = None) -> str:
        try:
            value = root.clipboard_get()
        except tk.TclError:
            return "break"
        delete_selection()
        try:
            widget.insert("insert", value)
        except (tk.TclError, AttributeError):
            pass
        return "break"

    def copy(_event: Any = None) -> str:
        try:
            value = widget.selection_get()
        except (tk.TclError, AttributeError):
            return "break"
        root.clipboard_clear()
        root.clipboard_append(value)
        return "break"

    def cut(event: Any = None) -> str:
        copy(event)
        delete_selection()
        return "break"

    def select_all(_event: Any = None) -> str:
        try:
            widget.selection_range(0, "end")
            widget.icursor("end")
        except (tk.TclError, AttributeError):
            pass
        return "break"

    bindings = {
        "v": paste,
        "c": copy,
        "x": cut,
        "a": select_all,
    }
    for key, callback in bindings.items():
        for pattern in (
            f"<Command-KeyPress-{key}>",
            f"<Command-{key}>",
            f"<Meta-KeyPress-{key}>",
        ):
            widget.bind(pattern, callback, add="+")

    widget.bind("<<Paste>>", paste, add="+")


def bind_canvas_title_overlay(root: Any, canvas: Any, text: str, width: int, height: int) -> None:
    """Show the anime title inside the cover canvas on hover."""

    state: dict[str, Any] = {"after": None}

    def hide(_event: Any = None) -> None:
        after_id = state.get("after")
        if after_id:
            try:
                root.after_cancel(after_id)
            except Exception:
                pass
            state["after"] = None
        try:
            canvas.delete("anime-title-overlay")
        except Exception:
            pass

    def show() -> None:
        state["after"] = None
        try:
            canvas.delete("anime-title-overlay")
            overlay_height = min(66, max(44, height // 3))
            y0 = height - overlay_height
            canvas.create_rectangle(
                0,
                y0,
                width,
                height,
                fill="#101827",
                outline="",
                tags="anime-title-overlay",
            )
            canvas.create_text(
                width / 2,
                y0 + overlay_height / 2,
                text=text,
                fill="#ffffff",
                width=max(40, width - 10),
                justify="center",
                font=("Helvetica Neue", 9, "bold"),
                tags="anime-title-overlay",
            )
            canvas.tag_raise("anime-title-overlay")
        except Exception:
            pass

    def enter(_event: Any = None) -> None:
        hide()
        state["after"] = root.after(220, show)

    canvas.bind("<Enter>", enter, add="+")
    canvas.bind("<Leave>", hide, add="+")
    canvas.bind("<ButtonPress>", hide, add="+")
