"""Single-window Windows operator panel; instrument access runs in child processes."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import uuid

from common import atomic_json, load_config, selected_channels, validate, parse_spans, time_label

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
CATALOG_PATH = PROJECT_ROOT / "test_cases" / "catalog.json"
# GUI launches with pythonw; child tools need python.exe so stdout reaches the log.
PYTHON = str(Path(sys.executable).with_name("python.exe")) if os.name == "nt" else sys.executable


class Panel:
    def __init__(self, root):
        self.root = root
        self.config = load_config(HERE / "bench_config.json")
        self.process = None
        self.messages = queue.Queue()
        self.closing = False
        self.run_kind = None
        self.stop_file = None
        remote_mode = self.config["instrument"].get("transport") == "raspi_ssh"
        root.title("MXO38 波形测试台架" + (" · 树莓派 USB" if remote_mode else ""))
        root.geometry("900x760")
        root.minsize(820, 660)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("TButton", padding=(10, 7))
        frame = ttk.Frame(root, padding=20)
        frame.grid(sticky="nsew")
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(7, weight=1)
        ttk.Label(frame, text="MXO38 / ControlDesk", font=("Microsoft YaHei UI", 17, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 18))

        with CATALOG_PATH.open(encoding="utf-8") as stream:
            catalog = json.load(stream)
        if catalog.get("schema_version") != 1:
            raise ValueError("不支持的测试用例目录版本")
        self.tests = [item for item in catalog["tests"] if item.get("enabled", False)]
        if not self.tests:
            raise ValueError("测试用例目录中没有启用的用例")

        case_frame = ttk.LabelFrame(frame, text="测试用例", padding=10)
        case_frame.grid(row=1, column=0, columnspan=3, sticky="nsew")
        case_frame.columnconfigure(0, weight=1)
        self.test_list = tk.Listbox(case_frame, selectmode="extended", exportselection=False,
                                    height=min(6, max(3, len(self.tests))), font=("Microsoft YaHei UI", 10))
        self.test_list.grid(row=0, column=0, rowspan=2, sticky="nsew")
        for item in self.tests:
            self.test_list.insert("end", f"{item['name']}  [{item['expected_cases']} 工况 / {item['manual_setup']}]")
        self.test_list.selection_set(0)
        self.test_list.bind("<<ListboxSelect>>", lambda _event: self.update_selection())
        scroll_cases = ttk.Scrollbar(case_frame, command=self.test_list.yview)
        scroll_cases.grid(row=0, column=1, rowspan=2, sticky="ns")
        self.test_list.configure(yscrollcommand=scroll_cases.set)
        self.select_all = ttk.Button(case_frame, text="全选", command=self.select_all_tests)
        self.select_all.grid(row=0, column=2, padx=(10, 0), sticky="ew")
        self.clear_all = ttk.Button(case_frame, text="清空", command=self.clear_tests)
        self.clear_all.grid(row=1, column=2, padx=(10, 0), sticky="new")

        options = ttk.Frame(frame)
        options.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        ttk.Label(options, text="每工况使能 (s)").pack(side="left")
        self.duration = tk.StringVar(value=str(self.config["timing"]["enable_duration_s"]))
        self.duration_input = ttk.Spinbox(options, from_=10, to=15, increment=1, textvariable=self.duration, width=6)
        self.duration_input.pack(side="left", padx=8)
        self.case_label = ttk.Label(options)
        self.case_label.pack(side="right")
        self.update_selection()
        channel_frame = ttk.Frame(frame)
        channel_frame.grid(row=3, column=0, columnspan=3, sticky="ew", pady=14)
        ttk.Label(channel_frame, text="采集通道").pack(side="left", padx=(0, 12))
        self.channel_vars = {}
        self.channel_boxes = []
        for name, item in self.config["channels"].items():
            var = tk.BooleanVar(value=item["enabled"])
            self.channel_vars[name] = var
            box = ttk.Checkbutton(channel_frame, text=name, variable=var)
            box.pack(side="left", padx=6)
            self.channel_boxes.append(box)
        # One compact second row, without exposing instrument/transport internals.
        shots_frame = ttk.Frame(frame)
        shots_frame.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(0, 12))
        self.screenshot_var = tk.BooleanVar(value=self.config["screenshots"]["enabled"])
        self.screenshot_box = ttk.Checkbutton(shots_frame, text="保存多时基截图", variable=self.screenshot_var)
        self.screenshot_box.pack(side="left")
        ttk.Label(shots_frame, text="整图跨度（约10格）").pack(side="left", padx=(14, 8))
        self.spans_var = tk.StringVar(value=", ".join(time_label(v) for v in self.config["screenshots"]["spans_s"]))
        self.spans_input = ttk.Entry(shots_frame, textvariable=self.spans_var, width=34)
        self.spans_input.pack(side="left", fill="x", expand=True)
        ttk.Label(frame, text="结果目录").grid(row=5, column=0, sticky="w", padx=(0, 12))
        default_output = (Path(self.config["instrument"]["remote"]["local_share_root"]) / "sweeps"
                          if remote_mode else HERE.parent / "sweep_results")
        self.output = tk.StringVar(value=str(default_output))
        self.output_input = ttk.Entry(frame, textvariable=self.output)
        self.output_input.grid(row=5, column=1, sticky="ew")
        self.browse = ttk.Button(frame, text="选择目录", command=self.choose_output)
        self.browse.grid(row=5, column=2, padx=(8, 0))
        buttons = ttk.Frame(frame)
        buttons.grid(row=6, column=0, columnspan=3, sticky="ew", pady=18)
        self.check = ttk.Button(buttons, text="检查连接", command=lambda: self.launch("Check"))
        self.start = ttk.Button(buttons, text="开始批测", command=lambda: self.launch("Run"))
        self.stop = ttk.Button(buttons, text="停止", command=self.request_stop, state="disabled")
        self.results = ttk.Button(buttons, text="打开结果", command=self.open_results)
        self.analyze = ttk.Button(buttons, text="重算已有波形", command=self.reanalyze)
        for button in (self.check, self.start, self.stop, self.results, self.analyze):
            button.pack(side="left", padx=(0, 8))
        log_frame = ttk.Frame(frame)
        log_frame.grid(row=7, column=0, columnspan=3, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log = tk.Text(log_frame, wrap="word", font=("Consolas", 10), height=15, state="disabled")
        self.log.grid(sticky="nsew")
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scroll.set)
        self.status = tk.StringVar(value="树莓派 USB · 结果保存到 PC · 就绪" if remote_mode else "就绪")
        ttk.Label(frame, textvariable=self.status).grid(row=8, column=0, columnspan=3, sticky="w", pady=(12, 0))
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)

    def selected_tests(self):
        return [self.tests[index] for index in self.test_list.curselection()]

    def update_selection(self):
        selected = self.selected_tests()
        count = sum(int(item["expected_cases"]) for item in selected)
        setups = sorted({item["manual_setup"] for item in selected})
        setup_text = " / ".join(setups) if setups else "未选择"
        self.case_label.configure(text=f"{len(selected)} 个用例 · {count} 个工况 · {setup_text}")

    def select_all_tests(self):
        self.test_list.selection_set(0, "end")
        self.update_selection()

    def clear_tests(self):
        self.test_list.selection_clear(0, "end")
        self.update_selection()

    def choose_output(self):
        path = filedialog.askdirectory(initialdir=self.output.get())
        if path:
            self.output.set(path)

    def open_results(self):
        path = Path(self.output.get()).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)

    def append(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def busy(self, value):
        for widget in [self.check, self.start, self.analyze, self.browse, self.output_input, self.duration_input,
                       self.screenshot_box, self.spans_input, self.test_list, self.select_all, self.clear_all,
                       *self.channel_boxes]:
            widget.configure(state="disabled" if value else "normal")
        self.stop.configure(state="normal" if value and self.run_kind == "Run" else "disabled")

    def launch(self, mode):
        try:
            selected = self.selected_tests()
            if not selected:
                raise ValueError("至少选择一个测试用例")
            setups = {item["manual_setup"] for item in selected}
            if mode == "Run" and len(setups) != 1:
                raise ValueError("一次自动批测只能包含相同人工电源设置的用例")
            # Another panel or deployment may have updated the connection since
            # this window opened. Overlay only the controls the operator edits.
            cfg = load_config(HERE / "bench_config.json")
            cfg["timing"]["enable_duration_s"] = float(self.duration.get())
            cfg["screenshots"]["enabled"] = self.screenshot_var.get()
            if self.screenshot_var.get():
                cfg["screenshots"]["spans_s"] = parse_spans(self.spans_var.get())
            for name, var in self.channel_vars.items():
                cfg["channels"][name]["enabled"] = var.get()
            validate(cfg)
            atomic_json(HERE / "bench_config.json", cfg)
            self.config = cfg
            session = HERE / "sessions" / uuid.uuid4().hex
            session.mkdir(parents=True)
            config_path = session / "config.json"
            atomic_json(config_path, cfg)
            self.stop_file = session / "stop.json"
            args = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    str(PROJECT_ROOT / "core" / "run_test_batch.ps1"), "-Mode", mode,
                    "-TestIds", ",".join(item["id"] for item in selected),
                    "-SessionScopeConfig", str(config_path),
                    "-PythonExecutable", PYTHON, "-StopFile", str(self.stop_file),
                    "-OutputDirectory", str(Path(self.output.get()).expanduser().resolve())]
            names = ", ".join(item["id"] for item in selected)
            self.append(f"\n{mode}: {names}, 通道={','.join(selected_channels(cfg))}, {self.duration.get()} s\n")
            self.execute(args, mode)
        except Exception as exc:
            messagebox.showerror("无法启动", str(exc))

    def execute(self, args, kind):
        self.run_kind = kind
        env = dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1")
        self.process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.busy(True)
        self.status.set("正在检查" if kind == "Check" else "运行中")
        def read():
            for line in iter(self.process.stdout.readline, b""):
                self.messages.put(line.decode("utf-8", errors="replace"))
            code = self.process.wait()
            self.messages.put(code)
        threading.Thread(target=read, daemon=True).start()

    def reanalyze(self):
        path = filedialog.askopenfilename(filetypes=[("HDF5 waveforms", "*.h5")])
        if path:
            try:
                self.execute([PYTHON, str(HERE / "bench.py"), "analyze", "--file", path], "Analyze")
            except Exception as exc:
                messagebox.showerror("无法分析", str(exc))

    def request_stop(self):
        if self.process is not None and self.run_kind == "Run":
            atomic_json(self.stop_file, {"stop": True})
            self.stop.configure(state="disabled")
            # A frozen waveform that is being exported cannot be aborted without
            # losing it, so state the real boundary the stop will take effect at.
            self.status.set("停止请求已发送，将在本工况采集与保存完成后停止")

    def close(self):
        if self.process is not None:
            self.closing = True
            self.request_stop()
            self.status.set("等待当前操作结束后关闭")
        else:
            self.root.destroy()

    def poll(self):
        while not self.messages.empty():
            message = self.messages.get_nowait()
            if isinstance(message, int):
                self.process.stdout.close()
                self.process = None
                self.busy(False)
                self.status.set("完成" if message == 0 else "已停止或失败，请查看日志")
                if self.closing:
                    self.root.destroy()
                    return
            else:
                self.append(message)
        self.root.after(100, self.poll)


if __name__ == "__main__":
    root = tk.Tk()
    try:
        Panel(root)
        root.mainloop()
    except Exception as exc:
        messagebox.showerror("台架启动失败", str(exc))
        root.destroy()
        raise
