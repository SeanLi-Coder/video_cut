"use strict";

const PREVIEW_DEBOUNCE_MS = 500;
const EXPORT_POLL_MS = 650;
const MEDIA_LOAD_TIMEOUT_MS = 20000;
const DEFAULT_MAX_FRAME_SECONDS = 5;

const state = {
  appToken: "",
  appReady: false,
  ffmpegReady: false,
  frameExportReady: false,
  operation: "clip",
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
  frameLimitHint: byId("frame-limit-hint"),
  startTime: byId("start-time"),
  endTime: byId("end-time"),
  startTimeError: byId("start-time-error"),
  endTimeError: byId("end-time-error"),
  clipDuration: byId("clip-duration"),
  rangeReadout: byId("range-readout"),
  rangeTotal: byId("range-total"),
  previewPanel: byId("preview-panel"),
  previewVideo: byId("preview-video"),
  previewPlaceholder: byId("preview-placeholder"),
  previewLoading: byId("preview-loading"),
  previewLoadingTitle: byId("preview-loading-title"),
  previewStatusDot: byId("preview-status-dot"),
  previewStatusText: byId("preview-status-text"),
  retryPreviewButton: byId("retry-preview-button"),
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
  if (!isClipPreview(state.previewMode)) return;
  elements.previewVideo.pause();
  elements.previewVideo.removeAttribute("src");
  elements.previewVideo.load();
  state.previewUrl = "";
}

function schedulePreview(range) {
  cancelPendingPreview();
  if (!range.valid || !state.video) return;

  if (rangeKey(range) === rangeKey(state.activeRange) && state.previewReady) {
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
        operation: state.operation,
      },
      controller.signal,
    );
    if (requestSerial !== state.previewRequestSerial) return;
    if (!result.preview_url) throw new Error("本地服务没有返回预览地址");

    const generation = result.generation ?? result.generation_id ?? "";
    if (result.suggested_output_name) {
      elements.suggestedOutputName.textContent = result.suggested_output_name;
    }
    await loadMedia(result.preview_url, result.mode || "proxy", range, {
      autoplay: options.autoplay !== false,
      generation,
    });
    if (requestSerial !== state.previewRequestSerial) return;

    state.previewReady = true;
    state.mediaFallbackAttempted = isClipPreview(result.mode || "proxy");
    const readyMessage = state.video.is_hdr
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

function renderModeCopy() {
  const frames = isFrameMode();
  elements.modeClipButton.setAttribute("aria-pressed", frames ? "false" : "true");
  elements.modeFramesButton.setAttribute("aria-pressed", frames ? "true" : "false");
  elements.pageTitle.textContent = frames ? "把这一小段逐帧保存" : "留下想要的这一段";
  elements.heroCopy.textContent = frames
    ? `选好不超过 ${state.maxFrameSeconds} 秒的范围，预览满意后，把其中每一帧按原始尺寸保存成无损图片。`
    : "选一个视频，填好起点和终点，预览满意后直接保存。原视频始终不会被修改。";
  elements.journeyRangeLabel.textContent = frames ? "设置范围" : "设置片段";
  elements.journeyExportLabel.textContent = frames ? "确认截图" : "确认导出";
  elements.trimHeading.textContent = frames ? "设置截图范围" : "设置剪辑范围";
  elements.trimDescription.textContent = frames
    ? "仍然只填起始时间和结束时间；修改后预览会自动更新。"
    : "只需填写起始时间和结束时间，预览会自动更新。";
  elements.durationLabel.textContent = frames ? "截图时长" : "片段时长";
  elements.frameLimitHint.textContent = `逐帧截图一次最多 ${state.maxFrameSeconds} 秒；图片按视频原尺寸无损保存。`;
  elements.frameLimitHint.hidden = !frames;
  elements.exportHeading.textContent = frames ? "确认并逐帧截图" : "确认并导出";
  elements.exportDescription.textContent = frames
    ? "截图会放进一个独立文件夹，原视频不会有任何变化。"
    : "导出为新文件，原视频不会有任何变化。";
  elements.qualityChipText.textContent = frames
    ? "原始宽高 · 无损图片"
    : "保持原分辨率与高品质音频";
  elements.outputNameLabel.textContent = frames ? "文件夹名" : "文件名";
  elements.outputNameNote.textContent = frames
    ? "图片按 frame_000001 开始顺序编号"
    : "如遇同名文件会自动添加编号";
  if (!state.exportCompleted) {
    elements.completionSummary.textContent = frames
      ? "截图完成，原视频未被修改"
      : "剪辑完成，原视频未被修改";
  }
  elements.continueButton.textContent = frames ? "继续截图" : "继续剪辑";
}

