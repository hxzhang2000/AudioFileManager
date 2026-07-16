"""Web 管理服务器（§12）。

通过 Flask + waitress 在局域网提供 REST API 与 Web UI，
让用户可以通过浏览器查看应用状态、控制批处理、浏览目录加载文件。

使用方式::

    from services.web_server import get_bridge, WebServer

    bridge = get_bridge()
    bridge.bind(main_window)

    server = WebServer(config)
    server.start()

架构::

    Web Browser (LAN)
         ↕ HTTP
    Flask (waitress, daemon thread)
         ↕ Bridge (thread-safe)
    PyQt6 MainWindow + BatchProcessor
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from utils.logger import logger

# ============================================================
# Flask / waitress 依赖 — 缺失时静默降级
# ============================================================
try:
    from flask import Flask, Response, jsonify, request, stream_with_context
except ImportError:
    Flask = None  # type: ignore
    jsonify = None  # type: ignore

try:
    import waitress
except ImportError:
    waitress = None  # type: ignore


# ============================================================
# 音频文件扩展名（与 file_list_widget 保持一致）
# ============================================================
_AUDIO_EXTENSIONS = {
    ".mp3", ".flac", ".m4a", ".ogg", ".wma", ".ape", ".wav", ".aac"
}


# ============================================================
# 线程安全状态桥 — 单例
# ============================================================

@dataclass
class ServerStatus:
    """服务端当前状态的快照，由 Qt 主线程定时更新，Flask 线程只读。"""
    # 应用状态
    app_running: bool = True
    batch_running: bool = False
    batch_paused: bool = False
    # 批处理进度
    current: int = 0
    total: int = 0
    current_file: str = ""
    step_name: str = ""
    # 统计
    done_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    total_files: int = 0  # 文件列表中文件总数
    checked_files: int = 0  # 勾选的文件数
    # 服务器信息
    server_port: int = 8080
    server_host: str = "0.0.0.0"
    server_running: bool = False
    elapsed_seconds: float = 0.0


class _WebBridge:
    """线程安全的桥接层，连接 Flask 线程与 Qt 主线程。

    单例模式，通过 :func:`get_bridge` 获取实例。

    Qt 主线程负责调用 :meth:`update_status` 定期推送状态快照；
    Flask 线程通过 :meth:`get_status` 读取最新状态。

    动作（Flask → Qt）通过 ``_action_queue`` 传递，Qt 主线程
    通过 :meth:`drain_actions` 轮询执行。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._status = ServerStatus()
        self._action_queue: queue.SimpleQueue[dict] = queue.SimpleQueue()
        # SSE 事件队列：每个连接的客户端对应一个队列
        self._sse_clients: list[queue.SimpleQueue] = []
        self._sse_lock = threading.Lock()

        # Qt 对象引用（仅供 invokeMethod 使用，不跨线程直接访问属性）
        self._main_window: Any = None
        self._file_list_widget: Any = None
        self._batch_processor: Any = None

        # 动作注册表：action_name → callable
        self._actions: dict[str, Callable] = {}

        # 文件列表快照（Qt 主线程定时缓存，Flask 线程只读）
        self._cached_files: list[dict] = []

    # ------------------------------------------------------------
    # 绑定（Qt 主线程调用）
    # ------------------------------------------------------------
    def bind(self, main_window=None, file_list_widget=None,
             batch_processor=None) -> None:
        """绑定 UI 组件引用。"""
        self._main_window = main_window
        self._file_list_widget = file_list_widget
        self._batch_processor = batch_processor

    def register_action(self, name: str, callback: Callable) -> None:
        """注册一个可由 Web API 触发的动作。"""
        self._actions[name] = callback

    # ------------------------------------------------------------
    # 状态更新（Qt 主线程调用）
    # ------------------------------------------------------------
    def update_status(self, **kwargs) -> None:
        """更新状态快照字段。"""
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self._status, k):
                    setattr(self._status, k, v)

    def get_status(self) -> ServerStatus:
        """获取当前状态快照（线程安全）。"""
        with self._lock:
            return self._status

    # ------------------------------------------------------------
    # 文件列表快照（Qt 主线程缓存，Flask 线程只读）
    # ------------------------------------------------------------
    def set_cached_files(self, files: list[dict]) -> None:
        """Qt 主线程调用：缓存文件列表快照。"""
        with self._lock:
            self._cached_files = list(files)

    def get_cached_files(self) -> list[dict]:
        """Flask 线程调用：获取缓存的文件列表（线程安全）。"""
        with self._lock:
            return list(self._cached_files)

    # ------------------------------------------------------------
    # 动作队列（Flask 线程入队，Qt 主线程出队执行）
    # ------------------------------------------------------------
    def enqueue_action(self, action: str, params: dict | None = None) -> None:
        """Flask 线程调用：将一个动作加入队列。"""
        self._action_queue.put({"action": action, "params": params or {}})

    def drain_actions(self) -> list[dict]:
        """Qt 主线程调用：取出所有待处理动作。"""
        actions: list[dict] = []
        while not self._action_queue.empty():
            try:
                actions.append(self._action_queue.get_nowait())
            except queue.Empty:
                break
        return actions

    def execute_action(self, action_name: str, params: dict) -> Any:
        """直接执行一个已注册的动作（在调用线程中）。"""
        cb = self._actions.get(action_name)
        if cb:
            try:
                return cb(**params)
            except Exception as e:
                logger.error(f"Web action '{action_name}' failed: {e}")
                return {"error": str(e)}
        return {"error": f"unknown action: {action_name}"}

    # ------------------------------------------------------------
    # SSE 事件（服务端推送）
    # ------------------------------------------------------------
    def subscribe_sse(self) -> queue.SimpleQueue:
        """为 SSE 客户端注册一个事件队列。"""
        q: queue.SimpleQueue = queue.SimpleQueue()
        with self._sse_lock:
            self._sse_clients.append(q)
        return q

    def unsubscribe_sse(self, q: queue.SimpleQueue) -> None:
        """移除 SSE 客户端队列。"""
        with self._sse_lock:
            if q in self._sse_clients:
                self._sse_clients.remove(q)

    def broadcast_sse(self, event: str, data: Any) -> None:
        """向所有 SSE 客户端推送事件。"""
        payload = json.dumps(data, ensure_ascii=False)
        with self._sse_lock:
            dead: list[queue.SimpleQueue] = []
            for q in self._sse_clients:
                try:
                    q.put_nowait((event, payload))
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self._sse_clients.remove(q)

    def broadcast_status(self) -> None:
        """推送当前状态到所有 SSE 客户端。"""
        status = self.get_status()
        self.broadcast_sse("status", {
            "app_running": status.app_running,
            "batch_running": status.batch_running,
            "batch_paused": status.batch_paused,
            "current": status.current,
            "total": status.total,
            "current_file": status.current_file,
            "step_name": status.step_name,
            "done_count": status.done_count,
            "skipped_count": status.skipped_count,
            "failed_count": status.failed_count,
            "total_files": status.total_files,
            "checked_files": status.checked_files,
            "server_running": status.server_running,
            "elapsed_seconds": status.elapsed_seconds,
        })


