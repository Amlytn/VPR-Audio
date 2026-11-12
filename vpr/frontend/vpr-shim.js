/**
 * pywebview 适配层（在业务脚本之前加载），为构建产物提供 window.__TAURI__ 兼容接口。
 */
(function () {
  'use strict';

  var ARG_NAME_MAP = {
    start_convert: { outputFolder: 'output_folder', concurrency: 'concurrency' },
  };

  var METHOD_MAP = {
    get_app_version: 'get_app_version',
    get_ffmpeg_version: 'get_ffmpeg_version',
    get_saved_paths: 'get_saved_paths',
    save_output_format: 'save_output_format',
    save_concurrency: 'save_concurrency',
    save_close_to_tray: 'save_close_to_tray',
    select_input: 'select_input',
    select_folder: 'select_folder',
    select_output_folder: 'select_output_folder',
    select_db: 'select_db',
    select_qmc_keydb: 'select_qmc_keydb',
    get_qmc_db_status: 'get_qmc_db_status',
    save_qmc_manual: 'save_qmc_manual',
    get_db_status: 'get_db_status',
    refresh_db: 'refresh_db',
    scan_input: 'scan_input',
    open_folder: 'open_folder',
    get_history: 'get_history',
    clear_history: 'clear_history',
    cancel_convert: 'cancel_convert',
    start_convert: 'start_convert',
  };

  function camelizeToApiMethod(cmd) {
    return METHOD_MAP[cmd] || cmd;
  }

  function apiReady() {
    if (window.pywebview && window.pywebview.api) {
      return Promise.resolve(window.pywebview.api);
    }
    return new Promise(function (resolve) {
      var done = false;
      var poll = setInterval(function () {
        if (window.pywebview && window.pywebview.api) {
          clearInterval(poll);
          if (!done) { done = true; resolve(window.pywebview.api); }
        }
      }, 50);
      window.addEventListener('pywebviewready', function () {
        clearInterval(poll);
        if (!done) { done = true; resolve(window.pywebview.api); }
      });
    });
  }

  function invoke(cmd, args) {
    args = args || {};
    var rename = ARG_NAME_MAP[cmd] || {};
    var pyArgs = {};
    Object.keys(args).forEach(function (k) {
      pyArgs[rename[k] || k] = args[k];
    });
    var keys = Object.keys(pyArgs);
    return apiReady().then(function (api) {
      var method = camelizeToApiMethod(cmd);
      // 无参命令不传参，避免与 Python 方法签名不匹配
      if (keys.length === 0) {
        return api[method]();
      }
      return api[method](pyArgs);
    });
  }

  var listeners = {};
  var callbacks = {};

  window.__vprEvent = function (name, payload) {
    var ids = listeners[name];
    if (!ids) return;
    ids.forEach(function (id) {
      var cb = callbacks[id];
      if (cb) {
        try { cb({ payload: payload, event: name, id: 0 }); } catch (e) { console.error(e); }
      }
    });
  };

  function listen(event, cb) {
    if (!listeners[event]) listeners[event] = new Set();
    var id = Math.random().toString(36).slice(2);
    callbacks[id] = cb;
    listeners[event].add(id);
    return Promise.resolve(function unlisten() {
      listeners[event].delete(id);
      delete callbacks[id];
    });
  }

  var dropHandlers = [];

  function emitDrop(type, paths) {
    dropHandlers.forEach(function (cb) {
      cb({ payload: { type: type, paths: paths || [] } });
    });
  }

  // WebView2 下真实路径由后端 DOM drop 桥通过 __vprDrop 送回
  window.__vprDropBridgeActive = false;
  window.__vprDrop = function (paths) {
    emitDrop('drop', paths || []);
  };

  function setupHtml5Drop() {
    var counter = 0;
    window.addEventListener('dragenter', function (e) {
      e.preventDefault();
      if (++counter === 1) emitDrop('over');
    });
    window.addEventListener('dragleave', function (e) {
      e.preventDefault();
      if (--counter === 0) emitDrop('leave');
    });
    window.addEventListener('dragover', function (e) { e.preventDefault(); });
    window.addEventListener('drop', function (e) {
      e.preventDefault();
      counter = 0;
      if (window.__vprDropBridgeActive) return;
      var files = Array.prototype.slice.call(e.dataTransfer.files || []);
      var paths = files.map(function (f) { return f.path || f.name; }).filter(Boolean);
      emitDrop('drop', paths);
    });
  }

  function setupDblclickMaximize() {
    window.addEventListener('dblclick', function (e) {
      var t = e.target;
      if (!(t && t.closest && t.closest('[data-tauri-drag-region]'))) return;
      if (e.clientY > 56) return;
      if (t.closest('#window-btns')) return;
      invoke('win_maximize_toggle', {}).catch(function () {});
    }, true);
  }

  function makeWindowObj() {
    var winCalls = function () { return apiReady(); };
    return {
      minimize: function () { return winCalls().then(function (a) { return a.win_minimize(); }); },
      maximize: function () { return winCalls().then(function (a) { return a.win_maximize(); }); },
      unmaximize: function () { return winCalls().then(function (a) { return a.win_restore(); }); },
      close: function () { return winCalls().then(function (a) { return a.win_close(); }); },
      isMaximized: function () { return winCalls().then(function (a) { return a.win_is_maximized(); }); },
      onDragDropEvent: function (cb) {
        dropHandlers.push(cb);
        return Promise.resolve(function () {
          var i = dropHandlers.indexOf(cb);
          if (i >= 0) dropHandlers.splice(i, 1);
        });
      },
    };
  }

  // 拖拽与缩放交由后端光标轮询处理，避免逐帧 IPC 产生拖拽延迟
  function setupWindowDrag() {
    document.addEventListener('mousedown', function (e) {
      if (e.button !== 0) return;
      var t = e.target;
      if (!(t instanceof Element)) return;

      if (t.closest('#window-btns,button,input,select,textarea,a,[data-no-drag]')) return;

      var dir = resizeHitTest(e);
      if (dir) {
        e.preventDefault();
        invoke('win_resize_start', { direction: dir }).catch(function () {});
        return;
      }

      var isDragRegion = !!(t.hasAttribute && t.hasAttribute('data-tauri-drag-region'));
      if (!isDragRegion && e.clientY > 56) return;
      if (e.detail >= 2) return;
      e.preventDefault();
      invoke('win_drag_start', {}).catch(function () {});
    });

    function stopGesture() {
      invoke('win_gesture_stop', {}).catch(function () {});
    }
    document.addEventListener('mouseup', stopGesture);
    window.addEventListener('blur', stopGesture);
  }

  function resizeHitTest(e) {
    var EDGE = 8;
    var CORNER = 14;
    var x = e.clientX, y = e.clientY;
    var w = window.innerWidth, h = window.innerHeight;
    var left = x <= EDGE, right = x >= w - EDGE, top = y <= EDGE, bottom = y >= h - EDGE;
    var cl = x <= CORNER, cr = x >= w - CORNER, ct = y <= CORNER, cb = y >= h - CORNER;
    if (ct && cl) return 'top_left';
    if (ct && cr) return 'top_right';
    if (cb && cl) return 'bottom_left';
    if (cb && cr) return 'bottom_right';
    if (left) return 'left';
    if (right) return 'right';
    if (top) return 'top';
    if (bottom) return 'bottom';
    return null;
  }

  function setupResizeCursor() {
    var cursors = {
      left: 'ew-resize', right: 'ew-resize', top: 'ns-resize', bottom: 'ns-resize',
      top_left: 'nwse-resize', bottom_right: 'nwse-resize',
      top_right: 'nesw-resize', bottom_left: 'nesw-resize',
    };
    var lastCursor = '';
    document.addEventListener('mousemove', function (e) {
      var dir = resizeHitTest(e);
      var next = dir ? cursors[dir] : '';
      if (next === lastCursor) return;
      lastCursor = next;
      document.body.style.cursor = next;
    });
  }

  // 窗口显示前先暂停入场动画，等后端就绪后再放开
  (function suspendAnimations() {
    var style = document.createElement('style');
    style.id = 'vpr-suspend-style';
    style.textContent =
      '.vpr-suspend *, .vpr-suspend *::before, .vpr-suspend *::after' +
      '{animation-play-state:paused !important}';
    (document.head || document.documentElement).appendChild(style);
    document.documentElement.classList.add('vpr-suspend');
    // 浏览器单独调试时 30s 自动恢复；正常桌面启动若已调过 __vprStartIntro 则跳过
    setTimeout(function () {
      if (window.__vprIntroStarted) return;
      if (window.__vprStartIntro) {
        window.__vprStartIntro();
      } else {
        document.documentElement.classList.remove('vpr-suspend');
      }
    }, 30000);
  })();

  window.__vprIntroPrepare = function () {
    document.documentElement.classList.add('vpr-suspend');
    document.body.classList.add('booting');
    Array.prototype.forEach.call(document.querySelectorAll('.wave-bars i'), function (bar) {
      try {
        if (bar.getAnimations) bar.getAnimations().forEach(function (a) { a.cancel(); });
      } catch (e) {}
      bar.style.transform = 'scaleY(0)';
    });
  };

  // 与 win_show 在同一任务内执行，让窗口首帧同步播放入场动画
  window.__vprStartIntro = function () {
    window.__vprIntroStarted = true;
    try {
      if (window.pywebview && window.pywebview.api && window.pywebview.api.win_show) {
        var p = window.pywebview.api.win_show();
        if (p && p.catch) p.catch(function () {});
      }
    } catch (e) {}

    // 交还焦点到 body，避免首个按钮在启动时出现 focus-visible 边框
    try {
      if (document.activeElement && document.activeElement !== document.body) {
        document.activeElement.blur();
      }
      document.body.focus();
    } catch (e) {}

    document.documentElement.classList.remove('vpr-suspend');
    var body = document.body;
    body.classList.add('booting');

    var startBars = function () {
      Array.prototype.forEach.call(document.querySelectorAll('.wave-bars i'), function (bar, i) {
        bar.style.transform = '';
        bar.style.transformOrigin = '50% 100%';
        var anim = bar.animate(
          [{ transform: 'scaleY(0)' }, { transform: 'scaleY(1)' }],
          { duration: 420, delay: i * 14, easing: 'cubic-bezier(0.16, 1, 0.3, 1)', fill: 'backwards' }
        );
        anim.onfinish = function () { bar.style.transformOrigin = ''; };
      });
      clearTimeout(window.__vprIntroTimer);
      window.__vprIntroTimer = setTimeout(function () {
        body.classList.remove('booting');
      }, 1400);
    };

    if (window.requestAnimationFrame) {
      requestAnimationFrame(function () { requestAnimationFrame(startBars); });
    } else {
      startBars();
    }
  };

  window.__vprReplayIntro = window.__vprStartIntro;

  function install() {
    var winObj = makeWindowObj();
    window.__TAURI__ = {
      core: { invoke: invoke },
      event: { listen: listen },
      window: { getCurrentWindow: function () { return winObj; } },
    };
    setupHtml5Drop();
    setupDblclickMaximize();
    setupWindowDrag();
    setupResizeCursor();
  }

  if (document.readyState === 'loading') {
    install();
  } else {
    install();
  }
})();
