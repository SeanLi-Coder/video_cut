"use strict";

const PREVIEW_DEBOUNCE_MS = 500;
const EXPORT_POLL_MS = 650;
const MODEL_DOWNLOAD_POLL_MS = 650;
const MEDIA_LOAD_TIMEOUT_MS = 20000;
const DEFAULT_MAX_FRAME_SECONDS = 5;
const AI_TARGETS = {
  "1080p": { label: "1080p", dimensions: "1920 × 1080" },
  "2k": { label: "2K QHD", dimensions: "2560 × 1440" },
  "4k": { label: "4K UHD", dimensions: "3840 × 2160" },
};
const DEFAULT_AI_MODEL_ID = "seedvr2-3b-fp16";
const FALLBACK_AI_MODELS = [
  {
    id: DEFAULT_AI_MODEL_ID,
    name: "SeedVR2 3B FP16",
    description: "质量优先的时序视频修复模型，面向 RTX 5090。",
    precision: "FP16",
    status: "stable",
    stable: true,
    default: true,
    compatible: true,
    runnable: true,
    supported_targets: ["1080p", "2k", "4k"],
    download_size_bytes: 7_284_343_622,
  },
  {
    id: "swiftvr-5b-bf16",
    name: "SwiftVR 5B BF16",
    description: "面向 RTX 5090 的实验性流式一阶段视频修复模型。",
    precision: "BF16",
    status: "experimental",
    experimental: true,
    compatible: false,
    runnable: false,
    supported_targets: ["1080p"],
    download_size_bytes: 20_167_236_623,
    compatibility_reason: "SwiftVR 仅支持 NVIDIA CUDA；正在等待本机能力信息。",
  },
];
const AVAILABLE_AI_MODEL_IDS = new Set(FALLBACK_AI_MODELS.map((model) => model.id));

const state = {
  appToken: "",
  appReady: false,
  aiFeaturesEnabled: false,
  aiFeaturesResolved: false,
  ffmpegReady: false,
  frameExportReady: false,
  rotationReady: false,
  aiEnhanceReady: false,
  aiRuntime: {},
  aiModels: FALLBACK_AI_MODELS.map((model) => ({ ...model })),
  selectedAiModelId: DEFAULT_AI_MODEL_ID,
  multiModelApiAvailable: false,
  modelCatalogLoading: true,
  workspace: "video",
  modelDownload: {},
  modelDownloadModelId: DEFAULT_AI_MODEL_ID,
  modelDownloadStarting: false,
  modelDownloadCancelling: false,
  modelDeleteModelId: "",
  modelDeleteError: "",
  modelDownloadPollTimer: null,
  modelDownloadPollSerial: 0,
  modelDownloadPollFailures: 0,
  modelDownloadRequestError: "",
  aiProxy: {
    configured: false,
    url: "",
    username: "",
    hasPassword: false,
    displayUrl: "直接连接",
  },
  aiProxyLoaded: false,
  aiProxyDirty: false,
  aiProxyBusy: "",
  aiProxyError: "",
  aiProxyTestResult: "",
  aiProxyRequestSerial: 0,
  operation: "clip",
  rotationDegrees: 90,
  enhanceTarget: "",
  maxFrameSeconds: DEFAULT_MAX_FRAME_SECONDS,
  outputDirectory: "",
  video: null,
  selectingVideo: false,
  selectingDirectory: false,
  previewTimer: null,
  previewController: null,
  previewRequestSerial: 0,
  previewReady: false,
  previewMode: "",
  previewUrl: "",
  activeRange: null,
  activePreviewKey: "",
  mediaLoading: false,
  mediaFallbackAttempted: false,
  playbackFrameId: null,
  exporting: false,
  cancellingExport: false,
  exportJobId: "",
  exportOutputName: "",
  exportPollTimer: null,
  exportPollSerial: 0,
  exportPollFailures: 0,
  exportError: "",
  exportCompleted: false,
  toastTimer: null,
};

const byId = (id) => document.getElementById(id);

const elements = {
  appName: byId("app-name"),
  versionLabel: byId("version-label"),
  systemBanner: byId("system-banner"),
  systemBannerTitle: byId("system-banner-title"),
  systemBannerMessage: byId("system-banner-message"),
  reloadButton: byId("reload-button"),
  workspaceTabs: byId("workspace-tabs"),
  videoWorkspaceTab: byId("video-workspace-tab"),
  modelWorkspaceTab: byId("model-workspace-tab"),
  videoWorkspace: byId("video-workspace"),
  modelWorkspace: byId("model-workspace"),
  modeClipButton: byId("mode-clip-button"),
  modeFramesButton: byId("mode-frames-button"),
  modeRotateButton: byId("mode-rotate-button"),
  modeEnhanceButton: byId("mode-enhance-button"),
  modeSwitch: byId("mode-switch"),
  pageTitle: byId("page-title"),
  heroCopy: byId("hero-copy"),
  journeySource: byId("journey-source"),
  journeyTrim: byId("journey-trim"),
  journeyExport: byId("journey-export"),
  journeyRangeLabel: byId("journey-range-label"),
  journeyExportLabel: byId("journey-export-label"),
  sourceCard: byId("source-card"),
  trimCard: byId("trim-card"),
  exportCard: byId("export-card"),
  sourceEmpty: byId("source-empty"),
  selectedFile: byId("selected-file"),
  selectVideoButton: byId("select-video-button"),
  replaceVideoButton: byId("replace-video-button"),
  videoName: byId("video-name"),
  videoPath: byId("video-path"),
  videoDuration: byId("video-duration"),
  videoResolution: byId("video-resolution"),
  videoFps: byId("video-fps"),
  videoCodecs: byId("video-codecs"),
  trimHeading: byId("trim-heading"),
  trimDescription: byId("trim-description"),
  durationLabel: byId("duration-label"),
  timePanel: byId("time-panel"),
  rotationPanel: byId("rotation-panel"),
  rotationOptions: Array.from(document.querySelectorAll(".rotation-option")),
  rotationNote: byId("rotation-note"),
  aiPanel: byId("ai-panel"),
  aiTargetOptions: Array.from(document.querySelectorAll(".ai-target-option")),
  aiTargetNote: byId("ai-target-note"),
  aiModelOptions: byId("ai-model-options"),
  aiModelNote: byId("ai-model-note"),
  activeAiModelBadge: byId("active-ai-model-badge"),
  aiRuntimeInlineNote: byId("ai-runtime-inline-note"),
  openModelManagerButton: byId("open-model-manager-button"),
  aiProxyForm: byId("ai-proxy-form"),
  aiProxyUrl: byId("ai-proxy-url"),
  aiProxyUsername: byId("ai-proxy-username"),
  aiProxyPassword: byId("ai-proxy-password"),
  aiProxyClearPassword: byId("ai-proxy-clear-password"),
  aiProxyStatusBadge: byId("ai-proxy-status-badge"),
  aiProxyCurrent: byId("ai-proxy-current"),
  aiProxyTestResult: byId("ai-proxy-test-result"),
  aiProxyNextTaskNote: byId("ai-proxy-next-task-note"),
  aiProxyError: byId("ai-proxy-error"),
  aiProxyTestButton: byId("ai-proxy-test-button"),
  aiProxyClearButton: byId("ai-proxy-clear-button"),
  aiProxySaveButton: byId("ai-proxy-save-button"),
  modelSelector: byId("model-selector"),
  modelKicker: byId("model-kicker"),
  modelManagerHeading: byId("model-manager-heading"),
  modelStatusBadge: byId("model-status-badge"),
  modelSizeValue: byId("model-size-value"),
  modelPrecisionValue: byId("model-precision-value"),
  modelTargetsValue: byId("model-targets-value"),
  modelBackendDescription: byId("model-backend-description"),
  modelCompatibilityNote: byId("model-compatibility-note"),
  modelDeviceValue: byId("model-device-value"),
  modelSpaceValue: byId("model-space-value"),
  runtimeComponentDot: byId("runtime-component-dot"),
  runtimeComponentStatus: byId("runtime-component-status"),
  weightsComponentDot: byId("weights-component-dot"),
  weightsComponentStatus: byId("weights-component-status"),
  modelProgressPanel: byId("model-progress-panel"),
  modelProgressKicker: byId("model-progress-kicker"),
  modelProgressTitle: byId("model-progress-title"),
  modelProgressValue: byId("model-progress-value"),
  modelProgressTrack: byId("model-progress-track"),
  modelProgressBar: byId("model-progress-bar"),
  modelDownloadBytes: byId("model-download-bytes"),
  modelDownloadSpeed: byId("model-download-speed"),
  modelDownloadElapsed: byId("model-download-elapsed"),
  modelDownloadRemaining: byId("model-download-remaining"),
  modelProgressMessage: byId("model-progress-message"),
  modelActionNote: byId("model-action-note"),
  modelDownloadButton: byId("model-download-button"),
  cancelModelDownloadButton: byId("cancel-model-download-button"),
  modelDeleteButton: byId("model-delete-button"),
  modelDeleteHint: byId("model-delete-hint"),
  modelDeleteError: byId("model-delete-error"),
  frameLimitHint: byId("frame-limit-hint"),
  startTime: byId("start-time"),
  endTime: byId("end-time"),
  startTimeError: byId("start-time-error"),
  endTimeError: byId("end-time-error"),
  clipDuration: byId("clip-duration"),
  rangeReadout: byId("range-readout"),
  rangeTotal: byId("range-total"),
  previewPanel: byId("preview-panel"),
  previewFrame: byId("preview-frame"),
  previewVideo: byId("preview-video"),
  previewPlaceholder: byId("preview-placeholder"),
  previewLoading: byId("preview-loading"),
  previewLoadingTitle: byId("preview-loading-title"),
  previewStatusDot: byId("preview-status-dot"),
  previewStatusText: byId("preview-status-text"),
  retryPreviewButton: byId("retry-preview-button"),
  destinationLabel: byId("destination-label"),
  outputDirectory: byId("output-directory"),
  selectDirectoryButton: byId("select-directory-button"),
  suggestedOutputName: byId("suggested-output-name"),
  exportHeading: byId("export-heading"),
  exportDescription: byId("export-description"),
  qualityChip: byId("quality-chip"),
  qualityChipText: byId("quality-chip-text"),
  outputNameLabel: byId("output-name-label"),
  outputNameNote: byId("output-name-note"),
  readyNote: byId("ready-note"),
  readyNoteText: byId("ready-note-text"),
  exportButton: byId("export-button"),
  exportSetup: byId("export-setup"),
  exportProgressPanel: byId("export-progress-panel"),
  exportProgressKicker: byId("export-progress-kicker"),
  exportProgressTitle: byId("export-progress-title"),
  exportProgressValue: byId("export-progress-value"),
  exportProgressTrack: byId("export-progress-track"),
  exportProgressBar: byId("export-progress-bar"),
  exportProgressMessage: byId("export-progress-message"),
  exportElapsed: byId("export-elapsed"),
  exportRemaining: byId("export-remaining"),
  cancelExportButton: byId("cancel-export-button"),
  exportCompletePanel: byId("export-complete-panel"),
  completedOutputName: byId("completed-output-name"),
  completedOutputPath: byId("completed-output-path"),
  completionSummary: byId("completion-summary"),
  revealOutputButton: byId("reveal-output-button"),
  continueButton: byId("continue-button"),
  toast: byId("toast"),
  toastMessage: byId("toast-message"),
};

class ApiError extends Error {
  constructor(message, status = 0, payload = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }
}

async function apiRequest(path, options = {}) {
  const method = options.method || "GET";
  const headers = { Accept: "application/json" };

  if (state.appToken) {
    headers["X-App-Token"] = state.appToken;
  }

  const request = {
    method,
    headers,
    cache: "no-store",
    signal: options.signal,
  };

  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    request.body = JSON.stringify(options.body);
  }

  let response;
  try {
    response = await fetch(path, request);
  } catch (error) {
    if (error && error.name === "AbortError") {
      throw error;
    }
    throw new ApiError("无法连接本地服务，请确认启动窗口仍在运行。", 0);
  }

  let payload = null;
  const contentType = response.headers.get("content-type") || "";
  try {
    payload = contentType.includes("application/json") ? await response.json() : await response.text();
  } catch (_error) {
    payload = null;
  }

  if (!response.ok) {
    let message = extractErrorMessage(payload) || `操作失败（${response.status}）`;
    if (response.status === 401 || response.status === 403) {
      message = "本地服务已重启，请刷新页面后继续。";
    }
    throw new ApiError(message, response.status, payload);
  }

  return payload || {};
}

function extractErrorMessage(payload) {
  if (!payload) return "";
  if (typeof payload === "string") return payload.trim();
  if (typeof payload.error === "string") return payload.error;
  if (typeof payload.message === "string") return payload.message;
  if (typeof payload.detail === "string") return payload.detail;
  if (Array.isArray(payload.detail)) {
    return payload.detail
      .map((item) => (typeof item === "string" ? item : item && item.msg))
      .filter(Boolean)
      .join("；");
  }
  if (payload.detail && typeof payload.detail.message === "string") {
    return payload.detail.message;
  }
  return "";
}

function post(path, body, signal) {
  const options = { method: "POST", signal };
  if (body !== undefined) options.body = body;
  return apiRequest(path, options);
}

function normalizeDirectory(value) {
  if (typeof value === "string") return value;
  if (!value || typeof value !== "object") return "";
  return value.path_display || value.path || value.directory || "";
}

function setButtonBusy(button, busy, busyLabel) {
  if (!button) return;
  const label = button.querySelector(".button-label");
  if (label && busy) {
    if (!button.dataset.idleLabel) button.dataset.idleLabel = label.textContent;
    label.textContent = busyLabel || "处理中…";
  } else if (label && button.dataset.idleLabel) {
    label.textContent = button.dataset.idleLabel;
  }
  button.classList.toggle("is-loading", busy);
  button.setAttribute("aria-busy", busy ? "true" : "false");
}

function showSystemBanner(title, message, type = "warning") {
  elements.systemBannerTitle.textContent = title;
  elements.systemBannerMessage.textContent = message;
  elements.systemBanner.classList.toggle("is-error", type === "error");
  elements.systemBanner.hidden = false;
}

function hideSystemBanner() {
  elements.systemBanner.hidden = true;
}

function validateAiProxyUrl(value) {
  const rawUrl = String(value || "").trim();
  if (!rawUrl) {
    return { valid: false, url: "", message: "请输入代理地址。" };
  }

  let parsed;
  try {
    parsed = new URL(rawUrl);
  } catch (_error) {
    return { valid: false, url: "", message: "代理地址格式不正确，请包含 http://、https:// 或 socks5://。" };
  }

  if (!["http:", "https:", "socks5:", "socks5h:"].includes(parsed.protocol.toLowerCase())) {
    return { valid: false, url: "", message: "代理仅支持 HTTP、HTTPS 或 SOCKS5。" };
  }
  if (!parsed.hostname) {
    return { valid: false, url: "", message: "代理地址缺少主机名。" };
  }
  if (parsed.username || parsed.password) {
    return { valid: false, url: "", message: "请把代理用户名和密码填写在单独的输入框中。" };
  }
  if ((parsed.pathname && parsed.pathname !== "/") || parsed.search || parsed.hash) {
    return { valid: false, url: "", message: "代理地址只需填写协议、主机和端口。" };
  }

  const authority = rawUrl.slice(rawUrl.indexOf("://") + 3).replace(/\/$/, "");
  const explicitPort = authority.match(/:(\d+)$/)?.[1] || "";
  const port = Number(explicitPort);
  if (!explicitPort || !Number.isInteger(port) || port < 1 || port > 65535) {
    return { valid: false, url: "", message: "代理地址必须包含 1 到 65535 的端口，例如 :7890。" };
  }

  const bareHostname = parsed.hostname.startsWith("[") && parsed.hostname.endsWith("]")
    ? parsed.hostname.slice(1, -1)
    : parsed.hostname;
  const hostname = bareHostname.includes(":") ? `[${bareHostname}]` : bareHostname;

  return {
    valid: true,
    url: `${parsed.protocol.toLowerCase()}//${hostname.toLowerCase()}:${port}`,
    message: "",
  };
}

function normalizeAiProxySettings(payload) {
  const value = payload && typeof payload === "object" ? payload : {};
  const configured = value.configured === true;
  const checkedUrl = validateAiProxyUrl(value.url);
  const url = configured && checkedUrl.valid ? checkedUrl.url : "";
  const username = configured && typeof value.username === "string" ? value.username : "";
  let displayUrl = "直接连接";
  if (configured && url) {
    const characters = Array.from(username);
    const maskedUsername = characters.length ? `${characters.slice(0, Math.min(2, characters.length)).join("")}***` : "";
    const passwordMarker = value.has_password === true ? ":••••" : "";
    const authorityStart = url.indexOf("://") + 3;
    displayUrl = `${url.slice(0, authorityStart)}${maskedUsername ? `${maskedUsername}${passwordMarker}@` : ""}${url.slice(authorityStart)}`;
  } else if (configured) {
    displayUrl = "已配置（地址已隐藏）";
  }
  return {
    configured,
    url,
    username,
    hasPassword: configured && value.has_password === true,
    displayUrl,
  };
}

function populateAiProxyForm() {
  elements.aiProxyUrl.value = state.aiProxy.url;
  elements.aiProxyUsername.value = state.aiProxy.username;
  elements.aiProxyPassword.value = "";
  elements.aiProxyClearPassword.checked = false;
  elements.aiProxyUrl.setAttribute("aria-invalid", "false");
}

function readAiProxyDraft() {
  return {
    url: elements.aiProxyUrl.value.trim(),
    username: elements.aiProxyUsername.value.trim(),
    password: elements.aiProxyPassword.value,
  };
}

function aiProxyDraftMatchesSaved(draft, normalizedUrl = "") {
  const checkedUrl = normalizedUrl
    ? { valid: true, url: normalizedUrl }
    : validateAiProxyUrl(draft.url);
  return state.aiProxy.configured
    && checkedUrl.valid
    && checkedUrl.url === state.aiProxy.url
    && draft.username === state.aiProxy.username;
}

function aiProxyPasswordAction(draft, normalizedUrl) {
  const sameProxy = aiProxyDraftMatchesSaved(draft, normalizedUrl);
  if (sameProxy && elements.aiProxyClearPassword.checked) return "clear";
  if (draft.password) return "replace";
  if (sameProxy && state.aiProxy.hasPassword) return "keep";
  return "clear";
}

function buildAiProxyRequestBody() {
  const draft = readAiProxyDraft();
  const checkedUrl = validateAiProxyUrl(draft.url);
  elements.aiProxyUrl.setAttribute("aria-invalid", checkedUrl.valid ? "false" : "true");
  if (!checkedUrl.valid) {
    return { body: null, error: checkedUrl.message };
  }
  return {
    body: {
      url: checkedUrl.url,
      username: draft.username,
      password: draft.password,
      password_action: aiProxyPasswordAction(draft, checkedUrl.url),
    },
    error: "",
  };
}

