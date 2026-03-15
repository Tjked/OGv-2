import torch
from ultralytics import YOLO
import pygame
import win32api, win32con, win32gui
import numpy as np
from mss import mss
import os
import threading
import tkinter as tk
from tkinter import ttk, colorchooser
import time
import ctypes
import math

# --- SYSTEM OPTIMIZATIONS ---
ctypes.windll.shcore.SetProcessDpiAwareness(1)
ctypes.windll.kernel32.SetPriorityClass(
    ctypes.windll.kernel32.GetCurrentProcess(), 0x00000100)

# ---------------------------------------------------------------------------
# HARDWARE-LEVEL MOUSE INPUT via Interception kernel driver
#
# Interception injects at Ring-0 (kernel driver layer), BELOW the Win32 input
# stack entirely. Games that use raw DirectInput or RawInput see this as a
# real physical mouse move — indistinguishable from actual hardware.
#
# Requirements (one-time setup):
#   1. Install the Interception driver:
#        https://github.com/oblitum/Interception
#        Run: install-interception.exe /install  (as Administrator, reboot)
#   2. pip install interception-python
#        https://github.com/cobrce/interception-python
#
# If the driver/package is NOT present we fall back to SendInput which still
# works in most games but can be filtered by anti-cheat.
# ---------------------------------------------------------------------------

_interception = None
_inter_device  = None

try:
    import interception as _ic

    _interception = _ic.Interception()
    # Find the first real mouse device (not a virtual/HID-only device)
    for _dev in range(11, 21):          # mice live in slots 11-20
        if _interception.is_mouse(_dev):
            _inter_device = _dev
            break

    if _inter_device is None:
        raise RuntimeError("No mouse device found via Interception")

    print(f"[MouseInput] Interception driver active — device {_inter_device}")

    def move_mouse_relative(dx, dy):
        """Kernel-level relative mouse move via Interception driver."""
        stroke = _ic.MouseStroke()
        stroke.state = 0
        stroke.flags = _ic.MouseFlag.MOUSE_MOVE_RELATIVE  # 0x000
        stroke.x     = int(dx)
        stroke.y     = int(dy)
        stroke.information = 0
        _interception.send(_inter_device, stroke)

except Exception as _e:
    # -----------------------------------------------------------------------
    # FALLBACK: ctypes mouse_event
    # -----------------------------------------------------------------------
    print(f"[MouseInput] Interception unavailable ({_e}), using win32api fallback")

    def move_mouse_relative(dx, dy):
        """Relative mouse move using standard Windows API."""
        ctypes.windll.user32.mouse_event(0x0001, int(dx), int(dy), 0, 0)


class Settings:
    def __init__(self):
        # General & Colors
        self.running = True
        self.fps = 0
        self.box_color = (0, 255, 0)
        self.overlay_opacity = 255

        # Detection Tab
        self.confidence = 0.40
        self.img_size = 160
        self.y_offset = 15
        self.target_bone = "Chest"

        # FOV & Stealth Tab
        self.fov_base = 250
        self.show_fov_circle = True
        self.dynamic_fov = False
        self.draw_delay_ms = 0

        # Prediction Tab
        self.prediction_mult = 1.0
        self.dynamic_predict = True
        self.smoothing = 0.2
        self.show_target_line = True

        # Visuals Tab
        self.box_thickness = 2
        self.draw_center_dot = True
        self.show_fps_overlay = True

        # Performance Tab
        self.fps_cap = 144
        self.capture_delay = 0

        # Auto Aim Tab
        self.auto_aim_enabled = False
        self.auto_aim_key = "RMB"      # "RMB", "LMB", "Always"
        self.auto_aim_speed = 8.0      # max pixels moved per frame
        self.auto_aim_fov_lock = True  # ignore targets outside FOV circle
        self.auto_aim_bone = "Chest"   # "Head", "Chest", "Pelvis"
        self.auto_aim_strength = 0.5   # 0.05 = soft glide, 1.0 = near-snap
        self.auto_aim_recoil = False   # pull down while LMB held


cfg = Settings()


# --- 1. MODERN GUI ---
import subprocess
import sys

try:
    import customtkinter as ctk
