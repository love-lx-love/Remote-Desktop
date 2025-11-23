import subprocess
import platform
import threading
import socket
import json
import time
from pynput.keyboard import Key, Controller as KeyboardController
from pynput.mouse import Controller as MouseController
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
import base64
import sys

# -------------------------- 配置参数 --------------------------
VIDEO_PORT = 1234  # 视频流端口（与客户端一致）
INPUT_PORT = 5678  # 控制端口（与客户端一致）
PASSWORD = "Admin@9000"  # 控制密码（与客户端一致）
SECRET_KEY = b"pS0eD3kY2mM8iX9kE8pS9gC5lX1zA4cZ" # 16/24/32字节密钥(与client一致)
FRAME_RATE = 15  # 推流帧率
QUALITY = 28  # 视频质量（1-51，越小越清晰）
MOUSE_MOVE_INTERVAL = 0.01  # 鼠标移动频率限制（秒）

# 全局状态变量（用于优雅退出）
authorized_clients = set()
client_ip = None  # 动态获取的客户端IP
is_running = True  # 服务运行标志
ffmpeg_process = None  # 存储FFmpeg子进程引用
control_socket = None  # 存储控制套接字引用
exit_thread = None  # 退出监听线程

# -------------------------- 加密工具类 --------------------------
class CryptoTool:
    @staticmethod
    def encrypt(data):
        """加密数据"""
        cipher = AES.new(SECRET_KEY, AES.MODE_CBC)
        iv = base64.b64encode(cipher.iv).decode()
        encrypted = cipher.encrypt(pad(json.dumps(data).encode(), AES.block_size))
        encrypted_data = base64.b64encode(encrypted).decode()
        return json.dumps({"iv": iv, "data": encrypted_data}).encode()

    @staticmethod
    def decrypt(encrypted_data):
        """解密数据"""
        try:
            data = json.loads(encrypted_data.decode())
            iv = base64.b64decode(data["iv"])
            encrypted = base64.b64decode(data["data"])
            cipher = AES.new(SECRET_KEY, AES.MODE_CBC, iv=iv)
            decrypted = unpad(cipher.decrypt(encrypted), AES.block_size)
            return json.loads(decrypted.decode())
        except Exception as e:
            print(f"解密失败：{e}")
            return None

# -------------------------- 优雅退出处理 --------------------------
def graceful_exit():
    """优雅退出核心逻辑"""
    global is_running
    print("\n\n🔴 开始清理资源...")
    is_running = False  # 置为False，触发所有循环退出
    
    # 1. 终止FFmpeg子进程
    if ffmpeg_process and ffmpeg_process.poll() is None:
        print("⏳ 终止FFmpeg推流进程...")
        try:
            # 先尝试优雅终止，失败则强制杀死
            ffmpeg_process.terminate()
            time.sleep(1)
            if ffmpeg_process.poll() is None:
                ffmpeg_process.kill()
            print("✅ FFmpeg进程已终止")
        except Exception as e:
            print(f"❌ 终止FFmpeg失败：{e}")
    
    # 2. 关闭控制套接字
    if control_socket:
        print("⏳ 关闭控制套接字...")
        try:
            control_socket.close()
            print("✅ 控制套接字已关闭")
        except Exception as e:
            print(f"❌ 关闭套接字失败：{e}")
    
    # 3. 等待所有子线程退出
    print("⏳ 等待子线程退出...")
    time.sleep(1)
    print("✅ 所有资源已清理，服务器退出")
    sys.exit(0)

def listen_for_exit():
    """监听控制台输入，输入quit退出（独立线程）"""
    while is_running:
        try:
            # 读取控制台输入（不阻塞主线程）
            user_input = input().strip().lower()
            if user_input == "quit" or user_input == "exit":
                graceful_exit()
        except:
            continue

# -------------------------- 视频推流函数 --------------------------
def check_ffmpeg():
    """检查FFmpeg是否安装"""
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False

