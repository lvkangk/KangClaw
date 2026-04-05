// kangclaw Web UI

// ── Theme ──
function applyTheme(theme) {
    localStorage.setItem('kangclaw-theme', theme);
    if (theme === 'system') {
        const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
        document.documentElement.setAttribute('data-theme', prefersDark ? 'dark' : 'light');
    } else {
        document.documentElement.setAttribute('data-theme', theme);
    }
    document.querySelectorAll('.theme-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.theme === theme);
    });
}

// 初始化主题
(function() {
    const saved = localStorage.getItem('kangclaw-theme') || 'dark';
    applyTheme(saved);
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
        if (localStorage.getItem('kangclaw-theme') === 'system') applyTheme('system');
    });
})();

const messagesEl = document.getElementById('messages');
const inputEl = document.getElementById('input');
const sendBtn = document.getElementById('send');
const attachBtn = document.getElementById('attach');
const fileInput = document.getElementById('file-input');
const attachPreview = document.getElementById('attachment-preview');
const newChatBtn = document.getElementById('new-chat');
const statusDot = document.querySelector('.status-dot');
const versionLabel = document.getElementById('version-label');

let ws = null;
let currentAssistantEl = null;
let currentAssistantText = '';
let thinkingEl = null;
let welcomeVisible = true;
let pendingRequests = 0;  // 待处理的消息数
let pendingAttachments = []; // {type, data, filename}

