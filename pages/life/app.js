import { ready, get, post, onConnection, userMessage } from "./api.js";
import { loadReferences, clearReferences } from "./references.js";

const SEASONS = ["春", "夏", "秋", "冬"];
const CATEGORIES = ["外出", "居家", "睡衣", "运动"];
const OUTFIT_NAMES = { outdoor: "外出", home: "居家", sleepwear: "睡衣", sports: "运动" };
const OUTFIT_LABELS = { outdoor: "外出服", home: "居家服", sleepwear: "睡衣", sports: "运动服" };
const PROFILE_FIELDS = [
  ["identity", "身份、年龄段与城市"], ["living", "居住环境与同住的人"],
  ["routine_workday", "工作日 / 上课日作息"], ["routine_restday", "休息日作息"],
  ["weekly_fixed", "每周固定安排"], ["periodic", "周期性事项"],
  ["vacations", "寒暑假和节假日规则"], ["social_circle", "社交圈"],
  ["hobbies_places", "爱好与常去的地方"], ["phone_habits", "手机使用习惯"]
];
const NAVIGATION = [
  { id: "overview", label: "今日概况", icon: "sun" },
  { id: "life", label: "日程与生活", icon: "calendar", defaultView: "schedule" },
  { id: "chat", label: "私聊互动", icon: "chat" },
  { id: "photos", label: "照片", icon: "photo", defaultView: "references" },
  { id: "voice", label: "语音", icon: "voice" },
  { id: "qzone", label: "QQ 空间", icon: "qzone" },
  { id: "settings", label: "基础设置", icon: "settings", secondary: true }
];
const SECTIONS = {
  life: [["schedule", "今日日程"], ["wardrobe", "衣柜"], ["profile", "生活档案"]],
  photos: [["references", "参考图库"], ["photos", "最近成片"]],
};
const VIEWS = {
  overview: ["今日概况", "看看此刻正在发生什么。"],
  schedule: ["今日日程", "从清晨到夜晚，日子正在慢慢发生。"],
  wardrobe: ["我的衣柜", "为不同日子，留一套喜欢的搭配。"],
  profile: ["生活档案", "让熟悉的习惯，成为生活的底色。"],
  chat: ["私聊互动", "查看每条私聊的主动互动，以及记下的对方的事。"],
  photos: ["照片", "回看最近的成片，了解它们有没有发出去。"],
  references: ["照片", "查看、上传、替换和删除面部与身材参考图。"],
  voice: ["语音", "查看语音准备情况，选择日常说话的音色。"],
  qzone: ["QQ 空间", "查看插件记下的说说、今日次数和自动回评情况。"],
  settings: ["基础设置", "把生活的节奏，调到刚刚好。"],
};
const DASHBOARD_VIEWS = new Set(["overview", "chat", "photos", "voice", "qzone"]);
const DASHBOARD_STATUS = {
  overview: { disabled: "日程功能已关闭。", unavailable: "暂时没有可显示的日程片段。", unbound: "还没有可用的全局默认人设。" },
  chat: { disabled: "私聊互动已关闭。", unavailable: "暂时读不到私聊互动状态。", unbound: "还没有填写目标私聊。" },
  photos: { disabled: "照片功能已关闭。", unavailable: "暂时读不到最近成片。", unbound: "还没有可用的当前人设或会话。" },
  voice: { disabled: "语音功能已关闭。", unavailable: "暂时读不到语音配置。", unbound: "还没有可用的语音配置。" },
  qzone: { disabled: "QQ 空间功能已关闭。", unavailable: "暂时读不到 QQ 空间记录。", unbound: "还没有可用的 QQ 空间账号。" },
};
const PHONE_LABELS = { free: "能及时看手机", glance: "偶尔看手机", offline: "暂时不看手机" };
const CARE_STATUS = { pending: "等事后关心", asked: "已关心过", done: "对方已说了结果" };
const PHOTO_ACTIONS = { generate: "拍照", continue: "继续拍照", edit: "改图", resend: "重发照片" };
const PHOTO_DELIVERY = {
  queued: "排队中", generating: "生成中", sending: "发送中", sent: "已确认送达",
  partial: "部分送达", failed: "处理未完成", stopped: "已停止", unknown: "送达结果未知", interrupted: "处理已中断", not_sent: "未发送",
};
const QZONE_POST_STATUS = { published: "已发布", deleted: "已删除", remote_deleted: "QQ 空间报告已移除" };
const QZONE_SOURCE = { autonomous: "自动分享", manual: "手动发布", external: "来自 QQ 空间" };
const VOICE_AVAILABILITY = {
  not_configured: "请在插件设置中补齐语音配置，或从已加载的列表中选择音色。",
  no_options: "暂无已加载的音色。可以打开插件设置并保存语音配置，重新加载列表。",
  unverified: "当前音色还未在已加载的列表中确认。",
  cached: "当前音色在已加载的列表中，实际语音以发送结果为准。",
};
const PATHS = {
  hanger: ["M9 7a3 3 0 1 1 4 3v3l9 5a1 1 0 0 1-.5 2h-19A1 1 0 0 1 2 18l11-5"],
  calendar: ["M5 4h14a2 2 0 0 1 2 2v14H3V6a2 2 0 0 1 2-2Z", "M7 2v5m10-5v5M3 10h18m-13 4h2m4 0h2m-8 3h2"],
  person: ["M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0ZM4 21v-2a8 8 0 0 1 16 0v2"],
  photo: ["M3 5h18v14H3z", "m4 16 5-5 4 4 3-3 4 4", "M16 9h.01"],
  sun: ["M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z", "M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"],
  chat: ["M4 4h16v12H9l-5 4V4Z", "M8 8h8m-8 4h5"],
  voice: ["M9 5a3 3 0 0 1 6 0v7a3 3 0 0 1-6 0V5Z", "M6 11v1a6 6 0 0 0 12 0v-1m-6 7v4m-3 0h6"],
  qzone: ["m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9L12 3Z"],
  settings: ["M4 6h16M4 12h16M4 18h16M8 3v6m8 0v6m-8 0v6"],
  plus: ["M12 5v14M5 12h14"], refresh: ["M20 6v6h-6M4 18v-6h6", "M5 8a7.5 7.5 0 0 1 12-4l3 3M4 17l3 3a7.5 7.5 0 0 0 12-4"],
  edit: ["m15 4 5 5M4 16 16 4a2 2 0 0 1 4 4L8 20H4v-4Z"],
  trash: ["M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"],
  location: ["M19 10c0 5-7 11-7 11S5 15 5 10a7 7 0 1 1 14 0Z", "M14.5 10a2.5 2.5 0 1 1-5 0 2.5 2.5 0 0 1 5 0Z"],
  phone: ["M7 2h10v20H7V2Zm3 17h4"], mood: ["M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0ZM8 9h.01M16 9h.01M8 14c2 3 6 3 8 0"],
  weather: ["M7 18h12a4 4 0 0 0 .3-8 6 6 0 0 0-11.7-1A4.5 4.5 0 0 0 7 18Z"],
};
const $ = id => document.getElementById(id);
const content = $("view-content"), actions = $("view-actions"), dialog = $("outfit-dialog");
const confirmationDialog = $("confirmation-dialog");
const state = { view: "overview", epoch: 0, season: "全部", entries: [], scheduleData: null, collecting: new Set(), collectionVersion: 0, confirmedCollections: new Map(), dirty: { profile: false, settings: false, voice: false }, saving: { profile: false, settings: false, voice: false }, baseline: {}, profileTask: null, profileRequest: false, profileGenerating: false, pollToken: 0, pollWaiters: new Set(), pollDeadline: null, scheduleRetrying: false, closed: false, dialog: null, dialogEpoch: 0 };
Object.assign(state, { setup: null, setupError: "", setupPollToken: 0, setupPollTimer: null, roleRevision: 0, dashboardFingerprint: null, photoScope: "" });
const SETUP_STATUSES = ["waiting_settings", "preparing", "ready", "failed", "unavailable"];
const SETUP_STAGES = { backup: "备份旧资料", profile: "生成生活档案", schedule: "生成今日日程与穿搭", ready: "准备完成" };
let toastTimer;
let confirmation = null;
let navigationPending = false;