function renderAiProxySettings() {
  const busy = state.aiProxyBusy;
  const controlsDisabled = !aiFeaturesAvailable() || !state.appReady || !state.aiProxyLoaded || Boolean(busy);
  const draft = readAiProxyDraft();
  const sameProxy = aiProxyDraftMatchesSaved(draft);
  const canClearPassword = state.aiProxy.hasPassword && sameProxy;
  const clearPasswordActive = canClearPassword && elements.aiProxyClearPassword.checked;

  elements.aiProxyUrl.disabled = controlsDisabled;
  elements.aiProxyUsername.disabled = controlsDisabled;
  elements.aiProxyPassword.disabled = controlsDisabled || clearPasswordActive;
  elements.aiProxyClearPassword.disabled = controlsDisabled || !canClearPassword;
  elements.aiProxyTestButton.disabled = controlsDisabled;
  elements.aiProxySaveButton.disabled = controlsDisabled;
  elements.aiProxyClearButton.disabled = controlsDisabled || !state.aiProxy.configured;
  setButtonBusy(elements.aiProxyTestButton, busy === "test", "测试中…");
  setButtonBusy(elements.aiProxySaveButton, busy === "save", "保存中…");
  setButtonBusy(elements.aiProxyClearButton, busy === "clear", "清除中…");

  let badgeStatus = "idle";
  let badgeText = "直接连接";
  if (busy === "load") [badgeStatus, badgeText] = ["running", "正在读取"];
  else if (busy === "test") [badgeStatus, badgeText] = ["running", "正在测试"];
  else if (busy) [badgeStatus, badgeText] = ["running", "正在保存"];
  else if (!state.aiProxyLoaded && state.aiProxyError) [badgeStatus, badgeText] = ["failed", "读取失败"];
  else if (!state.aiProxyLoaded) badgeText = "等待服务";
  else if (state.aiProxyDirty) [badgeStatus, badgeText] = ["dirty", "尚未保存"];
  else if (state.aiProxy.configured) [badgeStatus, badgeText] = ["ready", "已启用"];
  elements.aiProxyStatusBadge.dataset.status = badgeStatus;
  elements.aiProxyStatusBadge.textContent = badgeText;

  elements.aiProxyCurrent.textContent = state.aiProxyLoaded
    ? state.aiProxy.configured
      ? `当前：${state.aiProxy.displayUrl}${state.aiProxy.hasPassword ? " · 密码已保存且不会回显" : ""}`
      : "当前：直接连接（未使用代理）"
    : state.aiProxyError
      ? "无法读取当前代理设置，请刷新页面后重试。"
      : "正在读取当前设置…";
  elements.aiProxyError.textContent = state.aiProxyError;
  elements.aiProxyError.hidden = !state.aiProxyError;
  elements.aiProxyTestResult.textContent = state.aiProxyTestResult;
  elements.aiProxyTestResult.hidden = !state.aiProxyTestResult;
  elements.aiProxyNextTaskNote.textContent = modelDownloadIsActive()
    ? "当前模型任务会继续使用启动时的连接；新设置从下一次新开始或重试生效。"
    : "保存或清除后只影响下一次新开始或重试的模型下载。";
}

function markAiProxyDraftChanged(event) {
  if (event.currentTarget === elements.aiProxyPassword && elements.aiProxyPassword.value) {
    elements.aiProxyClearPassword.checked = false;
  }
  if (
    [elements.aiProxyUrl, elements.aiProxyUsername].includes(event.currentTarget)
    && elements.aiProxyClearPassword.checked
  ) {
    elements.aiProxyClearPassword.checked = false;
  }
  state.aiProxyDirty = true;
  state.aiProxyError = "";
  state.aiProxyTestResult = "";
  elements.aiProxyUrl.setAttribute("aria-invalid", "false");
  renderAiProxySettings();
}

function handleAiProxyClearPasswordChange() {
  if (elements.aiProxyClearPassword.checked) elements.aiProxyPassword.value = "";
  state.aiProxyDirty = true;
  state.aiProxyError = "";
  state.aiProxyTestResult = "";
  renderAiProxySettings();
}

function showAiProxyRequestError(error, fallbackMessage) {
  state.aiProxyError = error?.message || fallbackMessage;
  state.aiProxyTestResult = "";
  showToast(state.aiProxyError, "error");
}

async function refreshAiProxySettings() {
  if (!aiFeaturesAvailable() || !state.appReady) {
    renderAiProxySettings();
    return;
  }
  const requestSerial = ++state.aiProxyRequestSerial;
  state.aiProxyBusy = "load";
  state.aiProxyError = "";
  renderAiProxySettings();
  try {
    const payload = await apiRequest("/api/ai-download-proxy");
    if (requestSerial !== state.aiProxyRequestSerial) return;
    state.aiProxy = normalizeAiProxySettings(payload);
    state.aiProxyLoaded = true;
    state.aiProxyDirty = false;
    state.aiProxyTestResult = "";
    populateAiProxyForm();
  } catch (error) {
    if (requestSerial !== state.aiProxyRequestSerial) return;
    state.aiProxyLoaded = false;
    state.aiProxyError = error?.message || "无法读取 AI 模型下载代理设置。";
  } finally {
    if (requestSerial === state.aiProxyRequestSerial) {
      state.aiProxyBusy = "";
      renderAiProxySettings();
    }
  }
}

async function saveAiProxySettings(event) {
  event.preventDefault();
  if (!aiFeaturesAvailable() || !state.appReady || !state.aiProxyLoaded || state.aiProxyBusy) return;
  const request = buildAiProxyRequestBody();
  if (!request.body) {
    state.aiProxyError = request.error;
    state.aiProxyTestResult = "";
    renderAiProxySettings();
    elements.aiProxyUrl.focus();
    return;
  }

  const requestSerial = ++state.aiProxyRequestSerial;
  state.aiProxyBusy = "save";
  state.aiProxyError = "";
  state.aiProxyTestResult = "";
  renderAiProxySettings();
  try {
    const payload = await apiRequest("/api/ai-download-proxy", {
      method: "PUT",
      body: request.body,
    });
    if (requestSerial !== state.aiProxyRequestSerial) return;
    state.aiProxy = normalizeAiProxySettings(payload);
    state.aiProxyLoaded = true;
    state.aiProxyDirty = false;
    populateAiProxyForm();
    showToast(modelDownloadIsActive()
      ? "代理已保存；当前任务不变，下次模型任务生效"
      : "代理已保存，将用于下一次模型下载");
  } catch (error) {
    if (requestSerial !== state.aiProxyRequestSerial) return;
    showAiProxyRequestError(error, "无法保存 AI 模型下载代理。");
  } finally {
    if (requestSerial === state.aiProxyRequestSerial) {
      state.aiProxyBusy = "";
      renderAiProxySettings();
    }
  }
}

async function clearAiProxySettings() {
  if (!aiFeaturesAvailable() || !state.appReady || !state.aiProxyLoaded || state.aiProxyBusy || !state.aiProxy.configured) return;
  const requestSerial = ++state.aiProxyRequestSerial;
  state.aiProxyBusy = "clear";
  state.aiProxyError = "";
  state.aiProxyTestResult = "";
  renderAiProxySettings();
  try {
    const payload = await apiRequest("/api/ai-download-proxy", { method: "DELETE" });
    if (requestSerial !== state.aiProxyRequestSerial) return;
    state.aiProxy = normalizeAiProxySettings(payload);
    state.aiProxyLoaded = true;
    state.aiProxyDirty = false;
    populateAiProxyForm();
    showToast(modelDownloadIsActive()
      ? "代理已清除；当前任务不变，下次模型任务将直接连接"
      : "代理已清除，下次模型下载将直接连接");
  } catch (error) {
    if (requestSerial !== state.aiProxyRequestSerial) return;
    showAiProxyRequestError(error, "无法清除 AI 模型下载代理。");
  } finally {
    if (requestSerial === state.aiProxyRequestSerial) {
      state.aiProxyBusy = "";
      renderAiProxySettings();
    }
  }
}

async function testAiProxyDraft() {
  if (!aiFeaturesAvailable() || !state.appReady || !state.aiProxyLoaded || state.aiProxyBusy) return;
  const request = buildAiProxyRequestBody();
  if (!request.body) {
    state.aiProxyError = request.error;
    state.aiProxyTestResult = "";
    renderAiProxySettings();
    elements.aiProxyUrl.focus();
    return;
  }

  const requestSerial = ++state.aiProxyRequestSerial;
  state.aiProxyBusy = "test";
  state.aiProxyError = "";
  state.aiProxyTestResult = "";
  renderAiProxySettings();
  try {
    const payload = await post("/api/ai-download-proxy/test", request.body);
    if (requestSerial !== state.aiProxyRequestSerial) return;
    if (payload.test_success === false) {
      throw new ApiError(payload.test_message || "代理连接测试失败。", 502, payload);
    }
    const latency = Number(payload.latency_ms);
    const latencyText = Number.isFinite(latency) && latency >= 0 ? ` · ${Math.round(latency)} ms` : "";
    state.aiProxyTestResult = `${payload.test_message || payload.message || "代理连接测试成功"}${latencyText}`;
  } catch (error) {
    if (requestSerial !== state.aiProxyRequestSerial) return;
    showAiProxyRequestError(error, "代理连接测试失败。");
  } finally {
    if (requestSerial === state.aiProxyRequestSerial) {
      state.aiProxyBusy = "";
      renderAiProxySettings();
    }
  }
}

function showToast(message, type = "success") {
  window.clearTimeout(state.toastTimer);
  elements.toastMessage.textContent = message;
  elements.toast.classList.toggle("is-error", type === "error");
  elements.toast.hidden = false;
  state.toastTimer = window.setTimeout(() => {
    elements.toast.hidden = true;
  }, 3600);
}

function parseTime(value) {
  if (typeof value === "number") {
    return Number.isFinite(value) && value >= 0 ? value : null;
  }
  if (typeof value !== "string") return null;

  const normalized = value
    .trim()
    .replaceAll("：", ":")
    .replace(/[，,]/g, ".");
  if (!normalized) return null;

  if (/^\d+(?:\.\d{1,3})?$/.test(normalized)) {
    const seconds = Number(normalized);
    return Number.isFinite(seconds) ? seconds : null;
  }

  const parts = normalized.split(":");
  if (parts.length !== 2 && parts.length !== 3) return null;
  if (!parts.every((part) => /^\d+(?:\.\d{1,3})?$/.test(part))) return null;

  const secondsPart = Number(parts.at(-1));
  if (!Number.isFinite(secondsPart) || secondsPart >= 60) return null;

  if (parts.length === 2) {
    const minutes = Number(parts[0]);
    if (!Number.isInteger(minutes)) return null;
    return minutes * 60 + secondsPart;
  }

  const hours = Number(parts[0]);
  const minutes = Number(parts[1]);
  if (!Number.isInteger(hours) || !Number.isInteger(minutes) || minutes >= 60) return null;
  return hours * 3600 + minutes * 60 + secondsPart;
}

function formatTime(value, includeMilliseconds = true) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || numeric < 0) return "—";

  const totalMilliseconds = Math.max(0, Math.round(numeric * 1000));
  const milliseconds = totalMilliseconds % 1000;
  const totalSeconds = Math.floor(totalMilliseconds / 1000);
  const seconds = totalSeconds % 60;
  const totalMinutes = Math.floor(totalSeconds / 60);
  const minutes = totalMinutes % 60;
  const hours = Math.floor(totalMinutes / 60);
  const base = `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  return includeMilliseconds ? `${base}.${String(milliseconds).padStart(3, "0")}` : base;
}

function formatShortTime(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || numeric < 0) return "—";
  const rounded = Math.floor(numeric);
  const hours = Math.floor(rounded / 3600);
  const minutes = Math.floor((rounded % 3600) / 60);
  const seconds = rounded % 60;
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`
    : `${minutes}:${String(seconds).padStart(2, "0")}`;
}