function connect() {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${location.host}/ws`;

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
        statusDot.className = 'status-dot connected';
        sendBtn.disabled = false;
        loadHistory();
        fetchVersion();
        // 重连后重置重启按钮状态
        resetRestartBtn();
        checkConfigStatus();
    };

    ws.onclose = () => {
        statusDot.className = 'status-dot error';
        sendBtn.disabled = true;
        setTimeout(connect, 3000);
    };

    ws.onerror = () => {
        statusDot.className = 'status-dot error';
    };

    ws.onmessage = (event) => {
        const data = event.data;

        if (data === '[DONE]') {
            removeThinking();
            if (currentAssistantEl && currentAssistantText) {
                if (currentAssistantText.trim()) {
                    currentAssistantEl.innerHTML = marked.parse(currentAssistantText);
                    currentAssistantEl.classList.remove('streaming');
                } else {
                    currentAssistantEl.closest('.msg-row').remove();
                }
            }
            currentAssistantEl = null;
            currentAssistantText = '';
            pendingRequests = Math.max(0, pendingRequests - 1);
            if (pendingRequests > 0) {
                // 还有排队消息等待处理，保持 thinking 状态
                // 30秒兜底：防止计数器异常导致永久 thinking
                setTimeout(() => {
                    if (pendingRequests > 0 && !currentAssistantEl) {
                        pendingRequests = 0;
                        removeThinking();
                        sendBtn.disabled = false;
                    }
                }, 30000);
                showThinking();
            } else {
                sendBtn.disabled = false;
            }
            return;
        }

        if (data === '[ERROR]') {
            removeThinking();
            if (currentAssistantEl && currentAssistantText) {
                if (currentAssistantText.trim()) {
                    currentAssistantEl.innerHTML = marked.parse(currentAssistantText);
                    currentAssistantEl.classList.remove('streaming');
                } else {
                    currentAssistantEl.closest('.msg-row').remove();
                }
            }
            currentAssistantEl = null;
            currentAssistantText = '';
            pendingRequests = 0;
            sendBtn.disabled = false;
            return;
        }

        // 图片消息（JSON）
        try {
            const parsed = JSON.parse(data);
            if (parsed.type === 'image' && parsed.data) {
                removeThinking();
                addImageMessage('assistant', parsed.data, parsed.filename);
                return;
            }
        } catch (e) { /* not JSON, continue as text */ }

        // Tool execution notice
        const trimmed = data.trim();
        if (trimmed.startsWith('[正在执行 ') && trimmed.endsWith(']')) {
            // 工具调用标记：先完成当前流式消息，再显示工具提示
            if (currentAssistantEl && currentAssistantText) {
                if (currentAssistantText.trim()) {
                    currentAssistantEl.innerHTML = marked.parse(currentAssistantText);
                    currentAssistantEl.classList.remove('streaming');
                } else {
                    // 纯空白内容，移除整个消息行
                    currentAssistantEl.closest('.msg-row').remove();
                }
                currentAssistantEl = null;
                currentAssistantText = '';
            }
            removeThinking();
            addMessage('tool', trimmed);
            showThinking();
            return;
        }

        // 静默分段标记：完成当前流式消息但不显示标记
        if (trimmed === '[TOOL_BREAK]') {
            if (currentAssistantEl && currentAssistantText) {
                if (currentAssistantText.trim()) {
                    currentAssistantEl.innerHTML = marked.parse(currentAssistantText);
                    currentAssistantEl.classList.remove('streaming');
                } else {
                    currentAssistantEl.closest('.msg-row').remove();
                }
            }
            currentAssistantEl = null;
            currentAssistantText = '';
            showThinking();
            return;
        }

        // Streaming token
        if (!currentAssistantEl) {
            // 先累积，直到有非空白内容才创建消息行，避免空框闪现
            currentAssistantText = (currentAssistantText || '') + data;
            if (!currentAssistantText.trim()) return;
            removeThinking();
            const row = createMsgRow('assistant');
            currentAssistantEl = row.querySelector('.message');
            currentAssistantEl.classList.add('streaming');
        } else {
            currentAssistantText += data;
        }
        currentAssistantEl.textContent = currentAssistantText;
        scrollToBottom();
    };
}

async function fetchVersion() {
    try {
        const resp = await fetch('/api/status');
        if (!resp.ok) return;
        const data = await resp.json();
        if (data.version) {
            versionLabel.textContent = data.version;
        }
    } catch (e) { /* silent */ }
}

function clearWelcome() {
    if (!welcomeVisible) return;
    const w = messagesEl.querySelector('.welcome');
    if (w) w.remove();
    welcomeVisible = false;
}

function createMsgRow(role) {
    clearWelcome();
    const row = document.createElement('div');
    row.className = `msg-row ${role}-row`;

    const label = document.createElement('div');
    label.className = `msg-label ${role}-label`;
    label.textContent = role === 'user' ? 'You' : 'AI';

    const msg = document.createElement('div');
    msg.className = `message ${role}`;

    row.appendChild(label);
    row.appendChild(msg);

    if (role === 'assistant') {
        const copyBtn = document.createElement('button');
        copyBtn.className = 'msg-copy-btn';
        copyBtn.title = '复制';
        copyBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none"><rect x="5" y="5" width="9" height="9" rx="1.5" stroke="currentColor" stroke-width="1.3"/><path d="M11 5V3.5A1.5 1.5 0 009.5 2h-6A1.5 1.5 0 002 3.5v6A1.5 1.5 0 003.5 11H5" stroke="currentColor" stroke-width="1.3"/></svg>';
        copyBtn.addEventListener('click', () => {
            const text = msg.innerText || msg.textContent;
            navigator.clipboard.writeText(text).then(() => {
                copyBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M3 8.5l3 3 7-7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
                setTimeout(() => {
                    copyBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none"><rect x="5" y="5" width="9" height="9" rx="1.5" stroke="currentColor" stroke-width="1.3"/><path d="M11 5V3.5A1.5 1.5 0 009.5 2h-6A1.5 1.5 0 002 3.5v6A1.5 1.5 0 003.5 11H5" stroke="currentColor" stroke-width="1.3"/></svg>';
                }, 1500);
            });
        });
        row.appendChild(copyBtn);
    }

    messagesEl.appendChild(row);
    scrollToBottom();
    return row;
}

function addMessage(role, content) {
    if (role === 'tool') {
        clearWelcome();
        const row = document.createElement('div');
        row.className = 'msg-row';
        const msg = document.createElement('div');
        msg.className = 'message tool';
        msg.textContent = content;
        row.appendChild(msg);
        messagesEl.appendChild(row);
        scrollToBottom();
        return msg;
    }

    const row = createMsgRow(role);
    const msg = row.querySelector('.message');

    if (role === 'assistant' && content) {
        msg.innerHTML = marked.parse(content);
    } else {
        msg.textContent = content;
    }
    scrollToBottom();
    return msg;
}

function addImageMessage(role, dataUrl, filename) {
    const row = createMsgRow(role);
    const msg = row.querySelector('.message');
    const img = document.createElement('img');
    img.src = dataUrl;
    img.alt = filename || 'image';
    img.className = 'msg-image';
    img.onclick = () => window.open(dataUrl, '_blank');
    msg.appendChild(img);
    if (filename) {
        const label = document.createElement('div');
        label.className = 'msg-image-label';
        label.textContent = filename;
        msg.appendChild(label);
    }
    scrollToBottom();
    return msg;
}

function addUserAttachments(attachments) {
    const row = createMsgRow('user');
    const msg = row.querySelector('.message');
    for (const att of attachments) {
        if (att.type === 'image') {
            const img = document.createElement('img');
            img.src = att.data;
            img.alt = att.filename;
            img.className = 'msg-image';
            msg.appendChild(img);
        } else {
            const fileEl = document.createElement('div');
            fileEl.className = 'msg-file-badge';
            fileEl.textContent = att.filename;
            msg.appendChild(fileEl);
        }
    }
    scrollToBottom();
}

function showThinking() {
    if (thinkingEl) return;
    thinkingEl = document.createElement('div');
    thinkingEl.className = 'thinking';
    thinkingEl.innerHTML = `
        <div class="thinking-content">
            <div class="thinking-dots">
                <span></span><span></span><span></span>
            </div>
        </div>
    `;
    messagesEl.appendChild(thinkingEl);
    scrollToBottom();
}

function removeThinking() {
    if (thinkingEl) {
        thinkingEl.remove();
        thinkingEl = null;
    }
}

function scrollToBottom() {
    requestAnimationFrame(() => {
        messagesEl.scrollTop = messagesEl.scrollHeight;
    });
}

let historyLoaded = false;

async function loadHistory() {
    if (historyLoaded) return;
    historyLoaded = true;
    try {
        const resp = await fetch('/api/history');
        if (!resp.ok) return;
        const messages = await resp.json();
        if (messages.length === 0) return;
        clearWelcome();
        for (const msg of messages) {
            // 跳过空内容的 assistant 消息（工具调用产生的空 content）
            if (msg.role === 'assistant' && (!msg.content || !msg.content.trim())) continue;
            addMessage(msg.role, msg.content);
        }
    } catch (e) {
        // Silently fail — history is nice-to-have
    }
}

function sendMessage() {
    const text = inputEl.value.trim();
    if ((!text && pendingAttachments.length === 0) || !ws || ws.readyState !== WebSocket.OPEN) return;

    // 显示用户消息
    if (text) addMessage('user', text);
    if (pendingAttachments.length > 0) addUserAttachments(pendingAttachments);

    // 构建发送数据
    if (pendingAttachments.length > 0) {
        const payload = {
            content: text || '[附件]',
            attachments: pendingAttachments.map(a => ({
                type: a.type,
                data: a.data,
                filename: a.filename,
            })),
        };
        ws.send(JSON.stringify(payload));
    } else {
        ws.send(text);
    }

    inputEl.value = '';
    inputEl.style.height = 'auto';
    clearAttachments();
    pendingRequests++;
    sendBtn.disabled = true;
    showThinking();
}

// ── Attachment handling ──

attachBtn.addEventListener('click', () => fileInput.click());

fileInput.addEventListener('change', () => {
    for (const file of fileInput.files) {
        readFileAsAttachment(file);
    }
    fileInput.value = '';
});

function readFileAsAttachment(file) {
    const reader = new FileReader();
    reader.onload = () => {
        const type = file.type.startsWith('image/') ? 'image' : 'file';
        pendingAttachments.push({
            type,
            data: reader.result,
            filename: file.name,
        });
        renderAttachmentPreview();
    };
    reader.readAsDataURL(file);
}

function renderAttachmentPreview() {
    attachPreview.innerHTML = '';
    if (pendingAttachments.length === 0) {
        attachPreview.style.display = 'none';
        return;
    }
    attachPreview.style.display = 'flex';
    for (let i = 0; i < pendingAttachments.length; i++) {
        const att = pendingAttachments[i];
        const item = document.createElement('div');
        item.className = 'attach-item';

        if (att.type === 'image') {
            const img = document.createElement('img');
            img.src = att.data;
            img.alt = att.filename;
            item.appendChild(img);
        } else {
            const icon = document.createElement('span');
            icon.className = 'attach-file-icon';
            icon.textContent = att.filename;
            item.appendChild(icon);
        }

        const removeBtn = document.createElement('button');
        removeBtn.className = 'attach-remove';
        removeBtn.textContent = '\u00d7';
        removeBtn.onclick = () => {
            pendingAttachments.splice(i, 1);
            renderAttachmentPreview();
        };
        item.appendChild(removeBtn);
        attachPreview.appendChild(item);
    }
}

function clearAttachments() {
    pendingAttachments = [];
    attachPreview.innerHTML = '';
    attachPreview.style.display = 'none';
}

// 拖拽上传
const mainEl = document.querySelector('main');
mainEl.addEventListener('dragover', (e) => {
    e.preventDefault();
    mainEl.classList.add('drag-over');
});
mainEl.addEventListener('dragleave', () => mainEl.classList.remove('drag-over'));
mainEl.addEventListener('drop', (e) => {
    e.preventDefault();
    mainEl.classList.remove('drag-over');
    for (const file of e.dataTransfer.files) {
        readFileAsAttachment(file);
    }
});

// Events
sendBtn.addEventListener('click', sendMessage);

inputEl.addEventListener('keydown', (e) => {
    if (e.isComposing) return;
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
});

inputEl.addEventListener('input', () => {
    inputEl.style.height = 'auto';
    inputEl.style.height = Math.min(inputEl.scrollHeight, 150) + 'px';
});

newChatBtn.addEventListener('click', () => {
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send('/new');
        messagesEl.innerHTML = `
            <div class="welcome">
                <img class="welcome-logo" src="/logo.png" alt="KangClaw">
                <p class="welcome-subtitle">你的智能AI助手</p>
                <div class="welcome-hints">
                    <div class="welcome-hint">
                        <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M2 3h12v8H5l-3 3V3z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>
                        <span>随时和我聊天，我会记住你说过的话</span>
                    </div>
                    <div class="welcome-hint">
                        <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M9 1L4 9h4l-1 6 5-8H8l1-6z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>
                        <span>安装技能扩展我的能力</span>
                    </div>
                    <div class="welcome-hint">
                        <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="6.5" stroke="currentColor" stroke-width="1.3"/><path d="M8 4v4l2.5 2.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>
                        <span>设置定时任务，让我自动帮你做事</span>
                    </div>
                </div>
            </div>`;
        welcomeVisible = true;
        historyLoaded = true;
        // 不 disable sendBtn、不 show thinking
        // 如果 auto_greeting=true，服务端 token 到达时会自动清掉欢迎页
    }
});

// Start
connect();

// ── Page Navigation ──

const chatView = document.getElementById('chat-view');
const configPage = document.getElementById('config-page');
const configPageBody = document.getElementById('config-page-body');
const modelPage = document.getElementById('model-page');
const modelPageBody = document.getElementById('model-page-body');
const navChatBtn = document.getElementById('nav-chat');
const navModelBtn = document.getElementById('nav-model');
const navChannelsBtn = document.getElementById('nav-channels');
const navSkillsBtn = document.getElementById('nav-skills');
const navCronBtn = document.getElementById('nav-cron');
const navHeartbeatBtn = document.getElementById('nav-heartbeat');
const navSettingsBtn = document.getElementById('nav-settings');
const skillsPage = document.getElementById('skills-page');
const skillsPageBody = document.getElementById('skills-page-body');
const cronPage = document.getElementById('cron-page');
const cronPageBody = document.getElementById('cron-page-body');
const heartbeatPage = document.getElementById('heartbeat-page');
const heartbeatPageBody = document.getElementById('heartbeat-page-body');
const settingsPage = document.getElementById('settings-page');
const settingsPageBody = document.getElementById('settings-page-body');
const restartGatewayBtn = document.getElementById('restart-gateway');

const allPages = [chatView, configPage, modelPage, skillsPage, cronPage, heartbeatPage, settingsPage];
const allNavBtns = [navChatBtn, navModelBtn, navChannelsBtn, navSkillsBtn, navCronBtn, navHeartbeatBtn, navSettingsBtn];

function showPage(page, navBtn) {
    allPages.forEach(p => p.classList.add('hidden'));
    allNavBtns.forEach(b => b.classList.remove('active'));
    page.classList.remove('hidden');
    navBtn.classList.add('active');
    // Update URL path
    const pageId = page.dataset.page || '';
    const path = pageId ? '/' + pageId : '/';
    if (location.pathname !== path) {
        history.pushState(null, '', path);
    }
}

let channelSchema = null;
let cachedChannels = [];

const STATUS_LABELS = {
    online: '在线',
    offline: '离线',
    error: '启动失败',
    disabled: '未启用',
};

async function fetchChannelStatus() {
    try {
        const resp = await fetch('/api/channels');
        if (!resp.ok) return;
        cachedChannels = await resp.json();
    } catch (e) { /* silent */ }
}

async function showChannelsPage() {
    showPage(configPage, navChannelsBtn);
    await fetchChannelStatus();
    renderConfigPage(cachedChannels);
}

// ── Skills page ──

async function showSkillsPage() {
    showPage(skillsPage, navSkillsBtn);
    try {
        const resp = await fetch('/api/skills');
        if (!resp.ok) return;
        const skills = await resp.json();
        renderSkillsPage(skills);
    } catch (e) { /* silent */ }
}

function renderSkillsPage(skills) {
    skillsPageBody.innerHTML = '';
    if (!skills || skills.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'skill-empty-state';
        empty.innerHTML = '<svg width="40" height="40" viewBox="0 0 16 16" fill="none"><path d="M9 1L4 9h4l-1 6 5-8H8l1-6z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg><span>暂无用户技能</span>';
        skillsPageBody.appendChild(empty);
        return;
    }
    skills.forEach(skill => {
        skillsPageBody.appendChild(createSkillCard(skill));
    });
}

function createSkillCard(skill) {
    const card = document.createElement('div');
    card.className = 'skill-card';

    // Header
    const header = document.createElement('div');
    header.className = 'skill-card__header';
    header.innerHTML = `
        <div class="skill-card__info">
            <div class="skill-card__name">${skill.name}</div>
            <div class="skill-card__desc">${skill.description || ''}</div>
        </div>
        <svg class="skill-card__chevron" width="16" height="16" viewBox="0 0 16 16" fill="none">
            <path d="M6 3l5 5-5 5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
    `;
    header.addEventListener('click', () => card.classList.toggle('expanded'));

    // Delete button
    const delBtn = document.createElement('button');
    delBtn.className = 'skill-card__delete-btn';
    delBtn.title = '删除技能';
    delBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M2 4h10M5 4V2.5a.5.5 0 01.5-.5h3a.5.5 0 01.5.5V4M11 4v7.5a1 1 0 01-1 1H4a1 1 0 01-1-1V4" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/></svg><span>删除</span>';
    delBtn.addEventListener('click', async (e) => {
        e.stopPropagation();
        if (!confirm(`确定删除技能「${skill.name}」？此操作不可恢复。`)) return;
        try {
            const resp = await fetch(`/api/skills/${encodeURIComponent(skill.name)}`, { method: 'DELETE' });
            const result = await resp.json();
            if (result.ok) {
                card.remove();
                if (!skillsPageBody.querySelector('.skill-card')) {
                    renderSkillsPage([]);
                }
            } else {
                alert(result.error || '删除失败');
            }
        } catch (err) {
            alert('网络错误');
        }
    });
    header.insertBefore(delBtn, header.querySelector('.skill-card__chevron'));

    // Body (tree + editor)
    const body = document.createElement('div');
    body.className = 'skill-card__body';

    const tree = document.createElement('div');
    tree.className = 'skill-file-tree';
    renderFileTree(tree, skill.files, body, 0);

    const editor = document.createElement('div');
    editor.className = 'skill-editor';
    editor.innerHTML = '<div class="skill-editor__placeholder">点击左侧文件查看内容</div>';

    body.appendChild(tree);
    body.appendChild(editor);
    card.appendChild(header);
    card.appendChild(body);
    return card;
}

function renderFileTree(container, items, body, depth) {
    items.forEach(item => {
        if (item.type === 'dir') {
            const row = document.createElement('div');
            row.className = 'skill-tree-item';
            row.style.paddingLeft = (14 + depth * 16) + 'px';
            row.innerHTML = `
                <svg class="skill-tree-dir-arrow expanded" width="10" height="10" viewBox="0 0 10 10" fill="none">
                    <path d="M3 2l4 3-4 3" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>
                </svg>
                <svg class="skill-tree-item__icon" width="14" height="14" viewBox="0 0 14 14" fill="none">
                    <path d="M1 3h4l1.5 1.5H13v8H1V3z" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/>
                </svg>
                <span>${item.name}</span>
            `;
            container.appendChild(row);

            const childContainer = document.createElement('div');
            childContainer.className = 'skill-tree-dir-children expanded';
            container.appendChild(childContainer);

            row.addEventListener('click', () => {
                const arrow = row.querySelector('.skill-tree-dir-arrow');
                arrow.classList.toggle('expanded');
                childContainer.classList.toggle('expanded');
            });

            renderFileTree(childContainer, item.children, body, depth + 1);
        } else {
            const row = document.createElement('div');
            row.className = 'skill-tree-item';
            row.style.paddingLeft = (14 + depth * 16) + 'px';

            const ext = item.name.split('.').pop().toLowerCase();
            let fileIcon;
            if (ext === 'md') {
                fileIcon = '<svg class="skill-tree-item__icon" width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M2 1.5h7l3 3v8H2v-11z" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/><path d="M9 1.5v3h3" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/></svg>';
            } else if (ext === 'sh' || ext === 'py') {
                fileIcon = '<svg class="skill-tree-item__icon" width="14" height="14" viewBox="0 0 14 14" fill="none"><rect x="1" y="2" width="12" height="10" rx="1.5" stroke="currentColor" stroke-width="1.2"/><path d="M4 6l2 1.5L4 9" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/><path d="M7.5 9H10" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>';
            } else {
                fileIcon = '<svg class="skill-tree-item__icon" width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M2 1.5h7l3 3v8H2v-11z" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/></svg>';
            }

            row.innerHTML = `${fileIcon}<span>${item.name}</span>`;
            container.appendChild(row);

            row.addEventListener('click', () => {
                // Highlight active file
                body.querySelectorAll('.skill-tree-item.active').forEach(el => el.classList.remove('active'));
                row.classList.add('active');
                loadSkillFile(body, item.path);
            });
        }
    });
}

function renderMarkdown(text) {
    // Strip YAML frontmatter
    text = text.replace(/^---\n[\s\S]*?\n---\n?/, '');
    return marked.parse(text);
}

async function loadSkillFile(body, filePath) {
    const editor = body.querySelector('.skill-editor');
    editor.innerHTML = '<div class="skill-editor__placeholder">加载中...</div>';

    try {
        const resp = await fetch(`/api/skills/file?path=${encodeURIComponent(filePath)}`);
        const data = await resp.json();
        if (data.error) {
            editor.innerHTML = `<div class="skill-editor__placeholder">${data.error}</div>`;
            return;
        }

        const fileName = filePath.split('/').pop();
        editor.innerHTML = '';

        const header = document.createElement('div');
        header.className = 'skill-editor__header';

        const nameSpan = document.createElement('span');
        nameSpan.className = 'skill-editor__filename';
        nameSpan.textContent = filePath;

        const rightGroup = document.createElement('div');
        rightGroup.style.cssText = 'display:flex;align-items:center;gap:8px';

        const msgSpan = document.createElement('span');
        msgSpan.className = 'skill-editor__msg';

        const saveBtn = document.createElement('button');
        saveBtn.className = 'skill-editor__save-btn';
        saveBtn.textContent = '保存';
        saveBtn.disabled = true;

        rightGroup.appendChild(msgSpan);
        rightGroup.appendChild(saveBtn);

        // Preview button for .md files
        const isMd = filePath.endsWith('.md');
        let previewBtn = null;
        let previewDiv = null;
        if (isMd) {
            previewBtn = document.createElement('button');
            previewBtn.className = 'skill-editor__preview-btn';
            previewBtn.textContent = '预览';
            rightGroup.insertBefore(previewBtn, msgSpan);
        }

        header.appendChild(nameSpan);
        header.appendChild(rightGroup);

        const isCode = /\.(py|sh|bash|js|ts|json|yaml|yml|toml|ini|cfg|conf|rb|go|rs|java|c|cpp|h|hpp|css|html|xml|sql|lua|pl|r|swift|kt|scala|zig)$/i.test(filePath);
        const original = data.content;

        let editorBody;
        if (isCode) {
            // Code file: textarea with line number gutter
            editorBody = document.createElement('div');
            editorBody.className = 'skill-code-editor';

            const gutter = document.createElement('div');
            gutter.className = 'skill-code-gutter';

            const textarea = document.createElement('textarea');
            textarea.className = 'skill-editor__textarea skill-code-textarea';
            textarea.value = data.content;
            textarea.spellcheck = false;
            textarea.wrap = 'off';

            const updateGutter = () => {
                const count = textarea.value.split('\n').length;
                gutter.innerHTML = Array.from({ length: count }, (_, i) => `<span>${i + 1}</span>`).join('\n');
            };
            updateGutter();

            textarea.addEventListener('input', () => {
                updateGutter();
                saveBtn.disabled = textarea.value === original;
                msgSpan.textContent = '';
            });

            textarea.addEventListener('scroll', () => {
                gutter.scrollTop = textarea.scrollTop;
            });

            textarea.addEventListener('keydown', (e) => {
                if (e.key === 'Tab') {
                    e.preventDefault();
                    const start = textarea.selectionStart;
                    const end = textarea.selectionEnd;
                    textarea.value = textarea.value.substring(0, start) + '    ' + textarea.value.substring(end);
                    textarea.selectionStart = textarea.selectionEnd = start + 4;
                    textarea.dispatchEvent(new Event('input'));
                }
            });

            editorBody.appendChild(gutter);
            editorBody.appendChild(textarea);
            editorBody._textarea = textarea;
        } else {
            // Non-code file: plain textarea
            const textarea = document.createElement('textarea');
            textarea.className = 'skill-editor__textarea';
            textarea.value = data.content;
            textarea.spellcheck = false;

            textarea.addEventListener('input', () => {
                saveBtn.disabled = textarea.value === original;
                msgSpan.textContent = '';
            });

            textarea.addEventListener('keydown', (e) => {
                if (e.key === 'Tab') {
                    e.preventDefault();
                    const start = textarea.selectionStart;
                    const end = textarea.selectionEnd;
                    textarea.value = textarea.value.substring(0, start) + '    ' + textarea.value.substring(end);
                    textarea.selectionStart = textarea.selectionEnd = start + 4;
                    textarea.dispatchEvent(new Event('input'));
                }
            });

            editorBody = textarea;
            editorBody._textarea = textarea;
        }

        const getTextarea = () => editorBody._textarea || editorBody;

        // Save
        saveBtn.addEventListener('click', async () => {
            const textarea = getTextarea();
            saveBtn.disabled = true;
            saveBtn.textContent = '保存中...';
            try {
                const resp = await fetch('/api/skills/file', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ path: filePath, content: textarea.value }),
                });
                const result = await resp.json();
                if (result.ok) {
                    msgSpan.textContent = '已保存';
                    msgSpan.className = 'skill-editor__msg success';
                } else {
                    msgSpan.textContent = result.error || '保存失败';
                    msgSpan.className = 'skill-editor__msg error';
                    saveBtn.disabled = false;
                }
            } catch (e) {
                msgSpan.textContent = '网络错误';
                msgSpan.className = 'skill-editor__msg error';
                saveBtn.disabled = false;
            }
            saveBtn.textContent = '保存';
            setTimeout(() => { msgSpan.textContent = ''; }, 3000);
        });

        editor.appendChild(header);
        editor.appendChild(editorBody);

        // Preview toggle for .md files
        if (isMd) {
            previewDiv = document.createElement('div');
            previewDiv.className = 'skill-editor__preview';
            previewDiv.style.display = 'none';
            editor.appendChild(previewDiv);

            const ta = getTextarea();
            previewBtn.addEventListener('click', () => {
                const previewing = editorBody.style.display === 'none';
                if (previewing) {
                    // Switch to edit mode
                    editorBody.style.display = '';
                    previewDiv.style.display = 'none';
                    previewBtn.textContent = '预览';
                    saveBtn.style.display = '';
                } else {
                    // Switch to preview mode
                    previewDiv.innerHTML = renderMarkdown(ta.value);
                    editorBody.style.display = 'none';
                    previewDiv.style.display = '';
                    previewBtn.textContent = '编辑';
                    saveBtn.style.display = 'none';
                }
            });
        }
    } catch (e) {
        editor.innerHTML = '<div class="skill-editor__placeholder">加载失败</div>';
    }
}

// ── Shared: custom dropdown ──

/**
 * 创建自定义下拉选择器。
 * @param {Object} opts
 * @param {string} opts.key - data-key 属性
 * @param {string} opts.value - 当前选中值
 * @param {{value: string, label: string}[]} opts.options - 选项列表
 * @param {string} [opts.placeholder] - 未选中时的占位文字
 * @param {function} [opts.onChange] - 选中回调，参数为选中的 value
 * @returns {HTMLElement}
 */
function createDropdown({ key, value, options, placeholder = '请选择', onChange }) {
    const dropdown = document.createElement('div');
    dropdown.className = 'config-dropdown';
    dropdown.dataset.key = key;

    const selected = document.createElement('div');
    selected.className = 'config-dropdown__selected';
    const current = options.find(o => o.value === value);
    selected.textContent = current ? current.label : (value || placeholder);
    selected.dataset.value = value || '';

    const arrow = document.createElement('svg');
    arrow.className = 'config-dropdown__arrow';
    arrow.setAttribute('width', '10');
    arrow.setAttribute('height', '6');
    arrow.setAttribute('viewBox', '0 0 10 6');
    arrow.innerHTML = '<path d="M1 1l4 4 4-4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>';

    const menu = document.createElement('div');
    menu.className = 'config-dropdown__menu';
    for (const opt of options) {
        const item = document.createElement('div');
        item.className = 'config-dropdown__item';
        if (opt.value === value) item.classList.add('active');
        item.textContent = opt.label;
        item.dataset.value = opt.value;
        item.addEventListener('click', (e) => {
            e.stopPropagation();
            selected.textContent = item.textContent;
            selected.dataset.value = opt.value;
            menu.querySelectorAll('.config-dropdown__item').forEach(i => i.classList.remove('active'));
            item.classList.add('active');
            dropdown.classList.remove('open');
            if (onChange) onChange(opt.value);
        });
        menu.appendChild(item);
    }

    selected.addEventListener('click', (e) => {
        e.stopPropagation();
        document.querySelectorAll('.config-dropdown.open').forEach(d => {
            if (d !== dropdown) d.classList.remove('open');
        });
        dropdown.classList.toggle('open');
    });

    dropdown.appendChild(selected);
    dropdown.appendChild(arrow);
    dropdown.appendChild(menu);
    return dropdown;
}

// ── Shared: password field with eye toggle ──

const EYE_OPEN = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M1 8s2.5-5 7-5 7 5 7 5-2.5 5-7 5-7-5-7-5z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/><circle cx="8" cy="8" r="2" stroke="currentColor" stroke-width="1.3"/></svg>';
const EYE_CLOSED = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M1 8s2.5-5 7-5 7 5 7 5-2.5 5-7 5-7-5-7-5z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/><circle cx="8" cy="8" r="2" stroke="currentColor" stroke-width="1.3"/><path d="M2.5 13.5l11-11" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>';

function createPasswordInput(key, value) {
    const wrapper = document.createElement('div');
    wrapper.className = 'config-input-password';
    const input = document.createElement('input');
    input.className = 'config-input';
    input.type = 'password';
    input.dataset.key = key;
    input.value = value || '';
    input.placeholder = 'Enter value or ${ENV_VAR}';
    const eyeBtn = document.createElement('button');
    eyeBtn.type = 'button';
    eyeBtn.className = 'toggle-visibility';
    eyeBtn.innerHTML = EYE_CLOSED;
    eyeBtn.addEventListener('click', () => {
        const hidden = input.type === 'password';
        input.type = hidden ? 'text' : 'password';
        eyeBtn.innerHTML = hidden ? EYE_OPEN : EYE_CLOSED;
    });
    wrapper.appendChild(input);
    wrapper.appendChild(eyeBtn);
    return wrapper;
}

// ── Model Configuration Page ──

async function showModelPage() {
    showPage(modelPage, navModelBtn);
    await fetchModelConfig();
    renderModelPage();
}

let cachedModels = [];
let cachedModelSchema = null;
let cachedActivePrimaryKey = '';

async function fetchModelConfig() {
    try {
        const resp = await fetch('/api/model');
        if (!resp.ok) return;
        const data = await resp.json();
        cachedModelSchema = data.schema;
        cachedModels = data.models || [];
        cachedActivePrimaryKey = data.active_primary_key || '';
    } catch (e) { /* silent */ }
}

function renderModelPage() {
    modelPageBody.innerHTML = '';
    if (!cachedModelSchema) return;

    const providers = cachedModelSchema.providers || {};
    const fields = cachedModelSchema.fields || [];

    // 模型列表容器
    const listWrap = document.createElement('div');
    listWrap.className = 'model-list';

    for (let idx = 0; idx < cachedModels.length; idx++) {
        const m = cachedModels[idx];
        const isActive = m.primary_key === cachedActivePrimaryKey;
        listWrap.appendChild(createModelCard(m, idx, isActive, fields, providers));
    }
    modelPageBody.appendChild(listWrap);
}

function createModelCard(model, idx, isActive, fields, providers) {
    const card = document.createElement('div');
    card.className = 'model-card' + (model._isNew ? ' expanded' : '') + (isActive ? ' model-card--active' : '');
    card.dataset.idx = idx;

    // Header
    const header = document.createElement('div');
    header.className = 'model-card__header';

    // 左侧：激活指示条 + 信息
    const indicator = document.createElement('div');
    indicator.className = 'model-card__indicator';

    const info = document.createElement('div');
    info.className = 'model-card__info';

    const nameRow = document.createElement('div');
    nameRow.className = 'model-card__name-row';
    const nameEl = document.createElement('span');
    nameEl.className = 'model-card__name';
    nameEl.textContent = model.show_name || model.id || '新模型';
    nameRow.appendChild(nameEl);
    if (isActive) {
        const badge = document.createElement('span');
        badge.className = 'model-card__badge';
        badge.textContent = '当前使用';
        nameRow.appendChild(badge);
    }

    const providerInfo = providers[model.provider] || {};
    const metaEl = document.createElement('div');
    metaEl.className = 'model-card__meta';
    const displayUrl = model.base_url || providerInfo.default_base_url || '';
    metaEl.innerHTML =
        `<div class="model-card__meta-row"><span class="model-card__meta-label">模型ID</span> <span class="model-card__meta-value">${model.id || '-'}</span></div>` +
        `<div class="model-card__meta-row"><span class="model-card__meta-label">Base URL</span> <span class="model-card__meta-value">${displayUrl || '-'}</span></div>`;

    info.appendChild(nameRow);
    info.appendChild(metaEl);

    // 右侧：开关 + 展开箭头
    const controls = document.createElement('div');
    controls.className = 'model-card__controls';

    const toggleSwitch = document.createElement('label');
    toggleSwitch.className = 'toggle-switch';
    const toggleInput = document.createElement('input');
    toggleInput.type = 'checkbox';
    toggleInput.checked = isActive;
    toggleInput.addEventListener('change', async (e) => {
        if (e.target.checked) {
            cachedActivePrimaryKey = model.primary_key;
            try {
                await fetch('/api/model', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ models: cachedModels, active_primary_key: cachedActivePrimaryKey }),
                });
            } catch (err) { /* silent */ }
            renderModelPage();
        } else {
            // 不允许关闭唯一激活的模型
            e.target.checked = true;
        }
    });
    const toggleSlider = document.createElement('span');
    toggleSlider.className = 'toggle-slider';
    toggleSwitch.appendChild(toggleInput);
    toggleSwitch.appendChild(toggleSlider);

    const chevron = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    chevron.setAttribute('width', '16');
    chevron.setAttribute('height', '16');
    chevron.setAttribute('viewBox', '0 0 16 16');
    chevron.setAttribute('fill', 'none');
    chevron.classList.add('model-card__chevron');
    chevron.innerHTML = '<path d="M6 3l5 5-5 5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>';

    controls.appendChild(toggleSwitch);
    controls.appendChild(chevron);

    header.appendChild(indicator);
    header.appendChild(info);
    header.appendChild(controls);

    header.addEventListener('click', (e) => {
        if (e.target.closest('.toggle-switch')) return;
        card.classList.toggle('expanded');
    });

    card.appendChild(header);

    // Body (collapsible)
    const body = document.createElement('div');
    body.className = 'model-card__body';

    for (const f of fields) {
        if (f.type === 'hidden') continue;

        const fieldDiv = document.createElement('div');
        fieldDiv.className = 'config-field';
        const label = document.createElement('label');
        label.className = 'config-label';
        label.textContent = f.label;

        const val = model[f.key];

        if (f.type === 'select') {
            const dropdown = createDropdown({
                key: f.key,
                value: val,
                options: f.options.map(opt => ({
                    value: opt,
                    label: (providers[opt] || {}).show_name || opt,
                })),
                placeholder: '选择供应商',
                onChange: (selVal) => {
                    const selPi = providers[selVal];
                    const baseUrlInput = card.querySelector('[data-key="base_url"]');
                    if (baseUrlInput && selPi) {
                        baseUrlInput.value = selPi.default_base_url || '';
                    }
                },
            });
            fieldDiv.appendChild(label);
            fieldDiv.appendChild(dropdown);
        } else if (f.type === 'number') {
            const input = document.createElement('input');
            input.className = 'config-input';
            input.type = 'text';
            input.inputMode = 'numeric';
            input.pattern = '[0-9]*';
            input.dataset.key = f.key;
            // K 单位字段：显示时除以 1000
            const isKUnit = f.key === 'context_window_tokens';
            input.value = (isKUnit && val) ? val / 1000 : (val ?? '');
            if (f.min !== undefined) input.min = f.min;
            if (f.max !== undefined) input.max = f.max;
            if (f.step !== undefined) input.step = f.step;
            if (isKUnit) {
                const tip = document.createElement('span');
                tip.className = 'config-tooltip-icon';
                tip.textContent = '?';
                tip.dataset.tip = '默认0代表不配置；配置后，系统可以根据此值动态计算上下文压缩时机';
                label.appendChild(tip);
            }
            fieldDiv.appendChild(label);
            if (isKUnit) {
                const wrapper = document.createElement('div');
                wrapper.style.cssText = 'position:relative;display:flex;align-items:center';
                input.style.paddingRight = '28px';
                const unit = document.createElement('span');
                unit.style.cssText = 'position:absolute;right:10px;color:var(--text-muted);font-size:12px;pointer-events:none';
                unit.textContent = 'K';
                wrapper.appendChild(input);
                wrapper.appendChild(unit);
                fieldDiv.appendChild(wrapper);
            } else {
                fieldDiv.appendChild(input);
            }
        } else if (f.type === 'password') {
            fieldDiv.appendChild(label);
            fieldDiv.appendChild(createPasswordInput(f.key, val));
        } else {
            const input = document.createElement('input');
            input.className = 'config-input';
            input.type = 'text';
            input.dataset.key = f.key;
            input.value = val || '';
            fieldDiv.appendChild(label);
            fieldDiv.appendChild(input);
        }

        body.appendChild(fieldDiv);
    }

    // 操作按钮
    const actions = document.createElement('div');
    actions.className = 'model-card__actions';

    const delBtn = document.createElement('button');
    delBtn.className = 'model-delete-btn';
    delBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M2 4h10M5 4V2.5a.5.5 0 01.5-.5h3a.5.5 0 01.5.5V4M11 4v7.5a1 1 0 01-1 1H4a1 1 0 01-1-1V4" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/></svg><span>删除</span>';
    delBtn.addEventListener('click', () => {
        if (!confirm('确定要删除这个模型配置吗？')) return;
        cachedModels.splice(idx, 1);
        if (model.primary_key === cachedActivePrimaryKey && cachedModels.length > 0) {
            cachedActivePrimaryKey = cachedModels[0].primary_key;
        }
        renderModelPage();
    });

    const saveBtn = document.createElement('button');
    saveBtn.className = 'config-btn primary';
    saveBtn.textContent = '保存';
    saveBtn.addEventListener('click', () => saveSingleModel(card, idx));

    actions.appendChild(delBtn);
    actions.appendChild(saveBtn);
    body.appendChild(actions);

    card.appendChild(body);
    return card;
}

function collectModelFromCard(card, fields) {
    const model = {};
    for (const f of fields) {
        if (f.type === 'hidden') {
            // primary_key 从 cachedModels 取
            continue;
        }
        if (f.type === 'number') {
            const input = card.querySelector(`[data-key="${f.key}"]`);
            let numVal = input ? parseFloat(input.value) || 0 : 0;
            // K 单位字段：保存时乘以 1000
            if (f.key === 'context_window_tokens') {
                numVal = Math.round(numVal * 1000);
            }
            model[f.key] = numVal;
        } else if (f.type === 'select') {
            const dropdown = card.querySelector(`.config-dropdown[data-key="${f.key}"]`);
            model[f.key] = dropdown ? dropdown.querySelector('.config-dropdown__selected').dataset.value : '';
        } else {
            const input = card.querySelector(`[data-key="${f.key}"]`);
            model[f.key] = input ? input.value : '';
        }
    }
    return model;
}

async function saveSingleModel(card, idx) {
    if (!cachedModelSchema) return;
    const fields = cachedModelSchema.fields || [];
    const collected = collectModelFromCard(card, fields);
    collected.primary_key = cachedModels[idx] ? cachedModels[idx].primary_key : '';

    // 更新 cachedModels 中对应项
    cachedModels[idx] = collected;

    // 构建完整模型列表发送
    const allModels = cachedModels.map(m => {
        const obj = {};
        for (const f of fields) {
            obj[f.key] = m[f.key] ?? '';
        }
        obj.primary_key = m.primary_key;
        return obj;
    });

    const saveBtn = card.querySelector('.config-btn.primary');
    if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = '保存中...'; }

    try {
        const resp = await fetch('/api/model', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ models: allModels, active_primary_key: cachedActivePrimaryKey }),
        });
        const result = await resp.json();

        if (result.error) {
            alert(result.error);
        } else {
            await fetchModelConfig();
            renderModelPage();
        }
    } catch (e) {
        alert('Network error');
    } finally {
        if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = '保存'; }
    }
}

async function saveAllModels() {
    if (!cachedModelSchema) return;
    const fields = cachedModelSchema.fields || [];
    const cards = document.querySelectorAll('.model-card');
    const models = [];

    cards.forEach((card, i) => {
        const idx = parseInt(card.dataset.idx);
        const collected = collectModelFromCard(card, fields);
        // 保留 primary_key
        collected.primary_key = cachedModels[idx] ? cachedModels[idx].primary_key : '';
        models.push(collected);
    });

    try {
        const resp = await fetch('/api/model', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ models, active_primary_key: cachedActivePrimaryKey }),
        });
        const result = await resp.json();

        if (result.error) {
            alert(result.error);
        } else {
            await fetchModelConfig();
            renderModelPage();
        }
    } catch (e) {
        alert('Network error');
    }
}

// ── Channel Configuration Page ──

async function getChannelSchema() {
    if (channelSchema) return channelSchema;
    try {
        const resp = await fetch('/api/channels/schema');
        if (resp.ok) channelSchema = await resp.json();
    } catch (e) { /* silent */ }
    return channelSchema || {};
}

async function renderConfigPage(channels) {
    const schema = await getChannelSchema();
    configPageBody.innerHTML = '';

    for (const ch of channels) {
        const chSchema = schema[ch.name];
        if (!chSchema) continue;

        const card = document.createElement('div');
        card.className = 'channel-card';
        card.dataset.channel = ch.name;

        // Header
        const header = document.createElement('div');
        header.className = 'channel-card-header';

        const info = document.createElement('div');
        info.className = 'channel-card-info';
        const nameEl = document.createElement('div');
        nameEl.className = 'channel-card-name';
        nameEl.textContent = ch.label || ch.name;
        const statusEl2 = document.createElement('div');
        statusEl2.className = 'channel-card-status';
        statusEl2.textContent = STATUS_LABELS[ch.status] || ch.status;
        if (ch.error) statusEl2.textContent += ` — ${ch.error}`;
        info.appendChild(nameEl);
        info.appendChild(statusEl2);

        const chevron = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        chevron.setAttribute('width', '16');
        chevron.setAttribute('height', '16');
        chevron.setAttribute('viewBox', '0 0 16 16');
        chevron.setAttribute('fill', 'none');
        chevron.classList.add('channel-card-chevron');
        chevron.innerHTML = '<path d="M6 3l5 5-5 5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>';

        header.appendChild(info);
        header.appendChild(chevron);

        header.addEventListener('click', () => {
            card.classList.toggle('expanded');
        });

        // Body (form)
        const body = document.createElement('div');
        body.className = 'channel-card-body';

        // 启用开关（作为普通配置字段）
        const enabledField = document.createElement('div');
        enabledField.className = 'config-field';
        const enabledLabel = document.createElement('label');
        enabledLabel.className = 'config-label';
        enabledLabel.textContent = '启用开关';
        const toggleSwitch = document.createElement('label');
        toggleSwitch.className = 'toggle-switch';
        const toggleInput = document.createElement('input');
        toggleInput.type = 'checkbox';
        toggleInput.className = 'channel-enabled-toggle';
        toggleInput.dataset.channel = ch.name;
        toggleInput.checked = ch.enabled;
        const toggleSlider = document.createElement('span');
        toggleSlider.className = 'toggle-slider';
        toggleSwitch.appendChild(toggleInput);
        toggleSwitch.appendChild(toggleSlider);
        enabledField.appendChild(enabledLabel);
        enabledField.appendChild(toggleSwitch);
        body.appendChild(enabledField);

        // Fields
        for (const f of chSchema.fields) {
            const fieldDiv = document.createElement('div');
            fieldDiv.className = 'config-field';

            const label = document.createElement('label');
            label.className = 'config-label';
            label.textContent = f.label;

            const val = ch.config[f.key];

            if (f.type === 'password') {
                const pwWrapper = createPasswordInput(f.key, val || '');
                pwWrapper.querySelector('input').dataset.channel = ch.name;
                fieldDiv.appendChild(label);
                fieldDiv.appendChild(pwWrapper);
            } else if (f.type === 'list') {
                const input = document.createElement('input');
                input.className = 'config-input';
                input.dataset.channel = ch.name;
                input.dataset.key = f.key;
                input.type = 'text';
                input.value = Array.isArray(val) ? val.join(', ') : (val || '');
                input.placeholder = 'comma separated, e.g. user1, user2';
                fieldDiv.appendChild(label);
                fieldDiv.appendChild(input);
                const hint = document.createElement('div');
                hint.className = 'config-hint';
                hint.textContent = '可以用来限制Agent接收消息的聊天open_id，留空代表允许所有';
                fieldDiv.appendChild(hint);
            } else {
                const input = document.createElement('input');
                input.className = 'config-input';
                input.dataset.channel = ch.name;
                input.dataset.key = f.key;
                input.type = 'text';
                input.value = val || '';
                fieldDiv.appendChild(label);
                fieldDiv.appendChild(input);
            }

            body.appendChild(fieldDiv);
        }

        // Actions
        const actions = document.createElement('div');
        actions.className = 'channel-card-actions';
        const saveBtn = document.createElement('button');
        saveBtn.className = 'config-btn primary';
        saveBtn.textContent = '保存';
        saveBtn.addEventListener('click', () => saveChannel(ch.name, card));
        actions.appendChild(saveBtn);
        body.appendChild(actions);

        card.appendChild(header);
        card.appendChild(body);
        configPageBody.appendChild(card);
    }
}

async function saveChannel(name, card) {
    const schema = await getChannelSchema();
    const chSchema = schema[name];
    if (!chSchema) return;

    const body = {};
    const toggle = card.querySelector('.channel-enabled-toggle');
    body.enabled = toggle ? toggle.checked : false;

    for (const f of chSchema.fields) {
        const input = card.querySelector(`[data-key="${f.key}"]`);
        if (!input) continue;
        if (f.type === 'list') {
            const raw = input.value.trim();
            body[f.key] = raw ? raw.split(',').map(s => s.trim()).filter(Boolean) : [];
        } else {
            body[f.key] = input.value;
        }
    }

    const saveBtn = card.querySelector('.config-btn.primary');
    if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = 'Saving...'; }

    try {
        const resp = await fetch(`/api/channels/${name}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const result = await resp.json();

        const oldMsg = card.querySelector('.config-msg');
        if (oldMsg) oldMsg.remove();

        const msgDiv = document.createElement('div');
        if (result.error) {
            msgDiv.className = 'config-msg error';
            msgDiv.textContent = result.error;
        } else {
            msgDiv.className = 'config-msg success';
            msgDiv.textContent = 'Saved successfully';
            setTimeout(() => msgDiv.remove(), 3000);
        }
        card.querySelector('.channel-card-body').appendChild(msgDiv);
        // 保存后刷新配置页和重启状态
        await fetchChannelStatus();
        renderConfigPage(cachedChannels);
        checkConfigStatus();
    } catch (e) {
        const msgDiv = document.createElement('div');
        msgDiv.className = 'config-msg error';
        msgDiv.textContent = 'Network error';
        card.querySelector('.channel-card-body').appendChild(msgDiv);
    } finally {
        if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = 'Save'; }
    }
}