except ImportError:
    print("Installing customtkinter for premium GUI...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "customtkinter"])
    import customtkinter as ctk

def launch_gui():
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")

    root = ctk.CTk()
    root.title("Apex Sentinel v5.0")
    root.geometry("600x650")
    root.attributes("-topmost", True)

    title_font = ctk.CTkFont(family="Roboto", size=24, weight="bold")
    lbl_title = ctk.CTkLabel(root, text="APEX SENTINEL", font=title_font, text_color="#00aaff")
    lbl_title.pack(pady=(15, 5))

    tabview = ctk.CTkTabview(root, width=550, height=550)
    tabview.pack(padx=20, pady=10, fill="both", expand=True)

    for tab in ["Auto Aim", "Detection", "Visuals", "Prediction", "Stealth", "Engine"]:
        tabview.add(tab)

    def create_slider(parent, text, attr, from_, to_, res=1):
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.pack(fill="x", pady=10, padx=20)
        
        lbl = ctk.CTkLabel(frame, text=text, font=ctk.CTkFont(size=13))
        lbl.pack(anchor="w")
        
        val_lbl = ctk.CTkLabel(frame, text=str(getattr(cfg, attr)), text_color="#00aaff", font=ctk.CTkFont(weight="bold"))
        val_lbl.pack(anchor="e", side="right")
        
        def update_val(v):
            val = float(v) if res < 1 else int(v)
            setattr(cfg, attr, val)
            val_lbl.configure(text=f"{val:.2f}" if res < 1 else str(val))
            
        s = ctk.CTkSlider(frame, from_=from_, to_=to_, number_of_steps=int((to_-from_)/res), command=update_val)
        s.set(getattr(cfg, attr))
        s.pack(fill="x", pady=5)

    def create_switch(parent, text, attr):
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.pack(fill="x", pady=10, padx=20)
        
        def toggle_val():
            setattr(cfg, attr, switch.get())
            
        switch = ctk.CTkSwitch(frame, text=text, font=ctk.CTkFont(size=13), command=toggle_val)
        switch.select() if getattr(cfg, attr) else switch.deselect()
        switch.pack(anchor="w")

    # --- AUTO AIM TAB ---
    t_aim = tabview.tab("Auto Aim")
    create_switch(t_aim, "Enable Auto Aim", "auto_aim_enabled")
    create_switch(t_aim, "FOV Lock (Target must be inside FOV)", "auto_aim_fov_lock")
    create_switch(t_aim, "Recoil Compensation (Pull down slightly)", "auto_aim_recoil")
    create_slider(t_aim, "Aim Speed (Pixels per tick)", "auto_aim_speed", 1.0, 30.0, 0.5)
    create_slider(t_aim, "Aim Strength/Smoothing (0=Soft 1=Snap)", "auto_aim_strength", 0.01, 1.0, 0.01)
    
    # Bone Selection
    bone_frame = ctk.CTkFrame(t_aim, fg_color="transparent")
    bone_frame.pack(fill="x", pady=10, padx=20)
    ctk.CTkLabel(bone_frame, text="Target Bone:", font=ctk.CTkFont(size=13)).pack(side="left", padx=(0, 10))
    bone_var = ctk.StringVar(value=cfg.auto_aim_bone)
        
    def set_bone(v): cfg.auto_aim_bone = v
    for b in ["Head", "Chest", "Pelvis"]:
        ctk.CTkRadioButton(bone_frame, text=b, variable=bone_var, value=b, command=lambda v=b: set_bone(v)).pack(side="left", padx=10)

    # Key Selection
    key_frame = ctk.CTkFrame(t_aim, fg_color="transparent")
    key_frame.pack(fill="x", pady=10, padx=20)
    ctk.CTkLabel(key_frame, text="Trigger Key:", font=ctk.CTkFont(size=13)).pack(side="left", padx=(0, 10))
    key_var = ctk.StringVar(value=cfg.auto_aim_key)
        
    def set_key(v): cfg.auto_aim_key = v
    for k in ["RMB", "LMB", "Always"]:
        ctk.CTkRadioButton(key_frame, text=k, variable=key_var, value=k, command=lambda v=k: set_key(v)).pack(side="left", padx=10)

    # --- DETECTION TAB ---
    t_det = tabview.tab("Detection")
    create_slider(t_det, "AI Resolution (imgsz)", "img_size", 32, 640, 32)
    create_slider(t_det, "Confidence Threshold", "confidence", 0.01, 1.0, 0.01)
    create_slider(t_det, "Vertical Height Offset", "y_offset", -100, 100, 1)

    # --- VISUALS TAB ---
    t_vis = tabview.tab("Visuals")
    create_switch(t_vis, "Draw Center Dot", "draw_center_dot")
    create_slider(t_vis, "Box Thickness", "box_thickness", 1, 5, 1)
    
    def pick_color():
        from tkinter.colorchooser import askcolor
        c = askcolor(color=cfg.box_color)[0]
        if c: cfg.box_color = c
        
    ctk.CTkButton(t_vis, text="Change Box Color", command=pick_color).pack(pady=20)

    # --- PREDICTION TAB ---
    t_pred = tabview.tab("Prediction")
    create_switch(t_pred, "Dynamic Lead Calculation", "dynamic_predict")
    create_slider(t_pred, "Prediction Intensity", "prediction_mult", 0.0, 5.0, 0.1)
    create_slider(t_pred, "Input Smoothing", "smoothing", 0.05, 1.0, 0.05)

    # --- STEALTH TAB ---
    t_stl = tabview.tab("Stealth")
    create_switch(t_stl, "Dynamic FOV (Velocity Based)", "dynamic_fov")
    create_slider(t_stl, "FOV Radius", "fov_base", 10, 800, 5)
    create_slider(t_stl, "Drawing Stealth Delay (ms)", "draw_delay_ms", 0, 1000, 10)

    # --- ENGINE TAB ---
    t_eng = tabview.tab("Engine")
    create_slider(t_eng, "Max FPS Cap", "fps_cap", 30, 240, 1)
    
    lbl_fps = ctk.CTkLabel(t_eng, text="Engine FPS: 0", font=ctk.CTkFont(size=20, weight="bold"), text_color="#00ff00")
    lbl_fps.pack(pady=30)

    def update_loop():
        if cfg.running:
            lbl_fps.configure(text=f"Engine FPS: {cfg.fps}")
            root.after(500, update_loop)

    update_loop()
    root.mainloop()


# --- 2. MULTI-THREADED AI CORE ---
script_dir = os.path.dirname(os.path.abspath(__file__))
model = YOLO(os.path.join(script_dir, "apex_8n.pt"))
device = 'cuda' if torch.cuda.is_available() else 'cpu'
model.to(device)
if device == 'cuda':
    model.fuse()

# Lock-free slot: AI thread writes, render thread snapshots without blocking.
latest_results = None
_result_lock = threading.Lock()


def ai_thread():
    """Runs inference as fast as possible on a dedicated thread.
    np.frombuffer avoids the extra array copy that np.array(shot) makes.
    """
    global latest_results
    with mss() as sct:
        while cfg.running:
            f_size = cfg.fov_base
            left = (SW // 2) - (f_size // 2)
            top  = (SH // 2) - (f_size // 2)
            shot = sct.grab({"top": top, "left": left,
                             "width": f_size, "height": f_size})
            # Zero-copy: reinterpret raw BGRA buffer, drop alpha
            frame = np.frombuffer(shot.raw, dtype=np.uint8) \
                      .reshape(f_size, f_size, 4)[:, :, :3]
            res = model.predict(frame, imgsz=cfg.img_size,
                                conf=cfg.confidence,
                                half=(device == 'cuda'), verbose=False)
            with _result_lock:
                latest_results = res


# --- 3. HIGH-SPEED OVERLAY ---
pygame.init()
fuchsia = (255, 0, 128)
SW, SH = win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)
screen = pygame.display.set_mode((SW, SH), pygame.NOFRAME)
hwnd = pygame.display.get_wm_info()["window"]

win32gui.SetWindowLong(
    hwnd, win32con.GWL_EXSTYLE,
    win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) |
    win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT)
win32gui.SetLayeredWindowAttributes(
    hwnd, win32api.RGB(*fuchsia), 0, win32con.LWA_COLORKEY)
win32gui.SetWindowPos(
    hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
    win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)

# --- 4. RENDER + AIM ENGINE ---
threading.Thread(target=launch_gui, daemon=True).start()
threading.Thread(target=ai_thread, daemon=True).start()

smooth_x, smooth_y = SW // 2, SH // 2
last_target_pos = None
clock = pygame.time.Clock()

# Pre-built VK map — avoids dict construction inside the hot loop
_AIM_KEY_VK = {"RMB": 0x02, "LMB": 0x01, "Always": None}

_residual_x = 0.0
_residual_y = 0.0

while cfg.running:
    pygame.event.pump()
    screen.fill(fuchsia)

    if cfg.show_fov_circle:
        pygame.draw.circle(screen, (50, 50, 50),
                           (SW // 2, SH // 2), cfg.fov_base // 2, 1)

    # Snapshot latest results without blocking the AI thread
    with _result_lock:
        results_snap = latest_results

    if results_snap:
        left = (SW // 2) - (cfg.fov_base // 2)
        top  = (SH // 2) - (cfg.fov_base // 2)
        best_t = None
        min_d  = float('inf')

        for r in results_snap:
            for box in r.boxes:
                x1, y1, x2, y2 = box.xyxy[0].tolist()
                bx = (x1 + x2) / 2 + left
                by = (y1 + y2) / 2 + top
                dist = math.sqrt((bx - SW // 2) ** 2 + (by - SH // 2) ** 2)
                if dist < min_d:
                    min_d  = dist
                    best_t = (bx, by, x2 - x1, y2 - y1)

        if best_t:
            tx, ty, bw, bh = best_t

            # --- AUTO AIM ---
            if cfg.auto_aim_enabled:
                key_vk = _AIM_KEY_VK.get(cfg.auto_aim_key)
                trigger_held = (
                    key_vk is None or win32api.GetKeyState(key_vk) < 0)

                if trigger_held:
                    bone_offsets = {
                        "Head":   -bh * 0.35,
                        "Chest":   0.0,
                        "Pelvis":  bh * 0.30,
                    }
                    bone_dy = bone_offsets.get(cfg.auto_aim_bone, 0.0)

                    dist_from_center = math.sqrt(
                        (tx - SW // 2) ** 2 + (ty - SH // 2) ** 2)
                    in_fov = (
                        not cfg.auto_aim_fov_lock or
                        dist_from_center <= cfg.fov_base / 2)

                    if in_fov:
                        delta_x = tx - SW // 2
                        delta_y = (ty + bone_dy) - SH // 2

                        scale = cfg.auto_aim_strength * (cfg.auto_aim_speed / 30.0)
                        move_x = max(-cfg.auto_aim_speed, min(cfg.auto_aim_speed, delta_x * scale))
                        move_y = max(-cfg.auto_aim_speed, min(cfg.auto_aim_speed, delta_y * scale))

                        _residual_x += move_x
                        _residual_y += move_y

                        move_x_int = int(_residual_x)
                        move_y_int = int(_residual_y)

                        if move_x_int != 0 or move_y_int != 0:
                            move_mouse_relative(move_x_int, move_y_int)
                            _residual_x -= move_x_int
                            _residual_y -= move_y_int

                # Recoil compensation: nudge down while LMB held
                if cfg.auto_aim_recoil and win32api.GetKeyState(0x01) < 0:
                    move_mouse_relative(0, 1)

            # --- SMOOTHING & PREDICTION ---
            if last_target_pos:
                vx = tx - last_target_pos[0]
                vy = ty - last_target_pos[1]
                speed = (math.sqrt(vx ** 2 + vy ** 2)
                         if cfg.dynamic_predict else 1.0)

                px = tx + vx * cfg.prediction_mult * (1 + speed * 0.05)
                py = (ty + vy * cfg.prediction_mult * (1 + speed * 0.05)
                      - cfg.y_offset)

                smooth_x += (px - smooth_x) * cfg.smoothing
                smooth_y += (py - smooth_y) * cfg.smoothing

                pygame.draw.rect(
                    screen, cfg.box_color,
                    (int(smooth_x - bw / 2), int(smooth_y - bh / 2),
                     int(bw), int(bh)),
                    cfg.box_thickness)

                if cfg.show_target_line:
                    pygame.draw.line(
                        screen, (255, 255, 255),
                        (SW // 2, SH // 2),
                        (int(smooth_x), int(smooth_y)), 1)

                if cfg.draw_center_dot:
                    pygame.draw.circle(
                        screen, (255, 0, 0),
                        (int(smooth_x), int(smooth_y)), 3)

            last_target_pos = (tx, ty)
        else:
            last_target_pos = None

    pygame.display.update()
    clock.tick(cfg.fps_cap)
    cfg.fps = int(clock.get_fps())