function changeOperation(operation) {
  if (
    state.exporting
    || state.selectingVideo
    || !["clip", "frames"].includes(operation)
    || operation === state.operation
  ) {
    return;
  }
  cancelPendingPreview();
  stopExportPolling();
  resetExportResult();
  state.operation = operation;

  renderModeCopy();
  const range = readRange(true);
  renderRangeSummary(range);
  if (range.valid && state.video) {
    if (rangeKey(range) === rangeKey(state.activeRange) && state.previewReady) {
      setPreviewStatus("ready", "预览范围已就绪");
    } else {
      releaseGeneratedPreview();
      schedulePreview(range);
    }
  } else if (state.video) {
    state.previewReady = false;
    setPreviewStatus("error", "请修正时间后更新预览");
  }
  renderControls();
  showToast(isFrameMode() ? `已切换到逐帧截图，单次最多 ${state.maxFrameSeconds} 秒` : "已切换到视频剪辑");
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
  if (!state.video || state.exporting) return;
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
  const value = parseTime(event.currentTarget.value);
  if (value !== null) event.currentTarget.value = formatTime(value);
}

function handleTimeKeydown(event) {
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
  if (!state.appReady || state.selectingDirectory || state.exporting) return;
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
  return isFrameMode() ? "/api/frame-exports" : "/api/exports";
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
  elements.cancelExportButton.textContent = isFrameMode() ? "取消截图" : "取消导出";
  elements.exportProgressKicker.textContent = "正在准备";
  elements.exportProgressTitle.textContent = isFrameMode() ? "正在逐帧截图…" : "正在导出视频…";
  elements.exportProgressMessage.textContent = isFrameMode()
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
    const result = await post(exportApiBase(), {
      video_id: state.video.id,
      start: range.start,
      end: range.end,
    });
    if (!result.job_id) throw new Error("本地服务没有创建导出任务");
    state.exportJobId = result.job_id;
    state.exportOutputName = result.output_name || makeSuggestedOutputName(range);
    await pollExport(pollSerial);
  } catch (error) {
    if (pollSerial !== state.exportPollSerial) return;
    finishExportWithError(error.message || (isFrameMode() ? "截图任务启动失败" : "导出任务启动失败"));
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
    const progress = normalizeProgress(job.progress);
    setExportProgress(progress);
    elements.exportElapsed.textContent = Number.isFinite(Number(job.elapsed_seconds))
      ? formatElapsed(job.elapsed_seconds)
      : "";

    if (["queued", "pending", "waiting"].includes(status)) {
      elements.exportProgressKicker.textContent = "等待处理";
      elements.exportProgressTitle.textContent = isFrameMode() ? "正在准备截图…" : "正在准备导出…";
      elements.exportProgressMessage.textContent = job.message || "正在检查视频和保存位置。";
      scheduleExportPoll(pollSerial, 800);
      return;
    }

    if (["running", "processing", "exporting"].includes(status)) {
      elements.exportProgressKicker.textContent = "本机处理中";
      elements.exportProgressTitle.textContent = isFrameMode() ? "正在逐帧截图…" : "正在导出视频…";
      elements.exportProgressMessage.textContent = job.message || (isFrameMode()
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
        job.error || job.message || (isFrameMode() ? "截图失败，请重试。" : "导出失败，请重试。"),
        job,
      );
      return;
    }

    elements.exportProgressMessage.textContent = job.message || "正在处理，请稍候。";
    scheduleExportPoll(pollSerial);
  } catch (error) {
    if (pollSerial !== state.exportPollSerial) return;
    state.exportPollFailures += 1;
    if (state.exportPollFailures <= 4) {
      elements.exportProgressMessage.textContent = "连接短暂中断，正在重新连接本地服务…";
      scheduleExportPoll(pollSerial, Math.min(3000, 700 * state.exportPollFailures));
      return;
    }
    finishExportWithError(error.message || "无法获取导出进度");
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
  state.exportOutputName = job.output_name || state.exportOutputName || (isFrameMode() ? "逐帧截图" : "剪辑视频");

  setExportProgress(100);
  elements.exportProgressPanel.hidden = true;
  elements.exportSetup.hidden = true;
  elements.exportCompletePanel.hidden = false;
  elements.completedOutputName.textContent = state.exportOutputName;
  elements.completedOutputPath.textContent = job.output_path || state.outputDirectory;
  elements.completedOutputPath.title = job.output_path || state.outputDirectory;
  const frameCount = Number(job.frame_count);
  elements.completionSummary.textContent = isFrameMode()
    ? Number.isFinite(frameCount) && frameCount > 0
      ? `截图完成，共保存 ${frameCount} 张原尺寸图片`
      : "截图完成，原视频未被修改"
    : "剪辑完成，原视频未被修改";
  showToast(isFrameMode() ? "逐帧截图完成，已保存到独立文件夹" : "剪辑完成，已保存为新文件");
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
  showToast(isFrameMode() ? "已取消截图，未完成图片已清理" : "已取消导出，原视频未被修改");
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
  elements.exportProgressKicker.textContent = isFrameMode() ? "截图未完成" : "导出未完成";
  elements.exportProgressTitle.textContent = isFrameMode() ? "截图失败" : "导出失败";
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
  elements.exportProgressMessage.textContent = isFrameMode()
    ? "正在安全停止截图并清理未完成图片…"
    : "正在安全停止导出并清理未完成文件…";

  try {
    await post(`${exportApiBase()}/${encodeURIComponent(state.exportJobId)}/cancel`);
    scheduleExportPoll(state.exportPollSerial, 150);
  } catch (error) {
    state.cancellingExport = false;
    elements.cancelExportButton.disabled = false;
    elements.cancelExportButton.textContent = isFrameMode() ? "取消截图" : "取消导出";
    showToast(error.message || "取消失败，请稍后重试", "error");
  }
}

