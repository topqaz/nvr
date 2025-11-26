import cv2
import time
import threading
import os
from flask import Flask, render_template, send_from_directory, request, Response, abort, send_file, session, redirect, url_for, flash, jsonify
from datetime import datetime
import re
import secrets

app = Flask(__name__)
app.secret_key = secrets.token_hex(16)

USERS = {
    'admin': 'admin'
}

def login_required(f):
    def decorated_function(*args, **kwargs):
        if 'username' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    decorated_function.__name__ = f.__name__
    return decorated_function

CAMERA_INDEX = 0
RECORDINGS_DIR = "recordings"
RECORD_INTERVAL = 60

if not os.path.exists(RECORDINGS_DIR):
    os.makedirs(RECORDINGS_DIR)

camera = cv2.VideoCapture(CAMERA_INDEX)
camera.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

lock = threading.Lock()
frame = None
recording = False

def validate_video_file(file_path):
    try:
        cap = cv2.VideoCapture(file_path)
        if not cap.isOpened():
            return False, "文件无法打开"
        
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        
        cap.release()
        
        if frame_count <= 0 or fps <= 0:
            return False, "文件格式异常"

        file_size = os.path.getsize(file_path)
        if file_size < 1024:  # 小于1KB认为不完整
            return False, "文件过小"
        
        return True, "文件正常"
    except Exception as e:
        return False, f"验证失败: {str(e)}"