function formatElapsed(value) {
  const seconds = Math.max(0, Math.floor(Number(value) || 0));
  if (seconds < 60) return `已用 ${seconds} 秒`;
  return `已用 ${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

function formatRemainingTime(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || numeric < 0) return "预计还需：正在估算…";
  const seconds = Math.ceil(numeric);
  if (seconds <= 10) return "预计还需：即将完成";
  if (seconds < 60) {
    return `预计还需：约 ${Math.ceil(seconds / 5) * 5} 秒`;
  }
  if (seconds < 3600) {
    return `预计还需：约 ${Math.ceil(seconds / 60)} 分钟`;
  }
  if (seconds < 86400) {
    const totalMinutes = Math.ceil(seconds / 300) * 5;
    const hours = Math.floor(totalMinutes / 60);
    const minutes = totalMinutes % 60;
    return minutes > 0
      ? `预计还需：约 ${hours} 小时 ${minutes} 分钟`
      : `预计还需：约 ${hours} 小时`;
  }
  const totalHours = Math.ceil(seconds / 3600);
  const days = Math.floor(totalHours / 24);
  const hours = totalHours % 24;
  return hours > 0
    ? `预计还需：约 ${days} 天 ${hours} 小时`
    : `预计还需：约 ${days} 天`;
}

function remainingTimeText(snapshot, { cancelling = false } = {}) {
  if (cancelling) return "预计还需：正在停止…";
  const status = String(snapshot?.status || "").toLowerCase();
  const stage = String(snapshot?.stage || snapshot?.phase || status).toLowerCase();
  const active = ["queued", "pending", "waiting", "running", "processing", "exporting"].includes(status);
  if (!active) return "";
  if (["verify", "verifying", "remux", "finalize", "finalizing"].includes(stage)
    || normalizeProgress(snapshot?.progress) >= 99) {
    return "预计还需：正在收尾…";
  }
  const rawEstimate = snapshot?.estimated_remaining_seconds;
  if (rawEstimate !== null && rawEstimate !== undefined && rawEstimate !== "") {
    const estimate = Number(rawEstimate);
    if (Number.isFinite(estimate) && estimate >= 0) return formatRemainingTime(estimate);
  }
  return "预计还需：正在估算…";
}

function normalizeCodec(codec) {
  if (!codec) return "—";
  return String(codec).replaceAll("_", " ").toUpperCase();
}

function isFrameMode() {
  return state.operation === "frames";
}

function isRotateMode() {
  return state.operation === "rotate";
}

function aiFeaturesAvailable() {
  return state.aiFeaturesResolved && state.aiFeaturesEnabled;
}

function isEnhanceMode() {
  return aiFeaturesAvailable() && state.operation === "enhance";
}

function usesSourceDirectory() {
  return isRotateMode() || isEnhanceMode();
}

function normalizeAiModel(value, fallback = {}) {
  const raw = value && typeof value === "object" ? { ...fallback, ...value } : { ...fallback };
  const id = String(raw.id || "").trim().toLowerCase();
  if (!id) return null;
  const status = raw.blocked === true
    ? "blocked"
    : raw.experimental === true
      ? "experimental"
      : String(raw.status || "stable").trim().toLowerCase();
  const supportedTargets = Array.from(new Set(
    (Array.isArray(raw.supported_targets) ? raw.supported_targets : [])
      .map((target) => String(target).toLowerCase())
      .filter((target) => Boolean(AI_TARGETS[target])),
  ));
  const blocked = status === "blocked" || raw.blocked === true;
  const compatible = raw.compatible === true && !blocked;
  return {
    ...raw,
    id,
    name: String(raw.name || id),
    description: String(raw.description || "本地视频增强模型。"),
    precision: String(raw.precision || "—"),
    status,
    stable: status === "stable",
    experimental: status === "experimental",
    blocked,
    compatible,
    runnable: compatible && raw.runnable !== false,
    supported_targets: supportedTargets,
    download_size_bytes: Math.max(
      0,
      Number(raw.download_size_bytes ?? raw.total_download_bytes) || 0,
    ),
    partial_bytes: Math.max(0, Number(raw.partial_bytes) || 0),
    downloaded: typeof raw.downloaded === "boolean" ? raw.downloaded : null,
    prepared: typeof raw.prepared === "boolean" ? raw.prepared : null,
    offline_managed: raw.offline_managed === true || raw.runtime?.offline_managed === true,
    compatibility_reason: String(
      raw.blocked_reason || raw.compatibility_reason || raw.unavailable_reason || "",
    ),
    runtime: raw.runtime && typeof raw.runtime === "object" ? raw.runtime : null,
  };
}

function applyAiModelCatalog(payload) {
  const received = (Array.isArray(payload?.models) ? payload.models : [])
    .filter((model) => AVAILABLE_AI_MODEL_IDS.has(String(model?.id || "").toLowerCase()));
  const byId = new Map(received.map((model) => [String(model?.id || "").toLowerCase(), model]));
  const ordered = [];
  for (const fallback of FALLBACK_AI_MODELS) {
    ordered.push(normalizeAiModel(byId.get(fallback.id), fallback));
    byId.delete(fallback.id);
  }
  for (const model of byId.values()) ordered.push(normalizeAiModel(model));
  state.aiModels = ordered.filter(Boolean);
  const requestedDefault = String(payload?.default_model_id || "").toLowerCase();
  const defaultModel = state.aiModels.find((model) => model.id === requestedDefault)
    || state.aiModels.find((model) => model.default)
    || state.aiModels.find((model) => model.id === DEFAULT_AI_MODEL_ID)
    || state.aiModels[0];
  if (!state.aiModels.some((model) => model.id === state.selectedAiModelId)) {
    state.selectedAiModelId = defaultModel?.id || DEFAULT_AI_MODEL_ID;
  }
  state.modelDownloadModelId = state.selectedAiModelId;
}

function selectedAiModel() {
  return state.aiModels.find((model) => model.id === state.selectedAiModelId)
    || state.aiModels.find((model) => model.default)
    || state.aiModels[0]
    || null;
}

function selectedAiRuntime() {
  const runtime = selectedAiModel()?.runtime;
  return runtime && typeof runtime === "object" ? runtime : state.aiRuntime;
}

function aiModelStatusLabel(model = selectedAiModel()) {
  if (!model) return "未知";
  if (model.blocked) return "暂不可用";
  if (model.experimental) return "实验";
  return "稳定";
}

function aiModelTargetsLabel(model = selectedAiModel()) {
  if (!model?.supported_targets?.length) return "暂无";
  return model.supported_targets.map((target) => AI_TARGETS[target]?.label || target).join(" · ");
}

function aiModelCompatibilityReason(model = selectedAiModel()) {
  if (!model) return "尚未选择 AI 模型。";
  if (model.compatibility_reason) return model.compatibility_reason;
  if (model.blocked) return "该模型尚未通过当前平台的兼容性与质量验证。";
  if (!model.compatible || !model.runnable) return `${model.name} 与当前 AI 加速设备不兼容。`;
  return "";
}

function aiModelCanRun(model = selectedAiModel()) {
  if (!model || model.blocked || !model.compatible || !model.runnable) return false;
  const runtime = model.runtime;
  return runtime && typeof runtime.ready === "boolean"
    ? runtime.ready
    : state.aiEnhanceReady;
}

function aiModelReadyToEnhance(model = selectedAiModel()) {
  if (!aiModelCanRun(model)) return false;
  if (model?.id === DEFAULT_AI_MODEL_ID) return true;
  return modelRuntimePrepared(model.runtime || selectedAiRuntime(), model);
}

function aiModelPreparationReason(model = selectedAiModel()) {
  if (!model || !aiModelCanRun(model) || aiModelReadyToEnhance(model)) return "";
  return `请先到 AI 模型管理下载并准备 ${model.name || "所选模型"}`;
}

function selectedModelSupportsTarget(target) {
  return Boolean(selectedAiModel()?.supported_targets?.includes(String(target || "").toLowerCase()));
}

function normalizeAiTarget(value) {
  if (typeof value === "string") {
    const target = value.toLowerCase();
    return AI_TARGETS[target] ? { target, available: true, reason: "" } : null;
  }
  if (!value || typeof value !== "object") return null;
  const target = String(value.target || value.id || value.name || "").toLowerCase();
  if (!AI_TARGETS[target]) return null;
  const explicitlyUnavailable = value.available === false || value.enabled === false || value.supported === false;
  return {
    target,
    available: !explicitlyUnavailable,
    reason: String(value.reason || value.message || value.unavailable_reason || ""),
    warning: String(value.warning || ""),
    width: Number(value.width) || null,
    height: Number(value.height) || null,
    suggestedOutputName: String(value.suggested_output_name || ""),
  };
}

function videoAiTargets() {
  if (!state.video) return [];
  const configured = state.video.ai_targets;
  const values = Array.isArray(configured)
    ? configured
    : configured && typeof configured === "object"
      ? Object.entries(configured).map(([target, value]) => (
        value && typeof value === "object"
          ? { ...value, target }
          : { target, available: Boolean(value) }
      ))
      : [];
  const seen = new Set();
  return values.map(normalizeAiTarget).filter((item) => {
    if (!item || seen.has(item.target)) return false;
    seen.add(item.target);
    return true;
  });
}

function selectedAiTarget() {
  return videoAiTargets().find((item) => item.target === state.enhanceTarget) || null;
}

function aiSelectionReady() {
  return Boolean(
    !isEnhanceMode()
      || (aiModelReadyToEnhance() && selectedModelSupportsTarget(state.enhanceTarget) && selectedAiTarget()?.available),
  );
}

function selectDefaultAiTarget() {
  const targets = videoAiTargets();
  const current = targets.find((item) => (
    item.target === state.enhanceTarget
    && item.available
    && selectedModelSupportsTarget(item.target)
  ));
  state.enhanceTarget = current
    ? current.target
    : (targets.find((item) => item.available && selectedModelSupportsTarget(item.target))?.target || "");
}

function aiTargetLabel(target = state.enhanceTarget) {
  return AI_TARGETS[target]?.label || "AI 超清";
}

function operationLabel() {
  if (isFrameMode()) return "截图";
  if (isRotateMode()) return "旋转";
  if (isEnhanceMode()) return "AI 超清";
  return "导出";
}

function operationVerb() {
  if (isFrameMode()) return "截图";
  if (isRotateMode()) return "旋转";
  if (isEnhanceMode()) return "增强";
  return "导出";
}

function previewKey(range) {
  if (!range || !state.video) return "";
  const option = isRotateMode()
    ? `:${state.rotationDegrees}`
    : isEnhanceMode()
      ? `:${state.selectedAiModelId}:${state.enhanceTarget}`
      : "";
  return `${state.video.id}:${state.operation}${option}:${range.start.toFixed(3)}:${range.end.toFixed(3)}`;
}

function rangeKey(range) {
  return range ? `${range.start.toFixed(3)}:${range.end.toFixed(3)}` : "";
}

function setDefaultFrameEnd(startValue = elements.startTime.value) {
  if (!isFrameMode() || !state.video) return false;

  const start = parseTime(startValue);
  const duration = Number(state.video.duration);
  if (start === null || !Number.isFinite(duration) || duration <= 0 || start > duration) {
    return false;
  }

  elements.endTime.value = formatTime(Math.min(start + state.maxFrameSeconds, duration));
  return true;
}

function readRange(showErrors = true) {
  const result = {
    valid: false,
    start: null,
    end: null,
    startError: "",
    endError: "",
  };

  if (!state.video) {
    if (showErrors) renderFieldErrors(result);
    return result;
  }

  if (isRotateMode() || isEnhanceMode()) {
    const duration = Number(state.video.duration);
    result.start = 0;
    result.end = duration;
    result.valid = Number.isFinite(duration) && duration > 0;
    if (showErrors) renderFieldErrors(result);
    return result;
  }

  const startText = elements.startTime.value.trim();
  const endText = elements.endTime.value.trim();
  result.start = parseTime(startText);
  result.end = parseTime(endText);

  if (!startText) result.startError = "请输入起始时间";
  else if (result.start === null) result.startError = "时间格式无法识别";

  if (!endText) result.endError = "请输入结束时间";
  else if (result.end === null) result.endError = "时间格式无法识别";

  const duration = Number(state.video.duration);
  if (!result.startError && Number.isFinite(duration) && result.start > duration) {
    result.startError = `不能超过视频总时长 ${formatTime(duration)}`;
  }
  if (!result.endError && Number.isFinite(duration) && result.end > duration + 0.005) {
    result.endError = `不能超过视频总时长 ${formatTime(duration)}`;
  }
  if (!result.startError && !result.endError && result.start >= result.end) {
    result.endError = "结束时间必须晚于起始时间";
  }
  if (
    isFrameMode()
    && !result.startError
    && !result.endError
    && result.end - result.start > state.maxFrameSeconds + 0.000001
  ) {
    result.endError = `逐帧截图一次最多 ${state.maxFrameSeconds} 秒`;
  }

  result.valid = !result.startError && !result.endError;
  if (showErrors) renderFieldErrors(result);
  return result;
}

function renderFieldErrors(range) {
  elements.startTimeError.textContent = range.startError;
  elements.endTimeError.textContent = range.endError;
  elements.startTime.setAttribute("aria-invalid", range.startError ? "true" : "false");
  elements.endTime.setAttribute("aria-invalid", range.endError ? "true" : "false");
}

function renderRangeSummary(range) {
  const duration = state.video ? Number(state.video.duration) : 0;
  if (isEnhanceMode()) {
    elements.clipDuration.textContent = range.valid && state.enhanceTarget ? aiTargetLabel() : "—";
    elements.rangeReadout.style.setProperty("--range-start", "0%");
    elements.rangeReadout.style.setProperty("--range-width", range.valid ? "100%" : "0%");
    elements.rangeReadout.setAttribute(
      "aria-label",
      range.valid ? `整段视频将使用 AI 增强到 ${aiTargetLabel()}` : "尚未选择可增强的视频",
    );
    elements.rangeTotal.textContent = duration > 0 ? formatShortTime(duration) : "—";
    elements.suggestedOutputName.textContent = range.valid && state.enhanceTarget
      ? makeSuggestedOutputName(range)
      : "选择可用清晰度后自动生成";
    return;
  }
  if (isRotateMode()) {
    elements.clipDuration.textContent = range.valid ? `${state.rotationDegrees}°` : "—";
    elements.rangeReadout.style.setProperty("--range-start", "0%");
    elements.rangeReadout.style.setProperty("--range-width", range.valid ? "100%" : "0%");
    elements.rangeReadout.setAttribute(
      "aria-label",
      range.valid ? `整段视频将顺时针永久旋转 ${state.rotationDegrees} 度` : "尚未选择可旋转的视频",
    );
    elements.rangeTotal.textContent = duration > 0 ? formatShortTime(duration) : "—";
    elements.suggestedOutputName.textContent = range.valid
      ? makeSuggestedOutputName(range)
      : "选择视频后自动生成";
    return;
  }
  if (range.valid) {
    elements.clipDuration.textContent = formatTime(range.end - range.start);
    const startPercent = duration > 0 ? Math.min(100, Math.max(0, (range.start / duration) * 100)) : 0;
    const widthPercent = duration > 0 ? Math.min(100 - startPercent, ((range.end - range.start) / duration) * 100) : 0;
    elements.rangeReadout.style.setProperty("--range-start", `${startPercent}%`);
    elements.rangeReadout.style.setProperty("--range-width", `${widthPercent}%`);
    elements.rangeReadout.setAttribute(
      "aria-label",
      `当前${isFrameMode() ? "截图" : "剪辑"}范围从 ${formatTime(range.start)} 到 ${formatTime(range.end)}，时长 ${formatTime(range.end - range.start)}`,
    );
  } else {
    elements.clipDuration.textContent = "—";
    elements.rangeReadout.style.setProperty("--range-width", "0%");
    elements.rangeReadout.setAttribute("aria-label", "当前剪辑范围无效");
  }
  elements.rangeTotal.textContent = duration > 0 ? formatShortTime(duration) : "—";
  elements.suggestedOutputName.textContent = range.valid ? makeSuggestedOutputName(range) : "修正时间后自动生成";
}

function makeSuggestedOutputName(range) {
  if (!state.video || !range.valid) return "选择视频后自动生成";
  const original = String(state.video.name || "video");
  const dotIndex = original.lastIndexOf(".");
  const hasExtension = dotIndex > 0 && dotIndex < original.length - 1;
  const stem = (hasExtension ? original.slice(0, dotIndex) : original)
    .replace(/[<>:"/\\|?*]/g, "_")
    .trim() || "video";
  if (isRotateMode()) {
    const extensions = state.video.rotation_output_extensions || {};
    const requestedExtension = extensions[String(state.rotationDegrees)] || state.video.rotation_output_extension;
    const extension = [".mp4", ".mov", ".mkv"].includes(requestedExtension)
      ? requestedExtension
      : ".mp4";
    return `${stem}_rotated_${state.rotationDegrees}${extension}`;
  }
  if (isEnhanceMode()) {
    const suggested = selectedAiTarget()?.suggestedOutputName
      || `${stem}_ai_${state.enhanceTarget || "enhanced"}.mp4`;
    const model = selectedAiModel();
    if (!model || model.id === DEFAULT_AI_MODEL_ID) return suggested;
    const modelSuffix = String(model.id || "").split("-")[0].replace(/[^a-z0-9]/g, "");
    if (!modelSuffix) return suggested;
    const extensionIndex = suggested.lastIndexOf(".");
    const extension = extensionIndex > 0 ? suggested.slice(extensionIndex) : "";
    let suggestedStem = extensionIndex > 0 ? suggested.slice(0, extensionIndex) : suggested;
    const colorSuffix = suggestedStem.endsWith("_sdr") ? "_sdr" : "";
    if (colorSuffix) suggestedStem = suggestedStem.slice(0, -colorSuffix.length);
    return `${suggestedStem}_${modelSuffix}${colorSuffix}${extension}`;
  }
  const start = formatTime(range.start).replaceAll(":", "-").replace(".", "_");
  const end = formatTime(range.end).replaceAll(":", "-").replace(".", "_");
  if (isFrameMode()) return `${stem}_frames_${start}-${end}`;
  const extension = [".mp4", ".mov", ".mkv"].includes(state.video.output_extension)
    ? state.video.output_extension
    : ".mp4";
  return `${stem}_clip_${start}-${end}${extension}`;
}

function isClipPreview(mode) {
  const normalized = String(mode || "").toLowerCase();
  if (!normalized) return false;
  if (["direct", "source", "original", "stream"].includes(normalized)) return false;
  return ["proxy", "generated", "clip", "transcoded", "converted"].some((name) => normalized.includes(name));
}

function currentPlaybackBounds() {
  if (!state.activeRange) return null;
  if (isClipPreview(state.previewMode)) {
    const requestedDuration = state.activeRange.end - state.activeRange.start;
    const mediaDuration = Number(elements.previewVideo.duration);
    return {
      start: 0,
      end: Number.isFinite(mediaDuration) && mediaDuration > 0
        ? Math.min(requestedDuration, mediaDuration)
        : requestedDuration,
    };
  }
  return { start: state.activeRange.start, end: state.activeRange.end };
}

function seekToPreviewStart(autoplay = false) {
  const bounds = currentPlaybackBounds();
  if (!bounds) return;
  const player = elements.previewVideo;
  try {
    player.currentTime = Math.max(0, bounds.start);
  } catch (_error) {
    return;
  }
  if (autoplay) {
    const playPromise = player.play();
    if (playPromise && typeof playPromise.catch === "function") {
      playPromise.catch(() => {
        // Browser autoplay rules may require the user to press Play once.
      });
    }
  }
}

function enforcePreviewBounds() {
  const player = elements.previewVideo;
  const bounds = currentPlaybackBounds();
  if (!bounds || !Number.isFinite(player.currentTime)) return;
  if (player.currentTime >= bounds.end - 0.012 || player.currentTime < bounds.start - 0.05) {
    const shouldResume = !player.paused;
    try {
      player.currentTime = bounds.start;
    } catch (_error) {
      return;
    }
    if (shouldResume) {
      const playPromise = player.play();
      if (playPromise && typeof playPromise.catch === "function") playPromise.catch(() => {});
    }
  }
}

function startPlaybackMonitor() {
  stopPlaybackMonitor();
  const tick = () => {
    enforcePreviewBounds();
    if (!elements.previewVideo.paused && !elements.previewVideo.ended) {
      state.playbackFrameId = window.requestAnimationFrame(tick);
    }
  };
  state.playbackFrameId = window.requestAnimationFrame(tick);
}

function stopPlaybackMonitor() {
  if (state.playbackFrameId !== null) {
    window.cancelAnimationFrame(state.playbackFrameId);
    state.playbackFrameId = null;
  }
}

function withGeneration(url, generation) {
  if (!generation) return url;
  try {
    const parsed = new URL(url, window.location.href);
    parsed.searchParams.set("preview_generation", String(generation));
    return parsed.toString();
  } catch (_error) {
    return url;
  }
}

function loadMedia(url, mode, range, options = {}) {
  const autoplay = Boolean(options.autoplay);
  const generation = options.generation || "";
  const effectiveUrl = withGeneration(url, generation);
  const sameSource = state.previewUrl === effectiveUrl && elements.previewVideo.readyState >= 1;

  state.previewMode = mode || "direct";
  state.activeRange = { start: range.start, end: range.end };
  state.activePreviewKey = options.previewKey || previewKey(range);
  elements.previewPlaceholder.hidden = true;
  elements.previewVideo.hidden = false;

  if (sameSource) {
    seekToPreviewStart(autoplay);
    return Promise.resolve();
  }

  state.previewUrl = effectiveUrl;
  state.mediaLoading = true;

  return new Promise((resolve, reject) => {
    let settled = false;
    const player = elements.previewVideo;
    const timeout = window.setTimeout(() => finish(new Error("预览加载超时")), MEDIA_LOAD_TIMEOUT_MS);

    function cleanup() {
      window.clearTimeout(timeout);
      player.removeEventListener("loadedmetadata", onLoaded);
      player.removeEventListener("error", onError);
      state.mediaLoading = false;
    }

    function finish(error) {
      if (settled) return;
      settled = true;
      cleanup();
      if (error) reject(error);
      else resolve();
    }

    function onLoaded() {
      seekToPreviewStart(autoplay);
      finish();
    }

    function onError() {
      finish(new Error("浏览器无法播放当前预览"));
    }

    player.addEventListener("loadedmetadata", onLoaded);
    player.addEventListener("error", onError);
    player.src = effectiveUrl;
    player.load();
  });
}

function setPreviewLoading(loading, title = "正在更新预览…") {
  elements.previewLoadingTitle.textContent = title;
  elements.previewLoading.hidden = !loading;
  elements.previewPanel.setAttribute("aria-busy", loading ? "true" : "false");
}

function setPreviewStatus(type, text, allowRetry = false) {
  elements.previewStatusDot.className = "status-dot";
  if (type) elements.previewStatusDot.classList.add(`is-${type}`);
  elements.previewStatusText.textContent = text;
  elements.retryPreviewButton.hidden = !allowRetry;
}

function cancelPendingPreview() {
  window.clearTimeout(state.previewTimer);
  state.previewTimer = null;
  if (state.previewController) {
    state.previewController.abort();
    state.previewController = null;
  }
  state.previewRequestSerial += 1;
}

function releaseGeneratedPreview() {
  if (isClipPreview(state.previewMode)) {
    elements.previewVideo.pause();
    elements.previewVideo.removeAttribute("src");
    elements.previewVideo.load();
    state.previewUrl = "";
  }
  state.activePreviewKey = "";
}

function clearRotationPreview() {
  const player = elements.previewVideo;
  elements.previewFrame.classList.remove("is-rotation-preview");
  player.controls = true;
  player.style.removeProperty("width");
  player.style.removeProperty("height");
  player.style.removeProperty("transform");
  player.removeAttribute("role");
  player.removeAttribute("tabindex");
  player.removeAttribute("aria-label");
}

function parseAspectRatio(value) {
  const match = String(value || "").trim().match(/^(\d+)\s*[:/]\s*(\d+)$/);
  if (!match) return 1;
  const numerator = Number(match[1]);
  const denominator = Number(match[2]);
  if (!(numerator > 0 && denominator > 0)) return 1;
  return numerator / denominator;
}

function applyRotationPreview() {
  if (!isRotateMode() || !state.video || elements.previewVideo.hidden) {
    clearRotationPreview();
    return;
  }

  const player = elements.previewVideo;
  const frameWidth = elements.previewFrame.clientWidth;
  const frameHeight = elements.previewFrame.clientHeight;
  const sourceWidth = Number(state.video.width) || player.videoWidth;
  const sourceHeight = Number(state.video.height) || player.videoHeight;
  if (!(frameWidth > 0 && frameHeight > 0 && sourceWidth > 0 && sourceHeight > 0)) return;

  const displayWidth = sourceWidth * parseAspectRatio(state.video.sample_aspect_ratio);
  const sideways = state.rotationDegrees === 90 || state.rotationDegrees === 270;
  const rotatedWidth = sideways ? sourceHeight : displayWidth;
  const rotatedHeight = sideways ? displayWidth : sourceHeight;
  const scale = Math.min(frameWidth / rotatedWidth, frameHeight / rotatedHeight);

  elements.previewFrame.classList.add("is-rotation-preview");
  player.controls = false;
  player.setAttribute("role", "button");
  player.setAttribute("tabindex", "0");
  player.setAttribute("aria-label", "旋转预览，点击播放或暂停");
  player.style.width = `${displayWidth * scale}px`;
  player.style.height = `${sourceHeight * scale}px`;
  player.style.transform = `translate(-50%, -50%) rotate(${state.rotationDegrees}deg)`;
}

function rotationFallbackRange() {
  const duration = Number(state.video && state.video.duration) || 0;
  return {
    valid: duration > 0,
    start: 0,
    end: Math.min(duration, state.maxFrameSeconds),
  };
}

async function prepareRotationPreview(options = {}) {
  if (!isRotateMode() || !state.video) return;
  cancelPendingPreview();
  releaseGeneratedPreview();
  const requestSerial = ++state.previewRequestSerial;
  const videoId = state.video.id;
  const range = readRange(false);
  state.previewReady = false;
  setPreviewLoading(true, "正在读取整段旋转预览…");
  setPreviewStatus("working", "正在准备旋转预览…");
  renderControls();

  try {
    if (!state.video.preview_url) throw new Error("没有可直接播放的预览");
    await loadMedia(state.video.preview_url, state.video.preview_mode || "original", range, {
      autoplay: Boolean(options.autoplay),
      previewKey: previewKey(range),
    });
    if (requestSerial !== state.previewRequestSerial || !isRotateMode() || state.video.id !== videoId) return;
    state.previewReady = true;
    state.mediaFallbackAttempted = false;
    applyRotationPreview();
    setPreviewLoading(false);
    setPreviewStatus("ready", `已实时预览顺时针 ${state.rotationDegrees}°，点击画面可播放或暂停`);
  } catch (error) {
    if (requestSerial !== state.previewRequestSerial || !isRotateMode() || state.video.id !== videoId) return;
    state.mediaFallbackAttempted = true;
    const fallbackRange = rotationFallbackRange();
    if (!fallbackRange.valid) {
      state.previewReady = false;
      setPreviewLoading(false);
      setPreviewStatus("error", error.message || "旋转预览加载失败", true);
    } else {
      await requestPreview(fallbackRange, {
        fallback: true,
        autoplay: false,
        previewOperation: "frames",
      });
    }
  } finally {
    renderControls();
  }
}

async function prepareEnhancePreview(options = {}) {
  if (!isEnhanceMode() || !state.video) return;
  cancelPendingPreview();
  releaseGeneratedPreview();
  clearRotationPreview();
  const requestSerial = ++state.previewRequestSerial;
  const videoId = state.video.id;
  const range = readRange(false);
  state.previewReady = false;
  setPreviewLoading(true, "正在读取整段原片预览…");
  setPreviewStatus("working", "正在读取原片，仅供确认内容…");
  renderControls();

  try {
    if (!state.video.preview_url) throw new Error("没有可直接播放的原片预览");
    await loadMedia(state.video.preview_url, state.video.preview_mode || "original", range, {
      autoplay: Boolean(options.autoplay),
      previewKey: previewKey(range),
    });
    if (requestSerial !== state.previewRequestSerial || !isEnhanceMode() || state.video.id !== videoId) return;
    state.previewReady = true;
    state.mediaFallbackAttempted = false;
    setPreviewLoading(false);
    setPreviewStatus("ready", "当前播放整段原片，仅供确认内容；AI 成片需完整计算后查看");
  } catch (error) {
    if (requestSerial !== state.previewRequestSerial || !isEnhanceMode() || state.video.id !== videoId) return;
    state.mediaFallbackAttempted = true;
    const fallbackRange = rotationFallbackRange();
    if (!fallbackRange.valid) {
      state.previewReady = false;
      setPreviewLoading(false);
      setPreviewStatus("error", error.message || "原片预览加载失败", true);
    } else {
      await requestPreview(fallbackRange, {
        fallback: true,
        autoplay: false,
        previewOperation: "clip",
      });
    }
  } finally {
    renderControls();
  }
}

function schedulePreview(range) {
  cancelPendingPreview();
  if (!range.valid || !state.video) return;

  if (previewKey(range) === state.activePreviewKey && state.previewReady) {
    setPreviewStatus("ready", "预览范围已更新");
    renderControls();
    return;
  }

  state.previewReady = false;
  setPreviewStatus("working", "等待更新预览…");
  state.previewTimer = window.setTimeout(() => {
    requestPreview(range, {
      autoplay: true,
      fallback: state.mediaFallbackAttempted,
    });
  }, PREVIEW_DEBOUNCE_MS);
  renderControls();
}

async function requestPreview(range, options = {}) {
  if (!state.video || !range.valid) return;

  cancelPendingPreview();
  const requestSerial = ++state.previewRequestSerial;
  const controller = new AbortController();
  state.previewController = controller;
  state.previewReady = false;
  setPreviewLoading(true, options.fallback ? "正在准备兼容预览…" : "正在更新预览…");
  setPreviewStatus("working", options.fallback ? "正在转换预览格式…" : "正在生成新的预览范围…");
  renderControls();

  try {
    const result = await post(
      "/api/preview",
      {
        video_id: state.video.id,
        start: range.start,
        end: range.end,
        compatibility: Boolean(options.fallback),
        operation: options.previewOperation || (isRotateMode() ? "frames" : state.operation),
      },
      controller.signal,
    );
    if (requestSerial !== state.previewRequestSerial) return;
    if (!result.preview_url) throw new Error("本地服务没有返回预览地址");

    const generation = result.generation ?? result.generation_id ?? "";
    if (result.suggested_output_name && !isRotateMode() && !isEnhanceMode()) {
      elements.suggestedOutputName.textContent = result.suggested_output_name;
    }
    await loadMedia(result.preview_url, result.mode || "proxy", range, {
      autoplay: options.autoplay !== false,
      generation,
    });
    if (requestSerial !== state.previewRequestSerial) return;

    state.previewReady = true;
    state.mediaFallbackAttempted = isClipPreview(result.mode || "proxy");
    if (isRotateMode()) applyRotationPreview();
    const readyMessage = isEnhanceMode()
      ? range.end + 0.005 < Number(state.video.duration)
        ? `当前为原片前 ${formatShortTime(range.end - range.start)} 的兼容预览；AI 仍会处理整段，成片需完整计算后查看`
        : "当前为整段原片的兼容预览，仅供确认内容；AI 成片需完整计算后查看"
      : isRotateMode()
      ? `兼容预览已就绪，当前顺时针 ${state.rotationDegrees}°；最终会处理整段视频`
      : state.video.is_hdr
      ? isClipPreview(result.mode)
        ? "HDR 兼容预览仅供定位，颜色请以原片和最终成片为准"
        : "HDR 原片预览已更新，正在循环所选范围"
      : isClipPreview(result.mode)
        ? "兼容预览已更新，正在循环所选片段"
        : "预览已更新，正在循环所选范围";
    setPreviewStatus("ready", readyMessage);
    setPreviewLoading(false);
  } catch (error) {
    if (error && error.name === "AbortError") return;
    if (requestSerial !== state.previewRequestSerial) return;
    state.previewReady = false;
    setPreviewLoading(false);
    setPreviewStatus("error", error.message || "预览更新失败", true);
  } finally {
    if (requestSerial === state.previewRequestSerial) state.previewController = null;
    renderControls();
  }
}

function aiModelChoiceDetail(model, { manager = false } = {}) {
  const targets = aiModelTargetsLabel(model).replaceAll(" · ", " / ");
  if (model.blocked) return "此模型当前版本暂未开放";
  const backends = Array.isArray(model.supported_backends)
    ? model.supported_backends
      .filter((backend) => String(backend).toLowerCase() === "cuda")
      .map(() => "RTX")
      .join(" / ")
    : "RTX 5090";
  const prefix = manager && model.default ? "默认 · " : "";
  return `${prefix}${backends || "RTX 5090"} · ${targets}`;
}

function createAiModelChoice(model, className, { manager = false } = {}) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.dataset.modelId = model.id;
  button.dataset.status = model.status;
  button.setAttribute("aria-pressed", model.id === state.selectedAiModelId ? "true" : "false");
  const reason = aiModelCompatibilityReason(model);
  button.title = reason || `切换到 ${model.name}`;
  button.disabled = Boolean(
    state.exporting
    || state.modelDeleteModelId
    || modelDownloadIsActive()
    || state.modelCatalogLoading,
  );

  const title = document.createElement("span");
  const name = document.createElement("strong");
  name.textContent = model.name;
  const status = document.createElement("em");
  status.textContent = aiModelStatusLabel(model);
  title.append(name, status);

  const detail = document.createElement("small");
  detail.textContent = aiModelChoiceDetail(model, { manager });
  button.append(title, detail);
  return button;
}

function renderAiModelChoices() {
  const models = state.aiModels.filter((model) => model && model.id);
  elements.aiModelOptions.replaceChildren(
    ...models.map((model) => createAiModelChoice(model, "ai-model-option")),
  );
  elements.modelSelector.replaceChildren(
    ...models.map((model) => createAiModelChoice(
      model,
      "model-selector-option",
      { manager: true },
    )),
  );
  const model = selectedAiModel();
  if (!model) {
    elements.activeAiModelBadge.textContent = "尚未选择模型";
    elements.aiModelNote.textContent = "正在读取可用 AI 模型。";
    return;
  }
  elements.activeAiModelBadge.textContent = `${model.name} · ${aiModelStatusLabel(model)}`;
  const reason = aiModelCompatibilityReason(model);
  elements.aiModelNote.textContent = reason
    ? `${model.name} 暂不能运行：${reason}`
    : model.experimental
      ? `${model.name} 是实验模型，目前只开放 ${aiModelTargetsLabel(model)}。`
      : `${model.name} 是默认稳定模型，支持 ${aiModelTargetsLabel(model)}。`;
}

function renderAiTargetOptions(controlsLocked = state.exporting) {
  const targets = videoAiTargets();
  const byTarget = new Map(targets.map((item) => [item.target, item]));
  const unavailable = [];
  const model = selectedAiModel();
  const modelRunnable = aiModelCanRun(model);
  const modelReady = aiModelReadyToEnhance(model);

  for (const option of elements.aiTargetOptions) {
    const target = String(option.dataset.target || "").toLowerCase();
    const availability = byTarget.get(target);
    const modelSupportsTarget = selectedModelSupportsTarget(target);
    const available = Boolean(state.video && modelReady && modelSupportsTarget && availability?.available);
    option.disabled = !available || controlsLocked || state.selectingVideo;
    option.setAttribute("aria-pressed", available && target === state.enhanceTarget ? "true" : "false");
    const reason = availability?.reason
      || (!state.video
        ? "请先选择视频"
        : !modelRunnable
          ? aiModelCompatibilityReason(model) || "当前电脑无法使用所选 AI 模型"
          : !modelReady
            ? aiModelPreparationReason(model)
          : !modelSupportsTarget
            ? `${model?.name || "所选模型"} 仅支持 ${aiModelTargetsLabel(model)}`
          : "目标清晰度低于原片，不会降级处理");
    option.title = available ? `增强到 ${AI_TARGETS[target].label}` : reason;
    const dimensions = option.querySelector("[data-ai-dimensions]");
    if (dimensions) {
      dimensions.textContent = availability?.width && availability?.height
        ? `${availability.width} × ${availability.height}`
        : AI_TARGETS[target].dimensions;
    }
    if (state.video && !available) unavailable.push(`${AI_TARGETS[target].label}：${reason}`);
  }

  const runtime = selectedAiRuntime();
  const runtimeMessage = String(runtime.message || runtime.reason || "").trim();
  if (!state.video) {
    elements.aiTargetNote.textContent = "选择视频后会自动推荐第一个可用目标清晰度。";
  } else if (!modelRunnable) {
    elements.aiTargetNote.textContent = aiModelCompatibilityReason(model)
      || runtimeMessage
      || "当前电脑无法使用所选 AI 模型。";
  } else if (!modelReady) {
    elements.aiTargetNote.textContent = `${aiModelPreparationReason(model)}，完成后即可选择目标清晰度。`;
  } else if (!state.enhanceTarget) {
    elements.aiTargetNote.textContent = unavailable.length
      ? `当前没有可用目标：${unavailable.join("；")}`
      : "当前视频没有可用的 AI 超清目标。";
  } else {
    const suffix = unavailable.length ? ` 其余目标不可用：${unavailable.join("；")}` : "";
    const warning = selectedAiTarget()?.warning ? ` 注意：${selectedAiTarget().warning}。` : "";
    elements.aiTargetNote.textContent = `将用 ${model?.name || "所选模型"} 按原比例增强到 ${aiTargetLabel()} 边界，不拉伸画面。${warning}${suffix}`;
  }
}

function renderModeCopy() {
  const frames = isFrameMode();
  const rotate = isRotateMode();
  const enhance = isEnhanceMode();
  const sourceDirectory = rotate || enhance;
  const aiModel = selectedAiModel();
  const aiModelName = aiModel?.name || "AI 模型";
  const aiRuntime = selectedAiRuntime();
  elements.modeClipButton.setAttribute("aria-pressed", !frames && !rotate && !enhance ? "true" : "false");
  elements.modeFramesButton.setAttribute("aria-pressed", frames ? "true" : "false");
  elements.modeRotateButton.setAttribute("aria-pressed", rotate ? "true" : "false");
  elements.modeEnhanceButton.setAttribute("aria-pressed", enhance ? "true" : "false");
  elements.timePanel.hidden = rotate || enhance;
  elements.rotationPanel.hidden = !rotate;
  elements.aiPanel.hidden = !enhance;
  elements.pageTitle.textContent = enhance
    ? "让整段视频真正清晰起来"
    : rotate
      ? "把正确方向永久写进视频"
      : frames
        ? "把这一小段逐帧保存"
        : "留下想要的这一段";
  elements.heroCopy.textContent = enhance
    ? `选择模型和它支持的目标清晰度，使用 ${aiModelName} 在${aiDeviceShortName(aiRuntime)}本地增强；非标准色彩会自动转换，HDR 会明确映射为 SDR。`
    : rotate
      ? "选一个视频和旋转角度，确认预览后生成同级新文件。原视频始终不会被修改。"
      : frames
        ? `选好不超过 ${state.maxFrameSeconds} 秒的范围，预览满意后，把其中每一帧按原始尺寸保存成无损图片。`
        : "选一个视频，填好起点和终点，预览满意后直接保存。原视频始终不会被修改。";
  elements.journeyRangeLabel.textContent = enhance ? "选择清晰度" : rotate ? "选择角度" : frames ? "设置范围" : "设置片段";
  elements.journeyExportLabel.textContent = enhance ? "确认增强" : rotate ? "确认旋转" : frames ? "确认截图" : "确认导出";
  elements.trimHeading.textContent = enhance ? "选择 AI 超清目标" : rotate ? "选择永久旋转角度" : frames ? "设置截图范围" : "设置剪辑范围";
  elements.trimDescription.textContent = enhance
    ? "不需要填写时间；整段视频都会处理。下方播放的是原片内容预览，不是 AI 效果预览。"
    : rotate
      ? "不需要填写时间；整段视频都会处理，点击角度后预览立即更新。"
      : frames
        ? `填写起始时间后，结束时间会自动设为“起点 + ${state.maxFrameSeconds} 秒”；修改后预览会自动更新。`
        : "只需填写起始时间和结束时间，预览会自动更新。";
  elements.durationLabel.textContent = enhance ? "目标清晰度" : rotate ? "旋转角度" : frames ? "截图时长" : "片段时长";
  elements.frameLimitHint.textContent = `修改起点会自动取后 ${state.maxFrameSeconds} 秒（不足则到视频结尾）；图片按视频原尺寸无损保存。`;
  elements.frameLimitHint.hidden = !frames;
  elements.exportHeading.textContent = enhance ? "确认并开始 AI 超清" : rotate ? "确认并永久旋转" : frames ? "确认并逐帧截图" : "确认并导出";
  elements.exportDescription.textContent = enhance
    ? "整段视频会在本机完成 AI 计算，并在原视频同级目录保存为新文件；原视频不会被修改。"
    : rotate
      ? "生成在原视频同级目录；方向会真正写入画面，原视频不变。"
      : frames
        ? "截图会放进一个独立文件夹，原视频不会有任何变化。"
        : "导出为新文件，原视频不会有任何变化。";
  elements.qualityChipText.textContent = enhance
    ? `${aiModelName} · ${aiModel?.precision || "本机"} · ${aiModelStatusLabel(aiModel)}`
    : rotate
      ? "完整时长 · 原音频直拷"
      : frames
        ? "原始宽高 · 无损图片"
        : "保持原分辨率与高品质音频";
  elements.outputNameLabel.textContent = frames ? "文件夹名" : "文件名";
  elements.outputNameNote.textContent = enhance
    ? "10-bit HEVC · BT.709 SDR；原音频直接复制；同名自动编号"
    : rotate
      ? "如遇同名文件会自动添加编号"
      : frames
        ? "图片按 frame_000001 开始顺序编号"
        : "如遇同名文件会自动添加编号";
  elements.destinationLabel.textContent = sourceDirectory ? "固定保存到原视频同级目录" : "保存到";
  elements.rotationNote.textContent = state.rotationDegrees === 360
    ? "360° 看起来方向不变，但仍会重新生成一份已固化、已清除旋转标记的新视频。"
    : state.rotationDegrees === 90 || state.rotationDegrees === 270
      ? "90° 和 270° 会交换画面宽高；帧率、色彩信息和原音频会尽量保持不变。"
      : "180° 不会交换画面宽高；帧率、色彩信息和原音频会尽量保持不变。";
  for (const option of elements.rotationOptions) {
    option.setAttribute("aria-pressed", Number(option.dataset.degrees) === state.rotationDegrees ? "true" : "false");
  }
  renderAiModelChoices();
  renderAiTargetOptions();
  if (!state.exportCompleted) {
    elements.completionSummary.textContent = enhance
      ? "AI 超清完成，原视频未被修改"
      : rotate
      ? "永久旋转完成，原视频未被修改"
      : frames
        ? "截图完成，原视频未被修改"
        : "剪辑完成，原视频未被修改";
  }
  elements.continueButton.textContent = enhance ? "继续增强" : rotate ? "继续旋转" : frames ? "继续截图" : "继续剪辑";
  renderOutputDirectory();
}

function changeOperation(operation) {
  if (
    state.exporting
    || state.selectingVideo
    || !["clip", "frames", "rotate", "enhance"].includes(operation)
    || (operation === "enhance" && !aiFeaturesAvailable())
    || operation === state.operation
  ) {
    return;
  }
  cancelPendingPreview();
  stopExportPolling();
  resetExportResult();
  releaseGeneratedPreview();
  clearRotationPreview();
  state.operation = operation;
  if (isEnhanceMode()) selectDefaultAiTarget();
  if (isFrameMode()) setDefaultFrameEnd();
  state.previewReady = false;
  state.activePreviewKey = "";

  renderModeCopy();
  const range = readRange(true);
  renderRangeSummary(range);
  if (range.valid && state.video) {
    if (isRotateMode()) {
      prepareRotationPreview();
    } else if (isEnhanceMode()) {
      prepareEnhancePreview();
    } else {
      schedulePreview(range);
    }
  } else if (state.video) {
    state.previewReady = false;
    setPreviewStatus("error", isEnhanceMode() ? "当前视频没有可用的 AI 超清目标" : "请修正时间后更新预览");
  }
  renderControls();
  showToast(
    isEnhanceMode()
      ? `已切换到 AI 超清，整段视频将使用 ${selectedAiModel()?.name || "所选模型"} 处理`
      : isRotateMode()
      ? `已切换到永久旋转，当前顺时针 ${state.rotationDegrees}°`
      : isFrameMode()
        ? `已切换到逐帧截图，单次最多 ${state.maxFrameSeconds} 秒`
        : "已切换到视频剪辑",
  );
}

function resetExportResult() {
  state.exportCompleted = false;
  state.exportError = "";
  state.exportJobId = "";
  state.exportOutputName = "";
  elements.exportSetup.hidden = false;
  elements.exportProgressPanel.hidden = true;
  elements.exportProgressPanel.classList.remove("is-error");
  elements.exportCompletePanel.hidden = true;
  setExportProgress(0);
}

function handleTimeInput(event) {
  if (!state.video || state.exporting || isRotateMode() || isEnhanceMode()) return;
  if (state.exportCompleted || state.exportError) resetExportResult();
  releaseGeneratedPreview();
  if (isFrameMode() && event.currentTarget === elements.startTime) {
    setDefaultFrameEnd(event.currentTarget.value);
  }

  const range = readRange(true);
  renderRangeSummary(range);
  if (!range.valid) {
    cancelPendingPreview();
    state.previewReady = false;
    setPreviewLoading(false);
    setPreviewStatus("error", "请修正时间后更新预览");
  } else {
    schedulePreview(range);
  }
  renderControls();
}

function handleTimeBlur(event) {
  if (isRotateMode() || isEnhanceMode()) return;
  const value = parseTime(event.currentTarget.value);
  if (value !== null) event.currentTarget.value = formatTime(value);
}

function handleTimeKeydown(event) {
  if (isRotateMode() || isEnhanceMode()) return;
  if (event.key !== "Enter") return;
  event.preventDefault();
  event.currentTarget.blur();
  const range = readRange(true);
  renderRangeSummary(range);
  if (range.valid) requestPreview(range, { autoplay: true });
}

function renderVideoDetails() {
  const video = state.video;
  elements.sourceEmpty.hidden = Boolean(video);
  elements.selectedFile.hidden = !video;
  if (!video) return;

  elements.videoName.textContent = video.name || "未命名视频";
  elements.videoPath.textContent = video.path_display || "本地文件";
  elements.videoPath.title = video.path_display || "";
  elements.videoDuration.textContent = formatTime(video.duration);
  elements.videoResolution.textContent = video.width && video.height ? `${video.width} × ${video.height}` : "—";
  const fps = Number(video.fps);
  elements.videoFps.textContent = Number.isFinite(fps) && fps > 0 ? `${fps.toFixed(fps % 1 ? 2 : 0)} fps` : "—";
  elements.videoCodecs.textContent = `${normalizeCodec(video.video_codec)} · ${normalizeCodec(video.audio_codec)}`;
  renderOutputDirectory();
}

async function selectVideo() {
  if (!state.appReady || state.selectingVideo || state.exporting) return;
  state.selectingVideo = true;
  setButtonBusy(elements.selectVideoButton, true, "正在打开文件选择窗口…");
  setButtonBusy(elements.replaceVideoButton, true, "正在打开…");
  renderControls();

  try {
    const result = await post("/api/videos/select");
    if (result.cancelled) return;
    if (!result.video || !result.video.id) throw new Error("没有读取到有效的视频信息");

    cancelPendingPreview();
    stopExportPolling();
    resetExportResult();
    state.video = result.video;
    state.mediaFallbackAttempted = false;
    state.previewReady = false;
    state.previewMode = result.video.preview_mode || "direct";
    state.previewUrl = "";
    state.activePreviewKey = "";
    state.enhanceTarget = "";
    selectDefaultAiTarget();

    const duration = Number(result.video.duration);
    const suggestedStart = parseTime(result.suggested_start);
    const suggestedEnd = parseTime(result.suggested_end);
    const start = suggestedStart === null ? 0 : suggestedStart;
    const end = suggestedEnd === null ? duration : suggestedEnd;
    elements.startTime.value = formatTime(start);
    elements.endTime.value = formatTime(end);
    if (isFrameMode()) setDefaultFrameEnd(start);

    renderVideoDetails();
    const range = readRange(true);
    renderRangeSummary(range);
    if (isRotateMode()) {
      await prepareRotationPreview();
    } else if (isEnhanceMode()) {
      await prepareEnhancePreview();
    } else {
      setPreviewLoading(true, "正在读取视频预览…");
      setPreviewStatus("working", "正在读取视频…");
      renderControls();

      try {
        if (!result.video.preview_url) throw new Error("没有可直接播放的预览");
        await loadMedia(result.video.preview_url, result.video.preview_mode || "direct", range, { autoplay: false });
        state.previewReady = true;
        setPreviewLoading(false);
        setPreviewStatus(
          "ready",
          state.video.is_hdr
            ? "HDR 原片预览已就绪；修改时间后仍会使用原片定位"
            : "预览已就绪，播放时将循环所选范围",
        );
      } catch (_mediaError) {
        state.mediaFallbackAttempted = true;
        await requestPreview(range, { fallback: true, autoplay: false });
      }
    }

    showToast(`已选择“${result.video.name || "视频"}”`);
  } catch (error) {
    if (!error || error.name !== "AbortError") {
      showToast(error.message || "选择视频失败", "error");
    }
  } finally {
    state.selectingVideo = false;
    setButtonBusy(elements.selectVideoButton, false);
    setButtonBusy(elements.replaceVideoButton, false);
    renderControls();
  }
}

async function selectOutputDirectory() {
  if (!state.appReady || state.selectingDirectory || state.exporting || usesSourceDirectory()) return;
  state.selectingDirectory = true;
  setButtonBusy(elements.selectDirectoryButton, true, "正在打开文件夹选择窗口…");
  renderControls();

  try {
    const result = await post("/api/output-directory/select");
    if (result.cancelled) return;
    const directory = normalizeDirectory(result.output_directory);
    if (!directory) throw new Error("没有读取到有效的保存目录");
    state.outputDirectory = directory;
    renderOutputDirectory();
    showToast("保存目录已设置，下次启动会继续使用");
  } catch (error) {
    showToast(error.message || "选择保存目录失败", "error");
  } finally {
    state.selectingDirectory = false;
    setButtonBusy(elements.selectDirectoryButton, false);
    renderControls();
  }
}

function renderOutputDirectory() {
  const sourceDirectory = usesSourceDirectory();
  elements.selectDirectoryButton.hidden = sourceDirectory;
  if (sourceDirectory) {
    const directory = state.video && state.video.directory_display;
    elements.outputDirectory.textContent = directory || "选择视频后自动使用原视频同级目录";
    elements.outputDirectory.title = directory || "";
    return;
  }
  if (state.outputDirectory) {
    elements.outputDirectory.textContent = state.outputDirectory;
    elements.outputDirectory.title = state.outputDirectory;
    const label = elements.selectDirectoryButton.querySelector(".button-label");
    if (label && !state.selectingDirectory) label.textContent = "更改";
    elements.selectDirectoryButton.dataset.idleLabel = "更改";
  } else {
    elements.outputDirectory.textContent = "首次使用，请选择保存目录";
    elements.outputDirectory.title = "";
    const label = elements.selectDirectoryButton.querySelector(".button-label");
    if (label && !state.selectingDirectory) label.textContent = "选择文件夹";
    elements.selectDirectoryButton.dataset.idleLabel = "选择文件夹";
  }
}

function normalizeProgress(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return 0;
  return Math.min(100, Math.max(0, numeric));
}

function modelRuntimePrepared(runtime = selectedAiRuntime(), model = selectedAiModel()) {
  if (!runtime || typeof runtime !== "object") return false;
  const selectedSnapshot = state.modelDownloadModelId === model?.id ? state.modelDownload : null;
  if (typeof selectedSnapshot?.prepared === "boolean") return selectedSnapshot.prepared;
  if (typeof model?.prepared === "boolean") return model.prepared;
  if (typeof runtime.prepared === "boolean") return runtime.prepared;
  if (model && typeof model.downloaded === "boolean") {
    return Boolean(runtime.installed && model.downloaded);
  }
  return Boolean(runtime.installed && runtime.models_downloaded);
}

function aiBackendLabel(runtime = selectedAiRuntime()) {
  const configuredLabel = String(runtime?.backend_label || "").trim();
  if (configuredLabel) return configuredLabel;
  const backend = String(runtime?.backend || "").trim().toUpperCase();
  return backend || "本机 AI 加速";
}

function aiDeviceShortName(runtime = selectedAiRuntime()) {
  return String(runtime?.device_name || "").trim() || "这台电脑";
}

function aiDeviceSummary(runtime = selectedAiRuntime()) {
  const deviceName = String(runtime?.device_name || "").trim();
  const backendLabel = aiBackendLabel(runtime);
  const memoryGb = Number(runtime?.device_memory_gb);
  const parts = [];
  if (deviceName) parts.push(deviceName);
  if (backendLabel && !deviceName.toLowerCase().includes(backendLabel.toLowerCase())) {
    parts.push(backendLabel);
  }
  if (Number.isFinite(memoryGb) && memoryGb > 0) {
    const digits = memoryGb >= 10 || Number.isInteger(memoryGb) ? 0 : 1;
    parts.push(`${memoryGb.toFixed(digits)} GB`);
  }
  if (runtime?.hardware_verified === true) parts.push("已检测");
  else if (runtime?.hardware_verified === false && (deviceName || runtime?.backend)) {
    parts.push("待硬件校验");
  }
  return parts.join(" · ") || "正在检测本机 AI 加速设备";
}

function modelDownloadIsActive(snapshot = state.modelDownload) {
  const status = String(snapshot?.status || "").toLowerCase();
  return ["queued", "pending", "waiting", "running", "processing"].includes(status);
}

function modelHasInstalledFiles(model = selectedAiModel(), snapshot = state.modelDownload) {
  if (!model) return false;
  const snapshotMatchesModel = String(snapshot?.model_id || state.modelDownloadModelId || "").toLowerCase()
    === model.id;
  return model.downloaded === true
    || model.prepared === true
    || Number(model.partial_bytes) > 0
    || (snapshotMatchesModel && (
      snapshot?.models_downloaded === true
      || snapshot?.prepared === true
      || Number(snapshot?.downloaded_bytes) > 0
    ));
}

function aiEnhancementIsActive() {
  return state.exporting && isEnhanceMode();
}

function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return "";
  if (bytes < 1000) return `${Math.round(bytes)} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let amount = bytes;
  let unit = "B";
  for (const candidate of units) {
    amount /= 1000;
    unit = candidate;
    if (amount < 1000) break;
  }
  const digits = amount >= 100 ? 0 : amount >= 10 ? 1 : 2;
  return `${amount.toFixed(digits)} ${unit}`;
}