// ── Cron page ──

async function showCronPage() {
    showPage(cronPage, navCronBtn);
    try {
        const resp = await fetch('/api/cron');
        const jobs = resp.ok ? await resp.json() : [];
        renderCronPage(jobs);
    } catch (e) { /* silent */ }
}

function renderCronPage(jobs) {
    cronPageBody.innerHTML = '';

    if (!jobs || jobs.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'cron-empty-state';
        empty.innerHTML = '<svg width="40" height="40" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="6.5" stroke="currentColor" stroke-width="1.3"/><path d="M8 4v4l2.5 2.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg><span>暂无定时任务</span><span style="font-size:12px;color:var(--text-muted)">通过聊天创建，如"每天早上9点提醒我喝水"</span>';
        cronPageBody.appendChild(empty);
        return;
    }
    jobs.forEach(job => {
        cronPageBody.appendChild(createCronCard(job));
    });
}

function showCronMsg(el, text, type) {
    el.textContent = text;
    el.className = 'config-msg ' + type;
    el.style.display = 'block';
    if (type === 'success') {
        setTimeout(() => { el.style.display = 'none'; }, 3000);
    }
}

function createCronCard(job) {
    const card = document.createElement('div');
    card.className = 'cron-card';

    // Header
    const header = document.createElement('div');
    header.className = 'cron-card__header';
    header.innerHTML = `
        <div class="cron-card__info">
            <div class="cron-card__desc">${job.description || '(无描述)'}</div>
            <div class="cron-card__meta">
                <span class="cron-card__cron-expr">${job.cron_expr}</span>
                <span class="cron-card__channel">渠道: ${job.channel || 'web'}</span>
            </div>
        </div>
        <svg class="cron-card__chevron" width="16" height="16" viewBox="0 0 16 16" fill="none">
            <path d="M6 3l5 5-5 5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
    `;
    header.addEventListener('click', () => card.classList.toggle('expanded'));

    // Body (edit form)
    const body = document.createElement('div');
    body.className = 'cron-card__body';

    // Cron expression field
    const cronField = document.createElement('div');
    cronField.className = 'config-field';
    const cronLabel = document.createElement('label');
    cronLabel.className = 'config-label';
    cronLabel.textContent = 'Cron 表达式';
    const cronInput = document.createElement('input');
    cronInput.className = 'config-input';
    cronInput.type = 'text';
    cronInput.value = job.cron_expr;
    cronInput.placeholder = '分 时 日 月 星期，如 0 9 * * *';
    cronField.appendChild(cronLabel);
    cronField.appendChild(cronInput);

    // Description field
    const descField = document.createElement('div');
    descField.className = 'config-field';
    const descLabel = document.createElement('label');
    descLabel.className = 'config-label';
    descLabel.textContent = '任务描述';
    const descInput = document.createElement('input');
    descInput.className = 'config-input';
    descInput.type = 'text';
    descInput.value = job.description;
    descField.appendChild(descLabel);
    descField.appendChild(descInput);

    // Channel (read-only info)
    const chField = document.createElement('div');
    chField.className = 'config-field';
    const chLabel = document.createElement('label');
    chLabel.className = 'config-label';
    chLabel.textContent = '推送渠道';
    const chInput = document.createElement('input');
    chInput.className = 'config-input';
    chInput.type = 'text';
    chInput.value = job.channel || 'web';
    chInput.disabled = true;
    chInput.style.opacity = '0.6';
    chField.appendChild(chLabel);
    chField.appendChild(chInput);

    // Actions
    const actions = document.createElement('div');
    actions.className = 'cron-card__actions';

    const delBtn = document.createElement('button');
    delBtn.className = 'cron-delete-btn';
    delBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M2 4h10M5 4V2.5a.5.5 0 01.5-.5h3a.5.5 0 01.5.5V4M11 4v7.5a1 1 0 01-1 1H4a1 1 0 01-1-1V4" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/></svg><span>删除</span>';
    delBtn.addEventListener('click', async () => {
        if (!confirm('确定要删除这个定时任务吗？')) return;
        try {
            const resp = await fetch(`/api/cron/${job.id}`, { method: 'DELETE' });
            const result = await resp.json();
            if (result.ok) {
                showCronPage();
            } else {
                showCronMsg(msgEl, result.error || '删除失败', 'error');
            }
        } catch (e) {
            showCronMsg(msgEl, '网络错误', 'error');
        }
    });

    const saveBtn = document.createElement('button');
    saveBtn.className = 'config-btn primary';
    saveBtn.textContent = '保存';
    saveBtn.addEventListener('click', async () => {
        saveBtn.disabled = true;
        saveBtn.textContent = '保存中...';
        msgEl.style.display = 'none';
        try {
            const resp = await fetch(`/api/cron/${job.id}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    cron_expr: cronInput.value.trim(),
                    description: descInput.value.trim(),
                }),
            });
            const result = await resp.json();
            if (result.ok) {
                showCronMsg(msgEl, '已保存', 'success');
                // 刷新卡片头部信息
                const descEl = card.querySelector('.cron-card__desc');
                const exprEl = card.querySelector('.cron-card__cron-expr');
                if (descEl) descEl.textContent = descInput.value.trim() || '(无描述)';
                if (exprEl) exprEl.textContent = cronInput.value.trim();
            } else {
                showCronMsg(msgEl, result.error || '保存失败', 'error');
            }
        } catch (e) {
            showCronMsg(msgEl, '网络错误', 'error');
        } finally {
            saveBtn.disabled = false;
            saveBtn.textContent = '保存';
        }
    });

    actions.appendChild(delBtn);
    actions.appendChild(saveBtn);

    // 内联消息区域
    const msgEl = document.createElement('div');
    msgEl.className = 'config-msg';
    msgEl.style.display = 'none';

    body.appendChild(cronField);
    body.appendChild(descField);
    body.appendChild(chField);
    body.appendChild(actions);
    body.appendChild(msgEl);

    card.appendChild(header);
    card.appendChild(body);
    return card;
}

// ── Heartbeat page ──

async function showHeartbeatPage() {
    showPage(heartbeatPage, navHeartbeatBtn);
    try {
        const [hbResp, fileResp] = await Promise.all([
            fetch('/api/heartbeat'),
            fetch('/api/heartbeat/file'),
        ]);
        const hb = hbResp.ok ? await hbResp.json() : { enabled: false, interval_minutes: 30 };
        const fileData = fileResp.ok ? await fileResp.json() : { content: '' };
        renderHeartbeatPage(hb, fileData.content || '');
    } catch (e) { /* silent */ }
}

function renderHeartbeatPage(hb, fileContent) {
    heartbeatPageBody.innerHTML = '';

    // ── 设置区域 ──
    const settings = document.createElement('div');
    settings.className = 'heartbeat-settings';

    // 启用开关
    const enableRow = document.createElement('div');
    enableRow.className = 'heartbeat-settings__row';
    const enableLabelGroup = document.createElement('div');
    enableLabelGroup.className = 'heartbeat-settings__label-group';
    enableLabelGroup.innerHTML = '<div class="heartbeat-settings__label">启用心跳巡检</div><div class="heartbeat-settings__hint">开启后按间隔周期读取下方清单并执行</div>';
    const enableControl = document.createElement('div');
    enableControl.className = 'heartbeat-settings__control';
    const enableToggle = document.createElement('label');
    enableToggle.className = 'toggle-switch';
    const enableInput = document.createElement('input');
    enableInput.type = 'checkbox';
    enableInput.checked = hb.enabled;
    const enableSlider = document.createElement('span');
    enableSlider.className = 'toggle-slider';
    enableToggle.appendChild(enableInput);
    enableToggle.appendChild(enableSlider);
    enableControl.appendChild(enableToggle);
    enableRow.appendChild(enableLabelGroup);
    enableRow.appendChild(enableControl);

    // 间隔设置
    const intervalRow = document.createElement('div');
    intervalRow.className = 'heartbeat-settings__row';
    const intervalLabelGroup = document.createElement('div');
    intervalLabelGroup.className = 'heartbeat-settings__label-group';
    intervalLabelGroup.innerHTML = '<div class="heartbeat-settings__label">巡检间隔</div><div class="heartbeat-settings__hint">每隔多少分钟执行一次巡检</div>';
    const intervalControl = document.createElement('div');
    intervalControl.className = 'heartbeat-settings__control';
    const intervalInput = document.createElement('input');
    intervalInput.className = 'config-input';
    intervalInput.type = 'number';
    intervalInput.min = '1';
    intervalInput.value = hb.interval_minutes;
    const intervalUnit = document.createElement('span');
    intervalUnit.className = 'heartbeat-settings__unit';
    intervalUnit.textContent = '分钟';
    intervalControl.appendChild(intervalInput);
    intervalControl.appendChild(intervalUnit);
    intervalRow.appendChild(intervalLabelGroup);
    intervalRow.appendChild(intervalControl);

    // 保存按钮行
    const actionRow = document.createElement('div');
    actionRow.className = 'heartbeat-settings__actions';
    const settingsMsg = document.createElement('span');
    settingsMsg.className = 'skill-editor__msg';
    const saveSettingsBtn = document.createElement('button');
    saveSettingsBtn.className = 'config-btn primary';
    saveSettingsBtn.textContent = '保存设置';
    saveSettingsBtn.addEventListener('click', async () => {
        saveSettingsBtn.disabled = true;
        saveSettingsBtn.textContent = '保存中...';
        settingsMsg.textContent = '';
        try {
            const resp = await fetch('/api/heartbeat', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    enabled: enableInput.checked,
                    interval_minutes: parseInt(intervalInput.value) || 30,
                }),
            });
            const result = await resp.json();
            if (result.ok) {
                settingsMsg.textContent = '已保存';
                settingsMsg.className = 'skill-editor__msg success';
            } else {
                settingsMsg.textContent = result.error || '保存失败';
                settingsMsg.className = 'skill-editor__msg error';
            }
        } catch (e) {
            settingsMsg.textContent = '网络错误';
            settingsMsg.className = 'skill-editor__msg error';
        } finally {
            saveSettingsBtn.disabled = false;
            saveSettingsBtn.textContent = '保存设置';
            setTimeout(() => { settingsMsg.textContent = ''; }, 3000);
        }
    });
    actionRow.appendChild(settingsMsg);
    actionRow.appendChild(saveSettingsBtn);

    settings.appendChild(enableRow);
    settings.appendChild(intervalRow);
    settings.appendChild(actionRow);

    // ── HEARTBEAT.md 只读查看 ──
    const editor = document.createElement('div');
    editor.className = 'heartbeat-editor';

    const editorHeader = document.createElement('div');
    editorHeader.className = 'heartbeat-editor__header';
    const fileName = document.createElement('span');
    fileName.className = 'heartbeat-editor__filename';
    fileName.textContent = 'HEARTBEAT.md';
    const hint = document.createElement('span');
    hint.className = 'heartbeat-editor__hint';
    hint.textContent = '以下内容会由你的AI助手根据你的要求自动编辑，不需要你来编辑';
    const nameGroup = document.createElement('div');
    nameGroup.className = 'heartbeat-editor__name-group';
    nameGroup.appendChild(fileName);
    nameGroup.appendChild(hint);
    editorHeader.appendChild(nameGroup);

    const viewer = document.createElement('div');
    viewer.className = 'heartbeat-viewer';
    if (fileContent.trim()) {
        viewer.innerHTML = marked.parse(fileContent);
    } else {
        viewer.innerHTML = '<span class="heartbeat-viewer__empty">暂无巡检清单，请在 HEARTBEAT.md 文件中编辑。</span>';
    }

    editor.appendChild(editorHeader);
    editor.appendChild(viewer);

    heartbeatPageBody.appendChild(settings);
    heartbeatPageBody.appendChild(editor);
}

// ── Restart Gateway ──

let restartConfirmTimer = null;
const restartBanner = document.getElementById('restart-banner');
const bannerRestartBtn = document.getElementById('banner-restart');
const bannerDismissBtn = document.getElementById('banner-dismiss');

function resetRestartBtn() {
    restartGatewayBtn.disabled = false;
    restartGatewayBtn.classList.remove('confirming', 'needs-restart');
    restartGatewayBtn.querySelector('span').textContent = '重启网关';
    restartGatewayBtn.title = '';
    restartBanner.classList.add('hidden');
}

async function checkConfigStatus() {
    try {
        const resp = await fetch('/api/config-status');
        if (!resp.ok) return;
        const data = await resp.json();
        if (data.needs_restart) {
            restartGatewayBtn.classList.add('needs-restart');
            restartGatewayBtn.dataset.title = '配置已更新，重启后生效';
            restartBanner.classList.remove('hidden');
        } else {
            restartGatewayBtn.classList.remove('needs-restart');
            restartGatewayBtn.title = '';
            restartBanner.classList.add('hidden');
        }
    } catch (e) { /* silent */ }
}

restartGatewayBtn.addEventListener('click', () => {
    if (restartGatewayBtn.classList.contains('confirming')) {
        clearTimeout(restartConfirmTimer);
        restartGatewayBtn.classList.remove('confirming');
        restartGatewayBtn.classList.remove('needs-restart');
        restartGatewayBtn.querySelector('span').textContent = '正在重启...';
        restartGatewayBtn.disabled = true;
        fetch('/api/restart', { method: 'POST' }).catch(() => {});
    } else {
        restartGatewayBtn.classList.add('confirming');
        restartGatewayBtn.querySelector('span').textContent = '确认重启？';
        restartGatewayBtn.dataset.title = '确认重启？';
        restartConfirmTimer = setTimeout(() => {
            restartGatewayBtn.classList.remove('confirming');
            restartGatewayBtn.querySelector('span').textContent = '重启网关';
            restartGatewayBtn.dataset.title = '重启网关';
        }, 3000);
    }
});

// ── Sidebar collapse toggle ──
const sidebarToggle = document.getElementById('sidebar-toggle');
const sidebar = document.getElementById('sidebar');
// Move title to data-title on init to prevent native tooltips
sidebar.querySelectorAll('.sidebar-nav-item[title]').forEach(btn => {
    btn.dataset.title = btn.title;
    btn.title = '';
});
sidebarToggle.addEventListener('click', () => {
    sidebar.classList.toggle('collapsed');
});

// Event listeners
document.addEventListener('click', () => {
    document.querySelectorAll('.config-dropdown.open').forEach(d => d.classList.remove('open'));
});
document.querySelectorAll('.theme-btn').forEach(btn => {
    btn.addEventListener('click', () => applyTheme(btn.dataset.theme));
});
navChatBtn.addEventListener('click', () => showPage(chatView, navChatBtn));
navModelBtn.addEventListener('click', showModelPage);
document.getElementById('model-add-btn').addEventListener('click', () => {
    const pk = 'model_' + Date.now();
    const defaultProvider = 'openai';
    const defaultBaseUrl = cachedModelSchema?.providers?.[defaultProvider]?.default_base_url || '';
    const newModel = { primary_key: pk, id: '', show_name: '', provider: defaultProvider, api_key: '', base_url: defaultBaseUrl, context_window_tokens: 0, _isNew: true };
    cachedModels.push(newModel);
    if (cachedModels.length === 1) cachedActivePrimaryKey = pk;
    renderModelPage();
});
navChannelsBtn.addEventListener('click', showChannelsPage);
navSkillsBtn.addEventListener('click', showSkillsPage);
navCronBtn.addEventListener('click', showCronPage);
navHeartbeatBtn.addEventListener('click', showHeartbeatPage);
navSettingsBtn.addEventListener('click', showSettingsPage);

// ── Settings page ──
async function showSettingsPage() {
    showPage(settingsPage, navSettingsBtn);
    try {
        const resp = await fetch('/api/agent-settings');
        if (!resp.ok) return;
        const data = await resp.json();
        renderSettingsPage(data);
    } catch (e) { /* silent */ }
}

function renderSettingsPage(data) {
    settingsPageBody.innerHTML = '';

    const items = [
        { key: 'auto_greeting', label: '新会话自动打招呼', hint: '新建对话或重置时，AI 主动发起问候', icon: '👋' },
        { key: 'show_tool_calls', label: '展示工具消息', hint: '工具调用时显示 [正在执行 ...] 提示', icon: '🔧' },
    ];

    const grid = document.createElement('div');
    grid.className = 'settings-grid';

    for (const item of items) {
        const card = document.createElement('div');
        card.className = 'settings-card' + (data[item.key] ? ' active' : '');

        card.innerHTML = `
            <div class="settings-card__icon">${item.icon}</div>
            <div class="settings-card__body">
                <div class="settings-card__label">${item.label}</div>
                <div class="settings-card__hint">${item.hint}</div>
            </div>
            <label class="toggle-switch">
                <input type="checkbox" data-key="${item.key}" ${data[item.key] ? 'checked' : ''}>
                <span class="toggle-slider"></span>
            </label>
        `;

        const checkbox = card.querySelector('input');
        checkbox.addEventListener('change', () => {
            card.classList.toggle('active', checkbox.checked);
            saveSettings(card);
        });

        grid.appendChild(card);
    }

    settingsPageBody.appendChild(grid);
}

async function saveSettings(card) {
    const body = {};
    for (const cb of settingsPageBody.querySelectorAll('input[type="checkbox"]')) {
        body[cb.dataset.key] = cb.checked;
    }
    try {
        const resp = await fetch('/api/agent-settings', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (resp.ok) {
            // 短暂闪烁确认
            card.classList.add('saved');
            setTimeout(() => card.classList.remove('saved'), 600);
        }
    } catch (e) { /* silent */ }
}

// ── Path-based page routing ──
const pathPageMap = {
    channels: () => showChannelsPage(),
    model: () => showModelPage(),
    skills: () => showSkillsPage(),
    cron: () => showCronPage(),
    heartbeat: () => showHeartbeatPage(),
    settings: () => showSettingsPage(),
};

function navigateByPath() {
    const page = location.pathname.replace('/', '');
    if (page && pathPageMap[page]) {
        pathPageMap[page]();
    }
}

window.addEventListener('popstate', navigateByPath);
navigateByPath();

bannerDismissBtn.addEventListener('click', () => {
    restartBanner.classList.add('hidden');
});

bannerRestartBtn.addEventListener('click', () => {
    restartBanner.classList.add('hidden');
    restartGatewayBtn.classList.remove('needs-restart');
    restartGatewayBtn.querySelector('span').textContent = '正在重启...';
    restartGatewayBtn.disabled = true;
    fetch('/api/restart', { method: 'POST' }).catch(() => {});
});

// 页面加载时检查配置是否需要重启
checkConfigStatus();
