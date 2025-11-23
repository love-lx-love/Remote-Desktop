import sys
import cv2
import numpy as np
import socket
import json
from PyQt5.QtWidgets import (QApplication, QMainWindow, QLabel, QStatusBar, 
                             QAction, QMessageBox, QShortcut, QInputDialog, 
                             QLineEdit, QSplashScreen, QWidget, QVBoxLayout)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import (QImage, QPixmap, QKeySequence, QFont, QColor, 
                         QPainter, QBrush)
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
import base64

# -------------------------- 配置参数（集中管理）--------------------------
UDP_PORT = 1234
INPUT_PORT = 5678
BUFFER_SIZE = 1024 * 1024 * 50
APP_NAME = "远程桌面客户端"
APP_VERSION = "0.1"
DEFAULT_WINDOW_SIZE = (1280, 720)
MIN_WINDOW_SIZE = (640, 360)
PLACEHOLDER_COLOR = QColor(20, 20, 20)
TEXT_COLOR = QColor(180, 180, 180)
SUCCESS_COLOR = QColor(46, 204, 113)
ERROR_COLOR = QColor(231, 76, 60)
LOADING_COLOR = QColor(52, 152, 219)
# 加密配置（与服务器一致）
SECRET_KEY = b"pS0eD3kY2mM8iX9kE8pS9gC5lX1zA4cZ"  # 16/24/32字节密钥(与client一致)
RECONNECT_INTERVAL = 10  # 流断开后重试间隔（秒）

# -------------------------- 加密工具类 --------------------------
class CryptoTool:
    @staticmethod
    def encrypt(data):
        """加密数据（AES-CBC）"""
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

# -------------------------- 视频流接收线程 --------------------------
class StreamWorker(QThread):
    frame_received = pyqtSignal(np.ndarray)
    error_occurred = pyqtSignal(str)
    status_updated = pyqtSignal(str)
    finished_signal = pyqtSignal()

    def __init__(self, port):
        super().__init__()
        self.port = port
        self.is_running = True
        self.cap = None
        # 兼容旧版OpenCV
        try:
            cv2.setLogLevel(cv2.LOG_LEVEL_ERROR)
        except AttributeError:
            cv2.setLogLevel(3)

    def run(self):
        """优化：持续重试连接 + 增强容错"""
        while self.is_running:
            stream_url = (
                f"udp://0.0.0.0:{self.port}?"
                "overrun_nonfatal=1&fifo_size=50000000&buffer_size=8192k&reorder_queue_size=0&"
                "fflags=discardcorrupt+nobuffer+fastseek&flags=low_delay"
            )

            # 3次连接重试
            retry_count = 0
            max_retries = 3
            while retry_count < max_retries and self.is_running:
                self.cap = cv2.VideoCapture(stream_url, cv2.CAP_FFMPEG)
                if self.cap.isOpened():
                    self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    self.cap.set(cv2.CAP_PROP_FPS, 30)
                    try:
                        self.cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 3000)
                        self.cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 2000)
                    except AttributeError:
                        self.status_updated.emit("⚠️  旧版OpenCV不支持超时设置")
                    self.status_updated.emit("✅ 视频流已连接")
                    break
                
                retry_count += 1
                self.status_updated.emit(f"⚠️  连接视频流失败（{retry_count}/{max_retries}）")
                QThread.msleep(1500)

            if not self.cap or not self.cap.isOpened():
                self.status_updated.emit(f"❌ 无法打开视频流，{RECONNECT_INTERVAL}秒后重试...")
                QThread.msleep(RECONNECT_INTERVAL * 1000)
                continue

            # 帧读取循环
            frame_count = 0
            error_count = 0
            max_errors = 5
            while self.is_running:
                try:
                    if not self.cap.isOpened():
                        raise Exception("流已断开")
                    
                    ret, frame = self.cap.read()
                    if not ret or frame is None:
                        error_count += 1
                        if error_count >= max_errors:
                            self.status_updated.emit("⚠️  连续帧错误，重启流...")
                            self.restart_stream()
                            error_count = 0
                        continue
                    
                    if frame.shape[0] == 0 or frame.shape[1] == 0:
                        error_count += 1
                        continue
                    
                    error_count = 0
                    self.frame_received.emit(frame)

                    # 定期输出状态
                    frame_count += 1
                    if frame_count % 30 == 0:
                        fps = self.cap.get(cv2.CAP_PROP_FPS)
                        self.status_updated.emit(f"✅ 接收中 | 帧率：{fps:.1f} FPS | 累计帧数：{frame_count}")

                except Exception as e:
                    error_count += 1
                    self.status_updated.emit(f"⚠️  帧读取异常：{str(e)[:25]}")
                    if error_count >= max_errors:
                        self.restart_stream()
                        error_count = 0
                    QThread.msleep(500)

    def restart_stream(self):
        """重启视频流"""
        if not self.is_running:
            return
        
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
        
        stream_url = (
            f"udp://0.0.0.0:{self.port}?"
            "overrun_nonfatal=1&fifo_size=50000000&buffer_size=8192k&reorder_queue_size=0&"
            "fflags=discardcorrupt+nobuffer+fastseek&flags=low_delay"
        )
        self.cap = cv2.VideoCapture(stream_url, cv2.CAP_FFMPEG)
        if self.cap.isOpened():
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            self.cap.set(cv2.CAP_PROP_FPS, 30)
            try:
                self.cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 3000)
                self.cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 2000)
            except AttributeError:
                pass
            self.status_updated.emit("✅ 流重启成功")
        else:
            self.status_updated.emit("⚠️  流重启失败，将再次重试")
            QThread.msleep(1000)

    def stop(self):
        """安全停止线程"""
        self.is_running = False
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception as e:
                print(f"释放VideoCapture异常：{e}")
        self.wait(3000)
        self.finished_signal.emit()

