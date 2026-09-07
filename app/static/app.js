"use strict";

const PREVIEW_DEBOUNCE_MS = 500;
const EXPORT_POLL_MS = 650;
const MEDIA_LOAD_TIMEOUT_MS = 20000;
const DEFAULT_MAX_FRAME_SECONDS = 5;
const AI_TARGETS = {
  "1080p": { label: "1080p", dimensions: "1920 × 1080" },
  "2k": { label: "2K QHD", dimensions: "2560 × 1440" },
  "4k": { label: "4K UHD", dimensions: "3840 × 2160" },
};

const state = {
  appToken: "",
  appReady: false,
  ffmpegReady: false,
  frameExportReady: false,
  rotationReady: false,
  aiEnhanceReady: false,
  aiRuntime: {},
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
  modeClipButton: byId("mode-clip-button"),
  modeFramesButton: byId("mode-frames-button"),
  modeRotateButton: byId("mode-rotate-button"),
  modeEnhanceButton: byId("mode-enhance-button"),
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

function isEnhanceMode() {
  return state.operation === "enhance";
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
      || (state.aiEnhanceReady && !state.video?.is_hdr && selectedAiTarget()?.available),
  );
}

function selectDefaultAiTarget() {
  const targets = videoAiTargets();
  const current = targets.find((item) => item.target === state.enhanceTarget && item.available);
  state.enhanceTarget = current ? current.target : (targets.find((item) => item.available)?.target || "");
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
      ? `:${state.enhanceTarget}`
      : "";
  return `${state.video.id}:${state.operation}${option}:${range.start.toFixed(3)}:${range.end.toFixed(3)}`;
}

