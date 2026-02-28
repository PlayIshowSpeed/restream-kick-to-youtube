import webview
import threading
import subprocess
import time
import json
import os

CONFIG_FILE = os.path.join(os.path.expanduser("~"), "restream_config.json")

def load_config():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except:
        return {"kick_url": "", "yt_key": ""}

def save_config(data):
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f)

state = {
    "running": False,
    "is_live": False,
    "start_time": None,
    "restarts": 0,
    "crash_times": [],
    "logs": [],
    "config": load_config()
}

procs = {}
window = None

OFFLINE_CHECK = 30
LIVE_CHECK = 10
FFMPEG_RESTART_DELAY = 3
MAX_RESTARTS = 3
CRASH_WINDOW = 600
MIN_STREAM_DURATION = 120
URL_REFRESH_INTERVAL = 540
YT_RTMP = "rtmp://a.rtmp.youtube.com/live2/"

def log(msg, level="green"):
    t = time.strftime("%H:%M:%S")
    state["logs"].append({"time": t, "msg": msg, "level": level})
    if len(state["logs"]) > 200:
        state["logs"].pop(0)
    print(f"{t}  {msg}")
    if window:
        try:
            safe = msg.replace("'", "\\'").replace("`", "\\`")
            window.evaluate_js(f"addLog('{t}', '{safe}', '{level}')")
        except:
            pass

def get_hls_url():
    try:
        result = subprocess.run(
            ["python", "-m", "streamlink", state["config"]["kick_url"], "--stream-url", "best"],
            capture_output=True, text=True, timeout=30
        )
        url = result.stdout.strip()
        if result.returncode == 0 and url.startswith("http"):
            return url
        return None
    except Exception as e:
        log(f"Streamlink error: {e}", "yellow")
        return None

def build_cmd(hls_url, yt_key):
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-re", "-i", hls_url,
        "-c:v", "h264_amf",
        "-b:v", "6000k", "-minrate", "6000k", "-maxrate", "6000k", "-bufsize", "6000k",
        "-r", "30", "-g", "60",
        "-c:a", "aac", "-b:a", "192k",
        "-f", "flv",
        f"{YT_RTMP}{yt_key}",
    ]