function setProgressAriaValue(track, value, remainingText = "") {
  const rounded = Math.round(normalizeProgress(value));
  const conciseRemaining = String(remainingText || "").replace("预计还需：", "预计还需");
  track.setAttribute(
    "aria-valuetext",
    conciseRemaining ? `${rounded}%，${conciseRemaining}` : `${rounded}%`,
  );
}

function setModelProgress(value, remainingText = "") {
  const percentage = normalizeProgress(value);
  const rounded = Math.round(percentage);
  elements.modelProgressValue.textContent = `${rounded}%`;
  elements.modelProgressBar.style.width = `${percentage}%`;
  elements.modelProgressTrack.setAttribute("aria-valuenow", String(rounded));
  setProgressAriaValue(elements.modelProgressTrack, percentage, remainingText);
}

function modelStageCopy(stage, active, modelName = selectedAiModel()?.name || "AI 模型") {
  const normalized = String(stage || "").toLowerCase();
  if (["queued", "pending", "waiting"].includes(normalized)) {
    return ["等待开始", "模型准备任务正在排队…"];
  }
  if (normalized.includes("verify") || normalized.includes("validat")) {
    return ["完整性校验", "正在校验模型文件…"];
  }
  if (normalized.includes("model") || normalized === "download" || normalized === "downloading") {
    return ["下载模型", `正在下载 ${modelName}…`];
  }
  if (normalized.includes("install") || normalized.includes("depend") || normalized === "setup") {
    return ["准备运行环境", "正在安装本地 AI 运行环境…"];
  }
  if (active) return ["本机准备中", `正在准备 ${modelName}…`];
  return ["准备状态", "尚未开始下载"];
}

