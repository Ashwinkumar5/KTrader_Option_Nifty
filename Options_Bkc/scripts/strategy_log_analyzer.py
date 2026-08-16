from __future__ import annotations

import argparse
import sys
import tkinter as tk
from decimal import Decimal
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.research.strategy_trace import analyze_strategy, catalog_traces


DEFAULT_SOURCE = Path(r"E:\Option_Trade\data")


class StrategyLogAnalyzer(tk.Tk):
    def __init__(self, source: Path) -> None:
        super().__init__()
        self.title("Strategy Log Analyzer")
        self.geometry("1380x760")
        self.minsize(1050, 600)
        self.source_var = tk.StringVar(value=str(source))
        self.strategy_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Choose a trace folder or file.")
        self._files: tuple[Path, ...] = ()
        self._build()
        self.load_source()

    def _build(self) -> None:
        controls = ttk.Frame(self, padding=10)
        controls.pack(fill=tk.X)
        ttk.Label(controls, text="Trace source").grid(row=0, column=0, sticky="w")
        ttk.Entry(controls, textvariable=self.source_var, width=78).grid(
            row=0, column=1, padx=6, sticky="ew"
        )
        ttk.Button(controls, text="Folder", command=self.choose_folder).grid(
            row=0, column=2, padx=3
        )
        ttk.Button(controls, text="File", command=self.choose_file).grid(
            row=0, column=3, padx=3
        )
        ttk.Button(controls, text="Load", command=self.load_source).grid(
            row=0, column=4, padx=3
        )
        ttk.Label(controls, text="Enabled strategy").grid(
            row=1, column=0, pady=(10, 0), sticky="w"
        )
        self.strategy_box = ttk.Combobox(
            controls,
            textvariable=self.strategy_var,
            state="readonly",
            width=30,
        )
        self.strategy_box.grid(row=1, column=1, pady=(10, 0), sticky="w")
        ttk.Button(controls, text="Analyze 10 minutes", command=self.analyze).grid(
            row=1, column=2, columnspan=2, pady=(10, 0), padx=3
        )
        controls.columnconfigure(1, weight=1)

        self.summary = ttk.Label(self, padding=(10, 2), textvariable=self.status_var)
        self.summary.pack(fill=tk.X)

        columns = (
            "time",
            "strategy",
            "status",
            "side",
            "symbol",
            "entry",
            "bid10",
            "return",
            "max_gain",
            "max_drawdown",
            "gate",
            "reason",
        )
        self.table = ttk.Treeview(self, columns=columns, show="headings")
        headings = {
            "time": "Time (IST)",
            "strategy": "Strategy",
            "status": "Output",
            "side": "Side",
            "symbol": "Contract",
            "entry": "Entry Ask",
            "bid10": "10m Bid",
            "return": "10m Return %",
            "max_gain": "Max Gain %",
            "max_drawdown": "Max DD %",
            "gate": "Gate",
            "reason": "Reason",
        }
        widths = {
            "time": 145,
            "strategy": 145,
            "status": 85,
            "side": 80,
            "symbol": 180,
            "entry": 75,
            "bid10": 75,
            "return": 95,
            "max_gain": 90,
            "max_drawdown": 90,
            "gate": 70,
            "reason": 420,
        }
        for column in columns:
            self.table.heading(column, text=headings[column])
            self.table.column(column, width=widths[column], anchor="w")
        y_scroll = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self.table.yview)
        x_scroll = ttk.Scrollbar(self, orient=tk.HORIZONTAL, command=self.table.xview)
        self.table.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.table.pack(fill=tk.BOTH, expand=True, padx=(10, 26), pady=(6, 0))
        y_scroll.place(relx=1.0, rely=0.19, relheight=0.77, x=-20)
        x_scroll.pack(fill=tk.X, padx=10, pady=(0, 10))

    def choose_folder(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.source_var.get())
        if selected:
            self.source_var.set(selected)
            self.load_source()

    def choose_file(self) -> None:
        selected = filedialog.askopenfilename(
            initialdir=str(Path(self.source_var.get()).parent),
            filetypes=(("JSONL trace", "*.jsonl"), ("All files", "*.*")),
        )
        if selected:
            self.source_var.set(selected)
            self.load_source()

    def load_source(self) -> None:
        source = Path(self.source_var.get())
        catalog = catalog_traces(source)
        self._files = catalog.files
        self.strategy_box["values"] = catalog.enabled_strategies
        if catalog.enabled_strategies:
            current = self.strategy_var.get()
            self.strategy_var.set(
                current
                if current in catalog.enabled_strategies
                else catalog.enabled_strategies[0]
            )
        else:
            self.strategy_var.set("")
        self.status_var.set(
            f"Loaded {len(catalog.files)} trace file(s); enabled strategies: "
            f"{', '.join(catalog.enabled_strategies) or 'none'}"
        )

    def analyze(self) -> None:
        strategy = self.strategy_var.get()
        if not self._files or not strategy:
            messagebox.showinfo("Strategy Log Analyzer", "Load a valid trace first.")
            return
        self.table.delete(*self.table.get_children())
        try:
            outcomes = analyze_strategy(
                self._files,
                strategy=strategy,
                horizon_minutes=10,
            )
        except OSError as exc:
            messagebox.showerror("Unable to read trace", str(exc))
            return
        measured = [item for item in outcomes if item.return_percent is not None]
        winners = sum(item.return_percent > 0 for item in measured)
        average = (
            sum((item.return_percent for item in measured), Decimal("0"))
            / len(measured)
            if measured
            else None
        )
        self.status_var.set(
            f"{strategy}: {len(outcomes)} output(s), {len(measured)} complete "
            f"10-minute outcome(s), winners {winners}/{len(measured)}, "
            f"average return {_fmt(average)}%"
        )
        for item in outcomes:
            self.table.insert(
                "",
                tk.END,
                values=(
                    item.captured_at.strftime("%Y-%m-%d %H:%M:%S"),
                    item.strategy,
                    item.status,
                    item.side,
                    item.symbol or "-",
                    _fmt(item.entry_ask),
                    _fmt(item.horizon_bid),
                    _fmt(item.return_percent),
                    _fmt(item.maximum_gain_percent),
                    _fmt(item.maximum_drawdown_percent),
                    "PASS" if item.qualified else "FAIL",
                    item.reason,
                ),
            )


def _fmt(value: Decimal | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze strategy trace outcomes.")
    parser.add_argument("source", nargs="?", type=Path, default=DEFAULT_SOURCE)
    args = parser.parse_args()
    StrategyLogAnalyzer(args.source).mainloop()


if __name__ == "__main__":
    main()