def start_ffmpeg(hls_url):
    global procs
    key = state["config"]["yt_key"]
    if not key:
        log("No YouTube key set — open Settings first", "red")
        state["running"] = False
        return False
    try:
        proc = subprocess.Popen(
            build_cmd(hls_url, key),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        procs["ffmpeg"] = proc
        log(f"FFmpeg started (PID {proc.pid})")
        return True
    except FileNotFoundError:
        log("FFmpeg not found! Install it and add to PATH.", "red")
        state["running"] = False
        return False

def stop_ffmpeg():
    for key, proc in procs.items():
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
    procs.clear()

def update_ui_status():
    if not window:
        return
    try:
        is_live = "true" if state["is_live"] else "false"
        window.evaluate_js(f"updateStatus({is_live}, {state['restarts']})")
    except:
        pass

def stream_loop():
    stream_start_time = None

    if not state["config"]["kick_url"]:
        log("No Kick URL set — open Settings first", "red")
        state["running"] = False
        return

    while state["running"]:
        if state["is_live"] and procs.get("ffmpeg"):
            proc = procs["ffmpeg"]

            if proc.poll() is None:
                duration = int(time.time() - state["start_time"]) if state["start_time"] else 0
                h, m, s = duration // 3600, (duration % 3600) // 60, duration % 60

                if duration > 0 and duration % URL_REFRESH_INTERVAL < LIVE_CHECK:
                    log("Refreshing URL before expiry...", "yellow")
                    fresh_url = get_hls_url()
                    if fresh_url:
                        stop_ffmpeg()
                        time.sleep(2)
                        start_ffmpeg(fresh_url)
                        log("URL refreshed — stream continuing", "green")

                log(f"Streaming — {h}h {m}m {s}s", "muted")
                time.sleep(LIVE_CHECK)
                continue

            now = time.time()
            state["crash_times"] = [t for t in state["crash_times"] if now - t < CRASH_WINDOW]
            state["crash_times"].append(now)

            if len(state["crash_times"]) >= MAX_RESTARTS:
                log("3 crashes in 10 min — stopping", "red")
                stop_ffmpeg()
                state["is_live"] = False
                state["running"] = False
                update_ui_status()
                return

            if stream_start_time and (time.time() - stream_start_time) < MIN_STREAM_DURATION:
                log("Stream under 2 min — skipping restart", "yellow")
                stop_ffmpeg()
                state["is_live"] = False
                update_ui_status()
                time.sleep(OFFLINE_CHECK)
                continue

            state["restarts"] += 1
            log(f"FFmpeg crashed — getting fresh URL ({state['restarts']}/{MAX_RESTARTS})", "yellow")
            time.sleep(FFMPEG_RESTART_DELAY)
            fresh_url = get_hls_url()
            if fresh_url:
                start_ffmpeg(fresh_url)
                update_ui_status()
            else:
                log("Channel went offline", "yellow")
                stop_ffmpeg()
                state["is_live"] = False
                update_ui_status()

            time.sleep(LIVE_CHECK)
            continue

        log("Checking if Kick is live...", "muted")
        url = get_hls_url()

        if url and not state["is_live"]:
            log("Kick LIVE — starting restream now")
            state["is_live"] = True
            state["start_time"] = time.time()
            stream_start_time = time.time()
            state["restarts"] = 0
            state["crash_times"] = []
            start_ffmpeg(url)
            update_ui_status()
            time.sleep(LIVE_CHECK)

        elif not url and state["is_live"]:
            log("Kick ended — stopping restream")
            stop_ffmpeg()
            state["is_live"] = False
            state["start_time"] = None
            stream_start_time = None
            update_ui_status()
            time.sleep(OFFLINE_CHECK)

        else:
            log(f"Kick offline — checking in {OFFLINE_CHECK}s", "muted")
            time.sleep(OFFLINE_CHECK)

class API:
    def start(self):
        if not state["running"]:
            state["running"] = True
            state["restarts"] = 0
            state["crash_times"] = []
            threading.Thread(target=stream_loop, daemon=True).start()
            return "started"
        return "already running"

    def stop(self):
        state["running"] = False
        stop_ffmpeg()
        state["is_live"] = False
        state["start_time"] = None
        update_ui_status()
        log("Stopped by user", "red")
        return "stopped"

    def save_settings(self, kick_url, yt_key):
        state["config"]["kick_url"] = kick_url
        state["config"]["yt_key"] = yt_key
        save_config(state["config"])
        log(f"Settings saved — {kick_url}", "green")
        return "saved"

    def get_config(self):
        return json.dumps(state["config"])

HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Rajdhani:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root{--bg:#080808;--surface:#0f0f0f;--surface2:#141414;--border:#1e1e1e;--accent:#00ff88;--accent2:#00cc6a;--red:#ff3b3b;--text:#e0e0e0;--muted:#444;--mono:'Share Tech Mono',monospace;--sans:'Rajdhani',sans-serif;}
  *{margin:0;padding:0;box-sizing:border-box;}
  body{background:#080808;color:var(--text);font-family:var(--sans);height:100vh;display:flex;flex-direction:column;overflow:hidden;position:relative;}

  /* BACKGROUND IMAGE */
  .bg-image{position:fixed;inset:0;background:url('https://i.pinimg.com/736x/0a/6a/cb/0a6acb3962ac19f143026ecffb3838b9.jpg') center/cover no-repeat;opacity:0.08;z-index:0;pointer-events:none;}

  /* SNOW CANVAS */
  #snowCanvas{position:fixed;inset:0;z-index:1;pointer-events:none;}

  /* All content above snow */
  .titlebar,.main{position:relative;z-index:2;}

  .titlebar{height:38px;background:rgba(10,10,10,0.95);border-bottom:1px solid var(--border);display:flex;align-items:center;padding:0 16px;gap:8px;backdrop-filter:blur(10px);}
  .app-logo{width:14px;height:14px;background:var(--accent);clip-path:polygon(50% 0%,100% 50%,50% 100%,0% 50%);flex-shrink:0;}
  .app-name{font-family:var(--mono);font-size:11px;color:var(--accent);letter-spacing:2px;}
  .main{display:flex;flex:1;overflow:hidden;}
  .sidebar{width:210px;background:rgba(15,15,15,0.92);border-right:1px solid var(--border);display:flex;flex-direction:column;backdrop-filter:blur(10px);}
  .sidebar-section{padding:16px 14px 12px;border-bottom:1px solid var(--border);}
  .sidebar-label{font-family:var(--mono);font-size:9px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;margin-bottom:12px;}
  .status-card{background:rgba(20,20,20,0.9);border:1px solid var(--border);border-radius:8px;padding:12px;}
  .status-row{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;}
  .status-row:last-child{margin-bottom:0;}
  .status-key{font-size:11px;color:var(--muted);}
  .status-val{font-family:var(--mono);font-size:11px;color:var(--text);}
  .badge{display:flex;align-items:center;gap:5px;padding:3px 8px;border-radius:20px;font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;}
  .badge.live{background:rgba(0,255,136,0.1);border:1px solid rgba(0,255,136,0.3);color:var(--accent);}
  .badge.offline{background:rgba(255,59,59,0.1);border:1px solid rgba(255,59,59,0.3);color:var(--red);}
  .dot{width:6px;height:6px;border-radius:50%;}
  .dot.green{background:var(--accent);box-shadow:0 0 6px var(--accent);animation:pulse 1.5s ease-in-out infinite;}
  .dot.red{background:var(--red);}
  @keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:0.5;transform:scale(0.8)}}
  .nav{display:flex;flex-direction:column;gap:2px;padding:10px 8px;}
  .nav-item{display:flex;align-items:center;gap:10px;padding:9px 12px;border-radius:6px;cursor:pointer;font-size:13px;font-weight:500;color:var(--muted);transition:all 0.15s;}
  .nav-item:hover{background:rgba(255,255,255,0.05);color:var(--text);}
  .nav-item.active{background:rgba(0,255,136,0.08);color:var(--accent);}
  .sidebar-stats{margin-top:auto;padding:14px;border-top:1px solid var(--border);}
  .stat-row{display:flex;justify-content:space-between;align-items:center;margin-bottom:7px;}
  .stat-row:last-child{margin-bottom:0;}
  .stat-label{font-size:11px;color:var(--muted);}
  .stat-value{font-family:var(--mono);font-size:11px;color:var(--accent);}
  .content{flex:1;display:flex;flex-direction:column;overflow:hidden;}
  .topbar{height:52px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;padding:0 20px;background:rgba(15,15,15,0.92);backdrop-filter:blur(10px);}
  .topbar-left{display:flex;align-items:center;gap:12px;}
  .channel-name{font-size:15px;font-weight:700;}
  .channel-url{font-family:var(--mono);font-size:10px;color:var(--muted);}
  .topbar-actions{display:flex;gap:8px;}
  .btn{padding:7px 14px;border-radius:6px;border:none;font-family:var(--sans);font-size:12px;font-weight:700;letter-spacing:0.5px;cursor:pointer;text-transform:uppercase;transition:all 0.15s;}
  .btn-primary{background:var(--accent);color:#000;}
  .btn-primary:hover{background:var(--accent2);}
  .btn-danger{background:rgba(255,59,59,0.15);color:var(--red);border:1px solid rgba(255,59,59,0.3);}
  .btn-danger:hover{background:rgba(255,59,59,0.25);}
  .btn-ghost{background:rgba(20,20,20,0.9);color:var(--muted);border:1px solid var(--border);}
  .btn-ghost:hover{color:var(--text);}
  .preview-area{flex:1;display:flex;align-items:center;justify-content:center;padding:20px;background:transparent;position:relative;}
  .stream-frame{width:100%;max-width:860px;aspect-ratio:16/9;background:rgba(10,10,10,0.85);border:1px solid var(--border);border-radius:10px;overflow:hidden;position:relative;box-shadow:0 0 60px rgba(0,0,0,0.8);backdrop-filter:blur(4px);}
  .stream-bg{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:12px;background:linear-gradient(135deg,rgba(13,17,23,0.9),rgba(17,24,32,0.9),rgba(13,17,23,0.9));}
  .stream-icon{font-size:44px;opacity:0.1;}
  .stream-text{font-family:var(--mono);font-size:11px;color:var(--muted);letter-spacing:2px;}
  .scanline{position:absolute;inset:0;background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,0.03) 2px,rgba(0,0,0,0.03) 4px);pointer-events:none;}
  .live-overlay{position:absolute;top:12px;left:12px;display:flex;align-items:center;gap:12px;}
  .live-badge-big{display:flex;align-items:center;gap:6px;background:rgba(0,0,0,0.75);backdrop-filter:blur(8px);border:1px solid rgba(0,255,136,0.4);border-radius:6px;padding:5px 10px;font-family:var(--mono);font-size:11px;color:var(--accent);letter-spacing:2px;}
  .live-badge-big .dot{width:7px;height:7px;}
  .duration-badge{background:rgba(0,0,0,0.75);backdrop-filter:blur(8px);border:1px solid var(--border);border-radius:6px;padding:5px 10px;font-family:var(--mono);font-size:11px;color:var(--text);}
  .quality-badge{position:absolute;top:12px;right:12px;background:rgba(0,0,0,0.75);backdrop-filter:blur(8px);border:1px solid var(--border);border-radius:6px;padding:5px 10px;font-family:var(--mono);font-size:11px;color:var(--text);}
  .stream-bottom{position:absolute;bottom:0;left:0;right:0;height:55px;background:linear-gradient(transparent,rgba(0,0,0,0.8));display:flex;align-items:flex-end;padding:10px 14px;justify-content:space-between;}
  .stream-title-txt{font-size:12px;color:rgba(255,255,255,0.6);}
  .yt-badge{display:flex;align-items:center;gap:5px;font-size:11px;color:rgba(255,255,255,0.4);font-family:var(--mono);}
  .yt-dot{width:6px;height:6px;border-radius:50%;background:#ff4444;}
  .log-panel{height:130px;background:rgba(15,15,15,0.92);border-top:1px solid var(--border);overflow-y:auto;padding:10px 18px;backdrop-filter:blur(10px);}
  .log-panel::-webkit-scrollbar{width:3px;}
  .log-panel::-webkit-scrollbar-thumb{background:var(--border);}
  .log-header{font-family:var(--mono);font-size:9px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;margin-bottom:8px;}
  .log-line{font-family:var(--mono);font-size:11px;line-height:1.8;display:flex;gap:12px;}
  .log-time{color:#333;flex-shrink:0;}
  .log-msg.green{color:var(--accent);}
  .log-msg.muted{color:#555;}
  .log-msg.yellow{color:#f0c040;}
  .log-msg.red{color:var(--red);}
  .settings-panel{position:fixed;inset:0;background:rgba(0,0,0,0.85);backdrop-filter:blur(12px);display:none;align-items:center;justify-content:center;z-index:100;}
  .settings-card{background:rgba(15,15,15,0.98);border:1px solid var(--border);border-radius:14px;width:460px;overflow:hidden;box-shadow:0 40px 80px rgba(0,0,0,0.8);}
  .settings-header{padding:18px 22px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;}
  .settings-title{font-size:15px;font-weight:700;}
  .settings-close{width:26px;height:26px;border-radius:6px;background:var(--surface2);border:1px solid var(--border);color:var(--muted);cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:13px;}
  .settings-close:hover{color:var(--text);}
  .settings-body{padding:20px 22px;display:flex;flex-direction:column;gap:16px;}
  .field{display:flex;flex-direction:column;gap:6px;}
  .field-label{font-size:11px;font-weight:600;color:var(--muted);letter-spacing:1px;text-transform:uppercase;}
  .field-input{background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:10px 12px;font-family:var(--mono);font-size:12px;color:var(--text);outline:none;width:100%;transition:border-color 0.15s;}
  .field-input:focus{border-color:rgba(0,255,136,0.4);}
  .field-input::placeholder{color:#333;}
  .field-hint{font-size:10px;color:#333;font-family:var(--mono);}
  .settings-footer{padding:14px 22px;border-top:1px solid var(--border);display:flex;justify-content:flex-end;gap:8px;}
</style>
</head>
<body>

<div class="bg-image"></div>
<canvas id="snowCanvas"></canvas>

<div class="titlebar">
  <div class="app-logo"></div>
  <span class="app-name">RESTREAM</span>
</div>
<div class="main">
  <div class="sidebar">
    <div class="sidebar-section">
      <div class="sidebar-label">Status</div>
      <div class="status-card">
        <div class="status-row"><span class="status-key">Channel</span><span class="status-val" id="sideChannelName">not set</span></div>
        <div class="status-row"><span class="status-key">Stream</span><div class="badge offline" id="statusBadge"><div class="dot red" id="statusDot"></div><span id="statusText">OFFLINE</span></div></div>
        <div class="status-row"><span class="status-key">Quality</span><span class="status-val">1080p30</span></div>
        <div class="status-row"><span class="status-key">Encoder</span><span class="status-val">AMD AMF</span></div>
      </div>
    </div>
    <nav class="nav">
      <div class="nav-item active">▶ &nbsp;Dashboard</div>
      <div class="nav-item" onclick="openSettings()">⚙ &nbsp;Settings</div>
    </nav>
    <div class="sidebar-stats">
      <div class="sidebar-label">Session</div>
      <div class="stat-row"><span class="stat-label">Duration</span><span class="stat-value" id="timerDisplay">00:00:00</span></div>
      <div class="stat-row"><span class="stat-label">Restarts</span><span class="stat-value" id="restartsDisplay">0 / 3</span></div>
    </div>
  </div>
  <div class="content">
    <div class="topbar">
      <div class="topbar-left">
        <div class="badge offline" id="topBadge"><div class="dot red" id="topDot"></div><span id="topText">OFFLINE</span></div>
        <div>
          <div class="channel-name" id="topChannelName">Not Set</div>
          <div class="channel-url" id="topChannelUrl">open settings to configure</div>
        </div>
      </div>
      <div class="topbar-actions">
        <button class="btn btn-ghost" onclick="openSettings()">⚙ Settings</button>
        <button class="btn btn-danger" onclick="stopStream()">■ Stop</button>
        <button class="btn btn-primary" onclick="startStream()">▶ Start</button>
      </div>
    </div>
    <div class="preview-area">
      <div class="stream-frame">
        <div class="stream-bg">
          <div class="stream-icon">📺</div>
          <div class="stream-text" id="previewText">WAITING FOR STREAM</div>
        </div>
        <div class="scanline"></div>
        <div class="live-overlay">
          <div class="live-badge-big" id="liveBadgeBig" style="display:none"><div class="dot green"></div> LIVE</div>
          <div class="duration-badge" id="overlayTimer" style="display:none">00:00:00</div>
        </div>
        <div class="quality-badge">1080p · 30fps · AMD AMF</div>
        <div class="stream-bottom">
          <span class="stream-title-txt" id="streamTitle">Configure in Settings</span>
          <div class="yt-badge"><div class="yt-dot"></div> YouTube</div>
        </div>
      </div>
    </div>
    <div class="log-panel" id="logPanel">
      <div class="log-header">Console Output</div>
      <div id="logContainer"></div>
    </div>
  </div>
</div>

<div class="settings-panel" id="settingsPanel">
  <div class="settings-card">
    <div class="settings-header">
      <span class="settings-title">Settings</span>
      <button class="settings-close" onclick="closeSettings()">✕</button>
    </div>
    <div class="settings-body">
      <div class="field">
        <label class="field-label">Kick Stream URL</label>
        <input class="field-input" id="inputKickUrl" type="text" placeholder="https://kick.com/channelname">
        <span class="field-hint">// paste the full kick.com channel URL</span>
      </div>
      <div class="field">
        <label class="field-label">YouTube Stream Key</label>
        <input class="field-input" id="inputYtKey" type="password" placeholder="xxxx-xxxx-xxxx-xxxx-xxxx">
        <span class="field-hint">// YouTube Studio → Go Live → Stream key</span>
      </div>
    </div>
    <div class="settings-footer">
      <button class="btn btn-ghost" onclick="closeSettings()">Cancel</button>
      <button class="btn btn-primary" onclick="saveSettings()">Save</button>
    </div>
  </div>
</div>

<script>
  // SNOW
  const canvas = document.getElementById('snowCanvas');
  const ctx = canvas.getContext('2d');
  let flakes = [];

  function resize() {
    canvas.width = window.innerWidth;
    canvas.height = window.innerHeight;
  }
  resize();
  window.addEventListener('resize', resize);

  for (let i = 0; i < 120; i++) {
    flakes.push({
      x: Math.random() * window.innerWidth,
      y: Math.random() * window.innerHeight,
      r: Math.random() * 3 + 1,
      speed: Math.random() * 0.6 + 0.2,
      drift: Math.random() * 0.4 - 0.2,
      opacity: Math.random() * 0.5 + 0.2
    });
  }

  function drawSnow() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    flakes.forEach(f => {
      ctx.beginPath();
      ctx.arc(f.x, f.y, f.r, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(255,255,255,${f.opacity})`;
      ctx.fill();
      f.y += f.speed;
      f.x += f.drift;
      if (f.y > canvas.height) { f.y = -5; f.x = Math.random() * canvas.width; }
      if (f.x > canvas.width) f.x = 0;
      if (f.x < 0) f.x = canvas.width;
    });
    requestAnimationFrame(drawSnow);
  }
  drawSnow();

  // APP
  let timerInterval=null,startTime=null;

  function addLog(time,msg,level){
    const c=document.getElementById('logContainer');
    const l=document.createElement('div');
    l.className='log-line';
    l.innerHTML='<span class="log-time">'+time+'</span><span class="log-msg '+level+'">'+msg+'</span>';
    c.appendChild(l);
    document.getElementById('logPanel').scrollTop=99999;
  }

  function updateStatus(isLive,restarts){
    document.getElementById('restartsDisplay').textContent=restarts+' / 3';
    ['statusBadge','topBadge'].forEach(id=>{document.getElementById(id).className='badge '+(isLive?'live':'offline');});
    ['statusDot','topDot'].forEach(id=>{document.getElementById(id).className='dot '+(isLive?'green':'red');});
    document.getElementById('statusText').textContent=isLive?'LIVE':'OFFLINE';
    document.getElementById('topText').textContent=isLive?'LIVE':'OFFLINE';
    document.getElementById('liveBadgeBig').style.display=isLive?'flex':'none';
    document.getElementById('overlayTimer').style.display=isLive?'block':'none';
    document.getElementById('previewText').textContent=isLive?'RESTREAMING TO YOUTUBE':'WAITING FOR STREAM';
    if(isLive&&!timerInterval){startTime=Date.now();timerInterval=setInterval(tickTimer,1000);}
    if(!isLive&&timerInterval){clearInterval(timerInterval);timerInterval=null;document.getElementById('timerDisplay').textContent='00:00:00';}
  }

  function tickTimer(){
    const e=Math.floor((Date.now()-startTime)/1000);
    const t=String(Math.floor(e/3600)).padStart(2,'0')+':'+String(Math.floor((e%3600)/60)).padStart(2,'0')+':'+String(e%60).padStart(2,'0');
    document.getElementById('timerDisplay').textContent=t;
    document.getElementById('overlayTimer').textContent=t;
  }

  function startStream(){window.pywebview.api.start();}
  function stopStream(){window.pywebview.api.stop();}

  function openSettings(){
    window.pywebview.api.get_config().then(cfg=>{
      const c=JSON.parse(cfg);
      document.getElementById('inputKickUrl').value=c.kick_url||'';
      document.getElementById('inputYtKey').value=c.yt_key||'';
      document.getElementById('settingsPanel').style.display='flex';
    });
  }

  function closeSettings(){document.getElementById('settingsPanel').style.display='none';}

  function saveSettings(){
    const kick=document.getElementById('inputKickUrl').value.trim();
    const yt=document.getElementById('inputYtKey').value.trim();
    const slug=kick.replace('https://kick.com/','');
    document.getElementById('sideChannelName').textContent=slug||'not set';
    document.getElementById('topChannelName').textContent=slug?slug.charAt(0).toUpperCase()+slug.slice(1):'Not Set';
    document.getElementById('topChannelUrl').textContent=slug?'kick.com/'+slug:'open settings to configure';
    document.getElementById('streamTitle').textContent=slug?slug.charAt(0).toUpperCase()+slug.slice(1)+' Live Stream':'Configure in Settings';
    window.pywebview.api.save_settings(kick,yt);
    closeSettings();
  }
</script>
</body>
</html>"""

window = None

def main():
    global window
    api = API()
    window = webview.create_window(
        "ReStream",
        html=HTML,
        js_api=api,
        width=1100,
        height=720,
        min_size=(900, 600),
        background_color="#080808",
    )
    webview.start(debug=False)

if __name__ == "__main__":
    main()
