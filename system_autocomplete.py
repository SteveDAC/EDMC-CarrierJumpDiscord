"""
Spansh-backed system name autocomplete for Tk dialogs.

Adapted from the Spansh Tools AutoCompleter pattern (debounce + background
worker + live GET /api/systems). No dependency on that plugin.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Callable, Optional

import tkinter as tk

logger = logging.getLogger(__name__)

SPANSH_SYSTEMS_URL = "https://spansh.co.uk/api/systems"
DEBOUNCE_MS = 250
MAX_VISIBLE_RESULTS = 8
MIN_QUERY_LEN = 3
DEFAULT_TIMEOUT = 3.0


def search_systems(
    query: str,
    *,
    user_agent: str = "EDMC-CarrierJumpDiscord",
    timeout: float = DEFAULT_TIMEOUT,
) -> list[str]:
    """Return system name suggestions from Spansh for a partial query."""
    q = (query or "").strip()
    if len(q) < MIN_QUERY_LEN:
        return []
    try:
        import requests
    except ImportError:
        logger.warning("requests unavailable for Spansh system search")
        return []
    try:
        resp = requests.get(
            SPANSH_SYSTEMS_URL,
            params={"q": q},
            headers={"User-Agent": user_agent},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logger.exception("Spansh system search failed for %r", q)
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data if str(item).strip()]


class PlaceHolder(tk.Entry):
    """Entry that shows grey placeholder text when empty."""

    def __init__(self, parent: tk.Misc, placeholder: str, **kw: Any) -> None:
        super().__init__(parent, **kw)
        self.var = self["textvariable"] = tk.StringVar()
        self.placeholder = placeholder
        self.placeholder_color = "grey"
        self._placeholder_visible = False
        self._error_state = False
        self._default_fg = kw.get("fg") or self.cget("fg") or "black"

        self.bind("<FocusIn>", self.foc_in)
        self.bind("<FocusOut>", self.foc_out)
        self.put_placeholder()

    def put_placeholder(self) -> None:
        if self.get() != self.placeholder:
            self.set_text(self.placeholder, True)

    def set_text(self, text: str, placeholder_style: bool = True) -> None:
        if placeholder_style:
            self._placeholder_visible = True
            self._error_state = False
            self["fg"] = self.placeholder_color
        else:
            self._placeholder_visible = False
            self.set_default_style()
        self.delete(0, tk.END)
        self.insert(0, text)

    def set_default_style(self) -> None:
        self._error_state = False
        self["fg"] = self._default_fg

    def foc_in(self, *_args: Any) -> None:
        if self._error_state or self._placeholder_visible:
            self.set_default_style()
            if self._placeholder_visible and self.get() == self.placeholder:
                self.delete(0, tk.END)
                self._placeholder_visible = False
                return
        if self.get():
            self.after(10, lambda: self.select_range(0, tk.END))

    def foc_out(self, *_args: Any) -> None:
        if not self.get():
            self.put_placeholder()


class SystemAutoCompleter(PlaceHolder):
    """Entry with dropdown suggestions from Spansh system search."""

    def __init__(
        self,
        parent: tk.Misc,
        placeholder: str,
        *,
        user_agent: str = "EDMC-CarrierJumpDiscord",
        on_select: Optional[Callable[[str], None]] = None,
        **kw: Any,
    ) -> None:
        self.on_select = on_select
        self.user_agent = user_agent
        self.parent = parent
        entry_kw = dict(kw)
        listbox_kw = {key: kw[key] for key in ("width", "font") if key in kw}

        self.lb = tk.Listbox(self.parent, selectmode=tk.SINGLE, **listbox_kw)
        self.lb_up = False
        self.has_selected = False
        self.queue: queue.Queue[tuple[Optional[int], list[str]]] = queue.Queue()
        self._debounce_id: Optional[str] = None
        self._query_generation = 0
        self._active_queries = 0
        self._query_lock = threading.Lock()
        self._query_event = threading.Event()
        self._query_worker: Optional[threading.Thread] = None
        self._pending_query: Optional[tuple[str, int]] = None
        self._destroyed = False
        self._result_values: list[str] = []
        self._trace_id: Optional[str] = None
        self._update_id: Optional[str] = None
        self._last_fetch_ok = True

        PlaceHolder.__init__(self, parent, placeholder, **entry_kw)
        self._bind_change_trace()

        self.bind("<Any-Key>", self.keypressed)
        self.lb.bind("<Any-Key>", self.keypressed)
        self.bind("<Return>", self._handle_return)
        self.bind("<KP_Enter>", self._handle_return)
        self.lb.bind("<Return>", self._handle_return)
        self.lb.bind("<KP_Enter>", self._handle_return)
        self.lb.bind("<ButtonRelease-1>", self.selection)
        self.bind("<FocusOut>", self.ac_foc_out)
        self.lb.bind("<FocusOut>", self.ac_foc_out)
        self.bind("<Destroy>", self._on_destroy)

    def is_effectively_empty(self) -> bool:
        value = self.get().strip()
        return not value or value == self.placeholder

    def get_system_name(self) -> str:
        if self.is_effectively_empty():
            return ""
        return self.get().strip()

    def last_fetch_ok(self) -> bool:
        return self._last_fetch_ok

    def _bind_change_trace(self) -> None:
        self._trace_id = self.var.trace("w", self.changed)

    def _replace_text_without_trace(self, text: str) -> None:
        try:
            if self._trace_id is not None:
                self.var.trace_remove("write", self._trace_id)
        except Exception:
            pass
        self.delete(0, tk.END)
        self.insert(0, text)
        self._bind_change_trace()

    def set_text(self, text: str, placeholder_style: bool = True) -> None:
        if placeholder_style:
            self._placeholder_visible = True
            self._error_state = False
            self["fg"] = self.placeholder_color
        else:
            self._placeholder_visible = False
            self.set_default_style()
        self._replace_text_without_trace(text)

    def _cancel_debounce(self) -> None:
        if self._debounce_id is None:
            return
        try:
            self.after_cancel(self._debounce_id)
        except Exception:
            pass
        self._debounce_id = None

    def _discard_query_state(self) -> None:
        self._query_generation += 1
        self.hide_list()
        self.has_selected = False

    def _on_destroy(self, _event: Optional[tk.Event] = None) -> None:
        self._destroyed = True
        self._query_generation += 1
        self._cancel_debounce()
        if self._update_id is not None:
            try:
                self.after_cancel(self._update_id)
            except Exception:
                pass
            self._update_id = None
        with self._query_lock:
            self._pending_query = None
            self._active_queries = 0
        self._query_event.set()
        self.lb_up = False
        try:
            self.lb.destroy()
        except Exception:
            pass

    def ac_foc_out(self, event: Optional[tk.Event] = None) -> None:
        x, y = self.parent.winfo_pointerxy()
        widget_under_cursor = self.parent.winfo_containing(x, y)
        if (widget_under_cursor != self.lb and widget_under_cursor != self) or event is None:
            self.foc_out()
            self.hide_list()

    def keypressed(self, event: tk.Event) -> None:
        key = event.keysym
        if self._placeholder_visible and event.char and event.char.isprintable():
            self.set_default_style()
            self.delete(0, tk.END)
            self._placeholder_visible = False
            self.has_selected = False
            return
        if key == "Down":
            self._move_selection(1, event.widget.widgetName)
        elif key == "Up":
            self._move_selection(-1, event.widget.widgetName)
        elif key in ("Return", "Right"):
            if self.lb_up:
                self.selection()
        elif key in ("Escape", "Tab", "ISO_Left_Tab") and self.lb_up:
            self.hide_list()

    def _handle_return(self, _event: Optional[tk.Event] = None) -> Optional[str]:
        if self.lb_up:
            self.selection()
            return "break"
        return None

    def _next_list_index(self, step: int) -> Optional[int]:
        selection = self.lb.curselection()
        if not selection:
            return 0 if step > 0 else None
        index = int(selection[0])
        next_index = index + step
        if not (0 <= next_index < self.lb.size()):
            return index
        self.lb.selection_clear(first=index)
        return next_index

    def _move_selection(self, step: int, widget: str) -> None:
        if not self.lb_up:
            if step > 0:
                self.changed()
            return
        index = self._next_list_index(step)
        if index is None:
            return
        self.lb.selection_set(first=index)
        self.lb.see(index)
        if widget != "listbox":
            self.lb.activate(index)

    def changed(self, *_args: Any) -> None:
        if self._destroyed:
            return
        if self._error_state:
            self.set_default_style()
        value = self.var.get()
        stripped = value.strip()
        self._cancel_debounce()
        if self.has_selected or stripped == self.placeholder or len(stripped) < MIN_QUERY_LEN:
            self._discard_query_state()
            return
        self._debounce_id = self.after(DEBOUNCE_MS, lambda v=value: self._queue_query(v))

    def _queue_query(self, value: str) -> None:
        self._debounce_id = None
        if self._destroyed:
            return
        self._query_generation += 1
        gen = self._query_generation
        with self._query_lock:
            self._pending_query = (value, gen)
            self._active_queries = 1
            worker = self._query_worker
            if worker is None or not worker.is_alive():
                worker = threading.Thread(target=self._query_worker_loop, daemon=True)
                self._query_worker = worker
                worker.start()
        self._schedule_update()
        self._query_event.set()

    def _query_worker_loop(self) -> None:
        while True:
            self._query_event.wait()
            if self._destroyed:
                return
            while True:
                with self._query_lock:
                    item = self._pending_query
                    if item is None:
                        self._active_queries = 0
                        self._query_event.clear()
                        break
                    self._pending_query = None
                self._fetch_query_results(*item)
                if self._destroyed:
                    return

    def _fetch_query_results(self, inp: str, generation: Optional[int] = None) -> None:
        inp = inp.strip()
        try:
            if inp == self.placeholder or len(inp) < MIN_QUERY_LEN:
                self._enqueue_results([], generation=generation)
                return
            try:
                import requests

                resp = requests.get(
                    SPANSH_SYSTEMS_URL,
                    params={"q": inp},
                    headers={"User-Agent": self.user_agent},
                    timeout=DEFAULT_TIMEOUT,
                )
                resp.raise_for_status()
                data = resp.json()
                lista = [str(item) for item in data] if isinstance(data, list) else []
                self._last_fetch_ok = True
            except Exception:
                logger.exception("Spansh autocomplete fetch failed")
                self._last_fetch_ok = False
                lista = []
            if generation is not None and generation != self._query_generation:
                return
            self._enqueue_results(lista, generation=generation)
        finally:
            with self._query_lock:
                self._active_queries = 1 if self._pending_query is not None else 0

    def _enqueue_results(
        self, lista: list[str], generation: Optional[int] = None
    ) -> None:
        self.queue.put((generation, lista))

    def _schedule_update(self) -> None:
        if not self._destroyed and self._update_id is None:
            self._update_id = self.after(100, self._flush_results_queue)

    def _flush_results_queue(self) -> None:
        if self._destroyed:
            self._update_id = None
            return
        try:
            while True:
                generation, lista = self.queue.get_nowait()
                if generation is not None and generation != self._query_generation:
                    continue
                self.show_results(lista)
        except queue.Empty:
            pass
        self._update_id = None
        with self._query_lock:
            keep_polling = self._active_queries > 0
        if keep_polling or not self.queue.empty():
            self._schedule_update()

    def selection(self, _event: Optional[tk.Event] = None) -> None:
        if not self.lb_up:
            return
        sel = self.lb.curselection()
        if not sel:
            return
        selected_index = int(sel[0])
        if selected_index >= len(self._result_values):
            return
        self.has_selected = True
        self._replace_text_without_trace(self._result_values[selected_index])
        self.hide_list()
        self.icursor(tk.END)
        if callable(self.on_select):
            try:
                self.on_select(self.get().strip())
            except Exception:
                logger.exception("SystemAutoCompleter on_select failed")

    def show_results(self, results: list[str]) -> None:
        if self._destroyed:
            return
        if results:
            self._result_values = list(results)
            self.lb.delete(0, tk.END)
            for name in self._result_values:
                self.lb.insert(tk.END, name)
            self.show_list(len(results))
        else:
            self._result_values = []
            if self.lb_up:
                self.hide_list()

    def show_list(self, height: int) -> None:
        self.lb["height"] = min(height, MAX_VISIBLE_RESULTS)
        if not self.lb_up and self.parent.focus_get() is self:
            info = self.grid_info()
            if info:
                grid_kwargs = {}
                for key in ("column", "columnspan", "sticky", "padx", "pady"):
                    if key in info:
                        grid_kwargs[key] = info[key]
                self.lb.grid(row=int(info["row"]) + 1, **grid_kwargs)
                self.lb.lift()
                self.lb_up = True

    def hide_list(self) -> None:
        if self.lb_up:
            self.lb.grid_remove()
            self.lb_up = False
