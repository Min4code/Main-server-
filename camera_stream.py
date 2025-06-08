import socket
import threading
import logging
import subprocess
import smtplib
import time
import io  # For PiCamera stream

try:
    import picamera  # For Raspberry Pi Camera

    PICAMERA_AVAILABLE = True
    logging.info("picamera library loaded successfully.")
except ImportError:
    PICAMERA_AVAILABLE = False
    logging.warning("picamera library not found. Camera functionality will be disabled.")
except Exception as e:
    PICAMERA_AVAILABLE = False
    logging.error(f"Error importing picamera: {e}. Camera functionality will be disabled.")

import cv2  # Still used for "Camera Offline" placeholder generation
from flask import Flask, Response, render_template_string, request, jsonify, send_from_directory
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import re
import os  # For favicon

# Configure logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - [%(threadName)s] - %(name)s - %(message)s')
logger = logging.getLogger(__name__)

# --- CONFIGURATIONS ---
# Email Configuration (Update these)
SENDER_EMAIL = "besho.paul999@gmail.com"
RECEIVER_EMAILS = ["besho.paul@gmail.com"]
APP_PASSWORD = "uihg xtcj mmpd usve"  # Gmail App Password

# ArduinoCarController TCP Server Details (Script 1)
ARDUINO_CONTROLLER_HOST = 'localhost'  # Assumes Script 1 (ArduinoCarController.py) runs on the same machine
ARDUINO_CONTROLLER_PORT = 9000  # Port Script 1 listens on

# Flask Web Server
FLASK_HOST = '0.0.0.0'
FLASK_PORT = 5000

# PiCamera settings
# WEBCAM_INDEX = 0 # No longer needed for PiCamera
PICAMERA_RESOLUTION = (640, 480)  # Width, Height
PICAMERA_FPS = 20  # Target FPS for capture
JPEG_QUALITY = 85  # For PiCamera capture and cv2.imencode (placeholder)

# Cloudflared
ENABLE_TUNNEL = True  # Set to False to disable cloudflared

# --- Globals ---
app = Flask(__name__)
tunnel_url_global = None
tunnel_process_global = None
camera_handler = None  # Will be instance of CameraHandler (PiCamera based)
flask_running = True


# ========== Get Local IP ==========
def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.1)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


LOCAL_IP = get_local_ip()