function setComponentState(dot, statusElement, stateName, text) {
  dot.classList.toggle("is-ready", stateName === "ready");
  dot.classList.toggle("is-running", stateName === "running");
  statusElement.textContent = text;
}

function renderModelManager() {
  const model = selectedAiModel();
  const runtime = selectedAiRuntime() && typeof selectedAiRuntime() === "object"
    ? selectedAiRuntime()
    : {};
  const snapshot = state.modelDownload && typeof state.modelDownload === "object"
    ? state.modelDownload
    : {};
  const status = String(snapshot.status || "idle").toLowerCase();
  const stage = String(snapshot.stage || status).toLowerCase();
  const active = modelDownloadIsActive(snapshot);
  const prepared = modelRuntimePrepared(runtime, model);
  const blocked = Boolean(model?.blocked);
  const platformReady = aiModelCanRun(model);
  const unavailable = Boolean(snapshot.runtime_unavailable)
    || status === "unavailable"
    || !platformReady
    || blocked;
  const failed = status === "failed" || status === "error" || Boolean(state.modelDownloadRequestError);
  const cancelled = status === "cancelled" || status === "canceled";
  const needsSetup = status === "needs_setup" || Boolean(snapshot.requires_runtime_update);
  const checking = !state.appReady || state.modelCatalogLoading;
  const deleting = state.modelDeleteModelId === model?.id;
  const anyModelDeleting = Boolean(state.modelDeleteModelId);
  const installedFiles = modelHasInstalledFiles(model, snapshot);
  const progress = prepared ? 100 : normalizeProgress(snapshot.progress);
  const configuredTotal = Number(model?.download_size_bytes);
  const sizeGb = Number(runtime.first_download_gb);
  const fallbackTotal = Number.isFinite(configuredTotal) && configuredTotal > 0
    ? configuredTotal
    : Number.isFinite(sizeGb) && sizeGb > 0
      ? sizeGb * 1_000_000_000
      : 0;
  const totalBytes = Number(snapshot.total_bytes) > 0 ? Number(snapshot.total_bytes) : fallbackTotal;
  const downloadedBytes = prepared && totalBytes > 0
    ? totalBytes
    : Math.max(0, Number(snapshot.downloaded_bytes ?? model?.partial_bytes) || 0);
  const modelRemainingText = remainingTimeText(snapshot, {
    cancelling: state.modelDownloadCancelling,
  });

  const backendLabel = aiBackendLabel(runtime);
  const deviceName = aiDeviceShortName(runtime);
  elements.modelManagerHeading.textContent = model?.name || "AI 模型";
  elements.modelKicker.textContent = blocked
    ? "完整质量路径 · 等待兼容验证"
    : model?.experimental
      ? "实验模型 · 请先用短片验证"
      : model?.default
        ? "默认稳定模型"
        : "稳定模型";
  elements.modelPrecisionValue.textContent = model?.precision || "—";
  elements.modelTargetsValue.textContent = aiModelTargetsLabel(model);
  elements.modelDeviceValue.textContent = aiDeviceSummary(runtime);
  const minimumRuntimeGb = Number(runtime.minimum_runtime_free_gb);
  elements.modelSpaceValue.textContent = Number.isFinite(minimumRuntimeGb) && minimumRuntimeGb > 0
    ? `至少约 ${minimumRuntimeGb.toFixed(0)} GB`
    : totalBytes > 0
      ? `至少约 ${Math.ceil(totalBytes / 1_000_000_000)} GB`
      : "正在计算";
  const executionProfile = model?.id === DEFAULT_AI_MODEL_ID
    ? "FP16 + SDPA"
    : model?.precision || "本机默认精度";
  elements.modelBackendDescription.textContent = model
    ? `${model.description} 质量路径：${executionProfile}。${runtime.backend || runtime.backend_label ? ` 当前设备：${deviceName} · ${backendLabel}。` : ""}`
    : "正在读取本机可用的 AI 模型。";
  const compatibilityReason = aiModelCompatibilityReason(model);
  elements.modelCompatibilityNote.hidden = !compatibilityReason;
  elements.modelCompatibilityNote.dataset.level = blocked ? "blocked" : "unavailable";
  elements.modelCompatibilityNote.textContent = compatibilityReason;

  let badgeStatus = "idle";
  let badgeText = "尚未下载";
  if (checking) badgeText = "正在检查";
  else if (blocked) [badgeStatus, badgeText] = ["blocked", "暂不可用"];
  else if (prepared) [badgeStatus, badgeText] = ["ready", "已就绪"];
  else if (active) [badgeStatus, badgeText] = ["running", "准备中"];
  else if (unavailable) badgeText = "设备不兼容";
  else if (failed) [badgeStatus, badgeText] = ["failed", "未完成"];
  else if (cancelled) [badgeStatus, badgeText] = ["cancelled", "已暂停"];
  else if (needsSetup) badgeText = "需要更新";
  elements.modelStatusBadge.dataset.status = badgeStatus;
  elements.modelStatusBadge.textContent = badgeText;

  const runtimeBusy = active && !runtime.installed;
  const weightsBusy = active && (runtime.installed || stage.includes("model") || stage.includes("download") || stage.includes("verify"));
  const weightsReady = prepared || model?.downloaded === true || snapshot.models_downloaded === true;
  setComponentState(
    elements.runtimeComponentDot,
    elements.runtimeComponentStatus,
    runtime.installed ? "ready" : runtimeBusy ? "running" : "idle",
    runtime.installed
      ? "已安装并校验"
      : runtimeBusy
        ? "正在安装…"
        : blocked
          ? "当前不会安装"
          : needsSetup
            ? "需要安装或更新"
            : "尚未安装",
  );
  setComponentState(
    elements.weightsComponentDot,
    elements.weightsComponentStatus,
    weightsReady ? "ready" : weightsBusy ? "running" : "idle",
    weightsReady
      ? "已下载并校验"
      : weightsBusy
        ? "正在下载或校验…"
        : blocked
          ? "下载已锁定"
          : "尚未下载",
  );

  elements.modelSizeValue.textContent = totalBytes > 0 ? `约 ${formatBytes(totalBytes)}` : "正在计算";
  setModelProgress(progress, modelRemainingText);
  elements.modelProgressPanel.classList.toggle("is-ready", prepared);
  elements.modelProgressPanel.classList.toggle("is-error", failed);
  elements.modelProgressPanel.classList.toggle("is-blocked", blocked);

  const [stageKicker, stageTitle] = modelStageCopy(stage, active, model?.name);
  elements.modelProgressKicker.textContent = blocked
    ? "兼容性状态"
    : prepared
      ? "准备完成"
      : failed
        ? "准备未完成"
        : stageKicker;
  elements.modelProgressTitle.textContent = prepared
    ? "AI 模型已经可以直接使用"
    : blocked
      ? "等待官方完整质量路径支持 RTX 50"
    : unavailable
      ? "所选模型无法在当前设备运行"
    : failed
      ? "AI 模型下载或安装失败"
      : needsSetup
        ? "AI 运行环境需要更新"
      : cancelled
        ? "下载已暂停，可以继续"
        : checking
          ? "正在读取本机状态…"
          : stageTitle;

  if (totalBytes > 0 && (downloadedBytes > 0 || active || prepared || cancelled || failed)) {
    elements.modelDownloadBytes.textContent = `${formatBytes(Math.min(downloadedBytes, totalBytes))} / ${formatBytes(totalBytes)}`;
  } else if (totalBytes > 0) {
    elements.modelDownloadBytes.textContent = `模型共约 ${formatBytes(totalBytes)}`;
  } else {
    elements.modelDownloadBytes.textContent = active ? "正在计算下载大小" : "等待开始";
  }
  const speed = Number(snapshot.download_speed_bps);
  elements.modelDownloadSpeed.textContent = active && Number.isFinite(speed) && speed > 0
    ? `${formatBytes(speed)}/s`
    : "";
  elements.modelDownloadElapsed.textContent = Number.isFinite(Number(snapshot.elapsed_seconds))
    && Number(snapshot.elapsed_seconds) > 0
    ? formatElapsed(snapshot.elapsed_seconds)
    : "";
  elements.modelDownloadRemaining.textContent = modelRemainingText;

  const message = state.modelDownloadRequestError
    || snapshot.error
    || snapshot.message
    || runtime.message;
  elements.modelProgressMessage.textContent = prepared
    ? `${model?.name || "AI 模型"} 与本地 ${backendLabel} 运行环境均已完成校验。`
    : blocked
      ? compatibilityReason
      : String(message || "可以现在提前准备，之后做 AI 超清时无需再等待首次下载。");

  const busy = active || state.modelDownloadStarting;
  const buttonLabel = elements.modelDownloadButton.querySelector(".button-label");
  elements.modelDownloadButton.classList.toggle("is-loading", busy);
  elements.modelDownloadButton.setAttribute("aria-busy", busy ? "true" : "false");
  elements.modelDownloadButton.disabled = checking
    || busy
    || anyModelDeleting
    || state.modelDownloadCancelling
    || prepared
    || !platformReady
    || blocked;
  if (buttonLabel) {
    buttonLabel.textContent = prepared
      ? "模型已准备完成"
      : blocked
        ? "等待兼容验证"
        : unavailable
        ? "当前不可用"
      : busy
        ? "正在准备模型…"
        : needsSetup
          ? "更新运行环境"
        : failed || cancelled
          ? "继续下载"
          : runtime.installed
            ? "提前下载模型"
            : "提前下载并安装";
  }
  elements.cancelModelDownloadButton.hidden = !active;
  elements.cancelModelDownloadButton.disabled = state.modelDownloadCancelling;
  elements.cancelModelDownloadButton.textContent = state.modelDownloadCancelling ? "正在取消…" : "取消下载";

  const deleteBlockedByTask = active
    || state.modelDownloadStarting
    || state.modelDownloadCancelling
    || aiEnhancementIsActive();
  const offlineManaged = model?.offline_managed === true || runtime?.offline_managed === true;
  elements.modelDeleteButton.disabled = checking
    || anyModelDeleting
    || deleteBlockedByTask
    || offlineManaged
    || !state.multiModelApiAvailable
    || !installedFiles;
  elements.modelDeleteButton.setAttribute(
    "aria-label",
    deleting ? `正在删除 ${model?.name || "所选模型"}` : `删除 ${model?.name || "所选模型"} 的本地文件`,
  );
  setButtonBusy(elements.modelDeleteButton, deleting, "正在删除…");
  elements.modelDeleteHint.textContent = deleting
    ? `正在删除 ${model?.name || "所选模型"} 的本地文件，请不要关闭页面。`
    : deleteBlockedByTask
      ? "模型下载或 AI 超清任务进行期间不能删除模型。"
      : offlineManaged
        ? "这个模型由完整离线包管理，已禁止单独删除，避免破坏离线包。"
      : !state.multiModelApiAvailable
        ? "当前本地服务版本不支持在网页中删除模型。"
        : !installedFiles
          ? "所选模型尚未安装，无需删除。"
          : "只删除所选模型的权重和未完成下载；已安装的 AI 运行环境会保留。";
  elements.modelDeleteError.textContent = state.modelDeleteError;
  elements.modelDeleteError.hidden = !state.modelDeleteError;

  elements.modelActionNote.textContent = prepared
    ? "已经准备完成；以后不会重复下载。"
    : active
      ? "可以切回视频处理；下载会继续，已完成部分会保留。"
      : blocked
        ? compatibilityReason
      : !platformReady && !checking
        ? compatibilityReason || String(runtime.message || "当前电脑没有可用的 AI 加速运行环境。")
        : needsSetup
          ? "模型文件无需重下；只会更新本地运行环境。"
        : failed
          ? "检查网络或磁盘空间后点击继续，已下载部分会用于续传。"
          : "下载可以续传；切换页面不会中断。";

  elements.aiRuntimeInlineNote.textContent = prepared
    ? `${model?.name || "AI 模型"} 已提前准备完成，开始 AI 超清时可以直接加载。`
    : unavailable
      ? compatibilityReason || String(runtime.message || "当前环境无法使用 AI 超清。")
    : active
      ? `${model?.name || "AI 模型"} 正在后台准备（${Math.round(progress)}%），切换页面不会中断。`
      : `${aiDeviceSummary(runtime)}；全程本地处理，可以提前下载约 ${formatBytes(totalBytes)} 模型。`;
  elements.openModelManagerButton.textContent = prepared
    ? "查看模型状态"
    : active
      ? "查看下载进度"
      : "管理 AI 模型";
  renderAiModelChoices();
  renderAiProxySettings();
}

