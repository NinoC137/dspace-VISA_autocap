"""Windows layout smoke check; no buttons that access instruments are invoked."""
import sys
import tkinter as tk
from pathlib import Path

from ui import Panel


def check_layout(widget, root):
    if widget.winfo_ismapped():
        left = widget.winfo_rootx() - root.winfo_rootx()
        top = widget.winfo_rooty() - root.winfo_rooty()
        if left < 0 or top < 0 or left+widget.winfo_width() > root.winfo_width()+1 or top+widget.winfo_height() > root.winfo_height()+1:
            raise AssertionError("Widget outside window: " + str(widget))
    for child in widget.winfo_children():
        check_layout(child, root)


if __name__ == "__main__":
    root = tk.Tk()
    try:
        panel = Panel(root)
        for size in ("900x760", "820x660"):
            root.geometry(size)
            root.update()
            check_layout(root, root)
        assert len(panel.channel_vars) == 8
        assert len(panel.tests) >= 2
        assert len(panel.selected_tests()) == 1
        panel.select_all_tests()
        assert len(panel.selected_tests()) == len(panel.tests)
        panel.test_list.selection_clear(1, "end")
        panel.update_selection()
        assert panel.channel_vars["CH1"].get() == panel.config["channels"]["CH1"]["enabled"]
        panel.channel_vars["CH1"].set(False)
        panel.channel_vars["CH8"].set(True)
        assert not panel.channel_vars["CH1"].get() and panel.channel_vars["CH8"].get()
        panel.run_kind = "Run"
        panel.busy(True)
        root.update()
        assert "disabled" in panel.start.state()
        assert "disabled" not in panel.stop.state()
        panel.busy(False)
        panel.channel_vars["CH1"].set(True)
        panel.channel_vars["CH8"].set(False)
        root.geometry("900x760")
        root.update()
        if len(sys.argv) > 1:
            from PIL import ImageGrab
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
            ImageGrab.grab(window=hwnd).save(Path(sys.argv[1]))
        print("PASS: panel layout at 900x760 and 820x660; multi-select cases; eight toggles; busy/stop states.")
    finally:
        root.destroy()