function element(tag, className = "", text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}
function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  for (const [name, value] of Object.entries({ viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "1.5", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" })) svg.setAttribute(name, value);
  for (const pathData of PATHS[name] || []) { const path = document.createElementNS("http://www.w3.org/2000/svg", "path"); path.setAttribute("d", pathData); svg.append(path); }
  return svg;
}
function button(label, className = "button secondary", action, iconName) {
  const node = element("button", className); node.type = "button";
  if (iconName) node.append(icon(iconName));
  node.append(element("span", "", label));
  if (action) node.addEventListener("click", action);
  return node;
}
function showToast(message, error = false) {
  clearTimeout(toastTimer); const node = $("toast");
  node.textContent = message; node.className = "toast" + (error ? " error" : ""); node.hidden = false;
  toastTimer = setTimeout(() => { node.hidden = true; }, error ? 7000 : 4000);
}
function confirmAction(title, message, label) {
  if (confirmationDialog.open) return Promise.resolve(false);
  $("confirmation-title").textContent = title;
  $("confirmation-message").textContent = message;
  $("confirmation-accept").textContent = label;
  return new Promise(resolve => {
    confirmation = resolve;
    confirmationDialog.showModal();
  });
}
function finishConfirmation(accepted) {
  const resolve = confirmation;
  confirmation = null;
  if (confirmationDialog.open) confirmationDialog.close();
  resolve?.(accepted);
}
function statePanel(title, message, error = false, retry) {
  const panel = element("div", "state-panel" + (error ? " error" : ""));
  panel.append(icon(error ? "refresh" : "hanger"), element("h2", "", title), element("p", "", message));
  if (retry) panel.append(button("重新读取", "button secondary", retry, "refresh"));
  return panel;
}
function loading() { content.replaceChildren(statePanel("正在读取", "稍等一下，正在打开这一页。")); }
function sameView(view, epoch) { return !state.closed && state.view === view && state.epoch === epoch; }
function stopSetupPolling() {
  ++state.setupPollToken;
  clearTimeout(state.setupPollTimer); state.setupPollTimer = null;
}
function acceptSetup(data) {
  if (!data || !SETUP_STATUSES.includes(data.status) || typeof data.selected_persona_id !== "string" || typeof data.selected_persona_name !== "string" || typeof data.active_persona_name !== "string") throw new Error("角色准备状态格式有误，请刷新重试。");
  if (state.setup && (state.setup.selected_persona_id !== data.selected_persona_id || state.setup.status === "ready" && data.status !== "ready")) {
    ++state.roleRevision; state.confirmedCollections.clear(); state.collecting.clear(); state.profileTask = null;
    state.entries = []; state.scheduleData = null; state.season = "全部";
  }
  state.setup = data; state.setupError = "";
  return data;
}
function setupTitle(data) {
  return { waiting_settings: "等待保存插件设置", preparing: "正在准备角色生活", ready: "角色生活已就绪", failed: "角色生活准备失败", unavailable: "全局默认人设暂不可用" }[data.status];
}
function setupCopy(data) {
  return data.message || { waiting_settings: "请打开 AstrBot 插件管理中「拟人生活」的设置，填写日程分区后点击“保存并关闭”，后台会自动准备。", preparing: "正在跟随 AstrBot 全局默认人设准备生活档案、日程和穿搭，完成后会自动显示。", ready: "生活档案和日程已准备好，可以正常使用。", failed: "请检查失败原因，在 AstrBot 插件管理中「拟人生活」的设置里修改日程分区后，点击“保存并关闭”继续准备；已完成的步骤会保留。", unavailable: "请在 AstrBot 人设管理中设置有效的全局默认人设，再打开「拟人生活」的设置，点击“保存并关闭”。" }[data.status];
}
function setupControls() {
  const group = element("div", "setup-controls");
  if (state.setupError) group.append(button("重新读取状态", "button secondary", () => refreshSetup(), "refresh"));
  return group;
}
function renderSetupBanner() {
  const target = $("setup-status"), data = state.setup;
  target.hidden = false; target.replaceChildren();
  target.className = "setup-status" + (state.setupError || data?.status === "failed" ? " setup-error" : data?.status === "ready" ? " setup-ready" : "");
  if (state.setupError) {
    target.dataset.status = "read_error";
    target.append(element("div", "setup-copy"), setupControls());
    target.firstChild.append(element("strong", "", "暂时没能确认当前角色"), element("p", "", state.setupError)); return;
  }
  if (!data) { target.hidden = true; return; }
  target.dataset.status = data.status;
  const copy = element("div", "setup-copy");
  const name = data.status === "ready" ? data.active_persona_name : data.selected_persona_name;
  copy.append(element("strong", "setup-role", name ? "跟随全局默认人设：" + name : "尚未读到可用的全局默认人设"));
  copy.append(element("p", "", setupTitle(data) + "。" + setupCopy(data)));
  if (data.stage && data.status !== "ready") copy.append(element("span", "setup-stage", (data.status === "failed" ? "停在：" : "当前步骤：") + (SETUP_STAGES[data.stage] || data.stage)));
  if (data.error) copy.append(element("p", "setup-failure", "原因：" + userMessage(data.error, "这一步未完成，请在 AstrBot 的「拟人生活」设置中修改后保存。")));
  if (data.persona_changed && data.status === "ready") copy.append(element("p", "setup-notice", "后续生成采用最新人设，已有资料保留。"));
  if (state.view === "settings" && state.dirty.settings) copy.append(element("p", "setup-notice", "下面的设置尚未保存。"));
  target.append(copy, setupControls());
}
function renderSetupGate() {
  state.entries = []; state.scheduleData = null; actions.replaceChildren();
  const data = state.setup;
  if (!data || state.setupError) { content.replaceChildren(statePanel("暂时没能确认角色状态", state.setupError || "请稍后重试。", true, () => refreshSetup())); return; }
  const panel = statePanel(setupTitle(data), data.status === "preparing" ? "正在按顺序准备生活档案、日程和穿搭；完成后会自动显示，每 5 秒更新一次。" : data.status === "ready" ? "正在读取当前角色生活。" : "完成上方提示的设置后，这里会自动显示衣柜、今日日程和生活档案。基础设置仍可查看和编辑。", data.status === "failed");
  panel.id = "setup-gate"; content.replaceChildren(panel);
}
function setupRefreshBlocked() {
  return (hasDirty() && !(state.view === "settings" && state.setup?.status === "preparing")) || state.saving[state.view] || dialog.open || confirmationDialog.open;
}
async function refreshSetup(pollToken = null) {
  if (state.closed) return;
  const epoch = state.epoch, wasReady = state.setup?.status === "ready" && !state.setupError, roleRevision = state.roleRevision;
  const current = () => !state.closed && epoch === state.epoch && (pollToken === null || pollToken === state.setupPollToken);
  try {
    const data = await get("setup/status"); if (!current()) return;
    if (pollToken !== null && setupRefreshBlocked()) { watchSetup(); return; }
    acceptSetup(data);
  }
  catch (error) { if (!current()) return; if (pollToken !== null && setupRefreshBlocked()) { watchSetup(); return; } state.setupError = error.message; }
  renderSetupBanner();
  if (state.view !== "settings") {
    if (state.setup?.status === "ready" && !state.setupError) {
      if (!wasReady || roleRevision !== state.roleRevision) { await loadView(state.view); return; }
    } else { ++state.epoch; stopPolling(); renderSetupGate(); }
  }
  watchSetup();
}
function watchSetup() {
  stopSetupPolling();
  if (state.closed || state.view === "schedule" && state.setup?.status === "ready" && !state.setupError && $("schedule-timeline")) return;
  const token = state.setupPollToken, epoch = state.epoch;
  state.setupPollTimer = setTimeout(async () => {
    state.setupPollTimer = null;
    if (state.closed || token !== state.setupPollToken || epoch !== state.epoch) return;
    if (setupRefreshBlocked()) { watchSetup(); return; }
    await refreshSetup(token);
  }, state.setup?.status === "preparing" && !state.setupError ? 5000 : 60000);
}
function stopPolling() {
  ++state.pollToken;
  if (state.pollDeadline !== null) { clearTimeout(state.pollDeadline); state.pollDeadline = null; }
  for (const waiter of state.pollWaiters) { clearTimeout(waiter.timer); waiter.resolve(false); }
  state.pollWaiters.clear();
}
function pollWait(milliseconds, token) {
  if (state.closed || token !== state.pollToken) return Promise.resolve(false);
  return new Promise(resolve => {
    const waiter = { resolve, timer: null };
    waiter.timer = setTimeout(() => { state.pollWaiters.delete(waiter); resolve(token === state.pollToken && !state.closed); }, milliseconds);
    state.pollWaiters.add(waiter);
  });
}
function hasDirty() { return Boolean(state.dirty[state.view]); }
function mayDiscard() {
  return !hasDirty() || confirmAction("放弃未保存的修改？", "这一页还有未保存的修改，确认放弃并继续吗？", "放弃修改");
}
async function refreshView() {
  const { view, epoch } = state;
  if (state.saving[view]) { showToast("正在保存，请稍后刷新。"); return; }
  if (view === "schedule" && state.scheduleRetrying) { showToast("日程正在准备，稍等一下就会自动更新。"); return; }
  if (!await mayDiscard() || !sameView(view, epoch)) return;
  state.dirty[view] = false;
  return loadView(view);
}

function buildNavigation() {
  const nav = $("navigation");
  for (const item of NAVIGATION) {
    const node = button(item.label, "nav-button" + (item.secondary ? " nav-secondary" : ""), () => {
      if (sectionFor(state.view) !== item.id || item.id === "photos") navigate(item.defaultView || item.id);
    });
    node.replaceChildren(icon(item.icon), element("span", "nav-label", item.label));
    node.dataset.view = item.id; node.title = item.label; node.setAttribute("aria-label", item.label);
    nav.append(node);
  }
  for (const [section, views] of Object.entries(SECTIONS)) {
    for (const [view, label] of views) {
      const node = button(label, "section-nav-button", () => navigate(view));
      node.dataset.view = view;
      $(section + "-navigation").append(node);
    }
  }
}
function sectionFor(view) {
  return Object.entries(SECTIONS).find(([, views]) => views.some(([id]) => id === view))?.[0] || view;
}
function viewFromLocation() {
  const location = new URL(window.location.href);
  const route = location.hash.slice(1) || location.searchParams.get("view");
  if (route === "life") return "schedule";
  if (route === "photo") return "photos";
  return Object.hasOwn(VIEWS, route) ? route : "overview";
}
function writeViewLocation(view) {
  if (!window.history) return;
  const location = new URL(window.location.href);
  location.hash = view;
  window.history.pushState(null, "", location);
}
function updateNavigation() {
  const section = sectionFor(state.view);
  for (const nav of [$("navigation"), $("life-navigation"), $("photos-navigation")]) {
    for (const node of nav.children) {
      const active = node.dataset.view === (nav.id === "navigation" ? section : state.view);
      node.classList.toggle("active", active);
      if (active) node.setAttribute("aria-current", "page"); else node.removeAttribute("aria-current");
    }
  }
  $("life-navigation").hidden = section !== "life";
  $("photos-navigation").hidden = section !== "photos";
}
async function navigate(view, fromHistory = false) {
  if (view === state.view || navigationPending) return;
  const previous = state.view, epoch = state.epoch;
  const saving = state.saving[previous] || state.dialog?.saving;
  navigationPending = true;
  const accepted = !saving && !confirmationDialog.open && await mayDiscard() && (!dialog.open || await closeOutfit());
  navigationPending = false;
  if (!sameView(previous, epoch)) return;
  if (!accepted) {
    if (saving) showToast("正在保存，请稍后再切换。");
    if (fromHistory) writeViewLocation(state.view);
    return;
  }
  state.dirty[state.view] = false;
  if (!fromHistory || viewFromLocation() !== view) writeViewLocation(view);
  await loadView(view);
  if (state.view === view) window.scrollTo?.({ top: 0, behavior: "auto" });
}
async function loadView(view) {
  const epoch = ++state.epoch; stopPolling(); stopSetupPolling();
  state.dashboardFingerprint = null;
  state.view = view; updateNavigation();
  const [title, subtitle] = VIEWS[view];
  $("view-title").textContent = title; $("view-subtitle").textContent = subtitle;
  actions.replaceChildren();
  const showReferences = view === "references";
  content.hidden = showReferences;
  $("photo-references").hidden = !showReferences;
  $("setup-status").hidden = true;
  if (showReferences) {
    const refresh = button("刷新", "button secondary", refreshView, "refresh");
    content.replaceChildren();
    actions.append(refresh);
    await loadReferences(refresh);
    return;
  }
  clearReferences();
  loading();
  if (DASHBOARD_VIEWS.has(view)) {
    actions.append(button("刷新", "button secondary", refreshView, "refresh"));
    await loadDashboard(view, epoch);
    return;
  }
  try {
    await ready();
    try {
      const setup = await get("setup/status");
      if (!sameView(view, epoch)) return;
      acceptSetup(setup);
    } catch (error) {
      if (!sameView(view, epoch)) return;
      state.setupError = error.message;
    }
    renderSetupBanner();
    if (view !== "settings" && (state.setupError || state.setup.status !== "ready")) { renderSetupGate(); watchSetup(); return; }
    const endpoint = { wardrobe: "wardrobe/list", schedule: "schedule/today", profile: "profile/get", settings: "settings/get" }[view];
    const data = view === "schedule" ? await getSchedule() : await get(endpoint);
    if (!sameView(view, epoch) || state.closed) return;
    if (view === "wardrobe") {
      if (!Array.isArray(data.entries)) throw new Error("衣柜列表格式有误，请刷新重试。");
      state.entries = data.entries.map((entry, index) => ({ ...entry, number: Number.isInteger(entry.number) && entry.number > 0 ? entry.number : index + 1 }));
      renderWardrobe();
    } else if (view === "schedule") renderSchedule(data);
    else if (view === "profile") renderProfile(data);
    else renderSettings(data);
    watchSetup();
  } catch (error) {
    if (sameView(view, epoch)) { content.replaceChildren(statePanel("这一页暂时没有打开", error.message, true, refreshView)); if (state.setup) watchSetup(); }
  }
}

function dashboardPanel(title, iconName) {
  const panel = element("section", "dashboard-panel");
  const heading = element("h2", "dashboard-heading");
  heading.append(icon(iconName), element("span", "", title));
  panel.append(heading);
  return panel;
}
function dashboardNote(text) { return element("p", "dashboard-note", text); }
function dashboardFacts(rows) {
  const list = element("dl", "dashboard-facts");
  for (const [label, value] of rows) {
    const pair = element("div", "dashboard-fact");
    pair.append(element("dt", "", label), element("dd", "", value ?? "待确认"));
    list.append(pair);
  }
  return list;
}
function dashboardCount(value, unit = "次") {
  return Number.isInteger(value) && value >= 0 ? value + " " + unit : "待确认";
}
function dashboardTime(value) {
  return typeof value === "string" && /^\d{4}-\d\d-\d\dT\d\d:\d\d/.test(value) ? value.slice(0, 16).replace("T", " ") : "时间待确认";
}
function dashboardStatus(data, view) {
  return DASHBOARD_STATUS[view][data.status];
}
function renderOverview(data) {
  const current = dashboardPanel("此刻的生活", "sun");
  current.classList.add("dashboard-hero");
  const persona = element("p", "dashboard-persona", data.persona_name || "当前人设待确认");
  const details = dashboardFacts([
    ["正在做", data.activity || "暂无当前活动"],
    ["在哪里", data.location],
    ["心情", data.mood],
    ["手机状态", PHONE_LABELS[data.phone]],
    ["穿搭", data.outfit],
  ]);
  current.append(persona);
  const status = dashboardStatus(data, "overview");
  if (status) current.append(dashboardNote(status));
  current.append(details, button("查看今日日程", "button secondary dashboard-link", () => navigate("schedule"), "calendar"));
  content.replaceChildren(current);
}
function renderChat(data) {
  const panels = [];
  const status = dashboardStatus(data, "chat");
  const summary = dashboardPanel("主动互动", "sun");
  summary.append(dashboardFacts([
    ["手机状态", PHONE_LABELS[data.phone]],
    ["主动消息", data.proactive_enabled ? "已开启" : "已关闭"],
    ["每天最多主动联系", dashboardCount(data.daily_limit)],
    ["连续未回复上限", data.max_unanswered === 0 ? "不限制" : dashboardCount(data.max_unanswered)],
  ]));
  if (data.message || status) summary.append(dashboardNote(data.message || status));
  if (data.memory_notice) summary.append(dashboardNote(data.memory_notice));
  panels.push(summary);
  for (const target of data.targets || []) panels.push(renderChatTarget(target, data.can_write));
  content.replaceChildren(...panels);
}
function renderChatTarget(target, canWrite) {
  const panel = dashboardPanel(target.target, "chat");
  panel.append(dashboardFacts([
    ["今天已主动联系", dashboardCount(target.rounds_today)],
    ["今天主动发的照片", dashboardCount(target.photos_today, "张")],
    ["连续未回复", dashboardCount(target.unanswered, "条")],
    ["下一次想起对方", target.next_due ? dashboardTime(target.next_due) : "未安排"],
    ["上一次主动联系", target.last_proactive ? dashboardTime(target.last_proactive) : "还没有"],
    ["主动互动状态", target.reason],
  ]));
  const events = target.events || [];
  panel.append(element("h3", "dashboard-subheading", "对方提到的事"));
  if (!events.length) panel.append(dashboardNote("还没有记下对方提到的有时间的事。"));
  for (const item of events) {
    const row = element("div", "dashboard-fact");
    row.append(element("dt", "", (item.time ? dashboardTime(item.time) + " " : "") + item.content));
    const detail = element("dd", "", CARE_STATUS[item.status] || item.status);
    if (canWrite) detail.append(" ", button("删除", "button secondary", () => deleteCareEvent(target.target, item), "trash"));
    row.append(detail);
    panel.append(row);
  }
  if (canWrite && target.bound) {
    panel.append(button("重置机器人账号绑定", "button secondary", () => resetChatBinding(target.target), "refresh"));
  }
  if (!canWrite) panel.append(dashboardNote("修改需要面板管理员权限。"));
  return panel;
}
async function deleteCareEvent(target, item) {
  if (!await confirmAction("删除这条记录", "删除后角色不会再就这件事关心对方。", "删除")) return;
  try {
    await post("dashboard/chat/event/delete", { target, id: item.id });
    showToast("已删除。");
  } catch (error) {
    showToast(error.message, true);
  }
  await loadView("chat");
}
async function resetChatBinding(target) {
  if (!await confirmAction("重置机器人账号绑定", "重置后，下一条普通消息所在的机器人账号会被重新记下。", "重置")) return;
  try {
    await post("dashboard/chat/binding/reset", { target });
    showToast("已重置，请在这条私聊里发一句普通消息。");
  } catch (error) {
    showToast(error.message, true);
  }
  await loadView("chat");
}
function renderPhotoCard(item, scope, epoch) {
  const label = item.action === "generate" && item.mode === "text" ? "文字生图" : item.mode === "edit" && item.action !== "resend" ? "改图" : PHOTO_ACTIONS[item.action] || "照片";
  const card = element("article", "history-card");
  const media = element("div", "history-media");
  const placeholder = element("span", "", item.image_id ? "正在读取成片…" : item.generation_status === "pending" ? "成片还在准备" : "暂无成片预览");
  media.append(placeholder);
  const body = element("div", "history-body");
  const badge = element("span", "dashboard-badge", PHOTO_DELIVERY[item.delivery_status] || "送达状态待确认");
  badge.dataset.status = item.delivery_status || "";
  const time = item.created_at === null ? "旧记录未保存时间" : dashboardTime(item.created_at);
  body.append(element("h3", "", label), element("p", "history-time", time), badge);
  card.append(media, body);
  if (item.image_id) {
    const image = element("img");
    image.alt = label + "成片预览"; image.hidden = true;
    image.onload = () => { image.hidden = false; placeholder.hidden = true; };
    const failed = () => { placeholder.textContent = "暂时无法预览成片"; };
    image.onerror = failed;
    get("dashboard/photos/image", { id: item.image_id, scope }).then(data => {
      if (!sameView("photos", epoch) || !card.isConnected) return;
      if (safeImage(data.data_url)) image.src = data.data_url; else failed();
    }, failed);
    media.append(image);
  }
  return card;
}
function renderPhotos(data, epoch) {
  const panel = dashboardPanel("最近成片", "photo");
  state.photoScope = data.scope_id || "";
  if (data.scopes.length > 0) {
    const scope = element("select", "text-input");
    for (const item of data.scopes) {
      const option = element("option", "", item.name); option.value = item.id; scope.append(option);
    }
    scope.value = state.photoScope;
    scope.addEventListener("change", async () => {
      state.photoScope = scope.value;
      await loadView("photos");
      $("photo-history-scope")?.focus({ preventScroll: true });
    });
    panel.append(field("查看会话", "photo-history-scope", scope, "这里只显示该会话中能确认属于当前人设的照片。"));
  }
  const status = dashboardStatus(data, "photos");
  if (status) panel.append(dashboardNote(status));
  if (!data.items.length) {
    if (data.status === "ready") panel.append(dashboardNote("这个会话还没有可显示的成片记录。"));
  } else {
    const grid = element("div", "history-grid");
    grid.append(...data.items.map(item => renderPhotoCard(item, state.photoScope, epoch)));
    panel.append(grid);
  }
  content.replaceChildren(panel);
}
function renderVoice(data) {
  const panel = dashboardPanel("当前语音", "voice");
  const status = dashboardStatus(data, "voice");
  if (status) panel.append(dashboardNote(status));
  panel.append(dashboardFacts([
    ["当前音色", data.voice_name || data.voice_id || "还没有选择音色"],
    ["语音服务", data.status === "unavailable" ? "待确认" : data.configured ? "已填写本地配置" : "配置还未准备好"],
  ]));
  const availability = VOICE_AVAILABILITY[data.availability];
  if (availability) panel.append(dashboardNote(availability));
  if (data.can_write && data.options.length > 0) {
    const form = element("form", "dashboard-form");
    const select = element("select", "text-input");
    const currentInList = data.options.some(item => item.id === data.voice_id);
    if (!currentInList) {
      const placeholder = element("option", "", "请选择音色");
      placeholder.value = ""; placeholder.disabled = true; select.append(placeholder);
    }
    for (const item of data.options) {
      const option = element("option", "", item.name); option.value = item.id; select.append(option);
    }
    select.value = currentInList ? data.voice_id : "";
    const save = button("保存音色", "button primary");
    save.id = "voice-save"; save.type = "submit"; save.disabled = true;
    const feedback = element("p", "dashboard-note"); feedback.setAttribute("role", "status");
    select.addEventListener("change", () => {
      state.dirty.voice = Boolean(select.value) && select.value !== data.voice_id;
      save.disabled = !state.dirty.voice;
      feedback.textContent = state.dirty.voice ? "音色修改还未保存。" : "";
    });
    form.addEventListener("submit", event => {
      event.preventDefault();
      saveVoice(select, save, feedback);
    });
    form.append(field("选择音色", "voice-choice", select), save, feedback);
    panel.append(form);
  } else if (data.is_admin === false && data.status !== "unavailable") {
    panel.append(dashboardNote("当前账号可查看音色，修改需要面板管理员权限。"));
  } else if (data.is_admin && data.options.length === 1 && data.options[0].id === data.voice_id) {
    panel.append(dashboardNote("当前列表里只有正在使用的音色。"));
  }
  content.replaceChildren(panel);
}
async function saveVoice(select, save, feedback) {
  if (!state.dirty.voice || state.saving.voice) return;
  const epoch = state.epoch, voiceId = select.value;
  state.saving.voice = true;
  select.disabled = save.disabled = true;
  feedback.textContent = "正在保存音色…";
  try {
    const data = await post("dashboard/voice/save", { voice_id: voiceId });
    if (!sameView("voice", epoch)) return;
    if (data.voice_id !== voiceId) throw new Error("保存后的音色无法确认");
    state.dirty.voice = false;
    renderVoice(data);
    showToast("音色已保存。");
  } catch {
    if (sameView("voice", epoch)) feedback.textContent = "暂时没能保存音色。可以刷新音色列表后重试。";
  } finally {
    state.saving.voice = false;
    select.disabled = false; save.disabled = !state.dirty.voice;
  }
}
function renderQzone(data) {
  const totals = dashboardPanel("今天的 QQ 空间", "qzone");
  const status = dashboardStatus(data, "qzone");
  if (status) totals.append(dashboardNote(status));
  totals.append(dashboardFacts([
    ["今日已发布", dashboardCount(data.published_today)],
    ["今日自动分享（QQ 账号）", dashboardCount(data.autonomous_today)],
    ["自动分享每日上限", dashboardCount(data.daily_limit)],
  ]));
  const replies = dashboardPanel("自动回评", "chat");
  replies.append(dashboardFacts([
    ["回评状态", data.status === "disabled" || data.replies_enabled === false ? "已关闭" : data.status !== "ready" ? "待确认" : data.paused === true ? "已暂停" : data.replies_enabled === true ? "已开启" : "待确认"],
    ["今日已回评", dashboardCount(data.replies_today, "条")],
    ["待回评", dashboardCount(data.reply_pending, "条")],
    ["结果未知", dashboardCount(data.reply_unknown, "条")],
  ]));
  const posts = dashboardPanel("本地说说记录", "qzone");
  posts.append(dashboardNote("显示插件已经记录的说说。"));
  if (data.posts.length === 0) {
    posts.append(dashboardNote(data.status === "unavailable" ? "暂时读不到说说记录。" : "目前没有本地说说记录。"));
  } else {
    const list = element("ul", "dashboard-list");
    for (const item of data.posts) {
      const row = element("li", "dashboard-list-item");
      const details = [dashboardTime(item.created_at), QZONE_SOURCE[item.source], QZONE_POST_STATUS[item.status] || "发布状态待确认"].filter(Boolean);
      row.append(element("p", "history-caption", item.caption || "这条说说没有文字"), element("p", "history-time", details.join(" · ")));
      list.append(row);
    }
    posts.append(list);
  }
  content.replaceChildren(totals, replies, posts);
}
const DASHBOARD_RENDERERS = { overview: renderOverview, chat: renderChat, photos: renderPhotos, voice: renderVoice, qzone: renderQzone };
async function loadDashboard(view, epoch) {
  const params = view === "photos" && state.photoScope ? { scope: state.photoScope } : undefined;
  const canRender = () => sameView(view, epoch) && !state.dirty[view] && !state.saving[view];
  try {
    if (!canRender()) return;
    const data = await get("dashboard/" + view, params);
    if (!canRender()) return;
    if (data.status !== "ready" && !dashboardStatus(data, view)) throw new Error("面板状态无法确认");
    const fingerprint = JSON.stringify(data);
    if (fingerprint !== state.dashboardFingerprint) {
      DASHBOARD_RENDERERS[view](data, epoch);
      state.dashboardFingerprint = fingerprint;
    }
  } catch {
    if (!canRender()) return;
    state.dashboardFingerprint = null;
    content.replaceChildren(statePanel("暂时没能读到这一页", "请确认 AstrBot 已连接，再刷新查看。", true, refreshView));
  } finally {
    if (sameView(view, epoch) && view !== "voice") {
      state.setupPollTimer = setTimeout(() => loadDashboard(view, epoch), 60000);
    }
  }
}

function safeImage(reference) { return typeof reference === "string" && /^data:image\/(?:jpeg|png|webp);base64,[a-zA-Z0-9+/=\s]+$/.test(reference); }
function imagePlaceholder() {
  const node = element("div", "image-placeholder"); node.append(icon("hanger"), element("span", "", "暂无展示图")); return node;
}
function descriptionBlock(description) {
  const text = typeof description === "string" && description.trim() ? description : "暂无穿搭描述";
  if (text.length <= 40) return element("p", "description", text);
  const details = element("details", "description-details");
  const summary = element("summary", "description-summary");
  const toggle = element("span", "details-action", "展开");
  summary.append(element("span", "description-preview", text), toggle);
  details.addEventListener("toggle", () => { toggle.textContent = details.open ? "收起" : "展开"; });
  details.append(summary, element("p", "description", text)); return details;
}
function outfitCard(entry) {
  const name = "穿搭" + entry.number;
  const card = element("article", "outfit-card"); card.dataset.entryId = entry.id;
  const media = element("div", "card-media"); const empty = imagePlaceholder(); media.append(empty);
  if (safeImage(entry.image)) {
    const image = element("img"); image.alt = name + "的展示图"; image.loading = "lazy"; image.decoding = "async";
    image.addEventListener("error", () => { image.hidden = true; empty.hidden = false; });
    image.src = entry.image; empty.hidden = true; media.append(image);
  }
  const body = element("div", "card-body"); const heading = element("div", "card-heading");
  const title = element("h2", "outfit-name"); const dot = element("span", "number-dot"); dot.setAttribute("aria-hidden", "true");
  title.append(dot, element("span", "", name)); heading.append(title, element("span", "category", CATEGORIES.includes(entry.category) ? entry.category : "外出"));
  const tags = element("div", "card-tags"), seasons = element("div", "seasons");
  for (const season of SEASONS.filter(season => Array.isArray(entry.seasons) && entry.seasons.includes(season))) seasons.append(element("span", "season", season));
  const controls = element("div", "card-actions");
  for (const [action, label, iconName, handler] of [["edit", "编辑" + name, "edit", () => openOutfit(entry)], ["delete", "删除" + name, "trash", () => deleteOutfit(entry)]]) {
    const control = button("", "icon-button " + action, handler); control.replaceChildren(icon(iconName));
    control.dataset.action = action; control.title = label; control.setAttribute("aria-label", label); controls.append(control);
  }
  tags.append(seasons, controls); body.append(heading, tags, descriptionBlock(entry.description));
  if (entry.image_status && ["processing", "failed"].includes(entry.image_status.status)) body.append(element("p", "image-status " + entry.image_status.status, entry.image_status.status === "processing" ? "正在提取穿搭，稍后刷新看看…" : "图片识别失败，可编辑并重新上传"));
  card.append(media, body); return card;
}
function renderWardrobe() {
  actions.replaceChildren(button("添加穿搭", "button primary", () => openOutfit(), "plus"));
  const toolbar = element("div", "toolbar"), filters = element("div", "filter-list");
  filters.setAttribute("aria-label", "按季节筛选");
  for (const season of ["全部", ...SEASONS]) {
    const filter = button(season, "filter-button" + (state.season === season ? " active" : ""), () => { state.season = season; renderWardrobe(); });
    filter.dataset.season = season; filter.setAttribute("aria-pressed", String(state.season === season)); filters.append(filter);
  }
  const entries = state.entries.filter(entry => state.season === "全部" || (Array.isArray(entry.seasons) && entry.seasons.includes(state.season)));
  const count = element("span", "count", "共 " + entries.length + " 套穿搭"); count.id = "wardrobe-count";
  toolbar.append(filters, count);
  const grid = element("div", "wardrobe-grid"); grid.id = "wardrobe-grid";
  if (entries.length) grid.append(...entries.map(outfitCard));
  const empty = statePanel(state.entries.length ? "这个季节还没有穿搭" : "衣柜还是空的", state.entries.length ? "换一个季节看看，或添加一套新的搭配。" : "衣柜还是空的。没有衣服时，角色每天会由 AI 临时搭配；在『今日日程』里看到喜欢的，可以一键收进衣柜。也可以点右上角自己添加。");
  const note = element("p", "wardrobe-note", "衣柜里的穿搭会保存下来，角色会按季节和天气挑选合适的搭配。");
  content.replaceChildren(toolbar, entries.length ? grid : empty, note);
}
async function deleteOutfit(entry) {
  const epoch = state.epoch;
  if (!await confirmAction("删除穿搭？", "确认删除这套穿搭？", "删除穿搭") || !sameView("wardrobe", epoch)) return;
  const card = Array.from($("wardrobe-grid")?.children || []).find(card => card.dataset.entryId === entry.id);
  if (card?.dataset.busy === "true") return;
  if (card) { card.dataset.busy = "true"; card.querySelectorAll("button").forEach(button => { button.disabled = true; }); }
  try {
    await post("wardrobe/delete", { id: entry.id });
    if (sameView("wardrobe", epoch)) await loadView("wardrobe");
    showToast("这套穿搭已删除。");
  } catch (error) {
    showToast(error.message, true);
    if (card?.isConnected) { card.dataset.busy = "false"; card.querySelectorAll("button").forEach(button => { button.disabled = false; }); }
  }
}

function openOutfit(entry = null) {
  const session = { token: ++state.dialogEpoch, id: entry?.id || "", dirty: false, saving: false, recognizing: false, fileToken: 0, image: "", revisions: { description: 0, category: 0, seasons: 0 } };
  state.dialog = session;
  $("outfit-dialog-title").textContent = entry ? "编辑穿搭" : "添加穿搭";
  $("outfit-description").value = entry?.description || ""; $("outfit-category").value = CATEGORIES.includes(entry?.category) ? entry.category : "外出";
  for (const input of $("outfit-seasons").querySelectorAll("input")) input.checked = entry ? Array.isArray(entry.seasons) && entry.seasons.includes(input.value) : true;
  $("outfit-image-input").value = ""; $("outfit-error").hidden = true;
  $("outfit-upload-status").textContent = "只识别服装与造型，人物外貌和背景不会写入描述。";
  setPreview(safeImage(entry?.image) ? entry.image : "");
  updateOutfitButtons(); dialog.showModal();
}
function setPreview(reference) {
  const preview = $("outfit-preview"); preview.hidden = !reference;
  if (reference) preview.src = reference; else preview.removeAttribute("src");
  $("outfit-upload").classList.toggle("has-image", Boolean(reference));
}
function updateOutfitButtons() {
  const session = state.dialog; const pending = session?.saving || session?.recognizing;
  $("outfit-save").disabled = Boolean(pending);
  $("outfit-save").textContent = session?.saving ? "正在保存…" : session?.recognizing ? "正在识别…" : "保存穿搭";
  $("outfit-cancel").disabled = Boolean(session?.saving); $("outfit-close").disabled = Boolean(session?.saving);
  $("outfit-image-input").disabled = Boolean(session?.saving);
}
async function closeOutfit() {
  const session = state.dialog;
  if (session?.saving) return false;
  if (session?.dirty && !await confirmAction("放弃穿搭修改？", "穿搭还有未保存的修改，确认取消吗？", "放弃修改")) return false;
  if (state.dialog !== session || state.closed) return false;
  cancelOutfitWait(session); state.dialog = null; ++state.dialogEpoch; dialog.close();
  return true;
}
function cancelOutfitWait(session) {
  if (session?.imageWaiter) { clearTimeout(session.imageWaiter.timer); session.imageWaiter.resolve(false); session.imageWaiter = null; }
}
function imageTaskCurrent(session, ticket) { return state.dialog === session && session.fileToken === ticket && !state.closed && state.view === "wardrobe"; }
function waitForImagePoll(session, ticket) {
  if (!imageTaskCurrent(session, ticket)) return Promise.resolve(false);
  return new Promise(resolve => {
    const waiter = { resolve, timer: null };
    waiter.timer = setTimeout(() => { if (session.imageWaiter === waiter) session.imageWaiter = null; resolve(imageTaskCurrent(session, ticket)); }, 2000);
    session.imageWaiter = waiter;
  });
}
async function imageTaskResult(accepted, session, ticket) {
  // Keep compatibility with earlier servers that returned a small result
  // directly. New servers acknowledge a managed task before model queuing.
  if (!accepted.task_id) return accepted;
  if (accepted.type && accepted.type !== "wardrobe_transcribe") throw new Error("这次没有读到正确的图片识别结果，请重新上传。");
  session.imageTask = accepted.task_id;
  let task = accepted;
  while (imageTaskCurrent(session, ticket)) {
    if (task.type && task.type !== "wardrobe_transcribe") throw new Error("图片识别状态不匹配，请重新上传。");
    if (task.status === "done") {
      if (!task.result || typeof task.result !== "object") throw new Error("没有读到穿搭细节，可以手动填写后保存。");
      return task.result;
    }
    if (task.status === "failed") throw new Error(userMessage(task.message, "这次没能识别图片，可以手动填写后保存。"));
    $("outfit-upload-status").textContent = task.status === "queued" ? "图片已收到，正在排队识别穿搭…" : "正在等待模型并识别穿搭，完成后会自动填入。";
    if (!await waitForImagePoll(session, ticket)) return null;
    try { task = await get("tasks/get", { id: accepted.task_id }); }
    catch (error) { throw new Error(error.message + " 后台识别仍可能继续，你也可以先手动填写。" ); }
  }
  return null;
}
function readFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader(); reader.onload = () => resolve(String(reader.result || "")); reader.onerror = () => reject(new Error("图片读取失败，请重新选择。")); reader.readAsDataURL(file);
  });
}
async function selectImage(file) {
  const session = state.dialog;
  if (!session || session.saving || !file) return;
  if (!["image/jpeg", "image/png", "image/webp"].includes(file.type)) { $("outfit-upload-status").textContent = "图片请使用 JPG、PNG 或 WebP 格式。"; return; }
  if (file.size > 20 * 1024 * 1024) { $("outfit-upload-status").textContent = "图片请控制在 20 MB 以内。"; return; }
  cancelOutfitWait(session);
  const ticket = ++session.fileToken; const revisions = { ...session.revisions };
  session.recognizing = true; session.dirty = true; updateOutfitButtons();
  $("outfit-upload-status").textContent = "正在识别穿搭…";
  try {
    const image = await readFile(file);
    if (state.dialog !== session || session.fileToken !== ticket) return;
    if (!safeImage(image)) throw new Error("图片内容读取异常，请重新选择。");
    session.image = image; setPreview(image);
    const accepted = await post("wardrobe/transcribe", { image_b64: image });
    if (!imageTaskCurrent(session, ticket)) return;
    const result = await imageTaskResult(accepted, session, ticket);
    if (state.dialog !== session || session.fileToken !== ticket) return;
    if (!result) return;
    if (typeof result.description !== "string" || !result.description.trim()) throw new Error("没有识别到穿搭细节，可以手动填写描述后保存。");
    if (session.revisions.description === revisions.description) $("outfit-description").value = result.description;
    if (session.revisions.category === revisions.category && CATEGORIES.includes(result.category)) $("outfit-category").value = result.category;
    if (session.revisions.seasons === revisions.seasons && Array.isArray(result.seasons)) for (const input of $("outfit-seasons").querySelectorAll("input")) input.checked = result.seasons.includes(input.value);
    $("outfit-upload-status").textContent = "穿搭已识别。你可以继续调整描述、分类和季节。";
  } catch (error) {
    if (state.dialog === session && session.fileToken === ticket) $("outfit-upload-status").textContent = error.message + " 可手动填写后保存。";
  } finally {
    if (state.dialog === session && session.fileToken === ticket) { session.recognizing = false; updateOutfitButtons(); }
  }
}
async function saveOutfit(event) {
  event.preventDefault(); const session = state.dialog;
  if (!session || session.saving || session.recognizing) return;
  const body = { description: $("outfit-description").value.trim(), category: $("outfit-category").value, seasons: Array.from($("outfit-seasons").querySelectorAll("input:checked"), input => input.value) };
  if (!body.description) { $("outfit-error").textContent = "请先填写穿搭描述。"; $("outfit-error").hidden = false; return; }
  if (!body.seasons.length) { $("outfit-error").textContent = "至少选一个适合的季节。"; $("outfit-error").hidden = false; return; }
  if (session.id) body.id = session.id;
  if (session.image) body.image_b64 = session.image;
  session.saving = true; updateOutfitButtons(); $("outfit-error").hidden = true;
  try {
    await post(session.id ? "wardrobe/edit" : "wardrobe/add", body);
    if (state.dialog !== session) return;
    state.dialog = null; ++state.dialogEpoch; dialog.close();
    showToast(session.id ? "穿搭已更新。" : "新穿搭已放进衣柜。");
    if (state.view === "wardrobe") await loadView("wardrobe");
  } catch (error) {
    if (state.dialog === session) { $("outfit-error").textContent = error.message; $("outfit-error").hidden = false; }
  } finally { if (state.dialog === session) { session.saving = false; updateOutfitButtons(); } }
}
function setupOutfitDialog() {
  const seasons = $("outfit-seasons");
  for (const season of SEASONS) {
    const label = element("label", "season-option"); const input = element("input"); input.type = "checkbox"; input.value = season; input.setAttribute("aria-label", season + "季"); label.append(input, element("span", "", season)); seasons.append(label);
  }
  for (const [id, key, event] of [["outfit-description", "description", "input"], ["outfit-category", "category", "change"], ["outfit-seasons", "seasons", "change"]]) $(id).addEventListener(event, () => { if (state.dialog) { ++state.dialog.revisions[key]; state.dialog.dirty = true; } });
  $("outfit-form").addEventListener("submit", saveOutfit);
  $("outfit-cancel").addEventListener("click", closeOutfit); $("outfit-close").addEventListener("click", closeOutfit);
  dialog.addEventListener("cancel", event => { event.preventDefault(); closeOutfit(); });
  dialog.addEventListener("close", () => { if (state.dialog && !state.dialog.saving) { cancelOutfitWait(state.dialog); state.dialog = null; ++state.dialogEpoch; } });
  $("outfit-image-input").addEventListener("change", event => selectImage(event.target.files?.[0]));
  const zone = $("outfit-upload");
  zone.addEventListener("keydown", event => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); if (!state.dialog?.saving) $("outfit-image-input").click(); } });
  zone.addEventListener("dragover", event => { event.preventDefault(); zone.classList.add("dragover"); });
  zone.addEventListener("dragleave", () => zone.classList.remove("dragover"));
  zone.addEventListener("drop", event => { event.preventDefault(); zone.classList.remove("dragover"); selectImage(event.dataTransfer?.files?.[0]); });
}

