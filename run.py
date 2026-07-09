#!/usr/bin/env python3

from __future__ import annotations

import os
import socket
import subprocess
import sys
import webbrowser
from pathlib import Path
from tkinter import END, BooleanVar, IntVar, StringVar, TclError, messagebox
from tkinter import ttk

PROJECT_DIR = Path(__file__).resolve().parent
APP_SCRIPT = PROJECT_DIR / "app.py"


def lan_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return None


def stop_process(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


class ServerLauncher:
    def __init__(self) -> None:
        import tkinter as tk

        self.proc: subprocess.Popen | None = None
        self.tk = tk
        self.window = tk.Tk()
        self.window.title("Board Firmware Log")
        self.window.minsize(420, 380)
        self.window.protocol("WM_DELETE_WINDOW", self.on_close)

        self.lan_var = BooleanVar(value=False)
        self.debug_var = BooleanVar(value=False)
        self.port_var = IntVar(value=5000)
        self.status_var = StringVar(value="Server stopped.")

        self._build_ui()
        self.window.after(500, self.check_process)

    def _build_ui(self) -> None:
        pad = {"padx": 12, "pady": 6}
        frame = ttk.Frame(self.window, padding=12)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Board Firmware Log", font=("Segoe UI", 14, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", **pad
        )
        ttk.Checkbutton(
            frame,
            text="LAN Access",
            variable=self.lan_var,
            command=self.update_urls,
        ).grid(row=2, column=0, columnspan=2, sticky="w", **pad)

        port_row = ttk.Frame(frame)
        port_row.grid(row=3, column=0, columnspan=2, sticky="w", padx=12, pady=6)
        ttk.Label(port_row, text="Port").pack(side="left")
        ttk.Spinbox(
            port_row,
            from_=1024,
            to=65535,
            textvariable=self.port_var,
            width=8,
            command=self.update_urls,
        ).pack(side="left", padx=(8, 0))

        ttk.Checkbutton(
            frame,
            text="Debug Mode",
            variable=self.debug_var,
        ).grid(row=4, column=0, columnspan=2, sticky="w", **pad)

        btn_row = ttk.Frame(frame)
        btn_row.grid(row=5, column=0, columnspan=2, sticky="w", padx=12, pady=12)
        self.start_btn = ttk.Button(btn_row, text="Start server", command=self.start_server)
        self.start_btn.pack(side="left", padx=(0, 8))
        self.stop_btn = ttk.Button(btn_row, text="Stop server", command=self.stop_server, state="disabled")
        self.stop_btn.pack(side="left", padx=(0, 8))
        self.open_btn = ttk.Button(btn_row, text="Open in browser", command=self.open_browser, state="disabled")
        self.open_btn.pack(side="left")

        ttk.Label(frame, textvariable=self.status_var).grid(
            row=6, column=0, columnspan=2, sticky="w", **pad
        )

        ttk.Label(frame, text="Addresses").grid(row=7, column=0, columnspan=2, sticky="w", padx=12, pady=(12, 0))
        self.url_text = self.tk.Text(frame, height=6, width=48, wrap="word", state="disabled")
        self.url_text.grid(row=8, column=0, columnspan=2, sticky="we", padx=12, pady=6)

        frame.columnconfigure(1, weight=1)
        self.update_urls()

    def port(self) -> int:
        try:
            value = int(self.port_var.get())
        except (TclError, ValueError):
            value = 5000
            self.port_var.set(value)
        return max(1024, min(65535, value))

    def local_url(self) -> str:
        return f"http://127.0.0.1:{self.port()}"

    def set_url_text(self, lines: list[str]) -> None:
        self.url_text.configure(state="normal")
        self.url_text.delete("1.0", END)
        self.url_text.insert("1.0", "\n".join(lines))
        self.url_text.configure(state="disabled")

    def update_urls(self) -> None:
        lines = [f"This PC:  {self.local_url()}"]
        if self.lan_var.get():
            ip = lan_ip()
            host = socket.gethostname()
            if ip:
                lines.append(f"Network:  http://{ip}:{self.port()}")
            lines.append(f"Network:  http://{host}:{self.port()}")
            lines.append("")
            lines.append("Share one of the Network addresses with coworkers.")
            lines.append("Your PC must stay on and Windows may ask to allow Python through the firewall.")
        else:
            lines.append("")
            lines.append("LAN access is off.")
        if self.proc and self.proc.poll() is None:
            lines.insert(0, "Server is running.\n")
        self.set_url_text(lines)

    def server_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["FLASK_HOST"] = "0.0.0.0" if self.lan_var.get() else "127.0.0.1"
        env["FLASK_PORT"] = str(self.port())
        env["FLASK_DEBUG"] = "1" if self.debug_var.get() else "0"
        return env

    def start_server(self) -> None:
        if self.proc and self.proc.poll() is None:
            messagebox.showinfo("Already running", "The server is already running.")
            return
        if not APP_SCRIPT.is_file():
            messagebox.showerror("Missing app.py", f"Could not find:\n{APP_SCRIPT}")
            return

        self.proc = subprocess.Popen(
            [sys.executable, str(APP_SCRIPT)],
            cwd=str(PROJECT_DIR),
            env=self.server_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
        )
        self.status_var.set("Starting server…")
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.open_btn.configure(state="normal")
        self.lan_var.get()  # refresh
        self.window.after(800, self._confirm_started)

    def _confirm_started(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.status_var.set("Server running.")
            self.update_urls()
        else:
            self.status_var.set("Server failed to start.")
            self.proc = None
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            self.open_btn.configure(state="disabled")
            messagebox.showerror(
                "Start failed",
                "The server exited immediately. Check that port "
                f"{self.port()} is free and run py -3 -m pip install -r requirements.txt",
            )

    def stop_server(self) -> None:
        stop_process(self.proc)
        self.proc = None
        self.status_var.set("Server stopped.")
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.open_btn.configure(state="disabled")
        self.update_urls()

    def open_browser(self) -> None:
        webbrowser.open(self.local_url())

    def check_process(self) -> None:
        if self.proc and self.proc.poll() is not None:
            self.proc = None
            self.status_var.set("Server stopped unexpectedly.")
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            self.open_btn.configure(state="disabled")
            self.update_urls()
        self.window.after(1000, self.check_process)

    def on_close(self) -> None:
        if self.proc and self.proc.poll() is None:
            if not messagebox.askyesno("Quit", "Stop the server and close?"):
                return
            self.stop_server()
        self.window.destroy()

    def run(self) -> None:
        self.window.mainloop()


def main() -> None:
    if not APP_SCRIPT.is_file():
        print(f"Error: app.py not found in {PROJECT_DIR}", file=sys.stderr)
        sys.exit(1)
    ServerLauncher().run()


if __name__ == "__main__":
    main()