# -------------------------- 主窗口类 --------------------------
class DesktopStreamClient(QMainWindow):
    def __init__(self):
        super().__init__()
        self.splash = None
        self.splash_timer = None
        self.progress = 0
        self.server_ip = None
        self.authenticated = False
        self.input_sock = None
        self.server_input_addr = None
        self.is_fullscreen = False
        
        self.init_splash_screen()
        self.init_server_config()
        self.init_ui()
        self.init_signals()
        self.close_splash_screen()

    def init_splash_screen(self):
        """启动加载界面"""
        temp_pix = QPixmap(400, 200)
        temp_pix.fill(PLACEHOLDER_COLOR)

        painter = QPainter(temp_pix)
        painter.setPen(TEXT_COLOR)
        painter.setFont(QFont("Microsoft YaHei", 16, QFont.Bold))
        painter.drawText(100, 60, APP_NAME)
        painter.setFont(QFont("Microsoft YaHei", 10))
        painter.drawText(100, 90, f"版本：{APP_VERSION}")
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(50, 50, 50)))
        painter.drawRect(50, 130, 300, 8)
        painter.end()

        self.splash = QSplashScreen(temp_pix)
        self.splash.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.splash.show()
        QApplication.processEvents()

        self.progress = 0
        self.splash_timer = QTimer()
        self.splash_timer.timeout.connect(self.update_splash_progress)
        self.splash_timer.start(50)

    def update_splash_progress(self):
        """更新加载进度"""
        self.progress += 2
        if self.progress > 100:
            self.progress = 100
            self.splash_timer.stop()
        
        temp_pix = QPixmap(400, 200)
        temp_pix.fill(PLACEHOLDER_COLOR)
        
        with QPainter(temp_pix) as painter:
            painter.setPen(TEXT_COLOR)
            painter.setFont(QFont("Microsoft YaHei", 16, QFont.Bold))
            painter.drawText(100, 60, APP_NAME)
            painter.setFont(QFont("Microsoft YaHei", 10))
            painter.drawText(100, 90, f"版本：{APP_VERSION}")
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor(50, 50, 50)))
            painter.drawRect(50, 130, 300, 8)
            painter.setBrush(QBrush(LOADING_COLOR))
            painter.drawRect(50, 130, int(300 * self.progress / 100), 8)
        
        self.splash.setPixmap(temp_pix)
        QApplication.processEvents()

    def close_splash_screen(self):
        """关闭加载界面"""
        if self.splash_timer and self.splash_timer.isActive():
            self.splash_timer.stop()
        
        if self.splash:
            QApplication.processEvents()
            self.splash.finish(self)
            self.splash = None

    def init_server_config(self):
        """初始化服务器配置（支持重试 + 输入框置顶）"""
        # 输入服务器IP（置顶窗口）
        max_ip_retries = 3
        for _ in range(max_ip_retries):
            # 创建IP输入对话框并设置置顶
            ip_dialog = QInputDialog(self)
            ip_dialog.setWindowTitle(f"{APP_NAME} - 服务器设置")
            ip_dialog.setLabelText("请输入远程服务器 IP：")
            ip_dialog.setInputMode(QInputDialog.TextInput)
            ip_dialog.setWindowFlags(ip_dialog.windowFlags() | Qt.WindowStaysOnTopHint)  # 置顶标志
            ip_dialog.setModal(True)  # 模态窗口（阻塞其他操作）
            
            # 显示对话框并获取结果
            ok = ip_dialog.exec_()
            server_ip = ip_dialog.textValue().strip()
            
            if not ok:
                QMessageBox.information(self, "提示", "已取消操作，程序退出")
                self.close_splash_screen()
                sys.exit(0)
            
            if server_ip and len(server_ip.split('.')) == 4:
                self.server_ip = server_ip
                break
            else:
                QMessageBox.warning(self, "警告", f"IP格式不正确，剩余重试次数：{max_ip_retries - _ - 1}")
        else:
            QMessageBox.critical(self, "错误", "IP输入错误次数过多，程序退出")
            sys.exit(0)
        
        # 密码认证（置顶窗口）
        self.input_sock = self.create_udp_socket()
        self.server_input_addr = (self.server_ip, INPUT_PORT)
        
        # 发送连接请求（让服务器获取客户端IP）
        try:
            connect_data = CryptoTool.encrypt({"type": "connect"})
            self.input_sock.sendto(connect_data, self.server_input_addr)
        except Exception as e:
            QMessageBox.warning(self, "提示", f"发送连接请求失败：{str(e)}")
        
        # 3次密码重试（置顶窗口）
        for _ in range(3):
            # 创建密码输入对话框并设置置顶
            pwd_dialog = QInputDialog(self)
            pwd_dialog.setWindowTitle(f"{APP_NAME} - 身份认证")
            pwd_dialog.setLabelText("请输入远程控制密码：")
            pwd_dialog.setInputMode(QInputDialog.TextInput)
            pwd_dialog.setTextEchoMode(QLineEdit.Password)  # 密码隐藏显示
            pwd_dialog.setWindowFlags(pwd_dialog.windowFlags() | Qt.WindowStaysOnTopHint)  # 置顶标志
            pwd_dialog.setModal(True)  # 模态窗口（阻塞其他操作）
            
            # 显示对话框并获取结果
            ok = pwd_dialog.exec_()
            password = pwd_dialog.textValue().strip()
            
            if not ok:
                QMessageBox.information(self, "提示", "已取消认证，程序退出")
                self.cleanup_resources()
                self.close_splash_screen()
                sys.exit(0)
            
            if self.send_auth_request(password):
                self.authenticated = True
                break
        else:
            QMessageBox.critical(self, "认证失败", "密码错误次数过多，程序退出")
            self.cleanup_resources()
            self.close_splash_screen()
            sys.exit(0)

    def create_udp_socket(self):
        """创建UDP套接字"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(5.0)
        return sock

    def send_auth_request(self, password):
        """发送认证请求（加密）"""
        try:
            auth_data = CryptoTool.encrypt({"type": "auth", "password": password})
            self.input_sock.sendto(auth_data, self.server_input_addr)
            data, _ = self.input_sock.recvfrom(1024)
            resp = CryptoTool.decrypt(data)
            return resp.get("status") == "ok" if resp else False
        except socket.timeout:
            QMessageBox.warning(self, "认证失败", "服务器无响应，请检查网络连接")
            return False
        except Exception as e:
            QMessageBox.warning(self, "认证失败", f"未知错误：{str(e)}")
            return False

    def init_ui(self):
        """初始化主界面"""
        self.setWindowTitle(f"{APP_NAME} - 已连接：{self.server_ip}")
        self.setGeometry(
            (QApplication.desktop().width() - DEFAULT_WINDOW_SIZE[0]) // 2,
            (QApplication.desktop().height() - DEFAULT_WINDOW_SIZE[1]) // 2,
            *DEFAULT_WINDOW_SIZE
        )
        self.setMinimumSize(*MIN_WINDOW_SIZE)
        
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(0, 0, 0, 0)
        
        self.label = QLabel()
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setStyleSheet(f"background-color: {PLACEHOLDER_COLOR.name()}; border: 1px solid #333;")
        self.label.setMouseTracking(True)
        self.label.installEventFilter(self)
        layout.addWidget(self.label)
        
        self.status_bar = QStatusBar()
        self.status_bar.setStyleSheet("QStatusBar { background-color: #222; color: #eee; font-size: 12px; }")
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("🔄 正在连接视频流...")
        
        self.init_menu_bar()
        self.show_placeholder("等待视频流连接...")

    def init_menu_bar(self):
        """初始化菜单栏"""
        menubar = self.menuBar()
        menubar.setStyleSheet("""
            QMenuBar { background-color: #2c3e50; color: white; }
            QMenuBar::item { background: transparent; padding: 4px 8px; }
            QMenuBar::item:selected { background-color: #34495e; }
            QMenu { background-color: #2c3e50; color: white; }
            QMenu::item:selected { background-color: #3498db; }
        """)
        
        window_menu = menubar.addMenu("🪟 窗口")
        self.topmost_action = QAction("📌 置顶窗口", self, checkable=True)
        self.topmost_action.triggered.connect(self.toggle_topmost)
        window_menu.addAction(self.topmost_action)
        
        fullscreen_action = QAction("⛶ 全屏显示", self, shortcut=QKeySequence("F11"))
        fullscreen_action.triggered.connect(self.toggle_fullscreen)
        window_menu.addAction(fullscreen_action)
        
        refresh_action = QAction("🔄 刷新视频流", self, shortcut=QKeySequence("F5"))
        refresh_action.triggered.connect(self.refresh_stream)
        window_menu.addAction(refresh_action)
        
        help_menu = menubar.addMenu("❓ 帮助")
        about_action = QAction("ℹ️  关于", self)
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)
        
        exit_action = QAction("🚪 退出", self, shortcut=QKeySequence("Ctrl+Q"))
        exit_action.triggered.connect(self.close)
        help_menu.addAction(exit_action)

    def init_signals(self):
        """初始化信号绑定"""
        QShortcut(QKeySequence("Escape"), self).activated.connect(self.exit_fullscreen)
        QShortcut(QKeySequence("Ctrl+R"), self).activated.connect(self.refresh_stream)
        
        self.worker = StreamWorker(UDP_PORT)
        self.worker.frame_received.connect(self.update_frame)
        self.worker.error_occurred.connect(self.show_error)
        self.worker.status_updated.connect(self.update_status)
        self.worker.start()

    def show_placeholder(self, text="无信号"):
        """显示占位图"""
        label_size = self.label.size()
        placeholder = QImage(
            label_size.width() if label_size.width() > 0 else 640,
            label_size.height() if label_size.height() > 0 else 360,
            QImage.Format_RGB888
        )
        placeholder.fill(PLACEHOLDER_COLOR)
        
        with QPainter(placeholder) as painter:
            painter.setPen(TEXT_COLOR)
            painter.setFont(QFont("Microsoft YaHei", 14))
            painter.drawText(placeholder.rect(), Qt.AlignCenter, text)
        
        self.label.setPixmap(QPixmap.fromImage(placeholder))

    def update_frame(self, frame):
        """更新视频帧"""
        try:
            if frame is None or not self.isVisible():
                return
            
            label_size = self.label.size()
            frame_h, frame_w = frame.shape[:2]
            if frame_w == 0 or frame_h == 0:
                return
            
            # 保持宽高比缩放
            scale = min(label_size.width()/frame_w, label_size.height()/frame_h)
            new_w, new_h = int(frame_w*scale), int(frame_h*scale)
            resized_frame = cv2.resize(
                frame, (new_w, new_h),
                interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
            )
            
            rgb_frame = cv2.cvtColor(resized_frame, cv2.COLOR_BGR2RGB)
            h, w, ch = rgb_frame.shape
            bytes_per_line = ch * w
            qimg = QImage(rgb_frame.data, w, h, bytes_per_line, QImage.Format_RGB888)
            self.label.setPixmap(QPixmap.fromImage(qimg))
        except Exception as e:
            self.update_status(f"⚠️  画面更新失败：{str(e)[:20]}")

    def update_status(self, message):
        """更新状态栏消息"""
        if message.startswith("✅"):
            self.status_bar.setStyleSheet("QStatusBar { background-color: #27ae60; color: white; font-size: 12px; }")
        elif message.startswith("❌"):
            self.status_bar.setStyleSheet("QStatusBar { background-color: #c0392b; color: white; font-size: 12px; }")
        elif message.startswith("⚠️"):
            self.status_bar.setStyleSheet("QStatusBar { background-color: #f39c12; color: white; font-size: 12px; }")
        elif message.startswith("🔄"):
            self.status_bar.setStyleSheet("QStatusBar { background-color: #2980b9; color: white; font-size: 12px; }")
        else:
            self.status_bar.setStyleSheet("QStatusBar { background-color: #222; color: #eee; font-size: 12px; }")
        
        self.status_bar.showMessage(message)

    def show_error(self, message):
        """显示错误对话框"""
        QMessageBox.critical(self, "错误", message)
        self.close()

    def toggle_topmost(self):
        """切换窗口置顶"""
        is_topmost = self.topmost_action.isChecked()
        if is_topmost:
            self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
            self.update_status("📌 窗口已置顶")
        else:
            self.setWindowFlags(self.windowFlags() & ~Qt.WindowStaysOnTopHint)
            self.update_status("📌 窗口取消置顶")
        self.show()

    def toggle_fullscreen(self):
        """切换全屏"""
        if self.isFullScreen():
            self.showNormal()
            self.is_fullscreen = False
            self.update_status("⛶ 已退出全屏")
        else:
            self.showFullScreen()
            self.is_fullscreen = True
            self.update_status("⛶ 已进入全屏（ESC键退出）")

    def exit_fullscreen(self):
        """退出全屏"""
        if self.isFullScreen():
            self.showNormal()
            self.is_fullscreen = False
            self.update_status("⛶ 已退出全屏")

    def refresh_stream(self):
        """刷新视频流"""
        self.update_status("🔄 正在刷新视频流...")
        self.show_placeholder("刷新中...")
        
        if self.worker.isRunning():
            self.worker.stop()
        
        self.worker = StreamWorker(UDP_PORT)
        self.worker.frame_received.connect(self.update_frame)
        self.worker.error_occurred.connect(self.show_error)
        self.worker.status_updated.connect(self.update_status)
        self.worker.start()

    def show_about(self):
        """显示关于对话框"""
        about_text = f"""
        <h3>{APP_NAME} v{APP_VERSION}</h3>
        <p>📡 基于 PyQt5 + FFmpeg + OpenCV 开发</p>
        <p>🖥️  支持远程桌面查看和控制（加密传输）</p>
        <p><br><strong>快捷键：</strong></p>
        <p>• F11：全屏/退出全屏</p>
        <p>• ESC：退出全屏</p>
        <p>• Ctrl+Q：退出程序</p>
        <p>• F5/Ctrl+R：刷新视频流</p>
        <p><br>© 2025 远程桌面工具</p>
        """
        QMessageBox.about(self, f"关于 {APP_NAME}", about_text)

    def eventFilter(self, source, event):
        """事件过滤（鼠标键盘控制）"""
        if not self.authenticated or source != self.label:
            return super().eventFilter(source, event)
        
        try:
            # 鼠标移动
            if event.type() == event.MouseMove:
                if self.label.pixmap() is None:
                    return super().eventFilter(source, event)
                
                label_rect = self.label.rect()
                pixmap_rect = self.label.pixmap().rect()
                offset_x = (label_rect.width() - pixmap_rect.width()) // 2
                offset_y = (label_rect.height() - pixmap_rect.height()) // 2
                
                if (event.x() >= offset_x and event.x() < offset_x + pixmap_rect.width() and
                    event.y() >= offset_y and event.y() < offset_y + pixmap_rect.height()):
                    
                    x_ratio = (event.x() - offset_x) / pixmap_rect.width()
                    y_ratio = (event.y() - offset_y) / pixmap_rect.height()
                    data = {"type": "mouse_move", "x": round(x_ratio, 4), "y": round(y_ratio, 4)}
                    self.input_sock.sendto(CryptoTool.encrypt(data), self.server_input_addr)
            
            # 鼠标点击（按下/释放）
            elif event.type() == event.MouseButtonPress:
                btn = "left" if event.button() == Qt.LeftButton else "right"
                data = {"type": "mouse_click", "button": btn, "action": "press"}
                self.input_sock.sendto(CryptoTool.encrypt(data), self.server_input_addr)
            
            elif event.type() == event.MouseButtonRelease:
                btn = "left" if event.button() == Qt.LeftButton else "right"
                data = {"type": "mouse_click", "button": btn, "action": "release"}
                self.input_sock.sendto(CryptoTool.encrypt(data), self.server_input_addr)
            
            # 键盘事件（支持普通键、特殊键、组合键）
            elif event.type() == event.KeyPress:
                key = event.text()
                modifiers = event.modifiers()
                if key:
                    data = {"type": "key_press", "key": key, "modifiers": modifiers}
                    self.input_sock.sendto(CryptoTool.encrypt(data), self.server_input_addr)
                else:
                    key_code = event.key()
                    key_map = {
                        Qt.Key_Return: "enter",
                        Qt.Key_Enter: "numpad_enter",
                        Qt.Key_Backspace: "backspace",
                        Qt.Key_Tab: "tab",
                        Qt.Key_Escape: "escape",
                        Qt.Key_Space: "space",
                        Qt.Key_Up: "up",
                        Qt.Key_Down: "down",
                        Qt.Key_Left: "left",
                        Qt.Key_Right: "right",
                        Qt.Key_F1: "f1", Qt.Key_F2: "f2", Qt.Key_F3: "f3",
                        Qt.Key_F4: "f4", Qt.Key_F5: "f5", Qt.Key_F6: "f6",
                        Qt.Key_F7: "f7", Qt.Key_F8: "f8", Qt.Key_F9: "f9",
                        Qt.Key_F10: "f10", Qt.Key_F11: "f11", Qt.Key_F12: "f12",
                        Qt.Key_Shift: "shift", Qt.Key_Ctrl: "ctrl", Qt.Key_Alt: "alt"
                    }
                    if key_code in key_map:
                        key_name = key_map[key_code]
                        data = {"type": "key_press", "key": key_name, "modifiers": modifiers}
                        self.input_sock.sendto(CryptoTool.encrypt(data), self.server_input_addr)
        
        except Exception as e:
            self.update_status(f"⚠️  控制指令发送失败：{str(e)[:20]}")
        
        return super().eventFilter(source, event)

    def cleanup_resources(self):
        """释放资源"""
        if hasattr(self, "worker"):
            self.worker.is_running = False
            self.worker.finished_signal.connect(lambda: print("视频流线程已退出"))

        if hasattr(self, "input_sock"):
            try:
                self.input_sock.close()
            except Exception:
                pass
        
        cv2.destroyAllWindows()

    def closeEvent(self, event):
        """关闭事件"""
        reply = QMessageBox.question(
            self, f"{APP_NAME} - 退出确认", "确定要退出远程桌面客户端吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            self.status_bar.showMessage("🚪 正在退出程序...")
            QApplication.processEvents()
            self.cleanup_resources()
            event.accept()
        else:
            event.ignore()

# -------------------------- 主函数 --------------------------
if __name__ == "__main__":
    # 解决 Windows 环境冲突
    if sys.platform == "win32":
        import os
        os.environ["OPENCV_VIDEOIO_PRIORITY_MSMF"] = "0"
        if "FFMPEG_BIN" in os.environ:
            cv2.setNumThreads(1)
    
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    
    window = DesktopStreamClient()
    window.show()
    sys.exit(app.exec_())