def capture_frames():
    global frame
    while True:
        success, img = camera.read()
        if not success:
            print("⚠️ 无法读取摄像头帧，尝试重启摄像头...")
            time.sleep(1)
            camera.release()
            time.sleep(1)
            camera.open(CAMERA_INDEX)
            continue

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cv2.putText(img, timestamp, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        
        with lock:
            frame = img
        time.sleep(0.03)  # 控制帧率约 30fps

def record_video():
    global frame
    while True:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = os.path.join(RECORDINGS_DIR, f"{timestamp}.mp4")
        
        # 优先使用MJPG编码，然后XVID编码，最后使用默认编码
        fourcc = cv2.VideoWriter_fourcc(*'MJPG')
        out = cv2.VideoWriter(filename.replace('.mp4', '.avi'), fourcc, 20.0, (1280, 720))
        
        if not out.isOpened():
            print("⚠️ MJPG编码不支持，尝试使用XVID编码...")
            fourcc = cv2.VideoWriter_fourcc(*'XVID')
            out = cv2.VideoWriter(filename.replace('.mp4', '.avi'), fourcc, 20.0, (1280, 720))
        
        if not out.isOpened():
            print("⚠️ XVID编码不支持，使用默认编码...")
            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            out = cv2.VideoWriter(filename, fourcc, 20.0, (1280, 720))
        
        if not out.isOpened():
            print("❌ 所有编码都不支持，跳过本次录制")
            time.sleep(RECORD_INTERVAL)
            continue
        
        start_time = time.time()
        frame_count = 0

        while time.time() - start_time < RECORD_INTERVAL:
            with lock:
                if frame is not None:
                    # 添加时间戳水印到录制的视频
                    frame_with_timestamp = frame.copy()
                    timestamp_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    cv2.putText(frame_with_timestamp, timestamp_text, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                    out.write(frame_with_timestamp)
                    frame_count += 1
            time.sleep(0.05)

        out.release()
        
        # 验证文件完整性
        final_filename = filename.replace('.mp4', '.avi') if 'MJPG' in str(fourcc) or 'XVID' in str(fourcc) else filename
        is_valid, validation_msg = validate_video_file(final_filename)
        
        if is_valid:
            print(f"✅ 视频录制完成: {final_filename} ({frame_count}帧)")
        else:
            print(f"⚠️ 录制文件可能不完整: {final_filename} - {validation_msg}")

def generate_frames():
    """MJPEG 推流"""
    global frame
    while True:
        with lock:
            if frame is None:
                continue
            ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ret:
            continue
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')

def generate_video_frames(filename, start_time=0):
    """从视频文件生成MJPEG流，支持从指定时间开始"""
    file_path = os.path.join(RECORDINGS_DIR, filename)
    if not os.path.isfile(file_path):
        print(f"❌ 文件不存在: {file_path}")
        return None
    
    cap = cv2.VideoCapture(file_path)
    if not cap.isOpened():
        print(f"❌ 无法打开视频文件: {file_path}")
        return None
    
    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps <= 0:
        fps = 20  # 默认帧率
        print(f"⚠️ 无法获取帧率，使用默认值: {fps}")
    
    frame_delay = 1.0 / fps
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    total_duration = total_frames / fps if fps > 0 else 0
    
    print(f"📊 视频信息: {total_frames}帧, {fps}fps, 总时长: {total_duration:.1f}秒")
    if start_time > 0 and start_time < total_duration:
        print(f"🎬 尝试跳转到: {start_time:.1f}秒")

        success = cap.set(cv2.CAP_PROP_POS_MSEC, start_time * 1000)
        if success:
            actual_time = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000
            print(f"✅ 跳转成功: {actual_time:.1f}秒")
        else:
            print(f"⚠️ 时间戳跳转失败，尝试帧跳转...")
            start_frame = int(start_time * fps)
            cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
            current_frame = int(cap.get(cv2.CAP_PROP_POS_FRAMES))
            print(f"📍 帧跳转后位置: 第{current_frame}帧")
    
    frame_count = 0
    consecutive_failures = 0
    
    while True:
        ret, frame = cap.read()
        if not ret:
            consecutive_failures += 1
            if consecutive_failures > 10:  # 连续失败10次后重新开始
                print("🔄 连续读取失败，重新开始视频")
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                frame_count = 0
                consecutive_failures = 0
                continue
            else:
                time.sleep(0.1)  # 短暂等待
                continue
        
        consecutive_failures = 0
        frame_count += 1
        
        # 调整帧大小以匹配预览
        frame = cv2.resize(frame, (1280, 720))
        
        ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ret:
            continue
        
        # 每100帧打印一次进度（调试用）
        # if frame_count % 100 == 0:
        #     current_time = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000
        #     print(f"📺 当前播放: {current_time:.1f}秒 (第{frame_count}帧)")
        
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
        
        time.sleep(frame_delay)

# 路由定义
@app.route('/')
def index():
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        if username in USERS and USERS[username] == password:
            session['username'] = username
            flash('登录成功！', 'success')
            return redirect(url_for('home'))
        else:
            flash('用户名或密码错误！', 'error')
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('已退出登录', 'info')
    return redirect(url_for('login'))

@app.route('/home')
@login_required
def home():
    return render_template('index.html')

@app.route('/video_feed')
@login_required
def video_feed():
    return Response(generate_frames(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/video_stream/<path:filename>')
@login_required
def video_stream(filename):
    """视频流播放，支持时间参数"""
    start_time = request.args.get('t', 0, type=float)
    return Response(generate_video_frames(filename, start_time),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/video_frame/<path:filename>')
@login_required
def video_frame(filename):
    """获取视频指定时间的单帧"""
    file_path = os.path.join(RECORDINGS_DIR, filename)
    if not os.path.isfile(file_path):
        return abort(404)
    
    time_param = request.args.get('t', 0, type=float)
    
    cap = cv2.VideoCapture(file_path)
    if not cap.isOpened():
        return abort(404)
    
    # 跳转到指定时间
    cap.set(cv2.CAP_PROP_POS_MSEC, time_param * 1000)
    ret, frame = cap.read()
    cap.release()
    
    if not ret:
        return abort(404)
    
    # 调整帧大小
    frame = cv2.resize(frame, (1280, 720))
    ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    
    if not ret:
        return abort(404)
    
    return Response(buffer.tobytes(), mimetype='image/jpeg')

@app.route('/api/video_info/<path:filename>')
@login_required
def get_video_info(filename):
    """获取视频信息API"""
    try:
        file_path = os.path.join(RECORDINGS_DIR, filename)
        if not os.path.isfile(file_path):
            return jsonify({'error': '文件不存在'}), 404
        
        cap = cv2.VideoCapture(file_path)
        if not cap.isOpened():
            return jsonify({'error': '无法打开视频文件'}), 400
        
        fps = cap.get(cv2.CAP_PROP_FPS)
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        duration = frame_count / fps if fps > 0 else 0
        
        cap.release()
        
        return jsonify({
            'duration': duration,
            'frame_count': frame_count,
            'fps': fps,
            'file_size': os.path.getsize(file_path)
        })
    except Exception as e:
        print(f"❌ 获取视频信息失败: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route("/recordings")
@login_required
def recordings():
    # 获取筛选参数
    selected_date = request.args.get("date")
    selected_hour = request.args.get("hour")
    
    # 处理小时参数
    hour_filter = None
    if selected_hour and selected_hour.isdigit():
        hour_filter = int(selected_hour)

    files = []
    if os.path.exists(RECORDINGS_DIR):
        for f in os.listdir(RECORDINGS_DIR):
            # 支持更多视频格式
            if f.lower().endswith((".mp4", ".avi", ".mov", ".mkv", ".webm")):
                path = os.path.join(RECORDINGS_DIR, f)
                
                # 验证文件完整性
                is_valid, validation_msg = validate_video_file(path)
                if not is_valid:
                    print(f"⚠️ 跳过不完整文件: {f} - {validation_msg}")
                    continue
                
                stat = os.stat(path)
                mtime = datetime.fromtimestamp(stat.st_mtime)
                
                # 日期筛选
                if selected_date:
                    file_date = mtime.strftime("%Y-%m-%d")
                    if file_date != selected_date:
                        continue
                
                # 小时筛选
                if hour_filter is not None:
                    file_hour = mtime.hour
                    if file_hour != hour_filter:
                        continue
                
                # 根据文件扩展名确定MIME类型
                file_ext = f.lower().split('.')[-1]
                mime_type = {
                    'mp4': 'video/mp4',
                    'avi': 'video/x-msvideo',
                    'mov': 'video/quicktime',
                    'mkv': 'video/x-matroska',
                    'webm': 'video/webm'
                }.get(file_ext, 'video/mp4')
                
                files.append({
                    "name": f,
                    "size": round(stat.st_size / 1024 / 1024, 2),
                    "time": mtime,
                    "time_str": mtime.strftime("%Y-%m-%d %H:%M:%S"),
                    "mime_type": mime_type,
                    "extension": file_ext,
                    "validation": validation_msg
                })

    # 排序（最新在前）
    files.sort(key=lambda x: x["time"], reverse=True)

    # 分页
    page = int(request.args.get("page", 1))
    per_page = 10
    total = len(files)
    pages = (total + per_page - 1) // per_page
    start = (page - 1) * per_page
    end = start + per_page
    files_page = files[start:end]

    return render_template(
        "recordings.html",
        files=files_page,
        page=page,
        total_pages=pages,
        selected_date=selected_date or "",
        selected_hour=hour_filter,
    )

@app.route("/recordings/<path:filename>")
@login_required
def stream_recording(filename):
    file_path = os.path.join(RECORDINGS_DIR, filename)
    if not os.path.isfile(file_path):
        return abort(404)

    # 根据文件扩展名确定MIME类型
    file_ext = filename.lower().split('.')[-1]
    mime_type = {
        'mp4': 'video/mp4',
        'avi': 'video/x-msvideo',
        'mov': 'video/quicktime',
        'mkv': 'video/x-matroska',
        'webm': 'video/webm'
    }.get(file_ext, 'video/mp4')

    range_header = request.headers.get("Range", None)
    file_size = os.path.getsize(file_path)

    if range_header:
        # 解析 Range 请求
        match = re.search(r'bytes=(\d+)-(\d*)', range_header)
        if match:
            start = int(match.group(1))
            end = match.group(2)
            end = int(end) if end else file_size - 1
        else:
            start, end = 0, file_size - 1
        chunk_size = end - start + 1

        with open(file_path, "rb") as f:
            f.seek(start)
            data = f.read(chunk_size)

        response = Response(data, 206, mimetype=mime_type)
        response.headers.add("Content-Range", f"bytes {start}-{end}/{file_size}")
        response.headers.add("Accept-Ranges", "bytes")
        response.headers.add("Content-Length", str(chunk_size))
        return response

    return send_file(file_path, mimetype=mime_type)

if __name__ == '__main__':
    threading.Thread(target=capture_frames, daemon=True).start()
    threading.Thread(target=record_video, daemon=True).start()
    app.run(host='0.0.0.0', port=5000, debug=False)