def start_desktop_stream():
    """启动桌面推流（跨平台支持，记录进程引用）"""
    global ffmpeg_process
    # 检查FFmpeg
    if not check_ffmpeg():
        print("❌ 错误：未找到FFmpeg，请先安装并配置到环境变量")
        return
    
    # 权限提示
    system = platform.system()
    if system == "Darwin":
        print("⚠️  提示：请在 系统设置 > 安全性与隐私 > 屏幕录制 中允许终端/IDE")
    elif system == "Linux":
        print("⚠️  提示：需要安装依赖：sudo apt-get install libx11-dev x11-utils")
    
    # 选择桌面捕获方式
    if system == "Windows":
        input_params = ["-f", "gdigrab", "-framerate", str(FRAME_RATE), "-i", "desktop"]
    elif system == "Darwin":
        input_params = ["-f", "avfoundation", "-framerate", str(FRAME_RATE), "-i", "0"]
    elif system == "Linux":
        input_params = ["-f", "x11grab", "-framerate", str(FRAME_RATE), "-i", ":0.0"]
    else:
        print("❌ 不支持的操作系统")
        return

    # FFmpeg推流命令
    ffmpeg_cmd = [
        "ffmpeg",
        *input_params,
        "-c:v", "libx264",
        "-preset", "ultrafast",  # 快速编码（低延迟）
        "-crf", str(QUALITY),    # 视频质量
        "-pix_fmt", "yuv420p",   # 像素格式
        "-f", "mpegts",          # 流格式
        "-flush_packets", "1",   # 立即刷新数据包（低延迟）
        "-max_delay", "500",     # 最大延迟500ms
        f"udp://{client_ip}:{VIDEO_PORT}?overrun_nonfatal=1&fifo_size=50000000"
    ]

    print(f"✅ 启动推流：{' '.join(ffmpeg_cmd)}")
    try:
        # 启动FFmpeg并记录进程引用（不使用run，避免阻塞）
        ffmpeg_process = subprocess.Popen(
            ffmpeg_cmd,
            stdout=subprocess.DEVNULL,  # 屏蔽输出
            stderr=subprocess.DEVNULL,
            shell=False
        )
        # 等待进程结束或服务停止
        while is_running and ffmpeg_process.poll() is None:
            time.sleep(0.5)
    except Exception as e:
        print(f"❌ 推流异常：{e}")
    finally:
        # 确保进程被终止
        if ffmpeg_process and ffmpeg_process.poll() is None:
            ffmpeg_process.terminate()
            ffmpeg_process.wait()

