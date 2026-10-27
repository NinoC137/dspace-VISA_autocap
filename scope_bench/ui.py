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
PROFILES_PATH = HERE / "instrument_profiles.json"
# GUI launches with pythonw; child tools need python.exe so stdout reaches the log.
PYTHON = str(Path(sys.executable).with_name("python.exe")) if os.name == "nt" else sys.executable


def configured_profile(config, profiles):
    """Use the installed connection, not the profile table's display pointer."""
    instrument = config["instrument"]
    for key, profile in profiles["profiles"].items():
        target = profile["instrument"]
        if all(instrument.get(field) == target.get(field)
               for field in ("transport", "resource", "expected_model", "expected_serial")):
            return key
    return None


class Panel:
    def __init__(self, root):
        self.root = root
        self.config = load_config(HERE / "bench_config.json")
        self.profiles = json.loads(PROFILES_PATH.read_text(encoding="utf-8-sig"))
        self.profile_names = list(self.profiles["profiles"])
        self.profile_labels = {name: self.profiles["profiles"][name]["label"]
                               for name in self.profile_names}
        self.active_profile = configured_profile(self.config, self.profiles)
        self.process = None
        self.messages = queue.Queue()
        self.closing = False
        self.run_kind = None
        self.stop_file = None
        self._saved_regular_controls = None
        root.title("示波器波形测试台架")
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
        header = ttk.Frame(frame)
        header.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 18))
        ttk.Label(header, text="示波器 / ControlDesk", font=("Microsoft YaHei UI", 17, "bold")).pack(side="left")
        self.switch_button = ttk.Button(header, text="切换示波器", command=self.switch_instrument)
        self.switch_button.pack(side="right")
        self.profile_choice = ttk.Combobox(header, state="readonly", width=32,
                                           values=[self.profile_labels[name] for name in self.profile_names])
        self.profile_choice.pack(side="right", padx=(8, 8))

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
        self.screenshot_box = ttk.Checkbutton(shots_frame, text="保存截图", variable=self.screenshot_var)
        self.screenshot_box.pack(side="left")
        self.spans_label = ttk.Label(shots_frame, text="整图跨度（约10格）")
        self.spans_label.pack(side="left", padx=(14, 8))
        self.spans_var = tk.StringVar(value=", ".join(time_label(v) for v in self.config["screenshots"]["spans_s"]))
        self.spans_input = ttk.Entry(shots_frame, textvariable=self.spans_var, width=34)
        self.spans_input.pack(side="left", fill="x", expand=True)
        ttk.Label(frame, text="结果目录").grid(row=5, column=0, sticky="w", padx=(0, 12))
        default_output = Path(self.config["instrument"]["remote"]["local_share_root"]) / "sweeps"
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
        self.tek_files_button = ttk.Button(buttons, text="拉取 Tek .tss", command=self.pull_tek_files)
        for button in (self.check, self.start, self.stop, self.results, self.analyze,
                       self.tek_files_button):
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
        self.status = tk.StringVar(value="就绪")
        ttk.Label(frame, textvariable=self.status).grid(row=8, column=0, columnspan=3, sticky="w", pady=(12, 0))
        self.refresh_instrument()
        self.update_selection()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)

    def refresh_instrument(self):
        self.config = load_config(HERE / "bench_config.json")
        self.active_profile = configured_profile(self.config, self.profiles)
        if self.active_profile:
            self.profile_choice.current(self.profile_names.index(self.active_profile))
            name = self.profile_labels[self.active_profile]
        else:
            self.profile_choice.set("")
            name = "自定义仪器配置"
        self.root.title(name + " · 波形测试台架")
        is_tek = self.config["instrument"].get("transport") == "tek_socket"
        self.tek_files_button.configure(state="normal" if is_tek and self.process is None else "disabled")
        self.screenshot_box.configure(text="保存屏幕截图" if is_tek else "保存多时基截图")
        self.spans_label.configure(text="Tek 仅保存一张屏幕图" if is_tek else "整图跨度（约10格）")
        self.spans_input.configure(state="disabled" if is_tek else "normal")
        self.status.set(name + " · 就绪")

    def switch_instrument(self):
        index = self.profile_choice.current()
        if index < 0:
            messagebox.showerror("无法切换", "请选择一台示波器")
            return
        selected = self.profile_names[index]
        self.append("\n正在同步 " + self.profile_labels[selected] + "…\n")
        try:
            self.execute([PYTHON, str(HERE / "switch_scope.py"), selected], "Switch")
        except Exception as exc:
            messagebox.showerror("无法切换", str(exc))

    def selected_tests(self):
        return [self.tests[index] for index in self.test_list.curselection()]

    def update_selection(self):
        selected = self.selected_tests()
        count = sum(int(item["expected_cases"]) for item in selected)
        setups = sorted({item["manual_setup"] for item in selected})
        setup_text = " / ".join(setups) if setups else "未选择"
        dedicated = selected[0] if len(selected) == 1 and selected[0].get("runner") else None
        suffix = " · 固定15 s/8通道/截图" if dedicated else (
            " · 专用用例须单独选择" if any(item.get("runner") for item in selected) else "")
        self.case_label.configure(text=f"{len(selected)} 个用例 · {count} 个工况 · {setup_text}{suffix}")
        if not hasattr(self, "channel_vars"):
            return
        if dedicated:
            if self._saved_regular_controls is None:
                self._saved_regular_controls = (self.duration.get(), self.screenshot_var.get(),
                                                {name: var.get() for name, var in self.channel_vars.items()})
            fixed = load_config(PROJECT_ROOT / dedicated["scope_config"])
            self.duration.set(str(fixed["timing"]["enable_duration_s"]))
            self.screenshot_var.set(fixed["screenshots"]["enabled"])
            for name, var in self.channel_vars.items():
                var.set(fixed["channels"][name]["enabled"])
        elif self._saved_regular_controls is not None:
            duration, screenshots, channels = self._saved_regular_controls
            self.duration.set(duration)
            self.screenshot_var.set(screenshots)
            for name, var in self.channel_vars.items():
                var.set(channels[name])
            self._saved_regular_controls = None
        state = "disabled" if dedicated else "normal"
        self.duration_input.configure(state=state)
        self.screenshot_box.configure(state=state)
        for box in self.channel_boxes:
            box.configure(state=state)
        self.spans_input.configure(state="disabled" if dedicated or
                                   self.config["instrument"].get("transport") == "tek_socket" else "normal")

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

    def pull_tek_files(self):
        if self.process is not None:
            return
        installed = load_config(HERE / "bench_config.json")
        if (installed["instrument"].get("transport") != "tek_socket" or
                installed["instrument"].get("expected_model") != "MSO58B" or
                installed["instrument"].get("expected_serial") != "C070122"):
            messagebox.showerror("无法拉取", "请先切换至 MSO58B C070122")
            return
        approved = messagebox.askyesno(
            "拉取 Tek 内部文件",
            "将递归检查 MSO58B C070122 的 C:/zys/*.tss；本地已有、路径和大小匹配的"
            "完整文件将被复用，新增文件才复制到 "
            "F:\\ZYS_HYBASE\\automation\\Tek-scope 的新批次目录。原文件保留。\n\n"
            "请确保当前没有台架采集或截图。传输中断可能卡住 VXI-11，届时需停止使用并由操作者"
            "重新应用 LAN 设置或重启示波器。确认开始？")
        if approved:
            self.append("\n开始增量拉取 Tek 内部 .tss；每个新增文件完成后立即搬运。\n")
            self.execute([PYTHON, str(HERE / "tek_tss_transfer.py")], "TekFiles")

    def append(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def busy(self, value):
        for widget in [self.check, self.start, self.analyze, self.browse, self.output_input, self.duration_input,
                       self.switch_button, self.profile_choice,
                       self.screenshot_box, self.spans_input, self.test_list, self.select_all, self.clear_all,
                       self.tek_files_button,
                       *self.channel_boxes]:
            widget.configure(state="disabled" if value else "normal")
        if not value:
            self.profile_choice.configure(state="readonly")
            self.update_selection()
            self.tek_files_button.configure(state="normal" if self.config["instrument"].get("transport") == "tek_socket"
                                            else "disabled")
        self.stop.configure(state="normal" if value and self.run_kind == "Run" else "disabled")

    def launch(self, mode):
        try:
            selected = self.selected_tests()
            if not selected:
                raise ValueError("至少选择一个测试用例")
            dedicated = selected[0] if len(selected) == 1 and selected[0].get("runner") else None
            if any(item.get("runner") for item in selected) and dedicated is None:
                raise ValueError("ξ 切换或固定 ξ 用例须单独选择")
            setups = {item["manual_setup"] for item in selected}
            if mode == "Run" and len(setups) != 1:
                raise ValueError("一次自动批测只能包含相同人工电源设置的用例")
            # Another panel or deployment may have updated the connection since
            # this window opened. Overlay only the controls the operator edits.
            installed_cfg = load_config(HERE / "bench_config.json")
            selected_index = self.profile_choice.current()
            if selected_index < 0:
                raise ValueError("请先选择并切换示波器")
            if configured_profile(installed_cfg, self.profiles) != self.profile_names[selected_index]:
                self.refresh_instrument()
                raise ValueError("示波器配置已变化；请确认窗口中的仪器后重新启动")
            if dedicated:
                cfg = load_config(PROJECT_ROOT / dedicated["scope_config"])
                if configured_profile(cfg, self.profiles) != self.profile_names[selected_index]:
                    raise ValueError("ξ 测试用例要求 Tek 示波器；请先切换仪器")
            else:
                cfg = installed_cfg
                cfg["timing"]["enable_duration_s"] = float(self.duration.get())
                cfg["screenshots"]["enabled"] = self.screenshot_var.get()
                if self.screenshot_var.get():
                    cfg["screenshots"]["spans_s"] = parse_spans(self.spans_var.get())
                for name, var in self.channel_vars.items():
                    cfg["channels"][name]["enabled"] = var.get()
            validate(cfg)
            if not dedicated:
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
            self.append(f"\n{mode}: {names}, 通道={','.join(selected_channels(cfg))}, "
                        f"{cfg['timing']['enable_duration_s']} s"
                        f"{'，ξ 测试专用配置' if dedicated else ''}\n")
            self.execute(args, mode)
        except Exception as exc:
            messagebox.showerror("无法启动", str(exc))

    def execute(self, args, kind):
        self.run_kind = kind
        env = dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1")
        self.process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.busy(True)
        self.status.set("正在检查" if kind == "Check" else "正在切换" if kind == "Switch" else
                        "正在拉取 Tek 文件" if kind == "TekFiles" else "运行中")
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
                kind = self.run_kind
                self.process.stdout.close()
                self.process = None
                if kind == "Switch":
                    try:
                        self.refresh_instrument()
                    except Exception as exc:
                        self.append("切换后读取配置失败：" + str(exc) + "\n")
                self.busy(False)
                if kind == "Switch":
                    self.status.set("切换完成；运行前请检查连接" if message == 0
                                    else "切换未完全成功，请查看日志")
                elif kind == "TekFiles":
                    self.status.set("Tek 文件拉取完成" if message == 0 else "Tek 文件拉取失败，请查看日志")
                else:
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
