import { get, post } from "./api.js";

const byId = id => document.getElementById(id);
const faceLabels = ["正脸建议", "左 45° 建议", "右 45° 建议"];
const imageTypes = new Set(["image/jpeg", "image/png", "image/webp"]);
const maxImageBytes = 20 * 1024 * 1024;
const bodyLimit = 3;
let refresh = null;
const configStatus = byId("status");
const options = byId("options");
const references = byId("references");
let reading = false;
let changing = false;
let revision = 0;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  if (className) node.className = className;
  return node;
}

async function imageSource(target) {
  const { mime, base64 } = await get("photo/reference", target);
  if (!imageTypes.has(mime) || typeof base64 !== "string" || !base64) {
    throw new Error("图片格式无法预览");
  }
  return "data:" + mime + ";base64," + base64;
}

function updateControls() {
  const busy = reading || changing;
  if (refresh) refresh.disabled = busy;
  for (const control of references.querySelectorAll("[data-reference-action]")) control.disabled = busy;
}

function uploadStatus(status, message, error = false) {
  status.textContent = message;
  status.dataset.error = String(error);
}

function readImage(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error("图片读取失败，请重新选择。"));
    reader.readAsDataURL(file);
  });
}

function visibleError(error, fallback) {
  const message = error?.message || "";
  return /^[\u3400-\u9fff][^<>/\\\r\n]{0,100}$/.test(message) ? message : fallback;
}

async function changeReference(operation, target, feedback, file) {
  if (reading || changing) return;
  const current = revision;
  const isUpload = operation === "upload";
  const action = isUpload ? (target.index === null ? "上传" : "替换") : "删除";
  const groupStatus = byId(target.group + "-upload-status");
  uploadStatus(groupStatus, "");
  if (isUpload && !imageTypes.has(file.type)) {
    uploadStatus(feedback, "请选择 JPG、PNG 或 WebP 图片。", true);
    return;
  }
  if (isUpload && file.size > maxImageBytes) {
    uploadStatus(feedback, "图片不能超过 20 MB。", true);
    return;
  }
  changing = true;
  updateControls();
  uploadStatus(feedback, "正在" + action + "…");
  try {
    const payload = isUpload ? { ...target, image: await readImage(file) } : target;
    const data = await post("photo/reference/" + operation, payload);
    if (current === revision) {
      render(data);
      uploadStatus(groupStatus, "已" + action + "参考图。");
    }
  } catch (error) {
    if (current === revision) uploadStatus(feedback, visibleError(error, action + "失败，请稍后重试。"), true);
  } finally {
    changing = false;
    if (current !== revision && refresh) await readReferences(revision);
    else updateControls();
  }
}

function referenceControls(target, label, action) {
  const control = element("div", "", "reference-controls");
  const input = element("input");
  input.type = "file";
  input.accept = [...imageTypes].join(",");
  input.hidden = true;
  input.dataset.referenceAction = "";
  input.disabled = reading || changing;
  const feedback = element("p", "", "upload-status");
  feedback.setAttribute("role", "status");
  const actions = [[action, () => input.click()]];
  if (target.index !== null) {
    actions.push(["删除", () => changeReference("delete", target, feedback)]);
  }
  for (const [text, onClick] of actions) {
    const button = element("button", text, text === "删除" ? "button danger" : "button secondary");
    button.type = "button";
    button.dataset.referenceAction = "";
    button.disabled = reading || changing;
    button.setAttribute("aria-label", text + label);
    button.addEventListener("click", onClick);
    control.append(button);
  }
  input.addEventListener("change", () => {
    const file = input.files[0];
    input.value = "";
    if (file) changeReference("upload", target, feedback, file);
  });
  control.append(input, feedback);
  return control;
}