# ========== PiCamera Handler ==========
class CameraHandler:  # Renamed from WebcamHandler to generic CameraHandler
    def __init__(self, resolution=(640, 480), framerate=30, jpeg_quality=85):
        if not PICAMERA_AVAILABLE:
            logger.error("PiCamera library not available. CameraHandler cannot operate.")
            self.camera = None
            self.running = False
            return

        self.camera = None
        self.frame_bytes = None
        self.running = False
        self.lock = threading.Lock()
        self.resolution = resolution
        self.framerate = framerate
        self.jpeg_quality = jpeg_quality
        self.thread = None
        self.last_frame_time = 0

    def start(self):
        if not PICAMERA_AVAILABLE or self.camera is not None:  # Prevent re-init if already started
            if self.running:
                logger.info("PiCamera already running.")
            elif not PICAMERA_AVAILABLE:
                logger.error("Cannot start camera: picamera library not available.")
            return
        if self.running: return

        try:
            logger.info(f"Initializing Raspberry Pi Camera...")
            self.camera = picamera.PiCamera()
            self.camera.resolution = self.resolution
            self.camera.framerate = self.framerate
            # Optional: self.camera.rotation = 180
            # Optional: self.camera.hflip = True / self.camera.vflip = True
            logger.info("Giving PiCamera 2 seconds to warm up...")
            time.sleep(2)  # Recommended for PiCamera to adjust settings

            logger.info(
                f"PiCamera configured: {self.camera.resolution[0]}x{self.camera.resolution[1]} @ {self.camera.framerate} FPS")

            self.running = True
            self.thread = threading.Thread(target=self._capture_loop, name="PiCameraCaptureThread", daemon=True)
            self.thread.start()
            logger.info(f"PiCamera capture started (JPEG Quality: {self.jpeg_quality}).")

        except picamera.exc.PiCameraError as e:
            logger.error(f"PiCameraError initializing PiCamera: {e}", exc_info=True)
            self.running = False
            if self.camera: self.camera.close(); self.camera = None
        except Exception as e:
            logger.error(f"Error starting PiCamera: {e}", exc_info=True)
            self.running = False
            if self.camera: self.camera.close(); self.camera = None

    def _capture_loop(self):
        if not PICAMERA_AVAILABLE or not self.camera: return

        stream = io.BytesIO()
        try:
            for _ in self.camera.capture_continuous(stream, format='jpeg', use_video_port=True,
                                                    quality=self.jpeg_quality):
                if not self.running:
                    break

                with self.lock:
                    self.frame_bytes = stream.getvalue()
                    self.last_frame_time = time.perf_counter()

                stream.seek(0)
                stream.truncate()

            # If loop exits and self.running is still true, it means capture_continuous ended unexpectedly.
            if self.running:
                logger.warning(
                    "PiCamera capture_continuous stream ended unexpectedly while still supposed to be running.")

        except picamera.exc.PiCameraNotRecording as e:
            if not self.running:  # Expected if we are stopping
                logger.info("PiCamera capture stopped as requested (PiCameraNotRecording).")
            else:  # Not expected
                logger.error(f"PiCameraNotRecording error during capture: {e}", exc_info=True)
        except picamera.exc.PiCameraError as e:
            logger.error(f"PiCameraError during capture: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"Unexpected error in PiCamera capture loop: {e}", exc_info=True)
        finally:
            self.running = False  # Ensure running is false if loop exits for any reason
            logger.info("PiCamera capture loop attempting to stop.")
            if self.camera and hasattr(self.camera, 'closed') and not self.camera.closed:
                try:
                    # self.camera.stop_preview() # Only if a preview was started
                    self.camera.close()
                    logger.info("PiCamera closed in capture loop finally.")
                except Exception as e_close:
                    logger.error(f"Error closing PiCamera in capture loop finally: {e_close}")
            self.camera = None
            logger.info("PiCamera capture loop fully stopped.")

    def get_frame_jpeg_bytes(self):
        if not PICAMERA_AVAILABLE: return None
        with self.lock:
            # Check if frame is recent enough (e.g., within 1 second)
            if self.frame_bytes and (time.perf_counter() - self.last_frame_time < 1.0):
                return self.frame_bytes
            return None

    def stop(self):
        if not PICAMERA_AVAILABLE: return
        logger.info("Stopping CameraHandler (PiCamera)...")
        self.running = False  # Signal the capture loop to stop

        if self.thread and self.thread.is_alive():
            logger.info("Waiting for PiCamera capture thread to join...")
            self.thread.join(timeout=3.0)  # Increased timeout slightly for PiCamera
            if self.thread.is_alive():
                logger.warning("PiCamera capture thread did not join in time.")
            else:
                logger.info("PiCamera capture thread joined successfully.")

        # The capture loop's finally block should handle camera.close(),
        # but as a safeguard or if thread never started properly:
        if self.camera and hasattr(self.camera, 'closed') and not self.camera.closed:
            try:
                self.camera.close()
                logger.info("PiCamera closed in stop method (safeguard).")
            except Exception as e:
                logger.error(f"Error closing PiCamera in stop method (safeguard): {e}")
        self.camera = None
        self.frame_bytes = None
        logger.info("CameraHandler (PiCamera) stopped.")


# ========== Communication with ArduinoCarController (Same as previous) ==========
def send_command_to_arduino_controller(command_char):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            s.connect((ARDUINO_CONTROLLER_HOST, ARDUINO_CONTROLLER_PORT))
            s.sendall(command_char.encode('ascii'))
            return f"Command '{command_char}' sent to Arduino Controller."
    except socket.timeout:
        err_msg = f"Timeout sending '{command_char}' to Arduino Controller."
        logger.error(err_msg)
        return err_msg
    except socket.error as e:
        err_msg = f"Socket error sending '{command_char}': {e}"
        logger.error(err_msg)
        return err_msg
    except Exception as e:
        err_msg = f"Unexpected error sending '{command_char}': {e}"
        logger.error(err_msg, exc_info=True)
        return err_msg


def check_arduino_controller_connection():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect((ARDUINO_CONTROLLER_HOST, ARDUINO_CONTROLLER_PORT))
        return True
    except socket.error:
        return False


# ========== Internet Check / Tunnel / Email (Adapted - Same as previous) ==========
def wait_for_internet(timeout=10):
    logger.info("Checking for internet connection...")
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=1)
        logger.info("Internet is available.")
        return True
    except OSError:
        logger.warning("No internet connection for tunnel/email.")
        return False