async function revealOutput() {
  if (!state.exportJobId) return;
  elements.revealOutputButton.disabled = true;
  try {
    await post(`${exportApiBase()}/${encodeURIComponent(state.exportJobId)}/reveal`);
    showToast(isFrameMode() ? "已在 Finder 中显示截图文件夹" : "已在 Finder 中显示导出文件");
  } catch (error) {
    showToast(error.message || (isFrameMode() ? "无法在 Finder 中显示截图文件夹" : "无法在 Finder 中显示文件"), "error");
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
  elements.startTime.focus();
  elements.trimCard.scrollIntoView({ behavior: "smooth", block: "center" });
}

function canExport(range = readRange(false)) {
  const exporterReady = isFrameMode() ? state.frameExportReady : state.ffmpegReady;
  return Boolean(
    state.appReady
      && exporterReady
      && state.video
      && range.valid
      && state.previewReady
      && state.outputDirectory
      && !state.selectingVideo
      && !state.selectingDirectory
      && !state.exporting,
  );
}

function renderReadyNote(range) {
  elements.readyNote.classList.remove("is-ready", "is-error");
  if (state.exportError) {
    elements.readyNote.classList.add("is-error");
    elements.readyNoteText.textContent = isFrameMode()
      ? "上次截图未完成，检查提示后可重试"
      : "上次导出未完成，检查提示后可重试";
  } else if (!state.appReady) {
    elements.readyNoteText.textContent = "正在连接本地服务…";
  } else if (!state.video) {
    elements.readyNoteText.textContent = isFrameMode()
      ? "请先选择视频并设置截图范围"
      : "请先选择视频并设置剪辑范围";
  } else if (!range.valid) {
    elements.readyNoteText.textContent = "请先修正起始时间和结束时间";
  } else if (!state.previewReady) {
    elements.readyNoteText.textContent = "请等待新预览准备完成";
  } else if (!state.outputDirectory) {
    elements.readyNoteText.textContent = "请选择一个保存目录";
  } else if (!(isFrameMode() ? state.frameExportReady : state.ffmpegReady)) {
    elements.readyNote.classList.add("is-error");
    elements.readyNoteText.textContent = isFrameMode()
      ? "当前 FFmpeg 缺少逐帧截图所需编码器"
      : "未检测到 FFmpeg，暂时无法导出";
  } else {
    elements.readyNote.classList.add("is-ready");
    elements.readyNoteText.textContent = isFrameMode()
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
  if (!range.valid || !state.previewReady) {
    elements.journeyTrim.classList.add("is-active");
    return;
  }

  elements.journeyTrim.classList.add("is-complete");
  if (state.exportCompleted) elements.journeyExport.classList.add("is-complete");
  else elements.journeyExport.classList.add("is-active");
}

function renderCards(range) {
  elements.sourceCard.classList.toggle("is-active", !state.video);
  elements.trimCard.classList.toggle("is-disabled", !state.video);
  elements.trimCard.classList.toggle("is-active", Boolean(state.video && (!range.valid || !state.previewReady)));
  elements.trimCard.setAttribute("aria-disabled", state.video ? "false" : "true");
  elements.exportCard.classList.toggle("is-disabled", !state.video || !range.valid || !state.previewReady);
  elements.exportCard.classList.toggle("is-active", Boolean(state.video && range.valid && state.previewReady && !state.exportCompleted));
  elements.exportCard.setAttribute("aria-disabled", state.video && range.valid && state.previewReady ? "false" : "true");
}

function renderControls() {
  const range = readRange(false);
  const controlsLocked = state.exporting;

  elements.modeClipButton.disabled = controlsLocked || state.selectingVideo;
  elements.modeFramesButton.disabled = controlsLocked || state.selectingVideo;
  elements.selectVideoButton.disabled = !state.appReady || state.selectingVideo || controlsLocked;
  elements.replaceVideoButton.disabled = !state.appReady || state.selectingVideo || controlsLocked;
  elements.startTime.disabled = !state.video || controlsLocked || state.selectingVideo;
  elements.endTime.disabled = !state.video || controlsLocked || state.selectingVideo;
  elements.selectDirectoryButton.disabled = !state.appReady || !state.video || state.selectingDirectory || controlsLocked;
  elements.exportButton.disabled = !canExport(range);
  elements.exportButton.setAttribute("aria-busy", state.exporting ? "true" : "false");

  const exportLabel = elements.exportButton.querySelector(".button-label");
  if (exportLabel) {
    exportLabel.textContent = state.exportError
      ? isFrameMode()
        ? "重新截图"
        : "重新导出"
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
    } else if (!state.ffmpegReady) {
      showSystemBanner("未检测到 FFmpeg", "可以先选择和预览视频，但需要按启动窗口提示安装 FFmpeg 后才能导出。", "warning");
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
elements.selectDirectoryButton.addEventListener("click", selectOutputDirectory);
elements.exportButton.addEventListener("click", startExport);
elements.cancelExportButton.addEventListener("click", cancelExport);
elements.revealOutputButton.addEventListener("click", revealOutput);
elements.continueButton.addEventListener("click", continueEditing);
elements.reloadButton.addEventListener("click", () => window.location.reload());
elements.retryPreviewButton.addEventListener("click", () => {
  const range = readRange(true);
  if (range.valid) requestPreview(range, { fallback: true, autoplay: true });
});

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
elements.previewVideo.addEventListener("error", () => {
  if (state.mediaLoading || !state.video || !state.activeRange) return;
  if (!state.mediaFallbackAttempted && !isClipPreview(state.previewMode)) {
    state.mediaFallbackAttempted = true;
    requestPreview({ ...state.activeRange, valid: true }, { fallback: true, autoplay: false });
  } else {
    state.previewReady = false;
    setPreviewLoading(false);
    setPreviewStatus("error", "浏览器无法播放这个预览", true);
    renderControls();
  }
});

window.addEventListener("beforeunload", (event) => {
  if (!state.exporting) return;
  event.preventDefault();
  event.returnValue = "";
});

bootstrap();