# 全局实例
_bridge_instance: _WebBridge | None = None
_bridge_lock = threading.Lock()


def get_bridge() -> _WebBridge:
    """获取全局唯一 WebBridge 实例。"""
    global _bridge_instance
    if _bridge_instance is None:
        with _bridge_lock:
            if _bridge_instance is None:
                _bridge_instance = _WebBridge()
    return _bridge_instance


# ============================================================
# Web UI — 内嵌单页应用
# ============================================================

_WEB_UI_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>AudioFileManager</title>
<style>
  :root {
    --bg: #1a1a2e; --bg2: #16213e; --card: #0f3460;
    --accent: #00b4d8; --text: #e0e0e0; --muted: #888;
    --green: #00c853; --red: #ff5252; --amber: #ffab00;
    --radius: 8px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         background: var(--bg); color: var(--text); min-height: 100vh; }
  .nav { background: var(--bg2); padding: 12px 24px; display: flex;
         gap: 20px; align-items: center; flex-wrap: wrap; border-bottom: 1px solid #2a2a4e; }
  .nav h1 { font-size: 18px; font-weight: 600; color: var(--accent); margin-right: auto; }
  .nav a { color: var(--muted); text-decoration: none; padding: 6px 14px;
           border-radius: var(--radius); cursor: pointer; }
  .nav a:hover, .nav a.active { background: var(--card); color: var(--text); }
  .page { display: none; padding: 20px; max-width: 1000px; margin: 0 auto; }
  .page.active { display: block; }

  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 20px; }
  .card { background: var(--bg2); border-radius: var(--radius); padding: 16px; }
  .card .label { font-size: 12px; color: var(--muted); margin-bottom: 4px; }
  .card .value { font-size: 24px; font-weight: 700; }
  .card .value.green { color: var(--green); }
  .card .value.red { color: var(--red); }
  .card .value.amber { color: var(--amber); }
  .card .value.accent { color: var(--accent); }

  .progress-section { background: var(--bg2); border-radius: var(--radius); padding: 16px; margin-bottom: 16px; }
  .progress-section .bar-wrap { background: #2a2a4e; border-radius: 4px; height: 20px; overflow: hidden; margin: 8px 0; }
  .progress-section .bar-fill { height: 100%; background: var(--accent); transition: width .3s; border-radius: 4px; }
  .progress-section .bar-fill.paused { background: var(--amber); }
  .bar-text { font-size: 12px; color: var(--muted); text-align: center; padding: 2px 0; }

  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: 600; }
  .badge.running { background: var(--green); color: #fff; }
  .badge.paused { background: var(--amber); color: #000; }
  .badge.stopped { background: var(--muted); color: #fff; }
  .badge.idle { background: #333; color: var(--muted); }

  .btn { background: var(--card); color: var(--text); border: none; padding: 8px 20px;
         border-radius: var(--radius); cursor: pointer; font-size: 14px; }
  .btn:hover { filter: brightness(1.2); }
  .btn:disabled { opacity: .4; cursor: not-allowed; }
  .btn-primary { background: var(--accent); color: #fff; }
  .btn-danger { background: var(--red); color: #fff; }
  .btn-success { background: var(--green); color: #fff; }

  .controls { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }

  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { padding: 8px 12px; text-align: left; border-bottom: 1px solid #2a2a4e; }
  th { color: var(--muted); font-weight: 500; position: sticky; top: 0; background: var(--bg); }
  .file-table-wrap { max-height: 60vh; overflow-y: auto; border: 1px solid #2a2a4e; border-radius: var(--radius); }

  .dir-grid { display: flex; flex-wrap: wrap; gap: 4px; margin: 12px 0; }
  .dir-item { background: var(--bg2); border: 1px solid #2a2a4e; border-radius: var(--radius);
              padding: 6px 12px; cursor: pointer; font-size: 13px; }
  .dir-item:hover { border-color: var(--accent); }
  .dir-item.dir { color: var(--accent); }
  .dir-item.file { color: var(--text); }
  .dir-bar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 12px; }
  .dir-bar input { flex: 1; min-width: 200px; background: var(--bg2); border: 1px solid #2a2a4e;
                   color: var(--text); padding: 8px 12px; border-radius: var(--radius); }
  .log-box { background: #111; border-radius: var(--radius); padding: 8px 12px;
             font-family: monospace; font-size: 12px; max-height: 200px; overflow-y: auto;
             white-space: pre-wrap; word-break: break-all; color: #aaa; }
  @media (max-width: 600px) { .nav { padding: 8px 12px; gap: 8px; } .cards { grid-template-columns: 1fr 1fr; } }
</style>
</head>
<body>
<div class="nav">
  <h1>AudioFileManager</h1>
  <a class="active" data-page="dashboard" onclick="switchPage('dashboard')">概览</a>
  <a data-page="files" onclick="switchPage('files')">文件列表</a>
  <a data-page="browse" onclick="switchPage('browse')">浏览目录</a>
  <a data-page="log" onclick="switchPage('log')">日志</a>
  <span id="status-badge" class="badge idle">未连接</span>
</div>

<!-- ==================== 概览页 ==================== -->
<div id="page-dashboard" class="page active">
  <div class="cards">
    <div class="card"><div class="label">文件总数</div><div class="value accent" id="stat-total">0</div></div>
    <div class="card"><div class="label">已勾选</div><div class="value accent" id="stat-checked">0</div></div>
    <div class="card"><div class="label">已完成</div><div class="value green" id="stat-done">0</div></div>
    <div class="card"><div class="label">已跳过</div><div class="value amber" id="stat-skipped">0</div></div>
    <div class="card"><div class="label">失败</div><div class="value red" id="stat-failed">0</div></div>
    <div class="card"><div class="label">服务端口</div><div class="value accent" id="stat-port">-</div></div>
  </div>

  <div class="progress-section" id="batch-section">
    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
      <span><strong>批处理状态</strong></span>
      <span id="batch-status-label" class="badge idle">空闲</span>
    </div>
    <div style="margin:8px 0;font-size:13px;color:var(--muted)" id="batch-current-file"></div>
    <div class="bar-wrap">
      <div class="bar-fill" id="progress-bar" style="width:0%"></div>
    </div>
    <div class="bar-text" id="progress-text">0 / 0</div>
    <div class="controls">
      <button class="btn btn-success" id="btn-start" onclick="doAction('batch_start')">启动批处理</button>
      <button class="btn btn-primary" id="btn-pause" onclick="doAction('batch_pause')" disabled>暂停</button>
      <button class="btn btn-danger" id="btn-stop" onclick="doAction('batch_stop')" disabled>停止</button>
    </div>
  </div>
</div>

<!-- ==================== 文件列表页 ==================== -->
<div id="page-files" class="page">
  <div style="margin-bottom:8px"><strong>文件列表</strong> <span style="color:var(--muted);font-size:13px" id="files-count"></span></div>
  <div class="file-table-wrap" id="files-table-wrap">
    <table><thead><tr><th>文件名</th><th>大小</th><th>状态</th></tr></thead><tbody id="files-tbody"></tbody></table>
  </div>
</div>

<!-- ==================== 浏览目录页 ==================== -->
<div id="page-browse" class="page">
  <p style="color:var(--muted);margin-bottom:8px;font-size:13px">浏览服务端目录，选择文件夹加载音频文件到应用。</p>
  <div class="dir-bar">
    <input id="path-input" type="text" placeholder="输入路径或点击下方条目导航..." />
    <button class="btn" onclick="browseDir()">浏览</button>
    <button class="btn btn-success" onclick="loadDir()">加载到此目录</button>
  </div>
  <div class="dir-grid" id="dir-items"></div>
  <p style="color:var(--muted);margin-top:8px;font-size:12px" id="dir-path-display"></p>
</div>

<!-- ==================== 日志页 ==================== -->
<div id="page-log" class="page">
  <div style="display:flex;justify-content:space-between;margin-bottom:8px">
    <strong>运行日志</strong>
    <button class="btn" onclick="document.getElementById('log-box').textContent=''">清空</button>
  </div>
  <div class="log-box" id="log-box"></div>
</div>

<script>
let currentDir = "";
const PAGE_SIZE = 200;

function switchPage(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav a').forEach(a => a.classList.remove('active'));
  document.getElementById('page-'+name).classList.add('active');
  document.querySelector(`.nav a[data-page="${name}"]`).classList.add('active');
  if (name === 'files') loadFiles();
  if (name === 'browse') {
    var saved = localStorage.getItem('webDirPath');
    if (saved) {
      document.getElementById('path-input').value = saved;
      browseDir(saved);
    }
  }
}

function updateBadge(status) {
  const badge = document.getElementById('status-badge');
  if (status.batch_running) {
    if (status.batch_paused) {
      badge.className = 'badge paused'; badge.textContent = '已暂停';
    } else {
      badge.className = 'badge running'; badge.textContent = '批处理中';
    }
  } else if (status.server_running) {
    badge.className = 'badge idle'; badge.textContent = '就绪';
  } else {
    badge.className = 'badge idle'; badge.textContent = '未连接';
  }
}

function updateUI(status) {
  document.getElementById('stat-total').textContent = status.total_files;
  document.getElementById('stat-checked').textContent = status.checked_files;
  document.getElementById('stat-done').textContent = status.done_count;
  document.getElementById('stat-skipped').textContent = status.skipped_count;
  document.getElementById('stat-failed').textContent = status.failed_count;
  document.getElementById('stat-port').textContent = status.server_running ? status.server_host+':'+status.server_port : '-';

  const batchLabel = document.getElementById('batch-status-label');
  if (status.batch_running) {
    batchLabel.textContent = status.batch_paused ? '已暂停' : '运行中';
    batchLabel.className = 'badge ' + (status.batch_paused ? 'paused' : 'running');
  } else {
    batchLabel.textContent = '空闲';
    batchLabel.className = 'badge idle';
  }

  if (status.batch_running && status.total > 0) {
    const pct = Math.min(100, Math.round(status.current / status.total * 100));
    document.getElementById('progress-bar').style.width = pct + '%';
    document.getElementById('progress-bar').className = 'bar-fill' + (status.batch_paused ? ' paused' : '');
    document.getElementById('progress-text').textContent = status.current + ' / ' + status.total + '  —  ' + (status.step_name || '');
    document.getElementById('batch-current-file').textContent = status.current_file || '';
  } else if (status.total > 0 && !status.batch_running) {
    document.getElementById('progress-bar').style.width = '100%';
    document.getElementById('progress-bar').className = 'bar-fill';
    document.getElementById('progress-text').textContent = status.total + ' / ' + status.total + '  —  已完成';
    document.getElementById('batch-current-file').textContent = '';
  } else {
    document.getElementById('progress-bar').style.width = '0%';
    document.getElementById('progress-text').textContent = '0 / 0';
    document.getElementById('batch-current-file').textContent = '等待开始批处理...';
  }

  document.getElementById('btn-start').disabled = status.batch_running;
  document.getElementById('btn-pause').disabled = !status.batch_running;
  document.getElementById('btn-pause').textContent = status.batch_paused ? '继续' : '暂停';
  document.getElementById('btn-stop').disabled = !status.batch_running;

  updateBadge(status);
}

// ---------- SSE 实时推送 ----------
let sseRetry = 0;
function connectSSE() {
  const evtSource = new EventSource('/api/events');
  evtSource.onopen = () => { sseRetry = 0; };
  evtSource.addEventListener('status', (e) => {
    try { updateUI(JSON.parse(e.data)); } catch(ex) {}
  });
  evtSource.addEventListener('log', (e) => {
    try {
      const data = JSON.parse(e.data);
      showLog('['+data.time+'] ['+data.level+'] '+data.message);
    } catch(ex) {}
  });
  evtSource.addEventListener('message', (e) => {
    // keepalive
  });
  evtSource.onerror = () => {
    evtSource.close();
    const delay = Math.min(3000, 500 * (1 << sseRetry));
    sseRetry = Math.min(sseRetry + 1, 5);
    setTimeout(connectSSE, delay);
  };
}

// ---------- 轮询（SSE 断连时的 fallback） ----------
let pollInterval = null;
function startPoll() {
  if (pollInterval) return;
  pollInterval = setInterval(() => {
    fetch('/api/status').then(r => r.json()).then(updateUI).catch(() => {});
  }, 2000);
}

// 同时启用 SSE 和轮询 fallback
connectSSE();
startPoll();

// ---------- 动作 ----------
function doAction(action) {
  fetch('/api/action', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({action})
  }).then(r => r.json()).then(data => {
    if (data.error) showLog('[错误] '+data.error);
  }).catch(err => showLog('[错误] '+err));
}

// ---------- 文件列表 ----------
function formatSize(bytes) {
  if (!bytes || bytes <= 0) return '';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return (bytes/1024).toFixed(1) + ' KB';
  return (bytes/1048576).toFixed(1) + ' MB';
}
function loadFiles() {
  fetch('/api/files').then(r => r.json()).then(data => {
    const tbody = document.getElementById('files-tbody');
    const files = data.files || [];
    document.getElementById('files-count').textContent = '共 ' + files.length + ' 个文件' +
      (data.total ? '（总' + data.total + '，已勾选' + data.checked + '）' : '');
    tbody.innerHTML = files.map(f =>
      `<tr><td>${escapeHtml(f.name)}</td><td>${formatSize(f.size)}</td><td>${f.status || '等待'}</td></tr>`
    ).join('');
  }).catch(() => {});
}

// ---------- 目录浏览 ----------
function browseDir(path) {
  const input = document.getElementById('path-input');
  path = path || input.value.trim() || '.';
  input.value = path;
  currentDir = path;
  localStorage.setItem('webDirPath', path);

  fetch('/api/directory?path='+encodeURIComponent(path))
    .then(r => r.json()).then(data => {
      const cont = document.getElementById('dir-items');
      document.getElementById('dir-path-display').textContent = data.path || path;
      if (data.error) { cont.innerHTML = '<span style="color:var(--red)">'+escapeHtml(data.error)+'</span>'; return; }
      let html = '';
      if (data.parent) {
        html += `<div class="dir-item dir" onclick="browseDir('${escapeHtml(data.parent)}')">..</div>`;
      }
      (data.dirs || []).forEach(d => {
        html += `<div class="dir-item dir" onclick="browseDir('${escapeHtml(data.path)}/${escapeHtml(d)}')">${escapeHtml(d)}</div>`;
      });
      (data.files || []).forEach(f => {
        html += `<div class="dir-item file">${escapeHtml(f)}</div>`;
      });
      cont.innerHTML = html;
    }).catch(err => {
      document.getElementById('dir-items').innerHTML = '<span style="color:var(--red)">'+err+'</span>';
    });
}

function loadDir() {
  const path = document.getElementById('path-input').value.trim();
  if (!path) return;
  fetch('/api/directory/load', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({path})
  }).then(r => r.json()).then(data => {
    if (data.error) showLog('[错误] '+data.error);
    else showLog('[信息] 加载了 ' + data.count + ' 个文件');
    loadFiles();
  }).catch(err => showLog('[错误] '+err));
}

// ---------- 日志 ----------
function showLog(msg) {
  const box = document.getElementById('log-box');
  const t = new Date().toLocaleTimeString();
  box.textContent += '['+t+'] '+msg+'\n';
  box.scrollTop = box.scrollHeight;
}

function escapeHtml(s) {
  return (''+s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

// 启动后在日志页添加 Web 端日志
const origLog = console.log;
console.log = function() {
  origLog.apply(console, arguments);
  showLog(Array.from(arguments).join(' '));
};

// 初始自动浏览当前目录
browseDir('.');
</script>
</body>
</html>
"""


# ============================================================
# Flask Web 服务器
# ============================================================

def _get_audio_files(path: str) -> list[str]:
    """递归扫描目录下的音频文件，返回相对路径列表（用于显示）。"""
    result: list[str] = []
    root = Path(path).resolve()
    if not root.is_dir():
        return result
    try:
        for f in sorted(root.rglob("*")):
            if f.suffix.lower() in _AUDIO_EXTENSIONS and f.is_file():
                try:
                    result.append(str(f.resolve()))
                except (OSError, PermissionError):
                    pass
    except (OSError, PermissionError):
        pass
    return result


class _WebLogHandler(logging.Handler):
    """将 Python logging 消息转发到 WebBridge SSE 的日志处理器。

    在 WebServer.start() 时挂载到 root logger，stop() 时移除。
    """

    def __init__(self, bridge: _WebBridge):
        super().__init__()
        self._bridge = bridge
        self.setFormatter(logging.Formatter("%(message)s"))
        self.setLevel(logging.INFO)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            t = time.strftime("%H:%M:%S")
            self._bridge.broadcast_sse("log", {"time": t, "message": msg, "level": record.levelname})
        except Exception:
            pass


class WebServer:
    """Web 管理服务器。

    在后台线程中运行 Flask + waitress，提供 REST API 和 Web UI。
    通过 WebBridge 与 Qt 主线程通信。
    """

    def __init__(self, config: dict):
        self._config = config
        self._bridge = get_bridge()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._log_handler: _WebLogHandler | None = None

        self._flask_app: Any = None
        self._server: Any = None  # waitress server ref

    # ------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------
    def start(self) -> bool:
        """启动 Web 服务器（后台线程）。

        Returns:
            是否成功启动。
        """
        if self.is_running():
            logger.info("Web 服务器已在运行中")
            return True

        if Flask is None:
            logger.warning("Flask 未安装，Web 服务器无法启动")
            return False

        web_cfg = self._config.get("web", {})
        if not web_cfg.get("enabled", False):
            logger.info("Web 服务已禁用")
            return False

        host = web_cfg.get("host", "0.0.0.0")
        port = int(web_cfg.get("port", 8080))

        self._stop_event.clear()
        self._flask_app = self._create_app()

        # 更新 bridge 中的端口/主机信息
        bridge = self._bridge
        bridge.update_status(
            server_host=host,
            server_port=port,
        )

        self._thread = threading.Thread(
            target=self._run_server,
            args=(host, port),
            daemon=True,
            name="web-server",
        )
        self._thread.start()

        # 挂载 Web 日志处理器，将 logging 消息转发到 SSE
        self._log_handler = _WebLogHandler(bridge)
        logging.getLogger().addHandler(self._log_handler)

        logger.info(f"Web 服务器已启动: http://{host}:{port}")
        bridge.update_status(server_running=True)
        bridge.broadcast_status()
        return True

    def stop(self) -> None:
        """停止 Web 服务器。"""
        if not self.is_running():
            return

        # 移除 Web 日志处理器
        if self._log_handler is not None:
            logging.getLogger().removeHandler(self._log_handler)
            self._log_handler = None

        logger.info("正在停止 Web 服务器...")
        self._stop_event.set()
        # 发送关闭请求到 waitress（可能触发 OSError 当连接已关闭时）
        if self._server:
            try:
                self._server.close()
            except OSError:
                # WinError 10038: 非套接字上尝试操作（服务器已关闭）
                pass
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None
        self._server = None
        self._bridge.update_status(server_running=False)
        self._bridge.broadcast_status()
        logger.info("Web 服务器已停止")

    def is_running(self) -> bool:
        """Web 服务器是否正在运行。"""
        return self._thread is not None and self._thread.is_alive()

    def restart(self, config: dict | None = None) -> bool:
        """重启 Web 服务器（当配置变更时调用）。

        Args:
            config: 新配置，不传则使用已有配置。

        Returns:
            是否成功重启。
        """
        if config is not None:
            self._config = config
        self.stop()
        return self.start()

    # ------------------------------------------------------------
    # Flask 启动
    # ------------------------------------------------------------
    def _run_server(self, host: str, port: int) -> None:
        """在后台线程中运行 Flask 服务器。"""
        try:
            if waitress is not None:
                # 生产级 WSGI 服务器
                self._server = waitress.create_server(
                    self._flask_app,
                    host=host,
                    port=port,
                    threads=8,
                )
                # 在单独的线程中运行 serve 以便主线程可以停止
                serve_thread = threading.Thread(
                    target=self._server.run,
                    daemon=True,
                    name="waitress-serve",
                )
                serve_thread.start()
                # 等待停止事件
                self._stop_event.wait()
            else:
                # 回退到 Flask 开发服务器
                logger.warning("waitress 未安装，使用 Flask 开发服务器（仅推荐测试使用）")
                self._flask_app.run(
                    host=host,
                    port=port,
                    debug=False,
                    use_reloader=False,
                    threaded=True,
                )
        except Exception as e:
            logger.error(f"Web 服务器异常: {e}")
            self._bridge.update_status(server_running=False)
            self._bridge.broadcast_status()

    # ------------------------------------------------------------
    # Flask App 工厂
    # ------------------------------------------------------------
    def _create_app(self):
        """创建 Flask 应用，注册路由。"""
        app = Flask(__name__)
        bridge = self._bridge

        # ------ 静态页面 ------
        @app.route("/")
        def index():
            return _WEB_UI_HTML

        # ------ API: 获取状态 ------
        @app.route("/api/status")
        def api_status():
            s = bridge.get_status()
            return jsonify({
                "app_running": s.app_running,
                "batch_running": s.batch_running,
                "batch_paused": s.batch_paused,
                "current": s.current,
                "total": s.total,
                "current_file": s.current_file,
                "step_name": s.step_name,
                "done_count": s.done_count,
                "skipped_count": s.skipped_count,
                "failed_count": s.failed_count,
                "total_files": s.total_files,
                "checked_files": s.checked_files,
                "server_running": s.server_running,
                "server_host": s.server_host,
                "server_port": s.server_port,
                "elapsed_seconds": s.elapsed_seconds,
            })

        # ------ API: 执行动作 ------
        @app.route("/api/action", methods=["POST"])
        def api_action():
            data = request.get_json(silent=True) or {}
            action = data.get("action", "")
            if not action:
                return jsonify({"error": "missing action"}), 400

            # 内置批处理动作通过队列发往 Qt 主线程
            if action in ("batch_start", "batch_pause", "batch_stop"):
                bridge.enqueue_action(action)
                return jsonify({"ok": True, "action": action})

            # 通用动作直接执行
            result = bridge.execute_action(action, data.get("params", {}))
            if isinstance(result, dict) and "error" in result:
                return jsonify(result), 400
            return jsonify({"ok": True, "result": result})

        # ------ API: 获取文件列表 ------
        @app.route("/api/files")
        def api_files():
            s = bridge.get_status()
            cached = bridge.get_cached_files()
            return jsonify({
                "total": s.total_files,
                "checked": s.checked_files,
                "files": cached,
            })

        # ------ API: 浏览目录 ------
        @app.route("/api/directory")
        def api_directory():
            path = request.args.get("path", ".")
            try:
                p = Path(path).resolve()
                if not p.exists():
                    return jsonify({"error": "路径不存在", "path": str(p)}), 404
                if p.is_file():
                    p = p.parent

                # 获取上级目录
                parent = str(p.parent) if p.parent != p else None

                # 列出子目录和音频文件
                dirs: list[str] = []
                files: list[str] = []
                try:
                    for entry in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                        name = entry.name
                        if name.startswith("."):
                            continue
                        if entry.is_dir():
                            dirs.append(name)
                        elif entry.suffix.lower() in _AUDIO_EXTENSIONS:
                            files.append(name)
                except PermissionError:
                    pass

                return jsonify({
                    "path": str(p),
                    "parent": parent,
                    "dirs": dirs,
                    "files": files,
                })
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        # ------ API: 加载目录 ------
        @app.route("/api/directory/load", methods=["POST"])
        def api_directory_load():
            data = request.get_json(silent=True) or {}
            path = data.get("path", "")
            if not path:
                return jsonify({"error": "missing path"}), 400

            # 通过队列发往 Qt 主线程异步加载
            bridge.enqueue_action("add_folder", {"folder": path})
            # 先返回，实际加载是异步的
            return jsonify({"ok": True, "path": path, "count": 0})

        # ------ SSE: 实时事件推送 ------
        @app.route("/api/events")
        def api_events():
            def generate():
                client_queue = bridge.subscribe_sse()
                try:
                    # 发送初始状态
                    s = bridge.get_status()
                    yield f"event: status\ndata: {json.dumps({'app_running': s.app_running, 'batch_running': s.batch_running, 'batch_paused': s.batch_paused, 'current': s.current, 'total': s.total, 'current_file': s.current_file, 'step_name': s.step_name, 'done_count': s.done_count, 'skipped_count': s.skipped_count, 'failed_count': s.failed_count, 'total_files': s.total_files, 'checked_files': s.checked_files, 'server_running': s.server_running, 'server_host': s.server_host, 'server_port': s.server_port, 'elapsed_seconds': s.elapsed_seconds})}\n\n"

                    while not bridge.get_status().app_running:
                        # 如果应用退出则停止
                        break

                    while True:
                        try:
                            event, data = client_queue.get(timeout=15)
                            yield f"event: {event}\ndata: {data}\n\n"
                        except queue.Empty:
                            # 发送心跳保持连接（15 秒超时）
                            yield f": heartbeat {int(time.time())}\n\n"
                except GeneratorExit:
                    pass
                finally:
                    bridge.unsubscribe_sse(client_queue)

            return Response(
                stream_with_context(generate()),
                mimetype="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )

        return app