function referenceTile(target, label, configured, canUpload) {
  const card = element("article", "", "tile");
  const title = element("h3", label, "tile-head");
  const media = element("div", "", "media");
  const placeholder = element("p", configured ? "正在读取…" : "未上传", "placeholder");
  media.append(placeholder);
  card.append(title, media);
  if (configured) {
    const image = element("img");
    image.alt = label + "参考图";
    image.hidden = true;
    image.onload = () => {
      placeholder.hidden = true;
      image.hidden = false;
    };
    const imageError = error => {
      placeholder.textContent = visibleError(error, "参考图暂时无法显示，请刷新。");
    };
    image.onerror = imageError;
    media.append(image);
    imageSource(target).then(source => { image.src = source; }, imageError);
  }
  if (canUpload) {
    const action = configured ? "替换" : "上传";
    const control = referenceControls({ ...target, index: configured ? target.index : null }, label, action);
    control.classList.add("tile-action");
    card.append(control);
  }
  return card;
}

function render(data) {
  const face = new Set(data.face.map(item => item.index));
  const body = data.body.map(item => item.index);
  const revisions = data.revisions;
  const canWrite = data.can_write === true;
  const nextFace = faceLabels.findIndex((_, index) => !face.has(index));
  const faceSlots = [...new Set([0, 1, 2, ...face])].sort((a, b) => a - b);
  byId("face-count").textContent = "已配置 " + face.size + " 张";
  byId("body-count").textContent = "已配置 " + body.length + " 张";
  byId("face-grid").replaceChildren(...faceSlots.map(index =>
    referenceTile({ group: "face", index, revision: revisions.face },
      "第 " + (index + 1) + " 张 · " + (faceLabels[index] || "补充参考"),
      face.has(index), canWrite && (face.has(index) || index === nextFace))
  ));
  const bodyGrid = byId("body-grid");
  bodyGrid.replaceChildren(...body.map(index =>
    referenceTile({ group: "body", index, revision: revisions.body },
      "身材参考 " + (index + 1), true, canWrite)
  ));
  bodyGrid.hidden = body.length === 0;
  byId("body-empty").hidden = body.length !== 0;
  const bodyAdd = byId("body-add");
  bodyAdd.hidden = !canWrite || body.length >= bodyLimit;
  bodyAdd.replaceChildren();
  if (!bodyAdd.hidden) {
    const label = body.length ? "添加身材参考图" : "上传身材参考图";
    bodyAdd.append(referenceControls({ group: "body", index: null, revision: revisions.body }, "", label));
  }
  const libraryError = byId("library-error");
  libraryError.textContent = data.library_error || "";
  libraryError.hidden = !libraryError.textContent;
  byId("read-only").hidden = canWrite || Boolean(data.library_error);

  configStatus.textContent = data.config_error ? "配置需要检查" :
    data.configured ? "生图服务已配置" : "待填写生图服务设置";
  options.replaceChildren(...[
    ["文生图", data.generate_enabled],
    ["改图", data.edit_enabled],
    ["自拍参考照", data.selfie_enabled],
  ].map(([label, enabled]) => element("span", label + (enabled ? " 已开启" : " 已关闭"), "tag")));
  options.hidden = false;
  references.hidden = false;
}

async function readReferences(current) {
  reading = true;
  updateControls();
  try {
    const data = await get("photo/references");
    if (current === revision) render(data);
  } catch {
    if (current === revision) configStatus.textContent = "暂时无法读取参考图，请检查 AstrBot 登录状态后刷新。";
  } finally {
    if (current === revision) { reading = false; updateControls(); }
  }
}

export function clearReferences() {
  ++revision;
  refresh = null;
  reading = false;
  configStatus.textContent = "正在读取参考图配置…";
  options.replaceChildren();
  options.hidden = true;
  references.hidden = true;
  for (const id of ["face-grid", "body-grid", "body-add"]) byId(id).replaceChildren();
  for (const id of ["face-count", "body-count", "face-upload-status", "body-upload-status", "library-error"]) byId(id).textContent = "";
  byId("library-error").hidden = true;
  byId("read-only").hidden = true;
}

export async function loadReferences(refreshButton) {
  clearReferences();
  refresh = refreshButton;
  updateControls();
  if (!changing) await readReferences(revision);
}