function modelDownloadPath(modelId = state.modelDownloadModelId || state.selectedAiModelId) {
  return state.multiModelApiAvailable
    ? `/api/ai-models/${encodeURIComponent(modelId)}/download`
    : "/api/ai-model-download";
}

function initialModelDownloadSnapshot(model = selectedAiModel()) {
  const totalBytes = Math.max(0, Number(model?.download_size_bytes) || 0);
  const downloadedBytes = model?.downloaded
    ? totalBytes
    : Math.min(totalBytes, Math.max(0, Number(model?.partial_bytes) || 0));
  const prepared = model?.prepared === true;
  const unavailable = !aiModelCanRun(model);
  return {
    model_id: model?.id || state.selectedAiModelId,
    status: prepared
      ? "completed"
      : unavailable
        ? "unavailable"
        : downloadedBytes > 0
          ? "cancelled"
          : "idle",
    stage: prepared ? "completed" : unavailable ? "unavailable" : "idle",
    progress: prepared ? 100 : totalBytes > 0 ? downloadedBytes / totalBytes * 98 : 0,
    downloaded_bytes: downloadedBytes,
    total_bytes: totalBytes,
    download_speed_bps: 0,
    estimated_remaining_seconds: prepared ? 0 : null,
    prepared,
    models_downloaded: model?.downloaded === true,
    runtime_unavailable: unavailable,
    runtime: model?.runtime || selectedAiRuntime(),
    message: unavailable ? aiModelCompatibilityReason(model) : "",
  };
}

function applyModelDownloadSnapshot(snapshot, modelId = state.modelDownloadModelId) {
  if (!snapshot || typeof snapshot !== "object") return;
  const resolvedModelId = String(snapshot.model_id || modelId || state.selectedAiModelId).toLowerCase();
  state.modelDownloadModelId = resolvedModelId;
  state.modelDownload = snapshot;
  if (!modelDownloadIsActive(snapshot)) state.modelDownloadCancelling = false;
  const runtime = snapshot.runtime || snapshot.ai_runtime;
  const model = state.aiModels.find((item) => item.id === resolvedModelId);
  if (model) {
    if (typeof snapshot.prepared === "boolean") model.prepared = snapshot.prepared;
    if (typeof snapshot.models_downloaded === "boolean") {
      model.downloaded = snapshot.models_downloaded;
    }
    if (Number.isFinite(Number(snapshot.downloaded_bytes))) {
      model.partial_bytes = Math.max(0, Number(snapshot.downloaded_bytes));
    }
    if (runtime && typeof runtime === "object") model.runtime = runtime;
  }
  if (runtime && typeof runtime === "object") {
    if (!state.multiModelApiAvailable || resolvedModelId === DEFAULT_AI_MODEL_ID) {
      state.aiRuntime = runtime;
    }
    if (!state.multiModelApiAvailable) state.aiEnhanceReady = Boolean(runtime.ready);
  }
  state.modelDownloadRequestError = "";
  renderModelManager();
  renderAiTargetOptions();
  renderControls();
}

function stopModelDownloadPolling() {
  window.clearTimeout(state.modelDownloadPollTimer);
  state.modelDownloadPollTimer = null;
  state.modelDownloadPollSerial += 1;
}

function scheduleModelDownloadPoll(pollSerial, delay = MODEL_DOWNLOAD_POLL_MS) {
  if (!aiFeaturesAvailable()) return;
  window.clearTimeout(state.modelDownloadPollTimer);
  state.modelDownloadPollTimer = window.setTimeout(() => pollModelDownload(pollSerial), delay);
}

function startModelDownloadPolling(delay = 0, modelId = state.modelDownloadModelId) {
  if (!aiFeaturesAvailable()) return;
  stopModelDownloadPolling();
  state.modelDownloadModelId = modelId;
  const pollSerial = state.modelDownloadPollSerial;
  scheduleModelDownloadPoll(pollSerial, delay);
}

async function pollModelDownload(pollSerial) {
  if (!aiFeaturesAvailable() || pollSerial !== state.modelDownloadPollSerial) return;
  const wasActive = modelDownloadIsActive();
  const modelId = state.modelDownloadModelId || state.selectedAiModelId;
  try {
    const snapshot = state.multiModelApiAvailable
      ? await apiRequest(`/api/ai-models/${encodeURIComponent(modelId)}/download`)
      : await apiRequest("/api/ai-model-download");
    if (pollSerial !== state.modelDownloadPollSerial) return;
    state.modelDownloadPollFailures = 0;
    applyModelDownloadSnapshot(snapshot, modelId);
    if (modelDownloadIsActive(snapshot)) {
      scheduleModelDownloadPoll(pollSerial);
      return;
    }
    state.modelDownloadPollTimer = null;
    const finalStatus = String(snapshot.status || "").toLowerCase();
    if (wasActive && (modelRuntimePrepared() || finalStatus === "completed")) {
      showToast(`${selectedAiModel()?.name || "AI 模型"} 准备完成，以后可以直接开始超清`);
    } else if (wasActive && ["cancelled", "canceled"].includes(finalStatus)) {
      showToast("模型下载已暂停，下次可以继续");
    } else if (wasActive && ["failed", "error"].includes(finalStatus)) {
      showToast(snapshot.error || snapshot.message || "AI 模型准备失败", "error");
    }
  } catch (error) {
    if (pollSerial !== state.modelDownloadPollSerial) return;
    if (error instanceof ApiError && error.status >= 400 && error.status < 500) {
      stopModelDownloadPolling();
      state.modelDownloadRequestError = "本地服务连接已更新，请刷新页面后继续查看模型任务。";
      renderModelManager();
      elements.modelDownloadRemaining.textContent = "预计还需：请先刷新页面";
      setProgressAriaValue(
        elements.modelProgressTrack,
        elements.modelProgressTrack.getAttribute("aria-valuenow"),
        elements.modelDownloadRemaining.textContent,
      );
      showToast(state.modelDownloadRequestError, "error");
      return;
    }
    state.modelDownloadPollFailures += 1;
    elements.modelDownloadRemaining.textContent = "预计还需：等待重新连接…";
    setProgressAriaValue(
      elements.modelProgressTrack,
      elements.modelProgressTrack.getAttribute("aria-valuenow"),
      elements.modelDownloadRemaining.textContent,
    );
    elements.modelProgressMessage.textContent = state.modelDownloadPollFailures <= 4
      ? "连接短暂中断，正在重新获取下载进度…"
      : "暂时无法连接本地服务，下载可能仍在后台继续；正在自动重连。";
    scheduleModelDownloadPoll(
      pollSerial,
      Math.min(10000, 700 * Math.max(1, state.modelDownloadPollFailures)),
    );
  }
}

async function refreshModelDownloadStatus({ reconnect = true } = {}) {
  if (!aiFeaturesAvailable() || !state.appReady) {
    renderModelManager();
    return;
  }
  const model = selectedAiModel();
  const modelId = model?.id || state.selectedAiModelId;
  state.modelDownloadModelId = modelId;
  if (state.multiModelApiAvailable && !aiModelCanRun(model)) {
    applyModelDownloadSnapshot(initialModelDownloadSnapshot(model), modelId);
    return;
  }
  try {
    const snapshot = state.multiModelApiAvailable
      ? await apiRequest(`/api/ai-models/${encodeURIComponent(modelId)}/download`)
      : await apiRequest("/api/ai-model-download");
    applyModelDownloadSnapshot(snapshot, modelId);
    if (reconnect && modelDownloadIsActive(snapshot)) {
      startModelDownloadPolling(MODEL_DOWNLOAD_POLL_MS, modelId);
    }
  } catch (error) {
    state.modelDownloadRequestError = error.message || "无法读取 AI 模型状态";
    renderModelManager();
    elements.modelDownloadRemaining.textContent = "预计还需：暂时无法估算";
    setProgressAriaValue(
      elements.modelProgressTrack,
      elements.modelProgressTrack.getAttribute("aria-valuenow"),
      elements.modelDownloadRemaining.textContent,
    );
  }
}

async function startModelDownload() {
  const model = selectedAiModel();
  if (
    !aiFeaturesAvailable()
    || !state.appReady
    || state.modelDownloadStarting
    || state.modelDownloadCancelling
    || state.modelDeleteModelId
    || modelDownloadIsActive()
    || modelRuntimePrepared()
    || !aiModelCanRun(model)
  ) {
    return;
  }
  state.modelDownloadStarting = true;
  state.modelDownloadRequestError = "";
  state.modelDeleteError = "";
  renderModelManager();
  try {
    const modelId = model?.id || state.selectedAiModelId;
    const snapshot = state.multiModelApiAvailable
      ? await post(`/api/ai-models/${encodeURIComponent(modelId)}/download`)
      : await post("/api/ai-model-download");
    applyModelDownloadSnapshot(snapshot, modelId);
    if (modelDownloadIsActive(snapshot)) {
      startModelDownloadPolling(MODEL_DOWNLOAD_POLL_MS, modelId);
      showToast(`${model?.name || "AI 模型"} 已开始在后台准备`);
    } else if (modelRuntimePrepared()) {
      showToast(`${model?.name || "AI 模型"} 已经准备好了`);
    }
  } catch (error) {
    state.modelDownloadRequestError = error.message || "无法开始下载 AI 模型";
    showToast(state.modelDownloadRequestError, "error");
  } finally {
    state.modelDownloadStarting = false;
    renderModelManager();
  }
}

async function cancelModelDownload() {
  if (!aiFeaturesAvailable() || !modelDownloadIsActive() || state.modelDownloadCancelling || state.modelDeleteModelId) return;
  state.modelDownloadCancelling = true;
  let requestSucceeded = false;
  renderModelManager();
  try {
    const body = state.modelDownload.job_id ? { job_id: state.modelDownload.job_id } : undefined;
    const modelId = state.modelDownloadModelId || state.selectedAiModelId;
    const snapshot = state.multiModelApiAvailable
      ? await post(`/api/ai-models/${encodeURIComponent(modelId)}/download/cancel`, body)
      : await post("/api/ai-model-download/cancel", body);
    requestSucceeded = true;
    applyModelDownloadSnapshot(snapshot, modelId);
    if (modelDownloadIsActive(snapshot)) startModelDownloadPolling(150, modelId);
  } catch (error) {
    showToast(error.message || "取消模型下载失败", "error");
  } finally {
    if (!requestSucceeded || !modelDownloadIsActive()) state.modelDownloadCancelling = false;
    renderModelManager();
  }
}

function applyModelDeletionPayload(payload, modelId) {
  const catalog = payload?.catalog;
  if (catalog && typeof catalog === "object" && Array.isArray(catalog.models)) {
    if (catalog.ai_runtime && typeof catalog.ai_runtime === "object") {
      state.aiRuntime = catalog.ai_runtime;
    }
    applyAiModelCatalog(catalog);
  }

  const model = state.aiModels.find((item) => item.id === modelId);
  if (model) {
    model.downloaded = false;
    model.prepared = false;
    model.partial_bytes = 0;
  }
  if (modelId !== state.selectedAiModelId) return;

  const snapshot = payload?.download;
  if (snapshot && typeof snapshot === "object") {
    applyModelDownloadSnapshot(snapshot, modelId);
    return;
  }
  state.modelDownloadModelId = modelId;
  state.modelDownload = initialModelDownloadSnapshot(model || selectedAiModel());
}

async function deleteSelectedAiModel() {
  const model = selectedAiModel();
  const modelId = model?.id || "";
  if (
    !aiFeaturesAvailable()
    || !state.appReady
    || !state.multiModelApiAvailable
    || !modelId
    || state.modelDeleteModelId
    || state.modelDownloadStarting
    || state.modelDownloadCancelling
    || modelDownloadIsActive()
    || aiEnhancementIsActive()
    || model.offline_managed === true
    || !modelHasInstalledFiles(model)
  ) {
    return;
  }

  const confirmed = window.confirm(
    `确定删除 ${model.name} 吗？\n\n将删除这个模型的权重、校验缓存和未完成下载；已安装的 AI 运行环境会保留。以后使用时需要重新下载，此操作无法撤销。`,
  );
  if (!confirmed) return;

  state.modelDeleteModelId = modelId;
  state.modelDeleteError = "";
  renderModelManager();
  let payload = null;
  let deleteError = "";
  try {
    payload = await apiRequest(`/api/ai-models/${encodeURIComponent(modelId)}/download`, {
      method: "DELETE",
    });
  } catch (error) {
    deleteError = error.message || `无法删除 ${model.name}`;
  }

  if (payload) {
    applyModelDeletionPayload(payload, modelId);
    const removedBytes = Number(payload.deleted_bytes ?? payload.removed_bytes);
    showToast(Number.isFinite(removedBytes) && removedBytes > 0
      ? `${model.name} 已删除，已移除约 ${formatBytes(removedBytes)} 的本地模型文件`
      : `${model.name} 已从本机删除`);
  } else {
    state.modelDeleteError = deleteError;
    showToast(deleteError, "error");
  }

  try {
    await refreshAiModels({ preserveOnError: true });
  } catch (_error) {
    // The DELETE result is authoritative; a follow-up refresh is best-effort.
  } finally {
    state.modelDeleteModelId = "";
    state.modelDeleteError = deleteError;
    renderModelManager();
    renderControls();
  }
}

async function refreshAiModels({ preserveOnError = false } = {}) {
  if (!aiFeaturesAvailable() || !state.appReady) return;
  state.modelCatalogLoading = true;
  renderModelManager();
  let catalogRefreshFailed = false;
  try {
    const payload = await apiRequest("/api/ai-models");
    state.multiModelApiAvailable = true;
    if (payload.ai_runtime && typeof payload.ai_runtime === "object") {
      state.aiRuntime = payload.ai_runtime;
    }
    applyAiModelCatalog(payload);
    const activeModel = state.aiModels.find((model) => (
      ["queued", "running"].includes(String(model.download_status || "").toLowerCase())
    ));
    if (activeModel) {
      state.selectedAiModelId = activeModel.id;
      state.modelDownloadModelId = activeModel.id;
    }
  } catch (_error) {
    catalogRefreshFailed = true;
    if (!preserveOnError) {
      state.multiModelApiAvailable = false;
      applyAiModelCatalog({
        default_model_id: DEFAULT_AI_MODEL_ID,
        models: [{
          ...FALLBACK_AI_MODELS[0],
          compatible: state.aiEnhanceReady,
          runnable: state.aiEnhanceReady,
          runtime: state.aiRuntime,
          prepared: modelRuntimePrepared(state.aiRuntime, null),
          downloaded: Boolean(state.aiRuntime.models_downloaded),
        }],
      });
    }
  } finally {
    state.modelCatalogLoading = false;
  }
  if (catalogRefreshFailed && preserveOnError) {
    renderModelManager();
    return;
  }
  state.modelDownload = initialModelDownloadSnapshot(selectedAiModel());
  state.modelDownloadModelId = state.selectedAiModelId;
  selectDefaultAiTarget();
  renderModeCopy();
  renderModelManager();
  await refreshModelDownloadStatus();
}