function rangeKey(range) {
  return range ? `${range.start.toFixed(3)}:${range.end.toFixed(3)}` : "";
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
    return selectedAiTarget()?.suggestedOutputName || `${stem}_ai_${state.enhanceTarget || "enhanced"}.mp4`;
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

function renderAiTargetOptions(controlsLocked = state.exporting) {
  const targets = videoAiTargets();
  const byTarget = new Map(targets.map((item) => [item.target, item]));
  const unavailable = [];

  for (const option of elements.aiTargetOptions) {
    const target = String(option.dataset.target || "").toLowerCase();
    const availability = byTarget.get(target);
    const hdrBlocked = Boolean(state.video?.is_hdr);
    const available = Boolean(state.video && state.aiEnhanceReady && !hdrBlocked && availability?.available);
    option.disabled = !available || controlsLocked || state.selectingVideo;
    option.setAttribute("aria-pressed", available && target === state.enhanceTarget ? "true" : "false");
    const reason = availability?.reason
      || (!state.video
        ? "请先选择视频"
        : !state.aiEnhanceReady
          ? "当前 Mac 无法使用 AI 超清"
          : hdrBlocked
            ? "HDR 视频暂不支持安全 AI 超清"
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

  const runtimeMessage = String(state.aiRuntime.message || state.aiRuntime.reason || "").trim();
  if (!state.video) {
    elements.aiTargetNote.textContent = "选择视频后会自动推荐第一个可用目标清晰度。";
  } else if (!state.aiEnhanceReady) {
    elements.aiTargetNote.textContent = runtimeMessage || "AI 超清仅支持 Apple Silicon Mac，当前运行环境不可用。";
  } else if (state.video.is_hdr) {
    elements.aiTargetNote.textContent = "当前是 HDR 视频；为避免破坏亮度、色彩和元数据，AI 超清已停用。";
  } else if (!state.enhanceTarget) {
    elements.aiTargetNote.textContent = unavailable.length
      ? `当前没有可用目标：${unavailable.join("；")}`
      : "当前视频没有可用的 AI 超清目标。";
  } else {
    const suffix = unavailable.length ? ` 其余目标不可用：${unavailable.join("；")}` : "";
    const warning = selectedAiTarget()?.warning ? ` 注意：${selectedAiTarget().warning}。` : "";
    elements.aiTargetNote.textContent = `将按原比例增强到 ${aiTargetLabel()} 边界，不拉伸画面。${warning}${suffix}`;
  }
}

function renderModeCopy() {
  const frames = isFrameMode();
  const rotate = isRotateMode();
  const enhance = isEnhanceMode();
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
    ? "选择 1080p、2K 或 4K，使用 SeedVR2 3B FP16 在这台 Mac 本地逐帧增强。质量优先，耗时可能很长。"
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
        ? "仍然只填起始时间和结束时间；修改后预览会自动更新。"
        : "只需填写起始时间和结束时间，预览会自动更新。";
  elements.durationLabel.textContent = enhance ? "目标清晰度" : rotate ? "旋转角度" : frames ? "截图时长" : "片段时长";
  elements.frameLimitHint.textContent = `逐帧截图一次最多 ${state.maxFrameSeconds} 秒；图片按视频原尺寸无损保存。`;
  elements.frameLimitHint.hidden = !frames;
  elements.exportHeading.textContent = enhance ? "确认并开始 AI 超清" : rotate ? "确认并永久旋转" : frames ? "确认并逐帧截图" : "确认并导出";
  elements.exportDescription.textContent = enhance
    ? "整段视频会在本机完成 AI 计算并保存为新文件；原视频不会被修改。"
    : rotate
      ? "生成在原视频同级目录；方向会真正写入画面，原视频不变。"
      : frames
        ? "截图会放进一个独立文件夹，原视频不会有任何变化。"
        : "导出为新文件，原视频不会有任何变化。";
  elements.qualityChipText.textContent = enhance
    ? "SeedVR2 · FP16 · 质量优先"
    : rotate
      ? "完整时长 · 原音频直拷"
      : frames
        ? "原始宽高 · 无损图片"
        : "保持原分辨率与高品质音频";
  elements.outputNameLabel.textContent = frames ? "文件夹名" : "文件名";
  elements.outputNameNote.textContent = enhance
    ? "10-bit HEVC；原音频直接复制；同名自动编号"
    : rotate
      ? "如遇同名文件会自动添加编号"
      : frames
        ? "图片按 frame_000001 开始顺序编号"
        : "如遇同名文件会自动添加编号";
  elements.destinationLabel.textContent = rotate ? "固定保存到原视频同级目录" : "保存到";
  elements.rotationNote.textContent = state.rotationDegrees === 360
    ? "360° 看起来方向不变，但仍会重新生成一份已固化、已清除旋转标记的新视频。"
    : state.rotationDegrees === 90 || state.rotationDegrees === 270
      ? "90° 和 270° 会交换画面宽高；帧率、色彩信息和原音频会尽量保持不变。"
      : "180° 不会交换画面宽高；帧率、色彩信息和原音频会尽量保持不变。";
  for (const option of elements.rotationOptions) {
    option.setAttribute("aria-pressed", Number(option.dataset.degrees) === state.rotationDegrees ? "true" : "false");
  }
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
      ? "已切换到 AI 超清，整段视频将使用 SeedVR2 3B FP16 处理"
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

function handleTimeInput() {
  if (!state.video || state.exporting || isRotateMode() || isEnhanceMode()) return;
  if (state.exportCompleted || state.exportError) resetExportResult();
  releaseGeneratedPreview();

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
  setButtonBusy(elements.selectVideoButton, true, "正在打开 Finder…");
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
    let end = suggestedEnd === null ? duration : suggestedEnd;
    if (isFrameMode()) end = Math.min(end, start + state.maxFrameSeconds, duration);
    elements.startTime.value = formatTime(start);
    elements.endTime.value = formatTime(end);

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
  if (!state.appReady || state.selectingDirectory || state.exporting || isRotateMode()) return;
  state.selectingDirectory = true;
  setButtonBusy(elements.selectDirectoryButton, true, "正在打开 Finder…");
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
  elements.selectDirectoryButton.hidden = isRotateMode();
  if (isRotateMode()) {
    const directory = state.video && state.video.directory_display;
    elements.outputDirectory.textContent = directory || "选择视频后自动使用同级目录";
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

function exportApiBase() {
  if (isFrameMode()) return "/api/frame-exports";
  if (isRotateMode()) return "/api/rotations";
  if (isEnhanceMode()) return "/api/ai-enhancements";
  return "/api/exports";
}

function setExportProgress(value) {
  const percentage = normalizeProgress(value);
  const rounded = Math.round(percentage);
  elements.exportProgressValue.textContent = `${rounded}%`;
  elements.exportProgressBar.style.width = `${percentage}%`;
  elements.exportProgressTrack.setAttribute("aria-valuenow", String(rounded));
}

function showExportProgress() {
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
    ? "首次使用会准备环境并下载约 7.3 GB 模型，之后开始整段 AI 计算。"
    : isRotateMode()
    ? "正在准备同级目录中的新视频，请不要关闭此页面。"
    : isFrameMode()
      ? "正在准备独立截图文件夹，请不要关闭此页面。"
      : "正在安全地创建新文件，请不要关闭此页面。";
  elements.exportElapsed.textContent = "";
  setExportProgress(0);
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

  try {
    const body = isEnhanceMode()
      ? { video_id: state.video.id, target: state.enhanceTarget }
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
    setExportProgress(progress);
    elements.exportElapsed.textContent = Number.isFinite(Number(job.elapsed_seconds))
      ? formatElapsed(job.elapsed_seconds)
      : "";

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
      const aiStageCopy = {
        setup: ["首次准备", "正在准备 AI 运行环境…", "正在安装本地 AI 运行环境。"],
        installing: ["首次准备", "正在安装 AI 依赖…", "正在安装本地 AI 运行环境。"],
        download: ["下载模型", "正在下载 SeedVR2 模型…", "正在下载并校验约 7.3 GB 的模型文件。"],
        downloading: ["下载模型", "正在下载 SeedVR2 模型…", "正在下载并校验约 7.3 GB 的模型文件。"],
        inference: ["AI 计算", `正在增强到 ${aiTargetLabel()}…`, "SeedVR2 正在逐帧恢复细节，耗时可能很长。"],
        remux: ["封装成片", "正在复制原音频…", "画面已完成，正在无损复制原视频音频。"],
        verify: ["检查成片", "正在验证输出文件…", "正在检查分辨率、时长、编码和音频。"],
        verifying: ["检查成片", "正在验证输出文件…", "正在检查分辨率、时长、编码和音频。"],
      };
      const aiCopy = aiStageCopy[stage] || ["本机处理中", "正在进行 AI 超清…", "SeedVR2 正在本机处理整段视频。"];
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
  const completedPath = job.output_path || (isRotateMode()
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
    ? "AI 超清完成，已保存为新视频"
    : isRotateMode()
    ? "永久旋转完成，已在原视频同级目录生成新文件"
    : isFrameMode()
      ? "逐帧截图完成，已保存到独立文件夹"
      : "剪辑完成，已保存为新文件");
  renderControls();
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
  if (job && Number.isFinite(Number(job.elapsed_seconds))) {
    elements.exportElapsed.textContent = formatElapsed(job.elapsed_seconds);
  }
  showToast(state.exportError, "error");
  renderControls();
}

async function cancelExport() {
  if (!state.exporting || !state.exportJobId || state.cancellingExport) return;
  state.cancellingExport = true;
  elements.cancelExportButton.disabled = true;
  elements.cancelExportButton.textContent = "正在取消…";
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
      ? "已在 Finder 中显示 AI 超清视频"
      : isRotateMode()
      ? "已在 Finder 中显示旋转后的视频"
      : isFrameMode()
        ? "已在 Finder 中显示截图文件夹"
        : "已在 Finder 中显示导出文件");
  } catch (error) {
    showToast(error.message || (isFrameMode()
      ? "无法在 Finder 中显示截图文件夹"
      : "无法在 Finder 中显示文件"), "error");
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
    ? state.aiEnhanceReady
    : isRotateMode()
    ? state.rotationReady
    : isFrameMode()
      ? state.frameExportReady
      : state.ffmpegReady;
  const destinationReady = isRotateMode() ? Boolean(state.video && state.video.directory_display) : Boolean(state.outputDirectory);
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
      && !state.exporting,
  );
}

function renderReadyNote(range) {
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
  } else if (isEnhanceMode() && !aiSelectionReady()) {
    if (state.video.is_hdr) elements.readyNoteText.textContent = "HDR 视频暂不支持安全 AI 超清，请改用 SDR 视频";
    else if (!state.aiEnhanceReady) {
      elements.readyNote.classList.add("is-error");
      elements.readyNoteText.textContent = String(state.aiRuntime.message || state.aiRuntime.reason || "AI 超清仅支持 Apple Silicon Mac，当前运行环境不可用");
    } else elements.readyNoteText.textContent = "当前视频没有可用目标；AI 超清不会把原片降级到更低分辨率";
  } else if (!range.valid) {
    elements.readyNoteText.textContent = "请先修正起始时间和结束时间";
  } else if (!state.previewReady) {
    elements.readyNoteText.textContent = "请等待新预览准备完成";
  } else if (!isRotateMode() && !state.outputDirectory) {
    elements.readyNoteText.textContent = "请选择一个保存目录";
  } else if (!(isEnhanceMode() ? state.aiEnhanceReady : isRotateMode() ? state.rotationReady : isFrameMode() ? state.frameExportReady : state.ffmpegReady)) {
    elements.readyNote.classList.add("is-error");
    elements.readyNoteText.textContent = isEnhanceMode()
      ? String(state.aiRuntime.message || state.aiRuntime.reason || "AI 超清仅支持 Apple Silicon Mac，当前运行环境不可用")
      : isRotateMode()
      ? "当前 FFmpeg 缺少永久旋转所需编码器"
      : isFrameMode()
        ? "当前 FFmpeg 缺少逐帧截图所需编码器"
        : "未检测到 FFmpeg，暂时无法导出";
  } else {
    elements.readyNote.classList.add("is-ready");
    elements.readyNoteText.textContent = isEnhanceMode()
      ? `已就绪，将用 SeedVR2 3B FP16 把整段视频增强到 ${aiTargetLabel()}`
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
  elements.modeEnhanceButton.disabled = controlsLocked || state.selectingVideo;
  for (const option of elements.rotationOptions) {
    option.disabled = !state.video || controlsLocked || state.selectingVideo;
  }
  elements.selectVideoButton.disabled = !state.appReady || state.selectingVideo || controlsLocked;
  elements.replaceVideoButton.disabled = !state.appReady || state.selectingVideo || controlsLocked;
  elements.startTime.disabled = !state.video || controlsLocked || state.selectingVideo || isRotateMode() || isEnhanceMode();
  elements.endTime.disabled = !state.video || controlsLocked || state.selectingVideo || isRotateMode() || isEnhanceMode();
  elements.selectDirectoryButton.disabled = isRotateMode() || !state.appReady || !state.video || state.selectingDirectory || controlsLocked;
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
  setPreviewStatus("", "等待选择视频");

  try {
    const result = await apiRequest("/api/bootstrap");
    state.appToken = result.app_token || "";
    state.appReady = Boolean(state.appToken);
    state.ffmpegReady = Boolean(result.ffmpeg_ready);
    state.frameExportReady = Boolean(result.frame_export_ready);
    state.rotationReady = Boolean(result.rotation_ready);
    state.aiEnhanceReady = Boolean(result.ai_enhance_ready);
    state.aiRuntime = result.ai_runtime && typeof result.ai_runtime === "object" ? result.ai_runtime : {};
    const maxFrameSeconds = Number(result.max_frame_seconds);
    state.maxFrameSeconds = Number.isFinite(maxFrameSeconds) && maxFrameSeconds > 0
      ? maxFrameSeconds
      : DEFAULT_MAX_FRAME_SECONDS;
    state.outputDirectory = normalizeDirectory(result.output_directory);

    const appName = result.app_name || "本地视频剪辑";
    elements.appName.textContent = appName;
    document.title = appName;
    elements.versionLabel.textContent = result.version ? `${appName} v${result.version}` : appName;
    renderOutputDirectory();
    renderModeCopy();

    if (!state.appToken) {
      showSystemBanner("本地服务响应异常", "请刷新页面；如果仍未恢复，请重新双击 start.command。", "error");
    } else if (!state.ffmpegReady && !state.frameExportReady && !state.rotationReady) {
      showSystemBanner("未检测到 FFmpeg", "可以先选择和预览视频，但需要按启动窗口提示安装 FFmpeg 后才能导出。", "warning");
    } else if (!state.ffmpegReady || !state.frameExportReady || !state.rotationReady) {
      showSystemBanner("部分功能暂不可用", "当前 FFmpeg 只支持页面中的部分操作；不可用的功能会在确认按钮旁提示。", "warning");
    } else {
      hideSystemBanner();
    }
  } catch (error) {
    state.appReady = false;
    elements.versionLabel.textContent = "本地服务未连接";
    showSystemBanner("无法连接本地服务", error.message || "请确认启动窗口仍在运行，然后刷新页面。", "error");
  } finally {
    renderControls();
  }
}

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

window.addEventListener("beforeunload", (event) => {
  if (!state.exporting) return;
  event.preventDefault();
  event.returnValue = "";
});

bootstrap();
