// AstrBot supplies the authenticated bridge when it serves this plugin view.
let bridge;
let preparing;
let connected = false;
const listeners = new Set();

function connection(value) {
  connected = value;
  listeners.forEach(listener => listener(value));
}

export function onConnection(listener) {
  listeners.add(listener);
  listener(connected);
  return () => listeners.delete(listener);
}

async function bounded(promise, milliseconds) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((resolve, reject) => {
      timer = setTimeout(() => reject(new Error(milliseconds > 20000 ? "等待超过时限，处理仍可能在后台继续。请稍后刷新查看结果。" : "连接超时，请稍后重试。")), milliseconds);
    })]);
  } finally { clearTimeout(timer); }
}

function unwrap(value) {
  if (!value || typeof value !== "object") throw new Error("服务返回的数据格式有误。");
  if (value.status === "error" || value.ok === false) throw new Error(value.message || value.error || "操作未完成。");
  if ((value.status === "ok" || value.status === "success" || value.ok === true) && "data" in value) return value.data;
  return value;
}

export function userMessage(value, fallback = "这次操作没有完成，请稍后重试。") {
  const text = String(value || "").trim();
  if (!text) return fallback;
  if (/401|403|unauthor|forbidden|login|expired|登录|认证|过期/i.test(text)) {
    return "登录已失效或没有操作权限，请返回 AstrBot 重新登录。";
  }
  if (/413|payload too large|request entity too large/i.test(text)) return "这张图片太大了，请缩小后重新上传。";
  if (/409|conflict/i.test(text)) return "已经在处理中，请稍后点刷新查看结果。";
  if (/404|not found/i.test(text)) return "暂时没有找到这项内容，请点刷新重新查看。";
  if (/network error|failed to fetch|load failed|connection refused|connection reset|ENOTFOUND|ECONN|DNS/i.test(text)) return "暂时连不上 AstrBot，请检查连接后点刷新重试。";
  if (/timeout|timed out|operationtimeout/i.test(text)) return "等候时间有些长，请稍后重试；已经开始的生成会继续在后台处理。";
  if (/HTTP\s*5\d\d|status code 5\d\d|internal server error/i.test(text)) return "AstrBot 暂时没有处理好，请稍后重试。";
  if (/HTTP\s*400|status code 400|bad request/i.test(text)) return "填写的内容没有保存成功，请检查后重试。";
  if (/Traceback|\n\s*File\s+"/i.test(text)) return fallback;
  const clean = text.replace(/^(?:[A-Za-z_][\w.]*(?:Error|Exception)|Error|Exception)\s*[:：]\s*/i, "");
  if (!/[\u3400-\u9fff]/.test(clean)) return fallback;
  if (/(?:ValueError|TypeError|RuntimeError|AttributeError|KeyError|HTTP\s*\d{3}|status code|Traceback)/i.test(clean)) return fallback;
  return clean;
}

function explain(error) {
  const text = error?.message || "连接失败，请检查 AstrBot 后重试。";
  if (/401|403|unauthor|forbidden|login|expired|登录|认证|过期/i.test(text)) connection(false);
  if (/network|fetch|连接|timeout|超时|load failed/i.test(text)) connection(false);
  return new Error(userMessage(text));
}

export async function ready() {
  if (!preparing) {
    preparing = (async () => {
      bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
      if (!bridge || typeof bridge.ready !== "function" || typeof bridge.apiGet !== "function" || typeof bridge.apiPost !== "function") {
        throw new Error("请从 AstrBot 的插件视图中打开拟人生活。");
      }
      await bounded(bridge.ready(), 12000);
      connection(true);
    })().catch(error => { preparing = null; connection(false); throw explain(error); });
  }
  return preparing;
}

async function request(endpoint, body, params) {
  try {
    await ready();
    const value = await bounded(body === undefined ? bridge.apiGet(endpoint, params) : bridge.apiPost(endpoint, body), body === undefined ? 20000 : 210000);
    const data = unwrap(value);
    connection(true);
    return data;
  } catch (error) { throw explain(error); }
}

export const get = (endpoint, params) => request(endpoint, undefined, params);
export const post = (endpoint, body) => request(endpoint, body || {});