function selectAiModel(modelId) {
  const normalized = String(modelId || "").toLowerCase();
  const model = state.aiModels.find((item) => item.id === normalized);
  if (
    !aiFeaturesAvailable()
    || !model
    || model.id === state.selectedAiModelId
    || state.exporting
    || state.modelDownloadStarting
    || state.modelDeleteModelId
    || modelDownloadIsActive()
    || state.modelCatalogLoading
  ) {
    return;
  }
  if (state.exportCompleted || state.exportError) resetExportResult();
  state.selectedAiModelId = model.id;
  state.modelDownloadModelId = model.id;
  state.modelDownload = initialModelDownloadSnapshot(model);
  state.modelDownloadRequestError = "";
  state.modelDeleteError = "";
  selectDefaultAiTarget();
  if (state.activeRange) state.activePreviewKey = previewKey(state.activeRange);
  renderModeCopy();
  renderRangeSummary(readRange(false));
  renderModelManager();
  renderControls();
  const reason = aiModelCompatibilityReason(model);
  showToast(reason ? `${model.name} 暂不可用：${reason}` : `已切换到 ${model.name}`);
  if (state.appReady && state.multiModelApiAvailable && aiModelCanRun(model)) {
    void refreshModelDownloadStatus();
  }
}

function workspaceFromLocation() {
  return aiFeaturesAvailable() && window.location.hash === "#ai-model" ? "model" : "video";
}

function applyAiFeatureAvailability(enabled) {
  state.aiFeaturesEnabled = enabled === true;
  state.aiFeaturesResolved = true;
  const available = aiFeaturesAvailable();

  elements.workspaceTabs.hidden = !available;
  elements.modelWorkspaceTab.hidden = !available;
  elements.modeEnhanceButton.hidden = !available;
  elements.modeEnhanceButton.disabled = !available;
  elements.modeSwitch.classList.toggle("is-basic-only", !available);
  elements.modelWorkspace.inert = !available;

  if (!available) {
    stopModelDownloadPolling();
    if (state.operation === "enhance") state.operation = "clip";
    if (window.location.hash === "#ai-model") {
      window.history.replaceState({}, "", "#video");
    }
  }
  switchWorkspace(workspaceFromLocation(), { updateHistory: false, refreshModel: false });
}

function switchWorkspace(workspace, { updateHistory = true, focus = false, refreshModel = true } = {}) {
  const nextWorkspace = aiFeaturesAvailable() && workspace === "model" ? "model" : "video";
  if (state.aiFeaturesResolved && !aiFeaturesAvailable() && window.location.hash === "#ai-model") {
    window.history.replaceState({}, "", "#video");
  }
  state.workspace = nextWorkspace;
  const modelSelected = nextWorkspace === "model";
  elements.videoWorkspaceTab.setAttribute("aria-selected", modelSelected ? "false" : "true");
  elements.videoWorkspaceTab.tabIndex = modelSelected ? -1 : 0;
  elements.modelWorkspaceTab.setAttribute("aria-selected", modelSelected ? "true" : "false");
  elements.modelWorkspaceTab.tabIndex = modelSelected ? 0 : -1;
  elements.videoWorkspace.hidden = modelSelected;
  elements.modelWorkspace.hidden = !modelSelected;
  if (updateHistory) {
    const hash = modelSelected ? "#ai-model" : "#video";
    if (window.location.hash !== hash) window.history.pushState({}, "", hash);
  }
  if (focus) {
    (modelSelected ? elements.modelWorkspaceTab : elements.videoWorkspaceTab).focus();
  }
  if (modelSelected) {
    renderModelManager();
    if (refreshModel && state.appReady && !modelDownloadIsActive()) void refreshModelDownloadStatus();
  }
}

function handleWorkspaceTabKeydown(event) {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const showModel = event.key === "ArrowRight" || event.key === "End";
  switchWorkspace(showModel ? "model" : "video", { focus: true });
}

function exportApiBase() {
  if (isFrameMode()) return "/api/frame-exports";
  if (isRotateMode()) return "/api/rotations";
  if (isEnhanceMode()) return "/api/ai-enhancements";
  return "/api/exports";
}

function setExportProgress(value, remainingText = "") {
  const percentage = normalizeProgress(value);
  const rounded = Math.round(percentage);
  elements.exportProgressValue.textContent = `${rounded}%`;
  elements.exportProgressBar.style.width = `${percentage}%`;
  elements.exportProgressTrack.setAttribute("aria-valuenow", String(rounded));
  setProgressAriaValue(elements.exportProgressTrack, percentage, remainingText);
}

function showExportProgress() {
  const model = selectedAiModel();
  const modelSize = Math.max(0, Number(model?.download_size_bytes) || 0);
  elements.exportSetup.hidden = true;
  elements.exportCompletePanel.hidden = true;
  elements.exportProgressPanel.hidden = false;
  elements.exportProgressPanel.classList.remove("is-error");
  elements.cancelExportButton.hidden = false;
  elements.cancelExportButton.disabled = false;
  elements.cancelExportButton.textContent = `取消${operationVerb()}`;
  elements.exportProgressKicker.textContent = "正在准备";
  elements.exportProgressTitle.textContent = isEnhanceMode()
    ? "正在准备 AI 超清…"
    : isRotateMode()
    ? "正在永久旋转视频…"
    : isFrameMode()
      ? "正在逐帧截图…"
      : "正在导出视频…";
  elements.exportProgressMessage.textContent = isEnhanceMode()
    ? modelRuntimePrepared()
      ? `${model?.name || "AI 模型"} 已经提前准备完成，正在加载并开始整段 AI 计算。`
      : model?.id === DEFAULT_AI_MODEL_ID
        ? `首次使用会准备环境并下载${modelSize > 0 ? `约 ${formatBytes(modelSize)} 的` : "所选"}模型，之后开始整段 AI 计算。`
        : `${model?.name || "所选模型"} 尚未准备完成，请先到 AI 模型管理下载。`
    : isRotateMode()
    ? "正在准备同级目录中的新视频，请不要关闭此页面。"
    : isFrameMode()
      ? "正在准备独立截图文件夹，请不要关闭此页面。"
      : "正在安全地创建新文件，请不要关闭此页面。";
  elements.exportElapsed.textContent = "";
  elements.exportRemaining.textContent = "预计还需：正在估算…";
  setExportProgress(0, elements.exportRemaining.textContent);
}

async function startExport() {
  const range = readRange(true);
  if (!canExport(range)) return;

  cancelPendingPreview();
  state.exporting = true;
  state.cancellingExport = false;
  state.exportCompleted = false;
  state.exportError = "";
  state.exportPollFailures = 0;
  state.exportPollSerial += 1;
  const pollSerial = state.exportPollSerial;
  showExportProgress();
  renderControls();
  if (isEnhanceMode()) renderModelManager();

  try {
    const body = isEnhanceMode()
      ? {
          video_id: state.video.id,
          target: state.enhanceTarget,
          model_id: state.selectedAiModelId,
        }
      : isRotateMode()
      ? { video_id: state.video.id, degrees: state.rotationDegrees }
      : { video_id: state.video.id, start: range.start, end: range.end };
    const result = await post(exportApiBase(), body);
    if (!result.job_id) throw new Error("本地服务没有创建导出任务");
    state.exportJobId = result.job_id;
    state.exportOutputName = result.output_name || makeSuggestedOutputName(range);
    await pollExport(pollSerial);
  } catch (error) {
    if (pollSerial !== state.exportPollSerial) return;
    finishExportWithError(error.message || `${operationLabel()}任务启动失败`);
  }
}

function scheduleExportPoll(pollSerial, delay = EXPORT_POLL_MS) {
  window.clearTimeout(state.exportPollTimer);
  state.exportPollTimer = window.setTimeout(() => pollExport(pollSerial), delay);
}

async function pollExport(pollSerial) {
  if (!state.exportJobId || pollSerial !== state.exportPollSerial) return;

  try {
    const job = await apiRequest(`${exportApiBase()}/${encodeURIComponent(state.exportJobId)}`);
    if (pollSerial !== state.exportPollSerial) return;
    state.exportPollFailures = 0;

    const status = String(job.status || "running").toLowerCase();
    const stage = String(job.stage || job.phase || status).toLowerCase();
    const progress = normalizeProgress(job.progress);
    const exportRemainingText = remainingTimeText(job, {
      cancelling: state.cancellingExport,
    });
    setExportProgress(progress, exportRemainingText);
    elements.exportElapsed.textContent = Number.isFinite(Number(job.elapsed_seconds))
      ? formatElapsed(job.elapsed_seconds)
      : "";
    elements.exportRemaining.textContent = exportRemainingText;

    if (["queued", "pending", "waiting"].includes(status)) {
      elements.exportProgressKicker.textContent = "等待处理";
      elements.exportProgressTitle.textContent = isEnhanceMode()
        ? "正在排队准备 AI 超清…"
        : isRotateMode()
        ? "正在准备旋转…"
        : isFrameMode()
          ? "正在准备截图…"
          : "正在准备导出…";
      elements.exportProgressMessage.textContent = job.message || "正在检查视频和保存位置。";
      scheduleExportPoll(pollSerial, 800);
      return;
    }

    if (
      ["running", "processing", "exporting"].includes(status)
      || (isEnhanceMode() && ["setup", "installing", "downloading", "inference", "remux", "verify", "verifying"].includes(status))
    ) {
      const aiModelName = String(job.model_name || job.model || selectedAiModel()?.name || "AI 模型");
      const aiModelBytes = Math.max(0, Number(selectedAiModel()?.download_size_bytes) || 0);
      const aiModelSize = aiModelBytes > 0 ? `约 ${formatBytes(aiModelBytes)} 的` : "";
      const aiStageCopy = {
        inspect: ["检查原片", "正在核对逐帧时间戳…", "正在确认帧数和时间轴连续，避免长视频音画不同步。"],
        setup: ["首次准备", "正在准备 AI 运行环境…", "正在安装本地 AI 运行环境。"],
        installing: ["首次准备", "正在安装 AI 依赖…", "正在安装本地 AI 运行环境。"],
        download: ["下载模型", `正在下载 ${aiModelName}…`, `正在下载并校验${aiModelSize}模型文件。`],
        downloading: ["下载模型", `正在下载 ${aiModelName}…`, `正在下载并校验${aiModelSize}模型文件。`],
        inference: ["AI 计算", `正在增强到 ${aiTargetLabel()}…`, `${aiModelName} 正在逐帧恢复细节，耗时可能很长。`],
        remux: ["封装成片", "正在复制原音频…", "画面已完成，正在无损复制原视频音频。"],
        verify: ["检查成片", "正在验证输出文件…", "正在检查分辨率、时长、编码和音频。"],
        verifying: ["检查成片", "正在验证输出文件…", "正在检查分辨率、时长、编码和音频。"],
      };
      const aiCopy = aiStageCopy[stage] || ["本机处理中", "正在进行 AI 超清…", `${aiModelName} 正在本机处理整段视频。`];
      elements.exportProgressKicker.textContent = isEnhanceMode() ? aiCopy[0] : "本机处理中";
      elements.exportProgressTitle.textContent = isEnhanceMode()
        ? aiCopy[1]
        : isRotateMode()
        ? "正在永久旋转视频…"
        : isFrameMode()
          ? "正在逐帧截图…"
          : "正在导出视频…";
      elements.exportProgressMessage.textContent = job.message || (isEnhanceMode()
        ? aiCopy[2]
        : isRotateMode()
        ? "正在将方向写入画面并原样复制音频。"
        : isFrameMode()
          ? "正在按原始宽高保存无损图片。"
          : "正在保持原分辨率与高品质音频创建新文件。");
      scheduleExportPoll(pollSerial);
      return;
    }

    if (["completed", "complete", "done", "success", "succeeded"].includes(status)) {
      finishExportSuccessfully(job);
      return;
    }

    if (["cancelled", "canceled"].includes(status)) {
      finishCancelledExport();
      return;
    }

    if (["error", "failed", "failure"].includes(status)) {
      finishExportWithError(
        job.error || job.message || `${operationLabel()}失败，请重试。`,
        job,
      );
      return;
    }

    elements.exportProgressMessage.textContent = job.message || "正在处理，请稍候。";
    scheduleExportPoll(pollSerial);
  } catch (error) {
    if (pollSerial !== state.exportPollSerial) return;
    if (error instanceof ApiError && error.status >= 400 && error.status < 500) {
      finishExportWithError(error.message || "本地服务已找不到这次任务");
      return;
    }
    state.exportPollFailures += 1;
    elements.exportRemaining.textContent = "预计还需：等待重新连接…";
    setProgressAriaValue(
      elements.exportProgressTrack,
      elements.exportProgressTrack.getAttribute("aria-valuenow"),
      elements.exportRemaining.textContent,
    );
    if (state.exportPollFailures <= 4) {
      elements.exportProgressMessage.textContent = "连接短暂中断，正在重新连接本地服务…";
      scheduleExportPoll(pollSerial, Math.min(3000, 700 * state.exportPollFailures));
      return;
    }
    elements.exportProgressMessage.textContent = isEnhanceMode()
      ? "暂时无法连接本地服务，AI 任务可能仍在后台运行；正在继续重连，也可以点击取消。"
      : "暂时无法连接本地服务，任务可能仍在后台运行；正在继续重连，也可以点击取消。";
    scheduleExportPoll(pollSerial, Math.min(10000, 3000 + state.exportPollFailures * 500));
  }
}

function stopExportPolling() {
  window.clearTimeout(state.exportPollTimer);
  state.exportPollTimer = null;
  state.exportPollSerial += 1;
}

function finishExportSuccessfully(job) {
  stopExportPolling();
  state.exporting = false;
  state.cancellingExport = false;
  state.exportCompleted = true;
  state.exportError = "";
  state.exportOutputName = job.output_name || state.exportOutputName || (isEnhanceMode()
    ? "AI 超清视频"
    : isRotateMode()
      ? "永久旋转视频"
      : isFrameMode()
        ? "逐帧截图"
        : "剪辑视频");

  setExportProgress(100);
  elements.exportProgressPanel.hidden = true;
  elements.exportSetup.hidden = true;
  elements.exportCompletePanel.hidden = false;
  elements.completedOutputName.textContent = state.exportOutputName;
  const completedPath = job.output_path || (usesSourceDirectory()
    ? state.video && state.video.directory_display
    : state.outputDirectory) || "";
  elements.completedOutputPath.textContent = completedPath;
  elements.completedOutputPath.title = completedPath;
  const frameCount = Number(job.frame_count);
  elements.completionSummary.textContent = isEnhanceMode()
    ? `AI 超清到 ${aiTargetLabel()} 完成，原视频未被修改`
    : isRotateMode()
    ? `永久旋转 ${state.rotationDegrees}° 完成，原视频未被修改`
    : isFrameMode()
      ? Number.isFinite(frameCount) && frameCount > 0
        ? `截图完成，共保存 ${frameCount} 张原尺寸图片`
        : "截图完成，原视频未被修改"
      : "剪辑完成，原视频未被修改";
  showToast(isEnhanceMode()
    ? "AI 超清完成，已在原视频同级目录保存为新视频"
    : isRotateMode()
    ? "永久旋转完成，已在原视频同级目录生成新文件"
    : isFrameMode()
      ? "逐帧截图完成，已保存到独立文件夹"
      : "剪辑完成，已保存为新文件");
  renderControls();
  if (isEnhanceMode()) renderModelManager();
  if (isEnhanceMode()) void refreshModelDownloadStatus({ reconnect: false });
}

function finishCancelledExport() {
  stopExportPolling();
  state.exporting = false;
  state.cancellingExport = false;
  state.exportCompleted = false;
  state.exportError = "";
  state.exportJobId = "";
  elements.exportProgressPanel.hidden = true;
  elements.exportSetup.hidden = false;
  showToast(isEnhanceMode()
    ? "已取消 AI 超清，未完成文件已清理"
    : isRotateMode()
    ? "已取消旋转，未完成视频已清理"
    : isFrameMode()
      ? "已取消截图，未完成图片已清理"
      : "已取消导出，原视频未被修改");
  renderControls();
  if (isEnhanceMode()) renderModelManager();
}

function finishExportWithError(message, job = null) {
  stopExportPolling();
  state.exporting = false;
  state.cancellingExport = false;
  state.exportCompleted = false;
  state.exportError = typeof message === "string" ? message : extractErrorMessage(message) || "导出失败，请重试。";

  elements.exportSetup.hidden = false;
  elements.exportProgressPanel.hidden = false;
  elements.exportProgressPanel.classList.add("is-error");
  elements.cancelExportButton.hidden = true;
  elements.exportProgressKicker.textContent = `${operationLabel()}未完成`;
  elements.exportProgressTitle.textContent = `${operationLabel()}失败`;
  elements.exportProgressMessage.textContent = `${state.exportError} 原视频未被修改。`;
  elements.exportRemaining.textContent = "";
  setProgressAriaValue(
    elements.exportProgressTrack,
    elements.exportProgressTrack.getAttribute("aria-valuenow"),
  );
  if (job && Number.isFinite(Number(job.elapsed_seconds))) {
    elements.exportElapsed.textContent = formatElapsed(job.elapsed_seconds);
  }
  showToast(state.exportError, "error");
  renderControls();
  if (isEnhanceMode()) renderModelManager();
}

async function cancelExport() {
  if (!state.exporting || !state.exportJobId || state.cancellingExport) return;
  state.cancellingExport = true;
  elements.cancelExportButton.disabled = true;
  elements.cancelExportButton.textContent = "正在取消…";
  elements.exportRemaining.textContent = "预计还需：正在停止…";
  setProgressAriaValue(
    elements.exportProgressTrack,
    elements.exportProgressTrack.getAttribute("aria-valuenow"),
    elements.exportRemaining.textContent,
  );
  elements.exportProgressMessage.textContent = isEnhanceMode()
    ? "正在停止 AI 计算并清理未完成文件；已下载的模型会保留供下次使用…"
    : isRotateMode()
    ? "正在安全停止旋转并清理未完成视频…"
    : isFrameMode()
      ? "正在安全停止截图并清理未完成图片…"
      : "正在安全停止导出并清理未完成文件…";

  try {
    await post(`${exportApiBase()}/${encodeURIComponent(state.exportJobId)}/cancel`);
    scheduleExportPoll(state.exportPollSerial, 150);
  } catch (error) {
    state.cancellingExport = false;
    elements.cancelExportButton.disabled = false;
    elements.cancelExportButton.textContent = `取消${operationVerb()}`;
    showToast(error.message || "取消失败，请稍后重试", "error");
  }
}