def start_cloudflared_tunnel():
    global tunnel_url_global, tunnel_process_global
    if not ENABLE_TUNNEL:
        logger.info("Cloudflared tunnel is disabled.")
        return None

    logger.info("Attempting to start cloudflared tunnel...")
    try:
        tunnel_process_global = subprocess.Popen(
            ["cloudflared", "tunnel", "--url", f"http://localhost:{FLASK_PORT}", "--no-autoupdate"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, universal_newlines=True
        )

        url_found = False
        timeout_seconds = 30
        start_time = time.time()

        for line in iter(tunnel_process_global.stdout.readline, ''):
            line = line.strip()
            logger.info(f"Tunnel_Log: {line}")
            url_match = re.search(r"(https://[-a-zA-Z0-9._]+\.trycloudflare\.com)", line)
            if url_match:
                tunnel_url_global = url_match.group(1)
                logger.info(f"✓ Cloudflared tunnel established: {tunnel_url_global}")
                url_found = True
                return tunnel_url_global
            if "failed" in line.lower() or "error" in line.lower():
                logger.error(f"Cloudflared reported an error: {line}")
                break
            if time.time() - start_time > timeout_seconds:
                logger.warning("Timeout waiting for tunnel URL from cloudflared.")
                break

        if not url_found:
            logger.error("❌ Failed to obtain tunnel URL from cloudflared output.")
            if tunnel_process_global:
                tunnel_process_global.terminate()
                try:
                    tunnel_process_global.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    tunnel_process_global.kill()
            tunnel_process_global = None
            return None

    except FileNotFoundError:
        logger.error("❌ cloudflared command not found. Please install it and ensure it's in your PATH.")
        tunnel_process_global = None;
        return None
    except Exception as e:
        logger.error(f"❌ An error occurred while starting cloudflared tunnel: {e}", exc_info=True)
        if tunnel_process_global:
            try:
                tunnel_process_global.terminate(); tunnel_process_global.wait(timeout=2)
            except:
                pass
        tunnel_process_global = None;
        return None


def send_email_notification(url_to_send):
    if not SENDER_EMAIL or "your_email@gmail.com" in SENDER_EMAIL or not RECEIVER_EMAILS or not APP_PASSWORD or "your_gmail_app_password" in APP_PASSWORD:
        logger.warning("Email credentials appear to be default/unset. Skipping email notification.")
        return

    subject = "🚗 Car Control Panel Ready (RPi)"
    body = f"The Car Control Panel (Raspberry Pi) is accessible at:\n{url_to_send}\n\n"
    body += f"If the above is a tunnel URL and stops working, try the local IP (if on the same network):\nhttp://{LOCAL_IP}:{FLASK_PORT}"

    message = MIMEMultipart()
    message["From"] = SENDER_EMAIL
    message["To"] = ", ".join(RECEIVER_EMAILS)
    message["Subject"] = subject
    message.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SENDER_EMAIL, APP_PASSWORD)
            server.sendmail(SENDER_EMAIL, RECEIVER_EMAILS, message.as_string())
        logger.info(f"✅ Email notification sent to {', '.join(RECEIVER_EMAILS)}!")
    except smtplib.SMTPAuthenticationError:
        logger.error("❌ SMTP Authentication Error for email. Check SENDER_EMAIL and APP_PASSWORD.")
    except Exception as e:
        logger.error(f"❌ Failed to send email notification: {e}", exc_info=True)