# -------------------------- 控制指令处理 --------------------------
def handle_input():
    """处理客户端控制指令（支持优雅退出）"""
    global client_ip, control_socket
    control_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    control_socket.bind(("", INPUT_PORT))
    control_socket.settimeout(1.0)  # 设置超时，避免阻塞在recvfrom
    print(f"✅ 控制服务启动：端口 {INPUT_PORT}")
    print(f"⌛ 等待客户端连接...（输入 quit/exit 退出）")

    # 初始化鼠标/键盘控制器
    mouse = MouseController()
    keyboard = KeyboardController()
    screen_w, screen_h = mouse.position  # 获取屏幕分辨率
    mouse.position = (0, 0)  # 重置鼠标位置

    # 循环处理指令，直到is_running为False
    while is_running:
        try:
            # 超时会抛出异常，用于检查is_running状态
            data, addr = control_socket.recvfrom(1024)
            decrypted_data = CryptoTool.decrypt(data)
            if not decrypted_data:
                continue

            # -------------------------- 客户端认证 --------------------------
            if addr not in authorized_clients:
                # 处理连接请求（获取客户端IP）
                if decrypted_data.get("type") == "connect":
                    client_ip = addr[0]
                    print(f"📡 收到客户端连接请求：{client_ip}:{addr[1]}")
                    continue
                
                # 处理认证请求
                if decrypted_data.get("type") == "auth" and decrypted_data.get("password") == PASSWORD:
                    authorized_clients.add(addr)
                    print(f"✅ 客户端 {addr} 认证成功")
                    control_socket.sendto(CryptoTool.encrypt({"status": "ok"}), addr)
                    # 启动推流（单独线程，避免阻塞控制）
                    threading.Thread(target=start_desktop_stream, daemon=True).start()
                else:
                    print(f"❌ 客户端 {addr} 认证失败")
                    control_socket.sendto(CryptoTool.encrypt({"status": "fail"}), addr)
                continue

            # -------------------------- 已授权客户端指令处理 --------------------------
            cmd_type = decrypted_data.get("type")
            
            # 鼠标移动（平滑移动）
            if cmd_type == "mouse_move" and is_running:
                x_ratio = decrypted_data.get("x", 0)
                y_ratio = decrypted_data.get("y", 0)
                target_x = x_ratio * screen_w
                target_y = y_ratio * screen_h

                # 平滑移动（5步逼近）
                current_x, current_y = mouse.position
                step_x = (target_x - current_x) / 5
                step_y = (target_y - current_y) / 5
                for _ in range(5):
                    if not is_running:  # 退出时中断移动
                        break
                    mouse.move(step_x, step_y)
                    time.sleep(0.005)

            # 鼠标点击（按下/释放）
            elif cmd_type == "mouse_click" and is_running:
                button = decrypted_data.get("button")
                action = decrypted_data.get("action")
                if button == "left":
                    if action == "press":
                        mouse.press(mouse.Button.left)
                    else:
                        mouse.release(mouse.Button.left)
                elif button == "right":
                    if action == "press":
                        mouse.press(mouse.Button.right)
                    else:
                        mouse.release(mouse.Button.right)

            # 键盘控制（支持组合键，包括Ctrl+C）
            elif cmd_type == "key_press" and is_running:
                key = decrypted_data.get("key")
                modifiers = decrypted_data.get("modifiers", 0)

                # 处理修饰键（Ctrl/Shift/Alt）
                pressed_mods = []
                if modifiers & 0x40000:  # Qt.ControlModifier
                    pressed_mods.append(Key.ctrl)
                if modifiers & 0x80000:  # Qt.ShiftModifier
                    pressed_mods.append(Key.shift)
                if modifiers & 0x100000: # Qt.AltModifier
                    pressed_mods.append(Key.alt)

                # 按下修饰键
                for mod in pressed_mods:
                    keyboard.press(mod)

                # 处理普通键和特殊键（支持Ctrl+C复制）
                key_map = {
                    "enter": Key.enter,
                    "numpad_enter": Key.enter,
                    "backspace": Key.backspace,
                    "tab": Key.tab,
                    "escape": Key.esc,
                    "space": Key.space,
                    "up": Key.up,
                    "down": Key.down,
                    "left": Key.left,
                    "right": Key.right,
                    "f1": Key.f1, "f2": Key.f2, "f3": Key.f3,
                    "f4": Key.f4, "f5": Key.f5, "f6": Key.f6,
                    "f7": Key.f7, "f8": Key.f8, "f9": Key.f9,
                    "f10": Key.f10, "f11": Key.f11, "f12": Key.f12,
                    "shift": Key.shift, "ctrl": Key.ctrl, "alt": Key.alt
                }
                if key in key_map:
                    keyboard.press(key_map[key])
                    keyboard.release(key_map[key])
                elif len(key) == 1:
                    keyboard.press(key)
                    keyboard.release(key)

                # 释放修饰键
                for mod in reversed(pressed_mods):
                    keyboard.release(mod)

        except socket.timeout:
            continue  # 超时不处理，继续循环检查is_running
        except Exception as e:
            if is_running:  # 只有服务运行时才打印异常
                print(f"⚠️  指令处理异常：{e}")

# -------------------------- 主函数 --------------------------
if __name__ == "__main__":
    print("=" * 50)
    print(f"📡 远程桌面服务器 v2.0")
    print(f"🔑 密码：{PASSWORD}")
    print(f"📺 视频端口：{VIDEO_PORT} | 🎮 控制端口：{INPUT_PORT}")
    print("=" * 50)
    
    # 启动退出监听线程（独立线程，不阻塞控制逻辑）
    exit_thread = threading.Thread(target=listen_for_exit, daemon=True)
    exit_thread.start()
    
    try:
        # 启动控制服务（主线程）
        handle_input()
    except Exception as e:
        print(f"❌ 服务器异常：{e}")
    finally:
        # 兜底清理资源
        graceful_exit()