function zoneFor(data) {
  const zone = typeof data.timezone === "string" && data.timezone ? data.timezone : "Asia/Shanghai";
  try { new Intl.DateTimeFormat("zh-CN", { timeZone: zone }).format(0); return zone; }
  catch { return "Asia/Shanghai"; }
}
function zonedInstant(value, timezone) {
  const text = String(value || "").trim();
  if (/(?:Z|[+-]\d{2}:?\d{2})$/i.test(text)) return Date.parse(text);
  // Older records may omit an offset. Interpret those in the plugin zone,
  // never in the computer/browser's local zone.
  const match = text.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?$/);
  if (!match) return NaN;
  const local = Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]), Number(match[4]), Number(match[5]), Number(match[6] || 0));
  let instant = local;
  const formatter = new Intl.DateTimeFormat("en-CA", { timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });
  for (let attempt = 0; attempt < 3; attempt++) {
    const parts = Object.fromEntries(formatter.formatToParts(instant).map(part => [part.type, part.value]));
    const represented = Date.UTC(Number(parts.year), Number(parts.month) - 1, Number(parts.day), Number(parts.hour), Number(parts.minute), Number(parts.second));
    const corrected = local - (represented - instant);
    if (corrected === instant) break;
    instant = corrected;
  }
  return instant;
}
function timeText(value, timezone) {
  const instant = zonedInstant(value, timezone);
  return Number.isFinite(instant) ? new Intl.DateTimeFormat("zh-CN", { timeZone: timezone, hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(instant) : "时间待确认";
}
function dateTimeText(value, timezone) {
  const instant = zonedInstant(value, timezone);
  if (!Number.isFinite(instant)) return "尚未生成";
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", { timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(instant).map(part => [part.type, part.value]));
  return parts.year + "-" + parts.month + "-" + parts.day + " " + parts.hour + ":" + parts.minute;
}
function generationFor(data, view) {
  const supplied = data.generation;
  if (data.regenerating || data.status === "generating" || view === "schedule" && state.scheduleRetrying) return { status: "generating", message: view === "profile" ? "正在生成生活档案…" : "正在生成今天的日程…", attempts: 0, retry_at: null };
  if (supplied && typeof supplied === "object" && ["idle", "generating", "failed", "exhausted", "ready"].includes(supplied.status)) return supplied;
  const ready = view === "schedule" ? data.status === "ok" && Array.isArray(data.actual) && data.actual.length > 0 : Object.values(data.profile || {}).some(value => String(value || "").trim());
  return { status: data.regenerating || data.status === "generating" ? "generating" : ready ? "ready" : data.status === "failed" ? "failed" : "idle", message: "", retry_at: null };
}
function scheduleReady(data) { return data.status === "ok" && Array.isArray(data.actual) && data.actual.length > 0 && generationFor(data, "schedule").status !== "generating"; }
function scheduleNeedsPolling(data) {
  if (scheduleReady(data)) return false;
  const generation = generationFor(data, "schedule");
  return ["idle", "generating"].includes(generation.status) || generation.status === "failed" && Boolean(generation.retry_at);
}
function scheduleGenerationPanel(data) {
  const generation = generationFor(data, "schedule"), timezone = zoneFor(data);
  let title = generation.status === "generating" ? "正在生成今天的日程…" : "正在准备今天的日程…";
  let message = generation.status === "generating" ? "后台会继续准备，生成好后会自动显示，无需发送指令。" : userMessage(generation.message, "生成好后会自动显示，无需发送指令。");
  if (["failed", "exhausted"].includes(generation.status)) {
    title = generation.status === "exhausted" ? "今天的自动重试已用完" : "日程生成失败";
    message = userMessage(generation.message, "这次没能生成日程，请检查 AstrBot 的模型设置。");
    if (generation.retry_at && !message.includes("系统会在")) message += " 系统会在 " + timeText(generation.retry_at, timezone) + " 自动重试。";
    else message += " 可以点下面的立即重试，再试一次。";
  }
  const panel = statePanel(title, message, ["failed", "exhausted"].includes(generation.status));
  panel.id = "schedule-generation"; panel.dataset.generationStatus = generation.status;
  if (["generating", "failed", "exhausted"].includes(generation.status) || generation.retry_at) {
    const busy = generation.status === "generating";
    const retry = button(busy ? "正在生成中" : "立即重试", "button primary", retrySchedule, "refresh"); retry.id = "schedule-retry"; retry.disabled = state.scheduleRetrying || busy; panel.append(retry);
  }
  return panel;
}
function markScheduleSlow() {
  const panel = $("schedule-generation");
  if (!panel || !["idle", "generating"].includes(panel.dataset.generationStatus)) return;
  panel.querySelector("h2").textContent = "生成时间比较长";
  panel.querySelector("p").textContent = "可以稍后点右上角刷新，后台会继续处理。";
}
async function watchSchedule(epoch) {
  const token = ++state.pollToken;
  const deadline = setTimeout(() => {
    if (sameView("schedule", epoch) && token === state.pollToken) { stopPolling(); markScheduleSlow(); watchSlowSchedule(epoch); }
  }, 180000);
  state.pollDeadline = deadline;
  try {
    for (let attempt = 0; attempt < 36; attempt++) {
      if (!await pollWait(5000, token) || !sameView("schedule", epoch)) return;
      const data = await getSchedule();
      if (!sameView("schedule", epoch) || token !== state.pollToken || state.closed) return;
      renderSchedule(data, false);
      if (scheduleReady(data)) { watchReadySchedule(epoch); return; }
      if (!scheduleNeedsPolling(data)) return;
    }
    if (sameView("schedule", epoch) && token === state.pollToken) { markScheduleSlow(); watchSlowSchedule(epoch); }
  } catch (error) {
    if (sameView("schedule", epoch) && token === state.pollToken) content.replaceChildren(statePanel("暂时没能更新日程", error.message, true, refreshView));
  } finally {
    clearTimeout(deadline);
    if (state.pollDeadline === deadline) state.pollDeadline = null;
  }
}
async function watchSlowSchedule(epoch) {
  const token = ++state.pollToken;
  while (sameView("schedule", epoch) && token === state.pollToken) {
    if (!await pollWait(60000, token) || !sameView("schedule", epoch)) return;
    try {
      const data = await getSchedule();
      if (!sameView("schedule", epoch) || token !== state.pollToken) return;
      renderSchedule(data, false);
      if (scheduleReady(data)) { watchReadySchedule(epoch); return; }
      if (!scheduleNeedsPolling(data)) return;
      markScheduleSlow();
    } catch (error) {
      if (sameView("schedule", epoch) && token === state.pollToken) content.replaceChildren(statePanel("暂时没能更新日程", error.message, true, refreshView));
      return;
    }
  }
}
async function watchReadySchedule(epoch) {
  const token = ++state.pollToken;
  while (sameView("schedule", epoch) && token === state.pollToken && !state.closed) {
    if (!await pollWait(60000, token) || !sameView("schedule", epoch)) return;
    try {
      await refreshSetup();
      if (!sameView("schedule", epoch) || token !== state.pollToken || state.closed) return;
      const data = await getSchedule();
      if (!sameView("schedule", epoch) || token !== state.pollToken || state.closed) return;
      renderSchedule(data, false);
      if (!scheduleReady(data)) {
        if (scheduleNeedsPolling(data)) watchSchedule(epoch);
        return;
      }
    } catch (error) {
      if (!sameView("schedule", epoch) || token !== state.pollToken || state.closed) return;
      let note = $("schedule-refresh-error");
      if (!note) { note = element("div", "banner"); note.id = "schedule-refresh-error"; content.prepend(note); }
      note.textContent = error.message + " 当前显示的是上次读到的日程，可以点刷新重新查看。";
      if (/登录|权限/.test(error.message)) return;
    }
  }
}
async function retrySchedule() {
  if (state.scheduleRetrying) return;
  ++state.epoch; state.scheduleRetrying = true; stopPolling();
  content.replaceChildren(scheduleGenerationPanel({ generation: { status: "generating", message: "正在生成今天的日程…", attempts: 0, retry_at: null } }));
  try {
    await post("schedule/retry", {});
    if (!state.closed && state.view === "schedule") { showToast("已经在准备日程，生成好后会自动显示。"); await loadView("schedule"); }
  } catch (error) { state.scheduleRetrying = false; if (!state.closed && state.view === "schedule") { showToast(error.message, true); await loadView("schedule"); } }
  finally { state.scheduleRetrying = false; const current = $("schedule-retry"); if (current && $("schedule-generation")?.dataset.generationStatus !== "generating") { current.disabled = false; current.textContent = "立即重试"; } }
}
function renderSchedule(data, watch = true) {
  state.scheduleData = { ...data, outfits: todayOutfitRows(data) }; data = state.scheduleData;
  const timezone = zoneFor(data);
  actions.replaceChildren(button("刷新", "button secondary", refreshView, "refresh"));
  const weather = data.weather || {}; const overview = element("div", "overview"), badge = element("div", "overview-icon"); badge.append(icon("weather"));
  const weatherCopy = element("div");
  weatherCopy.append(element("h2", "", [data.date, data.weekday].filter(Boolean).join(" · ")));
  const temperature = weather.temp_min !== undefined && weather.temp_min !== null ? weather.temp_min + "～" + weather.temp_max + "℃" : "";
  const fallbackFrom = String(weather.fallback_from || "").trim(), city = String(weather.city || "").trim();
  const weatherNote = weather.source === "ai" ? "（AI 按季节模拟）" : fallbackFrom && city ? "（参考" + city + "天气）" : "";
  weatherCopy.append(element("p", "", [fallbackFrom || city, weather.summary, temperature].filter(Boolean).join(" · ") + weatherNote));
  overview.append(badge, weatherCopy);
  const nodes = [overview]; if (data.day_summary) nodes.push(element("p", "day-summary", data.day_summary));
  if (!scheduleReady(data)) {
    nodes.push(scheduleGenerationPanel(data)); content.replaceChildren(...nodes);
    if (watch && scheduleNeedsPolling(data)) watchSchedule(state.epoch);
    return;
  }
  if (["failed", "exhausted"].includes(generationFor(data, "schedule").status)) nodes.push(scheduleGenerationPanel(data));
  const outfits = todayOutfitSection(data); if (outfits) nodes.push(outfits);
  const serverNow = zonedInstant(data.server_time || data.now, timezone);
  const now = Number.isFinite(serverNow) ? serverNow : Date.now(); const timeline = element("div", "timeline"); timeline.id = "schedule-timeline";
  const slots = [...data.actual].sort((a, b) => zonedInstant(a.start, timezone) - zonedInstant(b.start, timezone));
  for (const slot of slots) {
    const current = zonedInstant(slot.start, timezone) <= now && now < zonedInstant(slot.end, timezone), past = zonedInstant(slot.end, timezone) <= now;
    const item = element("article", "timeline-item" + (current ? " current" : past ? " past" : ""));
    item.dataset.state = current ? "current" : past ? "past" : "future";
    const marker = element("div", "timeline-marker", current ? "▶ 现在" : past ? "已过去" : "计划");
    const card = element("div", "timeline-card");
    card.append(element("div", "timeline-time", timeText(slot.start, timezone) + " – " + timeText(slot.end, timezone)), element("h2", "timeline-activity", slot.activity || "未填写活动"));
    const meta = element("div", "slot-meta");
    const rest = { night_sleep: "夜睡 · 不看手机", nap: "午睡 · 不看手机" }[slot.rest_type];
    const phone = rest || { free: "能及时看手机", glance: "偶尔看手机", offline: "暂时不看手机" }[slot.phone];
    for (const [name, text] of [["location", slot.location], ["phone", phone], ["mood", slot.mood]]) if (text) { const label = element("span"); label.append(icon(name), element("span", "", text)); meta.append(label); }
    const outfitLabel = element("span", "slot-outfit", slot.outfit_label || OUTFIT_LABELS[slot.outfit_type] || "居家服");
    card.append(meta, outfitLabel); item.append(marker, card); timeline.append(item);
  }
  nodes.push(timeline); content.replaceChildren(...nodes);
  if (watch) watchReadySchedule(state.epoch);
}

function todayOutfitRows(data) {
  let rows = Array.isArray(data.outfits) ? data.outfits : [];
  if (data.outfits && typeof data.outfits === "object" && !Array.isArray(data.outfits)) {
    rows = Object.entries(data.outfits).filter(([key, value]) => OUTFIT_NAMES[key] && value && typeof value === "object").map(([key, value]) => ({ ...value, key, kind: key }));
    for (const [index, value] of (Array.isArray(data.sport_outfits) ? data.sport_outfits : []).entries()) if (value && typeof value === "object") rows.push({ ...value, key: "sports:" + index, kind: "sports" });
  }
  return rows.filter(row => row && typeof row === "object").map(row => {
    const kind = row.kind || (String(row.key || "").startsWith("sports:") ? "sports" : row.key);
    const source = ["temporary", "wardrobe", "deleted"].includes(row.source) ? row.source : row.temporary ? "temporary" : "wardrobe";
    return { ...row, kind, source, label: row.label || OUTFIT_NAMES[kind] || "穿搭", description: typeof row.description === "string" ? row.description.replace(/^(?:临时搭配\s*[:：]\s*)+/, "") : "" };
  });
}
function collectionKey(date, row) { return JSON.stringify([date, row.key, row.fingerprint]); }
async function getSchedule() {
  const version = state.collectionVersion, data = await get("schedule/today");
  const outfits = todayOutfitRows(data).map(row => {
    const confirmed = state.confirmedCollections.get(collectionKey(data.date, row));
    // Only repair a read started before this collection was acknowledged.
    // Later reads remain authoritative for wardrobe edits and deletions.
    return row.source === "temporary" && confirmed && confirmed.version > version ? { ...confirmed.row } : row;
  });
  return { ...data, outfits };
}
function outfitSource(row) {
  if (row.source === "temporary") return "AI 临时搭配";
  if (row.source === "deleted") return "已从衣柜删除";
  return Number.isInteger(row.wardrobe_number) && row.wardrobe_number > 0 ? "衣柜·穿搭" + row.wardrobe_number : row.source_label || "来自衣柜";
}
function todayOutfitSection(data) {
  if (!data.outfits.length && !data.outfit_notes) return null;
  const section = element("section", "today-outfits"); section.id = "today-outfits"; section.setAttribute("aria-labelledby", "today-outfits-title");
  const heading = element("h2", "today-outfits-title"); heading.id = "today-outfits-title"; heading.append(icon("hanger"), element("span", "", "今日穿搭")); section.append(heading);
  const grid = element("div", "today-outfit-grid");
  for (const row of data.outfits) {
    const item = element("article", "today-outfit"); item.dataset.outfitKey = row.key;
    const media = element("div", "today-outfit-media"), placeholder = imagePlaceholder(); media.append(placeholder);
    if (safeImage(row.image)) {
      const image = element("img"); image.src = row.image; image.alt = row.label + "的展示图"; image.loading = "lazy"; image.decoding = "async"; placeholder.hidden = true;
      image.addEventListener("error", () => { image.hidden = true; placeholder.hidden = false; }); media.append(image);
    }
    const body = element("div", "today-outfit-body"), name = element("div", "today-outfit-heading");
    name.append(element("h3", "", row.label), element("span", "outfit-source " + row.source, outfitSource(row)));
    body.append(name, descriptionBlock(row.description));
    const busy = state.collecting.has(collectionKey(data.date, row));
    if (row.source === "temporary" && row.can_collect === true && typeof row.fingerprint === "string" && row.fingerprint) {
      const collect = button(busy ? "正在收进衣柜…" : "收进衣柜", "button secondary collect-outfit", () => collectOutfit(data.date, row), "plus");
      collect.dataset.action = "collect"; collect.dataset.outfitKey = row.key; collect.disabled = busy;
      collect.setAttribute("aria-label", "把" + row.label + "穿搭收进衣柜"); body.append(collect);
    } else if (row._collectionMessage) {
      const receipt = button(row._collectionMessage, "button secondary collect-outfit collected"); receipt.disabled = true; body.append(receipt);
    }
    if (row._collectionError) { const error = element("p", "collection-error", row._collectionError); error.setAttribute("role", "alert"); body.append(error); }
    item.append(media, body); grid.append(item);
  }
  section.append(grid);
  if (data.outfit_notes) section.append(element("p", "outfit-notes", "补充：" + data.outfit_notes));
  return section;
}
function collectionCurrent(date, row) {
  if (state.closed || state.view !== "schedule" || state.scheduleData?.date !== date) return null;
  return state.scheduleData.outfits.find(value => value.key === row.key && value.fingerprint === row.fingerprint) || null;
}
function redrawTodayOutfits() {
  const old = $("today-outfits"), fresh = state.scheduleData && todayOutfitSection(state.scheduleData);
  if (old && fresh) { old.parentNode.insertBefore(fresh, old); old.remove(); }
}
async function collectOutfit(date, row) {
  const identity = collectionKey(date, row), roleRevision = state.roleRevision;
  if (state.collecting.has(identity) || !collectionCurrent(date, row)) return;
  state.collecting.add(identity); row._collectionError = ""; redrawTodayOutfits();
  try {
    const result = await post("wardrobe/collect", { date, outfit_key: row.key, fingerprint: row.fingerprint });
    if (roleRevision !== state.roleRevision) return;
    const current = collectionCurrent(date, row); if (!current) return;
    if (!result.saved) throw new Error("这套穿搭还没有收进衣柜，请刷新后再试。");
    if (!result.outfit || result.outfit.key !== row.key) { await loadView("schedule"); return; }
    const message = userMessage(result.message, "已收进衣柜");
    const saved = { ...result.outfit, _collectionMessage: message };
    for (const [key, value] of state.confirmedCollections) if (value.date !== date) state.confirmedCollections.delete(key);
    state.confirmedCollections.set(identity, { date, version: ++state.collectionVersion, row: saved });
    state.scheduleData.outfits = state.scheduleData.outfits.map(value => value === current ? saved : value);
    showToast(message);
  } catch (error) {
    const current = roleRevision === state.roleRevision && collectionCurrent(date, row);
    if (current) { current._collectionError = error.message; showToast(error.message, true); }
  } finally {
    if (roleRevision === state.roleRevision) {
      state.collecting.delete(identity);
      if (collectionCurrent(date, row)) redrawTodayOutfits();
      else if (!state.closed && state.view === "schedule" && state.scheduleData?.date === date && state.scheduleData.outfits.some(value => value.key === row.key && value._collectionMessage)) redrawTodayOutfits();
    }
  }
}

function actionFooter(saveId, saveLabel, onSave, statusId, extra) {
  const footer = element("div", "sticky-actions"), status = element("span", "save-status", "尚未修改"); status.id = statusId;
  const group = element("div", "action-group"); if (extra) group.append(extra);
  const save = button(saveLabel, "button primary", onSave); save.id = saveId; save.disabled = true; group.append(save); footer.append(status, group); return footer;
}
function markDirty(view, dirty = true) {
  state.dirty[view] = dirty;
  const save = $(view + "-save"), status = $(view + "-save-status");
  if (save) save.disabled = !dirty || Boolean(state.saving[view]);
  if (status) { status.textContent = dirty ? "有未保存的修改" : "修改已保存"; status.classList.toggle("dirty", dirty); }
  if (view === "settings") renderSetupBanner();
}
function profileValues() { return Object.fromEntries(PROFILE_FIELDS.map(([key]) => [key, $("profile-" + key).value])); }
function profileRetryButton(label, id = "") {
  const retry = button(state.profileGenerating ? "正在生成中" : label, "button secondary", regenerateProfile);
  retry.dataset.action = "profile-regenerate"; if (id) retry.id = id; retry.disabled = state.profileGenerating;
  return retry;
}
function profileGenerationBanner(data, generation) {
  const banner = element("div", "banner generation-banner"), copy = element("div", "generation-copy");
  banner.id = "profile-generation"; banner.dataset.generationStatus = state.profileGenerating ? "generating" : generation.status;
  let title, message;
  if (state.profileGenerating) {
    title = "正在生成生活档案…";
    message = "后台会继续生成，完成后会自动更新这一页。你也可以先去看看其他板块。";
  } else if (generation.status === "exhausted") {
    title = "今天的自动重试已用完";
    message = userMessage(generation.message, "检查 AstrBot 的模型设置后，仍然可以点重新生成再试一次。");
  } else if (generation.status === "failed") {
    title = "档案生成失败";
    message = userMessage(generation.message, "这次没能生成生活档案，请检查 AstrBot 的模型设置后重试。");
  } else {
    title = "还没有生活档案";
    message = "点重新生成，就能根据人设建立生活习惯；也可以直接填写下面的内容。";
  }
  if (!state.profileGenerating && generation.retry_at && !message.includes("系统会在")) message += " 系统会在 " + timeText(generation.retry_at, zoneFor(data)) + " 自动重试。";
  copy.append(element("strong", "", title), element("p", "", message));
  if (!state.profileGenerating && Number.isFinite(Number(generation.attempts)) && Number(generation.attempts) > 0) copy.append(element("span", "generation-attempts", "今天已尝试 " + Number(generation.attempts) + " 次"));
  banner.append(copy, profileRetryButton("重新生成", "profile-retry"));
  return banner;
}
function renderProfile(data, watch = true) {
  state.dirty.profile = false; state.saving.profile = false;
  const generation = generationFor(data, "profile");
  const regenerating = Boolean(data.regenerating || state.profileRequest || state.profileTask || generation.status === "generating");
  state.profileGenerating = regenerating;
  actions.replaceChildren(button("刷新", "button secondary", refreshView, "refresh"));
  const form = element("form"); form.id = "profile-form";
  const generated = data.profile_generated_at ? dateTimeText(data.profile_generated_at, zoneFor(data)) : "尚未生成";
  const meta = element("div", "profile-meta"), hashStatus = element("span", "status-pill", data.persona_changed ? "人设已变化" : data.persona_hash_status === "matched" ? "与当前人设一致" : "人设状态待确认"); hashStatus.id = "profile-hash-status";
  meta.append(element("span", "", "最近生成：" + generated), hashStatus);
  form.append(meta);
  if (!regenerating && data.persona_warning) form.append(element("div", "banner", userMessage(data.persona_warning, "没能读到全局默认人设，请在 AstrBot 人设管理中检查。")));
  if (!regenerating && data.persona_changed) {
    const banner = element("div", "banner"); banner.id = "profile-persona-warning"; banner.append(element("span", "", "后续生成采用最新人设，已有资料保留。")); form.append(banner);
  }
  if (regenerating || generation.status !== "ready") form.append(profileGenerationBanner(data, generation));
  const fields = element("div", "profile-fields");
  for (const [key, title] of PROFILE_FIELDS) {
    const row = element("div", "profile-field"), label = element("label", "", title), input = element("textarea", "text-input");
    input.id = "profile-" + key; label.htmlFor = input.id; input.value = String(data.profile?.[key] || ""); input.rows = key === "social_circle" ? 3 : 2; input.disabled = regenerating;
    input.addEventListener("input", () => markDirty("profile", JSON.stringify(profileValues()) !== state.baseline.profile)); row.append(label, input); fields.append(row);
  }
  form.append(fields, actionFooter("profile-save", "保存修改", saveProfile, "profile-save-status", profileRetryButton("重新生成档案", "profile-regenerate")));
  form.addEventListener("submit", event => { event.preventDefault(); saveProfile(); }); content.replaceChildren(form);
  state.baseline.profile = JSON.stringify(profileValues());
  if (regenerating) form.querySelectorAll("button").forEach(button => { button.disabled = true; });
  if (regenerating) $("profile-save-status").textContent = "正在生成中，完成后会自动更新";
  if (watch) {
    if (state.profileTask) watchProfileTask(state.profileTask);
    else if (regenerating) watchProfileGeneration(state.epoch);
  }
}
function showProfileGenerating() {
  state.profileGenerating = true;
  const form = $("profile-form"); if (!form) return;
  form.querySelectorAll(".banner").forEach(node => { node.remove(); });
  form.prepend(profileGenerationBanner({}, { status: "generating", attempts: 0, retry_at: null }));
  form.querySelectorAll("button,textarea").forEach(node => { node.disabled = true; });
  form.querySelectorAll("[data-action='profile-regenerate']").forEach(node => { node.textContent = "正在生成中"; });
  $("profile-save-status").textContent = "正在生成中，完成后会自动更新";
}
async function saveProfile() {
  if (state.saving.profile || !state.dirty.profile) return;
  state.saving.profile = true; $("profile-save").disabled = true; $("profile-save").textContent = "正在保存…";
  const epoch = state.epoch; const values = profileValues();
  $("profile-form").querySelectorAll("textarea").forEach(input => { input.disabled = true; });
  try {
    await post("profile/save", values);
    if (sameView("profile", epoch)) { state.baseline.profile = JSON.stringify(values); markDirty("profile", false); $("profile-hash-status").textContent = "与当前人设一致"; if ($("profile-persona-warning")) $("profile-persona-warning").hidden = true; showToast("生活档案已保存。"); }
  } catch (error) { if (sameView("profile", epoch)) showToast(error.message, true); }
  finally {
    state.saving.profile = false;
    if (sameView("profile", epoch)) { $("profile-form").querySelectorAll("textarea").forEach(input => { input.disabled = false; }); $("profile-save").textContent = "保存修改"; $("profile-save").disabled = !state.dirty.profile; }
  }
}
async function regenerateProfile() {
  if (state.saving.profile || state.profileTask || state.profileGenerating) return;
  const viewEpoch = state.epoch;
  if (state.dirty.profile && !await confirmAction("放弃档案修改？", "还有未保存的档案修改，确认放弃并重新生成吗？", "放弃修改")) return;
  if (!await confirmAction("重新生成生活档案？", "确认重新生成当前角色的生活档案吗？旧档案会先备份，并接着更新今后日程；衣柜、约定、事件和过去记录会保留。", "重新生成") || !sameView("profile", viewEpoch)) return;
  const epoch = ++state.epoch; stopPolling(); state.saving.profile = true; state.profileRequest = true;
  showProfileGenerating();
  try {
    const task = await post("profile/regenerate", {});
    if (task.type && task.type !== "profile") throw new Error("档案生成状态没有对上，请点刷新重新查看。");
    if (!state.closed && state.view === "profile") {
      const setup = await get("setup/status");
      if (state.closed) return;
      acceptSetup(setup); renderSetupBanner();
      if (state.setup.status !== "ready") {
        state.profileTask = null; state.profileGenerating = false; state.dirty.profile = false;
        renderSetupGate(); watchSetup(); return;
      }
    }
    if (!task.task_id) {
      state.profileRequest = false;
      if (!state.closed && state.view === "profile") await loadView("profile");
      return;
    }
    state.profileTask = task.task_id; state.dirty.profile = false;
    state.profileGenerating = true;
    if (!state.closed && state.view === "profile") {
      showProfileGenerating();
      showToast("已开始重新生成，旧档案会先备份，随后更新日程。");
      if ($("profile-save-status")) $("profile-save-status").textContent = "正在生成中，完成后会自动更新";
      $("profile-form")?.querySelectorAll("[data-action='profile-regenerate']").forEach(button => { button.disabled = true; button.textContent = "正在生成中"; });
      watchProfileTask(task.task_id);
    }
  } catch (error) { state.profileRequest = false; if (!state.closed && state.view === "profile") { showToast(error.message, true); await loadView("profile"); } }
  finally {
    state.saving.profile = false; state.profileRequest = false;
    if (sameView("profile", epoch) && !state.profileTask && !state.profileGenerating && $("profile-form")) { $("profile-form").querySelectorAll("button,textarea").forEach(node => { node.disabled = false; }); $("profile-save").disabled = !state.dirty.profile; }
  }
}
async function watchProfileTask(taskId) {
  const token = ++state.pollToken, epoch = state.epoch;
  for (let attempt = 0; ; attempt++) {
    if (attempt >= 120 && sameView("profile", epoch) && token === state.pollToken) $("profile-save-status").textContent = "生成时间比较长，后台仍在处理，完成后会自动显示。";
    if (!await pollWait(attempt < 120 ? 2000 : 60000, token) || !sameView("profile", epoch)) return;
    try {
      const task = await get("tasks/get", { id: taskId });
      if (!sameView("profile", epoch) || token !== state.pollToken) return;
      if (task.type && task.type !== "profile") throw new Error("档案生成状态没有对上，请点刷新重新查看。");
      if (task.status === "done") { state.profileTask = null; showToast("生活档案已重新生成。"); await loadView("profile"); return; }
      if (task.status === "failed") { state.profileTask = null; showToast(userMessage(task.message, "档案生成失败，请稍后重试。"), true); await loadView("profile"); return; }
    } catch (error) {
      if (!sameView("profile", epoch) || token !== state.pollToken) return;
      state.profileTask = null;
      showToast(error.message, true);
      // A missing task ID is not evidence that the server worker stopped.
      // Re-read its authoritative busy flag before exposing any edit controls.
      await loadView("profile");
      return;
    }
  }
}
async function watchProfileGeneration(epoch) {
  const token = ++state.pollToken;
  for (let attempt = 0; ; attempt++) {
    if (attempt >= 120 && sameView("profile", epoch) && token === state.pollToken) $("profile-save-status").textContent = "生成时间比较长，后台仍在处理，完成后会自动显示。";
    if (!await pollWait(attempt < 120 ? 2000 : 60000, token) || !sameView("profile", epoch)) return;
    try {
      const data = await get("profile/get");
      if (!sameView("profile", epoch) || token !== state.pollToken || state.closed) return;
      renderProfile(data, false);
      if (!data.regenerating && generationFor(data, "profile").status !== "generating") return;
    } catch (error) {
      if (sameView("profile", epoch) && token === state.pollToken) content.replaceChildren(statePanel("暂时没能更新档案", error.message, true, refreshView));
      return;
    }
  }
}

function field(labelText, id, input, hint = "") {
  const wrapper = element("div", "field"), label = element("label", "field-label", labelText);
  input.id = id; label.htmlFor = id; wrapper.append(label, input); if (hint) wrapper.append(element("p", "field-hint", hint)); return wrapper;
}
function settingsSection(title) { const section = element("section", "form-section"); section.append(element("h2", "", title)); return section; }
function input(type, value = "") { const node = element("input", "text-input"); node.type = type; node.value = String(value ?? ""); return node; }
function splitValues(value) { return String(value || "").split(/[,，\n]+/).map(value => value.trim()).filter(Boolean); }
function splitOwners(value) { return String(value || "").split(/[\s,，]+/).map(value => value.trim()).filter(Boolean); }
function renderSettings(data) {
  state.dirty.settings = false; state.saving.settings = false;
  const form = element("form"); form.id = "settings-form";
  const basics = settingsSection("天气和日程基础"), basicFields = element("div", "field-grid");
  const city = input("text", data.city); city.placeholder = "例如：杭州";
  const time = input("time", data.schedule_time || "07:00"); time.required = true;
  const owners = input("text", Array.isArray(data.owner_ids) ? data.owner_ids.join(", ") : data.owner_ids); owners.placeholder = "多个用户 ID 用逗号分隔";
  const ownerField = field("主人用户 ID", "settings-owner_ids", owners, "留空使用 AstrBot 管理员；多个 ID 用逗号分隔。"); ownerField.classList.add("full");
  basicFields.append(field("角色所在城市", "settings-city", city, "留空时按季节模拟天气；填写城市名后会查询真实天气。"), field("每日生成时间", "settings-schedule_time", time), ownerField); basics.append(basicFields);
  const models = settingsSection("模型选择"), provider = element("select", "text-input");
  const automatic = element("option", "", "跟随 AstrBot 当前模型"); automatic.value = ""; provider.append(automatic);
  const providers = Array.isArray(data.providers) ? data.providers : [];
  for (const item of providers) { if (!item || !item.id) continue; const option = element("option", "", item.name || item.model || item.id); option.value = item.id; provider.append(option); }
  if (data.llm_provider && !providers.some(item => item.id === data.llm_provider)) { const current = element("option", "", "当前配置：" + data.llm_provider); current.value = data.llm_provider; provider.append(current); }
  provider.value = data.llm_provider || ""; models.append(field("日程与档案模型", "settings-llm_provider", provider));
  const rhythm = settingsSection("日程细节调整"), ranges = element("div", "field-grid");
  for (const [key, title, value, max, step, suffix] of [["routine_jitter_minutes", "起床 / 入睡浮动", data.routine_jitter_minutes ?? 20, "60", "1", "分钟"], ["surprise_probability", "每天至少一次小意外", data.surprise_probability ?? 0.5, "1", "0.1", "%"]]) {
    const wrapper = element("div", "field"), heading = element("div", "range-header"), label = element("label", "field-label", title), range = input("range", value), output = element("output", "range-value");
    range.id = "settings-" + key; range.className = "range-input"; range.min = "0"; range.max = max; range.step = step; label.htmlFor = range.id; output.htmlFor = range.id;
    // Native range inputs clamp against their default step before attributes
    // are assigned. Restore the saved value after configuring min/max/step.
    range.value = String(value);
    const update = () => { output.textContent = (suffix === "%" ? Math.round(Number(range.value) * 100) : range.value) + suffix; }; update(); range.addEventListener("input", update);
    heading.append(label, output); wrapper.append(heading, range); ranges.append(wrapper);
  }
  rhythm.append(ranges);
  const keywords = settingsSection("聊天改日程关键词"), textarea = element("textarea", "text-input"); textarea.rows = 4; textarea.value = Array.isArray(data.change_trigger_keywords) ? data.change_trigger_keywords.join("\n") : String(data.change_trigger_keywords || "");
  keywords.append(field("一行一个关键词，或用逗号分隔", "settings-change_trigger_keywords", textarea, "有这些词时才判断聊天是否改变了日程，留空则每条消息都判断（较费钱）"));
  form.append(basics, models, rhythm, keywords, actionFooter("settings-save", "保存设置", saveSettings, "settings-save-status"));
  form.addEventListener("input", () => markDirty("settings", JSON.stringify(settingsValues()) !== state.baseline.settings)); form.addEventListener("change", () => markDirty("settings", JSON.stringify(settingsValues()) !== state.baseline.settings));
  form.addEventListener("submit", event => { event.preventDefault(); saveSettings(); }); content.replaceChildren(form);
  state.baseline.settings = JSON.stringify(settingsValues());
  renderSetupBanner();
}
function settingsValues() {
  const value = key => $("settings-" + key).value;
  return { city: value("city").trim(), schedule_time: value("schedule_time"), owner_ids: splitOwners(value("owner_ids")), llm_provider: value("llm_provider"), routine_jitter_minutes: Number(value("routine_jitter_minutes")), surprise_probability: Number(value("surprise_probability")), change_trigger_keywords: splitValues(value("change_trigger_keywords")) };
}
async function saveSettings() {
  if (state.saving.settings || !state.dirty.settings) return;
  const body = settingsValues();
  if (!/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(body.schedule_time)) { showToast("生成时间请填 00:00 到 23:59 之间的时间，例如 07:00。", true); return; }
  const epoch = state.epoch; state.saving.settings = true; stopSetupPolling();
  $("settings-form").querySelectorAll("input,textarea,select,button").forEach(node => { node.disabled = true; }); $("settings-save").textContent = "正在保存…";
  try { const result = await post("settings/save", body); if (sameView("settings", epoch)) { state.baseline.settings = JSON.stringify(body); markDirty("settings", false); showToast(userMessage(result.message, "基础设置已保存。")); await refreshSetup(); } }
  catch (error) { if (sameView("settings", epoch)) showToast(error.message, true); }
  finally {
    state.saving.settings = false;
    if (sameView("settings", epoch)) { $("settings-form").querySelectorAll("input,textarea,select,button").forEach(node => { node.disabled = false; }); $("settings-save").textContent = "保存设置"; $("settings-save").disabled = !state.dirty.settings; renderSetupBanner(); watchSetup(); }
  }
}

onConnection(connected => {
  const node = $("connection-state"); node.classList.toggle("connected", connected);
  node.querySelector(".connection-label").textContent = connected ? "已连接 AstrBot" : "未连接 AstrBot";
  node.title = connected ? "已连接 AstrBot" : "未连接 AstrBot";
});
window.addEventListener("beforeunload", event => { if (hasDirty() || state.dialog?.dirty) { event.preventDefault(); event.returnValue = ""; } });
window.addEventListener("pagehide", () => { state.closed = true; finishConfirmation(false); stopPolling(); stopSetupPolling(); clearReferences(); cancelOutfitWait(state.dialog); ++state.dialogEpoch; state.dialog = null; if (dialog.open) dialog.close(); });
window.addEventListener("pageshow", event => { if (event.persisted) { state.closed = false; if (!hasDirty()) loadView(state.view); else if (!DASHBOARD_VIEWS.has(state.view)) watchSetup(); } });
window.addEventListener("popstate", () => navigate(viewFromLocation(), true));
window.addEventListener("hashchange", () => navigate(viewFromLocation(), true));
$("confirmation-cancel").addEventListener("click", () => finishConfirmation(false));
$("confirmation-accept").addEventListener("click", () => finishConfirmation(true));
confirmationDialog.addEventListener("cancel", event => { event.preventDefault(); finishConfirmation(false); });
buildNavigation(); setupOutfitDialog(); loadView(viewFromLocation());
