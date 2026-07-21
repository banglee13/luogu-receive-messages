// ==UserScript==
// @name         洛谷私信实时系统通知
// @namespace    https://tampermonkey.net/
// @version      1.2
// @description  在洛谷接收私信时弹出系统级通知，极大减少 API 请求次数，支持多标签页主节点选举，兼容新旧前端
// @author       banglee
// @match        https://www.luogu.com.cn/
// @icon         https://www.luogu.com.cn/favicon.ico
// @grant        GM_notification
// @grant        GM_xmlhttpRequest
// @grant        unsafeWindow
// @run-at       document-start
// ==/UserScript==

(function() {
    'use strict';

    // ======== 配置项 ========
    const HEARTBEAT_INTERVAL = 5000;
    const LEADER_TIMEOUT = 12000;
    const RECONNECT_BASE_DELAY = 2000;
    const RECONNECT_MAX_DELAY = 60000;
    const LOGIN_CHECK_MAX_RETRIES = 30;
    const LOGIN_CHECK_RETRY_INTERVAL = 500;

    const STORAGE_KEY_LEADER = 'luogu_pm_leader_timestamp';
    const STORAGE_KEY_LEADER_ID = 'luogu_pm_leader_id';
    const STORAGE_KEY_WS_CONFIG = 'luogu_pm_ws_config';
    const STORAGE_KEY_WS_CONFIG_TIME = 'luogu_pm_ws_config_time';
    const WS_CONFIG_EXPIRE = 24 * 60 * 60 * 1000;

    const STORAGE_KEY_USER_CACHE = 'luogu_pm_user_cache';
    const STORAGE_KEY_USER_CACHE_TIME = 'luogu_pm_user_cache_time';
    const USER_CACHE_EXPIRE = 30 * 60 * 1000;

    const TAB_ID = Math.random().toString(36).substring(2, 10);

    let isLeader = false;
    let leaderTimer = null;
    let ws = null;
    let reconnectAttempts = 0;
    let isIntentionalClose = false;
    let currentUser = null;
    let isInitialized = false;

    // ======== 工具函数 ========

    function getPageWindow() {
        return unsafeWindow || window;
    }

    function isUserValid(u) {
        return u && typeof u === 'object' && u.uid && (typeof u.uid === 'number' || (typeof u.uid === 'string' && u.uid !== '0'));
    }

    function extractUserFromNuxtLike(nuxtData) {
        if (!nuxtData) return null;
        const candidates = [
            nuxtData.user,
            nuxtData.state && nuxtData.state.user,
            nuxtData.data && nuxtData.data.user,
            nuxtData.currentUser,
        ];
        for (const c of candidates) {
            if (isUserValid(c)) return c;
        }
        return null;
    }

    // 从缓存读取用户信息
    function getUserFromCache() {
        try {
            const cached = localStorage.getItem(STORAGE_KEY_USER_CACHE);
            const cachedTime = parseInt(localStorage.getItem(STORAGE_KEY_USER_CACHE_TIME) || '0', 10);
            if (cached && (Date.now() - cachedTime < USER_CACHE_EXPIRE)) {
                const obj = JSON.parse(cached);
                if (isUserValid(obj)) return obj;
            }
        } catch (e) { /* ignore */ }
        return null;
    }

    function cacheUser(user) {
        if (isUserValid(user)) {
            try {
                localStorage.setItem(STORAGE_KEY_USER_CACHE, JSON.stringify({ uid: user.uid, name: user.name || user.username || '', avatar: user.avatar || user.slogan || '' }));
                localStorage.setItem(STORAGE_KEY_USER_CACHE_TIME, Date.now().toString());
            } catch (e) { /* ignore */ }
        }
    }

    // 使用 GM_xmlhttpRequest 封装一个 Promise 化的请求（绕过 CORS）
    function gmFetch(url, opts) {
        opts = opts || {};
        return new Promise((resolve, reject) => {
            const details = {
                url: url,
                method: opts.method || 'GET',
                headers: opts.headers || {},
                timeout: opts.timeout || 15000,
                onload: function(response) {
                    resolve({
                        ok: response.status >= 200 && response.status < 300,
                        status: response.status,
                        json: function() {
                            try { return Promise.resolve(JSON.parse(response.responseText)); }
                            catch (e) { return Promise.reject(e); }
                        },
                        text: function() { return Promise.resolve(response.responseText); }
                    });
                },
                onerror: function(err) { reject(err); },
                ontimeout: function() { reject(new Error('timeout')); }
            };
            if (opts.responseType) details.responseType = opts.responseType;
            GM_xmlhttpRequest(details);
        });
    }

    // ======== 核心逻辑 ========

    // 检查是否登录（兼容新旧前端，优先级：_feInjection > _feInstance > Nuxt > 缓存 > API）
    async function checkLogin() {
        const w = getPageWindow();

        try {
            // 1. 优先尝试 _feInjection（新旧前端都通过 <script> 标签注入的纯 JSON 对象，最可靠）
            const feInjection = w._feInjection;
            if (feInjection && isUserValid(feInjection.currentUser)) {
                currentUser = {
                    uid: feInjection.currentUser.uid,
                    name: feInjection.currentUser.name || feInjection.currentUser.username || '',
                    avatar: feInjection.currentUser.avatar || feInjection.currentUser.slogan || ''
                };
                cacheUser(currentUser);
                return true;
            }
        } catch (e) { /* ignore access errors */ }

        try {
            // 2. 尝试旧/新前端 _feInstance（Vue 实例，兼容旧版）
            const feInstance = w._feInstance;
            if (feInstance) {
                const cu = feInstance.currentUser;
                if (isUserValid(cu)) {
                    currentUser = {
                        uid: cu.uid,
                        name: cu.name || cu.username || '',
                        avatar: cu.avatar || cu.slogan || ''
                    };
                    cacheUser(currentUser);
                    return true;
                }
            }
        } catch (e) { /* ignore */ }

        try {
            // 3. 尝试 Nuxt 数据（可能存在于旧版某些页面）
            const nuxtData = w.__NUXT__;
            const nuxtUser = extractUserFromNuxtLike(nuxtData);
            if (nuxtUser) {
                currentUser = {
                    uid: nuxtUser.uid,
                    name: nuxtUser.name || nuxtUser.username || '',
                    avatar: nuxtUser.avatar || ''
                };
                cacheUser(currentUser);
                return true;
            }
        } catch (e) { /* ignore */ }

        try {
            // 4. 尝试 __NUXT_DATA__（Nuxt 3 格式，如果未来迁移使用）
            const nuxtData2 = w.__NUXT_DATA__;
            if (nuxtData2) {
                // __NUXT_DATA__ 是一个扁平数组，遍历寻找包含 uid/name 模式的对象
                const arr = Array.isArray(nuxtData2) ? nuxtData2 : Object.values(nuxtData2);
                for (const item of arr) {
                    if (item && typeof item === 'object' && isUserValid(item)) {
                        currentUser = { uid: item.uid, name: item.name || '', avatar: item.avatar || '' };
                        cacheUser(currentUser);
                        return true;
                    }
                }
            }
        } catch (e) { /* ignore */ }

        // 5. 尝试从缓存读取
        const cachedUser = getUserFromCache();
        if (cachedUser) {
            currentUser = cachedUser;
            return true;
        }

        // 6. 通过 API 获取用户信息（使用 GM_xmlhttpRequest 绕过 CORS）
        const apiAttempts = [
            { url: 'https://www.luogu.com.cn/chat', headers: { 'x-luogu-type': 'content-only' }, path: ['currentUser'] },
            { url: 'https://www.luogu.com.cn/chat', headers: { 'x-lentille-request': 'content-only' }, path: ['user', 'currentUser'] },
            { url: 'https://www.luogu.com.cn/_lfe/config', headers: {}, path: ['user'] },
        ];

        for (const attempt of apiAttempts) {
            try {
                const res = await gmFetch(attempt.url, { headers: attempt.headers });
                if (res.ok) {
                    const data = await res.json();
                    let user = null;
                    for (const key of attempt.path) {
                        if (data && data[key]) { user = data[key]; data = data[key]; }
                        else { user = null; break; }
                    }
                    if (isUserValid(user)) {
                        currentUser = { uid: user.uid, name: user.name || user.username || '', avatar: user.avatar || '' };
                        cacheUser(currentUser);
                        return true;
                    }
                }
            } catch (e) { /* try next */ }
        }

        return false;
    }

    // ======== 主节点选举 ========

    function checkLeader() {
        const now = Date.now();
        const leaderTimestamp = parseInt(localStorage.getItem(STORAGE_KEY_LEADER) || '0', 10);
        const currentLeaderId = localStorage.getItem(STORAGE_KEY_LEADER_ID);

        if (now - leaderTimestamp > LEADER_TIMEOUT || currentLeaderId === TAB_ID) {
            if (!isLeader) {
                console.log('[洛谷私信通知] 当前标签页成为主节点，负责 WebSocket 连接。');
                isLeader = true;
                startWebSocket();
            }
            localStorage.setItem(STORAGE_KEY_LEADER, now.toString());
            localStorage.setItem(STORAGE_KEY_LEADER_ID, TAB_ID);
        } else {
            if (isLeader) {
                console.log('[洛谷私信通知] 当前标签页降级为跟随者。');
                isLeader = false;
                stopWebSocket();
            }
        }
    }

    // ======== WebSocket 配置获取 ========

    async function getWsConfig() {
        try {
            const cachedConfig = localStorage.getItem(STORAGE_KEY_WS_CONFIG);
            const cachedTime = parseInt(localStorage.getItem(STORAGE_KEY_WS_CONFIG_TIME) || '0', 10);
            const now = Date.now();

            if (cachedConfig && (now - cachedTime < WS_CONFIG_EXPIRE)) {
                return JSON.parse(cachedConfig);
            }
        } catch (e) { /* ignore cache errors */ }

        try {
            const res = await gmFetch('https://www.luogu.com.cn/_lfe/config');
            if (res.ok) {
                const data = await res.json();
                if (data && data.ws && data.ws.server) {
                    localStorage.setItem(STORAGE_KEY_WS_CONFIG, JSON.stringify(data.ws));
                    localStorage.setItem(STORAGE_KEY_WS_CONFIG_TIME, Date.now().toString());
                    return data.ws;
                }
            }
        } catch (error) {
            console.error('[洛谷私信通知] 获取 WebSocket 配置失败:', error);
        }
        return null;
    }

    // ======== WebSocket 管理 ========

    async function startWebSocket() {
        if (!isUserValid(currentUser)) return;

        isIntentionalClose = false;
        const wsConfig = await getWsConfig();
        if (!wsConfig || !wsConfig.server) {
            console.error('[洛谷私信通知] 无法获取 WebSocket 服务器地址，将在下次重试。');
            scheduleReconnect();
            return;
        }

        try {
            ws = new WebSocket(wsConfig.server);

            ws.onopen = () => {
                console.log('[洛谷私信通知] WebSocket 连接成功。');
                reconnectAttempts = 0;
                try {
                    ws.send(JSON.stringify({
                        channel: "chat",
                        channel_param: `${currentUser.uid}`,
                        type: "join_channel"
                    }));
                } catch (e) {
                    console.error('[洛谷私信通知] 发送加入频道消息失败:', e);
                }
            };

            ws.onmessage = (event) => {
                try {
                    const data = JSON.parse(event.data);
                    if (data._ws_type === "heartbeat") return;

                    console.log('[洛谷私信通知] 收到 WebSocket 消息:', data);

                    if (data._ws_type === "server_broadcast") {
                        const message = data.message;
                        if (message && message.sender) {
                            const senderUid = message.sender.uid;
                            if (String(senderUid) !== String(currentUser.uid)) {
                                console.log('[洛谷私信通知] 准备弹出通知:', message);
                                showNotification(message);
                            } else {
                                console.log('[洛谷私信通知] 这是自己发送的消息，忽略通知。');
                            }
                        }
                    } else if (data._ws_type === "exclusive_kickoff") {
                        console.warn('[洛谷私信通知] 被其他客户端踢出频道，将重新连接。');
                        ws = null;
                        if (isLeader && !isIntentionalClose) scheduleReconnect();
                    }
                } catch (e) {
                    console.error('[洛谷私信通知] 解析 WebSocket 消息失败:', e);
                }
            };

            ws.onclose = (e) => {
                console.log('[洛谷私信通知] WebSocket 连接断开:', e.reason, 'code:', e.code);
                ws = null;
                if (!isIntentionalClose && isLeader) {
                    scheduleReconnect();
                }
            };

            ws.onerror = (err) => {
                console.error('[洛谷私信通知] WebSocket 错误:', err);
            };
        } catch (err) {
            console.error('[洛谷私信通知] WebSocket 创建失败:', err);
            scheduleReconnect();
        }
    }

    function stopWebSocket() {
        isIntentionalClose = true;
        if (ws) {
            try { ws.close(); } catch (e) { /* ignore */ }
            ws = null;
        }
    }

    function scheduleReconnect() {
        if (!isLeader) return;
        const delay = Math.min(RECONNECT_MAX_DELAY, RECONNECT_BASE_DELAY * Math.pow(2, reconnectAttempts));
        reconnectAttempts++;
        console.log(`[洛谷私信通知] 将在 ${delay} 毫秒后尝试重连 (第 ${reconnectAttempts} 次)...`);
        setTimeout(() => {
            if (isLeader) {
                startWebSocket();
            }
        }, delay);
    }

    // ======== 系统通知 ========

    function showNotification(message) {
        const senderName = (message.sender && (message.sender.name || message.sender.username)) || '未知用户';
        const title = `新私信：来自 ${senderName}`;
        const content = message.content || '(无内容)';
        const senderUid = message.sender && message.sender.uid;
        const iconUrl = senderUid ? `https://cdn.luogu.com.cn/upload/usericon/${senderUid}.png` : '';

        // 优先使用 GM_notification（Scriptcat/Tampermonkey 原生通知，更稳定且在后台也能弹）
        try {
            if (typeof GM_notification !== 'undefined') {
                GM_notification({
                    title: title,
                    text: content,
                    image: iconUrl || undefined,
                    highlight: true,
                    onclick: function() {
                        try {
                            if (senderUid) {
                                window.open(`https://www.luogu.com.cn/chat?uid=${senderUid}`, '_blank');
                            } else {
                                window.open('https://www.luogu.com.cn/chat', '_blank');
                            }
                        } catch(e) {
                            window.location.href = `https://www.luogu.com.cn/chat${senderUid ? '?uid=' + senderUid : ''}`;
                        }
                    },
                    ondone: function() {}
                });
                console.log('[洛谷私信通知] GM通知已弹出:', { title, content });
                return;
            }
        } catch (e) {
            console.warn('[洛谷私信通知] GM_notification 失败，尝试 HTML5 Notification:', e);
        }

        // 回退到 HTML5 Notification API
        if ("Notification" in window) {
            if (Notification.permission === "granted") {
                doShowHtmlNotification(title, content, iconUrl, senderUid);
            } else if (Notification.permission !== "denied") {
                Notification.requestPermission().then(permission => {
                    if (permission === "granted") {
                        doShowHtmlNotification(title, content, iconUrl, senderUid);
                    }
                });
            }
        } else {
            console.warn('[洛谷私信通知] 您的浏览器不支持桌面通知');
        }
    }

    function doShowHtmlNotification(title, content, iconUrl, senderUid) {
        try {
            const notification = new Notification(title, {
                body: content,
                icon: iconUrl || undefined,
                requireInteraction: true
            });
            notification.onclick = () => {
                window.focus();
                window.location.href = `https://www.luogu.com.cn/chat${senderUid ? '?uid=' + senderUid : ''}`;
                notification.close();
            };
            console.log('[洛谷私信通知] HTML5通知已弹出:', { title, content });
        } catch (e) {
            console.error('[洛谷私信通知] 通知弹出异常:', e);
        }
    }

    // ======== 入口与初始化 ========

    function waitForLogin() {
        return new Promise((resolve) => {
            let attempts = 0;
            const tryCheck = async () => {
                const ok = await checkLogin();
                if (ok) {
                    resolve(true);
                    return;
                }
                attempts++;
                if (attempts >= LOGIN_CHECK_MAX_RETRIES) {
                    resolve(false);
                    return;
                }
                setTimeout(tryCheck, LOGIN_CHECK_RETRY_INTERVAL);
            };
            tryCheck();
        });
    }

    // 监听 SPA 路由变化，检测用户变化（例如登录/登出）
    function setupSpaWatcher() {
        let lastInjectionUser = currentUser ? currentUser.uid : null;
        const check = () => {
            try {
                const w = getPageWindow();
                const fi = w._feInjection;
                const newUid = fi && fi.currentUser && fi.currentUser.uid ? fi.currentUser.uid : null;
                if (newUid !== lastInjectionUser) {
                    console.log('[洛谷私信通知] 检测到用户状态变化:', lastInjectionUser, '->', newUid);
                    lastInjectionUser = newUid;
                    if (newUid) {
                        checkLogin().then(ok => {
                            if (ok && isInitialized) {
                                console.log(`[洛谷私信通知] 用户已更新: ${currentUser.name || currentUser.uid}`);
                                if (isLeader) {
                                    stopWebSocket();
                                    startWebSocket();
                                }
                            }
                        });
                    } else {
                        if (isLeader) stopWebSocket();
                    }
                }
            } catch (e) { /* ignore */ }
        };
        setInterval(check, 3000);
    }

    async function init() {
        // 跳过非主域的 iframe 等意外注入场景
        if (window.top !== window.self) {
            // 允许在同源 iframe 中运行，但跳过跨域 iframe（如 fecdn.luogu.com.cn 的 hub.frame）
            try {
                if (window.top.location.hostname !== window.location.hostname) {
                    return;
                }
            } catch (e) {
                // 跨域访问 top.location 会抛异常，说明是跨域 iframe，直接退出
                console.log('[洛谷私信通知] 在跨域 iframe 中，跳过执行。');
                return;
            }
        }

        const loggedIn = await waitForLogin();
        if (!loggedIn) {
            console.log('[洛谷私信通知] 用户未登录，脚本未激活。');
            return;
        }

        isInitialized = true;
        console.log(`[洛谷私信通知] 脚本已启动，当前用户: ${currentUser.name || currentUser.uid}（UID: ${currentUser.uid}），标签页 ID: ${TAB_ID}`);

        checkLeader();
        leaderTimer = setInterval(checkLeader, HEARTBEAT_INTERVAL);
        setupSpaWatcher();

        window.addEventListener('beforeunload', () => {
            if (isLeader) {
                localStorage.removeItem(STORAGE_KEY_LEADER);
                localStorage.removeItem(STORAGE_KEY_LEADER_ID);
            }
        });
    }

    // DOM 尚未就绪时也能执行（@run-at document-start + _feInjection 在 <script> 中立即定义）
    // 但为了保险，等待 DOM 准备好再启动主要逻辑
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => init());
    } else {
        init();
    }

})();
