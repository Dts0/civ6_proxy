"""Tkinter desktop client for the Civ 6 UDP relay."""

from __future__ import annotations

import ipaddress
import os
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .client import RelayClient, RelayClientError
from .config import ClientConfig, load_config, parse_relay_address, save_config
from .divert import WinDivertBridge
from .windows import (
    find_civ6_processes,
    is_admin,
    relaunch_as_admin,
    route_to,
    select_virtual_subnet,
    virtual_member_ip,
)

ERROR_MESSAGES = {
    "room_not_found": "房间不存在或已经关闭",
    "room_full": "房间已满（最多 8 人）",
    "host_offline": "房主暂时离线",
    "timeout": "中转服务器没有响应",
    "resolve_failed": "无法解析中转服务器地址",
}


class Civ6RelayApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Civ 6 UDP Relay")
        self.geometry("860x680")
        self.minsize(760, 600)
        self.configure(bg="#f3f4f6")

        config = load_config()
        self.relay_var = tk.StringVar(value=f"{config.relay_host}:{config.relay_port}")
        self.name_var = tk.StringVar(value=config.display_name)
        self.room_var = tk.StringVar()
        self.status_var = tk.StringVar(value="未连接")
        self.civ_var = tk.StringVar(value="Civ 6：未运行")
        self.route_var = tk.StringVar(value="虚拟地址：-")
        self.rtt_var = tk.StringVar(value="RTT：-")
        self.traffic_var = tk.StringVar(value="收发：0 / 0")

        self.client: RelayClient | None = None
        self.bridge: WinDivertBridge | None = None
        self.virtual_subnet: ipaddress.IPv4Network | None = None
        self.events: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=2048)
        self.log_lines: list[str] = []
        self._closing = False
        self._busy = False

        self._configure_style()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._drain_events)
        self.after(500, self._refresh_metrics)
        self.after(250, self._refresh_process_status)
        self._log("info", "客户端已就绪")

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Root.TFrame", background="#f3f4f6")
        style.configure("Band.TFrame", background="#ffffff")
        style.configure("Title.TLabel", background="#f3f4f6", foreground="#111827", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("Muted.TLabel", background="#f3f4f6", foreground="#6b7280", font=("Microsoft YaHei UI", 9))
        style.configure("Band.TLabel", background="#ffffff", foreground="#374151", font=("Microsoft YaHei UI", 9))
        style.configure("Status.TLabel", background="#ffffff", foreground="#0f766e", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("TButton", padding=(12, 7), font=("Microsoft YaHei UI", 9))
        style.configure("Accent.TButton", padding=(14, 8), font=("Microsoft YaHei UI", 9, "bold"))

    def _build_ui(self) -> None:
        root = ttk.Frame(self, style="Root.TFrame", padding=16)
        root.pack(fill="both", expand=True)
        ttk.Label(root, text="Civ 6 UDP Relay", style="Title.TLabel").pack(anchor="w")
        ttk.Label(root, text="Windows x64 · 公网 UDP 房间中转", style="Muted.TLabel").pack(anchor="w", pady=(2, 12))

        connection = ttk.Frame(root, style="Band.TFrame", padding=14)
        connection.pack(fill="x")
        connection.columnconfigure(1, weight=1)
        connection.columnconfigure(3, weight=1)

        ttk.Label(connection, text="中转地址", style="Band.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.relay_entry = ttk.Entry(connection, textvariable=self.relay_var)
        self.relay_entry.grid(row=0, column=1, sticky="ew", padx=(0, 14))
        ttk.Label(connection, text="昵称", style="Band.TLabel").grid(row=0, column=2, sticky="w", padx=(0, 8))
        self.name_entry = ttk.Entry(connection, textvariable=self.name_var)
        self.name_entry.grid(row=0, column=3, sticky="ew")

        ttk.Label(connection, text="房间码", style="Band.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(12, 0))
        self.room_entry = ttk.Entry(connection, textvariable=self.room_var)
        self.room_entry.grid(row=1, column=1, sticky="ew", padx=(0, 14), pady=(12, 0))
        actions = ttk.Frame(connection, style="Band.TFrame")
        actions.grid(row=1, column=2, columnspan=2, sticky="e", pady=(12, 0))
        self.create_button = ttk.Button(actions, text="创建房间", style="Accent.TButton", command=lambda: self._connect("create"))
        self.create_button.pack(side="left", padx=(0, 8))
        self.join_button = ttk.Button(actions, text="加入房间", command=lambda: self._connect("join"))
        self.join_button.pack(side="left", padx=(0, 8))
        self.copy_button = ttk.Button(actions, text="复制房间码", command=self._copy_room_code, state="disabled")
        self.copy_button.pack(side="left", padx=(0, 8))
        self.disconnect_button = ttk.Button(actions, text="断开", command=self._disconnect, state="disabled")
        self.disconnect_button.pack(side="left")

        status = ttk.Frame(root, style="Band.TFrame", padding=(14, 10))
        status.pack(fill="x", pady=(10, 0))
        ttk.Label(status, textvariable=self.status_var, style="Status.TLabel").pack(side="left")
        ttk.Label(status, textvariable=self.civ_var, style="Band.TLabel").pack(side="left", padx=(22, 0))
        ttk.Label(status, textvariable=self.route_var, style="Band.TLabel").pack(side="left", padx=(22, 0))
        ttk.Label(status, textvariable=self.traffic_var, style="Band.TLabel").pack(side="right")
        ttk.Label(status, textvariable=self.rtt_var, style="Band.TLabel").pack(side="right", padx=(0, 22))

        panes = ttk.Panedwindow(root, orient="vertical")
        panes.pack(fill="both", expand=True, pady=(10, 0))

        members_frame = ttk.Frame(panes, style="Band.TFrame", padding=10)
        members_frame.rowconfigure(1, weight=1)
        members_frame.columnconfigure(0, weight=1)
        ttk.Label(members_frame, text="房间成员", style="Band.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.members = ttk.Treeview(members_frame, columns=("id", "name", "role", "status", "virtual"), show="headings", height=7)
        for column, title, width in (
            ("id", "ID", 50),
            ("name", "昵称", 180),
            ("role", "角色", 80),
            ("status", "状态", 80),
            ("virtual", "虚拟地址", 170),
        ):
            self.members.heading(column, text=title)
            self.members.column(column, width=width, anchor="center" if column != "name" else "w")
        self.members.grid(row=1, column=0, sticky="nsew")
        panes.add(members_frame, weight=1)

        log_frame = ttk.Frame(panes, style="Band.TFrame", padding=10)
        log_frame.rowconfigure(1, weight=1)
        log_frame.columnconfigure(0, weight=1)
        log_top = ttk.Frame(log_frame, style="Band.TFrame")
        log_top.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(log_top, text="运行日志", style="Band.TLabel").pack(side="left")
        ttk.Button(log_top, text="导出", command=self._export_log).pack(side="right")
        self.log_text = tk.Text(
            log_frame,
            height=12,
            bg="#111827",
            fg="#d1d5db",
            insertbackground="#d1d5db",
            relief="flat",
            font=("Consolas", 9),
            padx=10,
            pady=8,
            state="disabled",
        )
        self.log_text.tag_configure("info", foreground="#d1d5db")
        self.log_text.tag_configure("ok", foreground="#86efac")
        self.log_text.tag_configure("warn", foreground="#fde68a")
        self.log_text.tag_configure("error", foreground="#fca5a5")
        self.log_text.grid(row=1, column=0, sticky="nsew")
        panes.add(log_frame, weight=2)

    def _connect(self, mode: str) -> None:
        if self._busy or (self.client and self.client.connected):
            return
        try:
            host, port = parse_relay_address(self.relay_var.get())
            name = self.name_var.get().strip()
            if not (1 <= len(name) <= 24):
                raise ValueError("昵称长度必须为 1 到 24 个字符")
            room_code = self.room_var.get().replace("-", "").strip().upper()
            if mode == "join" and len(room_code) != 10:
                raise ValueError("请输入 10 位房间码")
        except ValueError as exc:
            messagebox.showerror("参数错误", str(exc), parent=self)
            return
        save_config(ClientConfig(host, port, name))
        self._set_busy(True, "正在连接")
        threading.Thread(
            target=self._connect_worker,
            args=(mode, host, port, name, room_code),
            name="client-connect",
            daemon=True,
        ).start()

    def _connect_worker(self, mode: str, host: str, port: int, name: str, room_code: str) -> None:
        client = RelayClient(host, port, event_callback=self._relay_event)
        self.client = client
        bridge = None
        try:
            response = client.create_room(name) if mode == "create" else client.join_room(room_code, name)
            relay_endpoint = client.relay_endpoint
            if relay_endpoint is None:
                raise RelayClientError("中转服务器地址不可用")
            local_ip, interface_index, subinterface_index = route_to(relay_endpoint[0])
            subnet = select_virtual_subnet()
            bridge = WinDivertBridge(
                client,
                subnet,
                local_ip,
                interface_index,
                subinterface_index,
                log_callback=lambda level, message: self._post_event("log", (level, message)),
            )
            client.data_callback = bridge.inject_game_datagram
            bridge.start()
            self.bridge = bridge
            self.virtual_subnet = subnet
            self._post_event("connected", {"response": response, "local_ip": local_ip, "subnet": str(subnet)})
        except Exception as exc:
            if bridge:
                bridge.stop()
            client.disconnect(notify_server=True)
            self.client = None
            self._post_event("connect_error", exc)

    def _disconnect(self) -> None:
        if self._busy:
            return
        self._set_busy(True, "正在断开")
        threading.Thread(target=self._disconnect_worker, name="client-disconnect", daemon=True).start()

    def _disconnect_worker(self) -> None:
        bridge, client = self.bridge, self.client
        self.bridge = None
        self.client = None
        self.virtual_subnet = None
        if bridge:
            bridge.stop()
        if client:
            client.disconnect(notify_server=True)
        self._post_event("disconnected", {})

    def _relay_event(self, event: str, data: dict) -> None:
        self._post_event("relay", (event, data))

    def _post_event(self, kind: str, value: object) -> None:
        try:
            self.events.put_nowait((kind, value))
        except queue.Full:
            try:
                self.events.get_nowait()
                self.events.put_nowait((kind, value))
            except (queue.Empty, queue.Full):
                pass

    def _drain_events(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "connected":
                    data = value
                    self.room_var.set(data["response"]["room_code"])
                    self.route_var.set(f"虚拟网段：{data['subnet']}")
                    self.status_var.set("中继运行中")
                    self._set_busy(False)
                    self._set_connected_controls(True)
                    self._refresh_members(data["response"].get("members", []))
                    self._log("ok", f"已加入房间 {data['response']['room_code']}，本地出口 {data['local_ip']}")
                elif kind == "connect_error":
                    exc = value
                    code = getattr(exc, "code", "")
                    message = ERROR_MESSAGES.get(code, str(exc))
                    self.status_var.set("连接失败")
                    self._set_busy(False)
                    self._set_connected_controls(False)
                    self._log("error", message)
                    messagebox.showerror("连接失败", message, parent=self)
                elif kind == "disconnected":
                    self.status_var.set("未连接")
                    self.route_var.set("虚拟地址：-")
                    self.rtt_var.set("RTT：-")
                    self._set_busy(False)
                    self._set_connected_controls(False)
                    self._refresh_members([])
                    self._log("info", "已断开中转")
                elif kind == "relay":
                    event, data = value
                    self._handle_relay_event(event, data)
                elif kind == "log":
                    level, message = value
                    self._log(level, message)
        except queue.Empty:
            pass
        if not self._closing:
            self.after(100, self._drain_events)

    def _handle_relay_event(self, event: str, data: dict) -> None:
        if event == "peers":
            self._refresh_members(data.get("members", []))
        elif event == "room_closed":
            self.status_var.set("房间已关闭")
            self._log("warn", f"房间已关闭：{data.get('reason', 'unknown')}")
            if not self._busy:
                self._disconnect()
        elif event == "relay_silent":
            self.status_var.set("中转响应超时")
            self._log("warn", f"中转已 {data.get('seconds')} 秒无响应")
        elif event == "transport_error":
            self._log("error", str(data.get("message", "网络错误")))

    def _refresh_members(self, peers: list[dict]) -> None:
        self.members.delete(*self.members.get_children())
        subnet = self.virtual_subnet
        for peer in peers:
            member_id = int(peer.get("id", 0))
            try:
                virtual = virtual_member_ip(subnet, member_id) if subnet else "-"
            except ValueError:
                virtual = "-"
            self.members.insert(
                "",
                "end",
                values=(
                    member_id,
                    peer.get("name", ""),
                    "房主" if peer.get("host") else "成员",
                    "在线" if peer.get("online") else "离线",
                    virtual,
                ),
            )

    def _refresh_metrics(self) -> None:
        client = self.client
        bridge = self.bridge
        if client:
            metrics = client.metrics_snapshot()
            self.rtt_var.set("RTT：-" if metrics["rtt_ms"] is None else f"RTT：{metrics['rtt_ms']:.0f} ms")
            self.traffic_var.set(f"收发：{metrics['game_rx_datagrams']} / {metrics['game_tx_datagrams']}")
        if bridge and not bridge.running and client and client.connected:
            self.status_var.set("抓包驱动已停止")
        if not self._closing:
            self.after(500, self._refresh_metrics)

    def _refresh_process_status(self) -> None:
        processes = find_civ6_processes()
        if processes:
            self.civ_var.set("Civ 6：" + ", ".join(f"{name}({pid})" for pid, name in processes))
        else:
            self.civ_var.set("Civ 6：未运行")
        if not self._closing:
            self.after(2000, self._refresh_process_status)

    def _set_busy(self, busy: bool, status: str | None = None) -> None:
        self._busy = busy
        if status:
            self.status_var.set(status)
        if busy:
            for widget in (self.create_button, self.join_button, self.disconnect_button):
                widget.configure(state="disabled")
        else:
            self._set_connected_controls(bool(self.client and self.client.connected))

    def _set_connected_controls(self, connected: bool) -> None:
        normal = "disabled" if connected else "normal"
        self.create_button.configure(state=normal)
        self.join_button.configure(state=normal)
        self.relay_entry.configure(state=normal)
        self.name_entry.configure(state=normal)
        self.room_entry.configure(state=normal)
        self.disconnect_button.configure(state="normal" if connected else "disabled")
        self.copy_button.configure(state="normal" if connected and self.room_var.get() else "disabled")

    def _copy_room_code(self) -> None:
        code = self.room_var.get().strip()
        if code:
            self.clipboard_clear()
            self.clipboard_append(code)
            self._log("info", "房间码已复制")

    def _export_log(self) -> None:
        initial = f"civ6-relay-{time.strftime('%Y%m%d-%H%M%S')}.log"
        target = filedialog.asksaveasfilename(parent=self, initialfile=initial, defaultextension=".log")
        if target:
            Path(target).write_text("\n".join(self.log_lines) + "\n", encoding="utf-8")

    def _log(self, level: str, message: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} [{level.upper()}] {message}"
        self.log_lines.append(line)
        if len(self.log_lines) > 5000:
            self.log_lines = self.log_lines[-5000:]
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n", level if level in ("info", "ok", "warn", "error") else "info")
        if int(self.log_text.index("end-1c").split(".")[0]) > 2000:
            self.log_text.delete("1.0", "501.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _on_close(self) -> None:
        self._closing = True
        bridge, client = self.bridge, self.client
        if bridge:
            bridge.stop()
        if client:
            client.disconnect(notify_server=True)
        self.destroy()


def main() -> int:
    if os.name != "nt":
        raise SystemExit("Civ6RelayClient only supports Windows")
    if not is_admin():
        prompt = tk.Tk()
        prompt.withdraw()
        elevate = messagebox.askyesno("需要管理员权限", "Civ 6 UDP Relay 需要管理员权限加载网络驱动。现在重新启动吗？", parent=prompt)
        prompt.destroy()
        if elevate and relaunch_as_admin():
            return 0
        return 1
    app = Civ6RelayApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