# ========== HTML TEMPLATE FOR WEB UI (Unchanged) ==========
HTML_WEB_APP_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
    <title>Car Control Panel</title>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 0; background-color: #2c3e50; color: #ecf0f1; display: flex; flex-direction: column; align-items: center; padding-top: 20px; -webkit-tap-highlight-color: transparent; }
        .container { background-color: #34495e; padding: 20px; border-radius: 12px; box-shadow: 0 10px 25px rgba(0,0,0,0.3); width: 90%; max-width: 500px; text-align: center; }
        h1 { color: #1abc9c; margin-top: 0; margin-bottom: 20px; font-size: 2em; }
        .video-container { border: 3px solid #1abc9c; border-radius: 8px; overflow: hidden; background-color: #000; margin-bottom: 25px; aspect-ratio: 4 / 3; display: flex; align-items: center; justify-content: center; }
        .video-container img { display: block; max-width: 100%; max-height: 100%; object-fit: contain; }
        .controls { display: grid; grid-template-areas: ". up ." "left stop right" ". down ."; grid-template-columns: 1fr 1fr 1fr; grid-gap: 10px; margin-bottom: 20px; }
        .control-btn { background-color: #1abc9c; color: #2c3e50; border: none; border-radius: 8px; padding: 20px; font-size: 1.5em; font-weight: bold; cursor: pointer; transition: background-color 0.2s, transform 0.1s; user-select: none; touch-action: manipulation; }
        .control-btn:hover { background-color: #16a085; }
        .control-btn:active { background-color: #117a65; transform: scale(0.95); }
        #btn-up { grid-area: up; } #btn-left { grid-area: left; } #btn-stop { grid-area: stop; background-color: #e74c3c; color: white; }
        #btn-stop:hover { background-color: #c0392b; } #btn-stop:active { background-color: #a93226; }
        #btn-right { grid-area: right; } #btn-down { grid-area: down; }
        .status-area { background-color: #2c3e50; padding: 10px; border-radius: 8px; margin-top: 15px; font-size: 0.9em; }
        .status-area div { margin-bottom: 5px; }
        .status-light { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 8px; background-color: #7f8c8d; /* Grey */ }
        .status-light.on { background-color: #2ecc71; /* Green */ }
        .status-light.off { background-color: #e74c3c; /* Red */ }
        .url-info { font-size: 0.8em; margin-top: 20px; padding: 8px; background-color: rgba(26, 188, 156, 0.1); border: 1px solid #1abc9c; border-radius: 5px; word-break: break-all; }
        .url-info a { color: #1abc9c; text-decoration: none; }
        .url-info a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🕹️ Car Control</h1>
        <div class="video-container">
            <img id="video-stream" src="/video_feed" alt="Video Stream Loading..." onerror="this.alt='Video stream error or stopped.';">
        </div>
        <div class="controls">
            <button class="control-btn" id="btn-up">▲</button>
            <button class="control-btn" id="btn-left">◄</button>
            <button class="control-btn" id="btn-stop">■</button>
            <button class="control-btn" id="btn-right">►</button>
            <button class="control-btn" id="btn-down">▼</button>
        </div>
        <div class="status-area">
            <div id="status-message">Status: Initializing...</div>
            <div><span id="cam-light" class="status-light"></span>Camera: <span id="cam-status">Unknown</span></div>
            <div><span id="arduino-light" class="status-light"></span>Arduino Controller: <span id="arduino-status">Unknown</span></div>
            <div class="url-info" id="url-display">Access URLs will appear here.</div>
        </div>
    </div>

    <script>
        const videoStream = document.getElementById('video-stream');
        const statusMessage = document.getElementById('status-message');
        const camStatus = document.getElementById('cam-status');
        const arduinoStatus = document.getElementById('arduino-status');
        const camLight = document.getElementById('cam-light');
        const arduinoLight = document.getElementById('arduino-light');
        const urlDisplay = document.getElementById('url-display');

        let commandTimeout = null;
        const COMMAND_SEND_INTERVAL = 150; // ms, how often to send command while key/button is held

        function sendControlCommand(direction) {
            statusMessage.textContent = 'Sending: ' + direction.toUpperCase();
            fetch('/api/control/' + direction, { method: 'POST' })
                .then(response => response.json())
                .then(data => {
                    // statusMessage.textContent = 'Status: ' + (data.message || 'N/A');
                    if (data.status !== 'success') {
                        console.error('Control Error:', data.message);
                        statusMessage.textContent = 'Error: ' + data.message;
                    } else {
                         statusMessage.textContent = 'Last: ' + direction.toUpperCase() + ' OK';
                    }
                })
                .catch(error => {
                    statusMessage.textContent = 'Control Request Failed!';
                    console.error('Control Fetch Error:', error);
                });
        }

        function startSendingCommand(direction) {
            stopSendingCommand(); // Clear any existing interval
            sendControlCommand(direction); // Send immediately
            if (direction !== 'stop') {
                 commandTimeout = setInterval(() => sendControlCommand(direction), COMMAND_SEND_INTERVAL);
            }
        }

        function stopSendingCommand() {
            if (commandTimeout) {
                clearInterval(commandTimeout);
                commandTimeout = null;
            }
            sendControlCommand('stop'); // Always send stop when releasing a movement command
        }

        const controlButtons = [
            { id: 'btn-up', direction: 'up' }, { id: 'btn-down', direction: 'down' },
            { id: 'btn-left', direction: 'left' }, { id: 'btn-right', direction: 'right' },
            { id: 'btn-stop', direction: 'stop' }
        ];

        controlButtons.forEach(cb => {
            const btn = document.getElementById(cb.id);
            if (btn) {
                // Mouse events
                btn.addEventListener('mousedown', () => startSendingCommand(cb.direction));
                btn.addEventListener('mouseup', () => { if(cb.direction !== 'stop') stopSendingCommand(); });
                btn.addEventListener('mouseleave', () => { // If mouse leaves button while pressed
                    if (commandTimeout && cb.direction !== 'stop') stopSendingCommand();
                });
                // Touch events
                btn.addEventListener('touchstart', (e) => { e.preventDefault(); startSendingCommand(cb.direction); }, { passive: false });
                btn.addEventListener('touchend', (e) => { e.preventDefault(); if(cb.direction !== 'stop') stopSendingCommand(); });
                btn.addEventListener('touchcancel', (e) => { e.preventDefault(); if(cb.direction !== 'stop') stopSendingCommand(); });
            }
        });

        // Keyboard controls
        const keyMap = { 'ArrowUp': 'up', 'w': 'up', 'ArrowDown': 'down', 's': 'down', 'ArrowLeft': 'left', 'a': 'left', 'ArrowRight': 'right', 'd': 'right', ' ': 'stop', 'Escape': 'stop' };
        let activeKeyCommand = null;

        document.addEventListener('keydown', (e) => {
            if (e.repeat) return;
            const command = keyMap[e.key];
            if (command) {
                e.preventDefault();
                if (activeKeyCommand !== command) { // Prevent re-triggering if key already active
                    startSendingCommand(command);
                    activeKeyCommand = command;
                }
            }
        });
        document.addEventListener('keyup', (e) => {
            const command = keyMap[e.key];
            if (command && command !== 'stop') { // Only send stop if a movement key was released
                e.preventDefault();
                stopSendingCommand();
                activeKeyCommand = null;
            } else if (command === 'stop') { // If space/esc was released, ensure stop is sent if it was the active command
                 e.preventDefault();
                 sendControlCommand('stop'); // Explicitly send stop for stop keys
                 activeKeyCommand = null;
            }
        });

        function updateStatus() {
            fetch('/api/status')
                .then(response => response.json())
                .then(data => {
                    camStatus.textContent = data.camera_running ? `Running (${data.camera_resolution[0]}x${data.camera_resolution[1]} @ ${data.camera_target_fps} FPS)` : 'Not Running/Error';
                    camLight.className = 'status-light ' + (data.camera_running ? 'on' : 'off');

                    arduinoStatus.textContent = data.arduino_controller_status;
                    arduinoLight.className = 'status-light ' + (data.arduino_controller_status === 'Connected' ? 'on' : 'off');

                    let urlHtml = `<strong>Local:</strong> <a href="http://${data.local_ip}:${data.web_port}" target="_blank">http://${data.local_ip}:${data.web_port}</a>`;
                    if (data.tunnel_url) {
                        urlHtml += `<br><strong>Global:</strong> <a href="${data.tunnel_url}" target="_blank">${data.tunnel_url}</a>`;
                    }
                    urlDisplay.innerHTML = urlHtml;

                    // Check if video stream needs reload
                    if (data.camera_running && (videoStream.alt.includes('error') || !videoStream.complete || videoStream.naturalWidth === 0)) {
                        videoStream.src = '/video_feed?' + new Date().getTime(); // Force reload
                        videoStream.alt = 'Video Stream Loading...';
                    }
                })
                .catch(error => {
                    console.error('Status Update Error:', error);
                    statusMessage.textContent = 'Error updating status!';
                    camStatus.textContent = 'Error'; arduinoStatus.textContent = 'Error';
                    camLight.className = 'status-light off'; arduinoLight.className = 'status-light off';
                });
        }

        // Initial status update and periodic updates
        updateStatus();
        setInterval(updateStatus, 5000); // Update status every 5 seconds

        // Reload video stream if it errors out.
        videoStream.onerror = function() {
            console.warn("Video stream error. Attempting reload in 3s.");
            videoStream.alt = 'Video stream error. Retrying...';
            setTimeout(() => {
                videoStream.src = '/video_feed?' + new Date().getTime();
            }, 3000);
        };
    </script>
</body>
</html>
'''


# ========== Flask Routes ==========
@app.route('/')
def web_app_index():
    return render_template_string(HTML_WEB_APP_TEMPLATE)


def generate_video_frames():
    min_stream_interval = 1.0 / 30.0  # Max 30 FPS for stream yield
    placeholder_path = "camera_offline_placeholder.jpg"  # Optional: create this image

    try:
        while flask_running:
            if not camera_handler or not camera_handler.running or not PICAMERA_AVAILABLE:
                # Generate placeholder if OpenCV (cv2) is available
                img_offline_data = None
                try:
                    if os.path.exists(placeholder_path):
                        img_offline_data = cv2.imread(placeholder_path)

                    if img_offline_data is None:  # Fallback: create a dark gray image
                        img_offline_data = cv2.UMat(PICAMERA_RESOLUTION[1], PICAMERA_RESOLUTION[0], cv2.CV_8UC3,
                                                    (20, 20, 20)).get()

                    # Ensure it's a NumPy array for putText
                    if isinstance(img_offline_data, cv2.UMat):
                        img_offline_data = img_offline_data.get()

                    text_to_display = "PiCamera Offline" if PICAMERA_AVAILABLE else "PiCamera Lib Missing"
                    cv2.putText(img_offline_data, text_to_display,
                                (30, PICAMERA_RESOLUTION[1] // 2),
                                cv2.FONT_HERSHEY_SIMPLEX,
                                PICAMERA_RESOLUTION[0] / 640.0,
                                (220, 220, 220), 1, cv2.LINE_AA)

                    _, jpeg_bytes_encoded = cv2.imencode('.jpg', img_offline_data, [int(cv2.IMWRITE_JPEG_QUALITY),
                                                                                    JPEG_QUALITY // 2])  # Lower quality for placeholder
                    frame_bytes = jpeg_bytes_encoded.tobytes()
                except NameError:  # cv2 is not defined (likely opencv-python not installed)
                    logger.warning(
                        "cv2 (OpenCV) not available for placeholder image. Sending empty response for video feed when camera offline.")
                    frame_bytes = b''  # Send empty bytes or a very minimal valid JPEG
                except Exception as e:
                    logger.error(f"Error generating placeholder frame: {e}")
                    frame_bytes = b''

                yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' +
                       frame_bytes + b'\r\n')
                time.sleep(1)  # Send placeholder once per second
                continue

            jpeg_bytes = camera_handler.get_frame_jpeg_bytes()
            if jpeg_bytes:
                yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' +
                       jpeg_bytes + b'\r\n')
                time.sleep(min_stream_interval)  # Control yield rate
            else:
                # Camera running but no new frame yet (or frame too old)
                time.sleep(min_stream_interval / 2.0)
    except GeneratorExit:
        logger.info("Video stream client (browser/app) disconnected.")
    except Exception as e:
        if flask_running:
            logger.error(f"Error in generate_video_frames: {e}", exc_info=True)


@app.route('/video_feed')
def video_feed_route():
    return Response(generate_video_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route('/api/control/<direction>', methods=['POST'])
def api_control_route(direction):
    command_map = {'up': 'F', 'down': 'B', 'left': 'L', 'right': 'R', 'stop': 'S'}
    command_char = command_map.get(direction.lower())

    if command_char:
        message = send_command_to_arduino_controller(command_char)
        logger.info(f"Control API: '{direction}' -> '{command_char}'. Response: {message}")
        return jsonify({"status": "success", "message": message})
    else:
        logger.warning(f"Invalid control direction API: {direction}")
        return jsonify({"status": "error", "message": "Invalid direction"}), 400


@app.route('/api/status')
def api_status_route():
    global tunnel_url_global
    arduino_ctrl_conn = check_arduino_controller_connection()

    cam_running = False
    cam_res = PICAMERA_RESOLUTION  # Default
    cam_fps = PICAMERA_FPS  # Default

    if PICAMERA_AVAILABLE and camera_handler and camera_handler.running:
        cam_running = True
        # PiCamera sets resolution and framerate directly, so we use the handler's stored values
        cam_res = camera_handler.resolution
        cam_fps = camera_handler.framerate
    elif not PICAMERA_AVAILABLE:
        logger.debug("Status API: PiCamera library not available.")

    return jsonify({
        "camera_running": cam_running,
        "camera_resolution": cam_res,
        "camera_target_fps": cam_fps,  # For PiCamera, target is usually actual if set correctly
        "arduino_controller_status": "Connected" if arduino_ctrl_conn else "Disconnected",
        "arduino_controller_target": f"{ARDUINO_CONTROLLER_HOST}:{ARDUINO_CONTROLLER_PORT}",
        "local_ip": LOCAL_IP,
        "web_port": FLASK_PORT,
        "tunnel_url": tunnel_url_global,
        "picamera_available": PICAMERA_AVAILABLE
    })


@app.route('/favicon.ico')
def favicon():
    return ('', 204)


# ========== Main Application Logic ==========
def main():
    global camera_handler, flask_running, tunnel_url_global, tunnel_process_global

    logger.info("Starting Car Control HTTP Server (Web UI & API) for Raspberry Pi...")

    if PICAMERA_AVAILABLE:
        camera_handler = CameraHandler(
            resolution=PICAMERA_RESOLUTION,
            framerate=PICAMERA_FPS,
            jpeg_quality=JPEG_QUALITY
        )
        camera_handler.start()
        if not camera_handler.running:
            logger.warning("PiCamera failed to start. Video stream will show 'Offline'.")
    else:
        logger.error("PiCamera library is not available. Camera features will be disabled.")
        # camera_handler will remain None

    access_url = f"http://{LOCAL_IP}:{FLASK_PORT}"
    if ENABLE_TUNNEL and wait_for_internet():
        public_url = start_cloudflared_tunnel()
        if public_url:
            access_url = public_url

    send_email_notification(access_url)

    logger.info("=" * 60)
    logger.info("🚀 Car Control Server (RPi - Web UI & API) is UP! 🚀")
    logger.info(f"  💻 Web UI & Local API: http://{LOCAL_IP}:{FLASK_PORT}")
    if tunnel_url_global:
        logger.info(f"  🌍 Public Tunnel URL: {tunnel_url_global}")
    else:
        logger.info(f"  🌍 Public Tunnel: Not active or failed to start.")
    logger.info(f"  🔌 Arduino Controller Link: Check {ARDUINO_CONTROLLER_HOST}:{ARDUINO_CONTROLLER_PORT}")
    if PICAMERA_AVAILABLE:
        logger.info(f"  📷 PiCamera: {'Running' if camera_handler and camera_handler.running else 'Not Running/Error'}")
    else:
        logger.info(f"  📷 PiCamera: Library not found/disabled.")
    logger.info("=" * 60)
    print(f"\n--- Car Control Server Ready (Raspberry Pi) ---")
    print(f"Access Web UI at: {access_url}")
    print(f"Local IP for direct access or Flutter app: http://{LOCAL_IP}:{FLASK_PORT}")
    print(f"Press Ctrl+C to shut down.\n")

    try:
        from waitress import serve
        logger.info(f"Starting Waitress server on {FLASK_HOST}:{FLASK_PORT}")
        serve(app, host=FLASK_HOST, port=FLASK_PORT, threads=8)
    except ImportError:
        logger.warning("Waitress not found. Falling back to Flask development server (not for production).")
        logger.warning("Install Waitress with: pip install waitress")
        app.run(host=FLASK_HOST, port=FLASK_PORT, threaded=True, use_reloader=False, debug=False)
    except Exception as e:
        logger.critical(f"Flask/Waitress server failed to start: {e}", exc_info=True)
    finally:
        flask_running = False
        logger.info("Flask/Waitress server has shut down.")

        if camera_handler:  # This check is important
            camera_handler.stop()

        if tunnel_process_global and tunnel_process_global.poll() is None:
            logger.info("Terminating cloudflared tunnel process...")
            tunnel_process_global.terminate()
            try:
                tunnel_process_global.wait(timeout=3)
            except subprocess.TimeoutExpired:
                tunnel_process_global.kill()
            logger.info("Cloudflared tunnel process terminated.")

        logger.info("Car Control HTTP Server shutdown complete.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Ctrl+C pressed by user. Initiating graceful shutdown...")
    finally:
        flask_running = False  # Ensure this is set for all threads
        if camera_handler and camera_handler.running:  # Check if handler exists and is running
            camera_handler.stop()
        if tunnel_process_global and tunnel_process_global.poll() is None:
            logger.info("Final cleanup: Terminating cloudflared tunnel process...")
            tunnel_process_global.terminate()
            try:
                tunnel_process_global.wait(timeout=2)
            except subprocess.TimeoutExpired:
                tunnel_process_global.kill()
            except Exception:
                pass  # Ignore other errors during final cleanup
        logger.info("Application fully terminated.")