async function revealOutput() {
  if (!state.exportJobId) return;
  elements.revealOutputButton.disabled = true;
  try {
    await post(`${exportApiBase()}/${encodeURIComponent(state.exportJobId)}/reveal`);
    showToast(isEnhanceMode()
      ? "已在文件夹中显示 AI 超清视频"
      : isRotateMode()
      ? "已在文件夹中显示旋转后的视频"
      : isFrameMode()
        ? "已在文件夹中显示截图"
        : "已在文件夹中显示导出文件");
  } catch (error) {
    showToast(error.message || (isFrameMode()
      ? "无法在文件夹中显示截图"
      : "无法在文件夹中显示文件"), "error");
  } finally {
    elements.revealOutputButton.disabled = false;
  }
}

function continueEditing() {
  state.exportCompleted = false;
  state.exportError = "";
  state.exportJobId = "";
  state.exportOutputName = "";
  elements.exportCompletePanel.hidden = true;
  elements.exportProgressPanel.hidden = true;
  elements.exportSetup.hidden = false;
  renderControls();
  if (isRotateMode()) elements.rotationOptions.find((option) => option.getAttribute("aria-pressed") === "true")?.focus();
  else if (isEnhanceMode()) elements.aiTargetOptions.find((option) => option.getAttribute("aria-pressed") === "true")?.focus();
  else elements.startTime.focus();
  elements.trimCard.scrollIntoView({ behavior: "smooth", block: "center" });
}

function canExport(range = readRange(false)) {
  const exporterReady = isEnhanceMode()
    ? aiModelReadyToEnhance()
    : isRotateMode()
    ? state.rotationReady
    : isFrameMode()
      ? state.frameExportReady
      : state.ffmpegReady;
  const destinationReady = isEnhanceMode()
    ? Boolean(state.video)
    : isRotateMode()
      ? Boolean(state.video && state.video.directory_display)
      : Boolean(state.outputDirectory);
  const targetReady = aiSelectionReady();
  return Boolean(
    state.appReady
      && exporterReady
      && state.video
      && targetReady
      && range.valid
      && state.previewReady
      && destinationReady
      && !state.selectingVideo
      && !state.selectingDirectory
      && !state.exporting
      && !(isEnhanceMode() && state.modelDeleteModelId)
      && !(isEnhanceMode() && modelDownloadIsActive()),
  );
}

function renderReadyNote(range) {
  const aiModel = selectedAiModel();
  const aiRuntime = selectedAiRuntime();
  const aiModelReason = aiModelCompatibilityReason(aiModel);
  elements.readyNote.classList.remove("is-ready", "is-error");
  if (state.exportError) {
    elements.readyNote.classList.add("is-error");
    elements.readyNoteText.textContent = `上次${operationLabel()}未完成，检查提示后可重试`;
  } else if (!state.appReady) {
    elements.readyNoteText.textContent = "正在连接本地服务…";
  } else if (!state.video) {
    elements.readyNoteText.textContent = isEnhanceMode()
      ? "请先选择视频和目标清晰度"
      : isRotateMode()
      ? "请先选择视频和旋转角度"
      : isFrameMode()
        ? "请先选择视频并设置截图范围"
        : "请先选择视频并设置剪辑范围";
  } else if (isEnhanceMode() && state.modelDeleteModelId) {
    elements.readyNoteText.textContent = "正在删除所选 AI 模型，完成后可以重新下载。";
  } else if (isEnhanceMode() && modelDownloadIsActive()) {
    elements.readyNoteText.textContent = "AI 模型正在后台准备，完成后即可开始超清";
  } else if (isEnhanceMode() && !aiSelectionReady()) {
    if (!aiModelCanRun(aiModel)) {
      elements.readyNote.classList.add("is-error");
      elements.readyNoteText.textContent = aiModelReason
        || String(aiRuntime.message || aiRuntime.reason || "当前电脑没有可用的 AI 加速运行环境");
    } else if (!aiModelReadyToEnhance(aiModel)) {
      elements.readyNoteText.textContent = `${aiModelPreparationReason(aiModel)}，完成后即可开始超清`;
    } else elements.readyNoteText.textContent = "当前视频没有可用目标；AI 超清不会把原片降级到更低分辨率";
  } else if (!range.valid) {
    elements.readyNoteText.textContent = "请先修正起始时间和结束时间";
  } else if (!state.previewReady) {
    elements.readyNoteText.textContent = "请等待新预览准备完成";
  } else if (!usesSourceDirectory() && !state.outputDirectory) {
    elements.readyNoteText.textContent = "请选择一个保存目录";
  } else if (!(isEnhanceMode() ? aiModelReadyToEnhance(aiModel) : isRotateMode() ? state.rotationReady : isFrameMode() ? state.frameExportReady : state.ffmpegReady)) {
    elements.readyNote.classList.add("is-error");
    elements.readyNoteText.textContent = isEnhanceMode()
      ? aiModelReason || String(aiRuntime.message || aiRuntime.reason || "当前电脑没有可用的 AI 加速运行环境")
      : isRotateMode()
      ? "当前 FFmpeg 缺少永久旋转所需编码器"
      : isFrameMode()
        ? "当前 FFmpeg 缺少逐帧截图所需编码器"
        : "未检测到 FFmpeg，暂时无法导出";
  } else {
    elements.readyNote.classList.add("is-ready");
    elements.readyNoteText.textContent = isEnhanceMode()
      ? state.video.is_hdr
        ? `已就绪；将 HDR 映射为 BT.709 SDR 后，再用 ${aiModel?.name || "所选模型"} 增强到 ${aiTargetLabel()}；成片保存到原视频同级目录，原片不会修改`
        : `已就绪，将用 ${aiModel?.name || "所选模型"} 把整段视频增强到 ${aiTargetLabel()}，并保存到原视频同级目录`
      : isRotateMode()
      ? `已就绪，将把整段视频顺时针永久旋转 ${state.rotationDegrees}°`
      : isFrameMode()
        ? "已就绪，将按原始尺寸保存所选范围内的每一帧"
        : `已就绪，将导出 ${formatTime(range.end - range.start)} 的片段`;
  }
}

function renderJourney(range) {
  for (const item of [elements.journeySource, elements.journeyTrim, elements.journeyExport]) {
    item.classList.remove("is-active", "is-complete");
  }

  if (!state.video) {
    elements.journeySource.classList.add("is-active");
    return;
  }

  elements.journeySource.classList.add("is-complete");
  const configurationReady = range.valid && state.previewReady && aiSelectionReady();
  if (!configurationReady) {
    elements.journeyTrim.classList.add("is-active");
    return;
  }

  elements.journeyTrim.classList.add("is-complete");
  if (state.exportCompleted) elements.journeyExport.classList.add("is-complete");
  else elements.journeyExport.classList.add("is-active");
}

function renderCards(range) {
  const configurationReady = Boolean(range.valid && state.previewReady && aiSelectionReady());
  elements.sourceCard.classList.toggle("is-active", !state.video);
  elements.trimCard.classList.toggle("is-disabled", !state.video);
  elements.trimCard.classList.toggle("is-active", Boolean(state.video && !configurationReady));
  elements.trimCard.setAttribute("aria-disabled", state.video ? "false" : "true");
  elements.exportCard.classList.toggle("is-disabled", !state.video || !configurationReady);
  elements.exportCard.classList.toggle("is-active", Boolean(state.video && configurationReady && !state.exportCompleted));
  elements.exportCard.setAttribute("aria-disabled", state.video && configurationReady ? "false" : "true");
}

function renderControls() {
  const range = readRange(false);
  const controlsLocked = state.exporting;

  elements.modeClipButton.disabled = controlsLocked || state.selectingVideo;
  elements.modeFramesButton.disabled = controlsLocked || state.selectingVideo;
  elements.modeRotateButton.disabled = controlsLocked || state.selectingVideo;
  elements.modeEnhanceButton.disabled = !aiFeaturesAvailable() || controlsLocked || state.selectingVideo;
  for (const option of elements.rotationOptions) {
    option.disabled = !state.video || controlsLocked || state.selectingVideo;
  }
  elements.selectVideoButton.disabled = !state.appReady || state.selectingVideo || controlsLocked;
  elements.replaceVideoButton.disabled = !state.appReady || state.selectingVideo || controlsLocked;
  elements.startTime.disabled = !state.video || controlsLocked || state.selectingVideo || isRotateMode() || isEnhanceMode();
  elements.endTime.disabled = !state.video || controlsLocked || state.selectingVideo || isRotateMode() || isEnhanceMode();
  elements.selectDirectoryButton.disabled = usesSourceDirectory() || !state.appReady || !state.video || state.selectingDirectory || controlsLocked;
  elements.exportButton.disabled = !canExport(range);
  elements.exportButton.setAttribute("aria-busy", state.exporting ? "true" : "false");
  renderAiTargetOptions(controlsLocked);

  const exportLabel = elements.exportButton.querySelector(".button-label");
  if (exportLabel) {
    exportLabel.textContent = state.exportError
      ? isEnhanceMode()
        ? "重新增强"
        : isRotateMode()
        ? "重新旋转"
        : isFrameMode()
          ? "重新截图"
          : "重新导出"
      : isEnhanceMode()
        ? "确定并开始 AI 超清"
        : isRotateMode()
        ? "确定并旋转"
        : isFrameMode()
          ? "确定并截图"
          : "确定并导出";
  }

  renderReadyNote(range);
  renderJourney(range);
  renderCards(range);
}

async function bootstrap() {
  renderControls();
  renderModelManager();
  renderAiProxySettings();
  setPreviewStatus("", "等待选择视频");

  try {
    const result = await apiRequest("/api/bootstrap");
    state.appToken = result.app_token || "";
    state.appReady = Boolean(state.appToken);
    state.ffmpegReady = Boolean(result.ffmpeg_ready);
    state.frameExportReady = Boolean(result.frame_export_ready);
    state.rotationReady = Boolean(result.rotation_ready);
    const aiFeaturesEnabled = result.ai_features_enabled === true;
    state.aiEnhanceReady = aiFeaturesEnabled && Boolean(result.ai_enhance_ready);
    state.aiRuntime = result.ai_runtime && typeof result.ai_runtime === "object" ? result.ai_runtime : {};
    const maxFrameSeconds = Number(result.max_frame_seconds);
    state.maxFrameSeconds = Number.isFinite(maxFrameSeconds) && maxFrameSeconds > 0
      ? maxFrameSeconds
      : DEFAULT_MAX_FRAME_SECONDS;
    state.outputDirectory = normalizeDirectory(result.output_directory);
    applyAiFeatureAvailability(aiFeaturesEnabled);

    const appName = result.app_name || "本地视频剪辑";
    elements.appName.textContent = appName;
    document.title = appName;
    elements.versionLabel.textContent = result.version ? `${appName} v${result.version}` : appName;
    renderOutputDirectory();
    renderModeCopy();
    renderModelManager();

    if (!state.appToken) {
      showSystemBanner("本地服务响应异常", "请刷新页面；如果仍未恢复，请重新运行项目的启动入口。", "error");
    } else if (!state.ffmpegReady && !state.frameExportReady && !state.rotationReady) {
      showSystemBanner("未检测到 FFmpeg", "可以先选择和预览视频，但需要按启动窗口提示安装 FFmpeg 后才能导出。", "warning");
    } else if (!state.ffmpegReady || !state.frameExportReady || !state.rotationReady) {
      showSystemBanner("部分功能暂不可用", "当前 FFmpeg 只支持页面中的部分操作；不可用的功能会在确认按钮旁提示。", "warning");
    } else {
      hideSystemBanner();
    }
    if (aiFeaturesAvailable()) {
      await refreshAiProxySettings();
      await refreshAiModels();
    }
  } catch (error) {
    state.appReady = false;
    elements.versionLabel.textContent = "本地服务未连接";
    showSystemBanner("无法连接本地服务", error.message || "请确认启动窗口仍在运行，然后刷新页面。", "error");
    state.modelDownloadRequestError = error.message || "无法连接本地服务";
    renderModelManager();
  } finally {
    renderControls();
  }
}

elements.videoWorkspaceTab.addEventListener("click", () => switchWorkspace("video"));
elements.modelWorkspaceTab.addEventListener("click", () => switchWorkspace("model"));
elements.videoWorkspaceTab.addEventListener("keydown", handleWorkspaceTabKeydown);
elements.modelWorkspaceTab.addEventListener("keydown", handleWorkspaceTabKeydown);
elements.openModelManagerButton.addEventListener("click", () => {
  if (!aiFeaturesAvailable()) return;
  switchWorkspace("model", { focus: true });
  elements.modelWorkspace.scrollIntoView({ behavior: "smooth", block: "start" });
});
elements.modelDownloadButton.addEventListener("click", startModelDownload);
elements.cancelModelDownloadButton.addEventListener("click", cancelModelDownload);
elements.modelDeleteButton.addEventListener("click", deleteSelectedAiModel);
elements.aiProxyForm.addEventListener("submit", saveAiProxySettings);
elements.aiProxyTestButton.addEventListener("click", testAiProxyDraft);
elements.aiProxyClearButton.addEventListener("click", clearAiProxySettings);
elements.aiProxyUrl.addEventListener("input", markAiProxyDraftChanged);
elements.aiProxyUsername.addEventListener("input", markAiProxyDraftChanged);
elements.aiProxyPassword.addEventListener("input", markAiProxyDraftChanged);
elements.aiProxyClearPassword.addEventListener("change", handleAiProxyClearPasswordChange);
function handleAiModelSelection(event) {
  if (!aiFeaturesAvailable()) return;
  const option = event.target.closest("[data-model-id]");
  if (!option || !event.currentTarget.contains(option) || option.disabled) return;
  selectAiModel(option.dataset.modelId);
}
elements.aiModelOptions.addEventListener("click", handleAiModelSelection);
elements.modelSelector.addEventListener("click", handleAiModelSelection);
elements.selectVideoButton.addEventListener("click", selectVideo);
elements.replaceVideoButton.addEventListener("click", selectVideo);
elements.modeClipButton.addEventListener("click", () => changeOperation("clip"));
elements.modeFramesButton.addEventListener("click", () => changeOperation("frames"));
elements.modeRotateButton.addEventListener("click", () => changeOperation("rotate"));
elements.modeEnhanceButton.addEventListener("click", () => changeOperation("enhance"));
elements.selectDirectoryButton.addEventListener("click", selectOutputDirectory);
elements.exportButton.addEventListener("click", startExport);
elements.cancelExportButton.addEventListener("click", cancelExport);
elements.revealOutputButton.addEventListener("click", revealOutput);
elements.continueButton.addEventListener("click", continueEditing);
elements.reloadButton.addEventListener("click", () => window.location.reload());
elements.retryPreviewButton.addEventListener("click", () => {
  if (isRotateMode()) {
    prepareRotationPreview({ autoplay: true });
    return;
  }
  if (isEnhanceMode()) {
    prepareEnhancePreview({ autoplay: true });
    return;
  }
  const range = readRange(true);
  if (range.valid) requestPreview(range, { fallback: true, autoplay: true });
});

for (const option of elements.rotationOptions) {
  option.addEventListener("click", () => {
    if (!isRotateMode() || state.exporting || state.selectingVideo) return;
    const degrees = Number(option.dataset.degrees);
    if (![90, 180, 270, 360].includes(degrees) || degrees === state.rotationDegrees) return;
    if (state.exportCompleted || state.exportError) resetExportResult();
    state.rotationDegrees = degrees;
    if (state.activeRange) state.activePreviewKey = previewKey(state.activeRange);
    renderModeCopy();
    const range = readRange(false);
    renderRangeSummary(range);
    applyRotationPreview();
    if (state.previewReady) {
      setPreviewStatus("ready", `已实时预览顺时针 ${degrees}°，点击画面可播放或暂停`);
    }
    renderControls();
    showToast(`已改为顺时针 ${degrees}°`);
  });
}

for (const option of elements.aiTargetOptions) {
  option.addEventListener("click", () => {
    if (!isEnhanceMode() || state.exporting || state.selectingVideo || option.disabled) return;
    const target = String(option.dataset.target || "").toLowerCase();
    const availability = videoAiTargets().find((item) => item.target === target);
    if (!availability?.available || target === state.enhanceTarget) return;
    if (state.exportCompleted || state.exportError) resetExportResult();
    state.enhanceTarget = target;
    if (state.activeRange) state.activePreviewKey = previewKey(state.activeRange);
    renderModeCopy();
    renderRangeSummary(readRange(false));
    if (state.previewReady) {
      setPreviewStatus("ready", "当前播放整段原片，仅供确认内容；AI 成片需完整计算后查看");
    }
    renderControls();
    showToast(`AI 超清目标已改为 ${aiTargetLabel()}`);
  });
}

for (const input of [elements.startTime, elements.endTime]) {
  input.addEventListener("input", handleTimeInput);
  input.addEventListener("blur", handleTimeBlur);
  input.addEventListener("keydown", handleTimeKeydown);
}

elements.previewVideo.addEventListener("play", () => {
  const bounds = currentPlaybackBounds();
  if (bounds) {
    const current = elements.previewVideo.currentTime;
    if (current < bounds.start || current >= bounds.end) seekToPreviewStart(false);
  }
  startPlaybackMonitor();
});
elements.previewVideo.addEventListener("pause", stopPlaybackMonitor);
elements.previewVideo.addEventListener("timeupdate", enforcePreviewBounds);
elements.previewVideo.addEventListener("ended", () => seekToPreviewStart(true));
elements.previewVideo.addEventListener("loadedmetadata", applyRotationPreview);
elements.previewVideo.addEventListener("click", () => {
  if (!isRotateMode()) return;
  if (elements.previewVideo.paused) {
    const playPromise = elements.previewVideo.play();
    if (playPromise && typeof playPromise.catch === "function") playPromise.catch(() => {});
  } else {
    elements.previewVideo.pause();
  }
});
elements.previewVideo.addEventListener("keydown", (event) => {
  if (!isRotateMode() || !["Enter", " "].includes(event.key)) return;
  event.preventDefault();
  elements.previewVideo.click();
});
elements.previewVideo.addEventListener("error", () => {
  if (state.mediaLoading || !state.video || !state.activeRange) return;
  if (!state.mediaFallbackAttempted && !isClipPreview(state.previewMode)) {
    state.mediaFallbackAttempted = true;
    const range = isRotateMode() ? rotationFallbackRange() : { ...state.activeRange, valid: true };
    requestPreview(range, {
      fallback: true,
      autoplay: false,
      previewOperation: isRotateMode() ? "frames" : isEnhanceMode() ? "clip" : undefined,
    });
  } else {
    state.previewReady = false;
    setPreviewLoading(false);
    setPreviewStatus("error", "浏览器无法播放这个预览", true);
    renderControls();
  }
});

window.addEventListener("resize", applyRotationPreview);
window.addEventListener("popstate", () => {
  if (!["#ai-model", "#video"].includes(window.location.hash)) return;
  switchWorkspace(workspaceFromLocation(), { updateHistory: false });
});
window.addEventListener("beforeunload", (event) => {
  if (!state.exporting && !modelDownloadIsActive() && !state.modelDeleteModelId) return;
  event.preventDefault();
  event.returnValue = "";
});

switchWorkspace(workspaceFromLocation(), { updateHistory: false });
bootstrap();
